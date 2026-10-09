"""D14 #141: радиус поразрядной иерархии ОКПД2 против посегментной — ТОЛЬКО ДАННЫЕ, без Qdrant и LLM.

ЗАЧЕМ. `retriever.okpd2_match` сравнивал коды ПОСЕГМЕНТНО: «20.14.11.112» и «20.14.11.110» для него
соседи, хотя в ОКПД2 второй — категория, а первый — её подкатегория. Код ОКПД2 иерархический
ПОРАЗРЯДНО (ОК 034-2014: класс XX · подкласс XX.X · группа XX.XX · подгруппа XX.XX.X · вид XX.XX.XX ·
категория XX.XX.XX.Y00 · подкатегория XX.XX.XX.YZ0 · XX.XX.XX.YZW), а нули в хвосте последнего
сегмента — заполнитель уровня, не разряд. Посегментное сравнение теряет ТРИ уровня, а не один:
  * подкатегория «…YZ0» → её «…YZW» (н-бутан, хладон — `D14`);
  * подкласс «XX.X» → «XX.XY…» (в корпусе 27.3, 27.2, 13.2);
  * подгруппа «XX.XX.X» → «XX.XX.XY…» (в корпусе 29.10.2, 20.60.1 и др.).

ЧТО МЕРИТ. Каждый код справочника ОКПД2 (19 200) берётся как код вопроса; для него считаются
позиции корпуса, совпавшие по коду, по старому правилу и по новому:
  * ПОТЕРЯНО — совпадало раньше и не совпадает теперь (обязано быть 0: новое правило — надмножество);
  * ПРИОБРЕТЕНО — по уровню, на котором лежит связь, и по направлению (предок / потомок);
  * БЛИЖАЙШИЙ ПРЕДОК — самая глубокая позиция-предок (или равная). «Ближе, чем раньше» — популяция,
    у которой ответ получает более частную позицию; «впервые есть» — у которой раньше предка не было;
  * ПОПУЛЯЦИЯ ВРЕДА — у кода есть СВОЯ позиция (точное совпадение), а новое правило добавляет ещё и
    групповую: точная обязана остаться целевой;
  * «ИЗ» — сколько приобретённых позиций-предков в первоисточнике стоят с пометкой «из» (прим. 3:
    требования только к названным в графе товарам). Подъём к ним выводит КАНДИДАТА, а не вердикт.

Запуск: `.venv\\Scripts\\python scripts/diag_okpd2_climb.py` (печатает сводку; `--json PATH` — детали).
Оба правила реализованы ЗДЕСЬ, независимо от `app/`: замер обязан давать одно и то же до и после
правки, иначе «до/после» сравнивает инструмент сам с собой.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.console import enable_utf8  # noqa: E402

KB = ROOT / "knowledge_base"
STRUCTURED = KB / "pp719" / "structured"
SOURCE = KB / "pp719" / "pp719_full.txt"
CLASSIFIER = KB / "classifiers" / "okpd2.tsv"


# --- два правила, независимо от app/ ------------------------------------------------------------
def segs(code: str) -> list[str]:
    return [s for s in code.strip().replace(" ", "").split(".") if s]


def old_related(rec: str, q: str) -> bool:
    """Прежнее правило (`retriever.okpd2_match` до D14): посегментный префикс в любую сторону."""
    a, b = segs(rec), segs(q)
    n = min(len(a), len(b))
    return bool(n) and a[:n] == b[:n]


def key(code: str) -> str:
    """Поразрядный ключ: сегменты подряд, нули в хвосте ДЕВЯТИЗНАЧНОГО сегмента — заполнитель."""
    s = segs(code)
    if len(s) == 4 and len(s[3]) == 3:
        s = s[:3] + [s[3].rstrip("0")]
    return "".join(s)


def new_related(rec: str, q: str) -> bool:
    a, b = key(rec), key(q)
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def level(code: str) -> str:
    """Уровень связи по длине поразрядного ключа предка."""
    return {2: "класс XX", 3: "подкласс XX.X", 4: "группа XX.XX", 5: "подгруппа XX.XX.X",
            6: "вид XX.XX.XX", 7: "категория …Y00", 8: "подкатегория …YZ0",
            9: "код …YZW"}.get(len(key(code)), f"длина {len(key(code))}")


# --- данные ---------------------------------------------------------------------------------------
def load_records() -> list[dict]:
    recs: list[dict] = []
    for f in sorted(STRUCTURED.glob("*.json")):
        recs += json.loads(f.read_text(encoding="utf-8"))
    return recs


def load_classifier() -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in CLASSIFIER.read_text(encoding="utf-8").splitlines():
        if "\t" in ln:
            c, n = ln.split("\t", 1)
            out[c] = n
    return out


_ROW_CODE = re.compile(r"^(из\s+)?(\d{2}(?:\.\d+)*)\s*(?:<[^>]{1,16}>\s*)*,?\s*(?:\||$)", re.I)


def source_iz() -> dict[str, set[bool]]:
    """Код → {был ли он в графе кода с «из»} по строкам таблицы первоисточника.

    Ячейка кода бывает многострочной («из 27.12.31,\\nиз 27.12.32,\\nиз 27.12.10.190|…»), поэтому
    берётся каждая строка, начинающаяся с кода. Один код встречается в нескольких позициях — с «из»
    и без, — отсюда множество, а не флаг."""
    out: dict[str, set[bool]] = {}
    for ln in SOURCE.read_text(encoding="utf-8").splitlines():
        m = _ROW_CODE.match(ln.strip())
        if m:
            out.setdefault(m.group(2), set()).add(bool(m.group(1)))
    return out


def main() -> int:
    enable_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", help="куда сохранить детали по кодам с изменившимся предком")
    args = ap.parse_args()

    recs = load_records()
    names = load_classifier()
    iz = source_iz()
    rec_codes = [(i, c) for i, r in enumerate(recs) for c in (r.get("okpd2_codes") or [])]
    # Кандидаты на связь — только коды того же КЛАССА (первые два знака): и старое, и новое правило
    # связывают коды лишь внутри одного класса. Без этого 19 200 × 1 700 сравнений идут минутами.
    by_class: dict[str, list[tuple[int, str]]] = {}
    for i, c in rec_codes:
        by_class.setdefault(key(c)[:2], []).append((i, c))

    lost = 0
    gained_by = Counter()
    closer = new_anc = harm = 0
    max_anc = (0, "")
    gained_iz = Counter()
    details = []
    for q in names:
        pool = by_class.get(key(q)[:2], [])
        old = {i for i, c in pool if old_related(c, q)}
        new = {i for i, c in pool if new_related(c, q)}
        lost += len(old - new)
        for i in new - old:
            for c in recs[i].get("okpd2_codes") or []:
                if new_related(c, q) and not old_related(c, q):
                    anc = key(q).startswith(key(c))
                    gained_by[(level(c) if anc else level(q), "предок" if anc else "потомок")] += 1
                    if anc:
                        st = iz.get(c)
                        gained_iz["из" if st == {True} else "без из" if st == {False}
                                  else "и так, и так" if st else "нет в таблице"] += 1

        def closest(rule) -> tuple[int, list[int]]:
            best, who = -1, []
            for i, c in pool:
                if rule(c, q) and key(q).startswith(key(c)):
                    d = len(key(c))
                    if d > best:
                        best, who = d, [i]
                    elif d == best and i not in who:
                        who.append(i)
            return best, who

        ob, ow = closest(old_related)
        nb, nw = closest(new_related)
        n_anc = len({i for i, c in pool if key(q).startswith(key(c))})
        max_anc = max(max_anc, (n_anc, q))
        # ⚠ ПОПУЛЯЦИЯ ВРЕДА считается по ПРИОБРЕТЁННЫМ связям, а не по «ближайший стал глубже»:
        # у кода со своей позицией ближайший предок — он сам, глубже стать не может, и та метрика
        # давала 0 по построению. Вред — когда к своей позиции добавились чужие, конкурирующие
        # за окно и за роль целевой.
        if any(key(c) == key(q) for i, c in pool) and new - old:
            harm += 1
        if nb > ob:
            closer += 1
            if ob < 0:
                new_anc += 1
            details.append({"code": q, "name": names[q][:90], "old_depth": ob, "new_depth": nb,
                            "old": sorted({recs[i]["source_anchor"] for i in ow}),
                            "new": sorted({recs[i]["source_anchor"] for i in nw})})

    print(f"кодов справочника: {len(names)} · позиций корпуса: {len(recs)} · кодов у позиций: {len(rec_codes)}")
    print(f"ПОТЕРЯНО совпадений: {lost}  (обязано быть 0 — новое правило надмножество прежнего)")
    print("ПРИОБРЕТЕНО связей (код вопроса × код позиции) по уровню и направлению:")
    for (lv, d), n in sorted(gained_by.items(), key=lambda x: -x[1]):
        print(f"   {lv:22} {d:8} {n}")
    print(f"приобретённые позиции-предки по пометке первоисточника: {dict(gained_iz)}")
    print(f"кодов, у которых ближайшая позиция-предок стала ГЛУБЖЕ: {closer}")
    print(f"   из них предка по коду раньше не было вовсе: {new_anc}")
    print(f"кодов со СВОЕЙ позицией, к которой добавились новые связанные (популяция вреда — "
          f"своя обязана остаться целевой): {harm}")
    print(f"наибольшее число позиций-предков у одного кода: {max_anc[0]} ({max_anc[1]})")
    print("примеры:")
    for d in details[:12]:
        print(f"   {d['code']:14} {d['name'][:50]:50} глубина {d['old_depth']}→{d['new_depth']} "
              f"{', '.join(a.replace('Приложение к ПП №719, ', '') for a in d['new'])[:70]}")
    if args.json:
        Path(args.json).write_text(json.dumps(details, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"детали: {args.json} ({len(details)} кодов)")
    return 1 if lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
