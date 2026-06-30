"""P2 #6 — faithfulness-стресс на БОЛЬШОМ наборе (не 46 golden).

`eval_answers.py` мерил faithfulness на 46 golden. Этот харнес стрессует анти-галлюцинацию
ширше: на стратифицированной выборке позиций строит запросы, ЯВНО требующие числа (баллы/пороги),
и считает, как часто модель выдумывает число, которого нет в контексте. Это главный принцип
проекта — «ИИ черновик, число обязано быть заземлено». Использует РАНТАЙМ-постпроверку пайплайна
(`Answer.unverified_numbers`) — замер = поведение в проде.

Запрос даём с кодом ОКПД2 позиции → нужный контекст гарантированно найден (изолируем faithfulness
от ретрива: меряем именно «выдумывает ли модель число сверх контекста»).

ВНИМАНИЕ: вызывает DeepSeek (полный пайплайн на запрос). ~110 запросов ≈ ¥8-10.
  .venv\\Scripts\\python scripts\\eval_faithfulness_stress.py --per-section 4 --report docs/eval_faithfulness_stress_report.md
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.rag.pipeline import answer, claim_numbers  # noqa: E402
from scripts.eval_paraphrase import stratified  # noqa: E402
from scripts.load_kb import load_records  # noqa: E402


def _query(name: str) -> str:
    return (f"Какие требования и сколько баллов нужно для подтверждения производства в России: "
            f"{name}? Перечисли требования с баллами.")


def evaluate(per_section: int, seed: int, limit: int, progress: bool):
    recs = stratified(load_records(), per_section, seed)
    rows = []
    it = recs
    if progress:
        try:
            from tqdm import tqdm
            it = tqdm(recs, unit="зап", desc="faithfulness")
        except ImportError:
            pass
    for r in it:
        code = (r.get("okpd2_codes") or [None])[0]
        ans = answer(_query(r["product_name"]), okpd2=code, limit=limit)
        claims = claim_numbers(ans.text)
        rows.append({
            "section": r.get("section_roman", "?"),
            "name": r["product_name"],
            "n_claims": len(claims),
            "fabricated": ans.unverified_numbers,          # рантайм-постпроверка
            "faithful": not ans.unverified_numbers,
        })
    return rows


def report(rows) -> list[str]:
    n = len(rows)
    faithful = sum(1 for r in rows if r["faithful"])
    total_claims = sum(r["n_claims"] for r in rows)
    total_fab = sum(len(r["fabricated"]) for r in rows)
    L = ["=" * 78,
         f"P2 #6 FAITHFULNESS-СТРЕСС — {n} числовых запросов, модель={settings.DEEPSEEK_MODEL}",
         "=" * 78,
         f"  FAITHFULNESS (ответы без выдуманных чисел) = {faithful}/{n} = {faithful/n:.3f}",
         f"  всего чисел баллов/% в ответах: {total_claims}; выдумано (нет в контексте): {total_fab} "
         f"({100*total_fab/max(total_claims,1):.1f}% чисел)",
         f"  (рантайм-guard помечает КАЖДОЕ выдуманное число эксперту — здесь считаем сырую частоту)",
         ""]
    # по разделам
    bysec = defaultdict(list)
    for r in rows:
        bysec[r["section"]].append(r)
    weak = sorted(((s, len(rs), sum(1 for r in rs if r["faithful"]) / len(rs))
                   for s, rs in bysec.items()), key=lambda x: x[2])
    L.append("FAITHFULNESS ПО РАЗДЕЛАМ (слабые наверх):")
    for s, k, f in weak:
        L.append(f"  {s:>5} N={k:>3}  faithful={f:.2f}")
    L.append("")
    bad = [r for r in rows if not r["faithful"]]
    if bad:
        L.append(f"ВЫДУМАННЫЕ ЧИСЛА ({len(bad)} ответов):")
        for r in bad:
            L.append(f"  [{r['section']:>4}] {r['name'][:42]:42} выдумано: {', '.join(r['fabricated'])}")
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="P2 #6 faithfulness-стресс (вызывает DeepSeek)")
    ap.add_argument("--per-section", type=int, default=4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--report", type=str, default="")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()

    try:
        from app.rag.retriever import _client
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}).")
    if not (settings.DEEPSEEK_API_KEY or "").strip():
        sys.exit("Нет DEEPSEEK_API_KEY в .env.")

    rows = evaluate(args.per_section, args.seed, args.limit, not args.no_progress)
    out = report(rows)
    text = "\n".join(out)
    print("\n" + text)
    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# P2 #6 faithfulness-стресс\n\n```\n" + text + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
