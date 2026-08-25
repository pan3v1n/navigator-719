"""K12 — тематический роутер процедурной ветки (issue #45). Офлайн, без Qdrant и DeepSeek.

Тема задаёт две вещи: какие документы гарантированно попадут в окно и какие оговорки уедут в
промпт. Значит ошибка темы — это не «чуть хуже ранжирование», а НЕ ТЕ оговорки в ответе, поэтому
точность здесь закреплена гейтом, а не оставлена на глаз.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import topics  # noqa: E402


class TestRouterAccuracy(unittest.TestCase):
    """Критерий приёмки K12 — постоянным гейтом, а не разовым свипом."""

    @classmethod
    def setUpClass(cls):
        import eval_topics
        cls.pos, cls.neg = eval_topics.evaluate()

    def test_accuracy_on_real_questions(self):
        """40 реальных процедурных вопросов волны, темы расставлены руками."""
        acc = sum(1 for r in self.pos if r["ok"]) / len(self.pos)
        self.assertGreaterEqual(acc, 0.90, [(r["id"], r["expect"], r["got"])
                                            for r in self.pos if not r["ok"]])

    def test_no_topic_on_pure_product_questions(self):
        """Тема на товарном вопросе означала бы процедурные оговорки в товарном ответе."""
        wrong = [(r["id"], r["got"], r["query"]) for r in self.neg if r["got"] is not None]
        self.assertEqual(wrong, [])


class TestPrecedence(unittest.TestCase):
    """Приоритет намерения выше числа совпавших шаблонов."""

    def test_documents_beats_registry_entry(self):
        """Стоило первой версии 0.71: «внесение в реестр» + «Минпромторг» перебивали «документы»
        по числу совпадений, хотя намерение вопроса — состав документов."""
        q = "Какие документы нужны для внесения в реестр Минпромторга по позиции доильные установки"
        self.assertEqual(topics.classify(q), "documents")

    def test_documents_beats_criteria(self):
        """«какие ИМЕННО документы из подпункта „а“» — в вопросе назван подпункт, но спрашивают
        документы. Разрыв в шаблоне добавлен именно по этим трём реальным вопросам."""
        q = 'какие именно документы из подпункта "а" пункта 1 постановления №719 нужны для этикетировщиков?'
        self.assertEqual(topics.classify(q), "documents")

    def test_topics_order_is_narrow_to_broad(self):
        self.assertEqual(topics.TOPICS[0], "documents")
        self.assertEqual(topics.TOPICS[-1], "registry_entry")

    def test_unknown_intent_returns_none(self):
        """None — законный результат: работает общий процедурный промпт. Придумывать тему опаснее."""
        self.assertIsNone(topics.classify("расскажи что-нибудь про постановление"))
        self.assertIsNone(topics.classify(""))


class TestWiring(unittest.TestCase):
    """У каждой темы обязаны быть и предпочтение документов, и фрагмент промпта."""

    def test_every_topic_has_docs_and_fragment(self):
        for t in topics.TOPICS:
            self.assertTrue(topics.doc_types(t), f"{t}: нет предпочтения документов")
            self.assertTrue(topics.fragment(t), f"{t}: нет фрагмента промпта")

    def test_four_structures_cannot_disagree(self):
        """Тема живёт в четырёх структурах, и `classify` возвращает только то, что есть в TOPICS.

        Добавь седьмое намерение в шаблоны, документы и фрагменты, но забудь про `TOPICS` — оно
        будет совпадать, попадать в `matched` и молча отбрасываться: тема не определится, ответ
        уйдёт с общим промптом, ошибки не будет. Это ровно «молчаливый отказ хуже громкой ошибки»,
        поэтому расхождение структур ломает тест, а не продакшен."""
        from app.rag import topics as m
        names = set(m.TOPICS)
        self.assertEqual(set(m._DOC_TYPES), names, "предпочтения документов разошлись с TOPICS")
        self.assertEqual(set(m._FRAGMENTS), names, "фрагменты промпта разошлись с TOPICS")
        # у `documents` шаблонов нет намеренно — она спрашивает детектор P2
        self.assertEqual(set(m._PATTERNS) | {m.DOCUMENTS}, names,
                         "шаблоны разошлись с TOPICS")
        self.assertEqual(len(m.TOPICS), len(names), "в TOPICS есть дубликаты")

    def test_unknown_topic_is_silent(self):
        self.assertEqual(topics.doc_types(None), ())
        self.assertEqual(topics.fragment(None), "")
        self.assertEqual(topics.fragment("выдуманная"), "")

    def test_doc_types_point_at_real_documents(self):
        known = {"tpp_order_52", "rules_registry", "decree_body", "appendix_footnotes"}
        for t in topics.TOPICS:
            for d in topics.doc_types(t):
                self.assertIn(d, known, f"{t}: неизвестный doc_type {d}")

    def test_documents_rules_live_in_one_place(self):
        """Тема `documents` не заводит своих шаблонов — спрашивает детектор P2. Вторая копия
        правил разошлась бы с первой при первой правке."""
        import inspect
        src = inspect.getsource(topics)
        self.assertIn("asks_document_list", src)
        self.assertNotIn('"documents": (', src.split("_DOC_TYPES")[0].split("_PATTERNS")[1])


class TestThemeDoesNotReorderWindow(unittest.TestCase):
    """Регресс-гард: тема даёт представительство, но НЕ решает, чей пункт первый.

    Первая версия отдавала теме и первое место — атрибуция@1 упала 0.96 → 0.88 (Приказ №52 9/9 →
    7/9). Причина: тема не определяет документ однозначно. «Сроки рассмотрения заявления»
    устанавливают Правила, «сроки выдачи акта экспертизы» — Приказ №52, а тема у обоих одна."""

    def test_primary_still_comes_from_lexical_topic(self):
        import inspect

        from app.rag import retriever
        src = inspect.getsource(retriever.search_rules)
        self.assertIn("primary = rules_topic(query)", src)
        self.assertIn("extra_primary", src)

    def test_reason_is_recorded_next_to_the_code(self):
        """Урок стоил замера — он обязан лежать рядом с кодом, иначе идею вернут."""
        import inspect

        from app.rag import retriever
        src = inspect.getsource(retriever.search_rules)
        self.assertIn("0.88", src)


class TestMixedQuestionGetsDocuments(unittest.TestCase):
    """P2-остаток: «какие документы нужны для этикетировщиков» — товарный вопрос про документы.

    Бинарный роутер относит такие к товарным (719-якоря в них нет), и ответ советовал «спросите
    отдельно»: 23 реальных вопроса волны уходили без перечня при кластере жалоб №1."""

    def test_prompt_carries_documents_block(self):
        from app.core.prompts import build_navigator_user_prompt
        user = build_navigator_user_prompt("какие документы нужны для этикетировщиков", "КОНТЕКСТ",
                                           documents="[1] Приказ №52, п. 4.2.1 — копия устава")
        # ⚠ Якорь — СТАБИЛЬНЫЙ префикс блока, а не полная шапка: с `K15` в блок доехал ещё и
        # закрытый справочник, и шапка описывает уже два источника. Проверяем отдельно, что
        # Приказ №52 в ней по-прежнему назван, — иначе тест ловил бы формулировку, а не факт.
        self.assertIn("ДОКУМЕНТЫ (", user)
        self.assertIn("Приказа ТПП РФ №52", user)
        self.assertIn("копия устава", user)
        self.assertIn("п. 4.2.1", user)
        # ⚠ ссылки [N] в товарном ответе означают ПОЗИЦИЮ приложения — на пункт Приказа так ссылаться
        # нельзя, иначе эксперт пойдёт сверять номер не туда
        self.assertIn("НЕ на номер позиции [N]", user)

    def test_no_block_when_question_is_not_about_documents(self):
        from app.core.prompts import build_navigator_user_prompt
        user = build_navigator_user_prompt("требования к чиллерам", "КОНТЕКСТ")
        # ⚠ Тот же префикс, что в положительной половине. С прежним якорем
        # ("ДОКУМЕНТЫ (Приказ") эта проверка стала бы зелёной ВХОЛОСТУЮ, как только шапка
        # сменила формулировку, — и пропустила бы блок, приехавший не на свой вопрос.
        self.assertNotIn("ДОКУМЕНТЫ (", user)

    def test_answered_documents_are_not_suggested_again(self):
        """Товарный двойник петли: смешанный вопрос уже получил перечень — предлагать его снова
        значит обещать то, что только что выдано. Процедурную ветку от этого починили, товарную
        оставили — ревью нашло."""
        from app.rag import followup
        with_docs = followup.after_product(False, documents_answered=True)
        self.assertIn(followup.DEADLINE_QUESTION, with_docs)
        self.assertNotIn(followup.DOCS_QUESTION, with_docs)
        self.assertIn(followup.DOCS_QUESTION, followup.after_product(False))

    def test_block_carries_only_prikaz_52(self):
        """Шапка блока обещает Приказ №52, значит там обязаны быть только его пункты.

        `primary_docs` — предпочтение квоты, а не фильтр: если добор раздела 4 не удался (он в
        `except` и лишь логируется), в блок попали бы пункты про печати и сроки из Правил — под
        шапкой про состав документов, а промпт при этом запрещает отсылать «спросите отдельно»."""
        import inspect

        from app.rag import pipeline
        src = inspect.getsource(pipeline._plan_answer)
        self.assertIn('p.get("doc_type") == "tpp_order_52"', src)

    def test_pipeline_adds_the_block_by_topic(self):
        import inspect

        from app.rag import pipeline
        src = inspect.getsource(pipeline._plan_answer)
        # имя темы — константой, а не строкой по месту: опечатка в литерале тихо выключила бы блок
        self.assertIn("topics.classify(search_query) == topics.DOCUMENTS", src)
        self.assertIn("documents=docs_ctx", src)

    def test_documents_block_is_NOT_part_of_grounding(self):
        """⚠ Тест перевёрнут 17.08 по итогам ревью: первая версия ЗАКРЕПЛЯЛА дефект.

        Я добавлял блок документов в заземление «чтобы числа пунктов Приказа не считались
        выдумкой». Обоснование ложное — `claim_numbers` берёт числа только рядом с «балл»/«процент»,
        и на тексте Приказа она возвращает пусто. Фактически добавление расширяло стог сена для
        `number_in_context` (сверка цифры подстрокой, без единицы), и выдуманные «30 баллов»
        заземлялись о срок «30 календарных дней» из номера пункта. Замер ниже это показывает."""
        from app.rag.pipeline import unverified_numbers
        ctx = "Позиция: Этикетировщики. Операции: сварка станины — 6 баллов."
        docs = ("п. 4.2.1 выписка из ЕГРЮЛ, выданная не ранее чем за 30 календарных дней; "
                "лицензия со сроком 3 года; сведения по форме 4.3.19")
        ans = "Порог — не менее 30 баллов [1], доля импорта не более 3 процентов [1]."
        self.assertEqual(unverified_numbers(ans, ctx, ""), ["30", "3"],
                         "гард обязан видеть выдуманные величины")
        self.assertEqual(unverified_numbers(ans, ctx + "\n" + docs, ""), [],
                         "а с блоком в заземлении он их теряет — поэтому блока там быть не должно")

        import inspect

        from app.rag import pipeline
        src = inspect.getsource(pipeline._plan_answer)
        grounding_line = src.split("grounding =")[1][:200]
        self.assertNotIn("docs_ctx", grounding_line,
                         "блок документов вернулся в заземление — гард снова ослеплён")


if __name__ == "__main__":
    unittest.main()
