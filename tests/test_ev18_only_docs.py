"""`EV18` #110 — жёсткий фильтр документов в процедурном поиске."""

from __future__ import annotations

import unittest

from app.rag import retriever


class TestOnlyDocsIsAHardFilter(unittest.TestCase):
    """⚠⚠ `primary_docs` — ПРЕДПОЧТЕНИЕ КВОТЫ, `only_docs` — ФИЛЬТР. Разница стоила пункта в блоке.

    Блок состава документов (`docs_ctx`) просил у окна «документы темы», а двумя строками ниже
    фильтровал выдачу до Приказа №52 — то есть тратил места, которые тут же выбрасывал. Пока у
    темы был один документ, разницы не было; `K15` завела второй, и блок стал худеть с 3 пунктов
    до 2 на двух смешанных вопросах из трёх (замер 25.08.2026).
    """

    def test_filter_condition_is_built_for_the_query(self):
        conds = retriever._doc_filter(("tpp_order_52",))
        self.assertEqual(len(conds), 1, "условие не построено")
        self.assertEqual(conds[0].key, "doc_type")

    def test_empty_means_no_filter(self):
        """⚠ Отрицательный контроль: без `only_docs` поведение обязано остаться прежним.

        Иначе правка молча изменила бы КАЖДЫЙ процедурный запрос."""
        self.assertEqual(retriever._doc_filter(()), [])

    def test_condition_survives_alive_only(self):
        """Фильтр документа обязан сочетаться с фильтром «документ действует» (`K8`), а не заменять его."""
        f = retriever.alive_only(*retriever._doc_filter(("tpp_order_52", "rules_registry")))
        self.assertIsNotNone(f.must, "условие документа потерялось")
        self.assertIsNotNone(f.must_not, "фильтр «утратил силу» потерялся — вернулись бы отменённые нормы")

    def test_signature_carries_both_knobs(self):
        import inspect

        params = inspect.signature(retriever.search_rules).parameters
        self.assertIn("primary_docs", params, "предпочтение квоты исчезло")
        self.assertIn("only_docs", params, "жёсткий фильтр исчез")
        self.assertEqual(params["only_docs"].default, ())


class TestFiltersAreRespectedByTheTopUps(unittest.TestCase):
    """⚠ Доборы обязаны уважать фильтр — иначе вернут то, что основной запрос только что исключил.

    В `search_rules` их два: тематический документ и раздел 4 Приказа №52. Оба ходят в Qdrant
    ОТДЕЛЬНЫМИ запросами со своим фильтром, то есть мимо основного."""

    def test_source_guards_both_top_ups(self):
        import inspect

        src = inspect.getsource(retriever.search_rules)
        self.assertIn("if primary and primary not in only_docs", src,
                      "добор тематического документа игнорирует фильтр")
        self.assertIn('asks_document_list(query) and (not only_docs or "tpp_order_52" in only_docs)',
                      src, "добор раздела 4 игнорирует фильтр")


class TestProductPathAsksForWhatSurvivesTheFilter(unittest.TestCase):
    """Вызов и постфильтр должны говорить одно и то же — иначе места окна тратятся впустую."""

    def test_docs_ctx_uses_the_hard_filter(self):
        import inspect

        from app.rag import pipeline

        src = inspect.getsource(pipeline)
        i = src.index("RULES_DOC_POINTS, qvec=qvec")
        call = src[i - 200:i + 200]
        self.assertIn('only_docs=("tpp_order_52",)', call,
                      "блок документов снова просит предпочтение вместо фильтра")
        self.assertNotIn('primary_docs=("tpp_order_52",)', call)


if __name__ == "__main__":
    unittest.main()
