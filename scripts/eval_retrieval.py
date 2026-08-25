"""Eval-харнес качества РЕТРИВА навигатора (стадия 0 плана улучшения RAG).

Прогоняет golden set (scripts/eval_golden.json) через retriever.search и измеряет,
насколько хорошо гибрид-поиск находит нужный раздел 719. НЕ вызывает DeepSeek —
замер детерминированный и бесплатный (нужен только поднятый Qdrant + e5).

Метрики (по in-scope кейсам, эталон = ожидаемый section_roman):
  • recall@1/3/5 — доля кейсов, где верный раздел попал в топ-k;
  • MRR — средний обратный ранг верного раздела (1.0 = всегда первый).

Out-of-scope кейсы (продукция вне 719): меряем dense top-1 cosine — это данные для
калибровки порога out-of-scope guard (стадия 1). Если in-scope top-1 стабильно выше
out-of-scope top-1 — порог между ними отсекает мусор, не роняя реальные запросы.

Запуск (нужен поднятый Qdrant с коллекцией pp719):
  .venv\\Scripts\\python scripts\\eval_retrieval.py
  .venv\\Scripts\\python scripts\\eval_retrieval.py --limit 5 --report docs/eval_report.md
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

enable_utf8()  # #107: скрипт печатает значки вне cp1251 — падал бы в момент печати
from app.core.config import settings  # noqa: E402
from app.rag.embeddings import embed_query  # noqa: E402
from app.rag.retriever import DENSE, _client, search  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_golden.json"


def dense_top1_cosine(query: str) -> float:
    """Чистый dense-поиск (e5), top-1 косинусное сходство. Для калибровки порога guard."""
    dv = embed_query(query)
    res = _client().query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=dv,
        using=DENSE,
        limit=1,
        with_payload=False,
    )
    return float(res.points[0].score) if res.points else 0.0


def first_rank(hits, expected_section: str) -> int | None:
    """1-based ранг первого хита с нужным разделом, иначе None."""
    for i, h in enumerate(hits, 1):
        if h.section_roman == expected_section:
            return i
    return None


def evaluate(limit: int, rerank_on: bool = False):
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = data["cases"]
    rows = []
    for c in cases:
        hits = search(c["query"], okpd2=c.get("okpd2") or None, limit=limit)
        # Реранкер (стадия 2): только при ОТСУТСТВИИ совпадения по коду (код авторитетнее).
        if rerank_on and hits and not any(h.okpd2_match for h in hits):
            from app.rag.reranker import rerank
            hits = rerank(c["query"], hits)
        top_sec = hits[0].section_roman if hits else "—"
        rank = first_rank(hits, c["expected_section"]) if c["in_scope"] else None
        rows.append({
            "id": c["id"],
            "in_scope": c["in_scope"],
            "expected": c["expected_section"] or "—",
            "okpd2": c.get("okpd2") or "",
            "top_sec": top_sec,
            "rank": rank,
            "okpd2_match": any(h.okpd2_match for h in hits),
            "dense_top1": dense_top1_cosine(c["query"]),
            "query": c["query"],
        })
    return rows


def summarize(rows, limit: int) -> list[str]:
    out = [r for r in rows if not r["in_scope"]]
    ins = [r for r in rows if r["in_scope"]]

    def recall_at(k: int) -> float:
        hit = sum(1 for r in ins if r["rank"] is not None and r["rank"] <= k)
        return hit / len(ins) if ins else 0.0

    mrr = statistics.mean((1.0 / r["rank"] if r["rank"] else 0.0) for r in ins) if ins else 0.0

    lines = []
    lines.append("=" * 74)
    lines.append(f"EVAL РЕТРИВА — golden set {len(rows)} кейсов "
                 f"({len(ins)} in-scope, {len(out)} out-of-scope), limit={limit}")
    lines.append("=" * 74)
    lines.append("")
    lines.append("Метрики ретрива (in-scope, эталон = section_roman):")
    lines.append(f"  recall@1 = {recall_at(1):.2f}   "
                 f"recall@3 = {recall_at(3):.2f}   recall@5 = {recall_at(min(5, limit)):.2f}")
    lines.append(f"  MRR      = {mrr:.3f}")
    lines.append("")

    # калибровка порога out-of-scope guard
    in_d = [r["dense_top1"] for r in ins]
    out_d = [r["dense_top1"] for r in out]
    if in_d and out_d:
        in_min, out_max = min(in_d), max(out_d)
        lines.append("Калибровка out-of-scope guard (dense top-1 cosine):")
        lines.append(f"  in-scope :  min={in_min:.3f}  median={statistics.median(in_d):.3f}  max={max(in_d):.3f}")
        lines.append(f"  out-scope:  min={min(out_d):.3f}  median={statistics.median(out_d):.3f}  max={out_max:.3f}")
        if out_max < in_min:
            thr = (in_min + out_max) / 2
            lines.append(f"  → ЧИСТОЕ разделение. Рекомендуемый порог ≈ {thr:.3f} "
                         f"(между out_max={out_max:.3f} и in_min={in_min:.3f}).")
        else:
            lines.append(f"  → ПЕРЕСЕЧЕНИЕ (out_max={out_max:.3f} ≥ in_min={in_min:.3f}). "
                         f"Порог компромиссный — смотри таблицу, какие кейсы в зоне перекрытия.")
        lines.append("")

        # симуляция первого слоя guard (как порог из pipeline ведёт себя на golden set)
        try:
            from app.rag.pipeline import RELEVANCE_SOFT

            def flagged(r):  # как в pipeline: нет совпадения по коду И dense top-1 ниже порога
                return (not r["okpd2_match"]) and r["dense_top1"] < RELEVANCE_SOFT

            f_out = sum(1 for r in out if flagged(r))
            f_in = sum(1 for r in ins if flagged(r))
            lines.append(f"Симуляция out-of-scope guard (порог RELEVANCE_SOFT={RELEVANCE_SOFT}):")
            lines.append(f"  флаг на {f_out}/{len(out)} out-of-scope (хорошо — попадут под правило 1б),")
            lines.append(f"  флаг на {f_in}/{len(ins)} in-scope (безвредный nudge: ответ НЕ блокируется,")
            lines.append("       при совпадении смысла модель всё равно отвечает по позиции).")
            lines.append("")
        except Exception:  # noqa: BLE001 — симуляция опциональна
            pass

    # таблица
    lines.append(f"{'id':>3} {'scope':<5} {'ожид':>5} {'top1':>5} {'ранг':>5} {'dT1':>6}  запрос")
    lines.append("-" * 74)
    for r in rows:
        scope = "IN" if r["in_scope"] else "OUT"
        rank = "—" if r["rank"] is None else str(r["rank"])
        miss = "  ✗" if (r["in_scope"] and r["rank"] is None) else ""
        lines.append(f"{r['id']:>3} {scope:<5} {r['expected']:>5} {r['top_sec']:>5} "
                     f"{rank:>5} {r['dense_top1']:>6.3f}  {r['query'][:34]}{miss}")
    lines.append("")
    misses = [r for r in ins if r["rank"] is None]
    if misses:
        lines.append(f"ПРОМАХИ ретрива (верный раздел не в топ-{limit}): "
                     + ", ".join(f"#{r['id']}" for r in misses))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Eval-харнес ретрива навигатора 719")
    ap.add_argument("--limit", type=int, default=5, help="глубина выдачи (top-k)")
    ap.add_argument("--report", type=str, default="", help="путь для сохранения отчёта (markdown)")
    ap.add_argument("--rerank", action="store_true",
                    help="применить LLM-реранкер (DeepSeek) к code-less выдаче — замер стадии 2")
    args = ap.parse_args()

    try:
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}).\nПодними Docker Desktop — коллекция pp719 встанет сама.")

    rows = evaluate(args.limit, rerank_on=args.rerank)
    report = summarize(rows, args.limit)
    if args.rerank:
        report.insert(3, "  [РЕРАНКЕР ВКЛЮЧЁН: DeepSeek-rerank для code-less выдачи]")
    print("\n".join(report))

    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# Eval-отчёт ретрива (стадия 0)\n\n```\n" + "\n".join(report) + "\n```\n",
                        encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
