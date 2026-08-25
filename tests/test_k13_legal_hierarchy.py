"""`K13` #46 — юридическая иерархия источников в промпте.

ЧТО ЗАКРЫВАЕТ ЗАДАЧА. При расхождении норм модель не знала, что главнее: ведомственный порядок
ТПП не может отменить критерий постановления. На товарной стороне приём давно применён — код
ОКПД2 авторитетнее семантики; на процедурной аналога не было.

ЧЕМУ ЗДЕСЬ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ. Урок проекта, купленный `O3` #104: тест с одной только
положительной половиной остаётся зелёным и при ПОЛНОСТЬЮ УДАЛЁННОМ предохранителе. Поэтому
каждое утверждение ниже сопровождается проверкой мутацией: снимаем ровно тот механизм, который
утверждение стережёт, и убеждаемся, что тест краснеет.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.core import manifest as kb_manifest
from app.core.prompts import PROCEDURAL_SYSTEM_PROMPT
from app.rag import pipeline as pipeline_mod
from app.rag.pipeline import format_rules_context


def _point(doc_type: str | None, anchor: str, text: str = "Текст пункта.") -> dict:
    """Пункт процедурного корпуса в том виде, в каком его отдаёт `search_rules` (payload)."""
    p = {"source_anchor": anchor, "text": text}
    if doc_type is not None:
        p["doc_type"] = doc_type
    return p


DECREE = _point("decree_body", "ПП №719, п. 1, подпункт «г»")
ORDER52 = _point("tpp_order_52", "Приказ ТПП РФ №52, п. 4.2.1")
RULES = _point("rules_registry", "Правила ведения реестра, п. 12")


class TestForceReachesProceduralContext(unittest.TestCase):
    """Сила источника обязана доехать до КОНТЕКСТА, а не остаться в модуле манифеста."""

    def setUp(self):
        kb_manifest.legal_force_by_doc.cache_clear()

    def test_each_block_carries_force_of_its_document(self):
        ctx = format_rules_context([DECREE, ORDER52, RULES], show_legal_force=True)
        self.assertIn("юр. сила 1 — постановление Правительства РФ", ctx)
        self.assertIn("юр. сила 3 — ведомственный акт ТПП РФ", ctx)
        self.assertIn("юр. сила 2 — правила и приложение к постановлению", ctx)
        # Пометка стоит В ШАПКЕ блока, рядом с источником, а не отдельной легендой сверху:
        # легенда заставила бы модель сопоставлять блок с документом по имени.
        head = ctx.splitlines()[0]
        self.assertIn("ПП №719, п. 1", head)
        self.assertIn("юр. сила 1", head)

    def test_negative_control_without_flag_context_is_as_before_k13(self):
        """Мутация: снимаем флаг — пометки обязаны исчезнуть ВСЕ до одной.

        Без этой половины тест выше остался бы зелёным, даже если бы пометка печаталась всегда,
        и мы не заметили бы, что товарный путь платит за неё бюджетом контекста."""
        ctx = format_rules_context([DECREE, ORDER52, RULES])
        self.assertNotIn("юр. сила", ctx)
        # …и при этом сам контекст не пострадал — блоки на месте
        self.assertIn("Приказ ТПП РФ №52, п. 4.2.1", ctx)
        self.assertIn("Текст пункта.", ctx)

    def test_procedural_answer_actually_asks_for_it(self):
        """Сквозная проверка: флаг выставлен В ПАЙПЛАЙНЕ, а не только доступен в сигнатуре.

        ⚠ Отдельный тест нужен потому, что `show_legal_force` по умолчанию ВЫКЛЮЧЕН. Уберут
        аргумент на вызове — функция продолжит работать, контекст молча обеднеет, и ни один тест
        на `format_rules_context` этого не увидит."""
        sent: list[str] = []

        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "Ответ [1]."})()})()]
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()

        def _create(*a, **k):
            sent.append(k["messages"][-1]["content"])
            return _Resp()

        class _Client:
            chat = type("Ch", (), {"completions": type(
                "Co", (), {"create": staticmethod(_create)})()})()

        orig_search, orig_client = pipeline_mod.search_rules, pipeline_mod._client
        pipeline_mod.search_rules = lambda *a, **k: [DECREE, ORDER52]
        pipeline_mod._client = lambda: _Client()
        try:
            pipeline_mod._answer_procedural("какие документы нужны", "какие документы нужны")
        finally:
            pipeline_mod.search_rules, pipeline_mod._client = orig_search, orig_client

        self.assertEqual(len(sent), 1, "процедурный ответ обязан сходить в модель ровно один раз")
        self.assertIn("юр. сила 1 — постановление Правительства РФ", sent[0])
        self.assertIn("юр. сила 3 — ведомственный акт ТПП РФ", sent[0])


class TestDegradesInsteadOfFalling(unittest.TestCase):
    """Рантайм не имеет права остаться без ответа из-за манифеста — он контекст УКРАШАЕТ."""

    def setUp(self):
        kb_manifest.legal_force_by_doc.cache_clear()

    def tearDown(self):
        kb_manifest.legal_force_by_doc.cache_clear()

    def test_unknown_and_missing_doc_type_print_nothing(self):
        stranger = _point("no_such_document", "Неизвестный документ, п. 1")
        anonymous = _point(None, "Правила ведения реестра, п. 99")
        ctx = format_rules_context([stranger, anonymous], show_legal_force=True)
        self.assertNotIn("юр. сила", ctx)
        self.assertIn("Неизвестный документ, п. 1", ctx)
        self.assertIn("Правила ведения реестра, п. 99", ctx)

    def test_broken_manifest_leaves_the_answer_intact(self):
        """Битый манифест = пометок нет, ответ собирается как до `K13`.

        ⚠ Второй контур при этом на месте: испорченный манифест не доедет до боя незамеченным —
        на нём падает загрузка корпуса (`load_manifest` кидает `ManifestError`)."""
        orig = kb_manifest.MANIFEST_PATH
        kb_manifest.MANIFEST_PATH = Path("нет-такого-манифеста.yaml")
        kb_manifest.legal_force_by_doc.cache_clear()
        try:
            self.assertEqual(kb_manifest.legal_force_by_doc(), {})
            self.assertEqual(kb_manifest.legal_force_label("decree_body"), "")
            ctx = format_rules_context([DECREE, ORDER52], show_legal_force=True)
            self.assertNotIn("юр. сила", ctx)
            self.assertIn("ПП №719, п. 1, подпункт «г»", ctx)
            self.assertIn("Приказ ТПП РФ №52, п. 4.2.1", ctx)
        finally:
            kb_manifest.MANIFEST_PATH = orig
            kb_manifest.legal_force_by_doc.cache_clear()

    def test_negative_control_healthy_manifest_does_print(self):
        """Мутация к предыдущему: на ЖИВОМ манифесте пометка обязана быть.

        Без неё «манифест сломан» и «функция вообще ничего не печатает» неразличимы — тот самый
        класс, из-за которого у инструмента с результатом-«нет» обязан быть положительный
        контроль."""
        self.assertNotEqual(kb_manifest.legal_force_by_doc(), {})
        self.assertIn("юр. сила 1", kb_manifest.legal_force_label("decree_body"))


class TestLabelShape(unittest.TestCase):
    def setUp(self):
        kb_manifest.legal_force_by_doc.cache_clear()

    def test_label_carries_number_and_words_together(self):
        """Число задаёт ПОРЯДОК, слова — смысл. Ни того, ни другого по отдельности не хватает.

        Одно число: «1 главнее 3» неочевидно, интуиция подсказывает обратное. Одни слова: модель
        обязана была бы сама знать, что ведомственный акт ниже постановления, — а это ровно то
        знание, которого у неё, по условию задачи, нет."""
        label = kb_manifest.legal_force_label("tpp_order_52")
        self.assertIn("3", label)
        self.assertIn("ведомственный акт ТПП РФ", label)

    def test_every_live_document_has_a_name_for_its_force(self):
        """Новый документ с силой 4 не должен печататься как «юр. сила 4» без расшифровки."""
        docs = kb_manifest.load_manifest()["documents"]
        for d in docs:
            if d.get("status") == kb_manifest.RETIRED:
                continue
            self.assertIn(d["legal_force"], kb_manifest.LEGAL_FORCE_NAMES,
                          f"{d['doc_type']}: сила {d['legal_force']} без названия")

    def test_names_cover_the_manifest_vocabulary(self):
        """Словарь сил в манифесте и названия здесь обязаны совпадать по составу.

        Расхождение — это молчаливый дефект: документ пройдёт проверку словаря на загрузке и
        напечатается без расшифровки в рантайме."""
        vocab = kb_manifest.load_manifest()["vocabularies"]["legal_force"]
        self.assertEqual(sorted(vocab), sorted(kb_manifest.LEGAL_FORCE_NAMES))


class TestPromptRule(unittest.TestCase):
    """Строка в контексте без правила промпта — шум. Правка обязана внести обе половины."""

    RULE_MARK = "1а. ЮРИДИЧЕСКАЯ СИЛА ИСТОЧНИКА"

    def _rule(self) -> str:
        i = PROCEDURAL_SYSTEM_PROMPT.index(self.RULE_MARK)
        return PROCEDURAL_SYSTEM_PROMPT[i:PROCEDURAL_SYSTEM_PROMPT.index("2. СРОКИ И ЧИСЛА", i)]

    def test_rule_states_the_counterintuitive_direction(self):
        """Главное, чего модель не может знать сама: МЕНЬШЕ число — ВЫШЕ сила."""
        rule = self._rule()
        self.assertIn("МЕНЬШЕ ЧИСЛО — ВЫШЕ СИЛА", rule)

    def test_rule_names_all_five_ranks(self):
        rule = self._rule()
        for word in ("постановление", "правила", "ведомственный акт", "разъяснение", "практика"):
            self.assertIn(word, rule, f"ранг {word!r} не назван в правиле")

    def test_rule_requires_naming_both_sources_on_divergence(self):
        """Критерий приёмки `K13`: ответ не просто берёт норму выше, а ЯВНО это называет."""
        rule = self._rule()
        self.assertIn("назови оба источника", rule)

    def test_rule_forbids_inventing_conflicts(self):
        """Без этой оговорки правка портит ответы там, где расхождения нет вовсе.

        Пункты окна почти всегда отвечают на РАЗНЫЕ вопросы и просто дополняют друг друга;
        модель, которой велено разрешать иерархию, начнёт находить противоречия на пустом месте."""
        rule = self._rule()
        self.assertIn("НЕ ИЩИ ПРОТИВОРЕЧИЙ ТАМ, ГДЕ ИХ НЕТ", rule)
        self.assertIn("об иерархии не упоминай вовсе", rule)

    def test_rule_says_lower_force_cannot_cancel_higher(self):
        rule = self._rule()
        self.assertIn("НЕ отменяют и не заменяют критерий постановления", rule)

    def test_negative_control_checks_fail_without_the_rule(self):
        """Мутация: вырезаем правило 1а — все проверки выше обязаны покраснеть.

        Это и есть отрицательный контроль. Проверки, которые остаются зелёными при удалённом
        правиле, стерегут не правило, а собственный текст."""
        i = PROCEDURAL_SYSTEM_PROMPT.index(self.RULE_MARK)
        j = PROCEDURAL_SYSTEM_PROMPT.index("2. СРОКИ И ЧИСЛА", i)
        mutated = PROCEDURAL_SYSTEM_PROMPT[:i] + PROCEDURAL_SYSTEM_PROMPT[j:]
        self.assertNotIn(self.RULE_MARK, mutated)
        for phrase in ("МЕНЬШЕ ЧИСЛО — ВЫШЕ СИЛА", "назови оба источника",
                       "НЕ ИЩИ ПРОТИВОРЕЧИЙ ТАМ, ГДЕ ИХ НЕТ",
                       "НЕ отменяют и не заменяют критерий постановления"):
            self.assertNotIn(phrase, mutated,
                             f"{phrase!r} живёт вне правила 1а — проверка стережёт не то место")


if __name__ == "__main__":
    unittest.main()
