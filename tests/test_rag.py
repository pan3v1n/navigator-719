"""Юнит-тесты чистой логики RAG-слоя (без Qdrant, сети, модели и DeepSeek).

Покрывают детерминированные функции: иерархическое сопоставление ОКПД2, локальный
BM25 (токенизация/стемминг/векторы), сборку контекста и промпта, гарантию пометки.
Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.prompts import build_navigator_user_prompt  # noqa: E402
from app.rag import sparse  # noqa: E402
from app.rag import pipeline as pipeline_mod  # noqa: E402
from app.rag.pipeline import (  # noqa: E402
    _anchor_code,
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
from app.rag import translate  # noqa: E402
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


class TestEnvExampleMatchesSettings(unittest.TestCase):
    """R21: `.env.example` — единственная документация настроек для оператора, и она обязана
    совпадать с `Settings`. Расходилась на 7 переменных (включая флаги процедурной ветки — оператор
    не мог узнать, чем она управляется) и содержала `TELEGRAM_BOT_TOKEN`, которого в конфиге нет."""

    def test_no_drift_between_config_and_example(self):
        from app.core.config import Settings
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        documented = set(re.findall(r"^([A-Z_]+)=", text, re.M))
        declared = set(Settings.model_fields)
        self.assertEqual(declared - documented, set(), "нет в .env.example")
        self.assertEqual(documented - declared, set(), "лишнее в .env.example (нет в Settings)")


class TestSectionTitle(unittest.TestCase):
    """R28: название раздела не должно задваиваться — оно идёт и в ответ, и в вектор."""

    def test_doubled_title_is_cleaned(self):
        from app.rag.retriever import _clean_section_title
        self.assertEqual(
            _clean_section_title("Продукция судостроения\n\nXVIII. Продукция судостроения"),
            "Продукция судостроения")
        self.assertEqual(_clean_section_title("XVIII. Продукция судостроения"),
                         "Продукция судостроения")
        self.assertEqual(_clean_section_title("Продукция судостроения"), "Продукция судостроения")
        self.assertEqual(_clean_section_title(None), "")

    def test_dot_in_name_is_not_a_roman_prefix(self):
        # точка внутри названия не должна съедать его начало
        from app.rag.retriever import _clean_section_title
        self.assertEqual(_clean_section_title("Оборудование им. Иванова"),
                         "Оборудование им. Иванова")

    def test_parser_extracts_single_title(self):
        import sys
        from pathlib import Path
        s = str(Path(__file__).resolve().parents[1] / "scripts")
        if s not in sys.path:
            sys.path.insert(0, s)
        from structure_kb import section_title
        header = "# XVIII. Продукция судостроения\n\nXVIII. Продукция судостроения"
        self.assertEqual(section_title(header), "Продукция судостроения")
        self.assertEqual(section_title("", "запасное"), "запасное")

    def test_corpus_has_no_doubled_titles(self):
        # страховка от возврата дефекта при перепарсинге корпуса
        import glob
        import json
        bad = 0
        for f in glob.glob(str(ROOT / "knowledge_base" / "pp719" / "structured" / "*.json")):
            for r in json.loads(open(f, encoding="utf-8").read()):
                if "\n" in (r.get("section_title") or ""):
                    bad += 1
        self.assertEqual(bad, 0)


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
    # Тесты `_ensure_disclaimer` удалены вместе с функцией (R3): пометка в тело ответа не
    # добавляется намеренно, её гарантируют UI и выгрузки — см. tests/test_export.py.

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

    def test_component_only_block_is_shown_as_requirement(self):
        """R6: блок без operations, но с текстом в component — это ТРЕБОВАНИЕ, а не заголовок.

        Регресс: `_hit_operations` читал только `operations`, и такие блоки не попадали в контекст
        вообще — молча терялось 56 449 символов требований по 84 позициям (у 77 из них есть другие
        операции, поэтому потеря была незаметна). Класс потерянного — «обязательные требования»:
        права на КД/ТД, сервисный центр."""
        rights = ("наличие у юридического лица - налогового резидента прав на конструкторскую "
                  "и техническую документацию на срок не менее 5 лет")
        hit = make_hit(requirement_blocks=[
            {"component": rights, "operations": []},
            {"component": "несущая рама", "operations": [{"text": "сварка рамы", "points": 9}]},
        ])
        ctx = format_context([hit])
        self.assertIn(rights, ctx)                       # требование видно
        self.assertIn("сварка рамы — 9 балл.", ctx)      # обычные операции не сломаны
        # баллы требованию не приписаны → промпт выведет его как обязательное
        self.assertIn(rights + " — баллы в контексте не указаны", ctx)
        # заголовок узла у блока С операциями требованием НЕ становится
        self.assertNotIn("несущая рама — баллы", ctx)

    def test_component_only_keeps_source_order(self):
        # порядок первоисточника важен: обязательные требования стоят первыми и не должны
        # уезжать в хвост, где их срежет кап MAX_OPS_TARGET
        hit = make_hit(requirement_blocks=[
            {"component": "первое обязательное требование позиции", "operations": []},
            {"component": "узел", "operations": [{"text": "вторая операция", "points": 5}]},
        ])
        ops = [o["text"] for o in pipeline_mod._hit_operations(hit)]
        self.assertEqual(ops, ["первое обязательное требование позиции", "вторая операция"])

    def test_empty_component_without_operations_ignored(self):
        hit = make_hit(requirement_blocks=[{"component": "", "operations": []},
                                           {"component": None, "operations": []}])
        self.assertEqual(pipeline_mod._hit_operations(hit), [])

    def test_fragmented_position_marked_incomplete(self):
        """R29: позиция с расколотой общей ячейкой помечается как НЕПОЛНАЯ.

        «Хладон-218» показывает 4 операции по 100 баллов при пороге 100 — без пометки модель
        заключила бы «порог набирается», хотя это лишь обрывок общего списка группы (остальные
        операции значатся у «Хладон-125 ХП» и «Хладон 14»). Маркер намеренно тот же, что у
        мега-позиций, — тогда срабатывает правило 2а промпта без его правки."""
        from app.rag import fragments
        hit = make_hit(product_name="Хладон-218 (октафторпропан)", section_roman="XXI",
                       okpd2_codes=["20.14.19.120"],
                       requirement_blocks=[{"operations": [{"text": "пиролиз", "points": 100}]}])
        ctx = format_context([hit])
        self.assertIn("СПИСОК ОПЕРАЦИЙ НЕПОЛНЫЙ", ctx)
        self.assertIn("входит в группу с ОБЩИМИ требованиями", ctx)
        # обычная позиция пометку не получает
        self.assertNotIn("входит в группу с ОБЩИМИ требованиями", format_context([make_hit()]))
        self.assertTrue(fragments.is_fragmented("Хладон-218 (октафторпропан)"))
        self.assertFalse(fragments.is_fragmented("Подшипники шариковые или роликовые"))

    def test_fragmented_lookup_is_robust(self):
        from app.rag import fragments
        # многострочное наименование исходника → сверяем по ПЕРВОЙ строке
        self.assertTrue(fragments.is_fragmented("Хладон-218 (октафторпропан)\nХладон-23"))
        self.assertTrue(fragments.is_fragmented("  хладон-218   (октафторпропан)  "))  # регистр/пробелы
        self.assertFalse(fragments.is_fragmented(None))
        self.assertFalse(fragments.is_fragmented(""))

    def test_fragmented_list_covers_known_groups(self):
        # список сгенерирован из чанков детерминированно; страхуемся от его потери/обнуления
        from app.rag import fragments
        for name in ("Аддитивные установки экструзии материала", "Светодиоды зеленого диапазона",
                     "Хладон-125 ХП", "Мономер-6 технический (гексафторпропилен технический)"):
            self.assertTrue(fragments.is_fragmented(name), name)

    def test_inherited_group_requirements_are_attributed(self):
        """R6 шаг 3: позиция без своих требований получает требования ГРУППЫ — но с атрибуцией.

        «Автокраны» (29.10.51) в приложении имеют пустую ячейку требований: они входят в группу
        «Краны грузоподъемные стрелкового типа» (28.22.14.125). Подмены быть не должно — в контексте
        обязана стоять строка, называющая позицию-источник, иначе эксперт примет групповые
        требования за собственные и не заметит подмены (гард такое не ловит)."""
        hit = make_hit(product_name="Автокраны", section_roman="III",
                       okpd2_codes=["29.10.51"], requirement_blocks=[])
        ctx = format_context([hit])
        self.assertIn("ТРЕБОВАНИЯ ГРУППЫ", ctx)
        self.assertIn("Краны грузоподъемные стрелкового типа", ctx)   # источник назван
        self.assertIn("28.22.14.125", ctx)                            # и его код тоже
        self.assertIn("Ключевые операции группы:", ctx)               # заголовок отличается от «своих»

    def test_own_requirements_are_not_replaced_by_group(self):
        # у позиции есть свои требования → наследование не включается
        hit = make_hit(product_name="Автокраны", section_roman="III", okpd2_codes=["29.10.51"],
                       requirement_blocks=[{"operations": [{"text": "сварка стрелы", "points": 7}]}])
        ctx = format_context([hit])
        self.assertNotIn("ТРЕБОВАНИЯ ГРУППЫ", ctx)
        self.assertIn("сварка стрелы — 7 балл.", ctx)

    def test_inheritance_lookup_keyed_by_section(self):
        from app.rag import inheritance
        self.assertIsNotNone(inheritance.lookup("III", "Автокраны"))
        # тот же наименование в ЧУЖОМ разделе не должно наследовать
        self.assertIsNone(inheritance.lookup("XVIII", "Автокраны"))
        self.assertIsNone(inheritance.lookup(None, None))
        # сноски и регистр в наименовании не мешают
        self.assertIsNotNone(inheritance.lookup("III", " автокраны <9> "))

    def test_inheritance_key_tolerates_trailing_punctuation(self):
        """D6: имя-перечень из таблицы оставляет висячую « ;» после снятия сноски.

        Из-за неё ключ позиции расходился с ключом записи базы, родитель «не находился»,
        и 46 позиций молча оставались без требований — при живом родителе в базе."""
        from app.rag import inheritance

        self.assertEqual(
            inheritance._key("XVIII", "Оборудование системы опознавания судов <9>;\nкодирующее"),
            inheritance._key("XVIII", "Оборудование системы опознавания судов"),
        )
        # позиция из группы «опознавания судов» действительно наследует требования
        parent = inheritance.lookup("XVIII", "судовая система охранного оповещения <9>;\nсудовая земная")
        self.assertIsNotNone(parent, "восстановленная позиция XVIII осталась без требований группы")
        self.assertTrue(parent["operations"], "у родителя пустой список требований")

    def test_inheritance_map_keys_are_normalised(self):
        """Инвариант формата карты: ключ не заканчивается пунктуацией — иначе рантайм промахнётся."""
        from app.rag import inheritance

        parents, children = inheritance._map()
        bad = [k for k in list(children) + list(parents) if k != k.strip().rstrip(";,. ")]
        self.assertEqual(bad[:5], [], f"ключи с висячей пунктуацией: {len(bad)}")

    def test_inheritance_key_rule_matches_map_builder(self):
        """Карту строит скрипт, читает рантайм — правила ключа обязаны совпадать до символа."""
        from app.rag import inheritance
        from scripts.diag_orphan_requirements import _map_key

        for section, name in (
            ("XVIII", "Оборудование системы опознавания судов <9>;\nкодирующее устройство"),
            ("III", " Автокраны <9> "),
            ("XXI", "Хладон-125 ХП,"),
            (None, None),
        ):
            self.assertEqual(_map_key(section, name), inheritance._key(section, name))

    def test_fragmented_positions_excluded_from_inheritance(self):
        """R29 и R6 не должны конфликтовать: у позиции с расколотой ячейкой родитель — оборванная
        вводная, наследовать от него нельзя (потомок получил бы фразу без списка)."""
        from app.rag import fragments, inheritance
        for name in ("Аддитивные установки экструзии материала", "Хладон-125 ХП"):
            self.assertTrue(fragments.is_fragmented(name), name)
            self.assertIsNone(inheritance.lookup("I", name))
            self.assertIsNone(inheritance.lookup("XXI", name))

    def test_format_context_ranks_relevant_ops(self):
        # мега-продукт: релевантная операция стоит ПОСЛЕ порога усечения
        from app.rag.pipeline import MAX_OPS_TARGET
        fillers = [{"text": f"операция номер {i}", "points": None} for i in range(MAX_OPS_TARGET)]
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

    def test_structure_references_are_procedural(self):
        """P1: ссылка на структуру постановления — процедурный вопрос.

        Регресс: «что говорит подпункт г пункта 1 постановления 719» не ловил ни один маркер и
        уходил на ТОВАРНЫЙ путь. В контекст попадали позиции приложения, модель честно писала,
        что они нерелевантны, и объясняла подпункт ВЕРНО — но из собственных знаний, а не из
        корпуса. Ответ по процедуре оказывался НЕ ЗАЗЕМЛЁН, а faithfulness-гард этого не ловит:
        выдумки в числах нет, есть неподтверждённое утверждение.

        Формулировки взяты из РЕАЛЬНЫХ вопросов июльского теста — все они уходили не туда."""
        for q in ("что говорит подпункт г пункта 1 постановления 719",
                  "где найти полный перечень документов из подпункта «а» пункта 1 постановления №719?",
                  'какие именно документы из подпунктов "а", "б" и "г" пункта 1 ПП №719 требуются',
                  "какие критерии подтверждения производства российской пром продукции?"):
            self.assertTrue(procedural.is_procedural(q), f"НЕ распознан процедурный: {q!r}")

    def test_structure_patterns_do_not_grab_product_questions(self):
        """Узость намеренная: «пункт» и «подпункт» пестрят и на товарной стороне.

        Свип по 604 реальным вопросам июльского теста дал 5 новых срабатываний, и все пять —
        настоящие процедурные вопросы. Ложных на товарных запросах — ноль; здесь фиксируем
        границу, чтобы расширение шаблонов не прошло незамеченным."""
        for q in ("какие требования к чиллерам по пункту 5 приложения",
                  "сколько баллов даёт подпункт про сварку кузова",
                  "производим насосы, какой пункт приложения применим",
                  "требования к автокранам 29.10.51"):
            self.assertFalse(procedural.is_procedural(q), f"Ложное срабатывание на: {q!r}")

    def test_footnote_references_are_procedural(self):
        """D3: вопрос об определении сноски приложения — процедурный.

        Определение сноски товарным путём недостижимо в принципе: в коллекцию pp719 идут
        structured/*.json (продукты), а сами чанки приложения не индексируются. До правки
        `rules_topic` тему УЖЕ определял верно, но маршрут случался раньше — та же механика,
        что в P1. Отдельное правило нужно потому, что вопрос про сноску почти всегда содержит
        слово «требование», а оно уводит на товарный путь."""
        for q in ("что означает сноска 44 в приложении",
                  "что значит <44> в требованиях",
                  "сноска 6 к требованию о документации",
                  "поясните сноску 29"):
            self.assertTrue(procedural.is_procedural(q), f"НЕ распознан процедурный: {q!r}")

    def test_footnote_rule_yields_to_product_code(self):
        """Код ОКПД2 в запросе отменяет сносочное правило: это товарный вопрос со сноской-довеском.

        Свип по 538 уникальным реальным вопросам июльского теста: правило не изменило маршрут
        НИ ОДНОМУ вопросу (ложных срабатываний ноль). Здесь фиксируем границу."""
        for q in ("требования к автокранам 28.22.14 и что значит сноска 6",
                  "какие требования к чиллерам 28.25.13"):
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


class TestDeflectionHonesty(unittest.TestCase):
    """R4: фолбэк не должен врать о состоянии сервиса.

    Регресс: единственное сообщение утверждало «процедурный регламент в базу пока не загружен».
    После индексации Правил / тела ПП №719 / Приказа №52 это ложь — и звучала она ровно в момент
    аварии, когда пользователь и так не получил ответа. Теперь два сообщения под две причины."""

    BOTH = ("DEFLECTION", "DEFLECTION_DISABLED")

    def _texts(self):
        return [(n, getattr(procedural, n)) for n in self.BOTH]

    def test_no_claim_that_corpus_is_missing(self):
        for name, text in self._texts():
            low = text.lower()
            for lie in ("не загружен", "не загружена", "в базу пока", "регламент в базу"):
                self.assertNotIn(lie, low, f"{name} утверждает, что корпус не загружен")

    def test_no_invented_numbers_or_deadlines(self):
        # порядок действий и сроки не выдумываем — в сообщениях не должно быть ни баллов, ни сроков
        for name, text in self._texts():
            self.assertEqual(claim_numbers(text), [], f"{name}: выдуманные баллы/проценты")
            self.assertEqual(unverified_deadlines(text, ""), [], f"{name}: выдуманный срок")

    def test_both_point_to_primary_sources(self):
        for name, text in self._texts():
            self.assertIn("ГИСП", text, name)
            self.assertIn("719", text, name)
            self.assertIn("52", text, name)          # Приказ ТПП РФ №52 — состав документов
            self.assertIn("ТПП", text, name)

    def test_causes_are_distinguishable(self):
        # техсбой → предлагаем повторить; выключено настройкой → повтор не поможет, честно об этом
        self.assertIn("повторить", procedural.DEFLECTION.lower())
        self.assertIn("отключен", procedural.DEFLECTION_DISABLED.lower())
        self.assertIn("не поможет", procedural.DEFLECTION_DISABLED.lower())
        self.assertNotEqual(procedural.DEFLECTION, procedural.DEFLECTION_DISABLED)

    def test_pipeline_picks_message_by_cause(self):
        orig_flag, orig_search = (pipeline_mod.settings.PROCEDURAL_ANSWER_FROM_RULES,
                                  pipeline_mod.search_rules)
        try:
            pipeline_mod.settings.PROCEDURAL_ANSWER_FROM_RULES = False
            off = pipeline_mod._answer_procedural("как внести в реестр", "как внести в реестр")
            self.assertEqual(off.text, procedural.DEFLECTION_DISABLED)

            pipeline_mod.settings.PROCEDURAL_ANSWER_FROM_RULES = True
            pipeline_mod.search_rules = lambda *a, **k: []  # корпус недоступен/пуст
            down = pipeline_mod._answer_procedural("как внести в реестр", "как внести в реестр")
            self.assertEqual(down.text, procedural.DEFLECTION)
        finally:
            pipeline_mod.settings.PROCEDURAL_ANSWER_FROM_RULES = orig_flag
            pipeline_mod.search_rules = orig_search


class TestOptionalLookupsDegrade(unittest.TestCase):
    """R4: падение Qdrant в НЕОБЯЗАТЕЛЬНЫХ обращениях не должно превращаться в 503.

    `search_rules`/`search_cases` обещают в docstring пустой список при недоступном Qdrant, но
    оборачивали только `collection_exists` — исключение из самого запроса улетало наверх, и
    честный процедурный дефер был недостижим."""

    def setUp(self):
        from app.rag import retriever as r
        self.r = r
        self._orig = r._client

        class _Dying:
            def collection_exists(self, name):  # noqa: ARG002
                return True

            def query_points(self, **kw):
                raise ConnectionError("Qdrant недоступен")

        r._client = lambda: _Dying()

    def tearDown(self):
        self.r._client = self._orig

    def test_rules_and_cases_return_empty_instead_of_raising(self):
        self.assertEqual(self.r.search_rules("порядок внесения в реестр"), [])
        self.assertEqual(self.r.search_cases("насосы"), [])


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


class TestDocumentListRetrieval(unittest.TestCase):
    """P2: вопрос о составе документов — кластер жалоб №1 июльского теста (31 упоминание).

    Квота K10 доводит до окна нужный ДОКУМЕНТ, но внутри него раздел 4 Приказа №52 проигрывал по
    рангу: его пункты длинные, а короткие пункты про сроки и печати содержат те же слова. Ответ
    честно писал, что «перечень содержится в разделе 4 (в контексте не представлен)»."""

    def test_detects_document_list_questions(self):
        from app.rag.retriever import asks_document_list

        for q in ("какие документы нужны для акта экспертизы",
                  "перечень документов для подачи заявки",
                  "что подготовить для включения в реестр",
                  "какой пакет документов собрать",
                  "состав документов для подтверждения производства"):
            self.assertTrue(asks_document_list(q), q)

    def test_does_not_fire_on_other_procedural_questions(self):
        """Детектор узкий: он забирает половину окна, и ложное срабатывание вытеснит норму."""
        from app.rag.retriever import asks_document_list

        for q in ("сроки рассмотрения заявления о включении в реестр",
                  "что делать при отказе ТПП в выдаче акта экспертизы",
                  "как внести изменения в реестровую запись",
                  "требования к прицепам по 719"):
            self.assertFalse(asks_document_list(q), q)

    def test_doc_list_point_is_not_truncated_by_common_cap(self):
        """Перечень стоит В КОНЦЕ пункта: общий кап оставлял от п. 4.1 одну вводную фразу."""
        long_text = "Вводная. " + "А" * pipeline_mod.RULES_TEXT_CAP + " КОНЕЦ-ПЕРЕЧНЯ"
        ctx = format_rules_context([{"point": "4.1", "section_roman": "4",
                                     "section_title": "Документы", "text": long_text,
                                     "_doc_list": True}])
        self.assertIn("КОНЕЦ-ПЕРЕЧНЯ", ctx)
        ctx_capped = format_rules_context([{"point": "4.1", "section_roman": "4",
                                            "section_title": "Документы", "text": long_text}])
        self.assertNotIn("КОНЕЦ-ПЕРЕЧНЯ", ctx_capped)

    def test_parent_intro_reaches_context(self):
        """«4.2.1. Правоустанавливающие документы…» без вводной родителя — список неизвестно к чему."""
        ctx = format_rules_context([{
            "point": "4.2.1", "section_roman": "4", "section_title": "Документы",
            "text": "4.2.1. Правоустанавливающие и регистрационные документы заявителя: копия устава.",
            "parent_intro": "4.2. К заявке на включение сведений в реестр прилагаются следующие документы",
        }])
        self.assertIn("прилагаются следующие документы", ctx)
        self.assertIn("копия устава", ctx)


class TestTableOutput(unittest.TestCase):
    """T17: структурируемые данные выводятся таблицей — запрос заказчика (ОТЧ, расширение №4).

    Данные для таблиц уже есть (T2 пороги по годам, T3 обязательные/балльные), но подавались
    прозой, то есть сделанная работа не доходила до глаз пользователя."""

    def test_navigator_prompt_has_table_rule(self):
        from app.core.prompts import NAVIGATOR_SYSTEM_PROMPT

        self.assertIn("ТАБЛИЦЫ", NAVIGATOR_SYSTEM_PROMPT)
        self.assertIn("|---|", NAVIGATOR_SYSTEM_PROMPT)
        # число в ячейке обязано остаться проверяемым для faithfulness-гарда
        self.assertIn("балл", NAVIGATOR_SYSTEM_PROMPT.split("ТАБЛИЦЫ")[1][:800])

    def test_procedural_prompt_has_table_rule(self):
        """Состав документов и сроки — первые кандидаты на таблицу (кластер жалоб №1)."""
        from app.core.prompts import PROCEDURAL_SYSTEM_PROMPT

        tail = PROCEDURAL_SYSTEM_PROMPT.split("ТАБЛИЦЫ")
        self.assertGreater(len(tail), 1, "в процедурном промпте нет правила таблиц")
        rule = tail[1][:900]
        self.assertIn("Документ", rule)
        self.assertIn("Срок", rule)
        self.assertIn("рабочих дней", rule)  # единица измерения остаётся в ячейке
        self.assertIn("НЕ таблицей", rule)   # порядок действий — прозой

    def test_frontend_renders_tables_and_hides_partial_ones(self):
        """Рендер таблиц и защита от «палок» во время стриминга. JS-раннера в проекте нет, поэтому
        проверяем инварианты файла — чтобы правка не потерялась при следующей."""
        js = (ROOT / "app" / "web" / "static" / "chat.js").read_text(encoding="utf-8")
        self.assertIn("tbl-wrap", js, "рендер Markdown-таблиц пропал")
        self.assertIn("function renderMarkdown(text, streaming)", js)
        self.assertIn("renderMarkdown(acc, true)", js, "стриминг рендерит без флага — вернутся «палки»")
        css = (ROOT / "app" / "web" / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("overflow-x: auto", css.split(".tbl-wrap")[1][:200])  # адаптив на мобильном


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
        self.assertEqual(ans.rule_sources, [])  # дефер — источников нет


class TestProceduralSources(unittest.TestCase):
    """Клик по источникам процедурного ответа: пункты Правил/тела ПП №719/Приказа №52 →
    SourceItem с прямой ссылкой на первоисточник (Контур.Норматив), в порядке [n]."""

    def _rules(self):
        return [
            {"doc_type": "tpp_order_52", "source_anchor": "Приказ ТПП РФ №52, п. 7",
             "text": "Уполномоченная ТПП в течение 3 рабочих дней регистрирует заявление."},
            {"doc_type": "rules_registry", "source_anchor": "Правила ведения реестра, п. 12",
             "text": "Минпромторг включает сведения о продукции в реестр."},
            {"doc_type": "decree_body", "source_anchor": "ПП №719, п. 1, подпункт «г»",
             "text": "подтверждается сертификатом СТ-1."},
        ]

    def test_builds_ordered_clickable_sources(self):
        from app.api.chat import _sources_from_rules
        src = _sources_from_rules(self._rules())
        self.assertEqual(len(src), 3)
        # порядок [n] сохранён + метка пункта в подписи
        self.assertEqual(src[0].product_name, "Приказ ТПП РФ №52, п. 7")
        # Приказ №52 — отдельный документ 505398
        self.assertIn("documentId=505398", src[0].url)
        # Правила — раздел документа 719 (якорь h14240)
        self.assertIn("documentId=506899", src[1].url)
        self.assertIn("h14240", src[1].url)
        # тело ПП №719 — сам 719 без якоря раздела Правил
        self.assertIn("documentId=506899", src[2].url)
        self.assertNotIn("h14240", src[2].url)
        # текст-фрагмент для точной прокрутки браузером
        self.assertIn(":~:text=", src[0].url)

    def test_empty_rules_no_sources(self):
        from app.api.chat import _sources_from_rules
        self.assertEqual(_sources_from_rules([]), [])

    def test_answer_procedural_carries_rule_sources(self):
        # grounded-ветка кладёт найденные пункты в rule_sources (тот же порядок, что в контексте [n])
        rules = self._rules()
        orig_search, orig_client = pipeline_mod.search_rules, pipeline_mod._client
        pipeline_mod.search_rules = lambda *a, **k: rules

        class _Usage:
            prompt_tokens = completion_tokens = 1

        class _Msg:
            content = "Порядок по шагам [1][2][3]."

        class _Resp:
            choices = [type("C", (), {"message": _Msg()})()]
            usage = _Usage()

        class _Client:
            chat = type("Ch", (), {"completions": type(
                "Co", (), {"create": staticmethod(lambda *a, **k: _Resp())})()})()

        pipeline_mod._client = lambda: _Client()
        try:
            ans = pipeline_mod._answer_procedural("порядок внесения в реестр", "порядок внесения в реестр")
        finally:
            pipeline_mod.search_rules, pipeline_mod._client = orig_search, orig_client
        self.assertEqual(len(ans.rule_sources), 3)
        self.assertEqual(ans.rule_sources[0]["doc_type"], "tpp_order_52")


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

    def test_threshold_does_not_leak_up_the_hierarchy(self):
        """R8: порог из примечания для УЗКОГО кода не применяется к более широкой позиции.

        Регресс: матч кодов был симметричным, и порог «утекал» вверх по иерархии. «Устройства
        ввода или вывода» (26.20.16) получали порог, заданный для «Принтеров для печати этикеток»
        (26.20.16.120) и «Сканеров штрихкодов» (26.20.16.150). Число дословно из первоисточника,
        поэтому faithfulness-гард молчал."""
        from app.rag.thresholds import _code_applies
        self.assertIsNone(lookup_threshold(["26.20.16"], "Устройства ввода или вывода", "IX"))
        # направление: предок→потомок можно, потомок→предок нельзя
        self.assertTrue(_code_applies("22.22", "22.22.11"))
        self.assertTrue(_code_applies("22.22", "22.22"))
        self.assertFalse(_code_applies("22.22.11", "22.22"))
        self.assertFalse(_code_applies("22.11", "22.22"))

    def test_group_level_threshold_is_labelled(self):
        # порог ветки-предка показываем, но честно называем уровень — иначе читается как свой
        own = lookup_threshold(["22.22"], "Изделия пластмассовые упаковочные")
        child = lookup_threshold(["22.22.11"], "Изделия пластмассовые упаковочные")
        self.assertIn("90 баллов", own)
        self.assertNotIn("порог задан для группы", own)          # точное совпадение кода
        self.assertIn("порог задан для группы кодов", child)     # унаследован от 22.22

    def test_exact_note_wins_over_narrower_siblings(self):
        # у 15.20.14 есть и точная строка примечания, и узкие («Обувь валяная» 15.20.14.130):
        # после фикса узкие отброшены и остаётся верная
        thr = lookup_threshold(["15.20.14"], "Обувь с верхом из текстильных материалов", "XVII")
        self.assertIsNotNone(thr)
        self.assertNotIn("порог задан для группы", thr)  # это её собственный порог

    def test_operations_model_says_threshold_not_applicable(self):
        """R7: «порога нет в 719» и «порог не нашли» — разные вещи, и путать их нельзя.

        Молчание заставляло модель писать «Порог: в контексте не указан» — читается как пробел в
        данных, это была жалоба №1 теста. Но у 370 позиций требования заданы ПЕРЕЧНЕМ операций без
        баллов, и порога в постановлении для них просто не существует."""
        hit = make_hit(min_threshold=None, okpd2_codes=["99.99"], product_name="Нечто без порога",
                       payload={"requirement_type": "operations"},
                       requirement_blocks=[{"operations": [{"text": "сборка", "points": None}]}])
        self.assertIn("Порог: не предусмотрен", format_context([hit]))

    def test_points_model_without_threshold_stays_silent(self):
        # у позиции есть баллы → порог обязан быть; не нашли — молчим, а не заявляем «не предусмотрен»
        hit = make_hit(min_threshold=None, okpd2_codes=["99.99"], product_name="Нечто с баллами",
                       payload={"requirement_type": "points"},
                       requirement_blocks=[{"operations": [{"text": "сборка", "points": 5}]}])
        ctx = format_context([hit])
        self.assertNotIn("Порог: не предусмотрен", ctx)
        # и на смешанном типе без найденных баллов тоже молчим (возможна потеря при разборе)
        hit2 = make_hit(min_threshold=None, okpd2_codes=["99.99"], product_name="Нечто смешанное",
                        payload={"requirement_type": "mixed"},
                        requirement_blocks=[{"operations": [{"text": "сборка", "points": None}]}])
        self.assertNotIn("Порог: не предусмотрен", format_context([hit2]))

    def test_threshold_parses_footnote_and_multicode_rows(self):
        """R7: два формата строк примечаний, которые парсер раньше терял целиком."""
        # сноска между кодом и наименованием: «19.20.31 <11> "Пропан и бутан сжиженные" - …»
        self.assertIn("300 баллов", lookup_threshold(["19.20.31"], "Пропан и бутан сжиженные", "XXI"))
        # несколько кодов через запятую: «из 20.13.43.110, из 20.13.43.111, из 20.13.43.119 "Сода…"»
        for code in ("20.13.43.110", "20.13.43.111", "20.13.43.119"):
            thr = lookup_threshold([code], "Сода кальцинированная", "XXI")
            self.assertIsNotNone(thr, code)
            self.assertIn("460 баллов", thr)

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


class TestBlockNote(unittest.TestCase):
    """D9: условие блока (`note`) доезжает до контекста и остаётся при СВОЁМ узле.

    До 14.08.2026 поле не читалось нигде в рантайме, хотя лежало в payload: 503 блока у 259 позиций,
    из них 305 — рядом с балльными операциями. Терялись пороги отдельных узлов, пометки
    «обязательное требование», правила начисления и периоды действия.
    """

    NODE = [
        {"component": "криогенный насос низкого давления", "note": "не менее 100 баллов",
         "operations": [{"text": "производство насоса", "points": 20}]},
        {"component": "узел учета сжиженного природного газа", "note": "не менее 40 баллов",
         "operations": [{"text": "изготовление блока управления", "points": 15}]},
    ]

    def test_note_reaches_context(self):
        ctx = format_context([make_hit(requirement_blocks=self.NODE)])
        self.assertIn("криогенный насос низкого давления — не менее 100 баллов", ctx)
        self.assertIn("узел учета сжиженного природного газа — не менее 40 баллов", ctx)

    def test_node_threshold_never_becomes_position_threshold(self):
        """Порог узла в строке «Порог:» — тот самый класс «правдоподобно, но неверно»."""
        ctx = format_context([make_hit(requirement_blocks=self.NODE)])
        for line in ctx.splitlines():
            if line.strip().startswith("Порог:"):
                self.fail(f"порог узла подставился как порог позиции: {line.strip()}")

    def test_position_threshold_and_node_thresholds_coexist(self):
        ctx = format_context([make_hit(min_threshold="не менее 250 баллов",
                                       requirement_blocks=self.NODE)])
        self.assertIn("Порог: не менее 250 баллов", ctx)
        self.assertIn("криогенный насос низкого давления — не менее 100 баллов", ctx)

    def test_long_note_is_clipped_and_says_so(self):
        """Молча обрезанное условие превращает несколько порогов в один — резать можно только вслух."""
        note = "; ".join(f"с 1 января 202{i} г. - не менее {500 + i * 10} баллов" for i in range(9))
        self.assertGreater(len(note), pipeline_mod.NOTE_CAP_OTHER)
        self.assertLess(len(note), pipeline_mod.NOTE_CAP_TARGET)
        blocks = [{"component": "насосные установки", "note": note,
                   "operations": [{"text": "сборка", "points": 10}]}]

        full = format_context([make_hit(okpd2_match=True, requirement_blocks=blocks)])
        self.assertIn("не менее 500 баллов", full)
        self.assertIn("не менее 580 баллов", full)  # у целевого хита условие идёт целиком
        self.assertNotIn("условие показано не полностью", full)

        # тот же блок у КАНДИДАТА (целевой — другой хит, по совпадению кода) режется, но вслух
        short = format_context([make_hit(product_name="Целевая", okpd2_match=True),
                                make_hit(product_name="Кандидат", requirement_blocks=blocks)])
        self.assertIn("условие показано не полностью", short)
        self.assertIn("не менее 500 баллов", short)     # начало условия остаётся
        self.assertNotIn("не менее 580 баллов", short)  # хвост срезан

    def test_clip_keeps_sentence_boundary(self):
        clipped = pipeline_mod._clip_note("первое условие; второе условие; третье условие", 25)
        self.assertTrue(clipped.startswith("первое условие"))
        self.assertNotIn("третье", clipped)
        self.assertIn("условие показано не полностью", clipped)

    def test_note_shown_when_block_has_no_operations(self):
        ctx = format_context([make_hit(requirement_blocks=[
            {"component": "наличие прав на документацию", "note": "обязательное требование"}])])
        self.assertIn("наличие прав на документацию — обязательное требование", ctx)

    def test_inheritance_map_carries_block_intro(self):
        """K4 применялась только к прямому пути: 327 из 331 наследника видели операции без
        требования, частью которого они являются («…НА ТЕРРИТОРИИ РФ следующих операций»)."""
        path = ROOT / "knowledge_base/pp719/inherited_requirements.json"
        if not path.exists():
            self.skipTest("карта наследования не сгенерирована")
        parents = (json.loads(path.read_text(encoding="utf-8")).get("parents") or {})
        if not parents:
            self.skipTest("карта пуста")
        with_intro = sum(1 for p in parents.values()
                         for o in p.get("operations") or [] if o.get("_parent"))
        total = sum(len(p.get("operations") or []) for p in parents.values())
        self.assertGreater(with_intro, total * 0.8,
                           "вводная фраза блока перестала попадать в карту наследования")

    def test_block_without_note_renders_as_before(self):
        ctx = format_context([make_hit(requirement_blocks=[
            {"component": "осуществление на территории РФ следующих операций",
             "operations": [{"text": "сварка", "points": 40}]}])])
        self.assertIn("▸ осуществление на территории РФ следующих операций", ctx)
        self.assertNotIn("—", ctx.split("▸")[1].split("\n")[0])  # вводная без хвоста


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


class TestIndexTextAsymmetry(unittest.TestCase):
    """R9: dense и sparse индексируются РАЗНЫМ текстом.

    До R9 оба канала строились из identity-текста, и BM25 терял свой единственный смысл —
    лексический поиск по формулировкам требований. Гибрид вырождался в «dense + BM25 по четырём
    полям идентичности»."""

    REC = {
        "product_name": "Краны грузоподъемные стрелкового типа",
        "section_roman": "III", "section_title": "Спецмашиностроение",
        "okpd2_codes": ["28.22.14.125"], "min_threshold": "не менее 10 баллов",
        "requirement_blocks": [{"component": "несущая рама",
                                "operations": [{"text": "сварка и покраска стрелы", "points": 7}]}],
        "notes": "примечание к позиции",
    }

    def setUp(self):
        from load_kb import build_embedding_text, build_text  # ленивый: модуль тянет embeddings
        self.dense = build_embedding_text(self.REC)
        self.sparse = build_text(self.REC)

    def test_identity_present_in_both(self):
        for t in (self.dense, self.sparse):
            self.assertIn("Краны грузоподъемные стрелкового типа", t)
            self.assertIn("28.22.14.125", t)
            self.assertIn("не менее 10 баллов", t)

    def test_operations_only_in_sparse_text(self):
        self.assertNotIn("сварка и покраска стрелы", self.dense)  # F1: операции топят dense
        self.assertIn("сварка и покраска стрелы", self.sparse)    # R9: но нужны BM25
        self.assertNotIn("несущая рама", self.dense)
        self.assertIn("несущая рама", self.sparse)

    def test_requirement_phrase_is_searchable_only_via_sparse(self):
        from app.rag import sparse as sp
        q = set(sp.tokenize("сварка стрелы"))
        self.assertFalse(q <= set(sp.tokenize(self.dense)))
        self.assertTrue(q <= set(sp.tokenize(self.sparse)))

    def test_index_all_defaults_to_asymmetric(self):
        import inspect

        import load_kb
        sig = inspect.signature(load_kb.index_all)
        self.assertIs(sig.parameters["text_fn"].default, load_kb.build_embedding_text)
        self.assertIs(sig.parameters["sparse_text_fn"].default, load_kb.build_text)


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

    def test_navigator_prompt_says_when_tnved_not_resolved(self):
        """Код ТН ВЭД дан, но в переходном ключе его нет — пользователь обязан это узнать.

        Иначе позиции подобраны по наименованию, а выглядит как ответ по его коду: молчаливая
        деградация того же класса, что молчаливый отказ наследования."""
        p = build_navigator_user_prompt("вопрос", "ctx", tnved=("9999 99 999", []))
        self.assertIn("НЕ РАЗРЕШЁН", p)
        self.assertIn("9999 99 999", p)
        self.assertIn("по наименованию", p.lower())
        self.assertNotIn("соответствует ОКПД2:", p)  # нечему соответствовать

    def test_resolve_tnved_distinguishes_absent_from_unresolved(self):
        """Три исхода, и «код дан, но не разрешён» обязан отличаться от «кода нет»."""
        from app.rag.pipeline import _resolve_tnved

        self.assertIsNone(_resolve_tnved("производим насосы, требования по 719"))
        self.assertEqual(_resolve_tnved("код ТН ВЭД 9999 99 999 0"), ("9999 99 999", []))
        code, okpd = _resolve_tnved("код ТН ВЭД 8471 30 000 0")
        self.assertEqual(code, "8471 30 000")
        self.assertIn("26.20.11", okpd)


class TestTranslateIntent(unittest.TestCase):
    """T9: детерминированный перевод ТН ВЭД↔ОКПД2 по прямому запросу (translate.py) — без LLM."""

    def test_is_translate_positive(self):
        self.assertTrue(translate.is_translate("умеешь ли ты переводить ОКПД2"))
        self.assertTrue(translate.is_translate("переведи ТН ВЭД 8471 30 000 0 в ОКПД2"))
        self.assertTrue(translate.is_translate("какой ОКПД2 у ТН ВЭД 8471 30"))
        self.assertTrue(translate.is_translate("какой ТН ВЭД у ОКПД2 26.20.11"))
        self.assertTrue(translate.is_translate("конвертация ТН ВЭД в ОКПД2"))

    def test_is_translate_negative(self):
        # обычные товарные вопросы — НЕ перевод (их ведёт навигатор)
        self.assertFalse(translate.is_translate("какой ОКПД2 у гидравлического насоса"))
        self.assertFalse(translate.is_translate("требования к 28.13.14"))
        self.assertFalse(translate.is_translate("подпадает ли под 719 продукция с ТН ВЭД 8471 30"))
        self.assertFalse(translate.is_translate("что ты умеешь"))

    def test_answer_tnved_to_okpd2(self):
        a = translate.answer("переведи ТН ВЭД 8471 30 в ОКПД2")
        self.assertIn("26.20.11", a)
        self.assertIn("ОКПД2", a)

    def test_answer_okpd2_to_tnved(self):
        a = translate.answer("какой ТН ВЭД у ОКПД2 26.20.11")
        self.assertIn("847130", a)
        self.assertIn("ТН ВЭД", a)

    def test_answer_capability_when_no_code(self):
        a = translate.answer("умеешь ли ты переводить ОКПД2")
        self.assertIn("ТН ВЭД", a)
        self.assertIn("ОКПД2", a)
        # без кода — не «переводит», а описывает возможность и просит указать код
        self.assertIn("Назовите код", a)
        self.assertNotIn("Перевод ТН ВЭД в ОКПД2 (по переходному ключу)", a)


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


_FAKE_ID = iter(range(1, 10_000))


class _FakePoint:
    """Точка выдачи Qdrant (id + payload + score) — минимум, который читают `_to_hit`
    и `_case_dense_scores`.

    `id` обязателен (R30): порог релевантности кейсов сопоставляет гибридную выдачу с
    dense-пробой ПО ID. Без него `search_cases` падала внутрь своего же `except` и молча
    возвращала пусто — тесты при этом оставались зелёными, потому что проверяли только
    число прогонов энкодера. Ровно тот случай, когда заглушка скрывает боевой путь."""

    def __init__(self, payload: dict, score: float = 0.9, id: int | None = None):  # noqa: A002
        self.payload = payload
        self.score = score
        self.id = next(_FAKE_ID) if id is None else id


class _FakeQdrant:
    """Заглушка Qdrant-клиента: считает обращения, отдаёт заданную выдачу. Без сети."""

    def __init__(self, points=()):
        self.points = list(points)
        self.calls = 0

    def query_points(self, **kw):
        self.calls += 1
        return type("Res", (), {"points": self.points})()

    def collection_exists(self, name):  # noqa: ARG002 — сигнатура ради совместимости
        return True


class TestClientCached(unittest.TestCase):
    """R15: клиент DeepSeek строится ОДИН раз на процесс.

    Регресс: раньше `_client()` конструировал новый OpenAI на каждый вызов — до трёх на один
    вопрос (контекстуализация → реранкер → генерация), каждый со своим httpx-пулом, то есть
    заново TCP+TLS. На рваной сети с DPI рвётся первым."""

    def setUp(self):
        import openai
        self._orig_ctor = openai.OpenAI
        self._orig_key = pipeline_mod.settings.DEEPSEEK_API_KEY
        pipeline_mod.settings.DEEPSEEK_API_KEY = "test-key"
        pipeline_mod._client.cache_clear()

    def tearDown(self):
        import openai
        openai.OpenAI = self._orig_ctor
        pipeline_mod.settings.DEEPSEEK_API_KEY = self._orig_key
        pipeline_mod._client.cache_clear()

    def test_constructed_once_per_process(self):
        import openai
        calls = []
        openai.OpenAI = lambda **kw: calls.append(kw) or object()

        first = pipeline_mod._client()
        for _ in range(4):
            self.assertIs(pipeline_mod._client(), first)  # тот же объект, а не новый пул
        self.assertEqual(len(calls), 1, "клиент должен строиться один раз на процесс")
        # таймаут/ретраи не потеряны при кэшировании
        self.assertEqual(calls[0]["timeout"], 30.0)
        self.assertEqual(calls[0]["max_retries"], 1)

    def test_empty_key_raises_and_is_not_cached(self):
        # исключение lru_cache не кэширует → после заполнения .env клиент поднимется
        pipeline_mod.settings.DEEPSEEK_API_KEY = ""
        with self.assertRaises(RuntimeError):
            pipeline_mod._client()
        import openai
        calls = []
        openai.OpenAI = lambda **kw: calls.append(kw) or object()
        pipeline_mod.settings.DEEPSEEK_API_KEY = "test-key"
        pipeline_mod._client()
        self.assertEqual(len(calls), 1)


class TestQueryVectorReused(unittest.TestCase):
    """R16: dense-вектор запроса считается ОДИН раз на вопрос.

    Регресс: e5-large прогонялся 3–4 раза по одному и тому же тексту — поиск позиций,
    подстраховка по коду ОКПД2, поиск кейсов, out-of-scope guard. На 2 vCPU это сотни мс впустую."""

    PAYLOAD = {
        "section_roman": "XIX", "section_title": "Насосы", "product_name": "Насосы гидравлические",
        "okpd2_codes": ["28.13.14"], "min_threshold": None, "requirement_blocks": [],
        "source_anchor": "Раздел XIX, поз. 1",
    }

    def setUp(self):
        from app.rag import retriever as r
        self.r = r
        self.calls = 0
        self._orig_embed_r, self._orig_embed_p = r.embed_query, pipeline_mod.embed_query
        self._orig_client = r._client
        self._orig_rerank = pipeline_mod.settings.RERANK_ENABLED

        def counting_embed(text):
            self.calls += 1
            return [0.1] * 4

        r.embed_query = counting_embed
        pipeline_mod.embed_query = counting_embed
        self.fake = _FakeQdrant([_FakePoint(dict(self.PAYLOAD))])
        r._client = lambda: self.fake

    def tearDown(self):
        self.r.embed_query, pipeline_mod.embed_query = self._orig_embed_r, self._orig_embed_p
        self.r._client = self._orig_client
        pipeline_mod.settings.RERANK_ENABLED = self._orig_rerank

    def test_search_embeds_once_even_with_code(self):
        # с кодом ОКПД2 идут ДВА запроса в Qdrant (пул + подстраховка по префиксам),
        # но эмбеддинг должен быть один
        self.r.search("гидравлические насосы", okpd2="28.13.14")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.fake.calls, 2, "подстраховка по коду должна остаться")

    def test_passed_qvec_skips_embedding(self):
        self.r.search("гидравлические насосы", qvec=[0.1] * 4)
        self.r.search_cases("гидравлические насосы", qvec=[0.1] * 4)
        self.r.dense_top1("гидравлические насосы", [0.1] * 4)
        self.assertEqual(self.calls, 0)

    def test_full_hot_path_embeds_once(self):
        """Сквозной путь вопроса без кода: поиск + кейсы + guard = ОДИН прогон энкодера."""
        pipeline_mod.settings.RERANK_ENABLED = False  # реранкер — сеть, здесь не про него
        planned = pipeline_mod._plan_answer("производим гидравлические насосы")
        self.assertIsInstance(planned, pipeline_mod._Plan)
        self.assertEqual(self.calls, 1, "на один вопрос должен приходиться один прогон e5")


class TestCaseRelevanceThreshold(unittest.TestCase):
    """R30: кейс попадает в контекст только при подтверждённой близости.

    Кейс идёт с ВЫСШИМ приоритетом (правило 1а промпта), выше первоисточника, поэтому
    нерелевантный кейс — не шум, а правдоподобная дезинформация. Раньше порога не было
    вообще: `search_cases` отдавала top-3 на любой запрос, включая заведомо посторонние
    («оказываем юридические услуги» → кейс про НИОКР грузового автотранспорта), и заодно
    гасила out-of-scope-гард в пайплайне.

    Отсечка идёт по ЧИСТОМУ dense-косинусу, а не по фьюжн-score: последний мерит ранг, и
    на маленькой коллекции топ почти всегда нормируется в 1.0."""

    def setUp(self):
        from app.rag import retriever as r
        self.r = r
        self._orig_min = r.settings.CASE_RELEVANCE_MIN
        r.settings.CASE_RELEVANCE_MIN = 0.82
        self._orig_client = r._client
        r._client = lambda: type("C", (), {"collection_exists": lambda self, n: True})()
        self._orig_hybrid = r._hybrid
        self._orig_dense = r._case_dense_scores

    def tearDown(self):
        self.r.settings.CASE_RELEVANCE_MIN = self._orig_min
        self.r._client = self._orig_client
        self.r._hybrid = self._orig_hybrid
        self.r._case_dense_scores = self._orig_dense

    def _wire(self, points, dense_map):
        self.r._hybrid = lambda *a, **kw: points
        self.r._case_dense_scores = lambda qvec, probe: dense_map

    def test_relevant_case_passes_and_carries_dense_score(self):
        p = _FakePoint({"query": "нет в приложении", "expert_answer": "путь СТ-1"}, score=1.0, id=7)
        self._wire([p], {7: 0.87})
        out = self.r.search_cases("продукции нет в перечне", qvec=[0.1] * 4)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["_dense"], 0.87)
        self.assertEqual(out[0]["_score"], 1.0, "гибридный ранг сохраняется, фильтр его не подменяет")

    def test_irrelevant_case_is_dropped_despite_top_fusion_score(self):
        """Главный регресс: фьюжн-score 1.0 при чужой теме. Раньше такой кейс проходил."""
        p = _FakePoint({"query": "НИОКР грузовой автотранспорт", "expert_answer": "..."}, score=1.0, id=3)
        self._wire([p], {3: 0.755})  # замеренный косинус для «юридические услуги»
        self.assertEqual(self.r.search_cases("оказываем юридические услуги", qvec=[0.1] * 4), [])

    def test_all_below_threshold_gives_empty_so_guard_stays_armed(self):
        """Пустой список — условие того, чтобы out-of-scope-гард в пайплайне не гасился."""
        pts = [_FakePoint({"query": f"q{i}", "expert_answer": "a"}, score=1.0, id=i) for i in (1, 2, 3)]
        self._wire(pts, {1: 0.80, 2: 0.79, 3: 0.76})
        self.assertEqual(self.r.search_cases("выпекаем хлеб", qvec=[0.1] * 4), [])

    def test_mixed_batch_keeps_only_relevant(self):
        pts = [_FakePoint({"query": f"q{i}", "expert_answer": "a"}, score=1.0, id=i) for i in (1, 2, 3)]
        self._wire(pts, {1: 0.90, 2: 0.81, 3: 0.83})
        got = self.r.search_cases("вопрос", qvec=[0.1] * 4)
        self.assertEqual([c["query"] for c in got], ["q1", "q3"], "порядок гибрида сохранён, q2 отсечён")

    def test_point_missing_from_dense_probe_is_dropped_not_admitted(self):
        """Нет данных о близости — отказ, а не пропуск по умолчанию: лучше не показать, чем чужое."""
        p = _FakePoint({"query": "q", "expert_answer": "a"}, score=1.0, id=42)
        self._wire([p], {1: 0.99})  # id 42 в пробе отсутствует
        self.assertEqual(self.r.search_cases("вопрос", qvec=[0.1] * 4), [])

    def test_threshold_is_configurable(self):
        p = _FakePoint({"query": "q", "expert_answer": "a"}, score=1.0, id=5)
        self._wire([p], {5: 0.83})
        self.assertEqual(len(self.r.search_cases("вопрос", qvec=[0.1] * 4)), 1)
        self.r.settings.CASE_RELEVANCE_MIN = 0.85
        self.assertEqual(self.r.search_cases("вопрос", qvec=[0.1] * 4), [])

    def test_failure_is_logged_not_swallowed_silently(self):
        """Беззвучный отказ выключает всю петлю обучения и снаружи неотличим от «не нашлось»."""
        def boom(*a, **kw):
            raise RuntimeError("qdrant упал")
        self.r._hybrid = boom
        seen = []
        sink = self.r.logger.add(lambda m: seen.append(str(m)), level="WARNING")
        try:
            self.assertEqual(self.r.search_cases("вопрос", qvec=[0.1] * 4), [])
        finally:
            self.r.logger.remove(sink)
        self.assertTrue(any("петля кейсов" in s for s in seen), "сбой петли обязан попасть в лог")


class TestBlockIntroPreserved(unittest.TestCase):
    """K4: вводная фраза блока требований доезжает до контекста.

    Регресс, ради которого правка сделана: `_hit_operations` брал из блока только `operations`,
    а `component` — вводную фразу — отбрасывал. Формулируется же требование именно в ней:
    «осуществление НА ТЕРРИТОРИИ РОССИЙСКОЙ ФЕДЕРАЦИИ следующих технологических операций: …».
    Замер по корпусу: вводную имеют 3733 блока из 3959 (94 %), у 484 она называет территорию —
    это претензия июльского теста «отсутствует отсылка на обязательность осуществления операций
    на территории РФ» (15 упоминаний). Без неё ответ показывал подпункты, не говоря, частью
    какого требования они являются."""

    def _hit(self, blocks):
        from app.rag.retriever import Hit
        return Hit(score=1.0, section_roman="I", section_title="Раздел", product_name="Изделие",
                   okpd2_codes=["01.02"], min_threshold=None, requirement_blocks=blocks,
                   source_anchor=None)

    def test_intro_reaches_operations(self):
        h = self._hit([{"component": "осуществление на территории Российской Федерации операций:",
                        "operations": [{"text": "сварка рамы", "points": 10}]}])
        ops = pipeline_mod._hit_operations(h)
        self.assertEqual(ops[0]["_parent"], "осуществление на территории Российской Федерации операций:")
        self.assertEqual(ops[0]["text"], "сварка рамы", "сама операция не должна пострадать")

    def test_intro_rendered_above_its_operations(self):
        h = self._hit([{"component": "осуществление на территории Российской Федерации операций:",
                        "operations": [{"text": "сварка рамы", "points": 10}]}])
        ctx = pipeline_mod.format_context([h], "рама")
        self.assertIn("территории Российской Федерации", ctx,
                      "условие о территории обязано попасть в контекст")
        self.assertLess(ctx.index("территории Российской"), ctx.index("сварка рамы"),
                        "вводная идёт ПЕРЕД своими операциями")

    def test_duplicate_intro_is_not_repeated(self):
        """У 8.3 % блоков вводная дословно повторяет свою же операцию — это шум, не смысл."""
        h = self._hit([{"component": "сварка рамы",
                        "operations": [{"text": "сварка рамы", "points": 10}]}])
        ops = pipeline_mod._hit_operations(h)
        self.assertIsNone(ops[0].get("_parent"))
        self.assertEqual(pipeline_mod.format_context([h], None).count("сварка рамы"), 1)

    def test_payload_not_mutated(self):
        """Блоки приходят из payload Qdrant и могут быть переиспользованы — портить их нельзя."""
        block = {"component": "вводная:", "operations": [{"text": "оп", "points": 1}]}
        h = self._hit([block])
        pipeline_mod._hit_operations(h)
        self.assertNotIn("_parent", block["operations"][0])

    def test_block_without_operations_still_becomes_requirement(self):
        """R6 шаг 2 не сломан: блок без операций по-прежнему сам является требованием."""
        h = self._hit([{"component": "наличие сервисного центра", "operations": []}])
        ops = pipeline_mod._hit_operations(h)
        self.assertEqual(ops[0]["text"], "наличие сервисного центра")
        self.assertIsNone(ops[0]["points"])


class TestRulesTopic(unittest.TestCase):
    """K10: тема процедурного вопроса определяется детерминированно."""

    def setUp(self):
        from app.rag import retriever
        self.topic = retriever.rules_topic

    def test_documents_go_to_tpp_order(self):
        for q in ("какие документы нужны для внесения в реестр",
                  "что такое акт экспертизы и когда он нужен",
                  "нужен ли сертификат СТ-1", "какие документы нужны для акта экспертизы"):
            self.assertEqual(self.topic(q), "tpp_order_52", q)

    def test_ambiguous_query_has_no_topic(self):
        """Ничья — честный ответ: «перечень документов для подачи» задевает и Приказ №52
        (перечень документов), и Правила (подача). Навязывать тему при равных признаках хуже,
        чем не навязывать: квота тогда раздаёт места поровну и решает релевантность."""
        self.assertIsNone(self.topic("перечень документов для подачи"))

    def test_procedure_goes_to_registry_rules(self):
        for q in ("сроки рассмотрения заявления", "какой порядок подачи заявления через ГИСП",
                  "как внести изменения в реестровую запись", "где посмотреть выписку"):
            self.assertEqual(self.topic(q), "rules_registry", q)

    def test_criteria_go_to_decree_body(self):
        for q in ("какие критерии подтверждения производства установлены",
                  "что говорит подпункт г пункта 1"):
            self.assertEqual(self.topic(q), "decree_body", q)

    def test_unrelated_query_has_no_topic(self):
        self.assertIsNone(self.topic("производим гидравлические насосы"))


class TestRulesQuota(unittest.TestCase):
    """K10: квота на источник — один документ не забирает всё окно.

    Регресс, ради которого квота заведена (замер 12.08.2026): Приказ ТПП №52 — 172 пункта
    из 244, то есть 71 % корпуса, — занимал 24 места из 30 на пяти контрольных запросах,
    а на «сроки рассмотрения заявления» все 6, вытеснив Правила реестра, которые эти сроки
    и устанавливают."""

    def setUp(self):
        from app.rag import retriever as r
        self.r = r
        self._client = r._client
        self._hybrid = r._hybrid
        r._client = lambda: type("C", (), {"collection_exists": lambda self, n: True})()

    def tearDown(self):
        self.r._client = self._client
        self.r._hybrid = self._hybrid

    def _pool(self, doc_types):
        pts = [_FakePoint({"doc_type": dt, "point": f"п. {i}", "text": f"t{i}"},
                          score=1.0 - i / 100, id=i) for i, dt in enumerate(doc_types)]
        self.r._hybrid = lambda *a, **kw: pts

    def test_dominant_document_does_not_take_whole_window(self):
        # пул целиком из Приказа, кроме двух пунктов Правил в самом хвосте
        self._pool(["tpp_order_52"] * 20 + ["rules_registry"] * 4)
        got = self.r.search_rules("сроки рассмотрения заявления", limit=6)
        kinds = {c["doc_type"] for c in got}
        self.assertIn("rules_registry", kinds, "Правила обязаны получить место по квоте темы")
        self.assertEqual(len(got), 6)

    def test_primary_topic_gets_more_slots_than_others(self):
        self._pool(["tpp_order_52"] * 12 + ["rules_registry"] * 12)
        got = self.r.search_rules("сроки подачи заявления", limit=6)
        n_rules = sum(1 for c in got if c["doc_type"] == "rules_registry")
        self.assertGreaterEqual(n_rules, self.r.RULES_QUOTA_PRIMARY)

    def test_every_present_document_gets_at_least_one_slot(self):
        self._pool(["tpp_order_52"] * 20 + ["rules_registry"] * 2 + ["decree_body"] * 2)
        got = self.r.search_rules("что такое акт экспертизы", limit=6)
        self.assertEqual({c["doc_type"] for c in got},
                         {"tpp_order_52", "rules_registry", "decree_body"})

    def test_topic_document_goes_first_rank_preserved_inside(self):
        """K9 уточнил правило K10. Сначала было «квота меняет состав окна, а не порядок» — но
        замер атрибуции показал, что этого мало: тема угадывалась в 84 % случаев, а первым в
        окне оказывался более многословный Приказ №52, и ответ строился вокруг него. Теперь
        пункты ТЕМАТИЧЕСКОГО документа идут первыми, а внутри каждой группы порядок гибрида
        сохраняется — релевантность внутри документа не трогаем. Атрибуция@1: 0.72 → 0.92."""
        self._pool(["tpp_order_52"] * 20 + ["rules_registry"] * 4)
        got = self.r.search_rules("сроки рассмотрения заявления", limit=6)
        self.assertEqual(got[0]["doc_type"], "rules_registry", "первым идёт документ по теме")
        by_doc = {}
        for c in got:
            by_doc.setdefault(c["doc_type"], []).append(c["_score"])
        for doc, scores in by_doc.items():
            self.assertEqual(scores, sorted(scores, reverse=True),
                             f"внутри {doc} порядок гибрида должен сохраняться")

    def test_topic_is_reported_in_payload(self):
        self._pool(["rules_registry"] * 6)
        got = self.r.search_rules("сроки рассмотрения заявления", limit=6)
        self.assertEqual(got[0]["_topic"], "rules_registry")

    def test_empty_pool_gives_empty(self):
        self.r._hybrid = lambda *a, **kw: []
        self.assertEqual(self.r.search_rules("сроки", limit=6), [])

    def test_failure_is_logged_not_swallowed_silently(self):
        def boom(*a, **kw):
            raise RuntimeError("qdrant упал")
        self.r._hybrid = boom
        seen = []
        sink = self.r.logger.add(lambda m: seen.append(str(m)), level="WARNING")
        try:
            self.assertEqual(self.r.search_rules("сроки", limit=6), [])
        finally:
            self.r.logger.remove(sink)
        self.assertTrue(any("корпус Правил" in s for s in seen))


if __name__ == "__main__":
    unittest.main()
