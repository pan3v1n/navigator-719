"""Пробный режим: кука гостя и счёт пробных вопросов — лёгкая часть без движка.

Отдельно от `app/api/guest.py` (эндпоинты), чтобы страница `/chat` (`web.py`) могла поставить куку
и показать остаток, не загружая чат-модуль с движком. Почему кука и какие потолки — см. guest.py.
"""

from __future__ import annotations

import re
import uuid

from itsdangerous import BadSignature, URLSafeSerializer

from app.core.config import settings
from app.db import queries as q

GUEST_COOKIE = "guest719"
GUEST_COOKIE_MAX_AGE = 365 * 24 * 3600
_signer = URLSafeSerializer(settings.SESSION_SECRET, salt="guest-719")
_GUEST_ID = re.compile(r"^[0-9a-f]{32}$")


def guest_id(request) -> str | None:
    """id гостя из подписанной куки; None — куки нет или подпись не сошлась."""
    raw = request.cookies.get(GUEST_COOKIE)
    if not raw:
        return None
    try:
        gid = _signer.loads(raw)
    except BadSignature:
        return None
    return gid if isinstance(gid, str) and _GUEST_ID.match(gid) else None


def new_guest_id() -> str:
    return uuid.uuid4().hex


def set_guest_cookie(response, gid: str) -> None:
    response.set_cookie(GUEST_COOKIE, _signer.dumps(gid), max_age=GUEST_COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", secure=settings.COOKIE_SECURE)


def trial_view(db, gid: str | None) -> dict:
    """Остаток пробных вопросов для интерфейса: строка под полем ввода и баннер."""
    limit = settings.GUEST_TRIAL_QUESTIONS
    used = min(q.count_guest_answers(db, gid), limit) if gid else 0
    return {"limit": limit, "used": used, "remaining": limit - used}
