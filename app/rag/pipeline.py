"""RAG-пайплайн навигатора: запрос → гибрид-поиск → контекст → DeepSeek → ответ.

Ответ всегда снабжён обязательной пометкой (AI — черновик, вердикт за экспертом ТПП).
Запуск как смоук (нужен поднятый Qdrant с коллекцией и DEEPSEEK_API_KEY в .env):
  .venv/Scripts/python.exe -m app.rag.pipeline "производим прицепы для легковых авто" 29.20.23
"""

from __future__ import annotations

import re
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

MAX_OPS_PER_HIT = 25  # ограничиваем контекст: у некоторых продуктов сотни операций.
# 15→25 (INTERIM-фикс P2): у 264 мега-продуктов (20% базы) усечение теряло балльные требования
# (при 15 ВСЕ баллы влезали лишь у 13/242; при 25 — у 167/242). Выше не поднимаем: длинный
# перечень операций провоцирует авто-выдумку итога (см. eval_faithfulness_stress). Остаток
# усечения помечается «⚠ СПИСОК ОПЕРАЦИЙ НЕПОЛНЫЙ» (см. format_context + правило 2а промпта);
# экстремальный хвост (676/220 опер) лечится parent/child auto-merge — отложено в 2.0.
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
    unverified_numbers: list[str] = field(default_factory=list)  # числа баллов/% в ответе, не найденные в контексте
    prompt_tokens: int = 0  # токены DeepSeek за ответ (учёт затрат в админ-логах)
    completion_tokens: int = 0


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
            head += "  СОВПАДЕНИЕ ПО КОДУ ОКПД2 (наиболее вероятная позиция)"
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
                rel = " (показаны наиболее релевантные запросу)" if query else ""
                lines.append(
                    f"      СПИСОК ОПЕРАЦИЙ НЕПОЛНЫЙ: показаны {len(shown)} из {total} операций"
                    f"{rel}; полный перечень требований и баллов — в первоисточнике ПП №719 (этот раздел)."
                )
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


# --- Faithfulness-постпроверка (P0 анти-галлюцинаций) ------------------------------
# Число-претензия в ОТВЕТЕ — величина рядом с единицей «балл» или «процент/%». Именно такие
# числа модель ОБЯЗАНА брать из контекста (правило 2 промпта); даты/сроки («5 лет», «2018 г.»)
# другой единицы и сюда не попадают. Те же функции использует scripts/eval_answers.py —
# рантайм и замер меряют ОДНО И ТО ЖЕ.
_NUM = r"\d+(?:[.,]\d+)?"
_BALL_CLAIM_RE = re.compile(rf"({_NUM})\s*балл", re.IGNORECASE)
_PCT_CLAIM_RE = re.compile(rf"({_NUM})\s*(?:процент|%)", re.IGNORECASE)


def claim_numbers(text: str) -> list[str]:
    """Числа баллов/процентов, заявленные в ответе (нормализованы: запятая→точка)."""
    return [n.replace(",", ".") for n in _BALL_CLAIM_RE.findall(text) + _PCT_CLAIM_RE.findall(text)]


def number_in_context(num: str, context: str) -> bool:
    """True, если числовой токен есть в контексте (граница — не-цифра; «,»≡«.»)."""
    return any(
        re.search(rf"(?<!\d){re.escape(v)}(?!\d)", context)
        for v in {num, num.replace(".", ",")}
    )


def unverified_numbers(text: str, context: str) -> list[str]:
    """Числа баллов/% из ОТВЕТА, которых НЕТ в контексте (кандидаты в галлюцинации).

    Консервативно: число незаземлено, только если в контексте его НЕТ вовсе (это занижает,
    а не завышает — ложноположительных нет). Уникальные значения в порядке появления."""
    seen: set[str] = set()
    out: list[str] = []
    for n in claim_numbers(text):
        if n not in seen and not number_in_context(n, context):
            seen.add(n)
            out.append(n)
    return out


# Рантайм-очистка эмодзи/символов-значков из ответа модели (внутренний продукт — без эмодзи).
# Промпт просит их не использовать, но модель изредка добавляет (напр. ⚠); подчищаем гарантированно.
# Диапазоны: emoji, misc symbols/dingbats (⚠ ✓ ✅), доп. символы/стрелки, variation selector.
# Типографику (• « » — … № ™ ®) НЕ трогаем — она вне этих диапазонов.
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def answer(query: str, okpd2: str | None = None, limit: int = 5) -> Answer:
    # Базовые (meta) реплики (приветствие / что умеешь / как работать) — заготовки без LLM.
    from app.rag import meta
    if meta.is_meta(query):
        return Answer(text=meta.response(query), hits=[])

    # Процедурный дефер-предохранитель: чистый процедурный вопрос (внесение в реестр, ГИСП,
    # подача заявления, сроки, обжалование) корпусом НЕ покрыт. Деферим детерминированно ДО
    # поиска — без вызова LLM (ноль галлюцинаций/стоимости). Товарные/смешанные вопросы (есть
    # код или товарно-балльный сигнал) сюда не попадают — их обрабатывает обычный пайплайн.
    if settings.PROCEDURAL_DEFLECT_ENABLED:
        from app.rag import procedural
        if procedural.is_procedural(query, has_code=bool(okpd2)):
            return Answer(text=procedural.DEFLECTION, hits=[])

    hits = search(query, okpd2=okpd2, limit=limit)
    # Реранкер (стадия 2): переупорядочивает top-k через DeepSeek, но ТОЛЬКО при отсутствии
    # совпадения по коду ОКПД2 (код авторитетнее). Поднял recall@1 0.95→0.98 без регресса.
    if settings.RERANK_ENABLED and hits and not any(h.okpd2_match for h in hits):
        from app.rag.reranker import rerank
        hits = rerank(query, hits)
    cases = search_cases(query, limit=MAX_CASES)  # подтверждённые экспертом — высший приоритет
    if not hits and not cases:
        return Answer(
            text="Подходящая позиция в приложении к ПП №719 не найдена. Уточните "
            "наименование продукции или укажите код ОКПД2.",
            hits=[],
        )

    # Out-of-scope guard: совпадение по коду ОКПД2 или подтверждённый кейс = высокая
    # уверенность, флаг не поднимаем. Иначе смотрим dense top-1 (один лёгкий запрос).
    low_rel = (
        not cases
        and not any(h.okpd2_match for h in hits)
        and dense_top1(query) < RELEVANCE_SOFT
    )

    ctx = format_context(hits, query)
    cases_ctx = format_cases(cases) if cases else None
    user = build_navigator_user_prompt(
        query, ctx, okpd2, cases=cases_ctx, low_relevance=low_rel,
    )
    resp = _client().chat.completions.create(
        model=settings.DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": NAVIGATOR_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0,  # детерминизм (INTERIM-фикс P2): при 0.1 модель на мега-продуктах
        # перечисляла РАЗНЫЕ подмножества операций → числа баллов «плавали» (eval_determinism
        # 0.60). 0 стабилизирует набор чисел; реранкер и так temp=0.
    )
    usage = resp.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0

    # Faithfulness-постпроверка: числа баллов/% из ответа сверяем с контекстом. Незаземлённые
    # НЕ удаляем и НЕ пишем дисклеймер в ответ (внутренний продукт) — но фиксируем в
    # Answer.unverified_numbers: эксперт-admin видит флаг в логах диалогов.
    raw = _strip_emoji(resp.choices[0].message.content or "")
    grounding = ctx + ("\n" + cases_ctx if cases_ctx else "")
    ungrounded = unverified_numbers(raw, grounding)
    return Answer(
        text=raw,
        hits=hits,
        cases=cases,
        low_relevance=low_rel,
        unverified_numbers=ungrounded,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
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
