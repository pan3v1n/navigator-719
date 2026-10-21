"""Перепарс раздела ПОСЛЕ новой редакции: модель видит только изменённые позиции.

ЗАЧЕМ. `structure_kb.py --section X` перегенерирует раздел целиком, а LLM недетерминирована:
неизменные позиции получили бы ДРУГОЙ разбор («модель переписала бы иначе 36 из 47» — урок
актуализации 29.09, раздел IV). Радиус правки тогда — весь раздел, и потери прячутся среди
улучшений. Здесь позиция, чей исходный блок (`Product.raw_block`) в новом чанке ПОБАЙТНО тот же,
что в старом, получает свою прежнюю запись; модель зовётся только для новых и изменённых.

КАК СОПОСТАВЛЯЕТСЯ СТАРАЯ ЗАПИСЬ. Номер позиции в `source_anchor` у старых JSON не годится:
у разделов, разобранных прежней версией парсера, нумерация разошлась с нынешней (см.
`restore_source_names.py`). Поэтому старый кандидат `parse_section` → запись по КОДАМ (точное
множество) и близости имени; неоднозначное и слабое сопоставление — отказ, а не догадка.
Одна оставшаяся пара «кандидат ↔ запись с теми же кодами» сводится, если она единственная.

ЯКОРЯ (`--anchors`):
  renumber — у всех записей раздела номер позиции = нынешний разбор (раздел, где позиции
             вставлены и исключены массово). Карта «старый якорь → новый» пишется рядом
             (`--map-out`): по ней переводятся ссылки в наборах замеров и тестах.
  keep     — точечная заплатка: JSON остаётся как есть, меняются только записи позиций,
             чей исходник изменился. Пара «старая позиция ↔ новая» ищется по ИСХОДНЫМ
             ячейкам (коды + имя из таблицы), запись наследует якорь. Изменилось только
             наименование, а ячейки требований те же, — имя берётся из ячейки источника,
             модель не зовётся. Новая позиция без пары — отказ (её место — renumber).

Запуск (из корня; старый чанк берётся из git):
  .venv/Scripts/python.exe scripts/restructure_changed.py VII --anchors renumber            # план
  .venv/Scripts/python.exe scripts/restructure_changed.py VII --anchors renumber --write    # + DeepSeek
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import structure_kb as sk  # noqa: E402
from app.core.console import enable_utf8  # noqa: E402

ANCHOR_POS_RE = re.compile(r"позиция (\d+)$")
NAME_MIN = 0.6     # ниже — имя записи не похоже на ячейку источника: не сопоставляем
NAME_GAP = 0.05    # два кандидата ближе этого — неоднозначно: не сопоставляем


def _norm(s: str) -> str:
    return " ".join(s.replace(" ", " ").split()).lower()


def json_indent(text: str) -> int:
    """Отступ, которым записан JSON раздела: в репозитории есть и 1, и 2 (XI, XXI — 1; VII,
    XVIII — 2). Чужой отступ превращает правку одной позиции в дифф на весь файл."""
    second = text.split("\n", 2)[1]
    return len(second) - len(second.lstrip(" ")) or 2


def git_text(rev: str, path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    out = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=ROOT, capture_output=True, check=True)
    return out.stdout.decode("utf-8").replace("\r\n", "\n")


def match_records(products: list, records: list[dict]) -> dict[int, int]:
    """Кандидат старого разбора (индекс) → старая запись (индекс). Только однозначные пары."""
    pairs: dict[int, int] = {}
    for i, p in enumerate(products):
        cands = [j for j, r in enumerate(records) if sorted(r["okpd2_codes"]) == sorted(p.okpd2_codes)]
        scored = sorted(
            ((difflib.SequenceMatcher(None, _norm(records[j]["product_name"]), _norm(p.name_raw)).ratio(), j)
             for j in cands), reverse=True)
        if not scored or scored[0][0] < NAME_MIN:
            continue
        if len(scored) > 1 and scored[1][0] > scored[0][0] - NAME_GAP:
            continue
        pairs[i] = scored[0][1]
    # Остаток: ровно один несопоставленный кандидат и ровно одна свободная запись с теми же кодами.
    used = set(pairs.values())
    for i, p in enumerate(products):
        if i in pairs:
            continue
        free = [j for j, r in enumerate(records) if j not in used
                and sorted(r["okpd2_codes"]) == sorted(p.okpd2_codes)]
        rivals = [k for k, q in enumerate(products) if k not in pairs
                  and sorted(q.okpd2_codes) == sorted(p.okpd2_codes)]
        if len(free) == 1 and rivals == [i]:
            pairs[i] = free[0]
            used.add(free[0])
    js = list(pairs.values())
    assert len(js) == len(set(js)), "одна запись сопоставлена двум кандидатам"
    return pairs


def plan(roman: str, rev: str, anchors: str):
    chunk = sk.find_chunk(roman, None)
    new_text = chunk.read_text(encoding="utf-8")
    old_text = git_text(rev, chunk)
    out_path = sk.OUT_DIR / sk.out_name(chunk)
    old_json = git_text(rev, out_path)
    old_records = json.loads(old_json)
    header, new = sk.parse_section(new_text)
    _, old = sk.parse_section(old_text)
    title = sk.section_title(header, chunk.stem)

    products_recs = [j for j, r in enumerate(old_records) if r.get("record_type") != "section_methodology"]
    recs = [old_records[j] for j in products_recs]
    pairs = match_records(old, recs)
    unmatched_old = [old[i].name_raw.strip()[:70] for i in range(len(old)) if i not in pairs]
    if unmatched_old:
        sys.exit(f"старые кандидаты без записи ({len(unmatched_old)}): {unmatched_old[:5]} — "
                 "сопоставление ненадёжно, остановка")

    by_raw: dict[str, int] = {}
    for i, p in enumerate(old):
        by_raw.setdefault(p.raw_block(), i)
    # Изменённая позиция (keep): прежняя запись — по тем же правилам, но среди НЕиспользованных.
    reused_old: set[int] = set()
    steps = []   # (product, "reuse"|"llm", old_record_index|None)
    for p in new:
        i = by_raw.get(p.raw_block())
        if i is not None and i not in reused_old:
            reused_old.add(i)
            steps.append((p, "reuse", pairs[i]))
        else:
            steps.append((p, "llm", None))
    # Изменённая позиция → её прежняя запись (тем же сопоставлением, среди неиспользованных).
    # keep: обязательна — оттуда берётся якорь; renumber: только для карты якорей и отчёта.
    rest = [k for k in range(len(old)) if k not in reused_old]
    rest_recs = [recs[pairs[k]] for k in rest]
    llm_prods = [p for p, kind, _ in steps if kind == "llm"]
    sub = match_records(llm_prods, rest_recs)
    it = iter(range(len(llm_prods)))
    for n, (p, kind, _) in enumerate(steps):
        if kind != "llm":
            continue
        k = next(it)
        if k in sub:
            steps[n] = (p, "llm", pairs[rest[sub[k]]])
        elif anchors == "keep":
            sys.exit(f"--anchors keep: у новой позиции «{p.name_raw.strip()[:60]}» нет прежней записи")
    dropped = sorted(set(range(len(recs))) - {j for _, _, j in steps if j is not None})
    return chunk, title, out_path, old_records, recs, steps, dropped, new_text, json_indent(old_json)


def clean_name(p) -> str:
    """Наименование из ячейки источника: построчно, без сносок (`restore_source_names`), и без
    неразрывных пробелов — в именах корпуса их нет ни одного (замер 21.10.2026: 0 из 1583)."""
    from restore_source_names import source_name
    return source_name(p.name_raw).replace(" ", " ")


def run_patch(roman: str, rev: str, write: bool, workers: int, max_chars: int) -> None:
    """--anchors keep: JSON раздела как есть + заплатки на позиции с изменённым исходником."""
    chunk = sk.find_chunk(roman, None)
    new_text = chunk.read_text(encoding="utf-8")
    out_path = sk.OUT_DIR / sk.out_name(chunk)
    old_json = git_text(rev, out_path)
    records = json.loads(old_json)
    header, new = sk.parse_section(new_text)
    _, old = sk.parse_section(git_text(rev, chunk))
    title = sk.section_title(header, chunk.stem)

    old_raw = {p.raw_block() for p in old}
    new_raw = {p.raw_block() for p in new}
    changed = [p for p in new if p.raw_block() not in old_raw]
    gone = [p for p in old if p.raw_block() not in new_raw]
    # Пара по исходным ячейкам: тот же набор кодов, ближайшее имя (однозначно). Близость — доля
    # КОРОТКОГО имени, совпавшая с длинным: редакция чаще ДОПИСЫВАЕТ оговорку («Матрасы, кроме
    # матрасных основ (за исключением продукции … раздела VII …)»), и обычный ratio падает ниже порога.
    def near(a: str, b: str) -> float:
        a, b = _norm(a), _norm(b)
        if not a or not b:
            return 0.0
        hit = sum(m.size for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
        return hit / min(len(a), len(b))

    pairs: dict[int, int] = {}
    for i, p in enumerate(changed):
        scored = sorted(
            ((near(q.name_raw, p.name_raw), k)
             for k, q in enumerate(gone) if sorted(q.okpd2_codes) == sorted(p.okpd2_codes)), reverse=True)
        if scored and scored[0][0] >= NAME_MIN and (len(scored) == 1 or scored[1][0] < scored[0][0] - NAME_GAP):
            pairs[i] = scored[0][1]
    assert len(set(pairs.values())) == len(pairs), "одна старая позиция в паре с двумя новыми"
    unpaired = [p.name_raw.strip()[:60] for i, p in enumerate(changed) if i not in pairs]
    if unpaired:
        sys.exit(f"новые позиции без пары ({len(unpaired)}): {unpaired} — это раздел для --anchors renumber")
    # Старая позиция → её запись.
    plain = [j for j, r in enumerate(records) if r.get("record_type") != "section_methodology"]
    rec_of = match_records(gone, [records[j] for j in plain])
    lost = [q.name_raw.strip()[:60] for k, q in enumerate(gone) if k not in rec_of]
    if lost:
        sys.exit(f"у изменённых старых позиций не найдена запись: {lost}")
    jobs = []   # (record index, new product, "rename"|"llm")
    for i, p in enumerate(changed):
        q = gone[pairs[i]]
        j = plain[rec_of[pairs[i]]]
        same_reqs = [c.strip() for c in q.component_cells] == [c.strip() for c in p.component_cells]
        jobs.append((j, p, "rename" if same_reqs else "llm"))
    removed = [plain[rec_of[k]] for k in range(len(gone)) if k not in set(pairs.values())]

    print(f"Раздел {roman}: записей {len(records)}; изменено позиций {len(jobs)}, исключено {len(removed)}")
    for j, p, kind in jobs:
        print(f"  [{kind}] {records[j]['source_anchor'].rsplit(', ', 1)[-1]}: "
              f"{records[j]['product_name'][:50]!r} → {clean_name(p)[:70]!r}")
    for j in removed:
        print(f"  [удалить] {records[j]['source_anchor'].rsplit(', ', 1)[-1]}: {records[j]['product_name'][:70]}")
    if not write:
        print("\n(план: DeepSeek не вызывался, ничего не записано — повтори с --write)")
        return

    usage = sk.Usage()
    llm = [(j, p) for j, p, kind in jobs if kind == "llm"]
    fresh = {}
    if llm:
        client = sk.make_client()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {j: ex.submit(sk.normalize_product, client, p, roman, title, usage, max_chars) for j, p in llm}
            fresh = {j: f.result() for j, f in futs.items()}
    out = copy.deepcopy(records)
    for j, p, kind in jobs:
        if kind == "llm":
            rec = fresh[j]
            rec["source_anchor"] = records[j]["source_anchor"]
        else:
            rec = out[j]
            rec["product_name"] = clean_name(p)
            rec["okpd2_codes"] = p.okpd2_codes
        rec["amendments"] = p.amendments
        out[j] = rec
    out = [r for j, r in enumerate(out) if j not in set(removed)]
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=json_indent(old_json)), encoding="utf-8")
    print(f"\nЗаписано: {out_path.relative_to(ROOT)} ({len(out)} записей); вызовов API {usage.calls}, "
          f"≈ ¥{usage.cost_usd() * sk.USD_TO_CNY:.2f}")


def main() -> None:
    enable_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("section", help="римский номер раздела, напр. VII")
    ap.add_argument("--anchors", choices=("renumber", "keep"), required=True)
    ap.add_argument("--rev", default="HEAD", help="ревизия git со СТАРЫМИ чанком и JSON")
    ap.add_argument("--write", action="store_true", help="позвать DeepSeek и записать JSON")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-chars", type=int, default=9000)
    ap.add_argument("--map-out", help="куда записать карту «старый якорь → новый» (JSON)")
    args = ap.parse_args()
    roman = args.section
    if args.anchors == "keep":
        run_patch(roman, args.rev, args.write, args.workers, args.max_chars)
        return

    chunk, title, out_path, old_records, recs, steps, dropped, new_text, indent = plan(roman, args.rev, args.anchors)
    n_reuse = sum(k == "reuse" for _, k, _ in steps)
    print(f"Раздел {roman}: позиций сейчас {len(steps)}, было записей {len(recs)}")
    print(f"  прежняя запись (исходник побайтно тот же): {n_reuse}")
    print(f"  в модель (новые и изменённые):            {len(steps) - n_reuse}")
    for p, kind, j in steps:
        if kind == "llm":
            was = f" ← {recs[j]['source_anchor'].rsplit(', ', 1)[-1]}" if j is not None else ""
            print(f"    [{p.position}] {p.name_raw.strip()[:70]}{was}")
    print(f"  записей больше нет (позиция исключена или не узнана): {len(dropped)}")
    for j in dropped:
        print(f"    − {recs[j]['source_anchor'].rsplit(', ', 1)[-1]}: {recs[j]['product_name'][:70]}")

    if not args.write:
        print("\n(план: DeepSeek не вызывался, ничего не записано — повтори с --write)")
        return

    client = sk.make_client()
    usage = sk.Usage()
    llm_idx = [n for n, (_, k, _) in enumerate(steps) if k == "llm"]
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {n: ex.submit(sk.normalize_product, client, steps[n][0], roman, title, usage, args.max_chars)
                for n in llm_idx}
        fresh = {n: f.result() for n, f in futs.items()}

    out, amap = [], {}
    for n, (p, kind, j) in enumerate(steps):
        # Прежняя запись — целиком, включая поправки: исходник позиции не изменился, а поправки
        # разбора у старых JSON собраны другой версией парсера (смена их — шум, а не правка).
        if kind == "reuse":
            rec = copy.deepcopy(recs[j])
        else:
            rec = fresh[n]
            rec["amendments"] = p.amendments
        new_anchor = f"Приложение к ПП №719, Раздел {roman}, позиция {p.position}"
        rec["source_anchor"] = new_anchor
        if j is not None:
            amap[recs[j]["source_anchor"]] = new_anchor
        out.append(rec)
    anchors_all = [r["source_anchor"] for r in out]
    assert len(anchors_all) == len(set(anchors_all)), "дубли source_anchor"
    if any(r.get("record_type") == "section_methodology" for r in old_records):
        metho = sk.collect_section_thresholds(new_text)
        if metho:
            out.append(sk.methodology_record(roman, title, metho))
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=indent), encoding="utf-8")
    if args.map_out:
        for j in dropped:
            amap[recs[j]["source_anchor"]] = None
        Path(args.map_out).write_text(json.dumps(amap, ensure_ascii=False, indent=2), encoding="utf-8")
    cost = usage.cost_usd()
    print(f"\nЗаписано: {out_path.relative_to(ROOT)} ({len(out)} записей); вызовов API {usage.calls}, "
          f"≈ ¥{cost * sk.USD_TO_CNY:.2f}")


if __name__ == "__main__":
    main()
