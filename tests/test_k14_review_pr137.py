"""Ревью PR #137 (`K14` #35): семь находок, каждая со своим сторожем. Офлайн.

⚠⚠ ПАКЕТ ПРАВОК ПО РЕВЬЮ — САМ ОБЪЕКТ РЕВЬЮ. Батарея была ЗЕЛЁНОЙ на всех семи дефектах: они
жили ровно в тех зазорах, которых не касался ни один тест. Поэтому здесь по утверждению на
находку, и каждое проверено мутацией.

Что нашлось (все воспроизведены прогоном до правки):

1. HIGH — уступка товарного дисквалификатора снималась ЛЮБЫМ упоминанием «ТН ВЭД»/«СТ-1»:
   «сколько баллов нужно для 28.22.16.111, код ТН ВЭД 8428 10» уходило ПРОЦЕДУРНОЙ веткой.
   Ломался инвариант `K12`, и весь путь `T9` (код ТН ВЭД → ОКПД2 → требования) становился
   недостижим для формулировки, ради которой строился.
2. HIGH — `extract_tnved_position` брал ПЕРВОЕ четырёхзначное число: «в 2026 году … ТН ВЭД 8403»
   давало `2026`, а лукап печатал уверенное «Код ТН ВЭД 2026: … применяется общее правило».
3. MED — «усиленный» положительный контроль распознавателей ПЕРЕСТАЛ ловить распознаватель
   несуществующего документа: цикл по пустому списку имён не исполняется ни разу.
4. MED — Соглашение СНГ подписывалось в окне модели как «постановление Правительства РФ».
5. MED — голый токен «ТН ВЭД» перехватывал тему `registry_entry` по порядку.
6. MED — «сообщения сценариев РАЗНЫЕ» считалось по телу ответа 429, то есть по ненаблюдению.
7. LOW — исключённая позиция не сбрасывала группу: следующая строка-продолжение приклеилась бы
   к условию ПРЕДЫДУЩЕЙ группы.

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
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import procedural, topics  # noqa: E402
from app.rag.okpd2_ref import extract_tnved_position  # noqa: E402
from app.tools.navigator import extract_okpd2  # noqa: E402


def route(q: str) -> bool:
    return procedural.is_procedural(q, has_code=bool(extract_okpd2(q)))


class TestFinding1MixedQuestionStaysProduct(unittest.TestCase):
    """Уступка не снимает ОДНОЗНАЧНО товарные сигналы: код ОКПД2, «719», баллы, локализация."""

    MIXED = (
        "сколько баллов нужно для 28.22.16.111, код ТН ВЭД 8428 10",
        "какие требования по 719 к продукции с кодом ТН ВЭД 8501 40 200 0",
        "какие требования к локализации насосов, код ТН ВЭД 8413",
    )
    # ⚠ Положительная половина обязательна: сузь кто-нибудь уступку до нуля — тест должен упасть.
    SECOND_KEY = (
        # ⚠ Формулировка «укажите ваш код ТН ВЭД, распишите требования» УБРАНА третьим раундом:
        # она неотделима от пути `T9`, и цена её потери измерена и принята.
        "по какому коду смотреть требования СТ-1",
        "у вас итоговый документ скорее всего СТ-1, какие требования по коду ТН ВЭД",
    )

    def test_mixed_questions_keep_the_product_branch(self):
        for q in self.MIXED:
            with self.subTest(q=q[:44]):
                self.assertFalse(route(q), q)

    def test_pure_second_key_questions_still_pass(self):
        for q in self.SECOND_KEY:
            with self.subTest(q=q[:44]):
                self.assertTrue(route(q), q)


class TestFinding2CodeIsTakenNearTheCue(unittest.TestCase):
    """Код берётся рядом с маркером, а не первым числом в тексте."""

    def test_year_and_amount_do_not_win(self):
        for q, want in (
            ("в 2026 году какие условия достаточной переработки для кода ТН ВЭД 8403", "8403"),
            ("при обороте 500000 рублей какие условия по ТН ВЭД 8403", "8403"),
        ):
            with self.subTest(q=q[:44]):
                self.assertEqual(extract_tnved_position(q), want)

    def test_code_before_the_cue_still_found(self):
        self.assertEqual(
            extract_tnved_position("какие условия для товарной позиции 8402 по ТН ВЭД"), "8402")

    def test_far_number_is_not_taken(self):
        """Число далеко от маркера — не код. Иначе «рядом» ничего не значит."""
        q = ("в 2026 году, когда вступили в силу многочисленные поправки и уточнения к порядку, "
             "расскажите про ТН ВЭД")
        self.assertIsNone(extract_tnved_position(q))


class TestFinding3ControlCatchesAnUnknownDocType(unittest.TestCase):
    """Контроль обязан падать на распознавателе документа, которого в манифесте нет."""

    def test_recognizer_for_a_missing_doc_type_stops_the_measurement(self):
        import eval_documents

        saved = dict(eval_documents._SOURCE_RECOGNIZERS)
        try:
            eval_documents._SOURCE_RECOGNIZERS["doc_type_which_does_not_exist"] = re.compile("zzz")
            with self.assertRaises(SystemExit):
                eval_documents.check_source_recognizers()
        finally:
            eval_documents._SOURCE_RECOGNIZERS.clear()
            eval_documents._SOURCE_RECOGNIZERS.update(saved)
        eval_documents.check_source_recognizers()  # настоящие проходят


class TestFinding4TreatyIsNotADecree(unittest.TestCase):
    """Международный договор не должен подписываться постановлением Правительства."""

    def test_label_names_the_treaty(self):
        from app.core.manifest import legal_force_label

        label = legal_force_label("sng_origin_rules")
        self.assertIn("договор", label)
        self.assertNotIn("постановление", label)

    def test_decree_keeps_its_own_label(self):
        from app.core.manifest import legal_force_label

        self.assertIn("постановление", legal_force_label("decree_body"))


class TestFinding5TokenDoesNotHijackRegistry(unittest.TestCase):
    """Код в вопросе не делает вопрос вопросом о происхождении."""

    def test_registry_procedure_keeps_its_topic(self):
        for q in ("какой порядок внесения в реестр для ТН ВЭД 8403",
                  "как внести в реестр продукцию с кодом ТН ВЭД 8501"):
            with self.subTest(q=q[:44]):
                self.assertEqual(topics.classify(q), "registry_entry", q)

    def test_second_key_questions_keep_their_topic(self):
        for q in ("мой код ТН ВЭД 8544 49 910 0, какие условия",
                  "какие условия достаточной переработки для кода ТН ВЭД 8403",
                  "укажите ваш код ТН ВЭД, распишите требования"):
            with self.subTest(q=q[:44]):
                self.assertEqual(topics.classify(q), "st1_origin", q)


class TestFinding6ThrottledScenarioIsNotAMessage(unittest.TestCase):
    """Ненаблюдение не считается вторым сообщением сценария."""

    def test_rate_limited_scenario_is_excluded_from_distinctness(self):
        src = (ROOT / "scripts" / "eval_chaos.py").read_text(encoding="utf-8")
        # Утверждение о КОДЕ, а не о прогоне: сценарии требуют боевого сервиса и денег.
        self.assertIn('measured = {r["scenario"] for r in results '
                      'if r["verdict"] != "НЕ ИЗМЕРЕНО"}', src)
        self.assertIn("sc in measured", src)


class TestFinding7ExcludedRowResetsTheGroup(unittest.TestCase):
    """Строка-продолжение после исключённой позиции не липнет к условию соседа."""

    def test_parser_resets_last_group_on_the_inline_excluded_form(self):
        """⚠ Утверждение ПО СУЩЕСТВУ, а не по числу вхождений.

        Первая редакция считала, сколько раз в файле встречается `last_group = []`, и упала, когда
        раунд 4 добавил ещё две законные ветки сброса. Число вхождений — не свойство поведения:
        проверяем, что после ОТМЕНЁННОЙ позиции продолжение не липнет к условию соседа.
        """
        import convert_st1_perechen as conv

        # ⚠ Заголовок обязателен: `parse` останавливается, если его нет («не тот документ»).
        sample = "\n".join((
            "ПЕРЕЧЕНЬ УСЛОВИЙ",
            "Код ТН ВЭД |Наименование |Условие",
            "8401 |Реакторы ядерные |Изготовление из материалов позиции 8401",
            "8803 - Исключена.|",
            "хвост продолжения|",
        ))
        rows, _forms = conv.parse(sample)
        by_code = {r["code"]: r for r in rows}
        self.assertIn("8401", by_code)
        self.assertNotIn("хвост продолжения", by_code["8401"]["condition"],
                         "продолжение после отменённой позиции приклеилось к соседу")

    def test_table_still_parses_to_the_measured_size(self):
        """Положительный контроль: правка не изменила разбор первоисточника."""
        table = json.loads(
            (ROOT / "knowledge_base" / "classifiers" / "tnved_st1_conditions.json")
            .read_text(encoding="utf-8"))
        # ⚠ 245 после раунда 4: восстановлена строка со сноской в графе кода «из 3901 - 3915 <*>».
        self.assertEqual(len(table["rows"]), 245)
        self.assertEqual(sum(1 for r in table["rows"] if r.get("excluded")), 1)


if __name__ == "__main__":
    unittest.main()
