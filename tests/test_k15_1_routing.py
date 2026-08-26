"""`K15-1` #120 — маршрут вопроса и гейт закрытого справочника. Офлайн, без Qdrant и LLM.

ЗАЧЕМ. `is_procedural` — развилка, после которой вопрос уже не встретит ни требований приложения,
ни корпуса Правил, смотря куда ушёл. Мерить её было нечем: `eval_rules` дёргает `search_rules`
НАПРЯМУЮ, минуя гейт, поэтому «атрибуция@1 0.96» три месяца была утверждением о качестве поиска
в корпусе, а не о попадании вопроса в этот корпус.

Что чинилось (замерено свипом `scripts/eval_routing.py`, радиус +3 / −0 / ложных 0):

* **асимметрия разрыва.** Три соседних шаблона задавали РАЗНЫЕ правила: у отглагольного
  существительного («внесение … в реестр») разрыв запрещён вовсе, у глагола («внести его в
  реестр») разрешён до четырёх слов. Из-за одного слова «нужно ли заключение ТПП для ВНЕСЕНИЯ
  ПРОДУКЦИИ В РЕЕСТР» уходило товарной веткой и получало 12.8 КБ требований к охранной
  сигнализации вместо норм;
* **разошедшиеся гейты блока и его содержимого.** Разъяснение про несуществующий документ
  зависело от ВОПРОСА, а сам справочник — от ТЕМЫ. На вопросе выше тема `registry_entry`, и
  справочника не было вовсе — ровно там, где риск выдумать документ максимален.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import documents_ref, procedural, topics  # noqa: E402

# Дословный вопрос кейса #50 приёмочного набора — он же критерий приёмки issue #120.
CASE_50 = "нужно ли заключение ТПП для внесения продукции в реестр"


def _reference_header() -> str:
    """Шапка блока справочника — из самой функции, чтобы утверждение не устарело молча."""
    return documents_ref.documents_context_block("").splitlines()[0]


class TestGapIsAllowedForEveryForm(unittest.TestCase):
    """Разрыв между словом внесения/включения и «в реестр» — один и тот же для ВСЕХ форм."""

    FORMS = ("внесение", "внесения", "внесении", "включение", "включения", "внести", "включить")
    GAPS = ("", "продукции ", "сведений о продукции ", "изменений в сведения ")

    def test_all_forms_with_all_gaps(self):
        for form in self.FORMS:
            for gap in self.GAPS:
                q = f"каков порядок {form} {gap}в реестр"
                with self.subTest(form=form, gap=gap.strip() or "<без разрыва>"):
                    self.assertTrue(procedural.is_procedural(q), q)

    def test_case_50_routes_procedurally(self):
        self.assertTrue(procedural.is_procedural(CASE_50),
                        "вопрос #120 снова уходит товарной веткой")

    def test_two_more_cases_of_the_procedural_golden_set(self):
        """Оба из набора `K9` — их промах и показал, что дефект не единичный."""
        for q in ("в каком случае откажут во внесении изменений в реестр",
                  "как формируется заявка на включение сведений в реестр"):
            with self.subTest(q=q[:40]):
                self.assertTrue(procedural.is_procedural(q))


class TestNarrowFormWouldBreakIt(unittest.TestCase):
    """⚠ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ: с прежним узким шаблоном тесты выше обязаны падать.

    Без него утверждение «маршрут починен» зелено и при откате правки — тест с одной
    положительной половиной не ловит снятие предохранителя (урок `O3` #104)."""

    def test_old_pattern_sends_case_50_to_the_product_branch(self):
        narrow = [p for p in procedural._STRONG_PATTERNS
                  if "внесени|включени|внести|включит" not in p]
        narrow += [r"внесени\w*\s+в\s+реестр", r"включени\w*\s+в\s+реестр",
                   r"внести\s+\w+(?:\s+\w+){0,4}\s+в\s+реестр"]
        with unittest.mock.patch.object(procedural, "_STRONG_RE",
                                        re.compile("|".join(narrow), re.I)):
            self.assertFalse(procedural.is_procedural(CASE_50),
                             "узкий шаблон обязан воспроизводить дефект — иначе тест мерит не его")


class TestProductQuestionsStayOnTheProductBranch(unittest.TestCase):
    """Ложные срабатывания держит не узость шаблона, а товарно-балльный сигнал."""

    def test_localisation_question_is_not_procedural(self):
        # Пример из шапки самого модуля: «требования» уводят на товарный путь.
        for q in ("требования к локализации насосов для внесения в реестр",
                  "сколько баллов нужно набрать для внесения продукции в реестр",
                  "какие требования к локализации станков для включения в реестр"):
            with self.subTest(q=q[:40]):
                self.assertFalse(procedural.is_procedural(q), q)

    def test_sweep_pins_the_false_positive_count(self):
        """⚠⚠ ГЛАВНЫЙ ПРЕДОХРАНИТЕЛЬ ПРАВКИ: расширение маркера не должно тащить товарные вопросы.

        До `K15-1` у гейта не было НИ ОДНОГО такого теста — потому он и разошёлся с корпусом.
        Число закреплено на всех наборах репозитория (188 вопросов); единственное известное
        срабатывание — `topics#45`, оно старше этой правки."""
        import eval_routing

        rows = eval_routing.collect()
        false_pos = [r for r in rows if r["class"] == "товарный" and r["procedural"]]
        self.assertEqual([(r["set"], r["id"]) for r in false_pos], [("topics", 45)],
                         f"состав ложных срабатываний изменился: "
                         f"{[(r['set'], r['id'], r['query'][:40]) for r in false_pos]}")

    def test_sweep_covers_every_question_set(self):
        """Положительный контроль свипа: он молчал бы и на пустом наборе."""
        import eval_routing

        rows = eval_routing.collect()
        self.assertGreaterEqual(len(rows), 180)
        self.assertEqual(len({r["set"] for r in rows}), len(eval_routing.SETS))


class TestReferenceGateFollowsTheQuestion(unittest.TestCase):
    """Справочник приезжает, когда вопрос КАСАЕТСЯ документа, — а не только когда тема `documents`."""

    def test_predicate_recognises_the_four_measured_questions(self):
        for q in (CASE_50,
                  "сертификаты СТ-1 или заключения ТПП о происхождении сырья",
                  "какой порядок получения заключения о подтверждении производства",
                  "а что есть Заключение Минпромторга?"):
            with self.subTest(q=q[:40]):
                self.assertTrue(documents_ref.asks_about_conclusion(q))

    def test_predicate_is_quiet_on_a_plain_product_question(self):
        for q in ("производим станки с ЧПУ", "какие документы нужны для этикетировщиков"):
            with self.subTest(q=q[:40]):
                self.assertFalse(documents_ref.asks_about_conclusion(q))

    def test_case_50_is_not_a_documents_topic_and_still_gets_the_reference(self):
        """⚠ Суть дефекта: тема тут ДРУГАЯ, и раньше этого хватало, чтобы справочника не было."""
        self.assertNotEqual(topics.classify(CASE_50), topics.DOCUMENTS,
                            "кейс перестал быть примером расхождения гейтов — тест обесценился")
        self.assertTrue(documents_ref.asks_about_conclusion(CASE_50))

    def test_procedural_plan_carries_the_reference(self):
        """Через `plan_procedural` на заглушках: Qdrant не нужен, проверяется сборка промпта."""
        from app.rag import pipeline

        fake = [{"doc_type": "rules_registry", "text": "Пункт про реестр.",
                 "source_anchor": "Правила, п. 1", "_score": 1.0}]
        with unittest.mock.patch.object(pipeline, "search_rules", lambda *a, **k: fake):
            _topic, _rules, _ctx, user = pipeline.plan_procedural(CASE_50, CASE_50)
        # ⚠ Якорь берём из самой функции, а не константой: первая редакция теста сверяла
        # `TABLE_TITLE` — заголовок ТАБЛИЦЫ ОТВЕТА, а не блока контекста, и падала на верном коде.
        self.assertIn(_reference_header(), user, "закрытый справочник не доехал до промпта")
        self.assertIn(documents_ref.NONEXISTENT_EXPLANATION, user,
                      "разъяснение про несуществующий документ не доехало")

    def test_reference_absent_when_the_question_is_about_neither(self):
        """Отрицательный контроль: на процедурном вопросе без документов справочника быть не должно."""
        from app.rag import pipeline

        fake = [{"doc_type": "rules_registry", "text": "Пункт про сроки.",
                 "source_anchor": "Правила, п. 2", "_score": 1.0}]
        q = "какой срок рассмотрения заявления о внесении в реестр"
        self.assertNotEqual(topics.classify(q), topics.DOCUMENTS)
        with unittest.mock.patch.object(pipeline, "search_rules", lambda *a, **k: fake):
            _t, _r, _c, user = pipeline.plan_procedural(q, q)
        self.assertNotIn(_reference_header(), user,
                         "справочник приезжает туда, где о документах не спрашивали")


class TestAcceptanceCaseIsInTheSet(unittest.TestCase):
    """Критерий приёмки #120 обязан жить в наборе, а не в тексте issue."""

    def test_case_50_exists_with_the_right_expectations(self):
        cases = json.loads((ROOT / "scripts" / "eval_golden.json").read_text(encoding="utf-8"))["cases"]
        c = next((x for x in cases if x["id"] == 50), None)
        self.assertIsNotNone(c, "кейс #120 исчез из набора")
        self.assertEqual(c["query"], CASE_50)
        self.assertTrue(c["expect_explanation"])
        self.assertIn("Акт экспертизы уполномоченной ТПП", c["expect_documents"])


if __name__ == "__main__":
    unittest.main()
