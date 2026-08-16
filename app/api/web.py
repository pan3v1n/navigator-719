"""Веб-страницы UI 1.0 (Jinja2): вход, чат, приём формы обратной связи.

Страницы при отсутствии сессии РЕДИРЕКТЯТ на /login (не 401 — 401 только у /api/*, их ловит JS).
Шаблоны — app/web/templates, статика монтируется в main.py.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
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
from app.api.admin_stats import build_admin_view, system_health
from app.api.ratelimit import SlidingWindow
from app.core.config import settings
from app.core.prompts import EXPERT_DISCLAIMER
from app.core.regions import REGIONS, region_from_username
from app.rag.edition import corpus_edition, corpus_line, kontur_719_url
from app.db import queries as q
from app.db.engine import get_session
from app.db.models import User

router = APIRouter()
_WEB = Path(__file__).resolve().parents[1] / "web"
templates = Jinja2Templates(directory=str(_WEB / "templates"))
# Разряды числа с неразрывным пробелом (12345 → «12 345») — для читабельных токенов/₽ в админке.
templates.env.filters["spaced"] = lambda n: f"{int(n or 0):,}".replace(",", " ")


def _ctx(request: Request, **kw) -> dict:
    # E1: редакция корпуса — во ВСЕ страницы. Выводится из самого текста постановления
    # (app/rag/edition.py), поэтому не может разойтись с базой молча.
    return {"request": request, "app_title": settings.APP_TITLE, "org": settings.ORG_NAME,
            "corpus_edition": corpus_edition(), **kw}


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


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", _ctx(request, error=None))


# --------------------------------------------------------------------------- #
# Правовые и справочные страницы
#
# ПУБЛИЧНЫЕ, без входа: их читают ДО того, как согласиться. Политику конфиденциальности,
# спрятанную за авторизацией, невозможно прочитать перед тем, как дать согласие в профиле, —
# а согласие даётся именно на её условиях. Дата редакции задаётся здесь и показывается на
# странице: молча меняющийся правовой документ хуже отсутствующего.
# --------------------------------------------------------------------------- #
DOCS_UPDATED = "13.08.2026"


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


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or "unknown"


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


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if needs_profile(user):  # жёсткий гейт: роль user не в чат, пока не заполнит профиль+согласие
        return RedirectResponse("/profile", status_code=302)
    # kontur_719_url — адрес первоисточника в редакции КОРПУСА (у Контура documentId свой на каждую
    # редакцию). Отдаём фронту отсюда, чтобы константа была одна и не разъезжалась с базой.
    return templates.TemplateResponse(
        "chat.html", _ctx(request, user=user, kontur_719_url=kontur_719_url()))


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    prefill = user.region or region_from_username(user.username)
    return templates.TemplateResponse(
        "profile.html",
        _ctx(request, user=user, error=None, regions=REGIONS, prefill_region=prefill,
             full_name=user.full_name or "", telegram=user.telegram or "",
             consent=bool(user.consent)),
    )


@router.post("/profile", response_class=HTMLResponse)
def profile_submit(
    request: Request,
    consent: str = Form(default=""),
    full_name: str = Form(default=""),
    region: str = Form(default=""),
    telegram: str = Form(default=""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fn, rg, tg = full_name.strip(), region.strip(), telegram.strip()
    # Все поля обязательны (решение заказчика: жёсткий гейт). Незаполненное → дружелюбный ре-рендер.
    if not (consent and fn and rg and tg):
        return templates.TemplateResponse(
            "profile.html",
            _ctx(request, user=user,
                 error="Заполните ФИО, регион и Telegram и подтвердите согласие на обработку персональных данных.",
                 regions=REGIONS, prefill_region=rg or region_from_username(user.username),
                 full_name=fn, telegram=tg, consent=bool(consent)),
            status_code=400,
        )
    with get_session() as db:
        q.update_profile(db, user.id, full_name=fn, region=rg, telegram=tg, consent=True)
    return RedirectResponse("/chat", status_code=302)


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
    return templates.TemplateResponse(
        "admin.html", _ctx(request, admin=user, health=system_health(), **view))


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
