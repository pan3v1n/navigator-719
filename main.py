from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.api.chat import router as chat_router
from app.api.routes import router as navigate_router
from app.core.config import settings
from app.db.engine import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # таблицы приложения (users/messages/feedback), если их ещё нет — идемпотентно
    yield


app = FastAPI(
    title=settings.APP_TITLE,
    version=settings.APP_VERSION,
    description="Навигатор по ПП РФ №719 для Курской ТПП",
    lifespan=lifespan,
)

# Сессии-куки для auth веб-UI (подпись SESSION_SECRET). Для демо по http: https_only=False.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET,
    same_site="lax",
    https_only=False,
)

app.include_router(navigate_router, tags=["navigator"])
app.include_router(chat_router, tags=["chat"])


@app.get("/ping")
async def ping():
    return {"status": "ok", "app": settings.APP_TITLE, "version": settings.APP_VERSION}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=True)
