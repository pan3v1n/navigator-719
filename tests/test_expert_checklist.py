"""Лист сверки для эксперта: защита от молчаливой порчи списка (офлайн).

Список блока А строится ДИФФОМ двух сборок карты наследования — текущим ключом и ключом до
фикса D6. Это единственный способ ответить «что добавил фикс», не переписывая число руками
(прежняя запись «46 позиций» именно так и разошлась с корпусом — на деле 44).

Риск у приёма ровно один и он тихий: если правило ключа поменяется ещё раз, `_old_map_key`
перестанет быть «ключом до фикса» и станет «ключом позапрошлой версии». Дифф при этом
продолжит считаться и выдаст правдоподобный, но неверный список — а эксперт сверит не то.
Отсюда первые два теста.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


checklist = _load("build_expert_checklist", "scripts/build_expert_checklist.py")
diag = checklist.diag

# Имя-перечень из таблицы XVIII — на нём фикс и обнаружился: после снятия сноски остаётся « ;».
LIST_NAME = "Оборудование системы опознавания судов <9>;\nкодирующее устройство"

NAMES = [
    LIST_NAME,
    "Компас магнитный <9>;",
    "Этилен.\nЭта группировка включает этилен чистотой менее 95 процентов",
    "Круги шлифовальные",
    "Хладон-218 (октафторпропан),",
    "Суда обслуживающего флота.",
]


class TestHistoricalKey(unittest.TestCase):
    """`_old_map_key` обязан отличаться от текущего ровно на хвостовую пунктуацию."""

    def test_differs_only_by_trailing_punctuation(self):
        for name in NAMES:
            with self.subTest(name=name[:40]):
                old = checklist._old_map_key("XVIII", name)
                new = diag._map_key("XVIII", name)
                sec, _, tail = old.partition("|")
                self.assertEqual(f"{sec}|{tail.strip(';,.').strip()}", new)

    def test_the_fix_actually_changes_something(self):
        """Иначе дифф пуст, а список блока А тихо становится нулевым."""
        self.assertNotEqual(
            checklist._old_map_key("XVIII", LIST_NAME),
            diag._map_key("XVIII", LIST_NAME),
        )

    def test_current_key_matches_runtime(self):
        """Карту строит скрипт, а читает рантайм — расхождение = молчаливый отказ наследования."""
        from app.rag import inheritance
        for name in NAMES:
            with self.subTest(name=name[:40]):
                self.assertEqual(diag._map_key("XVIII", name), inheritance._key("XVIII", name))


class TestPlural(unittest.TestCase):
    """Документ уходит человеку: «44 позиции», а не «44 позиций»."""

    def test_forms(self):
        cases = {1: "1 позиция", 2: "2 позиции", 4: "4 позиции", 5: "5 позиций",
                 11: "11 позиций", 12: "12 позиций", 14: "14 позиций", 15: "15 позиций",
                 21: "21 позиция", 22: "22 позиции", 44: "44 позиции", 111: "111 позиций"}
        for n, expected in cases.items():
            with self.subTest(n=n):
                self.assertEqual(checklist._plural(n, "позиция", "позиции", "позиций"), expected)


class TestDamagedNameFallback(unittest.TestCase):
    """У повреждённых записей первая строка — обрывок ячейки, по нему позицию не опознать."""

    def test_short_head_gets_next_line(self):
        rec = {"product_name": "или\nналичие у юридического лица прав на документацию"}
        self.assertIn("наличие у юридического лица", checklist._name_full(rec))

    def test_colon_head_gets_next_line(self):
        rec = {"product_name": "до 1 января 2019 г.:\nналичие у юридического лица прав"}
        self.assertIn("наличие", checklist._name_full(rec))

    def test_normal_name_left_alone(self):
        rec = {"product_name": "Аппараты автоматического плазмафереза донорского\nвторая строка"}
        self.assertNotIn("вторая строка", checklist._name_full(rec))


if __name__ == "__main__":
    unittest.main()
