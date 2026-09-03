"""Pydantic-схемы запросов/ответов API навигатора ПП №719."""

from __future__ import annotations

from pydantic import BaseModel, Field


class NavigateRequest(BaseModel):
    # ⚠⚠ ВЕРХНИЙ ПРЕДЕЛ ОБЯЗАТЕЛЕН (находка 6 восьмого раунда ревью PR #137). Поле объявляло
    # только `min_length`, а `POST /navigate` доходит до `procedural.is_procedural()` через
    # `navigator.navigate → pipeline.answer`. Разбор маршрута сплайсит словарь в регулярку, и на
    # длинном входе она квадратична: 28.8 КБ — 1.5 с, 99 КБ — 4.8 с чистого CPU ДО всякого поиска
    # и вызова модели. Сама квадратичность чинится отдельно, но предел нужен и после этого:
    # ⚠ соседний путь чата (`chat.py:87`) ограничен 2000 знаков с самого начала — расхождение
    # держалось только потому, что `/navigate` никто не мерил на длинном входе.
    query: str = Field(..., min_length=2, max_length=2000,
                       description="Описание продукции или вопрос")
    okpd2: str | None = Field(None, description="Код ОКПД2 (если известен)")
    limit: int = Field(5, ge=1, le=20, description="Сколько позиций вернуть")


class SourceItem(BaseModel):
    section_roman: str
    section_title: str = ""
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
