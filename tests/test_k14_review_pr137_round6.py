"""Ревью PR #137, РАУНД 6: восемь находок, HIGH — НОЛЬ. Первый раунд без HIGH. Офлайн.

Динамика тяжести: 2 → 2 → 3 → 2 → 2 → **0** HIGH при 7 → 5 → 7 → 5 → 6 → 8 находках.
Число находок не упало, а серьёзность — да, и это первый признак сходимости за шесть раундов.

1. MED/HIGH — две таблицы тем РАЗОШЛИСЬ: `topics._DOC_TYPES` убрал Приказ №52 из темы «путь СТ-1»,
   а лексическая `rules_topic` продолжала называть его — и ПОБЕЖДАЛА, потому что даёт `primary`
   (квота 3) и ставит документ ВО ГЛАВЕ окна. По уроку `K9` ответ строится вокруг первого
   источника: вопрос «как получить СТ-1» возглавлял документ про АКТ ЭКСПЕРТИЗЫ.
   ⚠ Релизная проверка этого не видела: она сверяет ПРИСУТСТВИЕ в окне, а не главу.
2. MED — безусловный «№» в отсечке съедал КОД: «код ТН ВЭД № 8403», «товарная позиция № 8403».
3. MED — голая сумма рядом с маркером становилась «кодом»: «сумма контракта 250000».
4. MED/LOW — легенда шкалы юр. силы в промпте перечисляла 1…5, а в окно уже печаталась ступень 0.
5. LOW — разделы «9-1»/«9-2» теряли префикс «Раздел N.» в ЯКОРЕ (`isdigit()` их не признавал).
6. LOW/MED — «НЕ ИЗМЕРЕНО» ставилось при ЛЮБОМ ненулевом троттлинге: сервис мог не вернуться
   по-настоящему, а инструмент, который его и сломал, отчитывался «не наблюдалось».
7. LOW — знаменатель метрики «условие Перечня доехало» включал кейсы БЕЗ кода: 6/6 при трёх
   настоящих случаях — число завышено собственным знаменателем.
8. LOW — круговая раздача сортировала документы темы ПО АЛФАВИТУ, стирая объявленный порядок.

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

from app.core.manifest import LEGAL_FORCE_NAMES  # noqa: E402
from app.core.prompts import PROCEDURAL_SYSTEM_PROMPT  # noqa: E402
from app.rag.okpd2_ref import extract_tnved_position  # noqa: E402
from app.rag.retriever import rules_topic  # noqa: E402


class TestFinding1TopicTablesAgree(unittest.TestCase):
    """Лексическая тема и тема вопроса не должны называть РАЗНЫЕ документы для одной оси."""

    def test_st1_questions_head_their_own_document(self):
        for q in ("как получить сертификат СТ-1", "что такое СТ-1", "нужен ли сертификат СТ-1",
                  "моей продукции нет в приложении 719, можно ли получить СТ-1"):
            with self.subTest(q=q[:44]):
                self.assertEqual(rules_topic(q), "prikaz14_tpp", q)

    def test_conditions_questions_head_the_agreement(self):
        for q in ("какие условия достаточной переработки для кода ТН ВЭД 8403",
                  "что такое кумулятивный принцип"):
            with self.subTest(q=q[:44]):
                self.assertEqual(rules_topic(q), "sng_origin_rules", q)

    def test_order_52_keeps_its_own_questions(self):
        """⚠ Положительная половина: акт экспертизы и состав документов остались у Приказа №52."""
        for q in ("какие документы нужны для внесения в реестр",
                  "что такое акт экспертизы и когда он нужен",
                  "какие документы нужны для акта экспертизы"):
            with self.subTest(q=q[:44]):
                self.assertEqual(rules_topic(q), "tpp_order_52", q)

    def test_the_two_tables_name_the_same_documents(self):
        """⚠⚠ Инвариант, из-за отсутствия которого дефект и прожил: у темы `st1_origin` документы
        лексической таблицы обязаны лежать В СПИСКЕ документов темы, а не вне его."""
        from app.rag import topics

        declared = set(topics.doc_types(topics.ST1_ORIGIN))
        for q in ("как получить сертификат СТ-1",
                  "какие условия достаточной переработки для кода ТН ВЭД 8403"):
            with self.subTest(q=q[:44]):
                self.assertIn(rules_topic(q), declared,
                              f"лексическая тема называет документ вне темы вопроса: {q}")


class TestFinding2NumberSignIsNotAnActMarker(unittest.TestCase):
    """«№» перед кодом — обычная запись номера позиции."""

    def test_code_after_number_sign(self):
        for q in ("какие условия для кода ТН ВЭД № 8403",
                  "условия по ТН ВЭД N 8403",
                  "товарная позиция № 8403 ТН ВЭД"):
            with self.subTest(q=q[:44]):
                self.assertEqual(extract_tnved_position(q), "8403", q)

    def test_act_requisites_still_skipped(self):
        """⚠ Положительная половина: реквизит акта отсекается по СЛОВУ акта."""
        self.assertIsNone(extract_tnved_position("условия по ТН ВЭД, постановление № 1392"))
        self.assertIsNone(extract_tnved_position("условия по ТН ВЭД, постановление 719 от 2020"))


class TestFinding3BareAmountIsNotACode(unittest.TestCase):
    """Сумма узнаётся и по слову ПЕРЕД числом, не только после."""

    def test_amounts_before_the_number(self):
        for q in ("какие условия по ТН ВЭД, сумма контракта 250000",
                  "условия по ТН ВЭД при обороте 1500000",
                  "условия по ТН ВЭД, стоимость 480000"):
            with self.subTest(q=q[:44]):
                self.assertIsNone(extract_tnved_position(q), q)

    def test_real_codes_survive(self):
        for q, want in (("какие условия по ТН ВЭД 8403", "8403"),
                        ("какие условия по ТН ВЭД 2009", "2009"),
                        ("мой код ТН ВЭД 8544 49 910 0, какие условия", "8544 49 910 0")):
            with self.subTest(q=q[:44]):
                self.assertEqual(extract_tnved_position(q), want, q)


class TestFinding4PromptLegendCoversTheScale(unittest.TestCase):
    """Легенда в промпте обязана содержать КАЖДУЮ ступень, которую печатает манифест."""

    def test_every_rung_is_in_the_legend(self):
        legend = PROCEDURAL_SYSTEM_PROMPT.split("МЕНЬШЕ ЧИСЛО")[1][:240]
        missing = [k for k in LEGAL_FORCE_NAMES if f"{k} " not in legend]
        self.assertEqual(missing, [],
                         "модель видит в окне значение силы, которого нет в её же легенде")


class TestFinding5HyphenatedSectionKeepsItsPrefix(unittest.TestCase):
    """Якорь — это ССЫЛКА, которую видит пользователь."""

    def test_anchor_names_the_section(self):
        import load_rules_kb

        recs = load_rules_kb.parse_sng_origin(
            ROOT / "knowledge_base" / "pp719" / "sng_origin_rules.txt")
        hyphen = [r["source_anchor"] for r in recs if "9-2.1" in r.get("source_anchor", "")]
        self.assertTrue(hyphen, "раздел 9-2 исчез")
        self.assertIn("Раздел 9-2.", hyphen[0], "якорь снова без префикса раздела")

    def test_plain_sections_unchanged(self):
        import load_rules_kb

        recs = load_rules_kb.parse_sng_origin(
            ROOT / "knowledge_base" / "pp719" / "sng_origin_rules.txt")
        plain = [r["source_anchor"] for r in recs if "п. 9.1 " in r.get("source_anchor", "")]
        self.assertTrue(plain)
        self.assertIn("Раздел 9.", plain[0])


class TestFinding6NotMeasuredOnlyWhenNotObserved(unittest.TestCase):
    """«НЕ ИЗМЕРЕНО» — про отсутствие наблюдения, а не про наличие троттлинга."""

    def test_gate_is_on_the_last_probe(self):
        src = (ROOT / "scripts" / "eval_chaos.py").read_text(encoding="utf-8")
        self.assertIn("if not recovered and rate_limited(after):", src)
        self.assertNotIn("if not recovered and (rate_limited(after) or throttled):", src,
                         "вердикт снова downgrade-ится на любом ненулевом троттлинге")


class TestFinding7MetricDenominatorMatchesItsName(unittest.TestCase):
    """«Условие Перечня доехало» считается по позициям ИЗ Перечня."""

    def test_denominator_is_in_perechen(self):
        src = (ROOT / "scripts" / "eval_answers.py").read_text(encoding="utf-8")
        self.assertIn('cond = [r for r in st1 if r["in_perechen"] is True]', src)


class TestFinding8DeclaredOrderIsKept(unittest.TestCase):
    """Порядок документов темы берётся из манифеста тем, а не из алфавита."""

    def test_order_hint_is_used(self):
        src = (ROOT / "app" / "rag" / "retriever.py").read_text(encoding="utf-8")
        self.assertIn("order_hint = {d: i for i, d in enumerate(primary_docs)}", src)
        self.assertNotIn("rank.update({d: 1 for d in sorted(extra_primary)})", src,
                         "порядок документов темы снова алфавитный")


if __name__ == "__main__":
    unittest.main()
