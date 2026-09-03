"""Ревью PR #137, РАУНД 4: пять находок, две HIGH. Офлайн.

⚠⚠ ЧЕТВЁРТЫЙ РАУНД ПОДРЯД С HIGH (7 → 5 → 7 → 5 находок, 2 → 2 → 3 → 2 HIGH). Батарея из 1108
тестов была зелёной на всех пяти.

1. HIGH — разбор Перечня приклеивал ЧУЖИЕ правила к условию позиции. Строки уровня ГЛАВЫ
   («Группа 36 |…», «из группы 37 |…», 27 штук) и строки со сноской в графе кода
   («из 3901 - 3915 <*> |…») не матчились шаблоном строки и уходили в ветку продолжения, а
   `line.strip(" |")` резал только КРАЙНИЕ черты. Итог: 29 записей из 244 несли `|` внутри
   условия, 11 из них промышленные; условие позиции 3507 (ферментные препараты) содержало
   правила Групп 36 и 37 с ЧУЖИМ пределом 20 %, и лукап печатал это в промпт под «используй
   ТОЛЬКО это значение».
2. HIGH — три документа темы пересоздавали окно, и `decree_body` терял место на ГЛАВНОМ вопросе
   задачи. ⚠ РАУНДОМ РАНЕЕ Я ЗАПИСАЛ ЭТО КАК «НЕИСПРАВИМЫЙ ПРЕДЕЛ» — вывод был НЕВЕРЕН, см.
   `TestFinding2QuotaIsRoundRobin`.
3. MED — «2026 г» без точки и «постановление 719 от 2020» проходили как код ТН ВЭД.
4. MED — оговорка про узкие коды печатала один `code`, и диапазон показывался одной позицией;
   плюс код ЗА верхней границей диапазона объявлялся `narrower`.
5. LOW — два класса тестов стояли ПОСЛЕ блока `__main__`: прямой запуск давал 28 тестов вместо 32.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import st1_ref, topics  # noqa: E402
from app.rag.okpd2_ref import extract_tnved_position  # noqa: E402

TABLE = ROOT / "knowledge_base" / "classifiers" / "tnved_st1_conditions.json"


def rows() -> list[dict]:
    return json.loads(TABLE.read_text(encoding="utf-8"))["rows"]


class TestFinding1ConditionsAreNotPolluted(unittest.TestCase):
    """Условие позиции содержит ТОЛЬКО своё правило."""

    def test_no_cell_separator_survives_inside_a_condition(self):
        bad = [r["code"] for r in rows() if "|" in (r.get("condition") or "")]
        self.assertEqual(bad, [], "в условие снова уехала соседняя графа или чужая строка")

    def test_the_named_case_is_clean(self):
        """3507 нёс правила Групп 36 и 37 с чужим пределом 20 %."""
        r = [x for x in rows() if x["code"] == "3507"]
        self.assertTrue(r, "позиция 3507 исчезла из таблицы")
        cond = r[0]["condition"]
        self.assertIn("50%", cond)
        self.assertNotIn("Группа", cond)
        self.assertNotIn("20%", cond)

    def test_footnote_row_is_parsed_not_swallowed(self):
        """«из 3901 - 3915 <*>» — ПОЛНАЯ строка таблицы, а не продолжение."""
        codes = {r["code"] for r in rows()}
        self.assertIn("3901", codes, "строка со сноской в графе кода снова не разобрана")

    def test_table_size_is_pinned(self):
        # ⚠ 245, а не 244: строка со сноской восстановлена правкой этого раунда.
        self.assertEqual(len(rows()), 245)


# ⚠⚠ НАХОДКА HIGH-2 (раздача квоты по кругу) ПРОВЕРЯЕТСЯ В `tests/test_k14_window_quota.py`.
# Здесь утверждение звало живой Qdrant и валило CI — класс `O3` #104. Механизм пинится на
# заглушке: она держит СЦЕНАРИЙ (документ темы отсутствует в широком пуле, приходит добором,
# мест меньше, чем претензий), а не сегодняшнюю выдачу корпуса. Живое утверждение о настоящем
# корпусе — в релизной проверке `scripts/deploy/checks/v0.5.0-test23/`.


class TestFinding3YearWithoutPeriod(unittest.TestCase):
    """Год и реквизит акта — не код ТН ВЭД, как бы они ни были записаны."""

    def test_year_forms(self):
        for q in ("какие условия достаточной переработки по ТН ВЭД в 2026 г",
                  "условия по ТН ВЭД 2026г",
                  "условия по ТН ВЭД, постановление 719 от 2020"):
            with self.subTest(q=q[:44]):
                self.assertIsNone(extract_tnved_position(q), q)

    def test_real_headings_and_codes_survive(self):
        """⚠ Положительный контроль: 1902 и 2009 — НАСТОЯЩИЕ позиции, дата в вопросе не мешает."""
        self.assertEqual(extract_tnved_position("какие условия по ТН ВЭД 1902"), "1902")
        self.assertEqual(extract_tnved_position("какие условия по ТН ВЭД 2009"), "2009")
        self.assertEqual(extract_tnved_position(
            "в редакции от 08.06.2023 какие условия по ТН ВЭД 8403"), "8403")


class TestFinding4RangeInTheNarrowerNote(unittest.TestCase):
    """Один дефект — два места: в `exact` починили раундом раньше, в оговорке остался."""

    def test_narrower_note_prints_the_whole_range(self):
        block = st1_ref.format_for_context("1506")
        self.assertIn("более узких кодов", block)
        self.assertIn("1504 - 150600000", block,
                      "диапазон снова показан одной позицией")

    def test_code_beyond_the_upper_bound_is_not_narrower(self):
        res = st1_ref.conditions_for("150610")
        self.assertEqual(res["exact"], [])
        self.assertEqual(res["narrower"], [], "код ЗА границей диапазона объявлен узким")


class TestFinding5AllTestsRunOnDirectInvocation(unittest.TestCase):
    """Блок `__main__` — только в конце файла, иначе классы ниже молча не существуют."""

    def test_main_block_is_last(self):
        src = (ROOT / "tests" / "test_eval_chaos.py").read_text(encoding="utf-8")
        i = src.index('if __name__ == "__main__"')
        self.assertNotIn("\nclass ", src[i:], "после блока запуска снова объявлены классы")


if __name__ == "__main__":
    unittest.main()
