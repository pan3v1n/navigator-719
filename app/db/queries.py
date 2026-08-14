"""CRUD-операции над данными приложения — тонкая обёртка над сессией SQLAlchemy.

Списки/JSON сериализуются здесь (sources/unverified кладём в *_json), чтобы вызывающий код
(эндпоинты) не знал про формат хранения.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Feedback, Message, User, _utcnow


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


def update_profile(
    db: Session, user_id: int, *, full_name: str, region: str, telegram: str, consent: bool
) -> User | None:
    """Сохраняет профиль пользователя (ФИО/регион/Telegram) и согласие на ПДн. Момент согласия
    (consent_at) фиксируем ОДИН раз при первой отметке — след 152-ФЗ. Возвращает User или None."""
    u = db.get(User, user_id)
    if not u:
        return None
    u.full_name = (full_name or "").strip()
    u.region = (region or "").strip()
    u.telegram = (telegram or "").strip()
    if consent and not u.consent:
        u.consent = True
        u.consent_at = _utcnow()
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


def get_message(db: Session, message_id: int) -> Message | None:
    """Реплика по id — для проверки владельца перед сохранением оценки (см. web.submit_feedback).
    Без этой проверки любой залогиненный мог оценить ЧУЖОЙ ответ, и оценка попадала в приёмку."""
    return db.get(Message, message_id)


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
    Заголовок = первое сообщение эксперта в беседе.

    R24: раньше функция поднимала В ПАМЯТЬ ВСЕ реплики пользователя — включая полные тексты
    ОТВЕТОВ, которые тут не нужны вовсе и составляют основной объём. И вызывается она на каждом
    открытии чата. Теперь: время последней активности — агрегатом в SQL (тексты не передаются),
    заголовки — только из реплик `user`."""
    from sqlalchemy import func

    last_ts = {
        sid: ts
        for sid, ts in db.execute(
            select(Message.session_id, func.max(Message.ts))
            .where(Message.user_id == user_id)
            .group_by(Message.session_id)
        )
    }
    # Идём от НОВЫХ к старым и перезаписываем — в итоге останется первый вопрос беседы
    # (та же семантика, что у прежнего прохода по возрастанию с «первым непустым»).
    titles: dict[str, str] = {}
    for sid, content in db.execute(
        select(Message.session_id, Message.content)
        .where(Message.user_id == user_id, Message.role == "user")
        .order_by(Message.ts.desc(), Message.id.desc())
    ):
        titles[sid] = content

    sessions = [
        {"session_id": sid, "title": titles.get(sid), "ts": ts}
        for sid, ts in last_ts.items()
    ]
    return sorted(sessions, key=lambda s: (s["ts"] is not None, s["ts"]), reverse=True)


def get_session_messages(db: Session, user_id: int, session_id: str) -> list[Message]:
    """Реплики конкретной беседы пользователя (для переоткрытия), по возрастанию ts."""
    return list(
        db.execute(
            select(Message)
            .where(Message.user_id == user_id, Message.session_id == session_id)
            .order_by(Message.ts)
        ).scalars()
    )


def delete_session(db: Session, user_id: int, session_id: str) -> tuple[int, int]:
    """Удаляет беседу пользователя: её реплики И привязанную к ним обратную связь.

    Оценки удаляем ВМЕСТЕ с репликами (R2). Раньше уходили только `Message`, а строки `Feedback`
    оставались висеть на несуществующем `message_id` — и продолжали учитываться в приёмочной
    метрике (гейт 1.0), при том что вопрос и ответ в скоркарте были пустые. То есть пользователь,
    удаляя свой чат, тихо искажал главную метрику проекта.

    Фильтр по `user_id` везде — чужое не тронуть. Всё в одной транзакции.
    Возвращает (удалено реплик, удалено записей обратной связи)."""
    from sqlalchemy import delete as _delete, or_

    msg_ids = list(
        db.execute(
            select(Message.id).where(
                Message.user_id == user_id, Message.session_id == session_id
            )
        ).scalars()
    )
    # Оценка привязана к беседе (session_id) ИЛИ к конкретной реплике (message_id) — чистим оба следа.
    fb_where = [Feedback.session_id == session_id]
    if msg_ids:
        fb_where.append(Feedback.message_id.in_(msg_ids))
    fb_res = db.execute(
        _delete(Feedback).where(Feedback.user_id == user_id, or_(*fb_where))
    )
    res = db.execute(
        _delete(Message).where(Message.user_id == user_id, Message.session_id == session_id)
    )
    db.commit()
    return res.rowcount or 0, fb_res.rowcount or 0


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
