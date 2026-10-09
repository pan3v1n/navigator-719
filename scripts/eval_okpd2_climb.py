"""D14 #141: радиус правки иерархии ОКПД2 на ЖИВОМ ретриве — до и после. Без LLM, нужен Qdrant.

Статический замер (`diag_okpd2_climb.py`) отвечает, КАКИЕ позиции связаны с кодом по иерархии.
Этот — что ВЫБИРАЕТ рантайм: какая позиция становится целевой (`pipeline.target_hits` поверх
`retriever.search`), то есть о какой позиции пойдёт ответ и чьи требования попадут в контекст.

Три популяции (вопрос — «<наименование> <код>», как пишет пользователь):
  own      — каждая позиция корпуса СВОИМ первым кодом. Своя позиция обязана быть целевой и до, и
             после; это и есть популяция вреда правки (точное совпадение обязано побеждать групповое);
  climb    — коды справочника, у которых по поразрядной иерархии появляется БОЛЕЕ ЧАСТНАЯ позиция-
             предок (из `diag_okpd2_climb`, наименование — официальное из справочника). Ожидание —
             ближайшие предки, посчитанные НЕЗАВИСИМО от `app/`;
  control  — случайная (seed) выборка прочих кодов тех же классов: у них иерархия не меняется, и
             любое изменение целевой здесь — побочный эффект правки, который надо объяснить.

Запуск (сначала на коде ДО правки, затем ПОСЛЕ):
  .venv\\Scripts\\python scripts/eval_okpd2_climb.py --out before.json
  .venv\\Scripts\\python scripts/eval_okpd2_climb.py --out after.json
  .venv\\Scripts\\python scripts/eval_okpd2_climb.py --compare before.json after.json
⚠ Сравнение печатает «починено / сломано / изменено» по каждой популяции; «сломано» обязано быть 0.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.console import enable_utf8  # noqa: E402

enable_utf8()
from scripts import diag_okpd2_climb as diag  # noqa: E402

CONTROL_N = 600
SEED = 719


def populations() -> dict[str, list[dict]]:
    recs = diag.load_records()
    names = diag.load_classifier()
    rec_codes = [(i, c) for i, r in enumerate(recs) for c in (r.get("okpd2_codes") or [])]
    by_class: dict[str, list[tuple[int, str]]] = {}
    for i, c in rec_codes:
        by_class.setdefault(diag.key(c)[:2], []).append((i, c))

    own = [{"code": r["okpd2_codes"][0], "query": f"{r['product_name'][:150]} {r['okpd2_codes'][0]}",
            "expect": [r["source_anchor"]]}
           for r in recs if r.get("okpd2_codes") and r.get("record_type") != "section_methodology"]

    def closest(q: str, rule) -> list[str]:
        best, who = -1, set()
        for i, c in by_class.get(diag.key(q)[:2], []):
            if rule(c, q) and diag.key(q).startswith(diag.key(c)):
                d = len(diag.key(c))
                if d > best:
                    best, who = d, {recs[i]["source_anchor"]}
                elif d == best:
                    who.add(recs[i]["source_anchor"])
        return sorted(who)

    climb, rest = [], []
    for q, nm in names.items():
        if diag.key(q)[:2] not in by_class:
            continue
        old, new = closest(q, diag.old_related), closest(q, diag.new_related)
        row = {"code": q, "query": f"{nm[:150]} {q}", "expect": new}
        if new and new != old:
            climb.append(row)
        else:
            rest.append(row)
    random.Random(SEED).shuffle(rest)
    return {"own": own, "climb": climb, "control": rest[:CONTROL_N]}


def run(out: Path) -> None:
    from app.rag.pipeline import target_hits
    from app.rag.retriever import search

    pops = populations()
    res: dict[str, list[dict]] = {}
    t0 = time.time()
    for name, rows in pops.items():
        res[name] = []
        for k, row in enumerate(rows, 1):
            hits = search(row["query"], okpd2=row["code"], limit=8)
            tg = target_hits(hits, [row["code"]])
            res[name].append({**row, "targets": [h.source_anchor for h in tg],
                              "window": [h.source_anchor for h in hits],
                              "matched": [h.source_anchor for h in hits if h.okpd2_match]})
            if k % 200 == 0:
                print(f"  {name}: {k}/{len(rows)} · {time.time() - t0:.0f} с", flush=True)
        print(f"{name}: {len(rows)}", flush=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"сохранено: {out}")


def compare(before: Path, after: Path) -> int:
    a = json.loads(before.read_text(encoding="utf-8"))
    b = json.loads(after.read_text(encoding="utf-8"))
    broken_total = 0
    for name in ("own", "climb", "control"):
        rows_a = {r["code"] + "|" + r["query"]: r for r in a.get(name, [])}
        rows_b = {r["code"] + "|" + r["query"]: r for r in b.get(name, [])}
        fixed = broken = changed = ok_both = bad_both = 0
        examples: dict[str, list[str]] = {"сломано": [], "изменено": [], "починено": []}
        for k, ra in rows_a.items():
            rb = rows_b.get(k)
            if rb is None:
                continue
            exp = set(ra["expect"])
            hit_a = bool(exp & set(ra["targets"]))
            hit_b = bool(exp & set(rb["targets"]))
            if hit_a and not hit_b:
                broken += 1
                examples["сломано"].append(f"{ra['code']} {ra['targets']} → {rb['targets']}")
            elif hit_b and not hit_a:
                fixed += 1
                examples["починено"].append(f"{ra['code']} {ra['targets'][:1]} → {rb['targets'][:1]}")
            elif hit_a:
                ok_both += 1
            else:
                bad_both += 1
            if ra["targets"] != rb["targets"] and hit_a == hit_b:
                changed += 1
                examples["изменено"].append(f"{ra['code']} {ra['targets']} → {rb['targets']}")
        broken_total += broken
        print(f"\n{name}: всего {len(rows_a)} · ожидаемая целевая до {ok_both + broken}"
              f" → после {ok_both + fixed} · починено {fixed} · сломано {broken} · "
              f"целевая сменилась без смены вердикта {changed} · мимо и до, и после {bad_both}")
        for kind, ex in examples.items():
            for e in ex[:8]:
                print(f"   {kind}: {e}")
    return 1 if broken_total else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args()
    if args.compare:
        return compare(*args.compare)
    if not args.out:
        ap.error("нужен --out или --compare")
    from app.rag import retriever
    retriever.EXACT_SEARCH = True   # M1: замеру нужна повторяемость, а не скорость
    run(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
