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
            ("plan", "VARCHAR(32)"),          # тариф (#160); у старых строк NULL — без лимита
            ("plan_started_at", "DATETIME"),
            ("plan_expires_at", "DATETIME"),  # срок тарифа (09.10.2026); NULL — бессрочно
            ("blocked_at", "DATETIME"),       # админка (09.10.2026): блокировка учётки
            ("auth_epoch", "INTEGER NOT NULL DEFAULT 0"),  # эпоха входа: сброс/блокировка гасят сессии
            ("position", "VARCHAR(128)"),     # кабинет (09.10.2026): контакты пользователя
            ("email", "VARCHAR(255)"),
            ("phone", "VARCHAR(32)"),
            ("org", "TEXT"),                  # организация и ИНН — заполняет admin
            ("inn", "VARCHAR(12)"),
        ],
        "leads": [  # работа с заявкой в админке (09.10.2026); старые заявки — «новые»
            ("status", "VARCHAR(16) NOT NULL DEFAULT 'new'"),
            ("status_at", "DATETIME"),
            ("note", "TEXT"),
            ("user_id", "INTEGER"),
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
