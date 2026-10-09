"""Аутентификация и роли для веб-UI 1.0: bcrypt-хэши + сессия-кука (Starlette SessionMiddleware).

Простая модель под ≤6 юзеров: пользователи пресозданы (`scripts/seed_users.py`), логин по
username+password, состояние — в ПОДПИСАННОЙ сессионной куке (`request.session['user_id']`,
подпись секретом `settings.SESSION_SECRET`). Роли: `expert` (чат+фидбек), `admin` (логи всех).
"""

from __future__ import annotations

import secrets

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


# Эпоха входа (`users.auth_epoch`, 09.10.2026): сессия и кука «запомнить меня» несут её вместе с
# user_id, и `current_user` сверяет с базой. Сброс пароля и блокировка поднимают эпоху — все уже
# выданные сессии и куки (на 30 дней!) перестают действовать сразу, а не когда истекут сами.
# До правки кука несла голый user_id — такие читаются как эпоха 0 и живут до первого сброса.
def make_remember_token(user_id: int, epoch: int = 0) -> str:
    return _remember.dumps({"u": user_id, "e": epoch})


def remember_from(request: Request) -> tuple[int, int] | None:
    """(user_id, эпоха) из куки «запомнить меня»; None — куки нет, подпись или срок не сошлись."""
    tok = request.cookies.get(REMEMBER_COOKIE)
    if not tok:
        return None
    try:
        data = _remember.loads(tok, max_age=REMEMBER_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(data, int) and not isinstance(data, bool):   # кука до 09.10.2026
        return data, 0
    if isinstance(data, dict) and isinstance(data.get("u"), int) and isinstance(data.get("e", 0), int):
        return data["u"], data.get("e", 0)
    return None


def uid_from_remember(request: Request) -> int | None:
    got = remember_from(request)
    return got[0] if got else None


def set_remember_cookie(response: Response, user_id: int, epoch: int = 0) -> None:
    response.set_cookie(REMEMBER_COOKIE, make_remember_token(user_id, epoch),
                        max_age=REMEMBER_MAX_AGE, httponly=True, samesite="lax",
                        secure=settings.COOKIE_SECURE)


def clear_remember_cookie(response: Response) -> None:
    response.delete_cookie(REMEMBER_COOKIE)


def generate_password() -> str:
    """Пароль новой учётки (создание из `/admin`, 09.10.2026). 12 символов из urlsafe-алфавита —
    длиннее восьмисимвольных паролей `seed_users.py`: их раздают людям, и перебор держит только
    лимит попыток входа."""
    return secrets.token_urlsafe(9)


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
    request.session["epoch"] = user.auth_epoch or 0


def logout_session(request: Request) -> None:
    request.session.clear()


def current_user(request: Request) -> User | None:
    """Текущий пользователь из сессии или None (мягкая проверка, для страниц).
    Если сессии нет, но есть валидный cookie «запомнить меня» — восстанавливаем сессию (автовход).

    Заблокированная учётка и устаревшая эпоха (сброс пароля, блокировка) — None и очищенная
    сессия: проверка на КАЖДОМ запросе, иначе блокировка ждала бы, пока человек сам выйдет."""
    uid = request.session.get("user_id")
    epoch = request.session.get("epoch", 0)
    if not uid:
        got = remember_from(request)
        if got:
            uid, epoch = got
            request.session["user_id"], request.session["epoch"] = uid, epoch
    if not uid:
        return None
    with get_session() as db:
        user = q.get_user(db, uid)
        if user:
            db.expunge(user)
    if user is None or user.blocked_at is not None or (user.auth_epoch or 0) != epoch:
        request.session.clear()
        return None
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
    """Профиль заполнен: согласие на ПДн + ФИО + регион.

    Решение владельца 09.10.2026: обязательны ФИО, регион, рабочий email и телефон; Telegram —
    по желанию. Email и телефон в гейт НЕ входят: их требует форма кабинета при сохранении, а
    действующие учётки (заполняли анкету, когда этих полей не было) ходят в чат как раньше и
    дозаполнят их при следующем сохранении. Новая учётка без них анкету не пройдёт — согласие
    ставит только успешное сохранение формы. Telegram из гейта убран вместе с обязательностью:
    иначе анкета без него сохранялась бы и возвращала человека на себя же."""
    return bool(user.consent and user.full_name and user.region)


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
