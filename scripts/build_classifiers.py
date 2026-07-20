# -*- coding: utf-8 -*-
"""Сборка справочных данных ОКПД2 + переходных ключей ТН ВЭД↔ОКПД2 из xlsx-первоисточников (T9).

ИСТОЧНИКИ (даёт заказчик; сами xlsx НЕ коммитим — по конвенции проекта коммитим извлечённые данные,
как pp719_full.txt/prikaz52_full.txt):
  - ОКПД_2.xlsx (Общероссийский классификатор ОК 034-2014 (КПЕС 2008); лист «Лист1»: № | Код | Название)
  - ТНВЭД_ОКПД2_*.xlsx (переходные ключи; листы «ТН ВЭД - ОКПД2» и «ОКПД2 - ТН ВЭД»)

ВЫХОД (коммитим в репо, читает рантайм app/rag/okpd2_ref.py):
  - knowledge_base/classifiers/okpd2.tsv       — «код<TAB>наименование» (весь классификатор)
  - knowledge_base/classifiers/tnved_okpd2.tsv — «тнвэд_цифры<TAB>окпд2» (переходные пары, union двух листов)

Запуск (нужен PYTHONUTF8=1 на Windows — кириллица):
  .venv/Scripts/python.exe scripts/build_classifiers.py [ОКПД_2.xlsx] [ТНВЭД_ОКПД2.xlsx]

Читает xlsx НАПРЯМУЮ через stdlib (zipfile+ElementTree) — openpyxl не нужен (не ставится на рваной РФ-сети).
"""
from __future__ import annotations

import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUT = _ROOT / "knowledge_base" / "classifiers"
_OKPD2_RE = re.compile(r"^\d{2}(?:\.\d+)*$")

# Дефолтные пути к первоисточникам (Downloads заказчика); можно переопределить аргументами.
_DEF_OKPD2 = r"C:\Users\pan3v\Downloads\ОКПД_2.xlsx"
_DEF_TNVED = r"C:\Users\pan3v\Downloads\ТНВЭД_ОКПД2_20_07_2023 (1).xlsx"


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _col_idx(ref: str) -> int:
    letters = "".join(c for c in ref if c.isalpha())
    idx = 0
    for c in letters:
        idx = idx * 26 + (ord(c.upper()) - 64)
    return idx - 1


def read_sheet(path: str, sheet: str | None = None) -> list[list]:
    """Строки листа xlsx как списки ячеек (shared strings разрешены). Первый лист, если sheet=None."""
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    sheets = [(s.get("name"), s.get(rid)) for s in wb.iter() if _local(s.tag) == "sheet"]
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid2t = {r.get("Id"): r.get("Target") for r in rels}
    pick = next((s for s in sheets if s[0] == sheet), sheets[0]) if sheet else sheets[0]
    target = rid2t.get(pick[1], "worksheets/sheet1.xml")
    spath = ("xl/" + target) if not target.startswith("/") else target[1:]
    out: list[list] = []
    for row in ET.fromstring(z.read(spath)).iter():
        if _local(row.tag) != "row":
            continue
        cells: dict[int, str | None] = {}
        for c in row:
            if _local(c.tag) != "c":
                continue
            t = c.get("t")
            v = None
            for ch in c:
                if _local(ch.tag) == "v":
                    v = ch.text
                elif _local(ch.tag) == "is":
                    v = "".join(x.text or "" for x in ch.iter() if _local(x.tag) == "t")
            if t == "s" and v is not None:
                v = shared[int(v)]
            cells[_col_idx(c.get("r", "A1"))] = v
        if cells:
            mx = max(cells)
            out.append([cells.get(i) for i in range(mx + 1)])
    return out


def _norm_tnved(s: str | None) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def build_okpd2(path: str) -> dict[str, str]:
    """Классификатор: код → наименование. Данные с 6-й строки (шапка «№|Код|Название» на 5-й)."""
    names: dict[str, str] = {}
    for r in read_sheet(path)[5:]:
        code = (r[1] or "").strip() if len(r) > 1 else ""
        name = (r[2] or "").strip() if len(r) > 2 else ""
        if _OKPD2_RE.match(code) and name:
            names[code] = name
    return names


def build_keys(path: str) -> set[tuple[str, str]]:
    """Переходные пары (тнвэд_цифры, окпд2) — union двух листов. Пустые ячейки = merged-cell (ffill)."""
    pairs: set[tuple[str, str]] = set()
    # Лист «ОКПД2 - ТН ВЭД»: col0=ОКПД2 (ffill по merged-cell), col2=ТН ВЭД; данные с 5-й строки.
    cur = None
    for r in read_sheet(path, "ОКПД2 - ТН ВЭД")[4:]:
        ok = (r[0] or "").strip() if len(r) > 0 else ""
        if _OKPD2_RE.match(ok):
            cur = ok
        tn = _norm_tnved(r[2] if len(r) > 2 else "")
        if cur and tn:
            pairs.add((tn, cur))
    # Лист «ТН ВЭД - ОКПД2»: col0=ТН ВЭД (ffill), col2=ОКПД2 (ffill в пределах заголовка); данные с 6-й.
    cur_tn = cur_ok = None
    for r in read_sheet(path, "ТН ВЭД - ОКПД2")[5:]:
        tn = _norm_tnved(r[0] if len(r) > 0 else "")
        ok = (r[2] or "").strip() if len(r) > 2 else ""
        if tn:
            cur_tn = tn
        if _OKPD2_RE.match(ok):
            cur_ok = ok
        if cur_tn and cur_ok:
            pairs.add((cur_tn, cur_ok))
    return pairs


def main() -> None:
    okpd2_xlsx = sys.argv[1] if len(sys.argv) > 1 else _DEF_OKPD2
    tnved_xlsx = sys.argv[2] if len(sys.argv) > 2 else _DEF_TNVED
    _OUT.mkdir(parents=True, exist_ok=True)

    names = build_okpd2(okpd2_xlsx)
    okpd2_tsv = _OUT / "okpd2.tsv"
    with okpd2_tsv.open("w", encoding="utf-8", newline="\n") as f:
        for code in sorted(names):
            f.write(f"{code}\t{names[code]}\n")
    print(f"okpd2.tsv: {len(names)} кодов → {okpd2_tsv}")

    pairs = build_keys(tnved_xlsx)
    keys_tsv = _OUT / "tnved_okpd2.tsv"
    with keys_tsv.open("w", encoding="utf-8", newline="\n") as f:
        for tn, ok in sorted(pairs):
            f.write(f"{tn}\t{ok}\n")
    tn_uniq = len({t for t, _ in pairs})
    print(f"tnved_okpd2.tsv: {len(pairs)} пар ({tn_uniq} ТН ВЭД) → {keys_tsv}")


if __name__ == "__main__":
    main()
