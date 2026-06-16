"""RAG-пайплайн навигатора: запрос → гибрид-поиск → контекст → DeepSeek → ответ.

Ответ всегда снабжён обязательной пометкой (AI — черновик, вердикт за экспертом ТПП).
Запуск как смоук (нужен поднятый Qdrant с коллекцией и DEEPSEEK_API_KEY в .env):
  .venv/Scripts/python.exe -m app.rag.pipeline "производим прицепы для легковых авто" 29.20.23
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from app.core.config import settings
from app.core.prompts import (
    EXPERT_DISCLAIMER,
    NAVIGATOR_SYSTEM_PROMPT,
    build_navigator_user_prompt,
)
from app.rag.retriever import Hit, search

MAX_OPS_PER_HIT = 12  # ограничиваем контекст: у некоторых продуктов сотни операций


@dataclass
class Answer:
    text: str
    hits: list[Hit]


def format_context(hits: list[Hit]) -> str:
    blocks: list[str] = []
    for i, h in enumerate(hits, 1):
        lines = [f"[{i}] Раздел {h.section_roman} — {h.product_name}"]
        if h.okpd2_codes:
            lines.append(f"    ОКПД2: {', '.join(h.okpd2_codes)}")
        if h.min_threshold:
            lines.append(f"    Порог: {h.min_threshold}")
        ops: list[str] = []
        for b in h.requirement_blocks:
            for o in b.get("operations") or []:
                pts = o.get("points")
                ptxt = f" — {pts} балл." if pts is not None else " — балл зависит от условий/категории"
                ops.append(f"      • {o.get('text', '')}{ptxt}")
        if ops:
            lines.append("    Ключевые операции:")
            lines.extend(ops[:MAX_OPS_PER_HIT])
            if len(ops) > MAX_OPS_PER_HIT:
                lines.append(f"      … ещё {len(ops) - MAX_OPS_PER_HIT} операций")
        if h.source_anchor:
            lines.append(f"    Источник: {h.source_anchor}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _client():
    from openai import OpenAI

    if not settings.DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY пуст — заполни .env")
    return OpenAI(api_key=settings.DEEPSEEK_API_KEY, base_url=settings.DEEPSEEK_BASE_URL)


def _ensure_disclaimer(text: str) -> str:
    return text if EXPERT_DISCLAIMER in text else text.rstrip() + "\n\n" + EXPERT_DISCLAIMER


def answer(query: str, okpd2: str | None = None, limit: int = 5) -> Answer:
    hits = search(query, okpd2=okpd2, limit=limit)
    if not hits:
        return Answer(
            text="Подходящая позиция в приложении к ПП №719 не найдена. Уточните "
            "наименование продукции или укажите код ОКПД2.\n\n" + EXPERT_DISCLAIMER,
            hits=[],
        )

    user = build_navigator_user_prompt(query, format_context(hits), okpd2)
    resp = _client().chat.completions.create(
        model=settings.DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": NAVIGATOR_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
    )
    return Answer(text=_ensure_disclaimer(resp.choices[0].message.content), hits=hits)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit('Использование: python -m app.rag.pipeline "вопрос" [код ОКПД2]')
    query = sys.argv[1]
    okpd2 = sys.argv[2] if len(sys.argv) > 2 else None
    ans = answer(query, okpd2)
    print("=" * 70)
    print(f"ВОПРОС: {query}" + (f"  [ОКПД2 {okpd2}]" if okpd2 else ""))
    print("=" * 70)
    print("\nНАЙДЕНО:")
    for i, h in enumerate(ans.hits, 1):
        mark = "✓код" if h.okpd2_match else "    "
        print(f"  {i}. [{mark}][{h.section_roman}] {h.product_name[:60]} (score={h.score:.3f})")
    print("\nОТВЕТ:\n")
    print(ans.text)


if __name__ == "__main__":
    main()
