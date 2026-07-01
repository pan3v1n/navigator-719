"""Синхронный движок SQLite для данных приложения 1.0 (см. app/db/models.py).

Синхронный (не async) осознанно: CRUD крошечный, а эндпоинты объявлены как `def` → FastAPI
исполняет их в threadpool (как текущий `/navigate`). Это избавляет от async-сессий и гонок.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.models import Base

engine = create_engine(
    settings.APP_DB_URL,
    connect_args={"check_same_thread": False},  # SQLite + threadpool FastAPI
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет (идемпотентно). Зовётся на старте приложения."""
    Base.metadata.create_all(engine)


def get_session() -> Session:
    """Новая сессия БД. Использовать как контекст-менеджер: `with get_session() as db: ...`."""
    return SessionLocal()
