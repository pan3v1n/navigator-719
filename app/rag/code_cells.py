"""Ячейка кода первоисточника у позиции: пометка «из» и «за исключением» у каждого кода (D16 #173).

ЗАЧЕМ. Прим. 3 к приложению ПП №719: если коду предшествует «из», требования применяются ТОЛЬКО к
товарам, названным в графе «Наименование товара». При конвертации пометка терялась —
`okpd2_codes: ["32.50.22.110"]` вместо «из 32.50.22.110», — и модель читала частичную позицию как
охватывающую весь код: «Бедренная ножка SL-Stem» 32.50.22.110 получала требования позиции
«Протезы верхних конечностей» без оговорки (отчёт по бою 22.09, случай ⑤). Хотя бы один код с
«из» — у 977 позиций из 1573 (ещё у трёх наименование со строкой первоисточника не совпало — там
«не определено»). Тем же путём терялось «за исключением»: у четырёх позиций исключённые коды лежат
в `okpd2_codes` наравне со своими («26.20.11 (за исключением 26.20.11.120, 26.20.11.130)»).

КАНАЛ — РАНТАЙМ: файл `knowledge_base/pp719/code_cells.json` (генерирует
`scripts/build_code_cells.py`) сверяется по разделу, наименованию и кодам хита — тем же строкам,
что лежат в payload. Переиндексация не нужна. Файла нет, он битый или позиции в нём нет — пометок
нет, и вывод остаётся прежним (коды без пометок): молчать безопаснее, чем угадывать.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.rag import okpd2_ref

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
    own = [(c, mk) for c, mk in zip(codes, m) if mk != EXCEPT]
    excepted = [c for c, mk in zip(codes, m) if mk == EXCEPT]
    # «(за исключением …)» — к СВОЕМУ коду: исключённый код лежит под ним в иерархии ОКПД2. Свой
    # код один у всех четырёх таких позиций корпуса, но при нескольких скобка в конце прилипла бы
    # к последнему (ревью D16, LOW-5). Исключение без своего предка — в конец, как в ячейке.
    under: dict[int, list[str]] = {}
    loose: list[str] = []
    for e in excepted:
        i = next((i for i, (c, _) in enumerate(own) if _under(e, c)), None)
        (under.setdefault(i, []) if i is not None else loose).append(e)
    parts = []
    for i, (c, mk) in enumerate(own):
        part = f"из {c}" if mk == IZ else c
        if i in under:
            part += f" (за исключением {', '.join(under[i])})"
        parts.append(part)
    out = ", ".join(parts)
    if loose:
        out += f" (за исключением {', '.join(loose)})"
    return out


def _under(code: str, ancestor: str) -> bool:
    """`code` лежит под `ancestor` (или равен ему) в иерархии ОКПД2 — поразрядно, как `D14`."""
    a, c = okpd2_ref.okpd2_key(ancestor), okpd2_ref.okpd2_key(code)
    return bool(a) and c.startswith(a)


def excepted_covers(question_code: str, codes: list[str] | None, m: list[str] | None) -> bool:
    """Код вопроса попадает под код, указанный у позиции «за исключением» — позиция на него НЕ
    распространяется."""
    if m is None:
        return False
    return any(mk == EXCEPT and _under(question_code, c) for c, mk in zip(codes or [], m))


def note(m: list[str] | None) -> list[str]:
    """Строки-пояснения к ячейке кода для ЦЕЛЕВОЙ позиции. Пусто — пояснять нечего.

    ⚠ БЕЗ ЦИФР (ревью D16, MED-1). Первая редакция ссылалась на «прим. 3», и пояснение у 583
    позиций добавило в контекст отдельное число 3: гард чисел (`pipeline.number_in_context`) ищет
    его по границе не-цифры, и выдуманное «не менее 3 баллов» переставало ловиться у 580 позиций.
    Тот же урок, что у счётчика операций в EV7: шире заземление — слепее гард. Номер примечания
    модели не нужен, эксперт найдёт его по пометке."""
    if not m:
        return []
    out = []
    own = [mk for mk in m if mk != EXCEPT]
    if own and all(mk == IZ for mk in own):
        out.append("код указан с пометкой «из» (примечания к приложению): требования этой позиции "
                   "распространяются только на товары, названные в её наименовании, а не на всю "
                   "продукцию с этим кодом ОКПД2")
    elif IZ in own:
        out.append("часть кодов указана с пометкой «из» (примечания к приложению): для продукции с этими "
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
