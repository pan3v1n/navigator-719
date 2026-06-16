"""API-роутеры навигатора ПП №719."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import NavigateRequest, NavigateResponse, SourceItem
from app.tools.navigator import navigate

router = APIRouter()


@router.post("/navigate", response_model=NavigateResponse)
def navigate_endpoint(req: NavigateRequest) -> NavigateResponse:
    """Принимает описание/код продукции → применимая позиция 719 + требования + чек-лист.

    Синхронный обработчик: FastAPI выполнит его в пуле потоков, чтобы блокирующий вызов
    DeepSeek и локального эмбеддера не держал event loop.
    """
    try:
        nav = navigate(req.query, okpd2=req.okpd2, limit=req.limit)
    except Exception as e:  # noqa: BLE001 — наружу отдаём аккуратную 500
        raise HTTPException(status_code=500, detail=f"Ошибка навигации: {e}") from e

    return NavigateResponse(
        query=nav.query,
        okpd2_used=nav.okpd2_used,
        answer=nav.answer,
        sources=[
            SourceItem(
                section_roman=h.section_roman,
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
