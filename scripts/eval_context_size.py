"""Размер контекста, который сервис отдаёт модели (`K6` #43) — БЕЗ вызова DeepSeek.

⚠⚠ ЗАЧЕМ ЗАВЕДЁН. Числа `context_size` попали в базу сравнения (`docs/eval_baseline/baseline.json`)
из разовой сессии `K6` — «6 890 → 5 034, доля хвоста 0.50 → 0.31». Пересчитать их было НЕЧЕМ: ни
один скрипт репозитория контекст не мерил. Ровно тот класс, на котором проект уже горел с `K2`
(«24.6 % → 18.4 %» пришлось отзывать — доля жила только в сообщении коммита). Число, которое
нечем пересчитать, — не критерий, а цитата.

ЧТО МЕРИМ. Для каждого вопроса golden set собираем ровно тот контекст, который построил бы
рантайм (`_plan_answer` — общий путь `answer()` и `answer_stream()`, до вызова модели), и считаем:
  * длину контекста в символах — среднюю и МЕДИАННУЮ (среднее уводят мега-продукты);
  * долю ХВОСТА нецелевых кандидатов: `EV7` оставила им только имя, код, якорь и строку-запрет,
    и `K6` свернула повтор запрета в один блок. Именно эта доля показывает, не отъедает ли
    служебная часть контекст у содержательной.

⚠ БЕЗ LLM И БЕЗ ДЕНЕГ, но Qdrant нужен: контекст строится из реальной выдачи.

ЗАПУСК:
    EXACT_SEARCH=1 QDRANT_CASES_COLLECTION=__eval_disabled__ \\
        .venv/Scripts/python.exe scripts/eval_context_size.py
    ... --report docs/eval_runs/<дата>_context.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402  (только после sys.path)

enable_utf8()

from app.rag import pipeline as pipe  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_golden.json"

# Маркер блока нецелевых кандидатов. `K6` свернула их в ОДИН блок с ОДНИМ запретом — по этой
# строке он и опознаётся. Если строка в промпте изменится, счётчик хвоста обнулится, и это будет
# ВИДНО (доля 0.00 при непустом контексте), а не тихо соврёт.
TAIL_MARK = "ПРОЧИЕ КАНДИДАТЫ ОКНА"


def context_of(query: str, okpd2: str | None) -> tuple[str, str] | None:
    """`(заземление, весь промпт)` — то, что увидела бы модель. None — до контекста не дошло.

    ⚠⚠ ДВА ЧИСЛА, А НЕ ОДНО, И РАЗНИЦА МЕЖДУ НИМИ СОДЕРЖАТЕЛЬНАЯ. Скрипт с самого начала мерил
    `grounding` — стог, по которому сверяются числа ответа. Но это ПОДМНОЖЕСТВО того, что получает
    модель: блок документов (`K15`) в заземление осознанно не входит (см. комментарий в
    `_plan_answer` — иначе номера пунктов Приказа отмывали бы выдуманные баллы в «заземлённые»).
    Поэтому правку `dbfc622`, ужавшую справочник 1155 → 727 символов, этим скриптом нельзя было
    увидеть В ПРИНЦИПЕ: она меняет промпт, а не стог. Я записал в базу предсказание, что число
    просядет, — прогон дал те же 4934/3852, и причина была не в правке.

    `grounding` остаётся первым числом: с ним сравнивается база. Промпт — второе, новое.

    ⚠ Процедурная ветка раньше отдавала None («до контекста не дошёл») — и это молча выкидывало из
    замера вопросы про состав документов, кластер жалоб №1 (`EV21` #119). Теперь она собирается
    тем же кодом, что и рантайм (`plan_procedural`), без вызова модели и без денег."""
    # ⚠⚠ ПРОЦЕДУРНАЯ ВЕТКА ПРОВЕРЯЕТСЯ ДО `_plan_answer`, И ЭТО НЕ СТИЛЬ, А ЦЕНА ПРОГОНА.
    # Первая редакция звала `_plan_answer` первым, а он для процедурного вопроса уходит в
    # `_answer_procedural` и ГЕНЕРИРУЕТ ОТВЕТ DeepSeek — только потом возвращал `Answer`, и лишь
    # после этого скрипт пересобирал контекст сам. То есть «замер без вызова модели и без денег»
    # платил за две генерации на прогон и без ключа падал вместо того, чтобы мерить. Найдено
    # ревью пакета 26.08.2026; до `EV21` процедурных кейсов в наборе не было, и путь не исполнялся.
    # ⚠ Условия повторяют рантайм ЦЕЛИКОМ, включая обе настройки: при выключенном
    # `PROCEDURAL_ANSWER_FROM_RULES` сервис отвечает дефером, и мерить контекст было бы нечего.
    from app.core.config import settings
    from app.rag import procedural

    if (settings.PROCEDURAL_DEFLECT_ENABLED and settings.PROCEDURAL_ANSWER_FROM_RULES
            and procedural.is_procedural(query, has_code=bool(okpd2))):
        proc = pipe.plan_procedural(query, query)
        if proc is None:
            return None
        _topic, _rules, ctx, user = proc
        return ctx, user

    planned = pipe._plan_answer(query, okpd2=okpd2 or None)
    if isinstance(planned, pipe.Answer):
        return None    # meta / перевод кода / дефер — контекста нет, мерить нечего
    return (getattr(planned, "grounding", "") or "",
            planned.messages[-1]["content"] if planned.messages else "")


def main() -> int:
    ap = argparse.ArgumentParser(description="Размер контекста навигатора (K6), без DeepSeek")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--cases", type=int, default=0, help="только первые N кейсов")
    # ⚠⚠ ФИЛЬТР ПО ТИПУ — НЕ УДОБСТВО, А ТРЕБОВАНИЕ БАЗЫ СРАВНЕНИЯ.
    # Числа базы (`4934 / 3852`) сняты на 42 ТОВАРНЫХ кейсах. `EV21` добавила документные, и без
    # этого флага прежнее число стало бы нечем пересчитать — ровно то, на чём проект уже горел
    # дважды («24.6 % → 18.4 %» у `K2` и сам `context_size`, лежавший в базе без скрипта).
    # Правило 6 базы: у каждого числа обязана быть КОМАНДА, которой оно считается.
    ap.add_argument("--kind", choices=("all", "product", "documents"), default="all",
                    help="какие кейсы мерить: все · только товарные (популяция базы) · документные")
    args = ap.parse_args()

    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    cases = [c for c in cases if c.get("in_scope")]
    if args.kind != "all":
        cases = [c for c in cases if c.get("kind", "product") == args.kind]
    if args.cases:
        cases = cases[: args.cases]

    sizes: list[int] = []
    tails: list[int] = []
    prompts: list[int] = []
    doc_prompts: list[int] = []
    skipped = 0
    for c in cases:
        got = context_of(c["query"], c.get("okpd2"))
        if got is None:
            skipped += 1
            continue
        ctx, prompt = got
        sizes.append(len(ctx))
        prompts.append(len(prompt))
        if c.get("kind") == "documents":
            doc_prompts.append(len(prompt))
        i = ctx.find(TAIL_MARK)
        tails.append(0 if i == -1 else len(ctx) - i)

    if not sizes:
        print("ни один кейс не дошёл до контекста — проверь Qdrant")
        return 2

    mean = statistics.mean(sizes)
    median = statistics.median(sizes)
    tail_share = sum(tails) / sum(sizes)
    with_tail = sum(1 for t in tails if t)

    L = [
        "=" * 78,
        f"РАЗМЕР КОНТЕКСТА — {len(sizes)} in-scope кейсов, срез --kind {args.kind}"
        + (f" (пропущено {skipped}: до контекста не дошли)" if skipped else ""),
        "=" * 78,
        f"  средняя длина  = {mean:.0f} символов",
        f"  МЕДИАНА        = {median:.0f} символов   ← среднее уводят мега-продукты",
        f"  минимум / максимум = {min(sizes)} / {max(sizes)}",
        f"  доля ХВОСТА нецелевых кандидатов = {tail_share:.2f}"
        f"   (блок есть у {with_tail} из {len(sizes)} кейсов)",
        "",
        "ВЕСЬ ПРОМПТ (то, что реально получает модель; заземление — его подмножество):",
        f"  средняя длина  = {statistics.mean(prompts):.0f} символов",
        f"  МЕДИАНА        = {statistics.median(prompts):.0f} символов",
    ] + ([
        f"  документные кейсы (EV21): {len(doc_prompts)} шт., медиана "
        f"{statistics.median(doc_prompts):.0f}   ← здесь и живёт блок справочника K15,"
        " которого в заземлении нет",
    ] if doc_prompts else [
        "  ⚠ документных кейсов в наборе нет — блок справочника K15 не меряется ничем (EV21 #119)",
    ]) + [
        "",
        "⚠ Доля 0.00 при непустом контексте означает НЕ «хвоста нет», а что маркер"
        f" {TAIL_MARK!r} больше не совпадает с промптом — проверить, а не радоваться.",
    ]
    out = "\n".join(L)
    print(out)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("# Размер контекста (K6)\n\n```\n" + out + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
