"""Приём заявок с лендинга 719-навигатор.рф — `POST /api/leads`.

Форма «Оставить заявку» живёт на статическом лендинге, а запрос уходит на ТОТ ЖЕ адрес: caddy
проксирует `/api/leads` корневого домена в приложение (`caddy/Caddyfile`). Поэтому нет ни CORS,
ни кук сервиса — эндпоинт публичный и ничего не знает о сессиях.

⚠ ПДн (152-ФЗ). Проверки ДУБЛИРУЮТ клиентские: браузерной валидации верить нельзя. Без согласия
заявка не сохраняется. В журнал пишется только номер заявки и тариф — ни имени, ни контактов.
IP используется ограничителем частоты в памяти и в базу не попадает. Срок хранения, обещанный
политикой (12 месяцев), исполняется кодом: старые заявки удаляются при каждой новой и при открытии
админки (`queries.purge_old_leads`).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from app.api.ratelimit import SlidingWindow, client_ip
from app.core import pricing
from app.db import queries as q
from app.db.engine import get_session

router = APIRouter()

TARIFFS = ("Старт", "Стандарт", "Профи", "Для организаций")
# Честная заявка — одна-две с адреса; лимит держит перебор и автозаполнение ботами.
# ⚠ Списывается ТОЛЬКО за принятую заявку (ревью PR #158): отклонённые проверками попытки и
# проверка выкатки (пустая форма → 422) квоту не тратят — иначе посетитель, поправивший ошибку
# в форме, через пять попыток получал бы 429 на верную заявку.
LEADS_PER_HOUR = 5
_lead_limit = SlidingWindow(LEADS_PER_HOUR, window=3600.0)

# Имя поля-ловушки — нарочно не «website»/«url»/«company»: такие поля браузеры и менеджеры
# паролей заполняют сами, и настоящая заявка молча пропадала бы (ревью PR #158, находка 1).
HONEYPOT = "nav719_hp"

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_INN_RE = re.compile(r"^\d{10}(\d{2})?$")
LIMITS = {"name": 200, "org": 300, "email": 255, "phone": 32, "promo": 64, "message": 1000}   # = колонкам `Lead`


class LeadIn(BaseModel):
    tariff: str = ""
    name: str = ""
    org: str = ""
    inn: str = ""
    email: str = ""
    phone: str = ""
    promo: str = ""
    message: str = ""   # комментарий (вечер 09.10.2026): попап корпоративных тарифов, по желанию
    consent: bool = False
    nav719_hp: str = ""  # поле-ловушка: скрыто от людей, заполняют только боты
    # Опции с витрины тарифов (09.10.2026, `app/core/pricing.py`). По умолчанию — как у формы до
    # витрины: старый скрипт из кэша браузера их не шлёт, и заявка от этого не ломается.
    # ⚠ `Any`, а не строгие типы (ревью PR #184): несовпадение типа FastAPI отвергал бы своим ответом
    # `{"detail": [...]}`, которого форма не понимает, — разбор и ошибки делает `lead_options`.
    kind: Any = "individual"
    addon: Any = False   # опция витрины снята (источники бесплатны, 09.10.2026) — старый скрипт её шлёт, не читаем
    period: Any = "month"
    seats: Any = 1
    trial: Any = False


def lead_options(f: LeadIn) -> tuple[str | None, dict[str, str]]:
    """Опции витрины из заявки — ОДИН разбор и для проверки, и для записи (ревью PR #184). Типам
    чужого скрипта не верим: флаги — только true/false, пользователей — целое число, вид и период —
    строки; иное — ошибка опций тем же ответом `errors`, что понимает форма."""
    kind = f.kind if isinstance(f.kind, str) else "?"
    period = f.period if isinstance(f.period, str) else "?"
    seats = f.seats if isinstance(f.seats, int) and not isinstance(f.seats, bool) else 0
    opts, e = pricing.parse_options(f.tariff, kind=kind, period=period, seats=seats, trial=f.trial is True)
    if not isinstance(f.trial, bool):
        e["trial"] = "Проба — да или нет"
    return (None if e else opts), e


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def validate_lead(f: LeadIn) -> dict[str, str]:
    """Ошибки по полям — те же формулировки, что показывает форма лендинга. Длины — по колонкам
    таблицы: SQLite их не держит, а PostgreSQL (цель прода) ответил бы 500 вместо 422."""
    e: dict[str, str] = {}
    name, org, email, phone, promo = (s.strip() for s in (f.name, f.org, f.email, f.phone, f.promo))
    if f.tariff not in TARIFFS:
        e["tariff"] = "Выберите тариф"
    if not name:
        e["name"] = "Укажите имя"
    elif len(name) > LIMITS["name"]:
        e["name"] = "Слишком длинное имя"
    if not org:
        e["org"] = "Укажите организацию"
    elif len(org) > LIMITS["org"]:
        e["org"] = "Слишком длинное название организации"
    if not _INN_RE.match(f.inn.strip()):
        e["inn"] = "ИНН должен содержать 10 или 12 цифр"
    if not _EMAIL_RE.match(email) or len(email) > LIMITS["email"]:
        e["email"] = "Проверьте правильность email"
    if len(_digits(phone)) != 11 or len(phone) > LIMITS["phone"]:
        e["phone"] = "Укажите телефон полностью"
    if len(promo) > LIMITS["promo"]:
        e["promo"] = "Слишком длинный промокод"
    if len(f.message.strip()) > LIMITS["message"]:
        e["message"] = "Комментарий — не длиннее 1000 знаков"
    if not f.consent:
        e["consent"] = "Необходимо согласие на обработку данных"
    _, opt_errors = lead_options(f)
    if opt_errors:   # своего поля в форме у опций нет — фронт покажет их общей строкой
        e["options"] = "; ".join(opt_errors.values())
    return e


@router.post("/api/leads")
def submit_lead(lead: LeadIn, request: Request) -> JSONResponse:
    if lead.nav719_hp.strip():
        # Бот: отвечаем как успехом, чтобы не учить его обходу, но ничего не сохраняем.
        logger.info("lead: отброшена заявка с заполненной ловушкой")
        return JSONResponse({"ok": True})
    errors = validate_lead(lead)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)
    ip = client_ip(request)
    if not _lead_limit.check(ip):
        return JSONResponse({"ok": False, "error": "Слишком много заявок с этого адреса, попробуйте позже"},
                            status_code=429, headers={"Retry-After": str(_lead_limit.retry_after(ip))})
    options, _ = lead_options(lead)
    with get_session() as db:
        row = q.create_lead(db, tariff=lead.tariff, name=lead.name.strip(), org=lead.org.strip(),
                            inn=lead.inn.strip(), email=lead.email.strip(), phone=lead.phone.strip(),
                            promo=lead.promo.strip(), options=options, message=lead.message)
        purged = q.purge_old_leads(db)
    logger.info("lead: сохранена заявка #{} (тариф «{}»){}", row.id, row.tariff,
                f"; удалено просроченных: {purged}" if purged else "")
    return JSONResponse({"ok": True})
