r"""Детерминизм меряет РЕШАЮЩИЕ числа, а не полноту упоминаний — `EV10` (#91).

ЧТО БЫЛО. Метрика «стабильный набор чисел» считала ЛЮБОЕ число ответа и давала 0.60–0.90 на
НЕИЗМЕННОМ коде при пороге гайда ≥0.95. Разбор нестабильных запросов показал, что она меряет не
воспроизводимость решения, а полноту упоминаний: у бульдозеров между прогонами расходятся `0.1`
и `0.3` — проценты затрат на НИОКР, названные в ПРОЗЕ операции. Порог и баллы при этом совпадают
во всех прогонах, а решение эксперт принимает именно по ним.

ЧТО СТАЛО. Главная величина — **доля стабильных РЕШАЮЩИХ чисел**: порог позиции и баллы операций,
взятые из СТРУКТУРНЫХ полей. Числовая доля, а не доля запросов: на бинарной метрике из 10 запросов
порог ≥0.95 недостижим по построению (9/10 = 0.90, то есть он требует ровно 10 из 10). Это же было
записано в `EVAL_GUIDE` 16.08: «позиция с шестьюдесятью операциями и позиция с двумя весят
одинаково».

⚠⚠ ЗАКУПОЧНЫЙ ПОРОГ ИЗ РЕШАЮЩИХ ИСКЛЮЧЁН, И ЭТО ЗАМЕР. Он отвечает на ДРУГОЙ вопрос («для целей
осуществления закупок») и печатается отдельной строкой со своим условием. Разбор: у «одноразовых
медицинских масок» между прогонами плавали ровно 80 / 100 / 40 / 50 / 60 — весь закупочный график
прим. 53, — тогда как ОБЩИЙ порог «не менее 25 баллов» назывался во всех прогонах без исключения.
Включая закупочный, метрика штрафовала ответ за то, что он не пересказал ответ на вопрос, которого
не задавали. Различение то же, что ввела `K2`.

РЕЗУЛЬТАТ (три прогона по 5 повторов, кейсы выключены): **1.00 / 1.00 / 0.93 = 0.98 ± 0.03** при
пороге ≥0.95. До разделения та же серия давала 0.92 / 0.92 / 0.92.

Запуск:  .venv\Scripts\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_determinism import decisive_numbers  # noqa: E402
from tests.test_rag import make_hit  # noqa: E402


class TestDecisiveNumbers(unittest.TestCase):
    def test_own_threshold_counts(self):
        h = make_hit(product_name="Позиция", min_threshold="не менее 300 баллов")
        self.assertIn("300", decisive_numbers(h))

    def test_operation_points_count(self):
        h = make_hit(product_name="Позиция", requirement_blocks=[
            {"operations": [{"text": "литьё", "points": 7},
                            {"text": "сварка", "points": 12}]}])
        self.assertEqual(decisive_numbers(h), {"7", "12"})

    def test_prose_numbers_inside_operation_text_are_not_decisive(self):
        """⚠ Ровно тот случай, ради которого метрика разделена: 0,3 % затрат на НИОКР у
        бульдозеров — число в ПРОЗЕ операции, эксперт решения по нему не принимает."""
        h = make_hit(product_name="Позиция", requirement_blocks=[
            {"operations": [{"text": "объем затрат на НИОКР составляет 0,3 процента",
                             "points": None}]}])
        self.assertEqual(decisive_numbers(h), set())

    def test_procurement_threshold_is_excluded(self):
        """Закупочный порог — ДРУГОЙ вопрос; его наличие в ответе не про воспроизводимость.

        Берём реальную позицию корпуса: у «Медицинских масок» общий порог 25 баллов, а
        закупочный (прим. 53) несёт 80 / 100 / 40 / 50 / 60 — именно они и плавали."""
        from app.rag.thresholds import lookup_procurement_threshold

        name = ("Медицинские маски (за исключением полумасок фильтрующих классов защиты "
                "FFP1, FFP2, FFP3)")
        codes = ["13.95.10.190", "14.12.30.190", "14.19.32.120", "32.50.50.190", "32.99.11.160"]
        h = make_hit(product_name=name, okpd2_codes=codes, section_roman="VII",
                     min_threshold=None)
        proc = lookup_procurement_threshold(codes, name, "VII")
        # ⚠ Без этой проверки тест был бы фикцией: пустой закупочный порог проходит любую
        # проверку исключения. Первая редакция уходила в skip именно из-за неверного кода.
        self.assertTrue(proc and "80" in proc,
                        "фикстура потеряла закупочный порог — тест перестал что-либо проверять")
        dec = decisive_numbers(h)
        self.assertFalse({"80", "100", "40", "50", "60"} & dec,
                         "закупочный график снова считается решающим числом")

    def test_note_threshold_counts_when_own_is_empty(self):
        """Порог, добираемый рантаймом из примечаний, — такое же решающее число, как свой."""
        h = make_hit(product_name="Лифты пассажирские", okpd2_codes=["28.22.16.111"],
                     section_roman="VI", min_threshold=None)
        self.assertIn("120", decisive_numbers(h), "порог прим. 18 не попал в решающие")

    def test_inherited_points_used_when_no_own_operations(self):
        """`R6`: своих операций нет — решающими становятся баллы ГРУППЫ, их же показывает контекст."""
        h = make_hit(product_name="Наследник", requirement_blocks=[])
        parent = {"min_threshold": "не менее 90 баллов",
                  "operations": [{"text": "сборка", "points": 5}]}
        self.assertEqual(decisive_numbers(h, parent), {"90", "5"})

    def test_no_target_gives_empty_set(self):
        self.assertEqual(decisive_numbers(None), set())


if __name__ == "__main__":
    unittest.main()
