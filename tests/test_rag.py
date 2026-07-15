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
from app.rag import pipeline as pipeline_mod  # noqa: E402
from app.rag.pipeline import (  # noqa: E402
    _ensure_disclaimer,
    claim_numbers,
    format_cases,
    format_context,
    format_rules_context,
    number_in_context,
    unverified_deadlines,
    unverified_numbers,
)
from app.rag.retriever import Hit, _prefixes, _segments, okpd2_match  # noqa: E402
from app.rag import procedural  # noqa: E402

# Загрузчик Правил лежит в scripts/ (не пакет) — добавляем в путь для теста парсера.
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import load_rules_kb  # noqa: E402


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

    def test_format_context_okpd2_match_marker(self):
        # точный хит по коду помечается, чтобы модель отдала ему приоритет
        self.assertIn("СОВПАДЕНИЕ ПО КОДУ ОКПД2", format_context([make_hit(okpd2_match=True)]))
        self.assertNotIn("СОВПАДЕНИЕ ПО КОДУ ОКПД2", format_context([make_hit(okpd2_match=False)]))

    def test_format_context_ranks_relevant_ops(self):
        # мега-продукт: релевантная операция стоит ПОСЛЕ порога усечения
        from app.rag.pipeline import MAX_OPS_PER_HIT
        fillers = [{"text": f"операция номер {i}", "points": None} for i in range(MAX_OPS_PER_HIT)]
        relevant = {"text": "сварка кузова автомобиля", "points": 400}
        hit = make_hit(requirement_blocks=[{"operations": fillers + [relevant]}])
        # без запроса последняя (релевантная) операция усекается
        self.assertNotIn("сварка кузова автомобиля", format_context([hit]))
        # с запросом она поднимается по релевантности и попадает в контекст
        self.assertIn("сварка кузова автомобиля", format_context([hit], query="сварка кузова"))


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


class TestFaithfulness(unittest.TestCase):
    def test_claim_numbers_balls_and_percent(self):
        # числа рядом с «балл»/«процент»/«%» извлекаются; даты/сроки — нет
        nums = claim_numbers("операция даёт 400 баллов, порог не менее 64 балла, доля 25 процентов")
        self.assertIn("400", nums)
        self.assertIn("64", nums)
        self.assertIn("25", nums)

    def test_claim_numbers_ignores_dates_and_terms(self):
        # «5 лет», «2018 г.» — не баллы/проценты, не должны попасть
        self.assertEqual(claim_numbers("на срок не менее 5 лет, с 1 января 2018 г."), [])

    def test_number_in_context_boundaries(self):
        ctx = "операция — 800 балл. ; ещё 1800 единиц"
        self.assertTrue(number_in_context("800", ctx))
        self.assertTrue(number_in_context("1800", ctx))
        self.assertFalse(number_in_context("80", ctx))  # 80 не стоит отдельным токеном

    def test_unverified_detects_hallucinated_number(self):
        # в ответе «800 баллов», в контексте такого числа нет → незаземлено
        ctx = "Ключевые операции:\n  • сборка — 400 балл."
        self.assertEqual(unverified_numbers("начисляется 800 баллов", ctx), ["800"])

    def test_unverified_empty_when_grounded(self):
        ctx = "Порог: не менее 64 баллов\n  • сварка — 400 балл."
        self.assertEqual(unverified_numbers("нужно 400 баллов при пороге 64 балла", ctx), [])


class TestProceduralDeflect(unittest.TestCase):
    # ЧИСТО процедурные вопросы — детектор обязан сработать (деферим, LLM не зовём)
    PROCEDURAL = [
        "какой порядок внесения продукции в реестр",
        "как внести станки в реестр российской промышленной продукции",
        "как подать заявление в Минпромторг на подтверждение производства",
        "какие документы нужны для подачи заявки",
        "срок рассмотрения заявления о внесении в реестр",
        "как работать с ГИСП",
        "нужно обжаловать отказ во внесении в реестр",
        "как получить заключение о производстве продукции в России",
        "порядок регистрации в реестре промышленной продукции",
    ]
    # Товарные и СМЕШАННЫЕ (есть код/товарно-балльный сигнал) — НЕ деферим
    NOT_PROCEDURAL = [
        "какие требования к локализации центробежных насосов",
        "сколько баллов для гусеничных бульдозеров",
        "требования к подшипникам",
        "производим прицепы для легковых авто 29.20.23",
        # смешанный: спрашивают ТРЕБОВАНИЯ, хоть и «для внесения в реестр» → отвечаем по базе
        "какие требования к локализации насосов для внесения в реестр",
        "сколько баллов нужно, чтобы попасть в реестр промышленной продукции",
    ]

    def test_positive_cases_detected(self):
        for q in self.PROCEDURAL:
            self.assertTrue(procedural.is_procedural(q), f"НЕ распознан процедурный: {q!r}")

    def test_negative_cases_pass_through(self):
        for q in self.NOT_PROCEDURAL:
            self.assertFalse(procedural.is_procedural(q), f"Ложное срабатывание на: {q!r}")

    def test_code_does_not_block_procedural(self):
        # код ОКПД2 больше НЕ отменяет процедурный путь: смешанный «процедура + код» → по Правилам
        self.assertTrue(procedural.is_procedural("как внести в реестр", has_code=True))
        self.assertTrue(procedural.is_procedural("процедура внесения в реестр чиллеров 28.25.13"))
        # но товарно-балльный сигнал уводит на товарный путь даже с процедурным словом и кодом
        self.assertFalse(procedural.is_procedural("какие требования к чиллерам 28.25.13 для внесения в реестр"))

    def test_deflection_message_has_pointers(self):
        self.assertIn("ГИСП", procedural.DEFLECTION)
        self.assertIn("реестр", procedural.DEFLECTION.lower())
        # само сообщение не содержит выдуманных баллов/сроков
        self.assertEqual(claim_numbers(procedural.DEFLECTION), [])


class TestFormatRulesContext(unittest.TestCase):
    def test_numbers_blocks_and_anchor(self):
        rules = [
            {"point": "8", "section_roman": "II", "section_title": "Включение сведений",
             "text": "Заявка рассматривается в течение 10 рабочих дней."},
            {"point": "9", "section_roman": "II", "section_title": "Включение сведений",
             "text": "Минпромторг включает сведения в реестр."},
        ]
        ctx = format_rules_context(rules)
        self.assertIn("[1]", ctx)
        self.assertIn("[2]", ctx)
        self.assertIn("Правила ведения реестра, п. 8", ctx)
        self.assertIn("10 рабочих дней", ctx)

    def test_long_point_truncated_with_marker(self):
        long_text = "А" * (pipeline_mod.RULES_TEXT_CAP + 500)
        ctx = format_rules_context([{"point": "6", "section_roman": "II",
                                     "section_title": "Включение", "text": long_text}])
        self.assertIn("приведён не полностью", ctx)
        self.assertLess(len(ctx), pipeline_mod.RULES_TEXT_CAP + 200)


class TestUnverifiedDeadlines(unittest.TestCase):
    def test_flags_fabricated_deadline(self):
        # в контексте только 10 рабочих дней, ответ выдумал 20 → незаземлено
        ctx = "Заявка рассматривается в течение 10 рабочих дней [1]."
        self.assertEqual(unverified_deadlines("решение за 20 рабочих дней", ctx), ["20 дн."])

    def test_empty_when_grounded(self):
        ctx = "срок 15 календарных дней"
        self.assertEqual(unverified_deadlines("в течение 15 календарных дней", ctx), [])

    def test_ignores_non_deadline_numbers(self):
        self.assertEqual(unverified_deadlines("нужно 50 баллов", "контекст без сроков"), [])


class TestProceduralAnswerFallback(unittest.TestCase):
    """Ключевая гарантия безопасности: если корпус Правил пуст/недоступен — честный дефер,
    БЕЗ вызова DeepSeek (процедуру не выдумываем)."""

    def setUp(self):
        self._orig = pipeline_mod.search_rules

    def tearDown(self):
        pipeline_mod.search_rules = self._orig

    def test_deflects_when_rules_empty(self):
        pipeline_mod.search_rules = lambda *a, **k: []
        ans = pipeline_mod._answer_procedural("как внести в реестр", "как внести в реестр")
        self.assertEqual(ans.text, procedural.DEFLECTION)
        self.assertEqual(ans.hits, [])


class TestRulesLoader(unittest.TestCase):
    """Парсер корпуса Правил (scripts/load_rules_kb) — без Qdrant/e5."""

    def test_parse_all_sections(self):
        recs, _ = load_rules_kb.load_records()
        self.assertGreaterEqual(len(recs), 40)  # ~58 пунктов
        romans = {r["section_roman"] for r in recs}
        self.assertTrue({"I", "II", "III", "V", "VI"} <= romans)
        for r in recs[:5]:
            self.assertTrue(r["text"])
            self.assertTrue(r["source_anchor"].startswith("Правила ведения реестра, п."))
            self.assertEqual(r["doc_type"], "rules_registry")

    def test_edition_detected(self):
        _, edition = load_rules_kb.load_records()
        self.assertIn("ред. от", edition)  # штамп редакции извлечён из текста (нормализация nbsp)

    def test_subpoints_kept_in_parent(self):
        recs, _ = load_rules_kb.load_records()
        points = {r["point"] for r in recs}
        self.assertIn("3.1", points)  # подпункт с точкой распознан отдельным пунктом
        p2 = next(r for r in recs if r["section_roman"] == "I" and r["point"] == "2")
        self.assertIn("акт экспертизы", p2["text"])  # определения остались внутри п.2


if __name__ == "__main__":
    unittest.main()
