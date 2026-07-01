"""SQLAlchemy-модели данных приложения 1.0: пользователи, реплики диалога, обратная связь.

Отдельная синхронная БД (SQLite, `settings.APP_DB_URL`) — НЕ путать с Qdrant (векторы 719)
и не с `DATABASE_URL` (async, зарезервирован под будущий Postgres). Здесь — операционные данные
мультиюзер-чата: кто, что спросил, что ответил движок, флаги качества, и фидбек. Логи видит admin.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="expert")  # expert | admin
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    feedback: Mapped[list["Feedback"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Message(Base):
    """Одна реплика диалога. role='user' — вопрос эксперта; role='assistant' — ответ движка.
    Одна беседа = один `session_id` (группировка реплик в тред)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # заполняются только у ответов ассистента:
    sources_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    low_relevance: Mapped[bool] = mapped_column(Boolean, default=False)
    unverified_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="messages")


class Feedback(Base):
    """Обратная связь эксперта в конце сессии (оценка + комментарий)."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # полезность 1..5
    matched: Mapped[str | None] = mapped_column(String(16), nullable=True)  # да | частично | нет
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="feedback")
