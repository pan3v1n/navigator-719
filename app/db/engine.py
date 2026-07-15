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
    _ensure_columns()


def _ensure_columns() -> None:
    """Лёгкая миграция демо-БД: добавляет недостающие колонки в существующие таблицы
    (SQLite ALTER ADD COLUMN). Нужна, т.к. схема приложения эволюционирует между версиями,
    а create_all не меняет уже созданные таблицы."""
    wanted = {
        "messages": [("prompt_tokens", "INTEGER"), ("completion_tokens", "INTEGER")],
        "feedback": [
            ("matched", "VARCHAR(16)"),
            ("kind", "VARCHAR(16) DEFAULT 'service'"),  # answer|dialog|service; легаси-строки → service
            ("session_id", "VARCHAR(36)"),
            ("message_id", "INTEGER"),
            ("correction", "TEXT"),
        ],
        "users": [  # профиль + согласие на ПДн (роль user); DEFAULT 0 → у старых строк consent=False
            ("consent", "BOOLEAN DEFAULT 0"),
            ("consent_at", "DATETIME"),
            ("full_name", "TEXT"),
            ("region", "VARCHAR(128)"),
            ("telegram", "VARCHAR(128)"),
        ],
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def get_session() -> Session:
    """Новая сессия БД. Использовать как контекст-менеджер: `with get_session() as db: ...`."""
    return SessionLocal()
