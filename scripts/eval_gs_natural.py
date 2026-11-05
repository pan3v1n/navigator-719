"""recall на `gs_natural` — вопросах пользователей боя с подтверждённой позицией (EV3 #80, уровень 1).

ЗАЧЕМ. Главная цифра ретрива по `EVAL_GUIDE` («recall@1 на `gs_natural`, baseline, затем
невозрастание») не мерилась ни разу: набора не было, а `eval_coverage` спрашивает наименованием
позиции — верхняя граница. Набор собирает `scripts/build_gs_natural.py` из копии боевой базы; лежит
он ВНЕ репозитория (тексты вопросов — данные пользователей), поэтому путь передаётся аргументом.

ЧТО МЕРЯЕТСЯ. Путь ответа до вызова модели — `pipeline._plan_answer`, как у `eval_experts`: код из
вопроса, поиск, окно 8, выбор целевой. Реранкер выключен (зовёт DeepSeek; в бою работает только без
совпадения по коду). ⚠ Любой вызов DeepSeek на этом пути — ошибка замера, а не повод платить: клиент
подменён, и вопрос, который повёл бы в модель до плана (процедурный маршрут, контекстуализация),
считается отдельной строкой «ушёл не в товарную ветку».

  * целевая  — эталонная позиция среди целевых (`pipeline.target_hits`) — то, о чём ответ;
  * r@1 / r@3 — эталон на первом / в первых трёх местах окна;
  * в окне   — эталон среди 8 позиций контекста.
Разрез «с кодом / без кода»: с кодом ретрив решает по коду почти детерминированно, рабочая цифра —
без кода.

Запуск (Git Bash):
  NO_PROXY=localhost,127.0.0.1 QDRANT_COLLECTION=pp719_ed1031 \\
      .venv/Scripts/python scripts/eval_gs_natural.py --set D:/navigator-export/gs_natural.json \\
      [--json OUT_вне_репо.json]
Печатаются только агрегаты; тексты вопросов — только в `--json` вне репозитория.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class PaidCall(RuntimeError):
    """Путь замера дошёл до DeepSeek — так быть не должно."""


def _no_llm(*_a, **_k):
    raise PaidCall("вызов DeepSeek на пути замера")


def evaluate_case(case: dict) -> dict:
    from app.rag import pipeline
    from app.tools.navigator import extract_okpd2

    q = case["query"]
    gold = case["expect"]["anchor"]
    code = extract_okpd2(q)
    row = {"id": case["id"], "has_code": bool(code), "route": "товарная"}
    try:
        plan = pipeline._plan_answer(q, okpd2=code)
    except PaidCall:
        row["route"] = "ушёл бы в модель до плана"
        return row
    if isinstance(plan, pipeline.Answer):
        row["route"] = "ранний ответ без плана"
        return row
    window = [h.source_anchor for h in plan.hits]
    targets = [h.source_anchor for h in pipeline.target_hits(plan.hits, plan.codes)]
    row.update({
        "target": gold in targets,
        "r1": bool(window) and window[0] == gold,
        "r3": gold in window[:3],
        "window": gold in window,
        "n_targets": len(targets),
    })
    return row


def summarize(rows: list[dict]) -> dict:
    def part(rs):
        planned = [r for r in rs if r["route"] == "товарная"]
        n = len(rs)
        out = {"n": n, "не товарная": n - len(planned)}
        for k in ("target", "r1", "r3", "window"):
            hit = sum(1 for r in planned if r.get(k))
            out[k] = {"hit": hit, "rate": round(hit / n, 3) if n else None}
        return out
    return {"всего": part(rows),
            "с кодом": part([r for r in rows if r["has_code"]]),
            "без кода": part([r for r in rows if not r["has_code"]])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--set", required=True, type=Path, help="gs_natural.json (вне репозитория)")
    ap.add_argument("--json", type=Path, help="детали по кейсам (только вне репозитория)")
    args = ap.parse_args()
    for p in (args.set, args.json):
        if p is not None and ROOT in p.resolve().parents:
            sys.exit(f"{p}: набор и детали с текстами вопросов — только вне репозитория (152-ФЗ).")

    cases = json.loads(args.set.read_text(encoding="utf-8"))["cases"]

    from app.core.config import settings
    settings.RERANK_ENABLED = False
    from app.rag import pipeline, reranker, retriever
    pipeline._client = _no_llm          # ни одного платного вызова на пути замера
    reranker._client = _no_llm
    retriever.EXACT_SEARCH = True       # M1: повторяемость замера

    rows = [evaluate_case(c) for c in cases]
    s = summarize(rows)
    print("=" * 72)
    print(f"gs_natural: {args.set.name}, кейсов {len(rows)}, коллекция {settings.QDRANT_COLLECTION}")
    print("=" * 72)
    for part, v in s.items():
        if not v["n"]:
            continue
        line = "  ".join(f"{k} {v[k]['hit']}/{v['n']} = {v[k]['rate']:.2f}"
                         for k in ("target", "r1", "r3", "window"))
        print(f"  {part:<9} n={v['n']:>3}  {line}  · не товарная {v['не товарная']}")
    if args.json:
        args.json.write_text(json.dumps({"summary": s, "rows": rows}, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"\nДетали: {args.json}")
    return 0


if __name__ == "__main__":
    from app.core.console import enable_utf8  # #107

    enable_utf8()
    raise SystemExit(main())
