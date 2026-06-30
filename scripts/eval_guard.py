"""P1 #4 + #5 — out-of-scope guard и confusable-различение.

Два режима, оба читают курированные наборы:
  • --mode negative   → scripts/eval_golden_negative.json: запросы ВНЕ 719. Мерит, ловит ли
    guard их (dense top-1 < RELEVANCE_SOFT) и считает false-accept (вне-719 принят за in-scope).
    Опц. --check-refusal: гоняет полный pipeline.answer и проверяет реальный ОТКАЗ (+DeepSeek).
  • --mode confusable → scripts/eval_confusable.json: кросс-секционно-склонные запросы. Мерит
    различение — верный раздел в top-1/top-3 и как часто соблазн-раздел перебивает верный.

Детерминированное ядро без DeepSeek (нужен Qdrant + e5). Прогон после F1 (контеншн по e5).
  .venv\\Scripts\\python scripts\\eval_guard.py --mode both --rerank --report docs/eval_guard_report.md
  .venv\\Scripts\\python scripts\\eval_guard.py --mode negative --check-refusal
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

from app.rag.retriever import dense_top1, search  # noqa: E402

NEG = ROOT / "scripts" / "eval_golden_negative.json"
CONF = ROOT / "scripts" / "eval_confusable.json"


def _load(p: Path) -> list[dict]:
    return json.loads(p.read_text(encoding="utf-8"))["cases"]


def _sec_rank(hits, sec: str) -> int | None:
    for i, h in enumerate(hits, 1):
        if h.section_roman == sec:
            return i
    return None


# --------------------------------------------------------------------------- #
# Режим negative — out-of-scope guard
# --------------------------------------------------------------------------- #
def run_negative(limit: int, check_refusal: bool, progress: bool) -> list[str]:
    from app.rag.pipeline import RELEVANCE_SOFT

    cases = _load(NEG)
    rows = []
    it = cases
    if progress:
        try:
            from tqdm import tqdm
            it = tqdm(cases, unit="кейс", desc="negative")
        except ImportError:
            pass
    for c in it:
        dt1 = dense_top1(c["query"])
        flagged = dt1 < RELEVANCE_SOFT  # guard поднимет флаг → правило 1б → отказ
        row = {**c, "dt1": dt1, "flagged": flagged}
        if check_refusal:
            from app.rag.pipeline import answer
            ans = answer(c["query"])
            row["low_rel"] = ans.low_relevance
            row["refused"] = ans.low_relevance or any(
                m in ans.text.lower() for m in ("не найден", "вне сферы", "уточните", "не относится")
            )
        rows.append(row)

    n = len(rows)
    flagged = sum(1 for r in rows if r["flagged"])
    dts = [r["dt1"] for r in rows]
    L = ["=" * 78,
         f"OUT-OF-SCOPE GUARD (P1 #4) — {n} заведомо вне-719 запросов, порог RELEVANCE_SOFT={RELEVANCE_SOFT}",
         "=" * 78,
         f"  guard поднял флаг (dense top-1 < порога): {flagged}/{n} ({flagged/n:.2f})",
         f"  FALSE-ACCEPT (вне-719 принят за in-scope, dt1 ≥ порога): {n - flagged}/{n}",
         f"  dense top-1 по негативам: min={min(dts):.3f} median={statistics.median(dts):.3f} max={max(dts):.3f}",
         "  (для калибровки: in-scope min ≈ 0.808 по docs/eval_report.md — негативы должны быть НИЖЕ)",
         ""]
    if check_refusal:
        refused = sum(1 for r in rows if r["refused"])
        L.append(f"  РЕАЛЬНЫЙ ОТКАЗ (полный pipeline.answer): {refused}/{n} ({refused/n:.2f})")
        L.append("")
    # false-accept'ы — что движок ошибочно считает профильным (выше порога), худшие сверху
    fa = sorted((r for r in rows if not r["flagged"]), key=lambda r: -r["dt1"])
    if fa:
        L.append(f"FALSE-ACCEPT (dt1 ≥ {RELEVANCE_SOFT}) — кандидаты на ужесточение порога:")
        for r in fa:
            L.append(f"  dt1={r['dt1']:.3f} [{r['category']:<18}] {r['query'][:46]}")
        L.append("")
    return L


# --------------------------------------------------------------------------- #
# Режим confusable — кросс-секционное различение
# --------------------------------------------------------------------------- #
def run_confusable(limit: int, rerank_on: bool, progress: bool) -> list[str]:
    cases = _load(CONF)
    rows = []
    it = cases
    if progress:
        try:
            from tqdm import tqdm
            it = tqdm(cases, unit="кейс", desc="confusable")
        except ImportError:
            pass
    for c in it:
        hits = search(c["query"], okpd2=None, limit=limit)
        if rerank_on and hits and not any(h.okpd2_match for h in hits):
            from app.rag.reranker import rerank
            hits = rerank(c["query"], hits)
        cr = _sec_rank(hits, c["correct_section"])
        tr = _sec_rank(hits, c["tempting_section"])
        rows.append({**c, "correct_rank": cr, "tempting_rank": tr,
                     "top1": (f"[{hits[0].section_roman}] {hits[0].product_name}" if hits else "—")})

    n = len(rows)
    r1 = sum(1 for r in rows if r["correct_rank"] == 1) / n
    r3 = sum(1 for r in rows if r["correct_rank"] and r["correct_rank"] <= 3) / n
    # соблазн перебил верный (и сам верный не на 1-м)
    tempt = sum(1 for r in rows if r["tempting_rank"] and
                (not r["correct_rank"] or r["tempting_rank"] < r["correct_rank"]))
    L = ["=" * 78,
         f"CONFUSABLE-РАЗЛИЧЕНИЕ (P1 #5) — {n} кросс-склонных запросов, top-{limit}"
         + ("  [+реранкер]" if rerank_on else ""),
         "=" * 78,
         f"  верный раздел: recall@1={r1:.2f}  recall@3={r3:.2f}",
         f"  соблазн-раздел ПЕРЕБИЛ верный: {tempt}/{n} ({tempt/n:.2f})",
         ""]
    L.append("Детализация (ранг верного → соблазн):")
    for r in rows:
        cr = r["correct_rank"]; tr = r["tempting_rank"]
        mark = "✓" if cr == 1 else ("·" if cr and cr <= 3 else "✗")
        L.append(f"  {mark} {r['correct_section']:>4}#{str(cr):<4}/{r['tempting_section']:>4}#{str(tr):<4} "
                 f"{r['query'][:40]:40}  top1: {r['top1'][:26]}")
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="P1 out-of-scope guard + confusable различение")
    ap.add_argument("--mode", choices=["negative", "confusable", "both"], default="both")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--rerank", action="store_true", help="confusable: применить продакшен-реранкер")
    ap.add_argument("--check-refusal", action="store_true", help="negative: полный pipeline.answer (+DeepSeek)")
    ap.add_argument("--report", type=str, default="")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()

    try:
        from app.rag.retriever import _client
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}). Подними Docker + Qdrant (:6533).")

    out: list[str] = []
    if args.mode in ("negative", "both"):
        out += run_negative(args.limit, args.check_refusal, not args.no_progress)
    if args.mode in ("confusable", "both"):
        out += run_confusable(args.limit, args.rerank, not args.no_progress)

    text = "\n".join(out)
    print("\n" + text)
    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# P1 guard + confusable eval\n\n```\n" + text + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
