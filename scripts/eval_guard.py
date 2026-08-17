"""P1 #4 + #5 + EV2 — out-of-scope guard, пограничные in-scope и confusable-различение.

Четыре секции, все читают курированные наборы:
  • negative   → scripts/eval_golden_negative.json: запросы ВНЕ 719. Мерит, ловит ли guard их
    (dense top-1 < RELEVANCE_SOFT) и считает false-accept (вне-719 принят за in-scope).
  • borderline → scripts/eval_golden_borderline.json: запросы В сфере 719 с низким косинусом
    (EV2). Мерит ЛОЖНЫЕ ФЛАГИ — цену ужесточения порога.
  • threshold  → кривая обмена по обоим наборам: где вообще стоит порог.
  • confusable → scripts/eval_confusable.json: кросс-секционно-склонные запросы. Мерит
    различение — верный раздел в top-1/top-3 и как часто соблазн-раздел перебивает верный.

⚠ **Негативы в одиночку читать нельзя.** По ним одним порог хочется поднимать бесконечно: «пять
false-accept — кандидаты на ужесточение» выглядит убедительно, пока не видно, сколько при этом
получат ложный флаг ПРОФИЛЬНЫЕ вопросы. Поэтому `--mode both` (по умолчанию) запускает ВСЕ четыре
секции, а не две, как в прежней версии этого докстринга.

Режимы: both (всё, по умолчанию) · guard (negative + borderline + threshold) · и каждая секция
поимённо. Опц. --check-refusal: гоняет полный pipeline.answer и проверяет РЕАЛЬНЫЙ отказ
(+DeepSeek) — на негативах он должен быть, на пограничных in-scope его быть НЕ должно.

Детерминированное ядро без DeepSeek (нужен Qdrant + e5). Прогон после F1 (контеншн по e5).
  .venv\\Scripts\\python scripts\\eval_guard.py --report docs/eval_guard_report.md
  .venv\\Scripts\\python scripts\\eval_guard.py --mode guard --check-refusal
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
BORDER = ROOT / "scripts" / "eval_golden_borderline.json"  # EV2: пограничные IN-SCOPE


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
def run_negative(limit: int, check_refusal: bool, progress: bool) -> tuple[list[str], list[dict]]:
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
    return L, rows


# --------------------------------------------------------------------------- #
# Режим borderline — ПОГРАНИЧНЫЕ IN-SCOPE (вторая половина весов, EV2)
# --------------------------------------------------------------------------- #
def run_borderline(check_refusal: bool, progress: bool) -> tuple[list[str], list[dict]]:
    """Ложные флаги: запросы В сфере 719, которым guard поднял «похоже, вне сферы».

    Зачем отдельный набор. Негативы показывают, сколько чужого guard пропускает, и по ним одним
    порог хочется поднимать бесконечно. Цена этого не видна, пока нет второй половины весов —
    вопросов, которые в сфере, но по косинусу лежат низко. Гайд предупреждает прямым текстом:
    «отказы вне сферы выросли и ложные тоже → guard пережат»."""
    from app.rag.pipeline import RELEVANCE_SOFT

    cases = _load(BORDER)
    it = cases
    if progress:
        try:
            from tqdm import tqdm
            it = tqdm(cases, unit="кейс", desc="borderline")
        except ImportError:
            pass
    rows = []
    for c in it:
        dt1 = dense_top1(c["query"])
        row = {**c, "dt1": dt1, "flagged": dt1 < RELEVANCE_SOFT}
        if check_refusal:
            from app.rag.pipeline import answer
            ans = answer(c["query"])
            row["low_rel"] = ans.low_relevance
            # Отказом считаем ЯВНЫЙ уход от ответа: «вне сферы»/«не найдена». Просьба уточнить код
            # отказом НЕ является — правило 1г её предписывает, и штрафовать за неё нельзя.
            row["refused"] = any(m in ans.text.lower() for m in
                                 ("вне сферы", "не относится к", "не найдена", "не подпадает"))
        rows.append(row)

    n = len(rows)
    flagged = sum(1 for r in rows if r["flagged"])
    dts = [r["dt1"] for r in rows]
    L = ["=" * 78,
         f"ПОГРАНИЧНЫЕ IN-SCOPE (EV2) — {n} реальных вопросов волны, порог RELEVANCE_SOFT={RELEVANCE_SOFT}",
         "=" * 78,
         f"  ЛОЖНЫЙ ФЛАГ (in-scope принят за вне-719, dt1 < порога): {flagged}/{n} ({flagged/n:.2f})",
         f"  dense top-1 по in-scope: min={min(dts):.3f} median={statistics.median(dts):.3f} max={max(dts):.3f}",
         ""]
    if check_refusal:
        refused = sum(1 for r in rows if r["refused"])
        L.append(f"  РЕАЛЬНЫЙ ОТКАЗ на профильном вопросе (полный pipeline): {refused}/{n} — должно быть 0")
        L.append("")
    ff = sorted((r for r in rows if r["flagged"]), key=lambda r: r["dt1"])
    if ff:
        L.append(f"ЛОЖНЫЕ ФЛАГИ (dt1 < {RELEVANCE_SOFT}) — цена ужесточения порога:")
        for r in ff:
            L.append(f"  dt1={r['dt1']:.3f} [{'+'.join(r['evidence']):<18}] {r['query'][:44]}"
                     + (f"  → {r['top1_hint'][:26]}" if r.get("top1_hint") else ""))
        L.append("")
    return L, rows


# --------------------------------------------------------------------------- #
# Режим threshold — кривая обмена: где вообще стоит порог
# --------------------------------------------------------------------------- #
def run_threshold(neg: list[dict], pos: list[dict]) -> list[str]:
    """Обе стороны весов на одной шкале: сколько чужого проходит и сколько своего флагуется.

    Порог — не «настройка строгости», а точка на кривой обмена. Пока кривая не напечатана,
    любой разговор о его сдвиге — спор о вкусах."""
    from app.rag.pipeline import RELEVANCE_SOFT

    # ⚠ Косинусы приходят ГОТОВЫМИ из секций выше: пересчёт тех же 56 запросов через e5 — самая
    # медленная часть скрипта, и делать её дважды за прогон незачем.
    neg = [(c, c["dt1"]) for c in neg]
    pos = [(c, c["dt1"]) for c in pos]
    L = ["=" * 78,
         f"КРИВАЯ ОБМЕНА ПОРОГА (EV2) — {len(neg)} вне-719 против {len(pos)} пограничных in-scope",
         "=" * 78,
         "  порог | чужое прошло | своё зафлагано | сумма ошибок",
         "  ------|--------------|----------------|-------------"]
    curve = []
    for t in [round(0.78 + i * 0.005, 3) for i in range(25)]:
        fa = sum(1 for _c, d in neg if d >= t)          # вне-719 принят за профильный
        ff = sum(1 for _c, d in pos if d < t)           # профильный зафлагован как чужой
        mark = "  ← сейчас" if abs(t - RELEVANCE_SOFT) < 1e-9 else ""
        curve.append((t, fa, ff))
        L.append(f"  {t:.3f} | {fa:>4}/{len(neg):<8} | {ff:>4}/{len(pos):<10} | {fa + ff:>3}{mark}")

    # ⚠ Печатаем ПЛАТО, а не argmin. На сетке 0.005 с 56 кейсами равные суммы — норма, а не
    # исключение, и «первый минимум» всегда самый ПЕРМИССИВНЫЙ конец плато: прочитав его как
    # рекомендацию, порог понизят и добавят false-accept за несуществующий выигрыш.
    lo = min(fa + ff for _t, fa, ff in curve)
    plateau = [t for t, fa, ff in curve if fa + ff == lo]
    same = "" if len(plateau) == 1 else f" — ПЛАТО из {len(plateau)}: {plateau[0]:.3f}…{plateau[-1]:.3f}"
    L += ["",
          f"  Минимальная суммарная ошибка: {lo} из {len(neg) + len(pos)}{same}.",
          f"  Текущий порог {RELEVANCE_SOFT}: "
          + ("внутри минимума — двигать некуда." if RELEVANCE_SOFT in plateau
             else f"ВНЕ минимума, лучшие значения {plateau[0]:.3f}…{plateau[-1]:.3f}."),
          "  ⚠ Сумма ошибок — грубая мера: false-accept и ложный флаг НЕ равноценны. Флаг только",
          "     подсказывает правилу 1б, а решение «вне сферы» принимает модель — на пограничных",
          "     in-scope замер дал 10/20 флагов против 1/20 реальных отказов. Поэтому плато читать",
          "     вместе с `--check-refusal`, а из плато выбирать ВЕРХНИЙ конец: он строже к чужому",
          "     при той же цене.",
          ""]
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
    ap.add_argument("--mode", choices=["negative", "borderline", "threshold", "confusable", "both", "guard"],
                    default="both", help="guard = negative+borderline+threshold (обе стороны весов)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--rerank", action="store_true", help="confusable: применить продакшен-реранкер")
    ap.add_argument("--check-refusal", action="store_true",
                    help="negative/borderline: полный pipeline.answer (+DeepSeek)")
    ap.add_argument("--report", type=str, default="")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()

    try:
        from app.rag.retriever import _client
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}). Подними Docker + Qdrant (:6533).")

    out: list[str] = []
    neg_rows: list[dict] = []
    pos_rows: list[dict] = []
    # `both` = ВСЁ. Прежде он давал negative+confusable, то есть молча пропускал половину весов
    # guard'а — ту самую, без которой порог двигать нельзя.
    if args.mode in ("negative", "both", "guard"):
        text, neg_rows = run_negative(args.limit, args.check_refusal, not args.no_progress)
        out += text
    if args.mode in ("borderline", "both", "guard"):
        text, pos_rows = run_borderline(args.check_refusal, not args.no_progress)
        out += text
    if args.mode in ("threshold", "both", "guard"):
        if not (neg_rows and pos_rows):   # режим `threshold` в одиночку — считаем сами
            _, neg_rows = run_negative(args.limit, False, not args.no_progress)
            _, pos_rows = run_borderline(False, not args.no_progress)
        out += run_threshold(neg_rows, pos_rows)
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
