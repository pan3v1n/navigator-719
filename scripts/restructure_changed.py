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

СМЕНА ТОЛЬКО КОДА (оба режима, #212): исходный блок (имя + требования) побайтно тот же, а ячейка
кода другая (сценарий ревью PR #209: суда 30.11.33.190 → .191–.197). Запись переиспользуется с
НОВЫМИ кодами и поправками, модель не зовётся. Только однозначная пара: блок встречается ровно у
одной новой и ровно у одной старой позиции из ещё не сопоставленных; примечание записи, назвавшее
старый код, — повод отдать позицию модели (`notes_cite_old_code`). ⚠ На реальных переходах 2026 г.
ветка не сработала ни разу: у судов N 1002 сменилось и имя (снята сноска «<9>»), — её держат тесты.

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


def json_indent(text: str) -> int | None:
    """Отступ, которым записан JSON раздела: в репозитории есть и 1, и 2 (XI, XXI — 1; VII,
    XVIII — 2). Чужой отступ превращает правку одной позиции в дифф на весь файл.
    Однострочный файл → None (`json.dumps` без отступа, как он и записан)."""
    lines = text.replace("\r\n", "\n").split("\n", 2)
    if len(lines) < 2:
        return None
    return len(lines[1]) - len(lines[1].lstrip(" "))


def reuse_key(p) -> str:
    """Ключ «позиция в новой редакции та же»: ячейка КОДА и исходный блок (имя + требования).
    ⚠ Ревью PR #209: по одному `raw_block` позиция, которой редакция сменила только код (сценарий:
    суда 30.11.33.190 → .191–.197), переиспользовалась бы со СТАРЫМИ кодами — а по коду
    идут поиск, ОКПД2-буст и пороги."""
    return f"{p.okpd2_field.strip()}\n{p.raw_block()}"


def git_text(rev: str, path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    out = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=ROOT, capture_output=True, check=True)
    return out.stdout.decode("utf-8").replace("\r\n", "\n")


def match_records(products: list, records: list[dict], contested: set[int] | None = None) -> dict[int, int]:
    """Кандидат старого разбора (индекс) → старая запись (индекс). Только однозначные пары.
    `contested` (если передан) получает записи, на которые претендовали несколько кандидатов без
    явного лидера: отказ это или нет, решает вызывающий."""
    best: dict[int, tuple[int, float]] = {}
    for i, p in enumerate(products):
        cands = [j for j, r in enumerate(records) if sorted(r["okpd2_codes"]) == sorted(p.okpd2_codes)]
        scored = sorted(
            ((difflib.SequenceMatcher(None, _norm(records[j]["product_name"]), _norm(p.name_raw)).ratio(), j)
             for j in cands), reverse=True)
        if not scored or scored[0][0] < NAME_MIN:
            continue
        if len(scored) > 1 and scored[1][0] > scored[0][0] - NAME_GAP:
            continue
        best[i] = (scored[0][1], scored[0][0])
    # Одна запись у нескольких кандидатов (#212; прежде — голый `assert` ниже, и на XVIII ред. 29.09,
    # 187 → 344 позиций, скрипт падал трассой): достаётся лидеру, опередившему остальных на
    # `NAME_GAP`, без лидера — никому. ⚠ «Никому» без условия (первая редакция правки) отдавало в
    # `dropped` и явного победителя: XVIII «Грунтозаборные устройства…» — 0.695 против 0.633 и 0.622,
    # а карта якорей объявляла позицию исключённой (ревью #212).
    claims: dict[int, list[tuple[float, int]]] = {}
    for i, (j, score) in best.items():
        claims.setdefault(j, []).append((score, i))
    pairs: dict[int, int] = {}
    for j, cl in claims.items():
        cl.sort(reverse=True)
        if len(cl) == 1 or cl[0][0] - cl[1][0] >= NAME_GAP:
            pairs[cl[0][1]] = j
        elif contested is not None:
            contested.add(j)
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


def recode_pairs(new: list, old: list, skip_new=frozenset(), skip_old=frozenset()) -> dict[int, int]:
    """Новая позиция (индекс) → старая, у которых исходный блок побайтно тот же, а ячейка кода
    другая: редакция сменила ТОЛЬКО код. Неоднозначный блок (у двух новых или двух старых) —
    не пара: здесь догадка подменила бы коды чужой позиции."""
    nb: dict[str, list[int]] = {}
    ob: dict[str, list[int]] = {}
    for i, p in enumerate(new):
        if i not in skip_new:
            nb.setdefault(p.raw_block(), []).append(i)
    for k, q in enumerate(old):
        if k not in skip_old:
            ob.setdefault(q.raw_block(), []).append(k)
    return {ns[0]: ob[b][0] for b, ns in nb.items()
            if len(ns) == 1 and len(ob.get(b, [])) == 1
            and new[ns[0]].okpd2_field.strip() != old[ob[b][0]].okpd2_field.strip()}


def notes_cite_old_code(rec: dict, p) -> bool:
    """Примечание записи называет код, которого у позиции больше нет (ревью #212): «… под общим
    кодом «из 20.59.56»» осталось бы в `notes` при новом 20.59.57, а `notes` идёт в sparse-текст
    (`load_kb.build_text`) — старый код жил бы токеном BM25. Такую запись не переиспользуем: пара
    остаётся (якорь и карта верны), а разбирает позицию модель."""
    notes = rec.get("notes") or ""
    return any(re.search(rf"(?<![\d.]){re.escape(c)}(?![\d.])", notes)
               for c in rec.get("okpd2_codes") or [] if c not in p.okpd2_codes)


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
    steps, dropped = plan_steps(new, old, recs, anchors)
    return chunk, title, out_path, old_records, recs, steps, dropped, new_text, json_indent(old_json)


def plan_steps(new: list, old: list, recs: list[dict], anchors: str):
    """Шаги перепарса по разобранным кандидатам — ядро `plan` без файлов и git.
    Шаг — (кандидат, "reuse"|"recode"|"llm", индекс прежней записи|None)."""
    pairs = match_records(old, recs)
    unmatched_old = [old[i].name_raw.strip()[:70] for i in range(len(old)) if i not in pairs]
    if unmatched_old:
        sys.exit(f"старые кандидаты без записи ({len(unmatched_old)}): {unmatched_old[:5]} — "
                 "сопоставление ненадёжно, остановка")

    by_raw: dict[str, int] = {}
    for i, p in enumerate(old):
        by_raw.setdefault(reuse_key(p), i)
    # Изменённая позиция (keep): прежняя запись — по тем же правилам, но среди НЕиспользованных.
    reused_old: set[int] = set()
    steps = []   # (product, "reuse"|"recode"|"llm", old_record_index|None)
    for p in new:
        i = by_raw.get(reuse_key(p))
        if i is not None and i not in reused_old:
            reused_old.add(i)
            steps.append((p, "reuse", pairs[i]))
        else:
            steps.append((p, "llm", None))
    # Смена только кода: прежняя запись с новыми кодами. Без этой ветки позиция уходила в модель,
    # а её запись — в `dropped`, и карта якорей объявляла её исключённой (#212).
    taken_new = {n for n, (_, kind, _) in enumerate(steps) if kind != "llm"}
    for n, i in recode_pairs(new, old, skip_new=taken_new, skip_old=reused_old).items():
        reused_old.add(i)
        steps[n] = (new[n], "llm" if notes_cite_old_code(recs[pairs[i]], new[n]) else "recode", pairs[i])
    # Изменённая позиция → её прежняя запись (тем же сопоставлением, среди неиспользованных).
    # keep: обязательна — оттуда берётся якорь; renumber: только для карты якорей и отчёта.
    rest = [k for k in range(len(old)) if k not in reused_old]
    rest_recs = [recs[pairs[k]] for k in rest]
    llm_prods = [p for p, kind, j in steps if kind == "llm" and j is None]
    contested: set[int] = set()
    sub = match_records(llm_prods, rest_recs, contested)
    if contested and anchors == "renumber":
        # Иначе запись ушла бы в `dropped`, а карта якорей объявила бы живую позицию исключённой.
        sys.exit("записи, на которые претендуют несколько новых позиций без явного лидера: "
                 f"{[rest_recs[k]['product_name'][:50] for k in sorted(contested)]} — карта якорей была "
                 "бы неверной, остановка")
    it = iter(range(len(llm_prods)))
    for n, (p, kind, j) in enumerate(steps):
        if kind != "llm" or j is not None:
            continue
        k = next(it)
        if k in sub:
            steps[n] = (p, "llm", pairs[rest[sub[k]]])
        elif anchors == "keep":
            sys.exit(f"--anchors keep: у новой позиции «{p.name_raw.strip()[:60]}» нет прежней записи")
    dropped = sorted(set(range(len(recs))) - {j for _, _, j in steps if j is not None})
    return steps, dropped


def renumber_records(steps: list, recs: list[dict], fresh: dict, roman: str) -> tuple[list[dict], dict]:
    """Записи раздела в нынешней нумерации + карта «старый якорь → новый» (без методички)."""
    out, amap = [], {}
    for n, (p, kind, j) in enumerate(steps):
        # Прежняя запись — целиком, включая поправки: исходник позиции не изменился, а поправки
        # разбора у старых JSON собраны другой версией парсера (смена их — шум, а не правка).
        if kind == "reuse":
            rec = copy.deepcopy(recs[j])
        elif kind == "recode":   # смена кода — сама поправка редакции: коды и поправки новые
            rec = copy.deepcopy(recs[j])
            rec["okpd2_codes"] = p.okpd2_codes
            rec["amendments"] = p.amendments
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
    return out, amap


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
    jobs, removed = patch_jobs(new, old, records)

    print(f"Раздел {roman}: записей {len(records)}; изменено позиций {len(jobs)}, исключено {len(removed)}")
    for j, p, kind in jobs:
        print(f"  [{kind}] {records[j]['source_anchor'].rsplit(', ', 1)[-1]}: "
              f"{records[j]['product_name'][:50]!r} → {clean_name(p)[:70]!r}"
              + (f" | коды {records[j]['okpd2_codes']} → {p.okpd2_codes}" if kind == "recode" else ""))
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
    out = patch_records(records, jobs, removed, fresh)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=json_indent(old_json)), encoding="utf-8")
    print(f"\nЗаписано: {out_path.relative_to(ROOT)} ({len(out)} записей); вызовов API {usage.calls}, "
          f"≈ ¥{usage.cost_usd() * sk.USD_TO_CNY:.2f}")


def patch_jobs(new: list, old: list, records: list[dict]) -> tuple[list, list[int]]:
    """--anchors keep без файлов и git: задания (индекс записи, новый кандидат,
    "recode"|"rename"|"llm") и индексы записей исключённых позиций."""
    old_raw = {reuse_key(p) for p in old}
    new_raw = {reuse_key(p) for p in new}
    changed = [p for p in new if reuse_key(p) not in old_raw]
    gone = [p for p in old if reuse_key(p) not in new_raw]
    # Смена только кода — пара по исходному блоку; до правки #212 здесь был `sys.exit`
    # с советом «это раздел для --anchors renumber».
    recoded = recode_pairs(changed, gone)
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
    taken = set(recoded.values())
    for i, p in enumerate(changed):
        if i in recoded:
            continue
        scored = sorted(
            ((near(q.name_raw, p.name_raw), k)
             for k, q in enumerate(gone)
             if k not in taken and sorted(q.okpd2_codes) == sorted(p.okpd2_codes)), reverse=True)
        if scored and scored[0][0] >= NAME_MIN and (len(scored) == 1 or scored[1][0] < scored[0][0] - NAME_GAP):
            pairs[i] = scored[0][1]
    twice = sorted({k for k in pairs.values() if list(pairs.values()).count(k) > 1})
    if twice:   # ревью #212: голый `assert` падал трассой (IV ред. 22.07 → 29.09, XVIII)
        sys.exit(f"старая позиция в паре с несколькими новыми: {[gone[k].name_raw.strip()[:50] for k in twice]}"
                 " — это раздел для --anchors renumber")
    unpaired = [p.name_raw.strip()[:60] for i, p in enumerate(changed) if i not in pairs and i not in recoded]
    if unpaired:
        sys.exit(f"новые позиции без пары ({len(unpaired)}): {unpaired} — это раздел для --anchors renumber")
    # Старая позиция → её запись.
    plain = [j for j, r in enumerate(records) if r.get("record_type") != "section_methodology"]
    rec_of = match_records(gone, [records[j] for j in plain])
    lost = [q.name_raw.strip()[:60] for k, q in enumerate(gone) if k not in rec_of]
    if lost:
        sys.exit(f"у изменённых старых позиций не найдена запись: {lost}")
    jobs = []   # (record index, new product, "recode"|"rename"|"llm")
    for i, p in enumerate(changed):
        if i in recoded:
            j = plain[rec_of[recoded[i]]]
            jobs.append((j, p, "llm" if notes_cite_old_code(records[j], p) else "recode"))
            continue
        q = gone[pairs[i]]
        j = plain[rec_of[pairs[i]]]
        same_reqs = [c.strip() for c in q.component_cells] == [c.strip() for c in p.component_cells]
        jobs.append((j, p, "rename" if same_reqs else "llm"))
    paired = set(pairs.values()) | taken
    removed = [plain[rec_of[k]] for k in range(len(gone)) if k not in paired]
    return jobs, removed


def patch_records(records: list[dict], jobs: list, removed: list[int], fresh: dict) -> list[dict]:
    """JSON раздела с заплатками: `rename` — имя из ячейки и коды, `recode` — только коды (имя
    записи уже сверено с той же ячейкой), `llm` — свежий разбор под прежним якорем."""
    out = copy.deepcopy(records)
    for j, p, kind in jobs:
        if kind == "llm":
            rec = fresh[j]
            rec["source_anchor"] = records[j]["source_anchor"]
        else:
            rec = out[j]
            if kind == "rename":
                rec["product_name"] = clean_name(p)
            rec["okpd2_codes"] = p.okpd2_codes
        rec["amendments"] = p.amendments
        out[j] = rec
    return [r for j, r in enumerate(out) if j not in set(removed)]


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
    n_recode = sum(k == "recode" for _, k, _ in steps)
    print(f"Раздел {roman}: позиций сейчас {len(steps)}, было записей {len(recs)}")
    print(f"  прежняя запись (исходник побайтно тот же): {n_reuse}")
    print(f"  прежняя запись с новыми кодами:            {n_recode}")
    for p, kind, j in steps:
        if kind == "recode":
            print(f"    [{p.position}] {p.name_raw.strip()[:50]}: {recs[j]['okpd2_codes']} → {p.okpd2_codes}")
    print(f"  в модель (новые и изменённые):            {len(steps) - n_reuse - n_recode}")
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

    usage = sk.Usage()
    llm_idx = [n for n, (_, k, _) in enumerate(steps) if k == "llm"]
    fresh = {}
    if llm_idx:
        client = sk.make_client()
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {n: ex.submit(sk.normalize_product, client, steps[n][0], roman, title, usage, args.max_chars)
                    for n in llm_idx}
            fresh = {n: f.result() for n, f in futs.items()}

    out, amap = renumber_records(steps, recs, fresh, roman)
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
