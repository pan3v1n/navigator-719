"""Юнит-тесты чистой логики RAG-слоя (без Qdrant, сети, модели и DeepSeek).

Покрывают детерминированные функции: иерархическое сопоставление ОКПД2, локальный
BM25 (токенизация/стемминг/векторы), сборку контекста и промпта, гарантию пометки.
Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.prompts import EXPERT_DISCLAIMER, build_navigator_user_prompt  # noqa: E402
from app.rag import sparse  # noqa: E402
from app.rag.pipeline import _ensure_disclaimer, format_cases, format_context  # noqa: E402
from app.rag.retriever import Hit, _prefixes, _segments, okpd2_match  # noqa: E402


def make_hit(**kw) -> Hit:
    """Hit с разумными дефолтами — переопределяем только нужные поля в тесте."""
    base = dict(
        score=1.0, section_roman="III", section_title="Спецмашиностроение",
        product_name="Подшипники шариковые или роликовые", okpd2_codes=["28.15.10"],
        min_threshold=None, requirement_blocks=[], source_anchor="Раздел III, поз. 1",
        okpd2_match=False, payload={},
    )
    base.update(kw)
    return Hit(**base)


class TestOkpd2Match(unittest.TestCase):
    def test_segments(self):
        self.assertEqual(_segments("29.20.23.110"), ["29", "20", "23", "110"])
        self.assertEqual(_segments(" 29.20 "), ["29", "20"])
        self.assertEqual(_segments(""), [])

    def test_prefixes(self):
        self.assertEqual(_prefixes("29.20.23"), ["29", "29.20", "29.20.23"])

    def test_match_hierarchical_both_directions(self):
        # код записи длиннее запроса — общая ветка
        self.assertTrue(okpd2_match(["29.20.23.110"], "29.20.23"))
        # код запроса длиннее — тоже одна ветка
        self.assertTrue(okpd2_match(["29.20"], "29.20.23"))
        self.assertTrue(okpd2_match(["28.15.10"], "28.15.10"))

    def test_no_match_different_branch(self):
        self.assertFalse(okpd2_match(["28.15.10"], "29.10.2"))

    def test_segmentwise_not_substring(self):
        # ключевой кейс: 29.10.2 НЕ должен матчить 29.10.23 (посегментно 2 != 23)
        self.assertFalse(okpd2_match(["29.10.23"], "29.10.2"))
        self.assertFalse(okpd2_match(["29.10.2"], "29.10.23"))

    def test_empty_inputs(self):
        self.assertFalse(okpd2_match([], "29.20"))
        self.assertFalse(okpd2_match(["29.20"], ""))


class TestSparseBM25(unittest.TestCase):
    def test_tokenize_lowercase_and_drop_singletons(self):
        toks = sparse.tokenize("Большие Подшипники")
        self.assertEqual(len(toks), 2)
        self.assertTrue(all(t == t.lower() for t in toks))

    def test_tokenize_drops_one_char_and_empty(self):
        self.assertEqual(sparse.tokenize("в и с 1"), [])  # все токены длиной 1
        self.assertEqual(sparse.tokenize(""), [])

    def test_query_vector_counts_terms(self):
        idx, val = sparse.query_vector("подшипник подшипник")
        self.assertEqual(len(idx), 1)          # один уникальный терм
        self.assertEqual(val, [2.0])           # встретился дважды

    def test_query_vector_lengths_match(self):
        idx, val = sparse.query_vector("сварка кузова рамы")
        self.assertEqual(len(idx), len(val))
        self.assertGreater(len(idx), 0)

    def test_document_vector_length_normalization(self):
        # один и тот же терм: в длинном документе его вес BM25 ниже (нормировка по длине)
        short_idx, short_val = sparse.document_vector("сварка", avgdl=5.0)
        long_text = "сварка " + " ".join(f"слово{i}" for i in range(20))
        long_idx, long_val = sparse.document_vector(long_text, avgdl=5.0)
        h = short_idx[0]
        self.assertIn(h, long_idx)
        self.assertGreater(short_val[0], long_val[long_idx.index(h)])


class TestContextAndDisclaimer(unittest.TestCase):
    def test_ensure_disclaimer_adds_when_missing(self):
        out = _ensure_disclaimer("Краткий анализ.")
        self.assertTrue(out.strip().endswith(EXPERT_DISCLAIMER))

    def test_ensure_disclaimer_no_duplicate(self):
        text = "Анализ.\n\n" + EXPERT_DISCLAIMER
        self.assertEqual(_ensure_disclaimer(text).count(EXPERT_DISCLAIMER), 1)

    def test_format_context_points_and_null(self):
        hit = make_hit(requirement_blocks=[{"operations": [
            {"text": "сварка кузова", "points": 400},
            {"text": "окраска", "points": None},
        ]}])
        ctx = format_context([hit])
        self.assertIn("Подшипники шариковые или роликовые", ctx)
        self.assertIn("сварка кузова — 400 балл.", ctx)
        self.assertIn("окраска — балл зависит", ctx)

    def test_format_cases(self):
        out = format_cases([{"product_name": "Прицепы", "okpd2": "29.20.23",
                             "query": "вопрос", "expert_answer": "ответ эксперта"}])
        self.assertIn("Прицепы", out)
        self.assertIn("ответ эксперта", out)


class TestUserPrompt(unittest.TestCase):
    def test_low_relevance_warning_present(self):
        p = build_navigator_user_prompt("хлеб", "контекст", low_relevance=True)
        self.assertIn("СИГНАЛ РЕЛЕВАНТНОСТИ", p)

    def test_no_warning_when_relevant(self):
        p = build_navigator_user_prompt("подшипники", "контекст", low_relevance=False)
        self.assertNotIn("СИГНАЛ РЕЛЕВАНТНОСТИ", p)

    def test_okpd2_and_cases_included(self):
        p = build_navigator_user_prompt("q", "ctx", okpd2="28.15.10", cases="КЕЙС-ТЕКСТ")
        self.assertIn("28.15.10", p)
        self.assertIn("КЕЙС-ТЕКСТ", p)


if __name__ == "__main__":
    unittest.main()
