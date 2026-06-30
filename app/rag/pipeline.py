"""RAG-пайплайн навигатора: запрос → гибрид-поиск → контекст → DeepSeek → ответ.

Ответ всегда снабжён обязательной пометкой (AI — черновик, вердикт за экспертом ТПП).
Запуск как смоук (нужен поднятый Qdrant с коллекцией и DEEPSEEK_API_KEY в .env):
  .venv/Scripts/python.exe -m app.rag.pipeline "производим прицепы для легковых авто" 29.20.23
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.prompts import (
    EXPERT_DISCLAIMER,
    NAVIGATOR_SYSTEM_PROMPT,
    build_navigator_user_prompt,
)
from app.rag import sparse
from app.rag.retriever import Hit, dense_top1, search, search_cases

MAX_OPS_PER_HIT = 15  # ограничиваем контекст: у некоторых продуктов сотни операций
MAX_CASES = 3  # сколько подтверждённых кейсов подмешивать в контекст
# Out-of-scope guard: порог dense top-1 cosine. Ниже — подозрение, что продукция вне 719.
# Калибровка на golden set (docs/eval_report.md): out-of-scope ≤ 0.822, in-scope ≥ 0.808 —
# полоса перекрытия узкая, поэтому порог НЕ режет жёстко, а лишь поднимает флаг для модели
# (финальное решение «вне сферы» принимает LLM по смыслу контекста, см. правило 1б промпта).
RELEVANCE_SOFT = 0.83


@dataclass
class Answer:
    text: str
    hits: list[Hit]
    cases: list[dict] = field(default_factory=list)
    low_relevance: bool = False  # сработал ли сигнал out-of-scope guard


def _hit_operations(h: Hit) -> list[dict]:
    """Плоский список операций хита (из всех requirement_blocks)."""
    ops: list[dict] = []
    for b in h.requirement_blocks:
        ops.extend(b.get("operations") or [])
    return ops


def _rank_operations(ops: list[dict], query: str | None) -> list[dict]:
    """Переставляет операции так, чтобы релевантные запросу шли первыми.

    Нужно для мега-продуктов (сотни операций): усечение до MAX_OPS_PER_HIT иначе режет
    нужное, оставляя первые попавшиеся. Скоринг — пересечение стем-токенов операции и
    запроса (локальный токенизатор BM25, без сети/модели). Сортировка стабильна: при
    равной релевантности исходный порядок сохраняется. Без запроса/совпадений — без изменений."""
    qtok = set(sparse.tokenize(query)) if query else set()
    if not qtok:
        return ops
    return sorted(ops, key=lambda o: -len(qtok & set(sparse.tokenize(o.get("text", "")))))


def format_context(hits: list[Hit], query: str | None = None) -> str:
    blocks: list[str] = []
    for i, h in enumerate(hits, 1):
        sect = f"«{h.section_title}»" if h.section_title else f"Раздел {h.section_roman}"
        head = f"[{i}] {h.product_name} (раздел {sect})"
        if h.okpd2_match:
            head += "  ✓ СОВПАДЕНИЕ ПО КОДУ ОКПД2 (наиболее вероятная позиция)"
        lines = [head]
        if h.okpd2_codes:
            lines.append(f"    ОКПД2: {', '.join(h.okpd2_codes)}")
        if h.min_threshold:
            lines.append(f"    Порог: {h.min_threshold}")
        ops = _hit_operations(h)
        total = len(ops)
        if total > MAX_OPS_PER_HIT:
            ops = _rank_operations(ops, query)
        shown = ops[:MAX_OPS_PER_HIT]
        if shown:
            lines.append("    Ключевые операции:")
            for o in shown:
                pts = o.get("points")
                ptxt = f" — {pts} балл." if pts is not None else " — балл зависит от условий/категории"
                lines.append(f"      • {o.get('text', '')}{ptxt}")
            if total > MAX_OPS_PER_HIT:
                tail = f"      … ещё {total - MAX_OPS_PER_HIT} операций"
                if query:
                    tail += " (показаны наиболее релевантные запросу)"
                lines.append(tail)
        if h.source_anchor:
            lines.append(f"    Источник: {h.source_anchor}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_cases(cases: list[dict]) -> str:
    blocks: list[str] = []
    for i, c in enumerate(cases, 1):
        lines = [f"[Кейс {i}] {c.get('product_name', '')} (ОКПД2 {c.get('okpd2', '—')})"]
        lines.append(f"    Ситуация: {c.get('query', '')}")
        lines.append(f"    Ответ эксперта: {c.get('expert_answer', '')}")
        if c.get("source"):
            lines.append(f"    Источник: {c['source']}")
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
    cases = search_cases(query, limit=MAX_CASES)  # подтверждённые экспертом — высший приоритет
    if not hits and not cases:
        return Answer(
            text="Подходящая позиция в приложении к ПП №719 не найдена. Уточните "
            "наименование продукции или укажите код ОКПД2.\n\n" + EXPERT_DISCLAIMER,
            hits=[],
        )

    # Out-of-scope guard: совпадение по коду ОКПД2 или подтверждённый кейс = высокая
    # уверенность, флаг не поднимаем. Иначе смотрим dense top-1 (один лёгкий запрос).
    low_rel = (
        not cases
        and not any(h.okpd2_match for h in hits)
        and dense_top1(query) < RELEVANCE_SOFT
    )

    user = build_navigator_user_prompt(
        query,
        format_context(hits, query),
        okpd2,
        cases=format_cases(cases) if cases else None,
        low_relevance=low_rel,
    )
    resp = _client().chat.completions.create(
        model=settings.DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": NAVIGATOR_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
    )
    return Answer(
        text=_ensure_disclaimer(resp.choices[0].message.content),
        hits=hits,
        cases=cases,
        low_relevance=low_rel,
    )


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
