r"""Влияние петли кейсов эксперта (`verified_cases`) на замер — `EV15` (#99).

ЗАЧЕМ. `EVAL_GUIDE §1.1` требует выключать коллекцию `verified_cases` на прогоне: иначе система
отвечает по собственным подсказкам, и мы меряем не продукт, а кейсы. Предусловие не исполнялось
НИ РАЗУ (прогоны 17.08, 18.08, 20.08 — все с включёнными 12 кейсами). Прежде чем выбирать между
«начать исполнять и перезамерить базу» и «убрать предусловие из гайда», надо знать ЦЕНУ решения:
на скольких вопросах наборов петля вообще срабатывает. Если ни на одном — обе конфигурации дают
один и тот же контекст, предусловие исполняется бесплатно, а история замеров остаётся
сопоставимой; перезамер базы не нужен.

ЧТО МЕРИМ. Ровно тот вызов, который делает рантайм (`pipeline.answer`, строка с `search_cases`):

    cases = search_cases(search_query, limit=MAX_CASES, qvec=qvec)

Ни строкой логики не воспроизводим — зовём ту же функцию с теми же аргументами. В наборах один
ход диалога, поэтому `search_query == query`, а `qvec` — тот же вектор запроса.

⚠ Срабатывание петли влияет на ответ ДВАЖДЫ: кейс уходит в контекст с ВЫСШИМ приоритетом
(правило 1а промпта) И гасит гард out-of-scope (`confident = bool(cases) or ...`). Поэтому для
негативных наборов срабатывание опаснее, чем для товарных: там оно снимает отказ.

НАБОРЫ. Берём те, что доходят до пути ОТВЕТА, — только там есть петля кейсов:
`eval_golden` (уровень 2), `eval_golden_negative` и `eval_golden_borderline` (гейт `eval_guard`),
запросы `eval_determinism` (уровень 3). Ретрив-замеры (`eval_retrieval`, `eval_paraphrase`)
`search_cases` не зовут вовсе — там выключать нечего.

ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ — почему он здесь обязателен. «Ноль срабатываний» и «инструмент не
способен обнаружить срабатывание» снаружи неразличимы. На этом проект уже горел: сравнение
обрезанных якорей показало «ретрив стабилен», хотя нестабилен был именно он (урок 16.08 в
`EVAL_GUIDE`), а гейт «без чужих чисел» строил эталон из той самой функции, из которой правка
числа и убрала (урок 18.08). Поэтому сначала спрашиваем петлю ЕЁ ЖЕ кейсами: если кейс не
находится по собственному вопросу, скрипт падает с кодом 2 и числа не печатаются вовсе.

ЗАПУСК (нужен поднятый Qdrant с коллекцией `verified_cases`; DeepSeek НЕ нужен, денег не стоит):

  .venv\Scripts\python scripts\eval_cases_influence.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.rag.embeddings import embed_query  # noqa: E402
from app.rag.pipeline import MAX_CASES  # noqa: E402
from app.rag.retriever import search_cases  # noqa: E402
from scripts.seed_cases import load_cases  # noqa: E402


def _golden(name: str) -> list[str]:
    data = json.loads((ROOT / "scripts" / f"{name}.json").read_text(encoding="utf-8"))
    return [c["query"] for c in data["cases"]]


def _determinism() -> list[str]:
    from scripts.eval_determinism import QUERIES

    return [q for q, _ in QUERIES]


def sets() -> dict[str, list[str]]:
    return {
        "eval_golden (уровень 2)": _golden("eval_golden"),
        "eval_golden_negative (гейт отказа)": _golden("eval_golden_negative"),
        "eval_golden_borderline (гейт отказа)": _golden("eval_golden_borderline"),
        "eval_determinism (уровень 3)": _determinism(),
    }


def probe(query: str) -> list[dict]:
    """Тот же вызов, что в `pipeline.answer`."""
    return search_cases(query, limit=MAX_CASES, qvec=embed_query(query))


def _label(case: dict) -> str:
    return (case.get("product_name") or case.get("query") or "?")[:46]


def positive_control() -> int:
    """Петля обязана находить кейс по его собственному вопросу. Иначе всё остальное — не данные."""
    seeded = load_cases()
    print(f"ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ — {len(seeded)} кейсов спрашиваем их же вопросами")
    hits = 0
    for c in seeded:
        got = probe(c["query"])
        ok = bool(got)
        hits += ok
        mark = "  ok" if ok else "  ✗ НЕ НАШЁЛСЯ"
        best = f"{got[0].get('_dense'):.3f}" if got and got[0].get("_dense") is not None else "—"
        print(f"   {mark:14} dense={best:>6}  {c['_file']}")
    print(f"   найдено {hits} из {len(seeded)}")
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Сколько раз петля кейсов срабатывает на наборах замера (EV15 #99)")
    ap.add_argument("--quiet", action="store_true", help="без построчного контроля")
    args = ap.parse_args()

    print(f"коллекция кейсов: {settings.QDRANT_CASES_COLLECTION} · "
          f"порог CASE_RELEVANCE_MIN={settings.CASE_RELEVANCE_MIN} · MAX_CASES={MAX_CASES}")
    print("=" * 78)

    if args.quiet:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            control = positive_control()
        print(f"ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: кейс находится по своему вопросу в {control} случаях")
    else:
        control = positive_control()

    if control == 0:
        print("\n❌ ИНСТРУМЕНТ СЛЕП: петля не находит даже собственные кейсы. Числа ниже не\n"
              "   значили бы ничего — проверьте, что Qdrant поднят и коллекция засеяна\n"
              "   (scripts/seed_cases.py), и повторите.")
        sys.exit(2)

    print("=" * 78)
    total_q = total_fired = 0
    for name, queries in sets().items():
        fired = []
        for q in queries:
            got = probe(q)
            if got:
                fired.append((q, got))
        total_q += len(queries)
        total_fired += len(fired)
        share = len(fired) / len(queries) if queries else 0.0
        print(f"{name}: сработало {len(fired)} из {len(queries)} = {share:.1%}")
        for q, got in fired:
            print(f"   ⚠ «{q[:60]}»")
            for g in got:
                d = g.get("_dense")
                print(f"        dense={d if d is None else round(d, 3)} "
                      f"by_code={g.get('_by_code')}  {_label(g)}")

    print("=" * 78)
    share = total_fired / total_q if total_q else 0.0
    print(f"ИТОГО: {total_fired} срабатываний на {total_q} вопросах = {share:.1%}")
    if total_fired == 0:
        print("→ Выключение `verified_cases` НЕ МЕНЯЕТ контекст ни на одном вопросе наборов:\n"
              "  предусловие гайда исполняется бесплатно, перезамер базы не нужен.")
    else:
        print("→ Конфигурации РАСХОДЯТСЯ: числа, снятые с включённой петлёй, несопоставимы с\n"
              "  числами, снятыми с выключенной. Решение о перезамере базы принимать по этому\n"
              "  списку, а не по общей доле.")


if __name__ == "__main__":
    main()
