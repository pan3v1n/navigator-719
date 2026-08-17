"""K12 — точность тематического роутера процедурной ветки. Офлайн, без Qdrant и DeepSeek.

Критерий приёмки задачи: доля верной темы **≥0.9** и **ноль** тем, навешенных на чистый товарный
вопрос. Второе важнее первого: тема задаёт оговорки промпта, и не та тема даёт не те оговорки, а
на товарном вопросе тема означала бы, что процедурный фрагмент попал в товарный ответ.

Эталон размечен РУКАМИ (`scripts/eval_topics.json`): первая версия свипа задавала ожидаемую тему
регулярками, то есть метрика проверяла сама себя — и половина «расхождений» оказалась ошибкой
эталона, а не роутера.

Запуск:  .venv/Scripts/python.exe scripts/eval_topics.py [--report docs/eval_topics_report.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag import topics  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_topics.json"


def evaluate() -> tuple[list[dict], list[dict]]:
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    rows = []
    for c in cases:
        got = topics.classify(c["query"])
        rows.append({**c, "got": got, "ok": got == c["expect"]})
    pos = [r for r in rows if r["expect"] is not None]
    neg = [r for r in rows if r["expect"] is None]
    return pos, neg


def report(pos: list[dict], neg: list[dict]) -> list[str]:
    acc = sum(1 for r in pos if r["ok"]) / max(len(pos), 1)
    false_topics = [r for r in neg if r["got"] is not None]
    L = ["=" * 78,
         f"K12 ТЕМАТИЧЕСКИЙ РОУТЕР — {len(pos)} процедурных вопросов волны + {len(neg)} товарных",
         "=" * 78,
         f"  ВЕРНАЯ ТЕМА: {sum(1 for r in pos if r['ok'])}/{len(pos)} = {acc:.2f}   (порог ≥0.90)",
         f"  ТЕМА НА ТОВАРНОМ ВОПРОСЕ: {len(false_topics)}/{len(neg)}   (порог 0)",
         ""]
    dist = Counter(r["expect"] for r in pos)
    L.append("  Эталонное распределение тем: " + ", ".join(f"{k} {v}" for k, v in dist.most_common()))
    missing = [t for t in topics.TOPICS if t not in dist]
    if missing:
        L.append(f"  ⚠ Не покрыто реальными вопросами: {', '.join(missing)} — см. `gaps` в наборе")
    L.append("")
    bad = [r for r in pos if not r["ok"]]
    if bad:
        L.append("РАСХОЖДЕНИЯ (эталон → получено):")
        for r in bad:
            L.append(f"  #{r['id']:<3} {str(r['expect']):14} → {str(r['got']):14} {r['query'][:56]}")
        L.append("")
    if false_topics:
        L.append("ТЕМА НА ЧИСТОМ ТОВАРНОМ ВОПРОСЕ (процедурные оговорки уехали бы в товарный ответ):")
        for r in false_topics:
            L.append(f"  #{r['id']:<3} {str(r['got']):14} {r['query'][:60]}")
        L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="K12 точность тематического роутера (офлайн)")
    ap.add_argument("--report", type=str, default="")
    args = ap.parse_args()

    pos, neg = evaluate()
    text = "\n".join(report(pos, neg))
    print("\n" + text)
    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# K12 тематический роутер\n\n```\n" + text + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
