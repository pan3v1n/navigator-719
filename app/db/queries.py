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
) -> Message:
    m = Message(
        user_id=user_id,
        session_id=session_id,
        role=role,
        content=content,
        sources_json=json.dumps(sources, ensure_ascii=False) if sources is not None else None,
        low_relevance=low_relevance,
        unverified_json=json.dumps(unverified, ensure_ascii=False) if unverified else None,
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


# --- feedback ------------------------------------------------------------
def save_feedback(db: Session, *, user_id: int, rating: int | None, comment: str | None) -> Feedback:
    f = Feedback(user_id=user_id, rating=rating, comment=comment)
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
