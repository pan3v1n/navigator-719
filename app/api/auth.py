"""Аутентификация и роли для веб-UI 1.0: bcrypt-хэши + сессия-кука (Starlette SessionMiddleware).

Простая модель под ≤6 юзеров: пользователи пресозданы (`scripts/seed_users.py`), логин по
username+password, состояние — в ПОДПИСАННОЙ сессионной куке (`request.session['user_id']`,
подпись секретом `settings.SESSION_SECRET`). Роли: `expert` (чат+фидбек), `admin` (логи всех).
"""

from __future__ import annotations

import bcrypt
from fastapi import HTTPException, Request, status

from app.db import queries as q
from app.db.engine import get_session
from app.db.models import User


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
    """Текущий пользователь из сессии или None (мягкая проверка, для страниц)."""
    uid = request.session.get("user_id")
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
