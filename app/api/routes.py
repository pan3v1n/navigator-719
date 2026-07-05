"""API-роутеры навигатора ПП №719."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_user
from app.api.schemas import NavigateRequest, NavigateResponse, SourceItem
from app.db.models import User
from app.tools.navigator import navigate

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/navigate", response_model=NavigateResponse)
def navigate_endpoint(req: NavigateRequest, user: User = Depends(require_user)) -> NavigateResponse:
    """Принимает описание/код продукции → применимая позиция 719 + требования + чек-лист.

    Синхронный обработчик: FastAPI выполнит его в пуле потоков, чтобы блокирующий вызов
    DeepSeek и локального эмбеддера не держал event loop.
    """
    try:
        nav = navigate(req.query, okpd2=req.okpd2, limit=req.limit)
    except Exception as e:  # noqa: BLE001 — детали логируем, наружу только generic 500
        logger.exception("Ошибка навигации (query=%r, okpd2=%r)", req.query, req.okpd2)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при обработке запроса. Повторите попытку позже.",
        ) from e

    return NavigateResponse(
        query=nav.query,
        okpd2_used=nav.okpd2_used,
        answer=nav.answer,
        sources=[
            SourceItem(
                section_roman=h.section_roman,
                section_title=h.section_title,
                product_name=h.product_name,
                okpd2_codes=h.okpd2_codes,
                min_threshold=h.min_threshold,
                source_anchor=h.source_anchor,
                okpd2_match=h.okpd2_match,
                score=h.score,
            )
            for h in nav.sources
        ],
        checklist=nav.checklist,
        disclaimer=nav.disclaimer,
    )
