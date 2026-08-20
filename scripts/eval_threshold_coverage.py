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
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.rag import inheritance  # noqa: E402
from app.rag.thresholds import (  # noqa: E402
    lookup_procurement_threshold,
    lookup_threshold,
)

# ⚠ ПУТЬ ОТ КОРНЯ РЕПОЗИТОРИЯ, А НЕ ОТ CWD (ревью 20.08.2026). Относительный glob означал, что
# из любого каталога, кроме корня, приёмочный гейт МОЛЧА мерил ноль записей и печатал
# «✅ критерий ≤N %: 0.0 %» с кодом 0. Ровно та форма, что у `backup_db.py --verify` без имени
# файла: шаг отчитался успехом, не сделав ничего. Все прочие пути в файле уже строятся от
# `__file__` — только корпус не строился.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(_ROOT, "knowledge_base", "pp719", "structured", "*.json")
POINTS_TYPES = ("points", "mixed")

# ⚠ ПОРОГ БАЛЛОВ — ЭТО ПОРОГ В БАЛЛАХ (ревью 20.08.2026). Прежде «покрытой» считалась запись с
# ЛЮБОЙ непустой строкой порога, поэтому в покрытие попадали проценты локализации, счётные
# величины («не менее 7, с 1 января 2018 г. - не менее 8») и даже абзацы требований, утекавшие
# в строку порога из-за дефектов разбора. Замер: 116 из 529 «покрытых» записей не содержали
# слова «балл» вовсе, и доля без порога занижалась на 13 п.п. — 39.9 % вместо 53.1 %.
# Метрика, заведённая взамен непроверяемых «18.4 %», имела дефект того же класса.
_BALL_RE = re.compile(r"балл", re.IGNORECASE)


def _is_ball_threshold(value: str | None) -> bool:
    return bool(value) and bool(_BALL_RE.search(value))


def _items(path: str) -> list[dict]:
    """Записи-ПРОДУКТЫ раздела.

    ⚠ Псевдозаписи «методологические пороги раздела» (`section_methodology`) исключаются:
    их не считают ни `verify_structured --records`, ни `pipeline.target_hits`, и знаменатель
    1377 не совпадал ни с одним другим инструментом проекта (везде 1366). Метрика, у которой
    свой знаменатель, несравнима с остальными по определению.
    """
    data = json.load(open(path, encoding="utf-8"))
    items = data if isinstance(data, list) else (data.get("items") or data.get("positions") or [])
    return [r for r in items if (r.get("record_type") or "") != "section_methodology"]


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
            by_type[rtype]["total"] += 1
            if value is not None and not _is_ball_threshold(value):
                # Порог есть, но НЕ в баллах (проценты локализации, счётные величины).
                # Это данные, а не покрытие: на вопрос «сколько баллов нужно» они не отвечают.
                by_type[rtype]["non_ball"] += 1
                value = None
            else:
                by_type[rtype][src] += 1
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
    non_ball = sum(c["non_ball"] for c in by_type.values())
    missing_all = total - covered

    # ⚠ `by_type` — defaultdict, и обращение к отсутствующему типу его СОЗДАЁТ. Прежде это
    # добавляло пустой Counter, а печать потом делила на его нулевой `total` (ZeroDivisionError
    # ровно в том режиме, который запускает человек, — `--json` дефект маскировал).
    pts_total = sum(by_type[t]["total"] for t in POINTS_TYPES if t in by_type)
    pts_cov = sum(by_type[t]["own"] + by_type[t]["note"] + by_type[t]["inherited"]
                  for t in POINTS_TYPES if t in by_type)
    pts_missing = pts_total - pts_cov

    share_corpus = missing_all / total * 100 if total else 0.0
    share_points = pts_missing / pts_total * 100 if pts_total else 0.0

    if args.as_json:
        print(json.dumps({
            "total": total, "missing": missing_all, "share_corpus": round(share_corpus, 2),
            "points_total": pts_total, "points_missing": pts_missing,
            "share_points": round(share_points, 2), "procurement_only": proc_only,
            "non_ball": non_ball,
            "verdict": (None if args.max_share is None else
                        ((share_points if args.scope == "points" else share_corpus)
                         <= args.max_share)),
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
            share = miss / c["total"] * 100 if c["total"] else 0.0
            print(f"{rtype:16} {c['total']:6} {cov:10} {miss:11} {share:7.1f} %{mark}")
        print("-" * len(head))
        print(f"{'ВЕСЬ КОРПУС':16} {total:6} {covered:10} {missing_all:11} {share_corpus:7.1f} %")
        print(f"{'points + mixed':16} {pts_total:6} {pts_cov:10} {pts_missing:11} "
              f"{share_points:7.1f} %  ← приёмочный срез")

        src_totals = {s: sum(c[s] for c in by_type.values())
                      for s in ("own", "note", "inherited")}
        print("\nОткуда взялся порог: "
              + " · ".join(f"{k}={v}" for k, v in src_totals.items()))
        print(f"⚠ Только ЗАКУПОЧНЫЙ порог (общим не считается): {proc_only}")
        print(f"⚠ Порог есть, но НЕ в баллах (проценты, счётные величины): {non_ball}")

        if by_section:
            top = ", ".join(f"{s}:{n}" for s, n in by_section.most_common(6))
            print(f"Непокрытые по разделам (топ-6): {top}")
        for row in missing:
            print(f"   {row[0]:6} {row[1]:12} {row[2]:16} {row[3]}")

    if args.max_share is not None:
        share = share_points if args.scope == "points" else share_corpus
        label = "points+mixed" if args.scope == "points" else "весь корпус"
        ok = share <= args.max_share
        # ⚠ В машинном режиме вердикт НЕ дописываем строкой после JSON — это ломало разбор
        # документа («Extra data»). Код возврата его несёт, и он же лежит в поле `verdict`.
        if not args.as_json:
            print(f"\n{'✅' if ok else '❌'} критерий ≤{args.max_share:g} % по срезу «{label}»: "
                  f"{share:.1f} %")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
