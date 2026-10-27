"""Живучесть разбора порогов к следующему экспорту Контура: `D17` #207 и `K2-4` #211. Офлайн.

Оба дефекта на текущих данных не проявляются (радиус правок — 0 изменений по всем позициям), и
ровно поэтому их держат тесты, а не замер: проявятся они на следующей редакции, при актуализации.
Тексты берутся из НАСТОЯЩЕГО чанка примечаний, испорченного так, как его портил бы экспорт.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from app.rag import thresholds as t  # noqa: E402

CACHED = (t._tables, t._flat_thresholds, t._note_scopes)


def _clear():
    for f in CACHED:
        f.cache_clear()


@contextmanager
def chunk(text: str):
    """Разбор идёт по подменённому тексту чанка; предупреждения собираются в список."""
    orig, warnings = t._CHUNK, []
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "chunk.txt"
        p.write_bytes(text.encode("utf-8"))
        t._CHUNK = p
        _clear()
        sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
        try:
            yield warnings
        finally:
            logger.remove(sink)
            t._CHUNK = orig
            _clear()


def parse() -> tuple:
    return t._tables(), t._flat_thresholds(), t._note_scopes()


REAL = t._CHUNK.read_bytes().decode("utf-8")


class TestInvisibleCharactersD17(unittest.TestCase):
    """U+200B в строке ступени не должен прятать примечание (`D17` #207)."""

    @staticmethod
    def _dirty() -> str:
        """Хвост невидимых — как у первой ступени прим. 72.3 в ред. 29.09.2026; плюс в строках таблиц.

        ⚠ Хвост встаёт ДО конца строки: в рабочей копии на Windows чанк с CRLF, и символы после `\\r`
        `splitlines()` превратил бы в лишние пустые строки — это порча теста, а не экспорта."""
        out = []
        for line in REAL.split("\n"):
            body, cr = (line[:-1], "\r") if line.endswith("\r") else (line, "")
            if t._STEP_RE.search(body) or "|" in body:
                out.append(body + "​​" + cr)
                # ⚠ И строка ИЗ ОДНИХ невидимых между ступенями и строками таблиц (ревью PR #220,
                # MED): очищенная до пустоты, она стала бы границей и обрезала график молча.
                out.append("​" + cr)
            else:
                out.append(line)
        return "\n".join(out)

    def test_dirty_text_parses_like_clean(self):
        with chunk(REAL):
            clean = parse()
        with chunk(self._dirty()):
            self.assertEqual(parse(), clean, "невидимые символы изменили разбор порогов")

    def test_dirt_reaches_the_parser_without_cleaning(self):
        """Положительный контроль: без вычистки загрязнение МЕНЯЕТ разбор — иначе тест выше пуст."""
        with chunk(REAL):
            clean = parse()
        orig = t._ZERO_WIDTH_RE
        t._ZERO_WIDTH_RE = re.compile("(?!x)x")   # не совпадает ни с чем
        try:
            with chunk(self._dirty()):
                self.assertNotEqual(parse(), clean, "загрязнение не достаёт разбора — проверка пуста")
        finally:
            t._ZERO_WIDTH_RE = orig

    def test_word_joiner_is_also_invisible(self):
        self.assertEqual(t._ZERO_WIDTH_RE.sub("", "a​b‌c‍d⁠e﻿f"), "abcdef")


class TestAllOrNothingIsLoudK24(unittest.TestCase):
    """Отказ от формы по видам прим. 80.1 обязан звучать в журнале (`K2-4` #211)."""

    @staticmethod
    def _broken() -> str:
        """ГОСТ с «dd.dd» в имени первого вида 80.1 — непонятая строка, форма отвергается целиком."""
        lines = REAL.split("\n")
        start = next(i for i, ln in enumerate(lines) if ln.startswith("80.1."))
        k = next(i for i in range(start, len(lines))
                 if lines[i].startswith("Микроисточники") and "|" in lines[i])
        name, rest = lines[k].split("|", 1)
        lines[k] = f"{name} по ГОСТ 31.12|{rest}"
        return "\n".join(lines)

    def test_correct_text_is_silent(self):
        with chunk(REAL) as warnings:
            parse()
        self.assertEqual(warnings, [], "на верном тексте журнал обязан молчать — иначе его не читают")

    def test_rejected_variant_form_is_logged(self):
        with chunk(self._broken()) as warnings:
            tables = t._tables()
        self.assertFalse(any(tb["note"] == "80.1" for tb in tables),
                         "порча не воспроизвела отказ — тест ничего не проверяет")
        self.assertTrue(any("80.1" in w and "отвергнута" in w for w in warnings), warnings)
        self.assertTrue(any("80.1" in w and "разобрано 0" in w for w in warnings), warnings)


class TestZeroRowsAlarmSeesMultiCodeCells(unittest.TestCase):
    """Тревога «разобрано 0» узнаёт строку таблицы по коду В ЯЧЕЙКЕ, а не по началу строки.

    Ревью PR #220: `_ROW_START_RE` узнаёт одну ячейку с одним кодом на одной строке — у прим. 80.1
    таких 8 из 95, и таблица из одних многокодовых ячеек обнулилась бы без тревоги."""

    SYNTHETIC = ("99. Продукция, классифицируемая кодами по ОК 034-2014 (КПЕС 2008), может быть "
                 "отнесена к российской промышленной продукции при условии:\n"
                 "Код|Наименование|с 1 января 2026 г.|с 1 января 2027 г.|\n"
                 "\n"
                 "из 26.11.22.200,\nиз 26.11.22.210|Изделия первые|не менее 10 баллов|не менее 20 баллов|\n"
                 "из 26.11.22.220,\nиз 26.11.22.230|Изделия вторые|не менее 15 баллов|не менее 25 баллов|\n")

    def test_table_of_multi_code_cells_lost_whole_is_logged(self):
        with chunk(self.SYNTHETIC) as warnings:
            tables = t._tables()
        self.assertFalse(any(tb["note"] == "99" for tb in tables),
                         "синтетическая таблица разобралась — тест ничего не проверяет")
        self.assertTrue(any("99" in w and "разобрано 0" in w for w in warnings), warnings)

    def test_code_row_shapes(self):
        self.assertTrue(t._is_code_row("из 26.11.22.200,\nиз 26.11.22.210|Имя|не менее 10 баллов|"))
        # поправка с датой, похожей на код, и исключение — одна ячейка, их отсекает число ячеек
        self.assertFalse(t._is_code_row("(в ред. Постановления Правительства РФ от 05.10.2026 N 1297)|"))
        self.assertFalse(t._is_code_row("из 21.20.24.169 - Исключено.|"))
        self.assertFalse(t._is_code_row('из 28.13.27 "Компрессорная установка":|до 31 декабря 2025 г.|'
                                        "с 1 января 2026 г.|"),
                         "подзаголовок таблицы по узлам (прим. 74) — шапка, а не строка с кодом")


class TestUniformSchedule(unittest.TestCase):
    """Одно правило «одинаковые графики → общий порог» на три места (`K2-4` #211)."""

    def test_equal_schedules_give_a_copy(self):
        rows = [{"by_year": {"2026": "10", "2027": "20"}}, {"by_year": {"2026": "10", "2027": "20"}}]
        got = t._uniform_schedule(rows)
        self.assertEqual(got, {"2026": "10", "2027": "20"})
        got["2026"] = "x"
        self.assertEqual(rows[0]["by_year"]["2026"], "10", "общий график обязан быть копией")

    def test_different_schedules_give_none(self):
        self.assertIsNone(t._uniform_schedule([{"by_year": {"2026": "10"}}, {"by_year": {"2026": "11"}}]))

    def test_no_rows_give_none(self):
        self.assertIsNone(t._uniform_schedule([]))


if __name__ == "__main__":
    unittest.main()
