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

# Код ОКПД2 в свободном тексте: 29.20.23, 28.41, 21.20.21.110 …
OKPD2_IN_TEXT = re.compile(r"\b\d{2}\.\d{2}(?:\.\d+)*\b")

# Базовый перечень документов для подтверждения производства по 719 (экспертиза ТПП).
BASE_CHECKLIST = [
    "Заявление на проведение экспертизы (по форме ТПП).",
    "Учредительные документы предприятия-изготовителя.",
    "Конструкторская и техническая документация на продукцию.",
    "Технологические карты / описание техпроцесса по каждой заявленной операции, "
    "выполняемой на территории РФ.",
    "Договоры и первичные документы с поставщиками материалов и комплектующих "
    "(с подтверждением страны происхождения, сертификаты СТ-1 при наличии).",
    "Перечень и документы на производственное оборудование.",
]


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
    items = list(BASE_CHECKLIST)
    if hit is None:
        return items
    rtype = hit.payload.get("requirement_type")
    if rtype in ("points", "mixed"):
        items.append(
            "Расчёт набранных баллов по операциям (с привязкой к подтверждающим документам)."
        )
    if hit.min_threshold:
        items.append(f"Подтверждение достижения порога: {hit.min_threshold}.")
    # Права на КД/ТД упоминаются в требованиях ряда позиций
    joined = " ".join(
        (o.get("text") or "")
        for b in hit.requirement_blocks
        for o in (b.get("operations") or [])
    )
    if "конструкторск" in joined.lower() or "документац" in joined.lower():
        items.append("Документы, подтверждающие права на конструкторскую и техническую документацию.")
    return items


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
