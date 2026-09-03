# -*- coding: utf-8 -*-
"""Гейт вне-сферы ПРОЦЕДУРНОЙ ветки — структурная замена словарного механизма (раунд 8, PR #137).

ЧТО ЧИНИТСЯ. Раунд 7 записал диагноз «у процедурной ветки НЕТ гейта вне-сферы» и лечил его
СЛОВАРЁМ маршрута. Раунд 8: три HIGH из шести имели один корень (расширенная тема, кормящая
гейт), а замер на независимой популяции дал 22 → 23 — правка не улучшила ничего. Причина не была
тронута: `_answer_procedural` держал `low_relevance=False` ЖЁСТКО, то есть ветка объявляла себя
релевантной всегда, чем бы её ни спросили.

⚠⚠ ВСЁ ОФЛАЙН. Тест, зовущий живой Qdrant, — это класс `O3` #104: CI стоял красным шесть прогонов
подряд, и в этой красноте пакет уехал на бой. Здесь и поиск, и сходство подменены заглушками.
"""

import unittest
from unittest import mock

from app.core.prompts import build_procedural_user_prompt
from app.rag import pipeline, topics


def _rule(point="1", text="Пункт про реестр"):
    return {"doc_type": "rules_reestr", "section_roman": "I", "point": point,
            "text": text, "source_anchor": "Правила, п. 1", "_score": 1.0, "_topic": None}


class TestGateIsWiredIntoThePlan(unittest.TestCase):
    """`plan_procedural` обязан ВЫЧИСЛЯТЬ релевантность, а не объявлять её."""

    def _plan(self, score: float):
        with mock.patch.object(pipeline, "search_rules", return_value=[_rule()]), \
             mock.patch.object(pipeline, "dense_top1", return_value=score) as dt:
            planned = pipeline.plan_procedural("вопрос", "вопрос")
        return planned, dt

    def test_plan_returns_the_relevance_flag(self):
        planned, _ = self._plan(0.95)
        self.assertEqual(len(planned), 6, "план обязан нести признак релевантности шестым полем")

    def test_low_similarity_raises_the_flag(self):
        planned, _ = self._plan(pipeline.RULES_RELEVANCE_SOFT - 0.01)
        self.assertTrue(planned[5])

    def test_high_similarity_does_not(self):
        planned, _ = self._plan(pipeline.RULES_RELEVANCE_SOFT + 0.01)
        self.assertFalse(planned[5])

    def test_similarity_is_measured_on_the_rules_collection(self):
        """⚠ Своя коллекция, а не товарная: у корпуса норм проза, и полосы сходства лежат иначе.
        Ровно ради этого корпус и заведён отдельной коллекцией."""
        from app.core.config import settings

        _planned, dt = self._plan(0.95)
        self.assertEqual(dt.call_args.kwargs.get("collection"),
                         settings.QDRANT_RULES_COLLECTION)

    def test_threshold_is_its_own_not_the_product_one(self):
        self.assertNotEqual(pipeline.RULES_RELEVANCE_SOFT, pipeline.RELEVANCE_SOFT)


class TestGateHasThreeOutcomes(unittest.TestCase):
    """⚠⚠ «Не измерено» — третий исход, а не молчаливое «релевантно».

    Правило проекта, купленное дважды за 30.08: у всякой пробы три исхода. Но направление ошибки
    решает форму — ложно отрицательный гейт молча превратил бы рабочую ветку в отказ, поэтому при
    сбое ЗАМЕРА поведение остаётся прежним, и сбой ГОВОРИТ в журнал."""

    def test_measurement_failure_does_not_silently_refuse(self):
        with mock.patch.object(pipeline, "search_rules", return_value=[_rule()]), \
             mock.patch.object(pipeline, "dense_top1", side_effect=RuntimeError("Qdrant упал")):
            planned = pipeline.plan_procedural("вопрос", "вопрос")
        self.assertIsNotNone(planned, "сбой ЗАМЕРА не имеет права гасить найденные пункты")
        self.assertFalse(planned[5])

    def test_measurement_failure_is_logged(self):
        with mock.patch.object(pipeline, "search_rules", return_value=[_rule()]), \
             mock.patch.object(pipeline, "dense_top1", side_effect=RuntimeError("boom")), \
             mock.patch.object(pipeline.logger, "warning") as warn:
            pipeline.plan_procedural("вопрос", "вопрос")
        self.assertTrue(warn.called, "молчаливая деградация гейта — это его отсутствие")


class TestFlagReachesThePrompt(unittest.TestCase):
    """Признак обязан ДОЕХАТЬ до модели: гейт, не меняющий промпт, ничего не гейтит."""

    def test_prompt_carries_an_honest_refusal(self):
        low = build_procedural_user_prompt("вопрос", "ПУНКТ", low_relevance=True)
        high = build_procedural_user_prompt("вопрос", "ПУНКТ", low_relevance=False)
        self.assertIn("СИГНАЛ РЕЛЕВАНТНОСТИ", low)
        self.assertNotIn("СИГНАЛ РЕЛЕВАНТНОСТИ", high)

    def test_refusal_names_the_off_domain_classes(self):
        low = build_procedural_user_prompt("вопрос", "ПУНКТ", low_relevance=True)
        for word in ("таможен", "вне сферы"):
            self.assertIn(word, low)

    def test_plan_passes_the_flag_into_the_prompt(self):
        """Сквозная проверка: не «функция умеет», а «путь до неё существует»."""
        with mock.patch.object(pipeline, "search_rules", return_value=[_rule()]), \
             mock.patch.object(pipeline, "dense_top1", return_value=0.10):
            planned = pipeline.plan_procedural("вопрос", "вопрос")
        self.assertIn("СИГНАЛ РЕЛЕВАНТНОСТИ", planned[3])


class TestFlagReachesTheAnswer(unittest.TestCase):
    """⚠⚠ Флаг нужен НЕ ТОЛЬКО промпту. `Answer.low_relevance` пишется в БД (`chat.py`), считается
    в триаже админки (`admin_stats.py`) и ОТБРАСЫВАЕТ ответы при сборке `gs_natural`. Промпт и
    поле — два РАЗНЫХ потребителя одного признака, и тест на первый не проверяет второй."""

    def _answer(self, score: float):
        fake_resp = mock.Mock()
        fake_resp.choices = [mock.Mock(message=mock.Mock(content="Ответ по пунктам [1]."))]
        fake_resp.usage = mock.Mock(prompt_tokens=10, completion_tokens=5)
        fake_client = mock.Mock()
        fake_client.chat.completions.create.return_value = fake_resp
        with mock.patch.object(pipeline, "search_rules", return_value=[_rule()]), \
             mock.patch.object(pipeline, "dense_top1", return_value=score), \
             mock.patch.object(pipeline, "_client", return_value=fake_client):
            return pipeline._answer_procedural("вопрос", "вопрос")

    def test_low_similarity_marks_the_answer(self):
        self.assertTrue(self._answer(pipeline.RULES_RELEVANCE_SOFT - 0.01).low_relevance)

    def test_high_similarity_does_not_mark_it(self):
        self.assertFalse(self._answer(pipeline.RULES_RELEVANCE_SOFT + 0.01).low_relevance)


class TestOwnDomainVocabularyIsNotDisqualified(unittest.TestCase):
    """HIGH-4: `экспорт` и `вывоз` — лексика САМОЙ сделки, под которую выдаётся СТ-1."""

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


if __name__ == "__main__":
    unittest.main()
