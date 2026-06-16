from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    DATABASE_URL: str = "sqlite+aiosqlite:///./navigator.db"
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "pp719"
    QDRANT_CASES_COLLECTION: str = "verified_cases"  # кейсы, подтверждённые экспертом ТПП

    EMBEDDING_MODEL: str = "intfloat/multilingual-e5-large"
    EMBEDDING_DIM: int = 1024

    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    APP_TITLE: str = "Навигатор ПП РФ №719"
    APP_VERSION: str = "0.1.0"
    ORG_NAME: str = "Курская ТПП"


settings = Settings()
