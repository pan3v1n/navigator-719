"""Доборщик порогов блока (`D9`): правила разбора и предохранители. Офлайн, без корпуса.

ЗАЧЕМ ТЕСТ. Скрипт правит САМ КОРПУС — то, из чего собирается ответ эксперту. Ошибка правила
здесь не падает, а тихо переписывает данные: порог уезжает не к тому узлу, вытесненное требование
исчезает, повторный прогон копит мусор. Поэтому проверяются не «функции», а ровно те свойства,
на которых держится доверие к правке: порог берётся дословно из первоисточника, приписывается
блоку с ТЕМ ЖЕ названием, ничего не удаляет и не повторяется при втором прогоне.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import backfill_block_thresholds as bf  # noqa: E402


class _Product:
    """Заглушка продукта: доборщику нужен только сырой текст строки-исходника."""

    def __init__(self, text: str):
        self._text = text

    def raw_block(self) -> str:
        return self._text


NODE_SRC = (
    "Модульная криогенная автозаправочная станция\n"
    "использование произведенного на территории Российской Федерации следующего оборудования, "
    "оцениваемого в совокупности суммарным количеством баллов не менее 180 баллов:\n"
    "криогенный насос низкого давления (не менее 100 баллов):\n"
    "производство насоса - механическая обработка, сборка (20 баллов);\n"
    "термоизолированный резервуар (емкость) для хранения (не менее 60 баллов):\n"
    "производство сосудов - раскрой, резка (20 баллов);\n"
)

SCALE_SRC = (
    "Контрабасы\n"
    "Изготовление инструментов:\n"
    "выполнение на территории Российской Федерации следующих операций для каждой единицы "
    "продукции, оцениваемых в совокупности суммарным количеством баллов, до 31 декабря 2020 г. - "
    "не менее 170 баллов, с 1 января 2021 г. - не менее 180 баллов:\n"
    "распиловка древесины (5 баллов);\n"
    "Изготовление смычков:\n"
    "выполнение на территории Российской Федерации следующих операций для каждой единицы "
    "продукции, оцениваемых в совокупности суммарным количеством баллов, до 31 декабря 2020 г. - "
    "не менее 80 баллов, с 1 января 2021 г. - не менее 90 баллов:\n"
    "строгание трости смычка (5 баллов);\n"
)

DISPLACED_SRC = (
    "Тренировочный пэд\n"
    "с 1 января 2023 г. соблюдение процентной доли стоимости использованных при производстве "
    "иностранных товаров - не более 15 процентов цены товара <4>.\n"
    "Выполнение на территории Российской Федерации следующих операций для каждой единицы "
    "продукции, оцениваемых в совокупности суммарным количеством баллов с 1 января 2023 г., - "
    "не менее 75 баллов:\n"
    "сборка пэда (5 баллов)\n"
)


def _rec(min_threshold, *components):
    return {
        "product_name": "Позиция",
        "min_threshold": min_threshold,
        "requirement_blocks": [
            {"component": c, "operations": [{"text": f"операция {i}", "points": 5}]}
            for i, c in enumerate(components, 1)
        ],
    }


class TestNodeThreshold(unittest.TestCase):
    """Правило A: «<узел> (не менее N баллов):» → порог блока с тем же названием."""

    def test_threshold_goes_to_its_own_block(self):
        rec = _rec("не менее 180 баллов", "криогенный насос низкого давления",
                   "термоизолированный резервуар (емкость) для хранения")
        new, changes, warns = bf.fix_record(rec, _Product(NODE_SRC))
        blocks = new["requirement_blocks"]
        self.assertEqual(blocks[0]["min_threshold"], "не менее 100 баллов")
        self.assertEqual(blocks[1]["min_threshold"], "не менее 60 баллов")
        self.assertEqual(len(changes), 2)
        self.assertEqual(warns, [])

    def test_position_threshold_untouched(self):
        """Порог позиции — не порог узла: правило A его не трогает."""
        rec = _rec("не менее 180 баллов", "криогенный насос низкого давления")
        new, _, _ = bf.fix_record(rec, _Product(NODE_SRC))
        self.assertEqual(new["min_threshold"], "не менее 180 баллов")

    def test_unknown_block_gets_nothing(self):
        """Блок, которого нет в источнике, порога не получает — иначе он уедет к чужому узлу."""
        rec = _rec("не менее 180 баллов", "блок, которого нет в первоисточнике")
        new, changes, _ = bf.fix_record(rec, _Product(NODE_SRC))
        self.assertIsNone(new["requirement_blocks"][0].get("min_threshold"))
        self.assertEqual(changes, [])


class TestBlockScale(unittest.TestCase):
    """Правило B: у вида работ своя шкала по годам, отдельная от шкалы позиции."""

    def test_each_block_gets_its_own_scale(self):
        rec = _rec(None, "Изготовление инструментов", "Изготовление смычков")
        new, changes, _ = bf.fix_record(rec, _Product(SCALE_SRC))
        self.assertIn("не менее 170 баллов", new["requirement_blocks"][0]["min_threshold"])
        self.assertIn("не менее 80 баллов", new["requirement_blocks"][1]["min_threshold"])
        self.assertNotIn("не менее 80", new["requirement_blocks"][0]["min_threshold"])
        self.assertEqual(len(changes), 2)


class TestDisplacedPositionThreshold(unittest.TestCase):
    """Правило C: порог позиции вытеснен другим требованием — вернуть, ничего не потеряв."""

    def test_scale_returns_and_displaced_requirement_survives(self):
        old = ("с 1 января 2023 г. соблюдение процентной доли стоимости использованных при "
               "производстве иностранных товаров - не более 15 процентов цены товара <4>.")
        rec = _rec(old, "Тренировочный пэд")
        new, changes, warns = bf.fix_record(rec, _Product(DISPLACED_SRC))
        self.assertIn("не менее 75 баллов", new["min_threshold"])
        texts = [o["text"] for o in new["requirement_blocks"][0]["operations"]]
        self.assertTrue(any("не более 15 процентов" in t for t in texts),
                        "вытесненное требование пропало из записи")
        self.assertEqual(warns, [])
        self.assertTrue(bf._preserved(rec, new)[0])

    def test_refuses_when_displaced_text_is_not_in_source(self):
        """Пересказ вместо цитаты — не трогаем: восстановить его дословно мы не умеем."""
        rec = _rec("порог, которого нет в первоисточнике дословно", "Тренировочный пэд")
        new, changes, warns = bf.fix_record(rec, _Product(DISPLACED_SRC))
        self.assertEqual(new["min_threshold"], "порог, которого нет в первоисточнике дословно")
        self.assertEqual(changes, [])
        self.assertEqual(len(warns), 1)

    def test_position_with_points_threshold_is_left_alone(self):
        """Если порог позиции уже балльный — правило C не применяется."""
        rec = _rec("не менее 75 баллов", "Тренировочный пэд")
        new, changes, _ = bf.fix_record(rec, _Product(DISPLACED_SRC))
        self.assertEqual(new["min_threshold"], "не менее 75 баллов")


class TestIdempotenceAndSafety(unittest.TestCase):
    """Скрипт правит корпус, поэтому второй прогон обязан быть пустым, а потеря — невозможной."""

    def test_second_run_changes_nothing(self):
        rec = _rec("не менее 180 баллов", "криогенный насос низкого давления")
        once, changes1, _ = bf.fix_record(rec, _Product(NODE_SRC))
        twice, changes2, _ = bf.fix_record(once, _Product(NODE_SRC))
        self.assertTrue(changes1)
        self.assertEqual(changes2, [])
        self.assertEqual(once, twice)

    def test_preservation_catches_lost_points(self):
        old = _rec("не менее 180 баллов", "узел")
        new = _rec("не менее 180 баллов", "узел")
        new["requirement_blocks"][0]["operations"][0]["points"] = None
        ok, why = bf._preserved(old, new)
        self.assertFalse(ok)
        self.assertIn("баллы", why)

    def test_preservation_catches_lost_operation(self):
        old = _rec(None, "узел")
        new = _rec(None, "узел")
        new["requirement_blocks"][0]["operations"] = []
        ok, why = bf._preserved(old, new)
        self.assertFalse(ok)

    def test_indent_detected_from_file(self):
        """Корпус хранится с разным отступом; чужой отступ превращает правку в перезапись файла."""
        self.assertEqual(bf._detect_indent('[\n {\n  "a": 1\n }\n]'), 1)
        self.assertEqual(bf._detect_indent('[\n  {\n    "a": 1\n  }\n]'), 2)


if __name__ == "__main__":
    unittest.main()
