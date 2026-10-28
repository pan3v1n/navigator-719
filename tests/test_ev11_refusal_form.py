"""`EV11` #92 — отказ распознаётся по ФОРМЕ ответа (`scripts/eval_refusal.py`), а не по списку слов.

Тексты — сокращённые ответы живого DeepSeek из замеров 28.10.2026 (`R40`, `K2-2`): вопросы из
курированных наборов, персональных данных нет.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import eval_refusal as R  # noqa: E402

# Отказ БЕЗ единого слова прежнего списка («вне сферы», «не относится к», «не найден», …) — его
# старый детектор `eval_guard` пропускал (R40: «водка», 2 из 6 прогонов).
REFUSAL_NO_OLD_WORDS = (
    "Судя по описанию, продукция (водка и алкогольные напитки) — это готовая пищевая продукция, и прямого "
    "совпадения с позицией приложения к ПП №719 по ней не нашлось. Найденные позиции — про другое: "
    "технологическое оборудование для спиртовой промышленности [1], оборудование для розлива [2]. "
    "Для продукции, не включённой в приложение, происхождение подтверждается через сертификат СТ-1.")
REFUSAL_SERVICE = (
    "Судя по описанию, речь идёт о добыче (вылове) водных биоресурсов — это услуга/деятельность, а не "
    "промышленная продукция из приложения к ПП №719. Найденные позиции по смыслу не соответствуют: "
    "суда рыбопромыслового флота [1], судовое оборудование [4]. Уточните, что именно вы подтверждаете.")
# Уточнение по правилу 1г: точного совпадения нет, кандидаты, просьба выбрать — НЕ отказ.
CLARIFICATION = (
    "Точного совпадения по запросу не нашёл. Ближайшие позиции:\n- Перфторуглеродная смазка КСТ — ОКПД2 "
    "из 20.14.19.140 [1]\n- Перфторуглеродная смазка УПИ — ОКПД2 из 20.14.19.140 [2]\nПодтвердите, какая "
    "из них ваша, или назовите код ОКПД2.")
# Разбор позиции по шаблону правила 4.
ANALYSIS = (
    "Смотрите, что нашлось.\n\n**Позиция:** «Продукция отрасли специального машиностроения», прицепы и "
    "полуприцепы прочие (ОКПД2 29.20.23) [1].\n\n**Порог:** не менее 70 процентов (25 баллов) [1].")
# Отказ, к которому КОД дописал таблицу баллов ближайшей позиции (`points_table`): пользователь видит
# чужие баллы — это НЕ чистый отказ, и форма обязана это показать.
REFUSAL_WITH_TABLE = REFUSAL_NO_OLD_WORDS + (
    "\n\n**Операции и баллы — дословно из приложения:**\n\n| Операция или условие | Баллы |\n|---|---|\n"
    "| рама — раскрой, гибка | 10 балл. |")


class TestRefusalByForm(unittest.TestCase):
    def test_refusal_without_old_words_is_recognised(self):
        """Критерий #92: ответ, отказывающий без слов из прежнего списка, распознаётся как отказ."""
        from eval_guard import _refused
        self.assertTrue(R.refuses(REFUSAL_NO_OLD_WORDS))
        self.assertTrue(R.declines(REFUSAL_NO_OLD_WORDS))
        self.assertTrue(_refused(REFUSAL_NO_OLD_WORDS), "eval_guard берёт то же определение")

    def test_service_refusal(self):
        self.assertTrue(R.refuses(REFUSAL_SERVICE))

    def test_clarification_declines_but_is_not_a_refusal(self):
        """Уточнение — верное поведение вне сферы (`declines`), но не отказ на профильном (`refuses`)."""
        self.assertTrue(R.declines(CLARIFICATION))
        self.assertFalse(R.refuses(CLARIFICATION))

    def test_position_analysis_is_neither(self):
        """Разбор позиции с её баллами — не отказ, даже если в нём есть «уточните»/«не является»
        (прежний `DECLINE_RE` срабатывал на таких словах почти в любом разборе)."""
        text = ANALYSIS + " Уточните у заявителя наличие сервисного центра; это не является обязательным."
        self.assertFalse(R.declines(text))
        self.assertFalse(R.refuses(text))

    def test_refusal_with_appended_points_table_is_not_clean(self):
        """Таблица баллов после отказа — пользователь видит чужие баллы; форма это показывает."""
        self.assertTrue(R.relies_on_position(REFUSAL_WITH_TABLE))
        self.assertFalse(R.declines(REFUSAL_WITH_TABLE))

    def test_one_definition_of_commit_across_metrics(self):
        """`eval_completeness.is_clarifying` смотрит на ту же форму «**Позиция:**»."""
        import eval_completeness
        self.assertIs(eval_completeness._COMMIT_RE, R.COMMIT_RE)



class TestPointsTableOnlyUnderAnAnalysisEV11_1(unittest.TestCase):
    """`EV11-1`: таблицу баллов код дописывает только к ответу, назвавшему позицию. Под отказом она
    показывала баллы ЧУЖОЙ позиции («водка» → оборудование для спиртовой промышленности)."""

    TABLE = ("\n**Операции и баллы — дословно из приложения:**\n\n| Операция или условие | Баллы |\n"
             "|---|---|\n| рама | 10 балл. |")

    def _run(self, model_text):
        from types import SimpleNamespace
        from unittest import mock

        from app.rag import pipeline
        plan = pipeline._Plan(messages=[{"role": "user", "content": "в"}], grounding="", hits=[], cases=[],
                              low_relevance=True, points_table=self.TABLE)
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)

        def create(**kw):
            if kw.get("stream"):
                d = SimpleNamespace(content=model_text)
                return iter([SimpleNamespace(choices=[SimpleNamespace(delta=d, finish_reason="stop")], usage=None),
                             SimpleNamespace(choices=[], usage=usage)])
            msg = SimpleNamespace(content=model_text)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(pipeline, "_plan_answer", return_value=plan), \
                mock.patch.object(pipeline, "_client", return_value=client):
            sync = pipeline.answer("в").text
            done = [p for k, p in pipeline.answer_stream("в") if k == "done"][0].text
        return sync, done

    def test_refusal_gets_no_table(self):
        for text in self._run(REFUSAL_NO_OLD_WORDS):
            self.assertNotIn("дословно из приложения", text)
            self.assertTrue(R.declines(text), "отказ остался чистым — проверка формы это видит")

    def test_clarification_gets_no_table(self):
        for text in self._run(CLARIFICATION):
            self.assertNotIn("дословно из приложения", text)

    def test_analysis_keeps_the_table(self):
        for text in self._run(ANALYSIS):
            self.assertIn("дословно из приложения", text, "разбор позиции обязан получить таблицу")

    def test_rule_4_template_matches_the_runtime_sign(self):
        """Признак берётся из шаблона правила 4: сменят шаблон — таблица пропадёт у всех разборов."""
        from app.core.prompts import NAVIGATOR_SYSTEM_PROMPT
        from app.rag.pipeline import _COMMITS_RE
        self.assertTrue(_COMMITS_RE.search(NAVIGATOR_SYSTEM_PROMPT), "шаблон «**Позиция:**» ушёл из правила 4")


if __name__ == "__main__":
    unittest.main()
