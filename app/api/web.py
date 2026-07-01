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
from app.core.costs import cost_rub
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

    from collections import defaultdict

    # Собираем ЗА ОДИН проход: детальные логи (юзер → беседы → реплики, с датами, всё в плоских
    # структурах — объекты БД после закрытия detached) + агрегаты для дашборда.
    def _fmt(ts):
        return ts.strftime("%d.%m.%Y %H:%M") if ts else ""

    data = []
    g_prompt = g_completion = 0
    total_requests = total_answers = total_conversations = 0
    flags_unverified = flags_lowrel = 0
    per_day = defaultdict(lambda: {"requests": 0, "tokens": 0})
    per_user_stat = []
    matched_counts = {"да": 0, "частично": 0, "нет": 0}
    ratings = []

    with get_session() as db:
        for u in q.list_users(db):
            msgs = q.get_messages_for_user(db, u.id)
            by_sid, order = {}, []
            u_req = 0
            for m in msgs:
                s = by_sid.get(m.session_id)
                if s is None:
                    s = {"sid": m.session_id, "title": None, "date": "", "messages": [],
                         "prompt": 0, "completion": 0}
                    by_sid[m.session_id] = s
                    order.append(m.session_id)
                if s["title"] is None and m.role == "user":
                    s["title"] = m.content
                if not s["date"] and m.ts:
                    s["date"] = _fmt(m.ts)
                tok = (m.prompt_tokens or 0) + (m.completion_tokens or 0)
                s["messages"].append({
                    "role": m.role, "content": m.content, "ts": _fmt(m.ts),
                    "low_relevance": m.low_relevance, "unverified": m.unverified_json, "tokens": tok,
                })
                s["prompt"] += m.prompt_tokens or 0
                s["completion"] += m.completion_tokens or 0
                day = m.ts.strftime("%d.%m") if m.ts else None
                if m.role == "user":
                    u_req += 1
                    if day:
                        per_day[day]["requests"] += 1
                else:
                    total_answers += 1
                    if day:
                        per_day[day]["tokens"] += tok
                    if m.unverified_json:
                        flags_unverified += 1
                    if m.low_relevance:
                        flags_lowrel += 1
            u_prompt = u_completion = 0
            sessions = []
            for sid in reversed(order):  # новые беседы сверху
                s = by_sid[sid]
                s["title"] = (s["title"] or "Диалог")[:60]
                s["tokens"] = s["prompt"] + s["completion"]
                s["cost"] = cost_rub(s["prompt"], s["completion"])
                u_prompt += s["prompt"]
                u_completion += s["completion"]
                sessions.append(s)
            feedback = []
            for f in q.get_feedback_for_user(db, u.id):
                feedback.append({"rating": f.rating, "matched": f.matched, "comment": f.comment, "ts": _fmt(f.ts)})
                if f.rating is not None:
                    ratings.append(f.rating)
                if f.matched in matched_counts:
                    matched_counts[f.matched] += 1
            g_prompt += u_prompt
            g_completion += u_completion
            total_requests += u_req
            total_conversations += len(order)
            u_tokens, u_cost = u_prompt + u_completion, cost_rub(u_prompt, u_completion)
            data.append({
                "username": u.username, "role": u.role, "msg_count": len(msgs),
                "conversations": len(order), "requests": u_req,
                "sessions": sessions, "feedback": feedback, "tokens": u_tokens, "cost": u_cost,
            })
            per_user_stat.append({"username": u.username, "requests": u_req, "tokens": u_tokens, "cost": u_cost})

    per_user_stat.sort(key=lambda x: x["requests"], reverse=True)
    per_day_list = [{"date": d, "requests": v["requests"], "tokens": v["tokens"]}
                    for d, v in sorted(per_day.items())][-14:]
    tokens_total, cost_total = g_prompt + g_completion, cost_rub(g_prompt, g_completion)
    stats = {
        "users_total": len(data),
        "experts": sum(1 for x in data if x["role"] == "expert"),
        "admins": sum(1 for x in data if x["role"] == "admin"),
        "conversations": total_conversations,
        "requests": total_requests,
        "answers": total_answers,
        "tokens": tokens_total,
        "cost": cost_total,
        "avg_tokens": round(tokens_total / total_answers) if total_answers else 0,
        "avg_cost": round(cost_total / total_answers, 3) if total_answers else 0,
        "feedback_count": sum(len(x["feedback"]) for x in data),
        "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
        "matched": matched_counts,
        "flags_unverified": flags_unverified,
        "flags_lowrel": flags_lowrel,
        "per_user": per_user_stat,
        "per_day": per_day_list,
        "max_requests": per_user_stat[0]["requests"] if per_user_stat else 0,
        "max_cost": max((x["cost"] for x in per_user_stat), default=0),
        "max_day": max((d["requests"] for d in per_day_list), default=0),
    }
    totals = {"tokens": tokens_total, "cost": cost_total}
    return templates.TemplateResponse(
        "admin.html", _ctx(request, admin=user, data=data, totals=totals, stats=stats)
    )


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
