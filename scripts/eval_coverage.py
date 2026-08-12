"""Coverage sweep + PRODUCT-LEVEL retrieval eval (P0 батареи тестов движка).

В отличие от `eval_retrieval.py` (мерит попадание в РАЗДЕЛ на 46 авторских кейсах), этот
харнес — self-retrieval по ВСЕЙ базе на гранулярности ПОЗИЦИИ:

  для каждой записи приложения 719 запрашиваем её же (именем продукта; и отдельно кодом
  ОКПД2) и проверяем, возвращается ли ИМЕННО ЭТА позиция в top-K. Идентичность —
  уникальный `source_anchor` (для всех 1357 записей он непуст и уникален).

Зачем (см. memory navigator-719-test-battery):
  • recall@1 0.98 из eval_retrieval меряет РАЗДЕЛ (29 шт.), а не ПОЗИЦИЮ (1347 шт.) —
    угадать раздел на порядок легче. Реальный product-level recall был неизвестен.
  • sweep по ВСЕЙ базе вскрывает то, что выборка из 46 не видит: позиции-невидимки
    (их не находит ничто), коллизии (чужая позиция перебивает свою), слабые разделы.

Детерминированно и бесплатно (без DeepSeek; реранкер не трогаем — он в pipeline, не в
retriever.search). Нужен поднятый Qdrant с коллекцией pp719.

Запуск:
  .venv\\Scripts\\python scripts\\eval_coverage.py                       # полный sweep
  .venv\\Scripts\\python scripts\\eval_coverage.py --records 60          # быстрый смоук
  .venv\\Scripts\\python scripts\\eval_coverage.py --sweep name          # только по имени
  .venv\\Scripts\\python scripts\\eval_coverage.py --report docs/eval_coverage_report.md
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag import retriever  # noqa: E402
from app.rag.retriever import search  # noqa: E402
from scripts.load_kb import load_records  # noqa: E402  (тот же набор записей, что в индексе)


def _first_code(rec: dict) -> str | None:
    codes = rec.get("okpd2_codes") or []
    return codes[0] if codes else None


def _rank_of(hits, anchor: str) -> int | None:
    """1-based ранг хита с нужным source_anchor, иначе None (нет в top-K)."""
    for i, h in enumerate(hits, 1):
        if h.source_anchor == anchor:
            return i
    return None


def sweep(records: list[dict], limit: int, by_code: bool, progress: bool):
    """Прогон self-retrieval. Возвращает список строк-результатов по каждой записи."""
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    rows = []
    it = records
    if progress and tqdm:
        it = tqdm(records, unit="зап", desc="code" if by_code else "name")

    for rec in it:
        anchor = rec.get("source_anchor")
        name = (rec.get("product_name") or "").strip()
        code = _first_code(rec)

        if by_code:
            if not code:
                continue
            hits = search(code, okpd2=code, limit=limit)  # тест ОКПД2-буста/подстраховки
            rank = _rank_of(hits, anchor)
            resolved = any(h.okpd2_match for h in hits)  # код вообще разрешился в ветку
            rows.append({
                "anchor": anchor, "name": name, "section": rec.get("section_roman", "?"),
                "code": code, "rank": rank, "resolved": resolved,
                "top1": (hits[0].product_name if hits else ""),
                "top1_sec": (hits[0].section_roman if hits else ""),
            })
        else:
            if not name:
                continue
            hits = search(name, okpd2=None, limit=limit)  # честный семантика+BM25, без буста
            rank = _rank_of(hits, anchor)
            rows.append({
                "anchor": anchor, "name": name, "section": rec.get("section_roman", "?"),
                "code": code or "", "rank": rank,
                "top1": (hits[0].product_name if hits else ""),
                "top1_sec": (hits[0].section_roman if hits else ""),
                "top1_anchor": (hits[0].source_anchor if hits else None),
            })
    return rows


def _recall_at(rows, k: int) -> float:
    found = sum(1 for r in rows if r["rank"] is not None and r["rank"] <= k)
    return found / len(rows) if rows else 0.0


def _mrr(rows) -> float:
    return statistics.mean((1.0 / r["rank"] if r["rank"] else 0.0) for r in rows) if rows else 0.0


def classify_collisions(rows):
    """Коллизии (своя позиция найдена, но не top-1) → (все, внутрисекционные, кросс-секционные).

    Внутрисекционные = top-1 в ТОМ ЖЕ разделе (доброкачественные сиблинги-варианты, эксперт
    всё равно получает верное семейство). Кросс-секционные = top-1 в ДРУГОМ разделе — только
    они могут реально увести ответ; это actionable-набор и мишень для реранкера."""
    coll = [r for r in rows if r["rank"] is not None and r["rank"] > 1]
    same = [r for r in coll if r["section"] == r["top1_sec"]]
    cross = [r for r in coll if r["section"] != r["top1_sec"]]
    return coll, same, cross


def measure_rerank_lift(cross_rows, limit: int):
    """LLM-реранкер на кросс-секционных коллизиях: сколько top-1 он вытягивает.

    Это замер lift реранкера на РЕАЛЬНЫХ коллизиях по всей базе (а не на 46 golden). Реранкер
    может починить только коллизии, где своя позиция ЕСТЬ в top-{limit} (его кандидатах);
    невидимки (rank None) он не достаёт. +N вызовов DeepSeek (N = число кросс-коллизий)."""
    from app.rag.reranker import rerank

    before1 = after1 = lifted = regressed = 0
    details = []
    try:
        from tqdm import tqdm
        it = tqdm(cross_rows, unit="зап", desc="rerank")
    except ImportError:
        it = cross_rows
    for r in it:
        hits = search(r["name"], okpd2=None, limit=limit)
        before = _rank_of(hits, r["anchor"])
        after = _rank_of(rerank(r["name"], hits), r["anchor"])
        before1 += before == 1
        after1 += after == 1
        if before != 1 and after == 1:
            lifted += 1
        if before == 1 and after != 1:
            regressed += 1
        details.append((r, before, after))
    return {"n": len(cross_rows), "before1": before1, "after1": after1,
            "lifted": lifted, "regressed": regressed, "details": details}


def report_name(rows, limit: int) -> list[str]:
    L = []
    L.append("=" * 78)
    L.append(f"NAME sweep (запрос = product_name, okpd2=None) — {len(rows)} позиций, top-{limit}")
    L.append("=" * 78)
    L.append("PRODUCT-LEVEL ретрив (эталон = source_anchor самой позиции):")
    # M1: заголовочная метрика — recall@1. Замечание владельца 12.08.2026: эксперт не читает
    # десять кандидатов, он читает ответ, поэтому recall@10 = 1.000 — метрика для галочки.
    # Глубокие recall оставлены СПРАВОЧНО, чтобы видеть, «не нашли» или «нашли, но не первым».
    L.append(f"  recall@1 = {_recall_at(rows,1):.3f}   ← ГЛАВНАЯ: ответ строится вокруг top-1")
    L.append(f"  MRR      = {_mrr(rows):.3f}")
    L.append(f"  справочно: recall@3 = {_recall_at(rows,3):.3f}  recall@5 = {_recall_at(rows,5):.3f}"
             f"  recall@{limit} = {_recall_at(rows,limit):.3f} — глубина, которую пользователь не читает")
    L.append("")
    # M1: без этой оговорки число занижает продакшен. Реранкер живёт в pipeline, а не в
    # retriever.search, и включается на запросах БЕЗ совпадения по коду ОКПД2 — то есть ровно
    # там, где верная позиция стоит на ранге 2. Замер его вклада: 1/12 → 9/12 на
    # кросс-секционных коллизиях (`--rerank-collisions`).
    L.append("⚠ Замер БЕЗ LLM-реранкера: он в pipeline, а не в retriever.search. В проде он "
             "поднимает ранг-2 случаи,")
    L.append("  поэтому фактический продакшен-recall@1 ВЫШЕ измеренного здесь.")
    L.append("")

    # позиции-невидимки: своя позиция не попала даже в top-K
    invisible = [r for r in rows if r["rank"] is None]
    L.append(f"ПОЗИЦИИ-НЕВИДИМКИ (своя позиция не в top-{limit}): {len(invisible)}")
    for r in invisible[:40]:
        L.append(f"  ✗ [{r['section']:>4}] {r['name'][:48]:48}  → top1 перебил: "
                 f"[{r['top1_sec']}] {r['top1'][:32]}")
    if len(invisible) > 40:
        L.append(f"  … ещё {len(invisible) - 40}")
    L.append("")

    # коллизии: позиция нашлась, но НЕ на 1-м месте — делим на доброкач. сиблингов и риск
    collided, same, cross = classify_collisions(rows)
    L.append(f"КОЛЛИЗИИ (своя позиция найдена, но не top-1): {len(collided)}")
    L.append(f"  • внутрисекционные (доброкачественные сиблинги-варианты, верное семейство): {len(same)}")
    L.append(f"  • КРОСС-СЕКЦИОННЫЕ (top-1 в ДРУГОМ разделе — риск увода): {len(cross)}")
    L.append("")
    L.append(f"  Actionable — все {len(cross)} кросс-секционных коллизий (худшие по рангу сверху):")
    for r in sorted(cross, key=lambda r: -r["rank"]):
        L.append(f"    ранг {r['rank']:>2} [{r['section']:>4}] {r['name'][:40]:40} "
                 f"← top1 [{r['top1_sec']:>4}] {r['top1'][:34]}")
    L.append("")

    # покрытие по разделам — где движок слаб
    bysec = defaultdict(list)
    for r in rows:
        bysec[r["section"]].append(r)
    L.append("RECALL@1 ПО РАЗДЕЛАМ (слабые — наверх):")
    L.append(f"  {'разд':>5} {'N':>5} {'r@1':>6} {'r@'+str(limit):>6}")
    sec_rows = [(s, len(rs),
                 sum(1 for r in rs if r["rank"] == 1) / len(rs),
                 sum(1 for r in rs if r["rank"] and r["rank"] <= limit) / len(rs))
                for s, rs in bysec.items()]
    for s, n, r1, rk in sorted(sec_rows, key=lambda x: x[2]):
        L.append(f"  {s:>5} {n:>5} {r1:>6.2f} {rk:>6.2f}")
    L.append("")
    return L


def report_code(rows, limit: int) -> list[str]:
    L = []
    L.append("=" * 78)
    L.append(f"CODE sweep (запрос = код ОКПД2, okpd2=код) — {len(rows)} позиций с кодом, top-{limit}")
    L.append("=" * 78)
    resolved = sum(1 for r in rows if r["resolved"])
    L.append("Целостность ОКПД2-машинерии (буст + подстраховка по префиксам):")
    L.append(f"  код разрешился в свою ветку (есть okpd2_match): {resolved}/{len(rows)} "
             f"({resolved/len(rows):.3f})")
    L.append(f"  ТОЧНАЯ позиция в top-{limit}: {_recall_at(rows,limit):.3f}  "
             f"(низкое — НОРМА: один код делят несколько позиций)")
    L.append("")
    # код НЕ разрешился — это реальный дефект машинерии
    broken = [r for r in rows if not r["resolved"]]
    if broken:
        L.append(f"⚠ КОД НЕ РАЗРЕШИЛСЯ В СВОЮ ВЕТКУ (дефект буста/фильтра): {len(broken)}")
        for r in broken[:30]:
            L.append(f"  ✗ [{r['section']:>4}] код {r['code']:14} {r['name'][:40]}")
        if len(broken) > 30:
            L.append(f"  … ещё {len(broken) - 30}")
    else:
        L.append("✓ Все коды разрешились в свою ветку — машинерия ОКПД2 цела по всей базе.")
    L.append("")
    return L


def rerank_recall3(name_rows, limit: int):
    """Продакшен-конфиг: реранкер на ВСЕХ code-less кандидатах не-top-1 (rank≥2) → full-base
    recall@1/@3 ДО и ПОСЛЕ. rank-1 считаем стабильными (на 24 кросс-коллизиях 0 регрессий).
    Возвращает (строки отчёта, остаточные промахи recall@3 = после реранка rank>3)."""
    from app.rag.reranker import rerank
    try:
        from tqdm import tqdm
        wrap = lambda xs: tqdm(xs, unit="зап", desc="rerank3")
    except ImportError:
        wrap = lambda xs: xs

    cands = [r for r in name_rows if r["rank"] is not None and r["rank"] >= 2]
    newrank: dict = {}
    for r in wrap(cands):
        hits = search(r["name"], okpd2=None, limit=limit)
        newrank[r["anchor"]] = _rank_of(rerank(r["name"], hits), r["anchor"])

    n = len(name_rows) or 1

    def at(k: int, prod: bool) -> float:
        c = 0
        for r in name_rows:
            rk = newrank.get(r["anchor"], r["rank"]) if prod else r["rank"]
            if rk and rk <= k:
                c += 1
        return c / n

    L = ["=" * 78,
         f"ПРОДАКШЕН-КОНФИГ (реранкер на code-less; {len(cands)} кандидатов рангом ≥2) — full-base",
         "=" * 78,
         f"  recall@1: ДО {at(1, False):.3f}  →  ПОСЛЕ {at(1, True):.3f}",
         f"  recall@3: ДО {at(3, False):.3f}  →  ПОСЛЕ {at(3, True):.3f}",
         ""]

    resid = []
    for r in name_rows:
        rk = newrank.get(r["anchor"], r["rank"])
        if not rk or rk > 3:
            resid.append((r, rk))
    L.append(f"ОСТАТОЧНЫЕ ПРОМАХИ recall@3 (после реранка rank>3 или не в top-{limit}): {len(resid)}")
    for r, rk in sorted(resid, key=lambda x: (x[1] or 99)):
        cls = "сиблинг (своя секция)" if r["section"] == r["top1_sec"] else f"КРОСС → {r['top1_sec']}"
        L.append(f"  rank {str(rk):>3} [{r['section']:>4}] {r['name'][:38]:38} {cls}")
    L.append("")
    return L, resid


def repeat_block(records, limit: int, times: int, first_rows, progress: bool) -> list[str]:
    """M1: разброс recall@1 по повторным прогонам — и почему он вообще есть.

    Одиночный прогон не воспроизводим: слияние RRF поверх 8 сегментов коллекции разрешает ничьи
    между почти равными кандидатами непредсказуемо. Точный поиск (`exact=true`) эту часть НЕ
    лечит — проверено 12.08.2026: три прогона подряд дали 0.940 / 0.946 / 0.943.

    Разброс оказался БОЛЬШЕ разниц, по которым принимались решения, поэтому одно число тут врёт
    самой своей точностью. Печатаем среднее и границы: их и надо цитировать."""
    vals = [_recall_at(first_rows, 1)]
    for i in range(times - 1):
        if progress:
            print(f"  повтор {i + 2}/{times}…")
        vals.append(_recall_at(sweep(records, limit, by_code=False, progress=False), 1))
    lo, hi = min(vals), max(vals)
    mean = sum(vals) / len(vals)
    return [
        "",
        f"РАЗБРОС recall@1 по {times} прогонам: среднее {mean:.3f}, диапазон {lo:.3f}–{hi:.3f} "
        f"(±{(hi - lo) / 2:.3f})",
        "  Причина: слияние RRF по сегментам разрешает ничьи непредсказуемо; точный поиск это не",
        "  лечит. Цитировать нужно среднее с разбросом, а не одно число — иначе различия меньше",
        f"  ±{(hi - lo) / 2:.3f} будут выглядеть значимыми, не являясь таковыми.",
        "",
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description="Coverage sweep + product-level eval ретрива 719")
    ap.add_argument("--limit", type=int, default=10, help="глубина выдачи top-K")
    ap.add_argument("--records", type=int, default=0, help="ограничить число записей (смоук); 0 = все")
    ap.add_argument("--sweep", choices=["name", "code", "both"], default="both")
    ap.add_argument("--rerank-collisions", action="store_true",
                    help="прогнать LLM-реранкер на кросс-секционных коллизиях и замерить lift (+DeepSeek)")
    ap.add_argument("--rerank3", action="store_true",
                    help="продакшен-конфиг: реранкер на всём хвосте rank≥2, full-base recall@3 до/после (+DeepSeek)")
    ap.add_argument("--report", type=str, default="", help="путь для markdown-отчёта")
    ap.add_argument("--no-progress", action="store_true")
    # M1: по умолчанию замер идёт ТОЧНЫМ поиском — иначе число «плавает» между прогонами
    # (12.08.2026: четыре прогона одной команды дали 0.933/0.937/0.941/0.941 при разбросе
    # ±0.005, что БОЛЬШЕ разниц, по которым принимались решения). `--approx` возвращает
    # приближённый HNSW — то, как ищет продакшен; тогда число читать как оценку, а не как факт.
    ap.add_argument("--approx", action="store_true",
                    help="мерить приближённым HNSW (как в проде) вместо точного перебора")
    # M1: одиночный прогон НЕ воспроизводим — слияние RRF по 8 сегментам разрешает ничьи
    # непредсказуемо, и точный поиск это НЕ лечит (проверено: 0.940/0.946/0.943). Поэтому
    # заголовочное число берём как среднее по нескольким прогонам и печатаем разброс.
    ap.add_argument("--repeat", type=int, default=3,
                    help="сколько раз повторить NAME sweep для оценки разброса (по умолчанию 3)")
    args = ap.parse_args()
    retriever.EXACT_SEARCH = not args.approx

    try:
        from app.rag.retriever import _client
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}).\nПодними Docker Desktop + контейнер Qdrant (:6533).")

    records = load_records()
    if args.records:
        records = records[: args.records]
    print(f"Записей в sweep: {len(records)}")

    out: list[str] = []
    name_rows = None
    if args.sweep in ("name", "both"):
        name_rows = sweep(records, args.limit, by_code=False, progress=not args.no_progress)
        out += report_name(name_rows, args.limit)
        if args.repeat > 1:
            out += repeat_block(records, args.limit, args.repeat, name_rows,
                                progress=not args.no_progress)
    if args.sweep in ("code", "both"):
        rows = sweep(records, args.limit, by_code=True, progress=not args.no_progress)
        out += report_code(rows, args.limit)

    if args.rerank3 and name_rows is not None:
        print("\nПродакшен-реранк на хвосте rank≥2…")
        block, _ = rerank_recall3(name_rows, args.limit)
        out += block

    if args.rerank_collisions and name_rows is not None:
        _, _, cross = classify_collisions(name_rows)
        print(f"\nРеранкер на {len(cross)} кросс-секционных коллизиях…")
        res = measure_rerank_lift(cross, args.limit)
        out.append("=" * 78)
        out.append(f"LLM-РЕРАНКЕР на кросс-секционных коллизиях — {res['n']} запросов (code-less)")
        out.append("=" * 78)
        out.append(f"  recall@1 на подмножестве: ДО {res['before1']}/{res['n']} "
                   f"({res['before1']/max(res['n'],1):.2f})  →  "
                   f"ПОСЛЕ {res['after1']}/{res['n']} ({res['after1']/max(res['n'],1):.2f})")
        out.append(f"  поднял в top-1: +{res['lifted']}   регрессий: -{res['regressed']}")
        out.append("")
        out.append("  Детализация (ранг ДО→ПОСЛЕ):")
        for r, b, a in sorted(res["details"], key=lambda x: (x[2] or 99)):
            mark = "✓ починил" if (b != 1 and a == 1) else ("✗ регресс" if (b == 1 and a != 1) else "")
            out.append(f"    [{r['section']:>4}] {r['name'][:40]:40} {str(b):>3}→{str(a):>3}  {mark}")
        out.append("")

    text = "\n".join(out)
    print("\n" + text)

    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# Coverage sweep + product-level eval (P0)\n\n```\n" + text + "\n```\n",
                        encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
