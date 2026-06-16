from fastapi import FastAPI
from app.api.routes import router as navigate_router
from app.core.config import settings

app = FastAPI(
    title=settings.APP_TITLE,
    version=settings.APP_VERSION,
    description="Навигатор по ПП РФ №719 для Курской ТПП",
)

app.include_router(navigate_router, tags=["navigator"])


@app.get("/ping")
async def ping():
    return {"status": "ok", "app": settings.APP_TITLE, "version": settings.APP_VERSION}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=True)
