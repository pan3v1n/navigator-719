"""Пороги баллов из примечаний к приложению ПП №719 (таблицы «код | наименование | по годам»).

Порог (min_threshold) для многих позиций живёт НЕ в самой позиции, а в нумерованных примечаниях к
разделам: напр. прим. 77 к разд. XVI — «Чиллеры | 270 | 320 | 480 | 542 балла» по годам. В структуру
продукта эти пороги не попали (примечания не структурируются, см. память об актуализации), поэтому
`format_context` показывал «порог не указан». Здесь мы парсим эти таблицы из чанка примечаний
(закоммичен в git → есть и на VM) и отдаём порог для позиции — БЕЗ переиндексации pp719.

Числа берутся ДОСЛОВНО из первоисточника, поэтому при вставке в контекст ответа они заземлены
(faithfulness-гард `unverified_numbers` их пропускает). Матчим порог по НАИМЕНОВАНИЮ позиции (у одного
кода бывает несколько строк: общая категория и конкретный продукт «Чиллеры»), в рамках раздела.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_CHUNK = (Path(__file__).resolve().parents[2] / "knowledge_base" / "pp719" / "chunks"
          / "130_PRIMECHANIYA_prilozheniya.txt")

# Интро примечания-таблицы: «77. Продукция … в раздел XVI настоящего приложения …»
_INTRO_SIMPLE = re.compile(r"^\s*(\d+)\.\s+Продукция,.*?в\s+раздел\s+([IVXLC]+)\s+настоящего приложения")
_YEAR_RE = re.compile(r"с 1 января (\d{4})")
_CODE_RE = re.compile(r"\d{2}\.\d{2}(?:\.\d+)*")


@lru_cache(maxsize=1)
def _tables() -> list[dict]:
    """Парсит все таблицы-пороги из чанка примечаний. Кэш на процесс."""
    if not _CHUNK.exists():
        return []
    lines = _CHUNK.read_text(encoding="utf-8").split("\n")
    tables: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        m = _INTRO_SIMPLE.match(lines[i])
        if not m:
            i += 1
            continue
        note_no, section = m.group(1), m.group(2)
        # найти строку-заголовок таблицы (со «с 1 января» и разделителями «|»)
        j = i + 1
        while j < n and "с 1 января" not in lines[j]:
            if _INTRO_SIMPLE.match(lines[j]):
                break
            j += 1
        if j >= n or "с 1 января" not in lines[j]:
            i += 1
            continue
        years = _YEAR_RE.findall(lines[j])
        rows: list[dict] = []
        pending = ""  # строка-продолжение многострочной ячейки кода («из 28.25.12»)
        k = j + 1
        while k < n:
            raw = lines[k]
            if not raw.strip() or _INTRO_SIMPLE.match(raw):
                break
            cells = raw.split("|")
            if len([c for c in cells if c.strip()]) < 3:  # ячейка кода на отдельной строке
                pending = (pending + " " + raw.strip()).strip()
                k += 1
                continue
            code_cell = (pending + " " + cells[0]).strip()
            pending = ""
            name = cells[1].strip()
            thr = [c.strip() for c in cells[2:] if c.strip()]
            codes = _CODE_RE.findall(code_cell)
            by_year = {years[x]: thr[x] for x in range(min(len(years), len(thr)))}
            if codes and name and by_year:
                rows.append({"codes": codes, "name": name, "by_year": by_year})
            k += 1
        if rows:
            tables.append({"note": note_no, "section": section, "years": years, "rows": rows})
        i = k
    return tables


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _fmt(row: dict, note: str, section: str) -> str:
    steps = "; ".join(f"с 1 января {yr} г. — {val}" for yr, val in row["by_year"].items())
    return f"{steps} [прим. {note} к разд. {section}]"


def lookup_threshold(codes: list[str], product_name: str, section: str | None = None) -> str | None:
    """Порог по годам для позиции (из примечаний), либо None. Матч по НАИМЕНОВАНИЮ в рамках раздела
    (у одного кода бывают строки «общая категория» и «конкретный продукт» — имя различает)."""
    name = _norm(product_name)
    if not name:
        return None
    tables = _tables()
    # 1) точный матч имени в нужном разделе
    for t in tables:
        if section and t["section"] != section:
            continue
        for r in t["rows"]:
            if _norm(r["name"]) == name:
                return _fmt(r, t["note"], t["section"])
    # 2) матч имени без ограничения раздела
    for t in tables:
        for r in t["rows"]:
            if _norm(r["name"]) == name:
                return _fmt(r, t["note"], t["section"])
    return None
