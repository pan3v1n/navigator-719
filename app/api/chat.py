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


@router.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: User = Depends(require_user)) -> ChatResponse:
    session_id = req.session_id or uuid.uuid4().hex
    try:
        ans = answer(req.message)
    except Exception:  # noqa: BLE001 — наружу дружелюбно, детали в лог (рваная сеть/DeepSeek)
        logger.exception("chat: движок упал на запросе от user_id=%s", user.id)
        raise HTTPException(status_code=503, detail="Сервис временно недоступен, повторите запрос.")

    sources = _sources_from_hits(ans.hits)
    try:
        with get_session() as db:
            q.log_message(db, user_id=user.id, session_id=session_id, role="user", content=req.message)
            q.log_message(
                db, user_id=user.id, session_id=session_id, role="assistant", content=ans.text,
                sources=[s.model_dump() for s in sources],
                low_relevance=ans.low_relevance, unverified=ans.unverified_numbers,
                prompt_tokens=ans.prompt_tokens, completion_tokens=ans.completion_tokens,
            )
    except Exception:  # noqa: BLE001 — лог не должен ронять ответ эксперту
        logger.exception("chat: не удалось записать лог диалога (user_id=%s)", user.id)

    return ChatResponse(
        answer=ans.text, sources=sources, low_relevance=ans.low_relevance,
        unverified_numbers=ans.unverified_numbers, session_id=session_id,
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


def _serialize_messages(msgs) -> list[dict]:
    return [
        {
            "role": m.role,
            "content": m.content,
            "ts": m.ts.isoformat() if m.ts else None,
            "sources": json.loads(m.sources_json) if m.sources_json else None,
        }
        for m in msgs
    ]


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
def export_conversations(user: User = Depends(require_user)) -> Response:
    """Скачать ВСЕ беседы пользователя одним JSON (переносимость данных из сервиса)."""
    with get_session() as db:
        conversations = [
            {"session_id": s["session_id"], "title": s["title"],
             "messages": _serialize_messages(q.get_session_messages(db, user.id, s["session_id"]))}
            for s in q.get_user_sessions(db, user.id)
        ]
    return _json_download(
        {"user": user.username, "exported_conversations": len(conversations), "conversations": conversations},
        "navigator719-chats.json",
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
