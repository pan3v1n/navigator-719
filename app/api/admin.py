"""Админ-панель `/admin` — пересборка под работу сервиса (решения владельца 09.10.2026).

Прежняя панель была одной страницей под пилот («принимаем ли 1.0»): шесть вкладок, и КАЖДЫЙ её
показ читал все сообщения и все отзывы базы. Теперь — разделы с собственными адресами, в стиле
сервиса (сайдбар вместо вкладок), и каждый раздел читает только своё:

* «Пользователи» — список с поиском и фильтрами и карточка учётки: профиль, тариф и срок,
  организация и ИНН, роль, сброс пароля, блокировка, удаление, активность;
* «Заявки», «Качество» (приёмка, плохие ответы, исправления), «Диалоги», «Пробный режим» —
  прежнее содержимое, по своим адресам; тяжёлая сборка скоркарда (`build_admin_view`) живёт
  только в «Качестве», «Диалогах» и выгрузке.

Доступ — только роль admin: страница без входа ведёт на /login, не-admin'а — в чат; действия
(POST /api/admin/...) — через `require_admin` (401/403). Ссылки «Логи диалогов» в чате больше нет:
панель открывается по адресу /admin.

Защита от самоблокировки: admin не меняет себе роль, не блокирует и не удаляет себя, и нельзя
лишить сервис последнего действующего администратора — иначе вернуть доступ можно только
скриптом на сервере.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger

from app.api import quota
from app.api.admin_stats import build_admin_view, system_health
from app.api.auth import current_user, generate_password, hash_password, require_admin
from app.api.leads import _INN_RE as INN_RE
from app.api.web import (
    ORG_LIMIT,
    _ctx,
    _download,
    _json_download,
    _parse_date,
    templates,
)
from app.core import plans
from app.core.config import settings
from app.core.plans import PLAN_LIMITS
from app.core.prompts import EXPERT_DISCLAIMER
from app.db import queries as q
from app.db.engine import get_session
from app.db.models import User
from app.rag.edition import corpus_line

router = APIRouter()

# Разделы сайдбара: (ключ, адрес, подпись). Порядок — порядок работы: что сломалось и кто это.
SECTIONS = [
    ("users", "/admin/users", "Пользователи"),
    ("leads", "/admin/leads", "Заявки"),
    ("quality", "/admin/quality", "Качество"),
    ("dialogs", "/admin/dialogs", "Диалоги"),
    ("trial", "/admin/trial", "Пробный режим"),
]
ACCOUNT_ROLES = {"user": "Региональный участник", "expert": "Эксперт ТПП", "admin": "Администратор"}
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
LEADS_ON_PAGE = 200
GUEST_ON_PAGE = 400  # реплик пробного режима на странице (≈ 200 вопросов с ответами)

# Итог действия на карточке после редиректа (PRG) — фиксированные тексты по коду из URL:
# в адрес не попадает ничего, кроме кода.
DONE = {
    "plan": "Тариф сохранён.",
    "org": "Организация и ИНН сохранены.",
    "role": "Роль изменена.",
    "blocked": "Учётка заблокирована: вход закрыт, открытые сессии завершены.",
    "unblocked": "Учётка разблокирована.",
}


# --------------------------------------------------------------------------- #
# Каркас
# --------------------------------------------------------------------------- #
def _gate(request: Request) -> User | RedirectResponse:
    """Страницы панели: без входа — на /login, не-admin — в чат (а не 403: это страница)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "admin":
        return RedirectResponse("/chat", status_code=302)
    return user


def _page(request: Request, admin: User, template: str, section: str, title: str, *,
          status_code: int = 200, **kw):
    return templates.TemplateResponse(
        f"admin/{template}.html",
        _ctx(request, admin=admin, sections=SECTIONS, section=section, page_title=title, **kw),
        status_code=status_code)


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    """Вход в панель. До «Сводки» (следующий этап) — список пользователей."""
    gate = _gate(request)
    if isinstance(gate, RedirectResponse):
        return gate
    return RedirectResponse("/admin/users", status_code=302)


# --------------------------------------------------------------------------- #
# Пользователи
# --------------------------------------------------------------------------- #
def _msk(ts) -> str:
    return f"{plans.naive_utc(ts) + plans.MSK_OFFSET:%d.%m.%Y %H:%M}" if ts else ""


def _user_row(db, u: User, activity: dict) -> dict:
    st = quota.quota_state(db, u)
    last, asked = activity.get(u.id, (None, 0))
    return {
        "id": u.id, "username": u.username, "role": u.role, "role_label": ACCOUNT_ROLES.get(u.role, u.role),
        "full_name": u.full_name or "", "org": u.org or "", "inn": u.inn or "", "region": u.region or "",
        "email": u.email or "",
        "plan": u.plan or "", "unknown_plan": bool(u.plan) and u.plan not in plans.ALL_PLANS,
        "expires": quota.validity(u)["expires"], "expired": plans.plan_expired(u),
        "state": st, "blocked": u.blocked_at is not None,
        # Профиль обязателен только роли user — у остальных пустой профиль не сигнал
        "no_profile": u.role == "user" and not (u.consent and u.full_name and u.region),
        "last": _msk(last), "last_ts": last, "asked": asked,
    }


def _matches(row: dict, text: str, role: str, status: str, plan: str) -> bool:
    if text:
        hay = " ".join((row["username"], row["full_name"], row["org"], row["inn"], row["email"],
                        row["region"])).lower().replace("ё", "е")
        if not all(w in hay for w in text.lower().replace("ё", "е").split()):
            return False
    if role and row["role"] != role:
        return False
    if status == "active" and row["blocked"] or status == "blocked" and not row["blocked"]:
        return False
    if status == "no_profile" and not row["no_profile"]:
        return False
    if status == "expired" and not row["expired"]:
        return False
    if plan == "-" and row["plan"] or plan and plan != "-" and row["plan"] != plan:
        return False
    return True


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, role: str = "", status: str = "", plan: str = "", ok: str = "",
                login: str = ""):
    """Список учёток: поиск по логину, ФИО, организации, ИНН, email и региону; фильтры роли,
    состояния и тарифа. Свежая активность сверху."""
    gate = _gate(request)
    if isinstance(gate, RedirectResponse):
        return gate
    text = request.query_params.get("q", "").strip()   # ?q=… — имя `q` в коде занято модулем запросов
    with get_session() as db:
        activity = q.user_activity(db)
        rows = [_user_row(db, u, activity) for u in q.list_users(db)]
    total = len(rows)
    rows = [r for r in rows if _matches(r, text, role, status, plan)]
    rows.sort(key=lambda r: (r["last_ts"] is not None, r["last_ts"] or datetime.min, -r["id"]), reverse=True)
    notice = f"Учётка «{login}» удалена вместе с профилем и диалогами." if ok == "deleted" and login else ""
    return _page(request, gate, "users", "users", "Пользователи", rows=rows, total=total,
                 filters={"q": text, "role": role, "status": status, "plan": plan}, roles=ACCOUNT_ROLES,
                 all_plans=list(plans.ALL_PLANS), notice=notice, create_form={})


@router.post("/api/admin/users", response_class=HTMLResponse)
def admin_create_user(request: Request, username: str = Form(default=""), role: str = Form(default="user"),
                      admin: User = Depends(require_admin)):
    """Новая учётка: логин и роль; пароль генерирует сервер и показывает admin'у ОДИН раз — в базе
    только bcrypt-хеш, как у `seed_users.py`. Ответ — карточка новой учётки сразу, без редиректа:
    пароль не должен жить ни в URL, ни в куке. Регион региональному участнику с логином вида
    `kursk.expert2` подставит анкета (`regions`)."""
    login, role = username.strip().lower(), role.strip()
    error = None
    if not _USERNAME_RE.match(login):
        error = ("Логин — латинские буквы, цифры, точка, дефис или подчёркивание, от 3 до 64 "
                 "символов, начинается с буквы или цифры.")
    elif role not in ACCOUNT_ROLES:
        error = "Неизвестная роль."
    else:
        with get_session() as db:
            if q.get_user_by_username(db, login) is not None:
                error = f"Логин «{login}» уже занят."
            else:
                password = generate_password()
                uid = q.create_user(db, login, hash_password(password), role=role).id
    if error:
        with get_session() as db:
            activity = q.user_activity(db)
            rows = [_user_row(db, u, activity) for u in q.list_users(db)]
        return _page(request, admin, "users", "users", "Пользователи", status_code=400, rows=rows,
                     total=len(rows), filters={}, roles=ACCOUNT_ROLES, all_plans=list(plans.ALL_PLANS),
                     create_error=error, create_form={"username": login, "role": role})
    logger.info(f"учётка: admin_id={admin.id} создал «{login}» ({role})")  # пароль в журнал — никогда
    return _card(request, admin, uid, password_shown=password, notice="Учётка создана.")


def _card(request: Request, admin: User, user_id: int, *, password_shown: str | None = None,
          notice: str = "", error: str = "", status_code: int = 200):
    with get_session() as db:
        u = q.get_user(db, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        activity = q.user_activity(db)
        row = _user_row(db, u, activity)
        sessions = q.get_user_sessions(db, u.id)[:10]
        db.expunge(u)
    return _page(
        request, admin, "user", "users", u.username, status_code=status_code, u=u, row=row,
        sessions=[{**s, "ts": _msk(s["ts"])} for s in sessions], roles=ACCOUNT_ROLES,
        paid_plans=list(PLAN_LIMITS), internal_plans=list(plans.INTERNAL_PLANS),
        trial_plan=plans.TRIAL_PLAN, trial_days=plans.TRIAL_DAYS, today=plans.msk_today().isoformat(),
        started=plans.msk_date(u.plan_started_at).isoformat() if u.plan_started_at else "",
        expires_iso=plans.msk_date(u.plan_expires_at).isoformat() if u.plan_expires_at else "",
        period=(f"{plans.msk_date(row['state'].start):%d.%m.%Y} – "
                f"{plans.msk_date(row['state'].end - timedelta(seconds=1)):%d.%m.%Y}") if row["state"] else "",
        consent_at=_msk(u.consent_at), created_at=_msk(u.created_at), blocked_at=_msk(u.blocked_at),
        is_self=u.id == admin.id, password_shown=password_shown, notice=notice, error=error)


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_card(request: Request, user_id: int, ok: str = ""):
    gate = _gate(request)
    if isinstance(gate, RedirectResponse):
        return gate
    return _card(request, gate, user_id, notice=DONE.get(ok, ""))


def _back(user_id: int, done: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/users/{user_id}?ok={done}", status_code=303)


def _guard_admin_loss(db, admin: User, target: User, action: str) -> str:
    """Причина отказа или «»: себя не трогаем, последнего действующего admin'а — тоже."""
    if target.id == admin.id:
        return f"Нельзя {action} собственную учётку — это сделает другой администратор."
    if target.role == "admin" and target.blocked_at is None and q.count_active_admins(db) <= 1:
        return f"Нельзя {action} последнего действующего администратора."
    return ""


@router.post("/api/admin/users/{user_id}/role", response_class=HTMLResponse)
def admin_set_role(request: Request, user_id: int, role: str = Form(default=""),
                   admin: User = Depends(require_admin)):
    role = role.strip()
    if role not in ACCOUNT_ROLES:
        return _card(request, admin, user_id, error="Неизвестная роль.", status_code=400)
    with get_session() as db:
        target = q.get_user(db, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        why = "" if target.role == role else (
            _guard_admin_loss(db, admin, target, "сменить роль у") if target.role == "admin" or target.id == admin.id else "")
        if not why:
            q.set_user_role(db, user_id, role)
    if why:
        return _card(request, admin, user_id, error=why, status_code=400)
    logger.info(f"учётка: admin_id={admin.id} сменил роль user_id={user_id} на {role}")
    return _back(user_id, "role")


@router.post("/api/admin/users/{user_id}/password", response_class=HTMLResponse)
def admin_reset_password(request: Request, user_id: int, admin: User = Depends(require_admin)):
    """Новый пароль — показывается ОДИН раз, как при создании; эпоха входа растёт, и все сессии и
    куки «запомнить меня» этой учётки перестают действовать сразу."""
    password = generate_password()
    with get_session() as db:
        if q.reset_password(db, user_id, hash_password(password)) is None:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
    logger.info(f"учётка: admin_id={admin.id} сбросил пароль user_id={user_id}")
    note = ("Пароль сброшен. Вы сменили свой пароль — на следующей странице войдите заново."
            if user_id == admin.id else "Пароль сброшен, прежние входы этой учётки завершены.")
    return _card(request, admin, user_id, password_shown=password, notice=note)


@router.post("/api/admin/users/{user_id}/block", response_class=HTMLResponse)
def admin_block(request: Request, user_id: int, blocked: str = Form(default="1"),
                admin: User = Depends(require_admin)):
    want = blocked == "1"
    with get_session() as db:
        target = q.get_user(db, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        why = _guard_admin_loss(db, admin, target, "заблокировать") if want else ""
        if not why:
            q.set_blocked(db, user_id, want)
    if why:
        return _card(request, admin, user_id, error=why, status_code=400)
    logger.info(f"учётка: admin_id={admin.id} {'заблокировал' if want else 'разблокировал'} user_id={user_id}")
    return _back(user_id, "blocked" if want else "unblocked")


@router.post("/api/admin/users/{user_id}/delete", response_class=HTMLResponse)
def admin_delete_user(request: Request, user_id: int, confirm: str = Form(default=""),
                      admin: User = Depends(require_admin)):
    """Удаление учётки с профилем, диалогами, оценками и расходом — по требованию субъекта ПДн.
    Необратимо, поэтому подтверждается вводом логина."""
    with get_session() as db:
        target = q.get_user(db, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        login = target.username
        why = _guard_admin_loss(db, admin, target, "удалить")
        if not why and confirm.strip() != login:
            why = "Для удаления введите логин учётки точно."
        if not why:
            q.delete_user(db, user_id)
    if why:
        return _card(request, admin, user_id, error=why, status_code=400)
    logger.info(f"учётка: admin_id={admin.id} удалил user_id={user_id}")
    return RedirectResponse(f"/admin/users?ok=deleted&login={login}", status_code=303)


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
    return _back(user_id, "plan")


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
    return _back(user_id, "org")


# --------------------------------------------------------------------------- #
# Заявки с лендинга (ПДн: только здесь, за ролью admin)
# --------------------------------------------------------------------------- #
@router.get("/admin/leads", response_class=HTMLResponse)
def admin_leads_page(request: Request):
    """Заявки с лендинга. Просроченные (срок из политики ПДн) удаляются при каждом открытии;
    на страницу — не больше LEADS_ON_PAGE."""
    gate = _gate(request)
    if isinstance(gate, RedirectResponse):
        return gate
    with get_session() as db:
        q.purge_old_leads(db)
        leads = q.list_leads(db, limit=LEADS_ON_PAGE)
        leads_total = q.count_leads(db)
    return _page(request, gate, "leads", "leads", "Заявки", leads=leads, leads_total=leads_total)


@router.post("/api/admin/leads/{lead_id}/delete")
def admin_delete_lead(lead_id: int, admin: User = Depends(require_admin)) -> RedirectResponse:
    """Удалить заявку — по отзыву согласия или требованию субъекта ПДн (политика, раздел 9)."""
    with get_session() as db:
        q.delete_lead(db, lead_id)
    return RedirectResponse("/admin/leads", status_code=303)


# --------------------------------------------------------------------------- #
# Качество и Диалоги — приёмочный скоркард пилота (`build_admin_view`)
# --------------------------------------------------------------------------- #
def _stats_view(date_from: str, date_to: str, region: str, role: str) -> dict:
    with get_session() as db:
        return build_admin_view(db, date_from=_parse_date(date_from), date_to=_parse_date(date_to),
                                region=region, role=role)


@router.get("/admin/quality", response_class=HTMLResponse)
def admin_quality(request: Request, date_from: str = "", date_to: str = "", region: str = "",
                  role: str = ""):
    """Приёмка ≥4★, срезы, плохие ответы, исправления и комментарии экспертов, флаги движка.
    Фильтры (query, опц.): date_from/date_to (YYYY-MM-DD), region, role."""
    gate = _gate(request)
    if isinstance(gate, RedirectResponse):
        return gate
    view = _stats_view(date_from, date_to, region, role)
    return _page(request, gate, "quality", "quality", "Качество", health=system_health(),
                 filter_action="/admin/quality", **view)


@router.get("/admin/dialogs", response_class=HTMLResponse)
def admin_dialogs(request: Request, date_from: str = "", date_to: str = "", region: str = "",
                  role: str = "", user: str = ""):
    """Диалоги по пользователям; `user` — показать одного (ссылки из карточки и из плохих ответов)."""
    gate = _gate(request)
    if isinstance(gate, RedirectResponse):
        return gate
    view = _stats_view(date_from, date_to, region, role)
    if user:
        view["data"] = [u for u in view["data"] if u["username"] == user]
    return _page(request, gate, "dialogs", "dialogs", "Диалоги", filter_action="/admin/dialogs",
                 only_user=user, **view)


@router.get("/api/admin/export")
def admin_export(fmt: str = "json", date_from: str = "", date_to: str = "",
                 region: str = "", role: str = "",
                 admin: User = Depends(require_admin)) -> Response:
    """Выгрузка скоркарда (только admin). Учитывает те же фильтры, что и «Качество».
    fmt=csv — одна строка на оценённый ответ (для Excel, разделитель «;», BOM для кириллицы);
    fmt=json — полная структура (скоркард + разбивки по регионам/пользователям + все оценки +
    исправления). R27: роль — через зависимость `require_admin`."""
    st = _stats_view(date_from, date_to, region, role)["stats"]
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


# --------------------------------------------------------------------------- #
# Пробный режим без входа
# --------------------------------------------------------------------------- #
def _guest_view(db) -> dict:
    """Расход пробного режима за сутки против общего потолка и последние вопросы гостей с
    ответами. Гости обезличены — показываем только текст, время и токены."""
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


@router.get("/admin/trial", response_class=HTMLResponse)
def admin_trial(request: Request):
    gate = _gate(request)
    if isinstance(gate, RedirectResponse):
        return gate
    with get_session() as db:
        q.purge_old_guest_messages(db)  # срок хранения — обещание политики, как у заявок
        guest_view = _guest_view(db)
    return _page(request, gate, "trial", "trial", "Пробный режим", guest_view=guest_view)
