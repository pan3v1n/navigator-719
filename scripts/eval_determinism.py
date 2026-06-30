"""P2 #8 — детерминизм: воспроизводим ли ответ при повторных вызовах (temp=0.1 ≠ 0).

Прод-инструмент должен давать стабильный ответ: эксперт, переспросив то же, не должен видеть
ДРУГИЕ баллы/атрибуцию. Гоняем каждый запрос N раз и меряем дисперсию: набор чисел баллов/%,
раздел top-1, флаг незаземлённых чисел. Ретрив/реранкер детерминированы (temp=0) — дисперсия,
если есть, идёт от ГЕНЕРАЦИИ (temp=0.1).

ВНИМАНИЕ: DeepSeek, N×|queries| вызовов (по умолч. 5×10=50 ≈ ¥2-3).
  .venv\\Scripts\\python scripts\\eval_determinism.py --runs 5 --report docs/eval_determinism_report.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.rag.pipeline import answer, claim_numbers  # noqa: E402

# Представительные запросы (числоёмкие, разные разделы, с кодом и без)
QUERIES = [
    ("сколько баллов нужно для производства городских автобусов", None),
    ("требования и баллы для шариковых и роликовых подшипников", "28.15.10"),
    ("производим светодиоды белого диапазона, какие требования", None),
    ("какие баллы для гусеничных бульдозеров", None),
    ("требования к локализации центробежных насосов", None),
    ("сколько баллов для металлорежущих станков с ЧПУ", None),
    ("требования для производства фармацевтических субстанций", None),
    ("баллы для одноразовых медицинских масок", None),
    ("какие требования для башенных грузоподъёмных кранов", "29.22.14.400"),
    ("требования к производству интегральных микросхем", None),
]


def evaluate(runs: int, limit: int, progress: bool):
    rows = []
    it = QUERIES
    if progress:
        try:
            from tqdm import tqdm
            it = tqdm(QUERIES, unit="зап", desc="детерминизм")
        except ImportError:
            pass
    for q, code in it:
        num_sets, secs, flags = [], [], []
        for _ in range(runs):
            ans = answer(q, okpd2=code, limit=limit)
            num_sets.append(frozenset(claim_numbers(ans.text)))
            secs.append(ans.hits[0].section_roman if ans.hits else "—")
            flags.append(bool(ans.unverified_numbers))
        rows.append({
            "q": q, "code": code or "",
            "num_variants": len(set(num_sets)),       # 1 = стабильный набор чисел
            "sec_variants": len(set(secs)),           # 1 = стабильная атрибуция
            "flag_variants": len(set(flags)),         # 1 = стабильный guard-флаг
            "example_nums": sorted(set().union(*num_sets)) if num_sets else [],
        })
    return rows


def report(rows, runs: int) -> list[str]:
    n = len(rows)
    num_stable = sum(1 for r in rows if r["num_variants"] == 1)
    sec_stable = sum(1 for r in rows if r["sec_variants"] == 1)
    L = ["=" * 78,
         f"P2 #8 ДЕТЕРМИНИЗМ — {n} запросов × {runs} повторов, модель={settings.DEEPSEEK_MODEL}",
         "=" * 78,
         f"  СТАБИЛЬНЫЙ набор чисел баллов/% (одинаков во всех {runs}): {num_stable}/{n} = {num_stable/n:.2f}",
         f"  СТАБИЛЬНАЯ атрибуция (раздел top-1): {sec_stable}/{n} = {sec_stable/n:.2f}",
         "",
         "Детализация (вариантов из N повторов; 1 = детерминирован):",
         f"  {'числа':>6} {'раздел':>7} {'флаг':>5}  запрос"]
    for r in rows:
        mark = "" if r["num_variants"] == 1 else "  ⚠ числа плавают"
        L.append(f"  {r['num_variants']:>6} {r['sec_variants']:>7} {r['flag_variants']:>5}  {r['q'][:40]}{mark}")
    L.append("")
    unstable = [r for r in rows if r["num_variants"] > 1]
    if unstable:
        L.append("НЕСТАБИЛЬНЫЕ ЧИСЛА (объединение по повторам — эксперт может увидеть разное):")
        for r in unstable:
            L.append(f"  «{r['q'][:46]}»: {r['num_variants']} вариантов, числа из всех прогонов: {r['example_nums']}")
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="P2 #8 детерминизм (вызывает DeepSeek)")
    ap.add_argument("--runs", type=int, default=5)
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

    rows = evaluate(args.runs, args.limit, not args.no_progress)
    out = report(rows, args.runs)
    text = "\n".join(out)
    print("\n" + text)
    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# P2 #8 детерминизм\n\n```\n" + text + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
