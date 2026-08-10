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
    # Синхронный SQLite для данных приложения 1.0 (users / messages / feedback) — CRUD
    # крошечный, async не нужен; эндпоинты объявлены как `def` → FastAPI гонит в threadpool.
    APP_DB_URL: str = "sqlite:///./navigator_app.db"
    # Секрет подписи сессионных кук. В проде ОБЯЗАТЕЛЬНО переопределить в .env.
    SESSION_SECRET: str = "dev-insecure-secret-change-in-env"
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "pp719"
    QDRANT_CASES_COLLECTION: str = "verified_cases"  # кейсы, подтверждённые экспертом ТПП
    # Корпус «Правила формирования и ведения реестра российской промышленной продукции» —
    # ОТДЕЛЬНАЯ коллекция (проза, а не товарные записи), чтобы не сдвигать калибровку out-of-scope
    # guard (dense_top1 по pp719) и не трогать измеренный товарный recall. Грузится scripts/load_rules_kb.py.
    QDRANT_RULES_COLLECTION: str = "pp719_rules"

    EMBEDDING_MODEL: str = "intfloat/multilingual-e5-large"
    EMBEDDING_DIM: int = 1024

    # LLM-реранкер top-k (стадия 2): +1 вызов DeepSeek на code-less запрос, поднял recall@1
    # 0.95→0.98. Применяется только без совпадения по коду ОКПД2. Можно выключить для скорости.
    RERANK_ENABLED: bool = True

    # Процедурный дефер-предохранитель: чистый процедурный вопрос (реестр/ГИСП/подача/сроки)
    # детектируется детерминированно (см. app/rag/procedural.py). Раньше такой вопрос СРАЗУ
    # деферился; теперь при PROCEDURAL_ANSWER_FROM_RULES он маршрутизируется на корпус Правил
    # (QDRANT_RULES_COLLECTION) и отвечается по нему, а дефер остаётся ЧЕСТНЫМ ФОЛБЭКОМ, если
    # корпус ничего релевантного не вернул (коллекции нет / пусто) — ноль выдумок процедуры.
    PROCEDURAL_DEFLECT_ENABLED: bool = True
    PROCEDURAL_ANSWER_FROM_RULES: bool = True

    # Лимиты частоты запросов (R12), скользящее окно 60 с на процесс. Чат — на пользователя,
    # вход — на IP. Значения с запасом под живого человека: ответ движка занимает 10–30 с, так
    # что 20 вопросов в минуту один человек физически не задаёт; 10 попыток входа в минуту с
    # одного адреса — тоже с запасом, а перебор пароля этим отсекается.
    RATE_LIMIT_CHAT_PER_MIN: int = 20
    RATE_LIMIT_LOGIN_PER_MIN: int = 10

    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = ""  # путь к файловому логу loguru; пусто = только stderr. На VM — /data/app.log (том)

    APP_TITLE: str = "Навигатор ПП РФ №719"
    # 0.5.0 — движок навигатора готов; цель 1.0 = чат-ассистент по 719 (см. ROADMAP.md)
    APP_VERSION: str = "0.5.0"
    ORG_NAME: str = "Курская ТПП"


settings = Settings()
