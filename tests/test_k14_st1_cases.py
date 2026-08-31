"""`K14` #35, шаг 5 — приёмочные кейсы СТ-1 / ТН ВЭД и их оракул. Офлайн, без Qdrant и LLM.

ЗАЧЕМ. До этого набора вторая по частоте претензия июля (**29 упоминаний**) не проверялась
стандартным замером вовсе — ровно та дыра, которую `EV21` #119 закрыл для кластера жалоб №1.
Оба дефекта `K15` нашлись уже ПОСЛЕ выкатки именно потому, что адресные живые прогоны делались
руками и ровно столько раз, сколько я про них вспомнил.

⚠⚠ ОРАКУЛ НЕЗАВИСИМ ОТ РАНТАЙМА: он не зовёт `app.rag.st1_ref`. Позови — и снятие лукапа сделало
бы метрику зелёной (класс `O3` #104). Таблица фактов читается только как ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ
ожиданий: кейс с числом, которого в Перечне нет, обязан ОСТАНОВИТЬ замер, а не красить верные
ответы в красный.

⚠ Каждое утверждение проверено мутацией.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
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

import eval_documents  # noqa: E402
import eval_st1_cases as oracle  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_golden.json"


def st1_cases() -> list[dict]:
    return [c for c in json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
            if c.get("kind") == "st1"]


class TestCasesAreGroundedInTheSource(unittest.TestCase):
    """Ожидания набора сверены с ПЕРВОИСТОЧНИКОМ, а не с моей памятью."""

    def test_set_has_the_cases(self):
        cases = st1_cases()
        self.assertEqual(len(cases), 8, "шаг 5 требовал 5–10 приёмочных кейсов")
        # Обе половины оси обязаны быть представлены: позиция ИЗ Перечня и позиция ВНЕ него.
        self.assertTrue(any(c["in_perechen"] for c in cases))
        self.assertTrue(any(c["in_perechen"] is False for c in cases))

    def test_expectations_match_the_perechen(self):
        oracle.check_st1_reference(st1_cases())  # не бросает — значит сошлось

    def test_control_stops_on_a_number_absent_from_the_perechen(self):
        # ⚠ Мутация самого контроля: без неё «сошлось» неотличимо от «не проверяю».
        bad = [{"id": 51, "kind": "st1", "st1_code": "8403", "in_perechen": True,
                "expect_numbers": ["70"], "expect_keywords": []}]
        with self.assertRaises(SystemExit):
            oracle.check_st1_reference(bad)

    def test_control_stops_when_presence_is_misdeclared(self):
        for code, claimed in (("8402", True), ("8403", False)):
            with self.subTest(code=code):
                bad = [{"id": 1, "kind": "st1", "st1_code": code, "in_perechen": claimed,
                        "expect_numbers": [], "expect_keywords": []}]
                with self.assertRaises(SystemExit):
                    oracle.check_st1_reference(bad)

    def test_neighbour_case_is_the_point_of_the_axis(self):
        """8402 вне Перечня, сосед 8403 внутри — на этом и ловится подмена условия."""
        table = oracle.load_table()
        self.assertIsNone(oracle._condition_of(table, "8402"))
        self.assertIsNotNone(oracle._condition_of(table, "8403"))


class TestCasesReachTheBranchThatCanAnswerThem(unittest.TestCase):
    """⚠⚠ Кейс, не дошедший до своей ветки, меряет не то, ради чего заведён.

    Урок `#120` в чистом виде: тема сама по себе ничего не гарантирует, если вопрос не попал на
    ветку, где тема применяется. На товарной ветке нет ни Соглашения СНГ, ни Приказа №14 — ни при
    каком качестве поиска, поэтому «условие не доехало» означало бы дефект МАРШРУТА, а читалось бы
    как дефект ответа.
    """

    def test_every_st1_case_takes_the_procedural_branch(self):
        from app.rag import procedural
        from app.tools.navigator import extract_okpd2

        for c in st1_cases():
            with self.subTest(id=c["id"]):
                q = c["query"]
                self.assertTrue(procedural.is_procedural(q, has_code=bool(extract_okpd2(q))), q)

    def test_sweep_counts_them_as_procedural(self):
        """⚠ Находка добавления кейсов: в свипе они лежат в ТОВАРНОМ наборе `eval_golden.json`.

        Без своей ветки в `_class_of` все восемь считались бы ЛОЖНЫМИ срабатываниями, и главный
        предохранитель правки маркеров покраснел бы на ВЕРНОМ поведении. Он и покраснел.
        """
        import eval_routing

        rows = [r for r in eval_routing.collect()
                if r["set"] == "golden" and r["id"] in {c["id"] for c in st1_cases()}]
        self.assertEqual(len(rows), 8)
        self.assertEqual({r["class"] for r in rows}, {"процедурный"})


class TestOracleReadsTheAnswer(unittest.TestCase):
    """`st1_row` — что кейс обязан показать в тексте ответа."""

    CASE_8501 = {"id": 52, "kind": "st1", "st1_code": "8501", "in_perechen": True,
                 "expect_numbers": ["50", "10"], "expect_keywords": [["стоимост"], ["8503"]],
                 "expect_general_rule": False, "expect_sources": ["sng_origin_rules"]}

    FULL = ("Согласно Соглашению СНГ о правилах определения страны происхождения, для позиции "
            "8501 стоимость всех используемых материалов не должна превышать 50% цены конечной "
            "продукции; материалы позиции 8503 — в пределах 10%.")

    def test_complete_answer_passes(self):
        row = oracle.st1_row(self.CASE_8501, self.FULL)
        self.assertTrue(row["condition_ok"])
        self.assertEqual(row["named_sources"], ["sng_origin_rules"])

    def test_truncated_answer_fails_on_the_missing_limit(self):
        # ⚠ Именно этот класс кейс и ловит: ответ назвал 50 % и умолчал про подпредел 10 % —
        # заявителю недостающая оговорка меняет расчёт.
        half = ("Согласно Соглашению СНГ, стоимость используемых материалов не должна превышать "
                "50% цены конечной продукции.")
        row = oracle.st1_row(self.CASE_8501, half)
        self.assertFalse(row["condition_ok"])
        self.assertIn("10", row["missing_numbers"])

    def test_percent_spacing_does_not_decide(self):
        """«50 %» и «50%» — одно утверждение; пробел не должен ронять метрику.

        ⚠ Держится это НЕ нормализацией, а формой ожидания: величины записаны голыми числами
        («50»), а не «50%». Первая редакция теста приписывала заслугу `_norm` и потому пережила
        мутацию, снимавшую нормализацию, — то есть проверяла не то, что называла. Оставлен как
        утверждение о ФОРМЕ ОЖИДАНИЙ: привяжи кто-нибудь величину к знаку процента — упадёт здесь.
        """
        spaced = self.FULL.replace("50%", "50 %").replace("10%", "10 %")
        self.assertTrue(oracle.st1_row(self.CASE_8501, spaced)["condition_ok"])
        for c in st1_cases():
            for num in c.get("expect_numbers") or []:
                with self.subTest(id=c["id"], num=num):
                    self.assertNotIn("%", num, "величина ожидания привязана к знаку процента")

    def test_normalization_survives_a_line_break(self):
        """А вот ЧТО держит `_norm`: многословный признак, разорванный переносом строки.

        Ответы модели переносят строки где угодно; без склейки пробелов такой признак терялся бы.
        """
        case = {"id": 900, "kind": "st1", "st1_code": None, "in_perechen": None,
                "expect_numbers": [], "expect_keywords": [["цены конечной продукции"]],
                "expect_general_rule": False, "expect_sources": []}
        broken = "стоимость материалов не должна превышать 50% цены\n   конечной продукции"
        self.assertTrue(oracle.st1_row(case, broken)["condition_ok"])

    def test_general_rule_is_required_only_where_expected(self):
        case_rule = {"id": 53, "kind": "st1", "st1_code": "8402", "in_perechen": False,
                     "expect_numbers": [], "expect_keywords": [],
                     "expect_general_rule": True, "expect_sources": []}
        good = ("Позиция 8402 в Перечень не включена, поэтому действует общее правило: изменение "
                "товарной позиции по ТН ВЭД на уровне любого из первых четырёх знаков.")
        self.assertTrue(oracle.st1_row(case_rule, good)["general_rule_ok"])
        # Подмена условием соседа общего правила НЕ содержит — так дефект и ловится.
        leaked = ("Для позиции 8402 стоимость всех используемых материалов не должна превышать "
                  "50% цены конечной продукции.")
        self.assertFalse(oracle.st1_row(case_rule, leaked)["general_rule_ok"])
        # ⚠ А там, где правило НЕ ожидается, его отсутствие не наказывается: у позиции ИЗ Перечня
        # общее правило не применяется, и требовать его значило бы штрафовать за верный ответ.
        self.assertIsNone(oracle.st1_row(self.CASE_8501, self.FULL)["general_rule_ok"])


class TestSourceRecognizersDistinguish(unittest.TestCase):
    """⚠⚠ Распознаватель обязан узнать СВОЙ документ и не узнать чужой (`K14`)."""

    def test_prikaz_52_no_longer_swallows_prikaz_14(self):
        p52 = eval_documents._SOURCE_RECOGNIZERS["tpp_order_52"]
        p14 = eval_documents._SOURCE_RECOGNIZERS["prikaz14_tpp"]
        # До правки голое `приказ\w*\s+ТПП` узнавало ОБА — метрика «источник назван» давала бы
        # ложно ПОЛОЖИТЕЛЬНОЕ на ответе, сославшемся не на тот документ.
        self.assertTrue(p52.search("Приказ ТПП РФ №52"))
        self.assertFalse(p52.search("Приказ ТПП РФ №14"))
        self.assertTrue(p14.search("согласно Приказу ТПП РФ № 14 от 01.03.2024"))
        self.assertFalse(p14.search("Приказ ТПП РФ №52"))

    def test_cross_collision_control_is_live(self):
        """Контроль обязан падать, если распознаватель узнаёт ЧУЖОЙ ярлык."""
        import re

        saved = eval_documents._SOURCE_RECOGNIZERS["tpp_order_52"]
        try:
            eval_documents._SOURCE_RECOGNIZERS["tpp_order_52"] = re.compile(
                r"приказ\w*\s+ТПП", re.I)  # прежняя, коллизионная форма
            with self.assertRaises(SystemExit):
                eval_documents.check_source_recognizers()
        finally:
            eval_documents._SOURCE_RECOGNIZERS["tpp_order_52"] = saved
        eval_documents.check_source_recognizers()  # восстановленная — проходит

    def test_new_sources_are_recognized_by_the_set(self):
        wanted = {s for c in st1_cases() for s in (c.get("expect_sources") or [])}
        self.assertTrue(wanted)
        eval_documents.check_source_recognizers(wanted)  # не бросает


def _rows(answers: dict[int, str]) -> list[dict]:
    """Строки замера по синтетическим ответам — сводка иначе исполнится впервые в ПЛАТНОМ прогоне."""
    rows = []
    for c in st1_cases():
        r = {"id": c["id"], "kind": "st1", "in_scope": True, "expected": "—", "query": c["query"],
             "n_claims": 0, "hallucinated": [], "faithful": True, "guard_flagged": False,
             "attributed": None, "coherent": None, "declined": False, "cited": False,
             "cjk": False, "low_relevance": False, "guard_phantom": []}
        r.update(oracle.st1_row(c, answers[c["id"]]))
        rows.append(r)
    return rows


GOOD = {
    51: "Соглашение СНГ о правилах определения страны происхождения: стоимость всех используемых "
        "материалов не должна превышать 50% цены конечной продукции.",
    52: "Соглашение СНГ: стоимость материалов не более 50% цены; материалы позиции 8503 — в "
        "пределах 10%.",
    53: "Позиция 8402 в Перечень не включена — действует общее правило: изменение товарной позиции "
        "на уровне первых четырёх знаков (Соглашение СНГ о стране происхождения).",
    54: "Соглашение СНГ: стоимость материалов не более 50%, плюс технологические операции, включая "
        "контрольные испытания.",
    55: "Сертификат выдаёт уполномоченная ТПП по Приказу ТПП РФ № 14.",
    56: "Кумулятивный принцип: происхождение товара определяется с учётом операций в государствах "
        "— сторонах Соглашения СНГ.",
    57: "Да: для продукции вне приложения путь — сертификат СТ-1, подтверждающий происхождение "
        "товара.",
    58: "Соглашение СНГ: критерий — изменение товарной позиции на уровне любого из первых четырёх "
        "знаков.",
}


class TestMetricMovesInBothDirections(unittest.TestCase):
    """⚠⚠ «1.00» имеет смысл, только если метрика УМЕЕТ показать не 1.00.

    Сводка иначе впервые исполняется в платном прогоне, а её числа впервые читаются там же —
    то есть первая же ошибка в ней обнаружилась бы за деньги и после выкатки.
    """

    def test_ideal_answers_give_one(self):
        import eval_answers

        text = "\n".join(eval_answers.summarize(_rows(GOOD), 5))
        # ⚠ Знаменатель 3, а не 6: находка 7 шестого раунда сузила его до позиций ИЗ Перечня.
        self.assertIn("Условие Перечня доехало = 3/3 = 1.00", text)
        self.assertIn("Общее правило сформулировано = 2/2 = 1.00", text)
        self.assertIn("Источник назван = 7/7 = 1.00", text)

    def test_broken_answers_drop_every_metric(self):
        import eval_answers

        bad = dict.fromkeys(GOOD, "Не могу ответить на этот вопрос.")
        text = "\n".join(eval_answers.summarize(_rows(bad), 5))
        self.assertIn("Условие Перечня доехало = 0/3 = 0.00", text)
        self.assertIn("Общее правило сформулировано = 0/2 = 0.00", text)
        self.assertIn("Источник назван = 0/7 = 0.00", text)

    def test_the_neighbour_substitution_is_caught(self):
        """Подмена условия соседа: 8402 получает адвалорный предел 8403 — метрика обязана упасть."""
        import eval_answers

        leaked = dict(GOOD)
        leaked[53] = ("Для позиции 8402 стоимость всех используемых материалов не должна "
                      "превышать 50% цены конечной продукции (Соглашение СНГ).")
        text = "\n".join(eval_answers.summarize(_rows(leaked), 5))
        self.assertIn("Общее правило сформулировано = 1/2 = 0.50", text)


if __name__ == "__main__":
    unittest.main()
