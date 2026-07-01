"""Веб-страницы UI 1.0 (Jinja2): вход, чат, приём формы обратной связи.

Страницы при отсутствии сессии РЕДИРЕКТЯТ на /login (не 401 — 401 только у /api/*, их ловит JS).
Шаблоны — app/web/templates, статика монтируется в main.py.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.api.auth import (
    authenticate,
    current_user,
    login_session,
    logout_session,
    require_user,
)
from app.core.config import settings
from app.db import queries as q
from app.db.engine import get_session
from app.db.models import User

router = APIRouter()
_WEB = Path(__file__).resolve().parents[1] / "web"
templates = Jinja2Templates(directory=str(_WEB / "templates"))


def _ctx(request: Request, **kw) -> dict:
    return {"request": request, "app_title": settings.APP_TITLE, "org": settings.ORG_NAME, **kw}


@router.get("/", response_class=HTMLResponse)
def root(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/admin" if user.role == "admin" else "/chat", status_code=302)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", _ctx(request, error=None))


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = authenticate(username.strip(), password)
    if not user:
        return templates.TemplateResponse(
            "login.html", _ctx(request, error="Неверный логин или пароль"), status_code=401
        )
    login_session(request, user)
    return RedirectResponse("/", status_code=302)


@router.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/login", status_code=302)


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("chat.html", _ctx(request, user=user))


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    """Логи диалогов и отзывы по каждому пользователю. Только для роли admin."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "admin":
        return RedirectResponse("/chat", status_code=302)  # эксперт логи не видит

    # Извлекаем в ПЛОСКИЕ структуры внутри сессии (объекты БД после закрытия — detached).
    data = []
    with get_session() as db:
        for u in q.list_users(db):
            msgs = q.get_messages_for_user(db, u.id)
            sessions: dict[str, list[dict]] = {}
            for m in msgs:
                sessions.setdefault(m.session_id, []).append({
                    "role": m.role, "content": m.content, "ts": m.ts,
                    "low_relevance": m.low_relevance, "unverified": m.unverified_json,
                })
            feedback = [
                {"rating": f.rating, "matched": f.matched, "comment": f.comment, "ts": f.ts}
                for f in q.get_feedback_for_user(db, u.id)
            ]
            data.append({
                "username": u.username, "role": u.role, "msg_count": len(msgs),
                "sessions": sessions, "feedback": feedback,
            })
    return templates.TemplateResponse("admin.html", _ctx(request, admin=user, data=data))


class FeedbackIn(BaseModel):
    rating: int | None = None
    matched: str | None = None
    comment: str | None = None


@router.post("/api/feedback")
def submit_feedback(fb: FeedbackIn, user: User = Depends(require_user)) -> dict:
    with get_session() as db:
        q.save_feedback(
            db, user_id=user.id, rating=fb.rating, matched=fb.matched,
            comment=(fb.comment or "").strip() or None,
        )
    return {"ok": True}
