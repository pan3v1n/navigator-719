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


def context_of(query: str, okpd2: str | None) -> str | None:
    """Контекст, который увидела бы модель. None — вопрос до контекста не дошёл (meta/дефер)."""
    planned = pipe._plan_answer(query, okpd2=okpd2 or None)
    if isinstance(planned, pipe.Answer):
        return None
    return getattr(planned, "grounding", "") or ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Размер контекста навигатора (K6), без DeepSeek")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--cases", type=int, default=0, help="только первые N кейсов")
    args = ap.parse_args()

    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    cases = [c for c in cases if c.get("in_scope")]
    if args.cases:
        cases = cases[: args.cases]

    sizes: list[int] = []
    tails: list[int] = []
    skipped = 0
    for c in cases:
        ctx = context_of(c["query"], c.get("okpd2"))
        if ctx is None:
            skipped += 1
            continue
        sizes.append(len(ctx))
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
        f"РАЗМЕР КОНТЕКСТА — {len(sizes)} in-scope кейсов"
        + (f" (пропущено {skipped}: до контекста не дошли)" if skipped else ""),
        "=" * 78,
        f"  средняя длина  = {mean:.0f} символов",
        f"  МЕДИАНА        = {median:.0f} символов   ← среднее уводят мега-продукты",
        f"  минимум / максимум = {min(sizes)} / {max(sizes)}",
        f"  доля ХВОСТА нецелевых кандидатов = {tail_share:.2f}"
        f"   (блок есть у {with_tail} из {len(sizes)} кейсов)",
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
