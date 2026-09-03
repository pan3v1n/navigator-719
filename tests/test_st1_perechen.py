"""Разбор Перечня условий Соглашения СНГ (`K14` #35, `scripts/convert_st1_perechen.py`).

⚠⚠ ЗАЧЕМ ТЕСТИРОВАТЬ ЭТОТ ПАРСЕР ОСОБЕННО ТЩАТЕЛЬНО. Перечень — СПИСОК ИСКЛЮЧЕНИЙ из общего
правила: товар, которого в таблице НЕТ, получает общее правило («изменение товарной позиции на
уровне первых четырёх знаков»). Значит ЛЮБАЯ потеря строки при разборе не даёт пустого ответа —
она даёт ДРУГОЙ, правдоподобный и неверный. Снаружи это неотличимо от нормальной работы; поймать
можно только здесь.

ЧТО ЗАКРЕПЛЕНО (каждый пункт куплен реальным дефектом этого разбора, а не придуман):
1. Список кодов через запятую даёт НЕСКОЛЬКО записей. Первая редакция брала первый код и молча
   теряла остальные — «из 6804, из 6805» превращалось в одну запись.
2. Отменённая позиция («8803 - Исключена.») распознаётся, а НЕ приклеивается к условию соседа.
   Проверено на исходнике: хвост попадал в конец условия позиции 8704.
3. Предлог «из» сохраняется флагом: он сужает применение условия до ЧАСТИ товарной позиции
   (прим. 1.1 Перечня). Потеря флага отдала бы условие всей позиции.
4. Продолжение многострочной ячейки и пометка о редакции относятся КО ВСЕМ записям своей строки
   таблицы, а не только к последней.
5. Положительные контроли ПАДАЮТ на пустом и на подменённом входе: «ноль записей» и «парсер
   ослеп» снаружи неразличимы.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import convert_st1_perechen as P  # noqa: E402

HEAD = "ПЕРЕЧЕНЬ УСЛОВИЙ, ПРОИЗВОДСТВЕННЫХ И ТЕХНОЛОГИЧЕСКИХ ОПЕРАЦИЙ\n"


def doc(*lines: str) -> str:
    return HEAD + "Код ТН ВЭД|Наименование товара |Условия |\n1 |2 |3 |\n" + "\n".join(lines)


class TestRowForms(unittest.TestCase):
    def test_plain_heading(self):
        rows, forms = P.parse(doc("0201 |Мясо |Изготовление из товаров группы 01 |"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "0201")
        self.assertFalse(rows[0]["partial"])
        self.assertEqual(forms["exact4"], 1)

    def test_ten_digit_code_keeps_all_digits(self):
        """⚠ Пробелы внутри кода — оформление, а не разделитель: `0710 40 000` это один код."""
        rows, _ = P.parse(doc("0710 40 000 |Кукуруза |Изготовление из кукурузы |"))
        self.assertEqual(rows[0]["code"], "071040000")

    def test_from_prefix_is_kept_as_flag(self):
        """⚠ «из» сужает применение до ЧАСТИ позиции (прим. 1.1). Потеряв флаг, лукап отдал бы
        условие всей товарной позиции — правильное правило, привязанное не к тому товару."""
        rows, forms = P.parse(doc("из 0901 |Кофе жареный |Изготовление из материалов |"))
        self.assertEqual(rows[0]["code"], "0901")
        self.assertTrue(rows[0]["partial"])
        self.assertEqual(forms["from"], 1)

    def test_range_is_kept(self):
        rows, forms = P.parse(doc("1504 - 1506 00 000|Жиры |Изготовление |"))
        self.assertEqual((rows[0]["code"], rows[0]["code_to"]), ("1504", "150600000"))
        self.assertEqual(forms["range"], 1)


class TestCommaListIsNotTruncated(unittest.TestCase):
    """⚠⚠ ПЕРВАЯ РЕДАКЦИЯ ТЕРЯЛА ВТОРОЙ КОД МОЛЧА. Запись при этом выглядела целой: имя и условие
    на месте, просто товаров стало вдвое меньше. На исходнике это стоило 33 записей из 244."""

    def test_two_codes_give_two_records_with_the_same_condition(self):
        rows, _ = P.parse(doc("из 6804, из 6805 |Изделия из абразивов |Изготовление из материалов |"))
        self.assertEqual([r["code"] for r in rows], ["6804", "6805"])
        self.assertEqual(rows[0]["condition"], rows[1]["condition"])
        self.assertTrue(all(r["partial"] for r in rows))

    def test_single_code_still_gives_one_record(self):
        """⚠ Обратная половина: разбор, всегда дробящий строку, ломал бы обычные записи."""
        rows, _ = P.parse(doc("8528 |Мониторы |Изготовление |"))
        self.assertEqual(len(rows), 1)


class TestExcludedRowDoesNotContaminateNeighbour(unittest.TestCase):
    """⚠⚠ САМЫЙ ДОРОГОЙ ИЗ НАЙДЕННЫХ ДЕФЕКТОВ. «8803 - Исключена.» не совпадало с шаблоном (там
    нет `|` сразу за кодом) и уходило в ветку «продолжение ячейки», ПРИКЛЕИВАЯСЬ к условию
    ПРЕДЫДУЩЕЙ записи. Условие соседа становилось неверным, оставаясь правдоподобным."""

    SRC = doc("8704 |Транспортные средства |Изготовление, при котором стоимость 50 % |",
              "8803 - Исключена.|",
              "8805 |Стартовое оборудование |Изготовление из материалов |")

    def test_excluded_is_its_own_record(self):
        rows, forms = P.parse(self.SRC)
        excl = [r for r in rows if r["excluded"]]
        self.assertEqual(len(excl), 1)
        self.assertEqual(excl[0]["code"], "8803")
        self.assertEqual(forms["excluded"], 1)

    def test_neighbour_condition_is_clean(self):
        rows, _ = P.parse(self.SRC)
        prev = next(r for r in rows if r["code"] == "8704")
        self.assertNotIn("Исключена", prev["condition"])
        self.assertNotIn("8803", prev["condition"])


class TestContinuationAndEditionCoverWholeRow(unittest.TestCase):
    """⚠ Продолжение ячейки и пометка о редакции — свойства СТРОКИ ТАБЛИЦЫ. Если строка дала
    несколько записей, дописывать только в последнюю значит оставить остальные с ОБРЕЗАННЫМ
    условием — снаружи оно выглядит законченным."""

    SRC = doc("из 6804, из 6805 |Изделия |Изготовление из материалов любой позиции, |",
              "за исключением материалов позиций 6804 и 6805 |",
              "(в ред. Протокола от 31.05.2019)|")

    def test_continuation_reaches_every_record_of_the_row(self):
        rows, _ = P.parse(self.SRC)
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertIn("за исключением материалов позиций 6804 и 6805", r["condition"])

    def test_edition_reaches_every_record_of_the_row(self):
        rows, _ = P.parse(self.SRC)
        for r in rows:
            self.assertEqual(r.get("editions"), ["Протокола от 31.05.2019"])


class TestControlsRefuseEmptyParse(unittest.TestCase):
    """⚠⚠ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ — НЕСУЩАЯ ЧАСТЬ, А НЕ УКРАШЕНИЕ. Пустая таблица не даёт пустых
    ответов: лукап начинает отвечать «в Перечне нет» про КАЖДЫЙ товар, то есть всегда возвращает
    общее правило. Это выглядит как исправная работа."""

    def test_empty_parse_is_refused(self):
        rows, forms = P.parse(doc(""))
        problems = P.controls(rows, forms)
        self.assertTrue(problems)

    def test_each_missing_form_is_named(self):
        """⚠ Отрицательный контроль: контроль, молчащий при исчезнувшей форме, бесполезен."""
        rows = [{"code": "0201", "code_to": None, "partial": False, "name": "х",
                 "condition": "у" * 20, "excluded": False}] * 200
        forms = {"exact4": 200, "exact6": 0, "exact10": 0, "from": 0, "range": 0,
                 "excluded": 0, "editions": 0, "continuation": 0}
        problems = " ".join(P.controls(rows, forms))
        for expected in ("десятизначные", "из CODE", "диапазоны", "редакции", "Исключена"):
            self.assertIn(expected, problems)


class TestGeneratedTableIsSane(unittest.TestCase):
    """Проверка СГЕНЕРИРОВАННОГО файла, если он есть в дереве. ⚠ Офлайн: исходник СНГ в
    репозиторий не едет, а таблица едет — она и есть то, чем пользуется рантайм."""

    PATH = ROOT / "knowledge_base" / "classifiers" / "tnved_st1_conditions.json"

    def setUp(self):
        if not self.PATH.exists():
            self.skipTest("таблица ещё не сгенерирована (scripts/convert_st1_perechen.py)")
        self.data = json.loads(self.PATH.read_text(encoding="utf-8"))

    def test_key_type_and_general_rule_are_recorded(self):
        """⚠ Общее правило обязано ехать вместе с таблицей: без него «кода нет в Перечне»
        нечем ответить, а это САМЫЙ ЧАСТЫЙ случай."""
        self.assertEqual(self.data["key_type"], "ТН ВЭД")
        self.assertIn("четырёх знаков", self.data["general_rule"])

    def test_industrial_sections_are_present(self):
        """⚠ Ради них K14 и делается: 719 — про промышленную продукцию."""
        rows = self.data["rows"]
        industrial = [r for r in rows if r["code"][:2] in {"84", "85", "86", "88", "90", "94"}]
        self.assertGreaterEqual(len(industrial), 40)

    def test_codes_are_digits_only(self):
        for r in self.data["rows"]:
            self.assertTrue(r["code"].isdigit(), f"код не нормализован: {r['code']!r}")
            if r["code_to"]:
                self.assertTrue(r["code_to"].isdigit())


if __name__ == "__main__":
    unittest.main()
