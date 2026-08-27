"""`K15-3` #132: свип гейта смыслов «заключения» — набор и инструмент под защитой.

⚠⚠ ЗАЧЕМ ЭТОТ ТЕСТ. Радиус правок гейта ЧЕТЫРЕ раунда ревью подряд показывал НОЛЬ по 213
вопросам приёмочных наборов, и это означало не «правка безопасна», а «класс не покрыт». Дефект
каждый раз приходилось показывать конструированным вопросом. Набор `eval_conclusion_gate.json`
закрывает слепоту; тест не даёт ему тихо усохнуть.
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

import eval_conclusion_gate as sweep  # noqa: E402


class TestSweepCoversTheClass(unittest.TestCase):
    def setUp(self):
        self.rows = sweep.evaluate()

    def test_no_new_discrepancies(self):
        """Регрессией считается расхождение БЕЗ пометки `known_limit`."""
        bad = [r for r in self.rows if not r["ok"] and not r.get("known_limit")]
        self.assertEqual(bad, [], f"новые расхождения гейта смыслов: "
                                  f"{[(r['id'], r['query'][:50]) for r in bad]}")

    def test_dangerous_direction_is_fully_covered(self):
        """⚠⚠ НАПРАВЛЕНИЯ ПРОМАХА НЕ РАВНОЦЕННЫ.

        Промах «документ → действие» отнимает у вопроса ВЕСЬ справочник и оставляет модель наедине
        с памятью на самом опасном вопросе (`K15-1` #120). Обратный промах даёт лишний абзац —
        класс `dbfc622`, неприятный, но не опасный. Поэтому класс `document` обязан быть 1.00,
        а у класса `act` записанные пределы допустимы."""
        docs = [r for r in self.rows if r["sense"] == "document"]
        missed = [r["query"] for r in docs if not r["mentions"]]
        self.assertEqual(missed, [], f"вопрос о ДОКУМЕНТЕ принят за действие: {missed}")

    def test_known_limits_are_explained_and_still_limits(self):
        """Пометка `known_limit` обязана нести причину — иначе она прячет дефект, а не описывает
        предел. И обязана оставаться пределом: починился — снять пометку, иначе запись врёт."""
        for r in self.rows:
            if r.get("known_limit"):
                self.assertTrue(r.get("why"), f"#{r['id']}: предел без объяснения")
                self.assertFalse(r["ok"], f"#{r['id']}: больше не предел — снять known_limit")

    def test_positive_control_the_set_distinguishes_both_senses(self):
        """⚠ «Ноль расхождений» и «инструмент слеп» снаружи неразличимы."""
        self.assertGreaterEqual(len(self.rows), 30, "набор усох")
        senses = {r["sense"] for r in self.rows}
        self.assertEqual(senses, {"document", "act", "none"}, "класс выпал из набора")
        self.assertTrue(any(r["mentions"] for r in self.rows), "гейт не признал документом ничего")
        self.assertTrue(any(not r["mentions"] for r in self.rows), "гейт признал документом всё")

    def test_every_case_records_where_it_came_from(self):
        """Форма без происхождения через месяц неотличима от выдуманной — а половина набора
        куплена конкретными раундами ревью."""
        data = json.loads((ROOT / "scripts" / "eval_conclusion_gate.json").read_text("utf-8"))
        for c in data["cases"]:
            self.assertTrue(c.get("source"), f"#{c['id']}: не записано, откуда форма")


if __name__ == "__main__":
    unittest.main()
