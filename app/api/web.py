"""Веб-страницы UI 1.0 (Jinja2): вход, чат, приём формы обратной связи.

Страницы при отсутствии сессии РЕДИРЕКТЯТ на /login (не 401 — 401 только у /api/*, их ловит JS).
Шаблоны — app/web/templates, статика монтируется в main.py.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

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
    remember_from,
    require_user_profiled,
    set_remember_cookie,
)
from app.api import quota, trial
from app.api.leads import LIMITS as LEAD_LIMITS
from app.api.leads import TARIFFS as LEAD_TARIFFS
from app.api.leads import _EMAIL_RE as EMAIL_RE
from app.api.leads import _digits as digits
from app.api.ratelimit import SlidingWindow, client_ip
from app.core import pricing
from app.core.inn import inn_valid
from app.core.config import settings
from app.core.release import release_label
from app.core.regions import REGIONS, region_from_username
from app.rag import followup
from app.rag.edition import corpus_edition, kontur_719_url
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
# Витрина тарифов (09.10.2026) — описание одно на все отрисовки кабинета, не поле контекста страницы.
templates.env.globals["pricing"] = pricing


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
    if user.blocked_at is not None:
        # Только после верного пароля: так блокировка не подсказывает, какие логины существуют.
        return templates.TemplateResponse(
            "login.html", _ctx(request, error="Учётная запись заблокирована. Обратитесь к "
                                              "администратору сервиса."), status_code=403)
    # Успешный вход снимает счётчик: человек, вспомнивший пароль с 9-й попытки, не должен
    # оставаться под лимитом — он бьёт по перебору, а не по забывчивости.
    _login_limit.reset(f"login:{ip}")
    login_session(request, user)
    resp = RedirectResponse("/", status_code=302)
    if remember:  # «Запомнить меня» → персистентный cookie автовхода (с эпохой входа)
        set_remember_cookie(resp, user.id, user.auth_epoch or 0)
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
    # Вход был, но погас (блокировка, сброс пароля — `current_user` чистит сессию) — на вход, а не в
    # пробный режим: иначе заблокированный продолжал бы спрашивать гостем, а эксперт после сброса
    # пароля молча получал бы урезанные ответы (ревью PR #182). Признак — до `current_user`.
    # `current_user` очистит сессию в этом же запросе — пометка `lapsed` держит признак до выхода
    # или входа, иначе вход без «запомнить меня» уводился на вход лишь один раз (повторное ревью).
    had_login = (bool(request.session.get("user_id")) or bool(request.session.get("lapsed"))
                 or remember_from(request) is not None)
    user = current_user(request)
    if not user:
        if settings.GUEST_TRIAL_ENABLED and not had_login:
            return _guest_chat_page(request)
        if had_login:
            request.session["lapsed"] = True
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
            "org": user.org or "", "inn": user.inn or "", "consent": bool(user.consent)}
    form.update(over)
    return form


def _profile_response(request: Request, user: User, form: dict, *, tab: str = "profile",
                      saved: bool = False, error: str | None = None, status_code: int = 200,
                      requested: str = "", confirm: dict | None = None, tariff_kind: str = "",
                      editing: bool = False):
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
             requested=requested if requested in LEAD_TARIFFS else "",
             confirm=confirm, tariff_kind=tariff_kind if tariff_kind in pricing.KINDS else "individual",
             # Форма профиля — просмотр, правка по «Редактировать» (просьба владельца 09.10.2026). Открыта
             # сразу, пока профиль не заполнен (гейт до чата) и когда сохранение вернуло ошибку — иначе
             # нечего было бы исправлять.
             editing=editing or needs_profile(user) or bool(error),
             can_leave=not needs_profile(user)),
        status_code=status_code,
    )


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, tab: str = "profile", saved: str = "", requested: str = "",
                 err: str = "", edit: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    # «Подключить» на карточке — GET сюда же с выбранными условиями: панель подтверждения с данными
    # из профиля и галочкой согласия; заявку создаёт только она (`/api/plan-request`). Поля читаются из
    # адреса, а не параметрами функции: имя `trial` занято модулем.
    qp, confirm = request.query_params, None
    error = PLAN_REQUEST_ERRORS.get(err)
    if tab == "plan" and qp.get("confirm") == "1":
        seats = qp.get("seats")
        fields, summary, code = _plan_choice(qp.get("tariff", ""), qp.get("kind") or "individual",
                                             qp.get("period") or "month",
                                             "1" if seats is None else seats, qp.get("trial", ""))
        if code is None and not (user.email and user.phone):
            code = "profile"
        if code is None:
            confirm = {"tariff": fields["tariff"], "summary": summary, "fields": fields}
        else:
            error = PLAN_REQUEST_ERRORS[code]
    return _profile_response(request, user, _profile_form(user), tab=tab, saved=bool(saved),
                             requested=requested, error=error, confirm=confirm,
                             tariff_kind=qp.get("kind", ""), editing=edit == "1")


# «Подключить» на витрине тарифов кабинета (09.10.2026). Оплаты в сервисе нет — это заявка,
# привязанная к учётке: она попадает в «Заявки» админки, тариф назначает admin в карточке. Контакты —
# из профиля (их человек сам подтвердил в анкете с согласием). Лимит — от повторных нажатий.
PLAN_REQUESTS_PER_HOUR = 5
_plan_request_limit = SlidingWindow(PLAN_REQUESTS_PER_HOUR, window=3600.0)


# Ошибки заявки — кодом в адресе (post/redirect/get, как успех с `requested=`): страница ошибки не
# остаётся на адресе API и обновление не отправляет форму снова (ревью PR #184).
PLAN_REQUEST_ERRORS = {
    "tariff": "Выберите тариф.",
    "options": "Проверьте условия на карточке: пробная неделя — только у «Старта», пользователей — от 1 до "
               f"{pricing.MAX_SEATS}, период — месяц или год.",
    "profile": "Чтобы отправить заявку, заполните во вкладке «Профиль» рабочий email и телефон — по ним с "
               "вами свяжутся.",
    "consent": "Отметьте согласие на обработку персональных данных — без него заявку не отправить.",
    "limit": "Слишком много заявок подряд — попробуйте через час.",
}


def _plan_choice(tariff: str, kind: str, period: str, seats: str, trial: str):
    """Выбор на карточке → (поля заявки, строка условий, код ошибки). Один разбор и для панели
    подтверждения, и для самой заявки. Пользователей — только ASCII-цифры: «²».isdigit() — True, а
    int("²") роняет обработчик в 500 (ревью PR #184); пустое поле — ошибка, а не молча «1»."""
    if tariff not in LEAD_TARIFFS:
        return None, None, "tariff"
    n = int(seats) if seats.isascii() and seats.isdigit() and len(seats) <= 4 else 0
    summary, errs = pricing.parse_options(tariff, kind=kind, period=period, seats=n, trial=bool(trial))
    if errs:
        return None, None, "options"
    fields = {"tariff": tariff, "kind": kind, "period": period, "seats": str(n)}
    if trial:
        fields["trial"] = "1"
    return fields, summary, None


@router.post("/api/plan-request", response_class=HTMLResponse)
def plan_request(request: Request, tariff: str = Form(default=""), kind: str = Form(default="individual"),
                 period: str = Form(default="month"),
                 seats: str = Form(default="1"), trial_flag: str = Form(default="", alias="trial"),
                 consent: str = Form(default="")):
    # `trial_flag`, а не `trial`: имя занято модулем `app.api.trial` (ревью PR #184)
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    def back(err: str):
        # Ошибка — внутри витрины и на той вкладке, с которой пришла заявка (повторное ревью PR #184:
        # над карточкой тарифа она уезжала за край экрана, а корпоративная возвращалась на «Индивидуальные»)
        tab_kind = "&kind=corporate" if kind == "corporate" else ""
        return RedirectResponse(f"/profile?tab=plan&err={err}{tab_kind}#tariffs", status_code=303)

    _, options, code = _plan_choice(tariff, kind, period, seats, trial_flag)
    if code:
        return back(code)
    # Заявка — ПДн со временем согласия (152-ФЗ). Согласие — галочкой на панели подтверждения, как в
    # форме лендинга (политика, раздел 9): согласие анкеты дано на другую цель — тестирование сервиса
    # (повторное ревью PR #184). Контакты обязательны: без них с человеком не связаться.
    if consent != "1":
        return back("consent")
    if not (user.email and user.phone):
        return back("profile")
    if not _plan_request_limit.check(f"user:{user.id}"):
        return back("limit")
    with get_session() as db:
        lead = q.create_lead(db, tariff=tariff, name=user.full_name or user.username, org=user.org or "",
                             inn=user.inn or "", email=user.email, phone=user.phone,
                             options=" · ".join(filter(None, ("из личного кабинета", options))),
                             user_id=user.id)
        q.purge_old_leads(db)   # срок хранения политики — и на этом пути новой заявки (ревью PR #184)
    logger.info(f"заявка #{lead.id} из кабинета: user_id={user.id}, тариф «{tariff}»")
    return RedirectResponse(f"/profile?tab=plan&requested={quote(tariff)}", status_code=303)


def _contact_errors(position: str, email: str, phone: str) -> list[str]:
    """Формат контактов — как в заявке лендинга. Пустые email и телефон здесь не ошибка: их
    обязательность проверяет форма отдельно, одной строкой вместе с ФИО и регионом."""
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
    org: str = Form(default=""),
    inn: str = Form(default=""),
):
    """Сохранение профиля. Организацию и ИНН вписывает сам пользователь (решение владельца 09.10.2026,
    вечер; утром их вёл только admin) — по желанию; ИНН проверяется по контрольным цифрам. Правка
    организации или ИНН снимает подтверждение admin'а (`queries.update_profile`)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fn, rg, tg = full_name.strip(), region.strip(), telegram.strip()
    pos, em, ph = position.strip(), email.strip(), phone.strip()
    og, nn = " ".join(org.split()), inn.strip()
    form = _profile_form(user, full_name=fn, region=rg or region_from_username(user.username),
                         telegram=tg, position=pos, email=em, phone=ph, org=og, inn=nn,
                         consent=bool(consent))
    # Обязательны ФИО, регион, рабочий email, телефон и согласие; должность и Telegram — по
    # желанию (решение владельца 09.10.2026). Ошибка → ре-рендер с введённым, а не потеря формы.
    errs = []
    if not (consent and fn and rg and em and ph):
        errs.append("Заполните ФИО, регион, рабочий email и телефон и подтвердите согласие на "
                    "обработку персональных данных.")
    # Регион — только из справочника: поле с поиском выбирает из него, но форму можно прислать и
    # в обход страницы, а по региону строятся срезы админки.
    if rg and rg not in REGIONS:
        errs.append("Выберите регион из списка.")
    # Длины — по колонкам: SQLite их не держит, PostgreSQL (цель прода) ответил бы 500.
    if len(fn) > PROFILE_LIMITS["full_name"] or len(tg) > PROFILE_LIMITS["telegram"]:
        errs.append("Слишком длинное ФИО или ник в Telegram.")
    errs += _contact_errors(pos, em, ph)
    if len(og) > ORG_LIMIT:
        errs.append("Слишком длинное название организации.")
    if nn and not inn_valid(nn):
        errs.append("Проверьте ИНН: 10 цифр у организации или 12 у ИП, контрольные цифры не сходятся.")
    if errs:
        return _profile_response(request, user, form, error=" ".join(errs), status_code=400)
    first_fill = needs_profile(user)
    with get_session() as db:
        q.update_profile(db, user.id, full_name=fn, region=rg, telegram=tg, consent=True,
                         position=pos, email=em, phone=ph, org=og, inn=nn)
    # Первое заполнение — это гейт до чата: дальше сразу в работу. Правка в кабинете — остаёмся
    # на месте с «Изменения сохранены», как в макете (PRG: обновление страницы не шлёт форму снова).
    return RedirectResponse("/chat" if first_fill else "/profile?saved=1", status_code=302)


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
