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

    def test_top_ups_never_query_a_filtered_out_document(self):
        """⚠⚠⚠ ПРОВЕРЯЕТСЯ ПОВЕДЕНИЕ, А НЕ ТЕКСТ ИСХОДНИКА (раунд 12).

        Прежняя редакция утверждала наличие ДОСЛОВНОЙ строки
        `asks_document_list(query) and (not only_docs or "tpp_order_52" in only_docs)` — и
        покраснела на ВЕРНОМ коде, когда добор перечня стал документ-зависимым (`K16`: раздел 4
        есть и у Положения №49). Инвариант при этом не менялся ни на знак. Сторож, привязанный к
        написанию, краснеет от законного рефакторинга и молчит при подмене смысла — за эту
        сессию ровно этот класс нашёлся четырежды.
        Здесь перехватывается `_hybrid` и проверяется, ЧТО именно доборы спрашивают у Qdrant."""
        from types import SimpleNamespace
        from unittest import mock

        asked: list[list[str]] = []

        def _point(i):
            # ⚠ Пул ОБЯЗАН быть непустым: `search_rules` возвращается сразу на пустом пуле, и
            # доборы до вызова не доходят. Первая редакция этого теста мокала `_hybrid` на `[]`
            # — и была ЗЕЛЁНОЙ на мутанте со снятым фильтром, потому что проверяемый код просто
            # не исполнялся. Поймано мутацией, а не рассуждением.
            return SimpleNamespace(id=i, score=1.0 - i / 100, payload={
                "doc_type": "rules_registry", "section_roman": "II", "section_title": "т",
                "point": f"{i}", "text": "текст пункта достаточной длины для фильтра пустышек",
                "source_anchor": f"Правила, п. {i}"})

        def fake_hybrid(query, limit, collection=None, qvec=None, qfilter=None):
            vals = []
            for c in (getattr(qfilter, "must", None) or []):
                m = getattr(c, "match", None)
                if getattr(c, "key", "") == "doc_type" and m is not None:
                    vals.append(getattr(m, "value", None))
            asked.append(vals)
            return [_point(i) for i in range(8)]

        with mock.patch.object(retriever, "_hybrid", side_effect=fake_hybrid), \
                mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
            # ⚠ Вопрос подобран так, чтобы ОБА добора были достижимы: `asks_document_list`
            # истинно, а тема — ничья, поэтому `primary` не отсекается раньше времени.
            retriever.search_rules("какие документы нужны для реестровой записи", limit=6,
                                   primary_docs=("rules_registry",),
                                   only_docs=("rules_registry",))

        queried = {d for call in asked for d in call if d}
        self.assertEqual(queried - {"rules_registry"}, set(),
                         f"добор спросил документ, исключённый фильтром: {sorted(queried)}")

        # ⚠⚠ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ ДОСТИЖИМОСТИ. Без него утверждение выше истинно и тогда,
        # когда добор не исполняется ВООБЩЕ, — то есть «фильтр соблюдён» неотличимо от «код не
        # работал». Тот же запрос при разрешающем фильтре обязан дать ВТОРОЙ запрос к Qdrant.
        asked.clear()
        with mock.patch.object(retriever, "_hybrid", side_effect=fake_hybrid), \
                mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
            retriever.search_rules("какие документы нужны для реестровой записи", limit=6,
                                   primary_docs=("rules_registry",),
                                   only_docs=("rules_registry", "tpp_order_52"))
        self.assertGreater(len(asked), 1,
                           "добор не исполнился и при разрешающем фильтре — проверка выродилась")
        self.assertIn("tpp_order_52", {d for call in asked for d in call if d},
                      "разрешённый добор перечня не ушёл в Qdrant")


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
