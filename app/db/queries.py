"""CRUD-операции над данными приложения — тонкая обёртка над сессией SQLAlchemy.

Списки/JSON сериализуются здесь (sources/unverified кладём в *_json), чтобы вызывающий код
(эндпоинты) не знал про формат хранения.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Feedback, Message, User


# --- users ---------------------------------------------------------------
def get_user_by_username(db: Session, username: str) -> User | None:
    return db.execute(select(User).where(User.username == username)).scalar_one_or_none()


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def create_user(db: Session, username: str, password_hash: str, role: str = "expert") -> User:
    u = User(username=username, password_hash=password_hash, role=role)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def list_users(db: Session) -> list[User]:
    return list(db.execute(select(User).order_by(User.id)).scalars())


# --- messages (лог диалога) ----------------------------------------------
def log_message(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    role: str,
    content: str,
    sources: list | None = None,
    low_relevance: bool = False,
    unverified: list | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> Message:
    m = Message(
        user_id=user_id,
        session_id=session_id,
        role=role,
        content=content,
        sources_json=json.dumps(sources, ensure_ascii=False) if sources is not None else None,
        low_relevance=low_relevance,
        unverified_json=json.dumps(unverified, ensure_ascii=False) if unverified else None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def get_messages_for_user(db: Session, user_id: int) -> list[Message]:
    return list(
        db.execute(
            select(Message).where(Message.user_id == user_id).order_by(Message.ts)
        ).scalars()
    )


def get_all_messages(db: Session) -> list[Message]:
    return list(db.execute(select(Message).order_by(Message.ts)).scalars())


def get_user_sessions(db: Session, user_id: int) -> list[dict]:
    """Беседы пользователя для сайдбара: [{session_id, title, ts}], новые сверху.
    Заголовок = первое сообщение эксперта в беседе."""
    sessions: dict[str, dict] = {}
    for m in get_messages_for_user(db, user_id):  # по возрастанию ts
        s = sessions.setdefault(m.session_id, {"session_id": m.session_id, "title": None, "ts": m.ts})
        if s["title"] is None and m.role == "user":
            s["title"] = m.content
        s["ts"] = m.ts  # последняя реплика (проход по возрастанию → остаётся максимум)
    return sorted(sessions.values(), key=lambda s: s["ts"], reverse=True)


def get_session_messages(db: Session, user_id: int, session_id: str) -> list[Message]:
    """Реплики конкретной беседы пользователя (для переоткрытия), по возрастанию ts."""
    return list(
        db.execute(
            select(Message)
            .where(Message.user_id == user_id, Message.session_id == session_id)
            .order_by(Message.ts)
        ).scalars()
    )


def delete_session(db: Session, user_id: int, session_id: str) -> int:
    """Удаляет беседу пользователя (все её реплики). Фильтр по user_id — чужое не тронуть.
    Возвращает число удалённых реплик."""
    from sqlalchemy import delete as _delete

    res = db.execute(
        _delete(Message).where(Message.user_id == user_id, Message.session_id == session_id)
    )
    db.commit()
    return res.rowcount or 0


# --- feedback ------------------------------------------------------------
def save_feedback(
    db: Session, *, user_id: int, kind: str = "service", rating: int | None = None,
    matched: str | None = None, comment: str | None = None, correction: str | None = None,
    session_id: str | None = None, message_id: int | None = None,
) -> Feedback:
    f = Feedback(
        user_id=user_id, kind=kind, rating=rating, matched=matched, comment=comment,
        correction=correction, session_id=session_id, message_id=message_id,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def get_feedback_for_user(db: Session, user_id: int) -> list[Feedback]:
    return list(
        db.execute(
            select(Feedback).where(Feedback.user_id == user_id).order_by(Feedback.ts)
        ).scalars()
    )


def get_all_feedback(db: Session) -> list[Feedback]:
    return list(db.execute(select(Feedback).order_by(Feedback.ts)).scalars())
