"""`M2` #115 — полный порядок процедурного пула: ничьи RRF больше не решает случай.

⚠⚠ ЧТО ЭТО ЗА ДЕФЕКТ. Товарный путь сортируется `_order_key` с 16.08.2026 (`M1`): RRF выдаёт
РАЗНЫМ записям буквально одинаковый score, и на границе окна две такие записи конкурируют за
место — кто попадёт, решал порядок ответа Qdrant. Процедурный путь такой сортировки не имел.

Замер 25.08.2026: два прогона `eval_rules.py` ПОДРЯД, на НЕИЗМЕННОМ коде, с `EXACT_SEARCH=1` и без
LLM дали долю окна Приказа №52 **0.72 и 0.70**. Последствие то же, что описала `M1`: эксперту один
и тот же процедурный вопрос даёт РАЗНЫЙ контекст.
"""

from __future__ import annotations

import random
import unittest

from app.rag import retriever


class _P:
    """Точка Qdrant в объёме, который нужен порядку: score, payload, id."""

    def __init__(self, score, doc_type, point, anchor, pid):
        self.score = score
        self.payload = {"doc_type": doc_type, "point": point, "source_anchor": anchor}
        self.id = pid


def _tied_pool():
    """Пул, где ТРИ записи делят один score — ровно случай, который ломался."""
    return [
        _P(0.30, "rules_registry", "12", "Правила, п. 12", "id-a"),
        _P(0.20, "tpp_order_52", "4.1", "Приказ №52, п. 4.1", "id-b"),
        _P(0.20, "decree_body", "1", "ПП №719, п. 1", "id-c"),
        _P(0.20, "rules_registry", "31", "Правила, п. 31", "id-d"),
        _P(0.10, "appendix_footnotes", "44", "Сноска 44", "id-e"),
    ]


class TestTotalOrder(unittest.TestCase):
    def test_same_input_same_output(self):
        pool = _tied_pool()
        first = [p.id for p in retriever._rules_order(pool)]
        for _ in range(5):
            self.assertEqual([p.id for p in retriever._rules_order(pool)], first)

    def test_order_does_not_depend_on_arrival_order(self):
        """⚠⚠ ГЛАВНОЕ СВОЙСТВО. Именно порядок прихода из Qdrant и решал ничью.

        Перемешиваем пул десять раз — результат обязан быть один и тот же."""
        pool = _tied_pool()
        expected = [p.id for p in retriever._rules_order(pool)]
        rnd = random.Random(20260825)
        for i in range(10):
            shuffled = pool[:]
            rnd.shuffle(shuffled)
            got = [p.id for p in retriever._rules_order(shuffled)]
            self.assertEqual(got, expected, f"перестановка {i}: порядок поехал")

    def test_ranking_by_score_is_preserved(self):
        """⚠ Сортировка НЕ меняет ранжирование — она разрешает только ничьи.

        `K9` показал цену перестановки вслепую: атрибуция@1 0.96 -> 0.88, потому что ответ
        строится вокруг ПЕРВОГО источника."""
        ordered = retriever._rules_order(_tied_pool())
        scores = [p.score for p in ordered]
        self.assertEqual(scores, sorted(scores, reverse=True),
                         "порядок по score нарушен — это уже не разрешение ничьей")
        self.assertEqual(ordered[0].id, "id-a", "запись с лучшим score должна остаться первой")
        self.assertEqual(ordered[-1].id, "id-e", "запись с худшим score должна остаться последней")

    def test_key_is_total_no_two_records_compare_equal(self):
        """Ключ обязан быть ПОЛНЫМ: иначе ничья переезжает уровнем ниже и остаётся нерешённой."""
        pool = _tied_pool()
        # два полных близнеца, различимых только по id
        pool.append(_P(0.20, "rules_registry", "31", "Правила, п. 31", "id-twin"))
        ordered = retriever._rules_order(pool)
        self.assertEqual(len(ordered), len(pool), "записи потерялись при сортировке")
        rnd = random.Random(7)
        expected = [p.id for p in ordered]
        for _ in range(5):
            sh = pool[:]
            rnd.shuffle(sh)
            self.assertEqual([p.id for p in retriever._rules_order(sh)], expected,
                             "близнецы, различимые только по id, меняются местами")

    def test_missing_score_does_not_explode(self):
        """Точка без score — не повод уронить процедурный ответ."""
        pool = _tied_pool()
        pool.append(_P(None, "tpp_order_52", "9.9", "Приказ №52, п. 9.9", "id-none"))
        ordered = retriever._rules_order(pool)
        self.assertEqual(len(ordered), len(pool))


class TestAppliedEverywhere(unittest.TestCase):
    """⚠ Ничья, не разрешённая в ОДНОМ из трёх источников пула, разъезжается по всему окну."""

    def test_all_three_sources_are_ordered(self):
        import inspect

        src = inspect.getsource(retriever.search_rules)
        self.assertEqual(src.count("_rules_order("), 3,
                         "полный порядок применён не ко всем трём источникам "
                         "(основной пул, добор темы, добор раздела 4)")

    def test_main_pool_is_ordered_before_anything_reads_indices(self):
        """Пул сортируется СРАЗУ: ниже по индексам идут by_doc, квота, добор остатка и финальный порядок."""
        import inspect

        src = inspect.getsource(retriever.search_rules)
        i_sort = src.index("points = _rules_order(")
        i_bydoc = src.index("by_doc.setdefault")
        self.assertLess(i_sort, i_bydoc, "пул читают по индексам раньше, чем упорядочили")


if __name__ == "__main__":
    unittest.main()
