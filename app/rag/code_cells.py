"""Ячейка кода первоисточника у позиции: пометка «из» и «за исключением» у каждого кода (D16 #173).

ЗАЧЕМ. Прим. 3 к приложению ПП №719: если коду предшествует «из», требования применяются ТОЛЬКО к
товарам, названным в графе «Наименование товара». При конвертации пометка терялась —
`okpd2_codes: ["32.50.22.110"]` вместо «из 32.50.22.110», — и модель читала частичную позицию как
охватывающую весь код: «Бедренная ножка SL-Stem» 32.50.22.110 получала требования позиции
«Протезы верхних конечностей» без оговорки (отчёт по бою 22.09, случай ⑤). Хотя бы один код с «из» —
у 980 позиций из 1573. Тем же путём терялось «за исключением»: у четырёх позиций исключённые коды лежат в
`okpd2_codes` наравне со своими («26.20.11 (за исключением 26.20.11.120, 26.20.11.130)»).

КАНАЛ — РАНТАЙМ: файл `knowledge_base/pp719/code_cells.json` (генерирует
`scripts/build_code_cells.py`) сверяется по разделу, наименованию и кодам хита — тем же строкам,
что лежат в payload. Переиндексация не нужна. Файла нет, он битый или позиции в нём нет — пометок
нет, и вывод остаётся прежним (коды без пометок): молчать безопаснее, чем угадывать.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "knowledge_base" / "pp719" / "code_cells.json"

IZ, PLAIN, EXCEPT = "из", "", "за исключением"


def key(section: str | None, product_name: str | None, codes: list[str] | None) -> str:
    """Ключ позиции: раздел | наименование (пробелы схлопнуты) | коды через запятую, как в записи."""
    name = " ".join((product_name or "").split())
    return f"{section or '?'}|{name}|{','.join(c.strip() for c in (codes or []))}"


@lru_cache(maxsize=1)
def _positions() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8")).get("positions") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def marks(section: str | None, product_name: str | None, codes: list[str] | None) -> list[str] | None:
    """Пометка на каждый код позиции (в порядке `okpd2_codes`): IZ, PLAIN или EXCEPT; None — не
    известна (позиции нет в файле или сопоставление со строкой первоисточника не однозначно)."""
    entry = _positions().get(key(section, product_name, codes))
    m = entry.get("marks") if isinstance(entry, dict) else None
    if not isinstance(m, list) or len(m) != len(codes or []):
        return None
    return m


def render(codes: list[str] | None, m: list[str] | None) -> str:
    """Коды так, как они стоят в графе кода: «из 32.50.22.110», «26.20.11 (за исключением
    26.20.11.120, 26.20.11.130)». Без пометок — через запятую, как прежде."""
    codes = codes or []
    if m is None:
        return ", ".join(codes)
    own = [f"из {c}" if mk == IZ else c for c, mk in zip(codes, m) if mk != EXCEPT]
    out = ", ".join(own)
    excepted = [c for c, mk in zip(codes, m) if mk == EXCEPT]
    if excepted:
        out += f" (за исключением {', '.join(excepted)})"
    return out


def note(m: list[str] | None) -> list[str]:
    """Строки-пояснения к ячейке кода для ЦЕЛЕВОЙ позиции. Пусто — пояснять нечего."""
    if not m:
        return []
    out = []
    own = [mk for mk in m if mk != EXCEPT]
    if own and all(mk == IZ for mk in own):
        out.append("код указан с пометкой «из» (прим. 3 к приложению): требования этой позиции "
                   "распространяются только на товары, названные в её наименовании, а не на всю "
                   "продукцию с этим кодом ОКПД2")
    elif IZ in own:
        out.append("часть кодов указана с пометкой «из» (прим. 3 к приложению): для продукции с этими "
                   "кодами требования позиции распространяются только на товары, названные в её "
                   "наименовании")
    if EXCEPT in m:
        out.append("продукция с кодами, указанными «за исключением», под эту позицию не подпадает")
    return out


def mark_of(code: str, codes: list[str] | None, m: list[str] | None) -> str | None:
    """Пометка конкретного кода позиции; None — неизвестна или код в позиции встречается с
    разными пометками («Шлюпки спасательные»: 30.12.19.141 и «из 30.12.19.141»)."""
    if m is None:
        return None
    seen = {mk for c, mk in zip(codes or [], m) if c == code}
    return seen.pop() if len(seen) == 1 else None
