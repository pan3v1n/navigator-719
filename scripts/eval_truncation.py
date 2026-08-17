"""P2 #7 — truncation-impact: сколько информации теряет усечение операций (кап MAX_OPS_TARGET).

`format_context` показывает модели максимум MAX_OPS_TARGET операций ЦЕЛЕВОГО хита, ранжируя их по
релевантности запросу через `_rank_operations`. (У прочих кандидатов требований в контексте нет
вовсе — `EV7`, 17.08.2026; прежний парный кап `MAX_OPS_OTHER = 12` удалён.) У мега-продуктов операций сотни → модель
видит лишь верхушку. Вопрос: теряются ли при этом БАЛЛЬНЫЕ требования (то, что эксперт обязан
свести). Это решает, нужен ли отложенный в 2.0 parent/child-чанкинг или достаточно поднять порог.

Детерминированно, без DeepSeek: реплицируем выбор операций как в пайплайне и считаем покрытие
БАЛЛЬНЫХ операций (с непустым `points`) для NAME-запроса (типовой вход «расскажи про продукт X»).

Запуск:
  .venv\\Scripts\\python scripts\\eval_truncation.py --report docs/eval_truncation_report.md
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.pipeline import MAX_OPS_TARGET, _rank_operations  # noqa: E402
from scripts.load_kb import load_records  # noqa: E402


def _ops(rec: dict) -> list[dict]:
    out = []
    for b in rec.get("requirement_blocks") or []:
        out.extend(b.get("operations") or [])
    return out


def _has_points(o: dict) -> bool:
    return o.get("points") is not None


def evaluate(cap: int):
    recs = load_records()
    rows = []
    for r in recs:
        ops = _ops(r)
        if len(ops) <= cap:
            continue  # усечение не применяется
        shown = _rank_operations(ops, r["product_name"])[:cap]  # как в format_context (NAME-запрос)
        pts_total = sum(1 for o in ops if _has_points(o))
        pts_shown = sum(1 for o in shown if _has_points(o))
        rows.append({
            "section": r.get("section_roman", "?"),
            "name": r["product_name"],
            "ops_total": len(ops),
            "pts_total": pts_total,
            "pts_shown": pts_shown,
            "pts_cov": (pts_shown / pts_total) if pts_total else 1.0,
        })
    return recs, rows


def report(recs, rows, cap: int) -> list[str]:
    n_all = len(recs)
    L = ["=" * 78,
         f"P2 #7 TRUNCATION-IMPACT — MAX_OPS_TARGET={cap}, база {n_all} записей",
         "=" * 78,
         f"  мега-продуктов (>{cap} операций): {len(rows)} ({100*len(rows)/n_all:.1f}%)",
         ""]
    if not rows:
        L.append("  усечение не применяется ни к одной записи.")
        return L

    ops_t = [r["ops_total"] for r in rows]
    L.append(f"  операций у мега: median={int(statistics.median(ops_t))} max={max(ops_t)}; "
             f">30 опер: {sum(1 for v in ops_t if v>30)}, >50: {sum(1 for v in ops_t if v>50)}")
    L.append("")

    # ключевое: покрытие БАЛЛЬНЫХ операций (что эксперт обязан свести)
    with_pts = [r for r in rows if r["pts_total"] > 0]
    L.append("ПОКРЫТИЕ БАЛЛЬНЫХ ОПЕРАЦИЙ (points!=null) у мега-продуктов:")
    if with_pts:
        covs = [r["pts_cov"] for r in with_pts]
        full = sum(1 for r in with_pts if r["pts_shown"] >= r["pts_total"])
        L.append(f"  мега с балльными операциями: {len(with_pts)}")
        L.append(f"  ВСЕ балльные операции влезли в top-{cap}: {full}/{len(with_pts)} ({full/len(with_pts):.2f})")
        L.append(f"  доля показанных балльных опер: median={statistics.median(covs):.2f} mean={statistics.mean(covs):.2f}")
        lose = [r for r in with_pts if r["pts_shown"] < r["pts_total"]]
        L.append(f"  ТЕРЯЮТ балльные требования (pts_shown < pts_total): {len(lose)}")
        L.append("")
        if lose:
            L.append(f"  Худшие (скрыто больше всего балльных операций), top-15:")
            for r in sorted(lose, key=lambda r: (r["pts_shown"] - r["pts_total"]))[:15]:
                L.append(f"    [{r['section']:>4}] {r['name'][:40]:40} баллы {r['pts_shown']:>3}/{r['pts_total']:<3} "
                         f"(всего опер {r['ops_total']})")
    else:
        L.append("  у мега-продуктов нет балльных операций (баллы зависят от категории/в min_threshold).")
    L.append("")

    # симуляция: что даёт подъём порога
    L.append(f"ЕСЛИ ПОДНЯТЬ ПОРОГ (сколько мега перестанут терять балльные операции):")
    for newcap in (20, 25, 30, 40):
        if with_pts:
            full = sum(1 for r in with_pts if r["pts_total"] <= newcap)
            L.append(f"  MAX_OPS={newcap}: все баллы влезут у {full}/{len(with_pts)} ({full/len(with_pts):.2f}) "
                     f"мега-с-баллами  | мега-продуктов всего >{newcap}: {sum(1 for r in rows if r['ops_total']>newcap)}")
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="P2 #7 truncation-impact (детерм., без DeepSeek)")
    ap.add_argument("--cap", type=int, default=MAX_OPS_TARGET)
    ap.add_argument("--report", type=str, default="")
    args = ap.parse_args()

    recs, rows = evaluate(args.cap)
    out = report(recs, rows, args.cap)
    text = "\n".join(out)
    print(text)
    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# P2 #7 truncation-impact\n\n```\n" + text + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
