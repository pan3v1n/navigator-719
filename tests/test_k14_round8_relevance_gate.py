# -*- coding: utf-8 -*-
"""HIGH-4 восьмого раунда ревью PR #137 — и ЗАПРЕТ на порог релевантности процедурной ветки.

⚠⚠⚠ ЭТОТ ФАЙЛ ХРАНИТ ОТРИЦАТЕЛЬНЫЙ РЕЗУЛЬТАТ. Раунд 8 предписывал завести у процедурной ветки
гейт вне-сферы («щедрый маршрут + порог по плотному сходству»). Гейт был написан, выкачен в ветку
и ОТКАЧЕН 03.09.2026 — замером, а не спором. Тесты ниже держат обе половины решения: правку
маршрута, которая осталась, и отсутствие порога, которое стало осознанным.

ЗАМЕР (`scripts/eval_rules_relevance.py`, 164 вопроса пяти популяций):
    A  гейта нет                            15 утечек / 21 потеря = 36
    B  порог 0.851 всегда                    4 / 30 = 34
    C  порог мимо детерминированного пути   15 / 27 = 42   (доминирован)
B выигрывает у A два пункта из 36 — шум, — но создаёт ДЕВЯТЬ новых отказов: «что такое
кумулятивный принцип» (термин самого проиндексированного Соглашения), «какие условия экспорта в
Казахстан по ТН ВЭД 8403» (флагман HIGH-4), «моей продукции нет в приложении 719, можно ли
получить СТ-1» (подпункт «г»), «кто выдаёт сертификат происхождения в Курской области» — вопрос
про самого заказчика. Полосы перекрываются ПО СУЩЕСТВУ: таможенный вопрос про происхождение и
целевой вопрос про происхождение — про одно и то же, и корпус про то же.
"""

import unittest

from app.core import prompts
from app.rag import pipeline, topics


class TestOwnDomainVocabularyIsNotDisqualified(unittest.TestCase):
    """HIGH-4: `экспорт` и `вывоз` — лексика САМОЙ сделки, под которую выдаётся СТ-1.

    Раунд 7 положил их в `ST1_DISQUALIFIER`, то есть запретил словарь того, ради чего документ
    существует: СТ-1 — сертификат происхождения под экспорт в зону свободной торговли СНГ.
    Частота стволов в индексируемых этим же релизом документах: экспорт 111+12, вывоз 54+25."""

    def test_export_wording_keeps_the_st1_topic(self):
        for q in ("какие условия экспорта в Казахстан по ТН ВЭД 8403",
                  "какие условия при вывозе товара ТН ВЭД 8403",
                  "какие условия для экспорта по ТН ВЭД 8403"):
            with self.subTest(q=q):
                self.assertEqual(topics.classify(q), topics.ST1_ORIGIN)

    def test_import_side_is_still_disqualified(self):
        """⚠ `ввоз` и `импорт` ОСТАЮТСЯ: это чужая сделка, СТ-1 под неё не выдаётся."""
        for q in ("какие условия ввоза товара по ТН ВЭД 8703",
                  "какие условия импорта по ТН ВЭД 8703"):
            with self.subTest(q=q):
                self.assertNotEqual(topics.classify(q), topics.ST1_ORIGIN)

    def test_disqualifier_no_longer_lists_its_own_domain(self):
        for own in ("вывоз", "экспорт"):
            self.assertNotIn(own, topics.ST1_DISQUALIFIER)
        for alien in ("ввоз", "импорт"):
            self.assertIn(alien, topics.ST1_DISQUALIFIER)


class TestProceduralBranchHasNoRelevanceThreshold(unittest.TestCase):
    """⚠⚠ Отсутствие порога — РЕШЕНИЕ, и оно закреплено, чтобы не быть «починенным» молча.

    Правило проекта: осознанное решение, снятое по находке ревью, возвращают только новым
    замером. Раунд 9 (или любой следующий) обязан упереться в эти утверждения и прочитать шапку,
    а не завести порог заново «потому что у товарной ветки он есть»."""

    def test_no_threshold_constant_exists(self):
        self.assertFalse(hasattr(pipeline, "RULES_RELEVANCE_SOFT"),
                         "порог процедурной ветки заведён заново — см. шапку файла, нужен ЗАМЕР")

    def test_plan_carries_no_relevance_field(self):
        self.assertNotIn("low_relevance", pipeline.ProceduralPlan._fields)

    def test_procedural_prompt_takes_no_relevance_flag(self):
        """Параметр, которого никто не выставляет, — предохранитель, не могущий сработать."""
        import inspect

        sig = inspect.signature(prompts.build_procedural_user_prompt)
        self.assertNotIn("low_relevance", sig.parameters)

    def test_product_branch_keeps_its_own_threshold(self):
        """⚠ Отрицательный контроль: откат НЕ имеет права задеть товарный гейт.

        Там полосы расходятся (продукция вне 719 стабильно ниже профильной), порог измерен и
        работает с самого начала. Откатывалась ПРОЦЕДУРНАЯ ветка, а не идея порога вообще."""
        self.assertTrue(hasattr(pipeline, "RELEVANCE_SOFT"))
        self.assertAlmostEqual(pipeline.RELEVANCE_SOFT, 0.83)


class TestCollectionParameterSurvived(unittest.TestCase):
    """`dense_top1(collection=…)` оставлен: им меряет `eval_rules_relevance.py`, и без него
    отрицательный результат нельзя перепроверить. Инструмент замера переживает откат правки."""

    def test_dense_top1_accepts_a_collection(self):
        import inspect

        from app.rag.retriever import dense_top1

        self.assertIn("collection", inspect.signature(dense_top1).parameters)


if __name__ == "__main__":
    unittest.main()
