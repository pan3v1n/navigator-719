"""Лукап условий достаточной переработки по ТН ВЭД (`K14` #35, `app/rag/st1_ref.py`).

⚠⚠ ГЛАВНОЕ, ЧТО ЗАКРЕПЛЯЕТСЯ. Перечень — СПИСОК ИСКЛЮЧЕНИЙ, поэтому «кода нет в Перечне» — это
не пустой результат, а ПОЛНОЦЕННЫЙ ответ (действует общее правило). Отсюда следует неприятное:
ЛЮБАЯ ошибка лукапа даёт не пустоту, а правдоподобное и НЕВЕРНОЕ условие. Ни один внешний
признак его не отличает — только эти тесты.

Все проверки офлайн: таблица лежит в репозитории, сети и модели не нужно.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag import st1_ref as S  # noqa: E402


def row(code, condition="Изготовление из материалов любых позиций", name="Товар",
        code_to=None, partial=False, excluded=False, editions=None):
    r = {"code": code, "code_to": code_to, "partial": partial,
         "name": name, "condition": condition, "excluded": excluded}
    if editions:
        r["editions"] = editions
    return r


class _WithTable(unittest.TestCase):
    """Подменяем таблицу целиком: тесты о ПОВЕДЕНИИ лукапа, а не о содержимом Соглашения."""

    ROWS: tuple = ()

    def setUp(self):
        self._p = mock.patch.object(S, "_rows", lambda: self.ROWS)
        self._p.start()
        self.addCleanup(self._p.stop)


class TestMissingCodeMeansGeneralRule(_WithTable):
    """⚠⚠ САМЫЙ ЧАСТЫЙ СЛУЧАЙ И САМЫЙ ОПАСНЫЙ ДЛЯ ТОЛКОВАНИЯ. Товара нет в Перечне — значит
    действует ОБЩЕЕ правило, а не «условий не нашлось». Ответ «не нашли» толкнул бы эксперта
    искать несуществующее исключение."""

    ROWS = (row("8528"),)

    def test_absent_code_is_answered_by_the_general_rule(self):
        res = S.conditions_for("7318 15")
        self.assertFalse(res["matched"])
        self.assertIn("четырёх знаков", res["general_rule"])

    def test_context_block_says_explicitly_that_it_is_the_general_rule(self):
        """⚠ Модель обязана видеть РАЗНИЦУ между условием из Перечня и общим правилом. Без явной
        пометки она подаст второе как первое — и ответ станет неверным, оставаясь гладким."""
        t = S.format_for_context("7318 15")
        self.assertIn("НЕ включён", t)
        self.assertIn("общее правило", t)

    def test_present_code_is_not_answered_by_the_general_rule(self):
        """⚠ Обратная половина: лукап, всегда отдающий общее правило, прошёл бы тест выше."""
        res = S.conditions_for("8528 72 000 0")
        self.assertTrue(res["matched"])
        self.assertIn("включён в Перечень", S.format_for_context("8528 72 000 0"))


class TestMatchDirectionsMeanDifferentThings(_WithTable):
    """⚠⚠ ДВА НАПРАВЛЕНИЯ СОВПАДЕНИЯ НЕЛЬЗЯ СМЕШИВАТЬ. Запись шире запроса — условие применяется
    к спрошенному товару. Запись УЖЕ запроса — условие касается лишь ЧАСТИ спрошенного, и выдать
    его за ответ значит распространить правило на всю позицию."""

    ROWS = (row("8528", name="Мониторы"), row("852872", name="Мониторы ЖК", partial=True))

    def test_entry_broader_than_query_applies(self):
        res = S.conditions_for("8528 72 000 0")
        self.assertTrue(res["matched"])
        self.assertIn("8528", [r["code"] for r in res["exact"]])

    def test_entry_narrower_than_query_is_kept_apart(self):
        res = S.conditions_for("8528")
        self.assertIn("852872", [r["code"] for r in res["narrower"]])
        self.assertNotIn("852872", [r["code"] for r in res["exact"]])

    def test_narrower_entries_are_reported_when_nothing_matched(self):
        """⚠ Молчание о более узких кодах — потеря: эксперт применил бы общее правило там, где
        для его товара есть отдельное условие."""
        with mock.patch.object(S, "_rows", lambda: (row("852872", name="Мониторы ЖК"),)):
            t = S.format_for_context("8528")
            self.assertIn("более узких кодов", t)
            self.assertIn("852872", t)


class TestRangeRows(_WithTable):
    ROWS = (row("8702", code_to="8704", name="Автомобили", partial=True),)

    def test_code_inside_range_matches(self):
        self.assertTrue(S.conditions_for("8704")["matched"])
        self.assertTrue(S.conditions_for("8703")["matched"])

    def test_code_outside_range_does_not_match(self):
        self.assertFalse(S.conditions_for("8705")["matched"])
        self.assertFalse(S.conditions_for("8701")["matched"])

    def test_range_is_printed_whole(self):
        """⚠⚠ Первая редакция печатала только `code`: строка «из 8702 - 8704» показывалась как
        «позиция 8702», и на запрос про 8704 эксперт видел условие ЧУЖОЙ позиции. Условие верное,
        адресация неверная — самый дорогой класс дефекта в этом проекте."""
        t = S.format_for_context("8704")
        self.assertIn("8702 - 8704", t)


class TestPartialFlagIsSurfaced(_WithTable):
    """⚠ Прим. 1.1 Перечня: предлог «из» сужает применение до ЧАСТИ товаров позиции. Не сказав
    этого, ответ распространит условие на всю позицию."""

    ROWS = (row("6804", partial=True, name="Изделия из абразивов"),)

    def test_warning_about_partial_scope_is_present(self):
        t = S.format_for_context("6804")
        self.assertIn("ЧАСТИ товаров", t)
        self.assertIn("сверьте наименование", t)

    def test_full_scope_row_has_no_such_warning(self):
        """⚠ Обратная половина: предупреждение «всегда» обесценивает себя."""
        with mock.patch.object(S, "_rows", lambda: (row("6804", partial=False),)):
            self.assertNotIn("ЧАСТИ товаров", S.format_for_context("6804"))


class TestEdgeCases(_WithTable):
    ROWS = (row("8528"), row("8803", excluded=True, name="", condition=""))

    def test_group_code_is_refused_not_guessed(self):
        """⚠ «85» — это ГРУППА, а Перечень ведётся по позициям. Ответить общим правилом значило
        бы утверждать то, о чём не спрашивали."""
        res = S.conditions_for("85")
        self.assertTrue(res.get("too_short"))
        self.assertIn("не короче четырёх знаков", S.format_for_context("85"))

    def test_excluded_row_never_supplies_conditions(self):
        """⚠ Исключённая позиция — не «условие пустое», а возврат к ОБЩЕМУ правилу."""
        self.assertTrue(S.is_excluded("8803"))
        res = S.conditions_for("8803")
        self.assertFalse(res["matched"])
        self.assertEqual(res["exact"], [])

    def test_empty_and_garbage_input(self):
        for bad in (None, "", "не код", "12"):
            res = S.conditions_for(bad)
            self.assertFalse(res["matched"])


class TestMissingTableIsDistinguishable(unittest.TestCase):
    """⚠⚠ «ТАБЛИЦЫ НЕТ» И «КОДА НЕТ В ПЕРЕЧНЕ» СНАРУЖИ ОДИНАКОВЫ — оба дают общее правило.
    Первое это дефект развёртывания (файл не доехал в архив: так уже было с `classifiers/*.tsv`
    в T9 и с `inherited_requirements.json`), второе — законный ответ. Различать обязан код."""

    def test_available_is_false_without_table(self):
        with mock.patch.object(S, "_rows", lambda: ()):
            self.assertFalse(S.is_available())
            self.assertEqual(S.format_for_context("8528"), "")

    def test_available_is_true_with_the_real_table(self):
        """⚠ Положительный контроль: проверка выше зелена и при полностью сломанном лукапе."""
        S._rows.cache_clear()
        S._data.cache_clear()
        self.assertTrue(S.is_available(), "таблица K14 не читается — проверьте сборку")


class TestRealTableAnswers(unittest.TestCase):
    """Несколько ответов на НАСТОЯЩЕЙ таблице — она в репозитории, значит проверяема офлайн."""

    def setUp(self):
        S._rows.cache_clear()
        S._data.cache_clear()
        if not S.is_available():
            self.skipTest("таблица не сгенерирована")

    def test_industrial_code_is_found(self):
        """8407 — двигатели внутреннего сгорания: промышленная позиция, ради которых заход."""
        self.assertTrue(S.conditions_for("8407 34 100 0")["matched"])

    def test_source_is_named(self):
        self.assertIn("СНГ", S.format_for_context("8407 34 100 0"))


if __name__ == "__main__":
    unittest.main()
