r"""Классификация позиций БЕЗ порога в баллах — `A1` (#56), разблокирует приёмку `K2` (#47).

ЗАЧЕМ. Метрика `K2` (`eval_threshold_coverage.py`) отвечает «сколько позиций не могут назвать
порог» — 53.1 % по срезу `points`+`mixed`. Но эта доля складывает ДВА разных явления, и потому
не даёт починить ни одно:

    порог рядом ЕСТЬ, привязать не смогли   → наш дефект, чинится кодом
    порога НЕТ ни в разделе, ни в примечаниях → свойство первоисточника, уходит эксперту (#95)

Критерий приёмки вешается только на первую половину. 20.08.2026 её оценили двумя грубыми
детекторами — 16.6 % и 31.6 %; прежний критерий ≤22 % лёг ВНУТРЬ полосы, то есть по нему нельзя
было сказать даже, выполнен ли он. ⚠ Ни один из этих детекторов в репозитории не сохранился:
оба числа снова жили только в документах. Этот скрипт делает классификацию воспроизводимой и
поимённой — каждая позиция получает класс И УЛИКУ (примечание, строка, дословная цитата).

ПРАВИЛО ПОКРЫТИЯ НЕ ДУБЛИРУЕТСЯ. `resolve` и `_is_ball_threshold` импортируются из
`eval_threshold_coverage` — иначе два инструмента разойдутся молча, а проект это уже проходил
(«кто целевой» решалось в четырёх местах; оракул гейта разошёлся с кодом, который судит).

КЛАССЫ (в порядке разбора, каждая позиция попадает ровно в один):

  own_non_ball        у записи ЕСТЬ свой порог, но не в баллах — счётные величины («не менее 9
                      из следующих операций») или проценты локализации. НЕ дефект привязки:
                      первоисточник задал позиции другую модель. ⚠ Вопрос к типу требований,
                      а не к порогу.
  note_non_ball       порог добран из примечания/группы, но тоже не в баллах.
  procurement_only    единственный найденный порог — закупочный («Для целей осуществления
                      закупок»). Общего порога у позиции нет; показывать закупочный за общий
                      нельзя — это дефект, который `K2` уже закрыла.
  defect_unattached   ⚠ НАШ ДЕФЕКТ. Порога нет, но в примечаниях есть строка с порогом В БАЛЛАХ,
                      чей код покрывает код позиции (то же направление, что в рантайме).
                      Значит порог написан для этой позиции, а привязка не сработала —
                      как правило из-за неоднозначности, которую `_lookup` не стал угадывать.
  defect_name_match   ⚠ НАШ ДЕФЕКТ. Порога нет, кода в примечании нет, но наименование строки
                      примечания совпадает с наименованием позиции дословно.
  inline_in_record    порог написан ВНУТРИ текста требования («оцениваемых в совокупности
                      суммарным количеством баллов, составляющим не менее 96 баллов») и не
                      вынесен в поле. Дефект РАЗБОРА, но другого рода — правится парсером.
  component_threshold порог в тексте требования относится к КОМПОНЕНТУ, оцениваемому по другому
                      разделу («шасси … операций, установленных разделом II … не менее 1700
                      баллов»). Не порог позиции.
  note_other_product  под кодом позиции в примечаниях порог ЕСТЬ, но написан для другой
                      продукции («Турбины на водяном паре» ← строка «Главная энергетическая
                      установка» прим. 17 про судостроение). Не дефект: рантайм отказывается
                      верно, привязка дала бы правильное число не той позиции.
  narrower_note       в примечаниях есть порог для БОЛЕЕ УЗКОГО кода, чем код позиции. Улика
                      слабая: порог написан для части того, что покрывает позиция, и переносить
                      его вверх по иерархии нельзя — решает эксперт.
  no_source           улик нет: ни в примечаниях, ни внутри записи. Порога нет в первоисточнике
                      → эксперту (#95).

⚠ СКРИПТ НИЧЕГО НЕ ПРИВЯЗЫВАЕТ. Он только классифицирует и печатает улику. Автоматически
подставлять найденный порог нельзя: «правильное число, привязанное не к той позиции» — самый
дорогой класс дефекта в этом проекте, и именно так 57 позиций однажды получили закупочный порог
вместо общего.

ЗАПУСК (Qdrant и DeepSeek НЕ нужны, читается только корпус):
    .venv\Scripts\python.exe scripts\classify_missing_thresholds.py
    .venv\Scripts\python.exe scripts\classify_missing_thresholds.py --json > out.json
    .venv\Scripts\python.exe scripts\classify_missing_thresholds.py --show defect_unattached
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

from app.rag.thresholds import (  # noqa: E402
    _code_applies,
    _name_overlap,
    _flat_thresholds,
    _norm,
    _strip_fn,
    _tables,
    note_scope,
)
from scripts.eval_threshold_coverage import (  # noqa: E402
    CORPUS,
    POINTS_TYPES,
    _is_ball_threshold,
    _items,
    resolve,
)

_BALL_RE = re.compile(r"балл", re.IGNORECASE)
# Порог, написанный прозой внутри требования: «…суммарным количеством баллов … не менее 96 баллов».
_INLINE_THR_RE = re.compile(r"не менее\s+[\d\s.,]+\s*балл", re.IGNORECASE)
# ⚠ ПОРОГ В ТЕКСТЕ ТРЕБОВАНИЯ ЧАЩЕ ВСЕГО ОТНОСИТСЯ НЕ К ПОЗИЦИИ, А К ЕЁ КОМПОНЕНТУ.
# Все три случая, найденные 21.08.2026, — про шасси: «использование шасси колесного
# транспортного средства … с выполнением операций (условий), установленных РАЗДЕЛОМ II
# настоящего приложения, которые в совокупности оцениваются … не менее 1700 баллов».
# 1700 баллов здесь — порог ШАССИ по разделу II, а не порог снегоочистителя. Признак —
# ссылка на ДРУГОЙ раздел приложения рядом с числом.
_OTHER_SECTION_RE = re.compile(r"раздел[ео]?м?\s+[IVXLC]+\s+настоящего", re.IGNORECASE)

CLASSES = (
    "defect_unattached",
    "defect_name_match",
    "inline_in_record",
    "component_threshold",
    "note_other_product",
    "narrower_note",
    "own_non_ball",
    "note_non_ball",
    "procurement_only",
    "no_source",
)
DEFECT_CLASSES = ("defect_unattached", "defect_name_match", "inline_in_record")


def note_rows() -> list[dict]:
    """Строки примечаний, несущие порог В БАЛЛАХ и относящиеся к подтверждению происхождения.

    Закупочные примечания исключены на том же основании, что и в `K2`: они задают порог другой
    природы, и засчитывать их уликой значило бы снова смешать два вопроса.
    """
    out: list[dict] = []
    for r in _flat_thresholds():
        if note_scope(r.get("note")) != "general":
            continue
        if _BALL_RE.search(r.get("threshold") or ""):
            out.append({"codes": r["codes"], "names": r["names"], "note": r["note"],
                        "quote": (r.get("threshold") or "").strip(), "kind": "список"})
    for t in _tables():
        if note_scope(t["note"]) != "general":
            continue
        for row in t["rows"]:
            by_year = row.get("by_year") or {}
            if not _BALL_RE.search(" ".join(by_year.values())):
                continue
            quote = "; ".join(f"{k} — {v}" for k, v in list(by_year.items())[:2])
            out.append({"codes": row["codes"], "names": [row["name"]], "note": t["note"],
                        "quote": quote, "kind": "таблица"})
    return out


def _strings(obj):
    """Все строковые значения записи с путём до них."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            for p, s in _strings(v):
                yield (f"{k}.{p}" if p else k), s
    elif isinstance(obj, list):
        for v in obj:
            for p, s in _strings(v):
                yield (f"[].{p}" if p else "[]"), s
    elif isinstance(obj, str):
        yield "", obj


def inline_threshold(rec: dict) -> tuple[str, str] | None:
    """Порог, написанный прозой внутри записи → (путь, цитата)."""
    for path, text in _strings(rec):
        if path in ("product_name", "section_title"):
            continue
        m = _INLINE_THR_RE.search(text)
        if not m:
            continue
        around = text[max(0, m.start() - 260):m.end() + 20]
        quote = "…" + around[-140:].replace("\n", " ").strip() + "…"
        if _OTHER_SECTION_RE.search(around):
            return "component", quote  # порог компонента по ДРУГОМУ разделу — не порог позиции
        return path, quote
    return None


def classify(rec: dict, rows: list[dict]) -> dict:
    """→ {class, evidence, note, quote}. Порядок ветвей = порядок в докстринге."""
    codes = rec.get("okpd2_codes") or []
    name = rec.get("product_name") or ""
    value, src = resolve(rec)

    if value is not None and not _is_ball_threshold(value):
        cls = "own_non_ball" if src == "own" else "note_non_ball"
        return {"class": cls, "evidence": f"{src}: порог задан не в баллах",
                "note": None, "quote": value[:160]}

    if src == "procurement_only":
        return {"class": "procurement_only",
                "evidence": "единственный порог — закупочный, общего нет",
                "note": None, "quote": None}

    # Улики в примечаниях. Направление то же, что у рантайма: код примечания — предок или равен.
    covering, narrower, by_name = None, None, None
    for row in rows:
        for c in codes:
            for rc in row["codes"]:
                if _code_applies(rc, c):
                    covering = covering or row
                elif _code_applies(c, rc):
                    narrower = narrower or row
        if by_name is None and any(_norm(_strip_fn(n)) == _norm(_strip_fn(name))
                                   for n in row["names"]):
            by_name = row

    if covering:
        # ⚠ КОД СОВПАЛ — ЕЩЁ НЕ ЗНАЧИТ, ЧТО ПОРОГ НАШ. Первая редакция считала дефектом любое
        # покрытие по коду и насчитала 21 позицию; разбор всех показал, что в 18 случаях строка
        # примечания написана про ДРУГУЮ продукцию с тем же кодом — «Турбины на водяном паре»
        # (28.11.21) ловили порог строки «Главная энергетическая установка» из прим. 17, которое
        # вообще про судостроение, а «Наборы гинекологические» (32.50.13.190) — порог «Вакуумных
        # одноразовых пробирок». Привязать это значило бы совершить ровно тот дефект, который
        # проект называет самым дорогим. Рантайм отказывается правильно, и это не наша недоработка.
        #
        # Наш дефект — только когда строка примечания говорит про ЭТУ продукцию:
        #   * у строки вообще нет наименования (порог задан коду как таковому — прим. 18, 19,
        #     26(2), 71, 72: «Продукция, классифицируемая кодом …, может быть отнесена …»);
        #   * либо наименование совпадает с позицией (дословно или пересечением ≥2 значимых слов).
        names = [n for n in (covering.get("names") or []) if n.strip()]
        speaks_about_us = (not names
                           or _name_overlap(name, names) >= 2
                           or any(_norm(_strip_fn(n)) == _norm(_strip_fn(name)) for n in names))
        if speaks_about_us:
            return {"class": "defect_unattached",
                    "evidence": f"прим. {covering['note']} ({covering['kind']}), "
                                f"коды {', '.join(covering['codes'][:3])} покрывают "
                                f"{', '.join(codes[:2]) or '—'}"
                                + (f"; строка «{names[0][:60]}»" if names
                                   else "; строка без наименования — порог задан коду"),
                    "note": covering["note"], "quote": covering["quote"][:160]}
        return {"class": "note_other_product",
                "evidence": f"прим. {covering['note']}: порог под тем же кодом "
                            f"({', '.join(covering['codes'][:2])}) написан для ДРУГОЙ продукции "
                            f"— «{names[0][:60]}»; привязывать нельзя",
                "note": covering["note"], "quote": covering["quote"][:160]}
    if by_name:
        return {"class": "defect_name_match",
                "evidence": f"прим. {by_name['note']} ({by_name['kind']}): наименование строки "
                            f"совпадает дословно, коды {', '.join(by_name['codes'][:3])} — другие",
                "note": by_name["note"], "quote": by_name["quote"][:160]}

    inline = inline_threshold(rec)
    if inline and inline[0] == "component":
        return {"class": "component_threshold",
                "evidence": "порог в тексте относится к КОМПОНЕНТУ, оцениваемому по другому "
                            "разделу приложения (шасси по разделу II), а не к самой позиции",
                "note": None, "quote": inline[1][:200]}
    if inline:
        return {"class": "inline_in_record",
                "evidence": f"порог написан прозой в поле {inline[0] or '(корень)'}",
                "note": None, "quote": inline[1][:200]}

    if narrower:
        return {"class": "narrower_note",
                "evidence": f"прим. {narrower['note']}: порог написан для БОЛЕЕ УЗКОГО кода "
                            f"{', '.join(narrower['codes'][:3])}; переносить вверх нельзя",
                "note": narrower["note"], "quote": narrower["quote"][:160]}

    return {"class": "no_source", "evidence": "улик нет ни в примечаниях, ни внутри записи",
            "note": None, "quote": None}


def collect(scope: str) -> list[dict]:
    rows = note_rows()
    out: list[dict] = []
    for path in sorted(glob.glob(CORPUS)):
        for rec in _items(path):
            rtype = rec.get("requirement_type") or ""
            if scope == "points" and rtype not in POINTS_TYPES:
                continue
            value, _src = resolve(rec)
            if value is not None and _is_ball_threshold(value):
                continue  # порог в баллах есть — не наш случай
            info = classify(rec, rows)
            info.update({
                "section": rec.get("section_roman") or "?",
                "code": (rec.get("okpd2_codes") or ["—"])[0],
                "name": rec.get("product_name") or "",
                "requirement_type": rtype or "(нет типа)",
            })
            out.append(info)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Классификация позиций без порога в баллах (A1 #56)")
    ap.add_argument("--scope", choices=("points", "corpus"), default="points",
                    help="срез: points+mixed (по умолчанию, как у K2) или весь корпус")
    ap.add_argument("--json", dest="as_json", action="store_true", help="машинный вывод")
    ap.add_argument("--show", choices=CLASSES, help="напечатать все позиции одного класса с уликой")
    ap.add_argument("--limit", type=int, default=20, help="сколько строк печатать при --show")
    ap.add_argument("--max-defect-share", type=float, default=None,
                    help="критерий приёмки: доля ДЕФЕКТНЫХ классов в процентах от среза")
    args = ap.parse_args()

    items = collect(args.scope)
    # Знаменатель — весь срез, а не только непокрытые: доля должна сравниваться с метрикой K2.
    denom = 0
    for path in sorted(glob.glob(CORPUS)):
        for rec in _items(path):
            if args.scope == "points" and (rec.get("requirement_type") or "") not in POINTS_TYPES:
                continue
            denom += 1

    counts = collections.Counter(i["class"] for i in items)
    defects = sum(counts[c] for c in DEFECT_CLASSES)
    share = defects / denom * 100 if denom else 0.0

    if args.as_json:
        print(json.dumps({
            "scope": args.scope, "denominator": denom, "uncovered": len(items),
            "by_class": {c: counts[c] for c in CLASSES},
            "defects": defects, "defect_share": round(share, 2),
            "items": items,
        }, ensure_ascii=False, indent=2))
        return 0

    label = "points + mixed" if args.scope == "points" else "весь корпус"
    print(f"=== Классификация позиций без порога в баллах (A1 #56) — срез «{label}» ===\n")
    print(f"записей в срезе: {denom} · без порога в баллах: {len(items)} "
          f"({len(items) / denom * 100:.1f} %)\n")
    head = f"{'класс':20} {'позиций':>8} {'доля среза':>11}"
    print(head)
    print("-" * len(head))
    for c in CLASSES:
        mark = " ⚠ ДЕФЕКТ" if c in DEFECT_CLASSES else ""
        print(f"{c:20} {counts[c]:8} {counts[c] / denom * 100:10.1f} %{mark}")
    print("-" * len(head))
    print(f"{'ИТОГО ДЕФЕКТОВ':20} {defects:8} {share:10.1f} %")
    print(f"{'первоисточник/иная модель':20} {len(items) - defects:>3} "
          f"{(len(items) - defects) / denom * 100:10.1f} %")

    by_sec = collections.Counter(i["section"] for i in items if i["class"] in DEFECT_CLASSES)
    if by_sec:
        print("\nДефекты по разделам: "
              + ", ".join(f"{s}:{n}" for s, n in by_sec.most_common(8)))

    if args.show:
        sel = [i for i in items if i["class"] == args.show]
        print(f"\n=== {args.show}: {len(sel)} позиций (печатаю {min(len(sel), args.limit)}) ===")
        for i in sel[:args.limit]:
            print(f"\n  [{i['section']}] {i['code']:16} {i['name'][:62]}")
            print(f"      улика : {i['evidence']}")
            if i["quote"]:
                print(f"      цитата: {i['quote']}")

    if args.max_defect_share is not None:
        ok = share <= args.max_defect_share
        print(f"\n{'✅' if ok else '❌'} критерий ≤{args.max_defect_share:g} % дефектных: {share:.1f} %")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
