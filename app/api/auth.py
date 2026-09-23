"""Аутентификация и роли для веб-UI 1.0: bcrypt-хэши + сессия-кука (Starlette SessionMiddleware).

Простая модель под ≤6 юзеров: пользователи пресозданы (`scripts/seed_users.py`), логин по
username+password, состояние — в ПОДПИСАННОЙ сессионной куке (`request.session['user_id']`,
подпись секретом `settings.SESSION_SECRET`). Роли: `expert` (чат+фидбек), `admin` (логи всех).
"""

from __future__ import annotations

import bcrypt
from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import settings
from app.db import queries as q
from app.db.engine import get_session
from app.db.models import User

# «Запомнить меня»: подписанный cookie с user_id, переживает закрытие браузера (в отличие от
# сессионной куки, которая теперь session-only → логин требуется при каждом открытии сайта).
REMEMBER_COOKIE = "remember719"
REMEMBER_MAX_AGE = 30 * 24 * 3600  # 30 дней
_remember = URLSafeTimedSerializer(settings.SESSION_SECRET, salt="remember-719")


def make_remember_token(user_id: int) -> str:
    return _remember.dumps(user_id)


def uid_from_remember(request: Request) -> int | None:
    tok = request.cookies.get(REMEMBER_COOKIE)
    if not tok:
        return None
    try:
        return _remember.loads(tok, max_age=REMEMBER_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def set_remember_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(REMEMBER_COOKIE, make_remember_token(user_id),
                        max_age=REMEMBER_MAX_AGE, httponly=True, samesite="lax",
                        secure=settings.COOKIE_SECURE)


def clear_remember_cookie(response: Response) -> None:
    response.delete_cookie(REMEMBER_COOKIE)


def hash_password(password: str) -> str:
    """bcrypt-хэш пароля (соль внутри хэша)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def authenticate(username: str, password: str) -> User | None:
    """Проверяет логин/пароль. Возвращает отвязанный от сессии User при успехе, иначе None."""
    with get_session() as db:
        user = q.get_user_by_username(db, username)
        if user and verify_password(password, user.password_hash):
            db.expunge(user)  # объект переживёт закрытие сессии (нужны только скалярные поля)
            return user
    return None


def login_session(request: Request, user: User) -> None:
    request.session["user_id"] = user.id


def logout_session(request: Request) -> None:
    request.session.clear()


def current_user(request: Request) -> User | None:
    """Текущий пользователь из сессии или None (мягкая проверка, для страниц).
    Если сессии нет, но есть валидный cookie «запомнить меня» — восстанавливаем сессию (автовход)."""
    uid = request.session.get("user_id")
    if not uid:
        uid = uid_from_remember(request)
        if uid:
            request.session["user_id"] = uid
    if not uid:
        return None
    with get_session() as db:
        user = q.get_user(db, uid)
        if user:
            db.expunge(user)
        return user


def require_user(request: Request) -> User:
    """FastAPI-зависимость: требует авторизации, иначе 401 (фронт редиректит на /login)."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход")
    return user


def require_admin(request: Request) -> User:
    """FastAPI-зависимость: требует роль admin, иначе 401/403."""
    user = require_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ только для admin")
    return user


def profile_complete(user: User) -> bool:
    """Профиль заполнен: согласие на ПДн + ФИО + регион + Telegram (жёсткий гейт заказчика)."""
    return bool(user.consent and user.full_name and user.region and user.telegram)


def needs_profile(user: User) -> bool:
    """Роль `user` (региональный тестировщик) обязана заполнить профиль/согласие до чата.
    Для admin/expert всегда False — их через профиль НЕ гоняем (внутренние роли)."""
    return user.role == "user" and not profile_complete(user)


def require_user_profiled(request: Request) -> User:
    """Как require_user, но роль `user` без заполненного профиля → 403 (JSON-фронт редиректит на
    /profile). Гейт на УРОВНЕ РОУТА (после current_user) — держит и при автологине «запомнить меня»."""
    user = require_user(request)
    if needs_profile(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="profile_required")
    return user
