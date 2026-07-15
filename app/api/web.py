"""Веб-страницы UI 1.0 (Jinja2): вход, чат, приём формы обратной связи.

Страницы при отсутствии сессии РЕДИРЕКТЯТ на /login (не 401 — 401 только у /api/*, их ловит JS).
Шаблоны — app/web/templates, статика монтируется в main.py.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.api.auth import (
    authenticate,
    clear_remember_cookie,
    current_user,
    login_session,
    logout_session,
    needs_profile,
    require_user,
    require_user_profiled,
    set_remember_cookie,
)
from app.core.config import settings
from app.core.regions import REGIONS, region_from_username
from app.core.costs import cost_rub
from app.db import queries as q
from app.db.engine import get_session
from app.db.models import User
from app.rag import procedural

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
    if needs_profile(user):  # роль user без профиля/согласия → сначала анкета
        return RedirectResponse("/profile", status_code=302)
    # Все роли (вкл. admin) открываются на «главной» — чат с вводом. Дашборд `/admin`
    # доступен админу из сайдбара («Логи диалогов»), но не как стартовая страница.
    return RedirectResponse("/chat", status_code=302)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", _ctx(request, error=None))


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 remember: str = Form(default="")):
    user = authenticate(username.strip(), password)
    if not user:
        return templates.TemplateResponse(
            "login.html", _ctx(request, error="Неверный логин или пароль"), status_code=401
        )
    login_session(request, user)
    resp = RedirectResponse("/", status_code=302)
    if remember:  # «Запомнить меня» → персистентный cookie автовхода
        set_remember_cookie(resp, user.id)
    return resp


@router.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    logout_session(request)
    resp = RedirectResponse("/login", status_code=302)
    clear_remember_cookie(resp)  # выход снимает и автовход
    return resp


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if needs_profile(user):  # жёсткий гейт: роль user не в чат, пока не заполнит профиль+согласие
        return RedirectResponse("/profile", status_code=302)
    return templates.TemplateResponse("chat.html", _ctx(request, user=user))


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    prefill = user.region or region_from_username(user.username)
    return templates.TemplateResponse(
        "profile.html",
        _ctx(request, user=user, error=None, regions=REGIONS, prefill_region=prefill,
             full_name=user.full_name or "", telegram=user.telegram or "",
             consent=bool(user.consent)),
    )


@router.post("/profile", response_class=HTMLResponse)
def profile_submit(
    request: Request,
    consent: str = Form(default=""),
    full_name: str = Form(default=""),
    region: str = Form(default=""),
    telegram: str = Form(default=""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fn, rg, tg = full_name.strip(), region.strip(), telegram.strip()
    # Все поля обязательны (решение заказчика: жёсткий гейт). Незаполненное → дружелюбный ре-рендер.
    if not (consent and fn and rg and tg):
        return templates.TemplateResponse(
            "profile.html",
            _ctx(request, user=user,
                 error="Заполните ФИО, регион и Telegram и подтвердите согласие на обработку персональных данных.",
                 regions=REGIONS, prefill_region=rg or region_from_username(user.username),
                 full_name=fn, telegram=tg, consent=bool(consent)),
            status_code=400,
        )
    with get_session() as db:
        q.update_profile(db, user.id, full_name=fn, region=rg, telegram=tg, consent=True)
    return RedirectResponse("/chat", status_code=302)


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
    ans_ratings = []       # звёзды ответов 0..5 (kind='answer')
    ans_ratings_by_role: dict[str, list[int]] = defaultdict(list)  # сегментация приёмки: role → [звёзды]
    corrections = []       # исправления критич. ошибок (kind='answer', correction)
    answer_comments = []   # комментарии к ответам (kind='answer', comment)
    dialog_comments = []   # комментарии к диалогам (kind='dialog')
    procedural_questions = []  # вопросы, ушедшие в процедурный дефер (что спрашивают вне охвата → P2)

    with get_session() as db:
        for u in q.list_users(db):
            msgs = q.get_messages_for_user(db, u.id)
            by_sid, order = {}, []
            u_req = 0
            last_user = None  # последний вопрос эксперта — чтобы связать с дефером ассистента
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
                    last_user = m.content
                    u_req += 1
                    if day:
                        per_day[day]["requests"] += 1
                    # Процедурный интент считаем по САМОМУ вопросу (детектор pipeline'а) — так учитываются
                    # и отвеченные по Правилам, и деференные вопросы (не завязано на текст ответа).
                    if procedural.is_procedural(m.content or ""):
                        procedural_questions.append(
                            {"user": u.username, "q": (m.content or "")[:140], "ts": _fmt(m.ts)}
                        )
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
            msg_by_id = {m.id: m for m in msgs}
            for f in q.get_feedback_for_user(db, u.id):
                kind = f.kind or "service"
                if kind == "service":  # глобальная форма — как в мини-1.0
                    feedback.append({"rating": f.rating, "matched": f.matched, "comment": f.comment, "ts": _fmt(f.ts)})
                    if f.rating is not None:
                        ratings.append(f.rating)
                    if f.matched in matched_counts:
                        matched_counts[f.matched] += 1
                elif kind == "answer":  # звёзды 0..5 + опц. коммент/исправление, привязаны к ответу
                    if f.rating is not None:
                        ans_ratings.append(f.rating)
                        ans_ratings_by_role[u.role].append(f.rating)
                    orig = msg_by_id.get(f.message_id)
                    ans_txt = orig.content if orig else ""
                    snippet = (ans_txt[:220] + "…") if len(ans_txt) > 220 else ans_txt
                    if f.correction:
                        corrections.append({"user": u.username, "correction": f.correction,
                                            "ts": _fmt(f.ts), "answer": snippet})
                    if f.comment:
                        answer_comments.append({"user": u.username, "comment": f.comment,
                                                "ts": _fmt(f.ts), "answer": snippet})
                elif kind == "dialog" and f.comment:  # комментарий ко всей беседе
                    dialog_comments.append({"user": u.username, "comment": f.comment, "ts": _fmt(f.ts)})
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
    star_dist = [(i, sum(1 for r in ans_ratings if r == i)) for i in range(5, -1, -1)]
    avg_stars = round(sum(ans_ratings) / len(ans_ratings), 2) if ans_ratings else None
    def _accept(lst):  # доля оценок ≥4★ (гейт приёмки ≥70%)
        return round(100 * sum(1 for r in lst if r >= 4) / len(lst)) if lst else None
    accept_pct = _accept(ans_ratings)
    accept_user = _accept(ans_ratings_by_role.get("user", []))
    accept_expert = _accept(ans_ratings_by_role.get("expert", []))
    stats = {
        "users_total": len(data),
        "users": sum(1 for x in data if x["role"] == "user"),
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
        "answer_ratings": len(ans_ratings),
        "avg_stars": avg_stars,
        "accept_pct": accept_pct,
        "accept_user": accept_user,
        "accept_user_n": len(ans_ratings_by_role.get("user", [])),
        "accept_expert": accept_expert,
        "accept_expert_n": len(ans_ratings_by_role.get("expert", [])),
        "gate": 70,
        "star_dist": star_dist,
        "corrections": corrections,
        "answer_comments": answer_comments,
        "dialog_comments": dialog_comments,
        "procedural_questions": procedural_questions,
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
    kind: str = "service"          # answer | dialog | service
    rating: int | None = None      # answer: звёзды 0..5 / service: 1..5
    matched: str | None = None     # service: да | частично | нет
    comment: str | None = None     # dialog / service
    correction: str | None = None  # answer (опц.) — исправление критической ошибки
    session_id: str | None = None
    message_id: int | None = None


@router.post("/api/feedback")
def submit_feedback(fb: FeedbackIn, user: User = Depends(require_user_profiled)) -> dict:
    """Три канала (см. Feedback.kind): 'answer' — звёзды 0..5 + опц. исправление к ответу
    (нужен message_id); 'dialog' — комментарий к беседе (нужен session_id); 'service' —
    глобальная форма (оценка 1..5 + соответствие + текст), путь мини-1.0 без изменений."""
    kind = fb.kind if fb.kind in ("answer", "dialog", "service") else "service"
    if fb.rating is not None and not (0 <= fb.rating <= 5):
        raise HTTPException(status_code=422, detail="Оценка вне диапазона 0..5")
    comment = (fb.comment or "").strip() or None
    correction = (fb.correction or "").strip() or None
    if kind == "answer":
        if fb.message_id is None:
            raise HTTPException(status_code=422, detail="message_id обязателен для оценки ответа")
        if fb.rating is None and correction is None and comment is None:
            return {"ok": True, "skipped": True}  # пустой сигнал не храним
    if kind == "dialog":
        if not fb.session_id:
            raise HTTPException(status_code=422, detail="session_id обязателен для комментария к диалогу")
        if comment is None:
            return {"ok": True, "skipped": True}
    with get_session() as db:
        q.save_feedback(
            db, user_id=user.id, kind=kind, rating=fb.rating,
            matched=(fb.matched if kind == "service" else None),
            comment=comment,  # answer: коммент к ответу / dialog: к беседе / service: глоб. форма
            correction=(correction if kind == "answer" else None),
            session_id=fb.session_id, message_id=(fb.message_id if kind == "answer" else None),
        )
    return {"ok": True}
