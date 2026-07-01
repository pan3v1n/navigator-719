"""Чат-эндпоинт веб-UI 1.0: вопрос эксперта → движок навигатора → лог диалога в БД → ответ.

Эндпоинт СИНХРОННЫЙ (`def`) — FastAPI исполняет его в threadpool (как `/navigate`); движок
(`pipeline.answer`: e5 + Qdrant + DeepSeek) блокирующий, event loop не держим. Диалог группируется
по `session_id` (одна беседа = один id). Реплики (вопрос + ответ + флаги качества) логируются в БД —
их видит admin. History-aware follow-up («а какой порог?») — отложенный fast-follow: сейчас каждый
вопрос самостоятелен.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
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
            )
    except Exception:  # noqa: BLE001 — лог не должен ронять ответ эксперту
        logger.exception("chat: не удалось записать лог диалога (user_id=%s)", user.id)

    return ChatResponse(
        answer=ans.text, sources=sources, low_relevance=ans.low_relevance,
        unverified_numbers=ans.unverified_numbers, session_id=session_id,
    )
