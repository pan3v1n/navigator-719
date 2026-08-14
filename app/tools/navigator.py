"""Навигатор по ПП №719: текст/код продукции → применимая позиция + требования + чек-лист.

Тонкая обёртка над RAG-пайплайном: вытаскивает код ОКПД2 из текста запроса (если не
передан явно), вызывает pipeline.answer и формирует базовый чек-лист документов по
найденной позиции. Используется из API (POST /navigate) и Streamlit-MVP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.prompts import EXPERT_DISCLAIMER
from app.rag.pipeline import answer
from app.rag.retriever import Hit
from app.tools import checklist

# Код ОКПД2 в свободном тексте: 29.20.23, 28.41, 21.20.21.110 …
OKPD2_IN_TEXT = re.compile(r"\b\d{2}\.\d{2}(?:\.\d+)*\b")

# Перечень документов больше НЕ зашит в код (R25): он строится из раздела 4 Приказа ТПП РФ №52
# со ссылками на номера пунктов — см. app/tools/checklist.py. Прежний хардкод из шести пунктов
# был правдоподобным, но выдуманным: ровно то, за что проект ругает языковую модель.


@dataclass
class Navigation:
    query: str
    okpd2_used: str | None
    answer: str
    sources: list[Hit] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    disclaimer: str = EXPERT_DISCLAIMER


def extract_okpd2(text: str) -> str | None:
    m = OKPD2_IN_TEXT.search(text or "")
    return m.group(0) if m else None


def build_checklist(hit: Hit | None) -> list[str]:
    """Перечень документов по найденной позиции — из Приказа ТПП РФ №52 (R25).

    Базовые пункты 4.2.x нужны всегда; условные 4.3.x подбираются по ТЕКСТУ требований позиции
    (права на КД/ТД, сервисный центр, техоперации, процентная доля). У каждого пункта в выводе
    стоит его номер — эксперт сверится с первоисточником, а не поверит на слово."""
    if hit is None:
        return checklist.checklist_for()
    # Текст требований позиции: и операции, и «component»-блоки (там лежат обязательные условия —
    # см. pipeline._hit_operations), иначе условные пункты 4.3.x подбирались бы вслепую.
    parts: list[str] = []
    for b in hit.requirement_blocks or []:
        ops = b.get("operations") or []
        parts.extend((o.get("text") or "") for o in ops)
        if not ops:
            parts.append(b.get("component") or "")
    joined = " ".join(p for p in parts if p)
    rtype = hit.payload.get("requirement_type")
    has_points = rtype in ("points", "mixed") or any(
        o.get("points") is not None for b in (hit.requirement_blocks or [])
        for o in (b.get("operations") or [])
    )
    return checklist.checklist_for(joined, has_points=has_points, threshold=hit.min_threshold)


def navigate(query: str, okpd2: str | None = None, limit: int = 5) -> Navigation:
    code = okpd2 or extract_okpd2(query)
    ans = answer(query, okpd2=code, limit=limit)
    checklist = build_checklist(ans.hits[0]) if ans.hits else []
    return Navigation(
        query=query,
        okpd2_used=code,
        answer=ans.text,
        sources=ans.hits,
        checklist=checklist,
    )
