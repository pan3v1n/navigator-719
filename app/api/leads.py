"""Приём заявок с лендинга 719-навигатор.рф — `POST /api/leads`.

Форма «Оставить заявку» живёт на статическом лендинге, а запрос уходит на ТОТ ЖЕ адрес: caddy
проксирует `/api/leads` корневого домена в приложение (`caddy/Caddyfile`). Поэтому нет ни CORS,
ни кук сервиса — эндпоинт публичный и ничего не знает о сессиях.

⚠ ПДн (152-ФЗ). Проверки ДУБЛИРУЮТ клиентские: браузерной валидации верить нельзя. Без согласия
заявка не сохраняется. В журнал пишется только номер заявки и тариф — ни имени, ни контактов.
IP используется ограничителем частоты в памяти и в базу не попадает.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from app.api.ratelimit import SlidingWindow
from app.db import queries as q
from app.db.engine import get_session

router = APIRouter()

TARIFFS = ("Старт", "Стандарт", "Профи", "Для организаций")
# Честная заявка — одна-две с адреса; лимит держит перебор и автозаполнение ботами.
LEADS_PER_HOUR = 5
_lead_limit = SlidingWindow(LEADS_PER_HOUR, window=3600.0)

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_INN_RE = re.compile(r"^\d{10}(\d{2})?$")


class LeadIn(BaseModel):
    tariff: str = ""
    name: str = ""
    org: str = ""
    inn: str = ""
    email: str = ""
    phone: str = ""
    promo: str = ""
    consent: bool = False
    website: str = ""  # поле-ловушка: скрыто от людей, заполняют только боты


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def validate_lead(f: LeadIn) -> dict[str, str]:
    """Ошибки по полям — те же формулировки, что показывает форма лендинга."""
    e: dict[str, str] = {}
    if f.tariff not in TARIFFS:
        e["tariff"] = "Выберите тариф"
    if not f.name.strip() or len(f.name) > 200:
        e["name"] = "Укажите имя"
    if not f.org.strip() or len(f.org) > 300:
        e["org"] = "Укажите организацию"
    if not _INN_RE.match(f.inn.strip()):
        e["inn"] = "ИНН должен содержать 10 или 12 цифр"
    if not _EMAIL_RE.match(f.email.strip()) or len(f.email) > 255:
        e["email"] = "Проверьте правильность email"
    if len(_digits(f.phone)) != 11:
        e["phone"] = "Укажите телефон полностью"
    if len(f.promo) > 64:
        e["promo"] = "Слишком длинный промокод"
    if not f.consent:
        e["consent"] = "Необходимо согласие на обработку данных"
    return e


@router.post("/api/leads")
def submit_lead(lead: LeadIn, request: Request) -> JSONResponse:
    ip = (request.client.host if request.client else "") or "unknown"
    if not _lead_limit.check(ip):
        return JSONResponse({"ok": False, "error": "Слишком много заявок с этого адреса, попробуйте позже"},
                            status_code=429, headers={"Retry-After": str(_lead_limit.retry_after(ip))})
    if lead.website.strip():
        # Бот: отвечаем как успехом, чтобы не учить его обходу, но ничего не сохраняем.
        logger.info("lead: отброшена заявка с заполненной ловушкой")
        return JSONResponse({"ok": True})
    errors = validate_lead(lead)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)
    with get_session() as db:
        row = q.create_lead(db, tariff=lead.tariff, name=lead.name.strip(), org=lead.org.strip(),
                            inn=lead.inn.strip(), email=lead.email.strip(), phone=lead.phone.strip(),
                            promo=lead.promo.strip())
    logger.info("lead: сохранена заявка #{} (тариф «{}»)", row.id, row.tariff)
    return JSONResponse({"ok": True})
