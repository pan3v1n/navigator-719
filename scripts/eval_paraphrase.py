"""P1 — paraphrase/robustness харнес ретрива навигатора 719.

Coverage sweep (`eval_coverage.py`) гонял ВЕРБАТИМ-имя позиции — это легче живого
косноязычия эксперта. Этот харнес меряет ГЛАВНЫЙ неизвестный: насколько проседает ретрив,
когда запрос — разговорный ПЕРЕФРАЗ, а не дословное наименование.

Метод:
  1. Стратифицированная выборка позиций (по N на раздел, seed фиксирован).
  2. DeepSeek генерит K разговорных перефразов на позицию (как предприятие описало бы продукт
     своими словами) — кэш в scripts/eval_paraphrases.json (повторный прогон бесплатен,
     перефразы можно глазами проверить на качество).
  3. Каждый перефраз → retriever.search (+ продакшен-реранкер на code-less) → ранг ВЕРНОЙ
     позиции (exact, по source_anchor) и ВЕРНОГО раздела (section_roman).
  4. Сравнение с вербатим-базлайном на ТЕХ ЖЕ позициях → ПРОСАДКА recall@1/@3 (exact и раздел).

Что важно: раздел-recall@3 — мера «нет увода» (сравнима со старым 46-golden); exact-recall —
строгая позиционная. Худшие провалы (раздел не в top-10) — реальные семантические дыры.

Запуск (нужен Qdrant + DEEPSEEK_API_KEY):
  .venv\\Scripts\\python scripts\\eval_paraphrase.py --per-section 5 --paraphrases 2
  .venv\\Scripts\\python scripts\\eval_paraphrase.py --report docs/eval_paraphrase_report.md
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.rag.retriever import search  # noqa: E402
from scripts.load_kb import load_records  # noqa: E402

CACHE = ROOT / "scripts" / "eval_paraphrases.json"

PARA_SYS = (
    "Ты — инженер промышленного предприятия. Тебе дают ОФИЦИАЛЬНОЕ наименование продукции из "
    "нормативного перечня (ПП РФ №719). Сформулируй, как предприятие описало бы ЭТУ ЖЕ продукцию "
    "своими словами в живом вопросе эксперту: разговорно, можно отраслевым жаргоном/синонимами, "
    "БЕЗ дословного копирования официального термина. Сохрани суть (что это за продукт). "
    'Верни СТРОГО JSON {"paraphrases": ["...", ...]} — ровно столько коротких разных формулировок, '
    "сколько просят (одна фраза каждая, без пояснений и кавычек внутри)."
)


def _rank(hits, pred) -> int | None:
    for i, h in enumerate(hits, 1):
        if pred(h):
            return i
    return None


# --------------------------------------------------------------------------- #
# Выборка
# --------------------------------------------------------------------------- #
def stratified(records: list[dict], per_section: int, seed: int) -> list[dict]:
    rnd = random.Random(seed)
    bysec: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if (r.get("product_name") or "").strip():
            bysec[r.get("section_roman", "?")].append(r)
    out: list[dict] = []
    for sec in sorted(bysec):
        pool = bysec[sec][:]
        rnd.shuffle(pool)
        out.extend(pool[:per_section])
    return out


# --------------------------------------------------------------------------- #
# Генерация перефразов (DeepSeek, с кэшем)
# --------------------------------------------------------------------------- #
def gen_paraphrases(recs: list[dict], k: int, progress: bool) -> dict[str, list[str]]:
    from app.rag.pipeline import _client

    cache: dict[str, list[str]] = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))

    need = [r for r in recs if r["source_anchor"] not in cache or len(cache[r["source_anchor"]]) < k]
    print(f"Перефразы: в кэше {len(recs) - len(need)}/{len(recs)}, генерим {len(need)}")
    if need:
        client = _client()
        it = need
        try:
            from tqdm import tqdm
            if progress:
                it = tqdm(need, unit="поз", desc="DeepSeek-перефраз")
        except ImportError:
            pass
        for r in it:
            name = r["product_name"]
            sect = r.get("section_title") or r.get("section_roman", "")
            user = f"Официальное наименование: «{name}». Область: {sect}. Нужно {k} формулировок."
            try:
                resp = client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[{"role": "system", "content": PARA_SYS},
                              {"role": "user", "content": user}],
                    temperature=0.7,
                    response_format={"type": "json_object"},
                    timeout=30,
                )
                arr = json.loads(resp.choices[0].message.content or "{}").get("paraphrases", [])
                cache[r["source_anchor"]] = [str(a).strip() for a in arr if str(a).strip()][:k]
            except Exception:  # noqa: BLE001 — сеть/JSON: пустой список, не валим прогон
                cache[r["source_anchor"]] = cache.get(r["source_anchor"], [])
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Кэш перефразов обновлён: {CACHE}")
    return cache


# --------------------------------------------------------------------------- #
# Замер
# --------------------------------------------------------------------------- #
def evaluate(recs, paraphrases, limit, rerank_on, progress):
    from app.rag.reranker import rerank

    verbatim = []   # по позиции: вербатим-ранги
    para = []       # по перефразу: ранги raw и reranked
    it = recs
    try:
        from tqdm import tqdm
        if progress:
            it = tqdm(recs, unit="поз", desc="ретрив")
    except ImportError:
        pass

    for r in it:
        anc, sec = r["source_anchor"], r.get("section_roman", "?")
        ex = lambda h: h.source_anchor == anc           # noqa: E731
        se = lambda h: h.section_roman == sec           # noqa: E731

        vb = search(r["product_name"], okpd2=None, limit=limit)
        verbatim.append({"sec": sec, "exact": _rank(vb, ex), "section": _rank(vb, se)})

        for q in paraphrases.get(anc, []):
            hits = search(q, okpd2=None, limit=limit)
            row = {"sec": sec, "name": r["product_name"], "query": q,
                   "raw_exact": _rank(hits, ex), "raw_section": _rank(hits, se),
                   "top1": (f"[{hits[0].section_roman}] {hits[0].product_name}" if hits else "—")}
            if rerank_on and row["raw_exact"] != 1:
                rr = rerank(q, hits)
                row["rr_exact"] = _rank(rr, ex)
                row["rr_section"] = _rank(rr, se)
            else:
                row["rr_exact"] = row["raw_exact"]
                row["rr_section"] = row["raw_section"]
            para.append(row)
    return verbatim, para


def _recall(rows, key, k) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if r[key] is not None and r[key] <= k) / len(rows)


def report(verbatim, para, limit, rerank_on, sample_cache) -> list[str]:
    L = ["=" * 80,
         f"P1 PARAPHRASE/ROBUSTNESS — {len(verbatim)} позиций, {len(para)} перефраз-запросов, top-{limit}",
         "=" * 80, ""]

    def line(tag, rows, ke, ks):
        L.append(f"  {tag:<34} exact: r@1={_recall(rows,ke,1):.3f} r@3={_recall(rows,ke,3):.3f}   "
                 f"раздел: r@1={_recall(rows,ks,1):.3f} r@3={_recall(rows,ks,3):.3f}")

    L.append("РЕТРИВ (exact = своя позиция; раздел = верный section_roman):")
    line("ВЕРБАТИМ (имя позиции)", verbatim, "exact", "section")
    line("ПЕРЕФРАЗ raw (гибрид)", para, "raw_exact", "raw_section")
    if rerank_on:
        line("ПЕРЕФРАЗ +реранкер (прод)", para, "rr_exact", "rr_section")
    L.append("")

    # ПРОСАДКА вербатим → перефраз (главная метрика робастности)
    dv_e1 = _recall(verbatim, "exact", 1) - _recall(para, "raw_exact", 1)
    dv_s3 = _recall(verbatim, "section", 3) - _recall(para, "raw_section", 3)
    L.append(f"ПРОСАДКА от перефраза (raw): exact r@1 −{dv_e1:.3f}   раздел r@3 −{dv_s3:.3f}")
    if rerank_on:
        L.append(f"  раздел r@3 после реранкера: {_recall(para,'rr_section',3):.3f} "
                 f"(увод остаётся в {sum(1 for r in para if not r['rr_section'] or r['rr_section']>3)} запросах)")
    L.append("")

    # худшие провалы: верный РАЗДЕЛ не в top-10 даже после реранкера (реальные семант. дыры)
    fails = [r for r in para if not (r.get("rr_section") if rerank_on else r["raw_section"])
             or (r.get("rr_section") if rerank_on else r["raw_section"]) > limit]
    L.append(f"СЕМАНТИЧЕСКИЕ ДЫРЫ (верный раздел не в top-{limit}): {len(fails)} из {len(para)}")
    for r in fails[:30]:
        L.append(f"  ✗ [{r['sec']:>4}] {r['name'][:34]:34} ← «{r['query'][:38]}»  top1: {r['top1'][:30]}")
    if len(fails) > 30:
        L.append(f"  … ещё {len(fails) - 30}")
    L.append("")

    # просадка по разделам (raw exact r@1)
    bysec = defaultdict(list)
    for r in para:
        bysec[r["sec"]].append(r)
    L.append("ПЕРЕФРАЗ raw exact r@1 ПО РАЗДЕЛАМ (слабые наверх):")
    sec_rows = sorted(((s, len(rs), _recall(rs, "raw_exact", 1), _recall(rs, "raw_section", 3))
                       for s, rs in bysec.items()), key=lambda x: x[2])
    for s, n, e1, s3 in sec_rows:
        L.append(f"  {s:>5} N={n:>3}  exact_r@1={e1:.2f}  раздел_r@3={s3:.2f}")
    L.append("")

    # QC: образцы перефразов
    L.append("QC — образцы сгенерированных перефразов:")
    shown = 0
    for r in para:
        if shown >= 8:
            break
        L.append(f"  [{r['sec']}] {r['name'][:34]} → «{r['query']}»")
        shown += 1
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="P1 paraphrase/robustness eval ретрива 719")
    ap.add_argument("--per-section", type=int, default=5, help="позиций на раздел в выборке")
    ap.add_argument("--paraphrases", type=int, default=2, help="перефразов на позицию (DeepSeek)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--no-rerank", action="store_true", help="не применять продакшен-реранкер")
    ap.add_argument("--report", type=str, default="")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()

    try:
        from app.rag.retriever import _client
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}). Подними Docker + Qdrant (:6533).")

    records = load_records()
    sample = stratified(records, args.per_section, args.seed)
    print(f"Выборка: {len(sample)} позиций (≤{args.per_section}/раздел), перефразов на позицию: {args.paraphrases}")

    paraphrases = gen_paraphrases(sample, args.paraphrases, progress=not args.no_progress)
    verbatim, para = evaluate(sample, paraphrases, args.limit,
                              rerank_on=not args.no_rerank, progress=not args.no_progress)
    out = report(verbatim, para, args.limit, rerank_on=not args.no_rerank, sample_cache=paraphrases)
    text = "\n".join(out)
    print("\n" + text)

    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# P1 paraphrase/robustness eval\n\n```\n" + text + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
