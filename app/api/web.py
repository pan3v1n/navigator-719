"""Веб-страницы UI 1.0 (Jinja2): вход, чат, приём формы обратной связи.

Страницы при отсутствии сессии РЕДИРЕКТЯТ на /login (не 401 — 401 только у /api/*, их ловит JS).
Шаблоны — app/web/templates, статика монтируется в main.py.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel

from app.api.auth import (
    authenticate,
    clear_remember_cookie,
    current_user,
    login_session,
    logout_session,
    needs_profile,
    require_admin,
    require_user_profiled,
    set_remember_cookie,
)
from app.api import quota, trial
from app.api.admin_stats import build_admin_view, system_health
from app.api.leads import LIMITS as LEAD_LIMITS
from app.api.leads import _EMAIL_RE as EMAIL_RE
from app.api.leads import _INN_RE as INN_RE
from app.api.leads import _digits as digits
from app.api.ratelimit import SlidingWindow, client_ip
from app.core import plans
from app.core.config import settings
from app.core.plans import PLAN_LIMITS
from app.core.release import release_label
from app.core.prompts import EXPERT_DISCLAIMER
from app.core.regions import REGIONS, region_from_username
from app.rag import followup
from app.rag.edition import corpus_edition, corpus_line, kontur_719_url
from app.db import queries as q
from app.db.engine import get_session
from app.db.models import User

router = APIRouter()
_WEB = Path(__file__).resolve().parents[1] / "web"
templates = Jinja2Templates(directory=str(_WEB / "templates"))
# Разряды числа с неразрывным пробелом (12345 → «12 345») — для читабельных токенов/₽ в админке.
templates.env.filters["spaced"] = lambda n: f"{int(n or 0):,}".replace(",", " ")


def plural(n: int, one: str, few: str, many: str) -> str:
    """Форма слова по числу: 1 вопрос, 3 вопроса, 5 вопросов (11–14 — «многие»)."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


templates.env.filters["plural"] = plural


@lru_cache(maxsize=1)
def asset_version() -> str:
    """Версия статики для ссылок `?v=` — хеш содержимого `style.css`, `chat.js` и фавикона.

    ⚠ Без неё выкатка переоформления ломала бы интерфейс на часы: у статики нет `Cache-Control`,
    и браузер кэширует её ЭВРИСТИЧЕСКИ (доля возраста по Last-Modified) — новая разметка чата
    приходила бы со старыми скриптом и стилями. Замечено 08.10.2026 на локальном прогоне
    макета: headless Chrome показал новую кнопку в стилях прошлой сборки. Хеш, а не тег релиза:
    меняется ровно тогда, когда меняются файлы."""
    h = hashlib.sha1()
    for name in ("style.css", "chat.js", "favicon.svg"):
        try:
            h.update((_WEB / "static" / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


def _ctx(request: Request, **kw) -> dict:
    # E1: редакция корпуса — во ВСЕ страницы. Выводится из самого текста постановления
    # (app/rag/edition.py), поэтому не может разойтись с базой молча.
    # Метка релиза — по той же причине, что и редакция корпуса: одно определение на все
    # страницы. `APP_VERSION` для этого не годится (держится на 0.5.0 до приёмки), см.
    # `app/core/release.py`.
    # landing_url — адрес лендинга (тарифы, заявка на подключение) из того же PUBLIC_DOMAIN, на котором
    # caddy его обслуживает; без домена (разработка) ссылки на лендинг не рисуются вовсе.
    landing = f"https://{settings.PUBLIC_DOMAIN}" if settings.PUBLIC_DOMAIN else ""
    return {"request": request, "app_title": settings.APP_TITLE, "org": settings.ORG_NAME,
            "corpus_edition": corpus_edition(), "release_label": release_label(),
            "landing_url": landing, "asset_v": asset_version(),
            # пробный режим без входа — ссылка на него со страницы входа
            "guest_trial": settings.GUEST_TRIAL_ENABLED,
            "guest_questions": settings.GUEST_TRIAL_QUESTIONS,
            # демо-чат страницы входа (и её ре-рендера после ошибки) — без служебных полей сверки
            "login_demo": [{k: d[k] for k in ("q", "a", "ref")} for d in LOGIN_DEMO], **kw}


def _parse_date(s: str):
    """'YYYY-MM-DD' → date | None (фильтры периода в /admin). Некорректная строка → None."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


def _download(content: str, media_type: str, filename: str) -> Response:
    """Ответ-файл (attachment). Мелкий локальный хелпер — чтобы не тянуть тяжёлый chat.py в web.py."""
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _json_download(payload: dict, filename: str) -> Response:
    return _download(json.dumps(payload, ensure_ascii=False, indent=2),
                     "application/json", filename)


@router.get("/", response_class=HTMLResponse)
def root(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if needs_profile(user):  # роль user без профиля/согласия → сначала анкета
        return RedirectResponse("/profile", status_code=302)
    # Все роли (вкл. admin) открываются на «главной» — чат с вводом. Дашборд `/admin`
    # доступен админу из сайдбара («Логи диалогов»), но не как стартовая страница.
    return RedirectResponse("/chat", status_code=302)


# Демо-чат на синей панели входа — анимация как на первом экране лендинга (просьба владельца
# 09.10.2026). ⚠ Страница публичная, и каждый пример ссылается на пункт: ответ обязан стоять на
# тексте этого пункта. Поэтому у примера записаны источник и ДОСЛОВНАЯ цитата — их сверяет тест с
# корпусом (`tests.test_auth.TestLoginDemo`). Примеры лендинга сюда не скопированы как есть: два из
# четырёх (перечень документов к заявке, «через территориальные палаты») в цитируемых пунктах не
# подтверждаются. Первый пример — тот, что стоял на странице статично и уже сверен с п. 7 Правил.
LOGIN_DEMO = [
    {"q": "Кто выдаёт акт экспертизы для подтверждения производства?",
     "a": "Заявки для выдачи акта экспертизы рассматривает Торгово-промышленная палата Российской "
          "Федерации — в порядке, определённом ею по согласованию с Минпромторгом России.",
     "ref": "п. 7 Правил",
     "src": "pp719_full.txt", "start": "\n7. Заявки на включение сведений в реестр",
     "quote": "рассматриваются Торгово-промышленной палатой Российской Федерации в порядке, "
              "определенном ею по согласованию с Министерством промышленности и торговли"},
    {"q": "Куда подаётся заявка на получение акта экспертизы?",
     "a": "В уполномоченную ТПП: заявитель подаёт заявку на включение сведений в реестр в порядке, "
          "предусмотренном разделом 5 Положения.",
     "ref": "Приказ ТПП РФ № 52, п. 4.1",
     "src": "prikaz52_tpp_full.txt", "start": "\n4.1. ",
     "quote": "заявитель подает в уполномоченную ТПП в порядке, предусмотренном разделом 5 "
              "настоящего Положения, заявку на включение сведений в реестр"},
    {"q": "Какие требования применяются к металлообрабатывающим станкам?",
     "a": "Станки входят в раздел I приложения — «Продукция станкоинструментальной "
          "промышленности». Требования балльные: например, наличие управляющего "
          "программно-аппаратного комплекса, произведённого в России, даёт 25 баллов.",
     "ref": "Приложение, разд. I",
     "src": "chunks/02_I_stankoinstrument.txt", "start": "\n28.41.1|Станки для обработки металлов",
     "quote": "наличие управляющего программно-аппаратного комплекса, произведенного на "
              "территории Российской Федерации (25 баллов)"},
    {"q": "Где размещаются выданные акты экспертизы?",
     "a": "В ГИСП: акты экспертизы выдаются с использованием ГИСП, и выданные акты размещаются "
          "в этой системе.",
     "ref": "Приказ ТПП РФ № 52, п. 3.5",
     "src": "prikaz52_tpp_full.txt", "start": "\n3.5. ",
     "quote": "Выданные акты экспертизы, сертификаты о происхождении товара (продукции), акты о "
              "проведении оценки и акты экспертизы на компоненты размещаются в указанной "
              "информационной системе"},
]


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, trial_over: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=302)
    # trial_over — гость исчерпал пробные вопросы (фронт ведёт сюда по 402 пробного режима)
    return templates.TemplateResponse("login.html", _ctx(request, error=None,
                                                         trial_over=bool(trial_over)))


# --------------------------------------------------------------------------- #
# Правовые и справочные страницы
#
# ПУБЛИЧНЫЕ, без входа: их читают ДО того, как согласиться. Политику конфиденциальности,
# спрятанную за авторизацией, невозможно прочитать перед тем, как дать согласие в профиле, —
# а согласие даётся именно на её условиях. Дата редакции задаётся здесь и показывается на
# странице: молча меняющийся правовой документ хуже отсутствующего.
# --------------------------------------------------------------------------- #
DOCS_UPDATED = "09.10.2026"  # кабинет: должность, email, телефон, организация и ИНН в политике


def _doc(request: Request, template: str, page_title: str, active: str) -> HTMLResponse:
    return templates.TemplateResponse(template, _ctx(
        request, page_title=page_title, active=active, updated=DOCS_UPDATED,
        user=current_user(request)))


@router.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return _doc(request, "terms.html", "Условия использования", "terms")


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return _doc(request, "privacy.html", "Политика конфиденциальности", "privacy")


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    return _doc(request, "help.html", "Справочный центр", "help")


# R12: троттлинг входа по IP. bcrypt замедляет перебор, но не останавливает его, а пароли у нас
# 8-символьные и розданы людям. Ключ — адрес клиента; ⚠ когда перед приложением встанет обратный
# прокси (R11, TLS), сюда попадёт адрес прокси — тогда брать X-Forwarded-For.
_login_limit = SlidingWindow(settings.RATE_LIMIT_LOGIN_PER_MIN, window=60.0)


_client_ip = client_ip  # одно определение с ограничителем заявок (app/api/ratelimit.py)


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 remember: str = Form(default="")):
    ip = _client_ip(request)
    if not _login_limit.check(f"login:{ip}"):
        retry = _login_limit.retry_after(f"login:{ip}")
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, error=f"Слишком много попыток входа. Повторите через {retry} с."),
            status_code=429, headers={"Retry-After": str(retry)},
        )
    user = authenticate(username.strip(), password)
    if not user:
        return templates.TemplateResponse(
            "login.html", _ctx(request, error="Неверный логин или пароль"), status_code=401
        )
    # Успешный вход снимает счётчик: человек, вспомнивший пароль с 9-й попытки, не должен
    # оставаться под лимитом — он бьёт по перебору, а не по забывчивости.
    _login_limit.reset(f"login:{ip}")
    login_session(request, user)
    resp = RedirectResponse("/", status_code=302)
    if remember:  # «Запомнить меня» → персистентный cookie автовхода
        set_remember_cookie(resp, user.id)
    return resp


@router.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    logout_session(request)
    resp = RedirectResponse("/login", status_code=302)
    clear_remember_cookie(resp)  # выход снимает и автовход
    return resp


def _guest_chat_page(request: Request) -> HTMLResponse:
    """Пробный режим (09.10.2026): тот же чат без входа. Кука гостя ставится здесь, при первом
    открытии страницы, — API без неё не отвечает (`app/api/guest.py`)."""
    gid = trial.guest_id(request)
    fresh = gid is None
    if fresh:
        gid = trial.new_guest_id()
    with get_session() as db:
        tv = trial.trial_view(db, gid)
    resp = templates.TemplateResponse(
        "chat.html", _ctx(request, user=None, guest=tv, kontur_719_url=kontur_719_url(),
                          input_hint=followup.START_HINT, quota=None))
    if fresh:
        trial.set_guest_cookie(resp, gid)
    return resp


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    user = current_user(request)
    if not user:
        if settings.GUEST_TRIAL_ENABLED:
            return _guest_chat_page(request)
        return RedirectResponse("/login", status_code=302)
    if needs_profile(user):  # жёсткий гейт: роль user не в чат, пока не заполнит профиль+согласие
        return RedirectResponse("/profile", status_code=302)
    # kontur_719_url — адрес первоисточника в редакции КОРПУСА (у Контура documentId свой на каждую
    # редакцию). Отдаём фронту отсюда, чтобы константа была одна и не разъезжалась с базой.
    # input_hint — стартовая подсказка поля ввода (U5). По той же причине отдаётся сервером:
    # дальше её меняет ответ движка, и две копии текста разъехались бы при первой же правке.
    # quota — расход тарифа (Б4) для первого рендера; дальше его перечитывает фронт (`/api/quota`).
    with get_session() as db:
        qv = quota.quota_view(db, user)
    return templates.TemplateResponse(
        "chat.html", _ctx(request, user=user, kontur_719_url=kontur_719_url(),
                          input_hint=followup.START_HINT, quota=qv))


# --------------------------------------------------------------------------- #
# Личный кабинет (макет «Navigator 719 Service», экран account; решения владельца 09.10.2026)
#
# Две вкладки: «Профиль» и «Тариф». «Оплата» и «Документы» макета не показываются, пока нет
# биллинга — иначе на странице были бы выдуманные счета и договоры. Вкладка — параметр `tab`
# серверного рендера, а не скрипт: страница работает и без JS, и ссылку на тариф можно дать.
# --------------------------------------------------------------------------- #
PROFILE_TABS = ("profile", "plan")
# Длины и правила контактов — те же, что у формы заявки лендинга (`app/api/leads.py`): одно
# определение на сервис, иначе email, принятый в заявке, кабинет отверг бы (и наоборот).
CONTACT_LIMITS = {"position": 128, "email": LEAD_LIMITS["email"], "phone": LEAD_LIMITS["phone"]}
PROFILE_LIMITS = {"full_name": 200, "telegram": 128}   # telegram = колонке `User.telegram`
ORG_LIMIT = LEAD_LIMITS["org"]


def _profile_form(user: User, **over) -> dict:
    """Значения полей формы: из учётки (GET) или из присланного (ре-рендер ошибки POST)."""
    form = {"full_name": user.full_name or "", "telegram": user.telegram or "",
            "region": user.region or region_from_username(user.username),
            "position": user.position or "", "email": user.email or "", "phone": user.phone or "",
            "consent": bool(user.consent)}
    form.update(over)
    return form


def _profile_response(request: Request, user: User, form: dict, *, tab: str = "profile",
                      saved: bool = False, error: str | None = None, status_code: int = 200):
    """Страница кабинета. Расход тарифа считается один раз и нужен обеим вкладкам (в шапке —
    название тарифа). can_leave — профиль уже заполнен; пока нет, уйти некуда: чат вернёт сюда
    же (`needs_profile`), поэтому и «Вернуться к сервису» не рисуется."""
    with get_session() as db:
        pv = quota.plan_view(db, user)
    return templates.TemplateResponse(
        "profile.html",
        _ctx(request, user=user, form=form, regions=REGIONS, error=error, saved=saved,
             tab=tab if tab in PROFILE_TABS else "profile", plan=pv,
             quota=pv["quota"] if pv else None,
             can_leave=not needs_profile(user)),
        status_code=status_code,
    )


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, tab: str = "profile", saved: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _profile_response(request, user, _profile_form(user), tab=tab, saved=bool(saved))


def _contact_errors(position: str, email: str, phone: str) -> list[str]:
    """Необязательные контакты: пустое — можно, заполненное — проверяем как в заявке лендинга."""
    errs = []
    if len(position) > CONTACT_LIMITS["position"]:
        errs.append("Слишком длинное название должности.")
    if email and (not EMAIL_RE.match(email) or len(email) > CONTACT_LIMITS["email"]):
        errs.append("Проверьте правильность email.")
    if phone and (len(digits(phone)) != 11 or len(phone) > CONTACT_LIMITS["phone"]):
        errs.append("Укажите телефон полностью — 11 цифр, например +7 900 000-00-00.")
    return errs


@router.post("/profile", response_class=HTMLResponse)
def profile_submit(
    request: Request,
    consent: str = Form(default=""),
    full_name: str = Form(default=""),
    region: str = Form(default=""),
    telegram: str = Form(default=""),
    position: str = Form(default=""),
    email: str = Form(default=""),
    phone: str = Form(default=""),
):
    """Сохранение профиля. Организацию и ИНН форма не принимает вовсе: их ведёт admin
    (`admin_set_org`), присланные в форме поля `org`/`inn` отбрасываются ещё на разборе."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fn, rg, tg = full_name.strip(), region.strip(), telegram.strip()
    pos, em, ph = position.strip(), email.strip(), phone.strip()
    form = _profile_form(user, full_name=fn, region=rg or region_from_username(user.username),
                         telegram=tg, position=pos, email=em, phone=ph, consent=bool(consent))
    # ФИО, регион, Telegram и согласие обязательны (решение заказчика: жёсткий гейт), контакты —
    # нет. Ошибка → ре-рендер с введённым, а не потеря формы.
    errs = []
    if not (consent and fn and rg and tg):
        errs.append("Заполните ФИО, регион и Telegram и подтвердите согласие на обработку "
                    "персональных данных.")
    # Регион — только из справочника: поле с поиском выбирает из него, но форму можно прислать и
    # в обход страницы, а по региону строятся срезы админки.
    if rg and rg not in REGIONS:
        errs.append("Выберите регион из списка.")
    # Длины — по колонкам: SQLite их не держит, PostgreSQL (цель прода) ответил бы 500.
    if len(fn) > PROFILE_LIMITS["full_name"] or len(tg) > PROFILE_LIMITS["telegram"]:
        errs.append("Слишком длинное ФИО или ник в Telegram.")
    errs += _contact_errors(pos, em, ph)
    if errs:
        return _profile_response(request, user, form, error=" ".join(errs), status_code=400)
    first_fill = needs_profile(user)
    with get_session() as db:
        q.update_profile(db, user.id, full_name=fn, region=rg, telegram=tg, consent=True,
                         position=pos, email=em, phone=ph)
    # Первое заполнение — это гейт до чата: дальше сразу в работу. Правка в кабинете — остаёмся
    # на месте с «Изменения сохранены», как в макете (PRG: обновление страницы не шлёт форму снова).
    return RedirectResponse("/chat" if first_fill else "/profile?saved=1", status_code=302)


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, date_from: str = "", date_to: str = "",
               region: str = "", role: str = ""):
    """Админ-панель: приёмочный скоркард, срез по регионам, триаж плохих ответов, логи.
    Фильтры (query, опц.): date_from/date_to (YYYY-MM-DD), region, role. Только для admin.
    Вся сборка данных — в app/api/admin_stats.build_admin_view (тестируемо + переиспользует экспорт)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "admin":
        return RedirectResponse("/chat", status_code=302)  # эксперт логи не видит
    with get_session() as db:
        view = build_admin_view(
            db, date_from=_parse_date(date_from), date_to=_parse_date(date_to),
            region=region, role=role,
        )
        # Заявки с лендинга — ПДн, поэтому только здесь, за ролью admin. Просроченные (срок из
        # политики ПДн) удаляются при каждом открытии; на страницу — не больше LEADS_ON_PAGE.
        q.purge_old_leads(db)
        leads = q.list_leads(db, limit=LEADS_ON_PAGE)
        leads_total = q.count_leads(db)
        plan_rows = _plan_rows(db)
        q.purge_old_guest_messages(db)  # срок хранения — обещание политики, как у заявок
        guest_view = _guest_view(db)
    return templates.TemplateResponse(
        "admin.html", _ctx(request, admin=user, health=system_health(), leads=leads,
                           leads_total=leads_total, plan_rows=plan_rows, guest_view=guest_view,
                           paid_plans=list(PLAN_LIMITS), internal_plans=list(plans.INTERNAL_PLANS),
                           trial_plan=plans.TRIAL_PLAN, trial_days=plans.TRIAL_DAYS,
                           today=plans.msk_today().isoformat(), **view))


LEADS_ON_PAGE = 200
GUEST_ON_PAGE = 400  # реплик пробного режима на вкладке (≈ 200 вопросов с ответами)


def _guest_view(db) -> dict:
    """Вкладка «Пробный режим»: расход за сутки против общего потолка и последние вопросы гостей
    с ответами. Гости обезличены — показываем только текст, время и токены."""
    day_start = plans.anchor_from_date(plans.msk_today())
    rows, open_q = [], {}
    for m in reversed(q.list_guest_messages(db, limit=GUEST_ON_PAGE)):   # по времени
        if m.role == "user":
            row = {"ts": f"{plans.naive_utc(m.ts) + plans.MSK_OFFSET:%d.%m %H:%M}",
                   "question": m.content, "answer": "", "tokens": 0, "charged": False}
            rows.append(row)
            open_q[m.session_id] = row
        elif (row := open_q.pop(m.session_id, None)) is not None:
            row.update(answer=m.content, charged=m.charged,
                       tokens=(m.prompt_tokens or 0) + (m.completion_tokens or 0))
    return {"rows": rows[::-1], "today": q.count_guest_answers_since(db, day_start),
            "cap": settings.GUEST_DAILY_TOTAL, "enabled": settings.GUEST_TRIAL_ENABLED,
            "per_guest": settings.GUEST_TRIAL_QUESTIONS, "per_ip": settings.GUEST_IP_PER_DAY}


def _plan_rows(db) -> list[dict]:
    """Вкладка «Тарифы» (#160): тариф, дата подключения и расход текущего периода по каждому."""
    rows = []
    for u in q.list_users(db):
        st = quota.quota_state(db, u)
        rows.append({
            "id": u.id, "username": u.username, "role": u.role,
            "who": " · ".join(x for x in (u.full_name, u.region) if x),
            "plan": u.plan or "",
            "org": u.org or "", "inn": u.inn or "",
            # название не из таблицы тарифов молча снимает лимит (ревью PR #161, LOW-3) — показываем
            "unknown": bool(u.plan) and u.plan not in plans.ALL_PLANS,
            "started": plans.msk_date(u.plan_started_at).isoformat() if u.plan_started_at else "",
            # срок (09.10.2026): дата для формы и строка «до 16.10.2026» / «бессрочно» / истёк
            "expires": plans.msk_date(u.plan_expires_at).isoformat() if u.plan_expires_at else "",
            "expires_text": quota.validity(u)["expires"], "expired": plans.plan_expired(u),
            "state": st,
            "period": (f"{plans.msk_date(st.start):%d.%m.%Y} – "
                       f"{plans.msk_date(st.end - timedelta(seconds=1)):%d.%m.%Y}") if st else "",
        })
    return rows


@router.post("/api/admin/users/{user_id}/plan")
def admin_set_plan(user_id: int, plan: str = Form(default=""), started: str = Form(default=""),
                   expires: str = Form(default=""),
                   admin: User = Depends(require_admin)) -> RedirectResponse:
    """Назначить тариф, дату подключения и срок (#160; срок и внутренние тарифы — 09.10.2026).
    Пустой тариф — снять лимит. Дата подключения по умолчанию — сегодня по Москве; в будущем
    нельзя: период, которого ещё нет, пользователь не увидит. Срок — дата, с которой тариф уже не
    действует («Активен до …»); пусто — бессрочно, у «Пробного режима» — TRIAL_DAYS дней.
    Срок в прошлом допустим: так admin закрывает доступ, не снимая тариф."""
    plan = plan.strip()
    if plan and plan not in plans.ALL_PLANS:
        raise HTTPException(status_code=422, detail=f"Неизвестный тариф «{plan}»")
    started_at = expires_at = None
    if plan:
        day = _parse_date(started.strip()) if started.strip() else plans.msk_today()
        if day is None:
            raise HTTPException(status_code=422, detail="Дата подключения — в формате ГГГГ-ММ-ДД")
        if day > plans.msk_today():
            raise HTTPException(status_code=422, detail="Дата подключения не может быть в будущем")
        started_at = plans.anchor_from_date(day)
        until = _parse_date(expires.strip()) if expires.strip() else None
        if expires.strip() and until is None:
            raise HTTPException(status_code=422, detail="Срок действия — в формате ГГГГ-ММ-ДД")
        if until is None and plan == plans.TRIAL_PLAN:
            until = day + timedelta(days=plans.TRIAL_DAYS)
        if until is not None and until <= day:
            raise HTTPException(status_code=422, detail="Срок действия должен быть позже даты подключения")
        expires_at = plans.anchor_from_date(until) if until else None
    with get_session() as db:
        if q.set_user_plan(db, user_id, plan or None, started_at, expires_at) is None:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
    logger.info(f"тариф: admin_id={admin.id} назначил user_id={user_id} "
                f"«{plan or 'без тарифа'}»" + (f" с {plans.msk_date(started_at):%d.%m.%Y}" if plan else "")
                + (f" до {plans.msk_date(expires_at):%d.%m.%Y}" if expires_at else ""))
    return RedirectResponse("/admin", status_code=303)


@router.post("/api/admin/users/{user_id}/org")
def admin_set_org(user_id: int, org: str = Form(default=""), inn: str = Form(default=""),
                  admin: User = Depends(require_admin)) -> RedirectResponse:
    """Организация и ИНН пользователя (решение владельца 09.10.2026: заполняет только admin
    сервиса; в кабинете пользователь их видит, но не правит). Пустые — очистить."""
    org, inn = org.strip(), inn.strip()
    if len(org) > ORG_LIMIT:
        raise HTTPException(status_code=422, detail="Слишком длинное название организации")
    if inn and not INN_RE.match(inn):
        raise HTTPException(status_code=422, detail="ИНН должен содержать 10 или 12 цифр")
    with get_session() as db:
        if q.set_user_org(db, user_id, org, inn) is None:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
    logger.info(f"организация: admin_id={admin.id} изменил user_id={user_id}")
    return RedirectResponse("/admin", status_code=303)


@router.post("/api/admin/leads/{lead_id}/delete")
def admin_delete_lead(lead_id: int, admin: User = Depends(require_admin)) -> RedirectResponse:
    """Удалить заявку — по отзыву согласия или требованию субъекта ПДн (политика, раздел 9)."""
    with get_session() as db:
        q.delete_lead(db, lead_id)
    return RedirectResponse("/admin", status_code=303)


@router.get("/api/admin/export")
def admin_export(fmt: str = "json", date_from: str = "", date_to: str = "",
                 region: str = "", role: str = "",
                 admin: User = Depends(require_admin)) -> Response:
    """Выгрузка данных панели (только admin). Учитывает те же фильтры, что и /admin.
    fmt=csv — скоркард: одна строка на оценённый ответ (для Excel, разделитель «;», BOM для кириллицы);
    fmt=json — полная структура (скоркард + разбивки по регионам/пользователям + все оценки + исправления).
    Заменяет ручной SSH-дамп таблиц с VM.

    R27: проверка роли — через зависимость `require_admin`, а не ручным `if` в теле. Хелпер
    существовал с самого начала и не использовался нигде, хотя ROADMAP заявлял его как часть
    ролевого гейтинга; ручная проверка при этом дублировала его логику. `/admin` (страница)
    осознанно оставлена на ручной проверке: там не-админа надо РЕДИРЕКТИТЬ в чат, а не отдавать
    403 — зависимость такого не умеет."""
    with get_session() as db:
        view = build_admin_view(db, date_from=_parse_date(date_from), date_to=_parse_date(date_to),
                                region=region, role=role)
    st = view["stats"]
    stamp = datetime.now().strftime("%Y%m%d")

    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Дата", "Пользователь", "Регион", "Роль", "Оценка", "Вопрос", "Ответ ИИ",
                    "Непроверенные числа", "Низкая релевантность", "Комментарий", "Исправление"])
        for r in st["answer_rows"]:
            w.writerow([r["ts"], r["user"], r["region"], r["role"], r["rating"],
                        r["question"], r["answer"], r["unverified"] or "",
                        "да" if r["low_relevance"] else "", r["comment"], r["correction"]])
        body = chr(0xFEFF) + buf.getvalue()  # BOM → Excel корректно читает кириллицу
        return _download(body, "text/csv; charset=utf-8", f"navigator719-scorecard-{stamp}.csv")

    scalar = ("users_total", "users", "experts", "admins", "conversations", "requests", "answers",
              "tokens", "cost", "answer_ratings", "avg_stars", "accept_pct", "accept_user",
              "accept_expert", "gate", "gate_pass", "flags_unverified", "flags_lowrel",
              "demand_product", "demand_procedural", "orphan_ratings")
    payload = {
        # R3: выгрузка админки тоже уносит ответы ИИ наружу (в отчёты, заказчику) — маркируем.
        "disclaimer": EXPERT_DISCLAIMER,
        "corpus": corpus_line(),  # E1: редакция рядом с дисклеймером
        "generated_at": st["generated_at"],
        "filters": {"date_from": st["filter_from"] or None, "date_to": st["filter_to"] or None,
                    "region": st["filter_region"] or None, "role": st["filter_role"] or None},
        "scorecard": {k: st[k] for k in scalar},
        "regions": st["regions"],
        "per_user": st["per_user"],
        "answer_ratings": st["answer_rows"],
        "corrections": st["corrections"],
    }
    return _json_download(payload, f"navigator719-admin-{stamp}.json")


class FeedbackIn(BaseModel):
    kind: str = "service"          # answer | dialog | service
    rating: int | None = None      # answer: звёзды 0..5 / service: 1..5
    matched: str | None = None     # service: да | частично | нет
    comment: str | None = None     # dialog / service
    correction: str | None = None  # answer (опц.) — исправление критической ошибки
    session_id: str | None = None
    message_id: int | None = None


@router.post("/api/feedback")
def submit_feedback(fb: FeedbackIn, user: User = Depends(require_user_profiled)) -> dict:
    """Три канала (см. Feedback.kind): 'answer' — звёзды 0..5 + опц. исправление к ответу
    (нужен message_id); 'dialog' — комментарий к беседе (нужен session_id); 'service' —
    глобальная форма (оценка 1..5 + соответствие + текст), путь мини-1.0 без изменений."""
    kind = fb.kind if fb.kind in ("answer", "dialog", "service") else "service"
    if fb.rating is not None and not (0 <= fb.rating <= 5):
        raise HTTPException(status_code=422, detail="Оценка вне диапазона 0..5")
    comment = (fb.comment or "").strip() or None
    correction = (fb.correction or "").strip() or None
    if kind == "answer":
        if fb.message_id is None:
            raise HTTPException(status_code=422, detail="message_id обязателен для оценки ответа")
        if fb.rating is None and correction is None and comment is None:
            return {"ok": True, "skipped": True}  # пустой сигнал не храним
    if kind == "dialog":
        if not fb.session_id:
            raise HTTPException(status_code=422, detail="session_id обязателен для комментария к диалогу")
        if comment is None:
            return {"ok": True, "skipped": True}
    with get_session() as db:
        if kind == "answer":
            # R2: оценить можно ТОЛЬКО свой ответ ассистента. Раньше проверки не было — любой
            # залогиненный мог проставить оценку чужому message_id, и она засчитывалась в приёмку
            # (гейт 1.0) от лица своей роли и своего региона. Метрика должна быть защищена.
            msg = q.get_message(db, fb.message_id)
            if msg is None or msg.user_id != user.id:
                raise HTTPException(status_code=403, detail="Оценить можно только свой ответ")
            if msg.role != "assistant":
                raise HTTPException(status_code=422, detail="Оценка ставится ответу ассистента")
        q.save_feedback(
            db, user_id=user.id, kind=kind, rating=fb.rating,
            matched=(fb.matched if kind == "service" else None),
            comment=comment,  # answer: коммент к ответу / dialog: к беседе / service: глоб. форма
            correction=(correction if kind == "answer" else None),
            session_id=fb.session_id, message_id=(fb.message_id if kind == "answer" else None),
        )
    return {"ok": True}
