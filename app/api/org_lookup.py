"""`POST /api/org-lookup` — организация по ИНН для кабинета и карточки пользователя в `/admin`
(решение владельца 29.10.2026; сам поиск — `app/core/org_registry.py`).

Только для вошедших: открытый адрес позволил бы кому угодно расходовать суточный лимит DaData — по
той же причине его нет у формы лендинга. POST, а не GET: в запросе и название, которое вписал
человек (для сверки), а адрес запроса попадает в журнал доступа.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.auth import require_user
from app.api.ratelimit import SlidingWindow
from app.core import org_registry as reg
from app.core.inn import inn_valid
from app.db.models import User

router = APIRouter()

# Человек вводит ИНН раз-другой, admin открывает карточки подряд — с запасом на обоих; ниже лимита DaData
# на порядки, а повтор того же ИНН берётся из кэша.
LOOKUPS_PER_HOUR = 60
_limit = SlidingWindow(LOOKUPS_PER_HOUR, window=3600.0)


class LookupIn(BaseModel):
    inn: str = ""
    name: str = ""     # вписанное название — ответ скажет, совпадает ли оно с реестром


@router.post("/api/org-lookup")
def org_lookup(body: LookupIn, user: User = Depends(require_user)):
    inn = body.inn.strip()
    if len(inn) == 12 and inn.isascii() and inn.isdigit():
        # Решение владельца: ИНН ИП и физлица (ПДн) наружу не отправляем — и не делаем вид, что искали
        raise HTTPException(status_code=422, detail="По ИНН ИП название не подбирается — впишите его сами.")
    if not inn_valid(inn):
        raise HTTPException(status_code=422, detail="Проверьте ИНН: 10 цифр у организации, контрольные "
                                                    "цифры не сходятся.")
    if not reg.enabled():
        return {"outcome": reg.UNAVAILABLE}
    if not _limit.check(f"user:{user.id}"):
        return JSONResponse({"outcome": reg.UNAVAILABLE, "detail": "Слишком много проверок подряд — "
                             "попробуйте через час."}, status_code=429,
                            headers={"Retry-After": str(_limit.retry_after(f"user:{user.id}"))})
    res = reg.lookup(inn)
    if res is None or res.org is None:
        return {"outcome": res.outcome if res else reg.UNAVAILABLE}
    o = res.org
    return {"outcome": res.outcome,
            "org": {"name": o.name, "full_name": o.full_name, "inn": o.inn, "kpp": o.kpp, "ogrn": o.ogrn,
                    "active": o.active, "status_text": o.status_text, "address": o.address},
            "matches": reg.name_matches(o, body.name) if body.name.strip() else None}
