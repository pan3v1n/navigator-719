"""Pydantic-схемы запросов/ответов API навигатора ПП №719."""

from __future__ import annotations

from pydantic import BaseModel, Field


class NavigateRequest(BaseModel):
    query: str = Field(..., min_length=2, description="Описание продукции или вопрос")
    okpd2: str | None = Field(None, description="Код ОКПД2 (если известен)")
    limit: int = Field(5, ge=1, le=20, description="Сколько позиций вернуть")


class SourceItem(BaseModel):
    section_roman: str
    product_name: str
    okpd2_codes: list[str]
    min_threshold: str | None = None
    source_anchor: str | None = None
    okpd2_match: bool = False
    score: float


class NavigateResponse(BaseModel):
    query: str
    okpd2_used: str | None = None
    answer: str
    sources: list[SourceItem]
    checklist: list[str]
    disclaimer: str
