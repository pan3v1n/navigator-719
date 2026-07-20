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
    _anchor_code,
    _ensure_disclaimer,
    _is_continuation,
    claim_numbers,
    format_cases,
    format_context,
    format_rules_context,
    number_in_context,
    unverified_deadlines,
    unverified_numbers,
)
from app.rag.retriever import Hit, _prefixes, _segments, okpd2_match  # noqa: E402
from app.rag import meta  # noqa: E402
from app.rag import okpd2_ref  # noqa: E402
from app.rag import procedural  # noqa: E402
from app.rag.thresholds import _tables, lookup_threshold  # noqa: E402

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
        self.assertIn("окраска — баллы в контексте не указаны", ctx)

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

    def test_suggest_okpd2_only_when_flagged(self):
        # поиск по наименованию (кода нет) → просим предложить код
        self.assertIn("Предполагаемый код ОКПД2",
                      build_navigator_user_prompt("чиллеры", "ctx", suggest_okpd2=True))
        # код указан → подсказку кода не навязываем
        self.assertNotIn("Предполагаемый код ОКПД2",
                         build_navigator_user_prompt("чиллеры", "ctx", okpd2="28.25.13"))


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
        "срок действия акта экспертизы на компоненты",         # склонение «акта» (Приказ №52)
        "какие документы нужны для получения акта экспертизы",  # склонение + документы
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


class TestThresholds(unittest.TestCase):
    """Пороги баллов из примечаний (thresholds.py) — без Qdrant/DeepSeek."""

    def test_tables_parsed(self):
        self.assertGreaterEqual(len(_tables()), 8)  # ~10 таблиц-порогов по разделам

    def test_chillery_threshold_year_stepped(self):
        thr = lookup_threshold(["28.25.13"], "Чиллеры", "XVI")
        self.assertIsNotNone(thr)
        for n in ("270", "320", "480", "542"):  # авторитетная строка «Чиллеры» прим.77
            self.assertIn(n, thr)
        self.assertIn("прим. 77", thr)

    def test_no_match_returns_none(self):
        self.assertIsNone(lookup_threshold(["99.99"], "Несуществующая продукция", "XVI"))

    def test_format_context_injects_threshold_for_target(self):
        # целевой хит по чиллерам без своего min_threshold → порог подтягивается из примечаний
        hit = make_hit(product_name="Чиллеры", section_roman="XVI",
                       okpd2_codes=["28.25.13"], okpd2_match=True, min_threshold=None)
        ctx = format_context([hit])
        self.assertIn("Порог:", ctx)
        self.assertIn("270", ctx)

    def test_flat_threshold_by_code(self):
        # простой порог «не менее N баллов» из примечания-списка по КОДУ (прим. 7)
        thr = lookup_threshold(["22.22.11"], "Изделия пластмассовые упаковочные")
        self.assertIsNotNone(thr)
        self.assertIn("90 баллов", thr)
        self.assertIn("прим. 7", thr)

    def test_flat_threshold_name_disambiguation(self):
        # у кода 32.99.53.130 несколько строк с разными порогами → выбор по наименованию (прим. 31)
        self.assertIn("10 баллов", lookup_threshold(["32.99.53.130"], "Оборудование для практикума"))
        self.assertIn("15 баллов", lookup_threshold(["32.99.53.130"], "Конструктор робототехнический"))

    def test_flat_threshold_none_when_absent(self):
        self.assertIsNone(lookup_threshold(["28.41.1"], "Станки лазерные"))

    def test_flat_threshold_multiline_note9(self):
        # прим.9: пороги нефтегаз-компрессоров идут ОТДЕЛЬНЫМИ строками-ступенями под «код "имя":»
        # (многострочный формат) — раньше терялись, теперь собираются в один порог.
        thr = lookup_threshold(
            ["28.13.24"], "Компрессорные станции на колесных шасси на базе поршневых объемных компрессоров")
        self.assertIsNotNone(thr)
        self.assertIn("170 баллов", thr)  # с 1 января 2023 г.
        self.assertIn("180 баллов", thr)  # с 1 января 2024 г.
        self.assertIn("прим. 9", thr)


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


class TestDialogAnchor(unittest.TestCase):
    """T6: перенос кода-якоря из истории для продолжающих ход вопросов (без «ухода» в другие коды)."""

    def test_anchor_code_from_user_not_assistant(self):
        hist = [
            {"role": "user", "content": "производим станки 28.41.1"},
            {"role": "assistant", "content": "нашлось по коду 26.20.13 ..."},
            {"role": "user", "content": "покажи требования"},
        ]
        self.assertEqual(_anchor_code(hist), "28.41.1")  # код из реплики ПОЛЬЗОВАТЕЛЯ, не ассистента

    def test_anchor_code_most_recent_user_wins(self):
        hist = [
            {"role": "user", "content": "мой код 28.41.1"},
            {"role": "user", "content": "нет, мой код 27.40.33.130"},
        ]
        self.assertEqual(_anchor_code(hist), "27.40.33.130")

    def test_anchor_code_none(self):
        self.assertIsNone(_anchor_code([{"role": "user", "content": "какие требования к насосам"}]))
        self.assertIsNone(_anchor_code(None))

    def test_continuation_bare_commands(self):
        for q in ("да", "покажи", "поясни", "распиши", "давай"):
            self.assertTrue(_is_continuation(q), q)

    def test_continuation_strong_backref(self):
        for q in ("а какой порог?", "сколько баллов надо набрать", "покажи полный перечень операций",
                  "покажи требования", "поясни требования для этой позиции", "какие документы нужны"):
            self.assertTrue(_is_continuation(q), q)

    def test_continuation_false_for_new_product(self):
        # новый продукт по наименованию — НЕ продолжение (иначе ложно заякорит прежний код)
        for q in ("покажи требования к насосам", "какие требования к спецодежде", "мешки для мусора",
                  "требования для станков"):
            self.assertFalse(_is_continuation(q), q)

    def test_continuation_false_when_own_code_or_empty(self):
        self.assertFalse(_is_continuation("требования к 28.41.1"))
        self.assertFalse(_is_continuation(""))


class TestOkpd2Prefixes(unittest.TestCase):
    """T5 (Волна 2): поле okpd2_prefixes для поиска по частичному коду (load_kb)."""

    def test_prefixes_union(self):
        from load_kb import okpd2_prefixes  # ленивый импорт: модуль тянет embeddings
        self.assertEqual(okpd2_prefixes(["26.51.52.120"]),
                         ["26", "26.51", "26.51.52", "26.51.52.120"])
        self.assertTrue({"27.3", "27.32", "27.32.14"} <= set(okpd2_prefixes(["27.3", "27.32.14"])))
        self.assertEqual(okpd2_prefixes([]), [])


class TestProceduralCorpus(unittest.TestCase):
    """Волна 1 шаг 1: парсеры доп. процедурных источников — тело ПП №719 + Приказ ТПП №52."""

    def test_decree_body_criteria_subpoints(self):
        body = load_rules_kb.parse_decree_body(load_rules_kb.BODY_PATH)
        pts = {r["point"] for r in body}
        self.assertTrue({"1а", "1б", "1в", "1г"} <= pts)  # критерии п.1 разбиты по подпунктам
        g = next(r for r in body if r["point"] == "1г")   # критерий «г» = СТ-1
        self.assertIn("сертификат", g["text"].lower())     # «наличие сертификата о происхождении»
        self.assertIn("происхождени", g["text"].lower())
        self.assertIn("отсутстви", g["text"].lower())      # «в случае отсутствия … в приложении»
        self.assertEqual(g["doc_type"], "decree_body")
        self.assertIn("подпункт «г»", g["source_anchor"])
        self.assertNotIn("(в ред", g["text"])              # аннотации редакций вырезаны

    def test_order52_components_and_documents(self):
        order = load_rules_kb.parse_order52(load_rules_kb.ORDER52_PATH)
        self.assertGreater(len(order), 100)
        self.assertTrue(all(r["source_anchor"].startswith("Приказ ТПП РФ №52") for r in order))
        self.assertTrue(all(r["doc_type"] == "tpp_order_52" for r in order))
        p38 = next((r for r in order if r["point"] == "3.8"), None)  # акт на компоненты, срок 3 года
        self.assertIsNotNone(p38)
        self.assertIn("3 года", p38["text"])
        self.assertTrue(any(r["point"].startswith("4.2") for r in order))  # раздел 4 — состав документов


class TestOkpd2Ref(unittest.TestCase):
    """T9: справочник ОКПД2 + переходные ключи ТН ВЭД↔ОКПД2 (okpd2_ref.py) — без сети/модели."""

    def test_okpd2_name_exact_and_fallback(self):
        self.assertEqual(okpd2_ref.okpd2_name("28.13.14.190"), "Насосы прочие")
        self.assertIsNotNone(okpd2_ref.okpd2_name("28.13.14.999"))  # нет листа → фолбэк на родителя
        self.assertIsNone(okpd2_ref.okpd2_name("00.00.00"))  # корня «00» нет — фолбэк упирается в None

    def test_tnved_to_okpd2(self):
        self.assertIn("26.20.11", okpd2_ref.tnved_to_okpd2("8471 30"))     # ЭВМ
        self.assertIn("26.20.11", okpd2_ref.tnved_to_okpd2("8471300000"))  # 10-знач → 6-знач префикс ГС
        self.assertEqual(okpd2_ref.tnved_to_okpd2("0000 00"), [])

    def test_okpd2_to_tnved(self):
        self.assertIn("847130", okpd2_ref.okpd2_to_tnved("26.20.11"))

    def test_suggest_by_name(self):
        codes = [c for c, _n, _s in okpd2_ref.suggest_okpd2_by_name("сверла")]
        self.assertTrue(any(c.startswith("25.73.4") for c in codes))          # сверла → сменный инструмент
        codes2 = [c for c, _n, _s in okpd2_ref.suggest_okpd2_by_name("гидравлические насосы")]
        self.assertTrue(any(c.startswith("28.12.13") for c in codes2))        # → «Насосы гидравлические»
        self.assertEqual(okpd2_ref.suggest_okpd2_by_name("!!! ??? …"), [])    # нет значимых токенов

    def test_extract_tnved(self):
        self.assertEqual(okpd2_ref.extract_tnved("код ТН ВЭД 8471 30 000 0"), "8471 30 000")
        self.assertEqual(okpd2_ref.extract_tnved("сертификат 8536 50"), "8536 50")  # пробел-группировка
        self.assertIsNone(okpd2_ref.extract_tnved("производим насосы 28.13.14"))    # ОКПД2, не ТН ВЭД
        self.assertIsNone(okpd2_ref.extract_tnved("договор №8471301234"))           # слитный без маркера

    def test_navigator_prompt_tnved_and_suggestions(self):
        p = build_navigator_user_prompt("вопрос", "ctx", "26.20.11", tnved=("8471 30", ["26.20.11"]))
        self.assertIn("ТН ВЭД", p)
        self.assertIn("переходному ключу", p)
        self.assertNotIn("указан пользователем", p)  # код из перевода ТН ВЭД, не от пользователя
        p2 = build_navigator_user_prompt("вопрос", "ctx", okpd2_suggestions=[("25.73.40.110", "Сверла")])
        self.assertIn("классификатор", p2.lower())
        self.assertIn("25.73.40.110", p2)


class TestAnswerStream(unittest.TestCase):
    """T18: контракт стриминга на РАННЕМ пути (meta — без сети/Qdrant/DeepSeek). LLM-путь
    (реальные delta от DeepSeek) проверяется живым e2e-скриптом, здесь — только пламбинг."""

    def test_plan_answer_meta_returns_ready_answer(self):
        planned = pipeline_mod._plan_answer("что ты умеешь")
        self.assertIsInstance(planned, pipeline_mod.Answer)  # ранний путь — готовый Answer, не _Plan
        self.assertEqual(planned.text, meta.HELP)

    def test_stream_meta_yields_delta_then_done(self):
        events = list(pipeline_mod.answer_stream("привет"))
        self.assertEqual([k for k, _ in events], ["delta", "done"])  # ровно один кусок + финал
        self.assertEqual(events[0][1], meta.GREETING)                # текст отдан одним delta
        done = events[-1][1]
        self.assertIsInstance(done, pipeline_mod.Answer)
        self.assertEqual(done.text, meta.GREETING)
        self.assertEqual(done.hits, [])

    def test_stream_matches_nonstream_on_early_path(self):
        # Ранний путь (meta) должен давать тот же текст, что и non-stream answer() — оба через заготовку.
        streamed = "".join(t for k, t in pipeline_mod.answer_stream("спасибо!") if k == "delta")
        self.assertEqual(streamed, pipeline_mod.answer("спасибо!").text)
        self.assertEqual(streamed, meta.THANKS)


if __name__ == "__main__":
    unittest.main()
