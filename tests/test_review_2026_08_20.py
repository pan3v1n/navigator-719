"""Находки ревью 20.08.2026 — дельта `main..dev` (заход 2: `D12` #90 + `K2` #47).

Ревью проводилось ПОСЛЕ переиндексации и замера, и нашло пять дефектов, три из которых внесены
самим заходом 2. Главный — того же класса, ради которого писалась `K2`: правильное число из
первоисточника, привязанное не к тому режиму.

Каждый тест проверяет МЕХАНИЗМ, а не совпадение результата. Повод — четвёртая находка того же
ревью: `test_coefficient_table_is_not_a_threshold` утверждал намерение, которого в коде не было,
и проходил впустую (его единственный `assertIsNone` был бы верен и при сломанном разборе).
"""
from __future__ import annotations

import glob
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.thresholds import (  # noqa: E402
    _split_exception,
    _tables,
    lookup_threshold,
)

CORPUS = str(ROOT / "knowledge_base" / "pp719" / "structured" / "*.json")
_STEP_RE = re.compile(r"((?:с|до)\s+\d{1,2}\s+\w+\s+\d{4}\s*г\.)\s*[-—]?\s*не менее\s+(\d+)")
_MARKER = "⚠ ИНОЙ порог"


def _records() -> list[dict]:
    out: list[dict] = []
    for path in sorted(glob.glob(CORPUS)):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        out.extend(data if isinstance(data, list) else (data.get("items") or []))
    return out


class TestTwoSchedulesInOneLine(unittest.TestCase):
    """Прим. 17: одно предложение несёт ДВА графика — общий и оговорку «за исключением судов…».

    Склеенные в одну строку «Порог:», они давали одну дату с разными числами: «с 1 июля 2025 г.
    не менее 3400» и, тремястами символов ниже, «с 1 июля 2025 г. не менее 2900». Оба дословны,
    поэтому faithfulness-гард молчит, а условие («инвестиционные проекты по договорам о
    закреплении доли квоты добычи») терялось в середине 708-символьного блока.
    """

    def test_split_only_when_the_exception_carries_its_own_threshold(self):
        """Режем ТОЛЬКО оговорку со своим порогом: «за исключением X» без «не менее» — это
        сужение охвата, оно относится к общему графику, и резать его нельзя."""
        general, exc = _split_exception(
            "до 30 июня 2023 г. не менее 2300 баллов, за исключением судов, "
            "которые оцениваются с 1 июля 2025 г. не менее 2900 баллов")
        self.assertEqual(general, "до 30 июня 2023 г. не менее 2300 баллов")
        self.assertIsNotNone(exc)
        self.assertIn("2900", exc)

        text = "не менее 300 баллов, за исключением судов рыбопромыслового флота"
        self.assertEqual(_split_exception(text), (text, None))

    def test_fishing_vessels_general_schedule_is_free_of_the_exception(self):
        """У «Судов рыболовных» общий график обязан давать 3400 на 2025 год, а 2900 —
        встречаться только ПОСЛЕ маркера, вместе со своим условием."""
        thr = lookup_threshold(["30.11.31.110"], "Суда рыболовные <9>", "XVIII")
        self.assertIsNotNone(thr, "порог прим. 17 перестал находиться")
        self.assertIn(_MARKER, thr, "оговорка не отделена от общего графика")

        general, exception = thr.split(_MARKER, 1)
        # ⚠ Сверяем ПРИВЯЗКУ даты к числу, а не наличие числа: 2900 законно стоит и в общем
        # графике — но за 2023 годом. Ровно на этом различии и построен весь дефект, поэтому
        # проверка «2900 не встречается» была бы той же ошибкой в зеркале.
        steps = dict(_STEP_RE.findall(general))
        self.assertEqual(steps.get("с 1 июля 2025 г."), "3400",
                         "в общем графике 2025 год получил порог оговорки")
        self.assertEqual(steps.get("с 1 июля 2023 г."), "2900")
        self.assertEqual(dict(_STEP_RE.findall(exception)).get("с 1 июля 2025 г."), "2900",
                         "график оговорки потерялся при разделении")
        self.assertIn("квот", exception, "условие оговорки не поехало вместе с её числами")

    def test_no_position_shows_one_date_with_two_thresholds(self):
        """Радиус по ВСЕМУ корпусу: ни у одной позиции общий график не содержит одну и ту же
        дату с разными порогами. Правило меряется на всех, а не на целевом случае."""
        offenders: list[str] = []
        for rec in _records():
            if (rec.get("min_threshold") or "").strip():
                continue
            thr = lookup_threshold(rec.get("okpd2_codes") or [],
                                   rec.get("product_name") or "", rec.get("section_roman"))
            if not thr:
                continue
            general = thr.split(_MARKER, 1)[0]
            seen: dict[str, set[str]] = {}
            for date, value in _STEP_RE.findall(general):
                seen.setdefault(date, set()).add(value)
            if any(len(v) > 1 for v in seen.values()):
                offenders.append(f"{rec.get('product_name', '')[:40]}: "
                                 f"{ {d: sorted(v) for d, v in seen.items() if len(v) > 1} }")
        self.assertEqual(offenders, [], "одна дата с разными порогами в общем графике")


class TestColumnLabelsStayVerbatim(unittest.TestCase):
    """Подписи колонок — цитата первоисточнику, а не синтез.

    Фолбэк `_YEAR_ANY_RE.findall(header)` возвращал ГОЛЫЙ год из группы регулярки, поэтому
    колонка «2022 - 2023 годы» подписывалась как «2023»: диапазон схлопывался в последнюю дату,
    предлог «с 1 января» терялся. Срабатывал он на 3 таблицах из 30 и молчал лишь потому, что в
    их первой колонке нет кода ОКПД2 — предохранитель был случайным, а не задуманным.
    """

    def test_no_column_label_is_a_bare_synthesised_year(self):
        """Голый «2024» не встречается в шапках первоисточника ни разу: там либо «2024 год»,
        либо «с 1 января 2024 г.», либо диапазон. Значит, четыре цифры без единого слова —
        след синтеза, а не цитата."""
        tables = _tables()
        self.assertTrue(tables, "таблицы примечаний перестали разбираться — тест ослеп")
        for table in tables:
            for label in table["years"]:
                self.assertNotRegex(
                    label, r"^\d{4}$",
                    f"прим. {table['note']}: подпись {label!r} — голый год, то есть синтез")


class TestCoefficientTableIsRejectedByMechanism(unittest.TestCase):
    """Прим. 80 — «Коэффициент | Срок действия коэффициента», а не пороги.

    Прежний тест утверждал это через `assertIsNone` на одной позиции и проходил бы при любом
    сломе разбора. Теперь проверяется САМ признак: ни одна разобранная таблица не объявляет
    колонку коэффициента.
    """

    def test_no_parsed_table_declares_a_coefficient_column(self):
        for table in _tables():
            for label in table["years"]:
                self.assertNotIn("коэффициент", label.lower(),
                                 f"прим. {table['note']}: коэффициент разобран как порог")

    def test_note_80_is_not_among_parsed_tables(self):
        self.assertNotIn("80", [t["note"] for t in _tables()])


class TestExcludedCodesGateSeesEveryMarkerRow(unittest.TestCase):
    """`excluded_codes()` — гейт предиката `is_section_title_stub`, и ему нужны одни КОДЫ.

    Строился он из списка, отфильтрованного по НЕПУСТОЙ ячейке требований (`if req:`), поэтому
    строка `из 32.50.13.110 Позиция исключена.||` — третье из пяти канонических написаний —
    в гейт не попадала. «Маркер + пустая ячейка» — обычная форма исключённой строки.
    """

    def test_row_with_empty_requirements_cell_still_reaches_the_gate(self):
        from scripts.drop_excluded_positions import excluded_codes, excluded_rows
        rows = excluded_rows()
        codes = excluded_codes(rows)
        self.assertIn("32.50.13.110", codes,
                      "код строки с пустой ячейкой требований не дошёл до гейта")
        self.assertTrue(any(req == "" for _c, req in rows),
                        "строки с пустой ячейкой требований снова отфильтрованы на входе")


if __name__ == "__main__":
    unittest.main()
