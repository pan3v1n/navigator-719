"""Чат-эндпоинт веб-UI 1.0: вопрос эксперта → движок навигатора → лог диалога в БД → ответ.

Эндпоинт СИНХРОННЫЙ (`def`) — FastAPI исполняет его в threadpool (как `/navigate`); движок
(`pipeline.answer`: e5 + Qdrant + DeepSeek) блокирующий, event loop не держим. Диалог группируется
по `session_id` (одна беседа = один id). Реплики (вопрос + ответ + флаги качества) логируются в БД —
их видит admin. History-aware follow-up («а какой порог?») — отложенный fast-follow: сейчас каждый
вопрос самостоятелен.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from loguru import logger
from pydantic import BaseModel, Field

from app.api.auth import require_user
from app.db import queries as q
from app.db.engine import get_session
from app.db.models import User
from app.rag.pipeline import answer
from app.tools.navigator import extract_okpd2

router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None  # None → начать новую беседу


class SourceItem(BaseModel):
    section: str | None = None
    product_name: str
    okpd2: list[str] = []
    source_anchor: str | None = None
    okpd2_match: bool = False


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceItem]
    low_relevance: bool
    unverified_numbers: list[str]
    session_id: str
    message_id: int | None = None  # id сохранённой реплики-ответа — для привязки оценки/исправления


def _sources_from_hits(hits) -> list[SourceItem]:
    return [
        SourceItem(
            section=h.section_title or h.section_roman,
            product_name=h.product_name,
            okpd2=h.okpd2_codes or [],
            source_anchor=h.source_anchor,
            okpd2_match=h.okpd2_match,
        )
        for h in hits
    ]


def _load_history(user_id: int, session_id: str, max_msgs: int = 4) -> list[dict]:
    """Последние реплики беседы для мультитёрн-контекста (ответы ассистента усекаем).
    Пусто для новой беседы — тогда движок ведёт себя как одиночный вопрос."""
    try:
        with get_session() as db:
            prev = q.get_session_messages(db, user_id, session_id)
    except Exception:  # noqa: BLE001 — история не критична, не роняем ответ
        return []
    return [
        {"role": m.role, "content": m.content if m.role == "user" else m.content[:600]}
        for m in prev[-max_msgs:]
    ]


@router.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: User = Depends(require_user)) -> ChatResponse:
    session_id = req.session_id or uuid.uuid4().hex
    history = _load_history(user.id, session_id)  # мультитёрн: прошлые ходы беседы (пусто для новой)
    okpd2 = extract_okpd2(req.message)  # код ОКПД2 в тексте → авторитетный иерархический буст + правило 3а
    try:
        ans = answer(req.message, okpd2=okpd2, history=history)
    except Exception:  # noqa: BLE001 — наружу дружелюбно, детали в лог (рваная сеть/DeepSeek)
        logger.exception(f"chat: движок упал на запросе от user_id={user.id}")
        raise HTTPException(status_code=503, detail="Сервис временно недоступен, повторите запрос.")

    sources = _sources_from_hits(ans.hits)
    message_id: int | None = None
    try:
        with get_session() as db:
            q.log_message(db, user_id=user.id, session_id=session_id, role="user", content=req.message)
            asst = q.log_message(
                db, user_id=user.id, session_id=session_id, role="assistant", content=ans.text,
                sources=[s.model_dump() for s in sources],
                low_relevance=ans.low_relevance, unverified=ans.unverified_numbers,
                prompt_tokens=ans.prompt_tokens, completion_tokens=ans.completion_tokens,
            )
            message_id = asst.id
    except Exception:  # noqa: BLE001 — лог не должен ронять ответ эксперту
        logger.exception(f"chat: не удалось записать лог диалога (user_id={user.id})")

    return ChatResponse(
        answer=ans.text, sources=sources, low_relevance=ans.low_relevance,
        unverified_numbers=ans.unverified_numbers, session_id=session_id, message_id=message_id,
    )


@router.get("/api/conversations")
def list_conversations(user: User = Depends(require_user)) -> list[dict]:
    """Список бесед пользователя для сайдбара (новые сверху)."""
    with get_session() as db:
        sess = q.get_user_sessions(db, user.id)
    return [
        {"session_id": s["session_id"], "title": (s["title"] or "Диалог")[:48],
         "ts": s["ts"].isoformat()}
        for s in sess
    ]


@router.get("/api/conversations/{session_id}")
def get_conversation(session_id: str, user: User = Depends(require_user)) -> dict:
    """Реплики одной беседы для переоткрытия (только свои — фильтр по user_id)."""
    with get_session() as db:
        msgs = q.get_session_messages(db, user.id, session_id)
        out = []
        for m in msgs:
            item = {"role": m.role, "content": m.content}
            if m.sources_json:
                item["sources"] = json.loads(m.sources_json)
            out.append(item)
    return {"session_id": session_id, "messages": out}


@router.delete("/api/conversations/{session_id}")
def delete_conversation(session_id: str, user: User = Depends(require_user)) -> dict:
    """Удалить беседу пользователя (только свою — фильтр по user_id)."""
    with get_session() as db:
        removed = q.delete_session(db, user.id, session_id)
    return {"ok": True, "removed": removed}


def _serialize_messages(msgs, include_sources: bool = True) -> list[dict]:
    out = []
    for m in msgs:
        d = {"role": m.role, "content": m.content, "ts": m.ts.isoformat() if m.ts else None}
        if include_sources and m.sources_json:
            d["sources"] = json.loads(m.sources_json)
        out.append(d)
    return out


def _parse_date(s: str):
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


def _convs_to_markdown(user_name: str, picked) -> str:
    lines = [f"# Экспорт диалогов — {user_name}", ""]
    for s, msgs in picked:
        lines.append(f"## {s['title'] or 'Диалог'}")
        if s["ts"]:
            lines.append(f"_{s['ts'].strftime('%d.%m.%Y %H:%M')}_")
        lines.append("")
        for m in msgs:
            who = "Эксперт" if m.role == "user" else "Ассистент"
            lines += [f"**{who}:**", "", m.content, ""]
        lines += ["---", ""]
    return "\n".join(lines)


def _convs_to_text(user_name: str, picked) -> str:
    lines = [f"Экспорт диалогов — {user_name}", "=" * 40, ""]
    for s, msgs in picked:
        stamp = f"  [{s['ts'].strftime('%d.%m.%Y %H:%M')}]" if s["ts"] else ""
        lines += [(s["title"] or "Диалог") + stamp, "-" * 30]
        for m in msgs:
            who = "Эксперт" if m.role == "user" else "Ассистент"
            lines += [f"{who}:", m.content, ""]
        lines.append("")
    return "\n".join(lines)


def _json_download(payload: dict, filename: str) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _download(content: str, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _conv_to_markdown(title: str, msgs) -> str:
    lines = [f"# {title}", ""]
    for m in msgs:
        who = "Эксперт" if m.role == "user" else "Ассистент"
        lines += [f"**{who}:**", "", m.content, ""]
    return "\n".join(lines)


def _conv_to_text(title: str, msgs) -> str:
    lines = [title, "=" * min(len(title), 60), ""]
    for m in msgs:
        who = "Эксперт" if m.role == "user" else "Ассистент"
        lines += [f"{who}:", m.content, ""]
    return "\n".join(lines)


@router.get("/api/export")
def export_conversations(
    fmt: str = "json", date_from: str = "", date_to: str = "", sources: int = 1,
    user: User = Depends(require_user),
) -> Response:
    """Скачать беседы пользователя с настройками: формат (json/md/txt), период дат, источники."""
    df, dt = _parse_date(date_from), _parse_date(date_to)
    incl_sources = bool(sources)
    picked = []  # список (session-dict, msgs) после фильтра по датам
    with get_session() as db:
        for s in q.get_user_sessions(db, user.id):
            d = s["ts"].date() if s["ts"] else None
            if df and (d is None or d < df):
                continue
            if dt and (d is None or d > dt):
                continue
            picked.append((s, q.get_session_messages(db, user.id, s["session_id"])))

    base = "navigator719-chats"
    if fmt == "md":
        return _download(_convs_to_markdown(user.username, picked), "text/markdown; charset=utf-8", base + ".md")
    if fmt == "txt":
        return _download(_convs_to_text(user.username, picked), "text/plain; charset=utf-8", base + ".txt")
    conversations = [
        {"session_id": s["session_id"], "title": s["title"],
         "messages": _serialize_messages(msgs, incl_sources)}
        for s, msgs in picked
    ]
    return _json_download(
        {"user": user.username,
         "filters": {"date_from": date_from or None, "date_to": date_to or None, "sources": incl_sources},
         "exported_conversations": len(conversations), "conversations": conversations},
        base + ".json",
    )


@router.get("/api/conversations/{session_id}/export")
def export_conversation(session_id: str, fmt: str = "json", user: User = Depends(require_user)) -> Response:
    """Скачать ОДНУ беседу пользователя (только свою). Формат fmt: json | md | txt."""
    with get_session() as db:
        msgs = q.get_session_messages(db, user.id, session_id)
        title = next((m.content for m in msgs if m.role == "user"), "Диалог")
    base = f"navigator719-chat-{session_id[:8]}"
    if fmt == "md":
        return _download(_conv_to_markdown(title, msgs), "text/markdown; charset=utf-8", base + ".md")
    if fmt == "txt":
        return _download(_conv_to_text(title, msgs), "text/plain; charset=utf-8", base + ".txt")
    return _json_download(
        {"user": user.username, "conversation": {
            "session_id": session_id, "title": title, "messages": _serialize_messages(msgs)}},
        base + ".json",
    )
