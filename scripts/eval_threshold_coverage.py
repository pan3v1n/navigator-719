"""Покрытие корпуса ОБЩИМ порогом баллов — приёмочная метрика `K2` (#47).

Зачем отдельный скрипт. Доля «позиций без общего порога» служит критерием приёмки `K2`
(≤22 %), и по ней 19.08.2026 задача была принята с числом 18.4 %. Кода, который это число
считает, в репозитории не было ни одного: величина жила только в сообщении коммита и в
документах. Пересчитать её при следующей правке корпуса было нечем, а критерий приёмки,
который нельзя пересчитать, перестаёт быть критерием — он превращается в цитату.

Правило разрешения порога ПОВТОРЯЕТ РАНТАЙМ (`pipeline.format_context`, порядок важен):

1. `min_threshold` самой записи;
2. иначе — разбор примечаний по коду и наименованию (`thresholds.lookup_threshold`);
3. иначе — порог ГРУППЫ через наследование (`inheritance.lookup`), и только для записи,
   у которой нет СВОИХ требований: в рантайме родитель подтягивается ровно тогда, когда
   собственный список операций пуст.

⚠ Закупочный порог общим НЕ считается. Примечания «Для целей осуществления закупок …»
задают порог другой природы; до правки 19.08.2026 рантайм их не различал, и 57 позиций
отвечали закупочным порогом на вопрос о подтверждении происхождения. Такие записи
считаются здесь как «без общего порога» и выводятся отдельной строкой — иначе метрика
снова начнёт засчитывать чужой порог за свой.

⚠ У метрики НЕПОСТОЯННЫЙ ЗНАМЕНАТЕЛЬ, поэтому печатаются оба среза:
* по всему корпусу — сколько записей вообще не могут назвать порог;
* по `points`/`mixed` — сколько НЕ могут там, где порог обязан быть по модели требований.
Второй срез — содержательный: у записи с моделью `operations` порога не существует
в самом приложении, и считать её «непокрытой» значит штрафовать корпус за первоисточник.

Запуск:
    .venv\\Scripts\\python.exe scripts/eval_threshold_coverage.py
    .venv\\Scripts\\python.exe scripts/eval_threshold_coverage.py --max-share 22 --scope points
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.rag import inheritance  # noqa: E402
from app.rag.thresholds import (  # noqa: E402
    lookup_procurement_threshold,
    lookup_threshold,
)

CORPUS = "knowledge_base/pp719/structured/*.json"
POINTS_TYPES = ("points", "mixed")


def _items(path: str) -> list[dict]:
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("positions") or []


def _has_own_requirements(rec: dict) -> bool:
    """Есть ли у записи СВОИ требования — то же условие, при котором рантайм НЕ идёт к родителю.

    R6: блок без `operations`, но с текстом в `component` — это тоже требование.
    """
    for block in rec.get("requirement_blocks") or []:
        if block.get("operations") or (block.get("component") or "").strip():
            return True
    return False


def resolve(rec: dict) -> tuple[str | None, str]:
    """→ (порог, источник). Источник: own | note | inherited | procurement_only | none."""
    codes = rec.get("okpd2_codes") or []
    name = rec.get("product_name") or ""
    section = rec.get("section_roman")

    own = (rec.get("min_threshold") or "").strip()
    if own:
        return own, "own"

    # ⚠ `requirement_blocks[].min_threshold` — порог УЗЛА изделия (D9), рантайм печатает его
    # отдельной строкой «Порог узла» и НЕ подставляет вместо общего (`pipeline` строит `mt`
    # только из `min_threshold` позиции и примечаний). Засчитывать его за общий значило бы
    # завысить покрытие: у позиции с порогом узла общего порога может не быть вовсе.

    note = lookup_threshold(codes, name, section)
    if note:
        return note, "note"

    if not _has_own_requirements(rec):
        parent = inheritance.lookup(section, name)
        if parent and (parent.get("min_threshold") or "").strip():
            return parent["min_threshold"], "inherited"

    if lookup_procurement_threshold(codes, name, section):
        return None, "procurement_only"
    return None, "none"


def main() -> int:
    ap = argparse.ArgumentParser(description="Покрытие корпуса общим порогом баллов (K2 #47)")
    ap.add_argument("--max-share", type=float, default=None,
                    help="критерий приёмки в процентах; при превышении код выхода 1")
    ap.add_argument("--scope", choices=("corpus", "points"), default="points",
                    help="срез, к которому применяется --max-share (по умолчанию points+mixed)")
    ap.add_argument("--json", dest="as_json", action="store_true", help="машинный вывод")
    ap.add_argument("--list-missing", type=int, default=0,
                    help="напечатать N непокрытых записей (диагностика)")
    args = ap.parse_args()

    by_type: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_section: collections.Counter = collections.Counter()
    missing: list[tuple[str, str, str, str]] = []

    for path in sorted(glob.glob(CORPUS)):
        for rec in _items(path):
            rtype = rec.get("requirement_type") or "(нет типа)"
            value, src = resolve(rec)
            by_type[rtype][src] += 1
            by_type[rtype]["total"] += 1
            if value is None:
                by_section[rec.get("section_roman") or "?"] += 1
                if len(missing) < max(args.list_missing, 0):
                    codes = rec.get("okpd2_codes") or []
                    missing.append((rec.get("section_roman") or "?", rtype,
                                    codes[0] if codes else "—",
                                    (rec.get("product_name") or "")[:60]))

    total = sum(c["total"] for c in by_type.values())
    covered = sum(c["own"] + c["note"] + c["inherited"] for c in by_type.values())
    proc_only = sum(c["procurement_only"] for c in by_type.values())
    missing_all = total - covered

    pts_total = sum(by_type[t]["total"] for t in POINTS_TYPES)
    pts_cov = sum(by_type[t]["own"] + by_type[t]["note"] + by_type[t]["inherited"]
                  for t in POINTS_TYPES)
    pts_missing = pts_total - pts_cov

    share_corpus = missing_all / total * 100 if total else 0.0
    share_points = pts_missing / pts_total * 100 if pts_total else 0.0

    if args.as_json:
        print(json.dumps({
            "total": total, "missing": missing_all, "share_corpus": round(share_corpus, 2),
            "points_total": pts_total, "points_missing": pts_missing,
            "share_points": round(share_points, 2), "procurement_only": proc_only,
            "by_source": {s: sum(c[s] for c in by_type.values())
                          for s in ("own", "note", "inherited",
                                    "procurement_only", "none")},
        }, ensure_ascii=False, indent=2))
    else:
        print("=== Покрытие ОБЩИМ порогом (K2 #47) ===\n")
        head = f"{'тип требований':16} {'всего':>6} {'с порогом':>10} {'БЕЗ порога':>11} {'доля':>8}"
        print(head)
        print("-" * len(head))
        for rtype in sorted(by_type, key=lambda t: -by_type[t]["total"]):
            c = by_type[rtype]
            cov = c["own"] + c["note"] + c["inherited"]
            miss = c["total"] - cov
            mark = " ←" if rtype in POINTS_TYPES else ""
            print(f"{rtype:16} {c['total']:6} {cov:10} {miss:11} "
                  f"{miss / c['total'] * 100:7.1f} %{mark}")
        print("-" * len(head))
        print(f"{'ВЕСЬ КОРПУС':16} {total:6} {covered:10} {missing_all:11} {share_corpus:7.1f} %")
        print(f"{'points + mixed':16} {pts_total:6} {pts_cov:10} {pts_missing:11} "
              f"{share_points:7.1f} %  ← приёмочный срез")

        src_totals = {s: sum(c[s] for c in by_type.values())
                      for s in ("own", "note", "inherited")}
        print("\nОткуда взялся порог: "
              + " · ".join(f"{k}={v}" for k, v in src_totals.items()))
        print(f"⚠ Только ЗАКУПОЧНЫЙ порог (общим не считается): {proc_only}")

        if by_section:
            top = ", ".join(f"{s}:{n}" for s, n in by_section.most_common(6))
            print(f"Непокрытые по разделам (топ-6): {top}")
        for row in missing:
            print(f"   {row[0]:6} {row[1]:12} {row[2]:16} {row[3]}")

    if args.max_share is not None:
        share = share_points if args.scope == "points" else share_corpus
        label = "points+mixed" if args.scope == "points" else "весь корпус"
        ok = share <= args.max_share
        print(f"\n{'✅' if ok else '❌'} критерий ≤{args.max_share:g} % по срезу «{label}»: "
              f"{share:.1f} %")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
