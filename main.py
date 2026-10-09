from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from app.api.chat import router as chat_router
from app.api.guest import router as guest_router
from app.api.history import router as history_router
from app.api.leads import router as leads_router
from app.api.routes import router as navigate_router
from app.api.web import router as web_router
from app.api.admin import router as admin_router
from app.core.config import settings
from app.core.release import release_label
from app.db.engine import init_db


_DEFAULT_SECRET = "dev-insecure-secret-change-in-env"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Гард секрета: публичный дефолт SESSION_SECRET (лежит в репо) позволяет подделать сессию и
    # войти как admin. Локально (development) — громкое предупреждение; в проде — не стартуем.
    if settings.SESSION_SECRET == _DEFAULT_SECRET:
        msg = ("SESSION_SECRET не задан в .env — используется ПУБЛИЧНЫЙ дефолт из репозитория "
               "(позволяет подделать сессию и войти как admin)")
        if settings.APP_ENV == "development":
            logger.warning(msg + ". Для прод-деплоя ОБЯЗАТЕЛЬНО задайте случайный SESSION_SECRET.")
        else:
            raise RuntimeError(msg + f". Задайте случайный SESSION_SECRET (APP_ENV={settings.APP_ENV}).")
    if settings.APP_ENV != "development" and not settings.COOKIE_SECURE:
        # Не останавливаем: до появления домена бой законно жил по http. Но молчать нельзя —
        # без флага кука уйдёт по http, если кто-то откроет сервис по голому адресу.
        logger.warning("COOKIE_SECURE=false при APP_ENV=%s: куки сессии могут уйти по http. "
                       "За HTTPS-прокси (Caddy) задайте COOKIE_SECURE=true." % settings.APP_ENV)
    if settings.LOG_FILE:  # персистентный файловый лог (на VM — на томе, переживает редеплой)
        logger.add(settings.LOG_FILE, rotation="10 MB", retention="14 days",
                   encoding="utf-8", enqueue=True, level=settings.LOG_LEVEL)
    init_db()  # таблицы приложения (users/messages/feedback), идемпотентно
    yield


def api_docs_kwargs(app_env: str) -> dict:
    """Адреса автодокументации FastAPI: локально — дефолтные, вне `development` — выключены (`O9` #145).

    Дефолт FastAPI публикует `/docs`, `/redoc` и `/openapi.json`, то есть карту эндпоинтов вместе
    с `/api/admin/export`. За 08–22.09.2026 сканеры получили её 11 раз; авторизация держала (перебор
    14.09 → 401), но раздавать карту незачем. Условие то же, что у гарда секрета: всё, что не
    `development`, считается боем."""
    if app_env == "development":
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(
    title=settings.APP_TITLE,
    version=settings.APP_VERSION,
    description="Навигатор по ПП РФ №719 для Курской ТПП",
    lifespan=lifespan,
    **api_docs_kwargs(settings.APP_ENV),
)

# Сессии-куки для auth веб-UI (подпись SESSION_SECRET). `https_only` — из COOKIE_SECURE: локально
# и на демо по http выключен, за Caddy с HTTPS включается в .env (см. app/core/config.py).
# max_age=None → session-only cookie (умирает при закрытии браузера): логин требуется при каждом
# открытии сайта. Персистентный автовход — через cookie «запомнить меня» (app/api/auth.py).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET,
    same_site="lax",
    https_only=settings.COOKIE_SECURE,
    max_age=None,
)

app.include_router(navigate_router, tags=["navigator"])
app.include_router(chat_router, tags=["chat"])
app.include_router(guest_router, tags=["chat"])  # пробный режим без входа (09.10.2026)
app.include_router(history_router, tags=["chat"])  # Б3: страница «История запросов»
app.include_router(web_router, tags=["web"])
app.include_router(admin_router, tags=["admin"])  # админ-панель /admin (пересборка 09.10.2026)
app.include_router(leads_router, tags=["leads"])  # форма заявки лендинга (caddy проксирует /api/leads корня)

# Статика веб-фронта (css/js)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "app" / "web" / "static")),
    name="static",
)


@app.get("/ping")
async def ping():
    # ⚠ `release` рядом с `version` намеренно: без него пометку версии во фронте нечем
    # проверить с самой VM — только глазами в браузере. Релизная проверка выкатки читает её
    # отсюда и сверяет с тегом профиля.
    return {"status": "ok", "app": settings.APP_TITLE, "version": settings.APP_VERSION,
            "release": release_label()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=True)
