from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень репозитория (app/core/config.py → app/core → app → корень).
# Путь к .env делаем абсолютным, чтобы конфиг грузился независимо от cwd
# (иначе запуск не из корня молча откатывает настройки к дефолтам).
_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ROOT / ".env", env_file_encoding="utf-8"
    )

    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    DATABASE_URL: str = "sqlite+aiosqlite:///./navigator.db"
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "pp719"
    QDRANT_CASES_COLLECTION: str = "verified_cases"  # кейсы, подтверждённые экспертом ТПП

    EMBEDDING_MODEL: str = "intfloat/multilingual-e5-large"
    EMBEDDING_DIM: int = 1024

    # LLM-реранкер top-k (стадия 2): +1 вызов DeepSeek на code-less запрос, поднял recall@1
    # 0.95→0.98. Применяется только без совпадения по коду ОКПД2. Можно выключить для скорости.
    RERANK_ENABLED: bool = True

    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    APP_TITLE: str = "Навигатор ПП РФ №719"
    # 0.5.0 — движок навигатора готов; цель 1.0 = чат-ассистент по 719 (см. ROADMAP.md)
    APP_VERSION: str = "0.5.0"
    ORG_NAME: str = "Курская ТПП"


settings = Settings()
