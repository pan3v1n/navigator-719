"""D14-1 #176: пороги примечаний остаются на ПОСЕГМЕНТНОЙ иерархии ОКПД2 — поразрядная отклонена замером.

Замер 27.10.2026 (`docs/eval_runs/2026-10-27_d14_1_thresholds_digitwise.md`): поразрядный `_code_applies`
дал бы порог 21 позиции и снял бы у двух — верны 1–2 приобретения. Строка с кодом «…YZ0» или короткой
группировкой называет конкретное изделие, и подъём к ней отдаёт ЧИСЛО чужого изделия без его имени.
Здесь — представительные случаи; поразрядное правило каждый из них ломает. Офлайн."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.rag import thresholds as t

ROOT = Path(__file__).resolve().parents[1]
RECORDS = [r for f in sorted((ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json"))
           for r in json.loads(f.read_text(encoding="utf-8")) if r.get("record_type") != "section_methodology"]


def _rec(start: str, section: str) -> dict:
    found = [r for r in RECORDS if r["section_roman"] == section and r["product_name"].startswith(start)]
    assert found, (section, start)
    return found[0]


def _thr(r: dict, proc: bool = False) -> str:
    fn = t.lookup_procurement_threshold if proc else t.lookup_threshold
    return fn(r["okpd2_codes"], r["product_name"], r["section_roman"]) or ""


class TestThresholdsStaySegmentwise(unittest.TestCase):
    def test_rule_is_segmentwise(self):
        self.assertFalse(t._code_applies("29.32.30.340", "29.32.30.345"))   # поразрядно — предок
        self.assertFalse(t._code_applies("28.92.1", "28.92.11.120"))
        self.assertTrue(t._code_applies("28.92", "28.92.11.120"))

    def test_sound_equipment_of_xxv_gets_no_radioelectronics_threshold(self):
        """XXV/1 прямо исключает «промышленную продукцию 26.40.4»; строка прим. 26.1 — радиоэлектроника IX."""
        self.assertNotIn("прим. 26.1", _thr(_rec("Микрофоны и подставки для них", "XXV")))
        self.assertNotIn("прим. 26.1", _thr(_rec("Усилители электрические звуковых частот", "XXV")))

    def test_mining_machines_get_no_drilling_services_threshold(self):
        """Строка прим. 78 28.92.1 — «Высокотехнологичные сервисы при бурении (роторные управляемые системы)»."""
        self.assertNotIn("прим. 78", _thr(_rec("Конвейеры непрерывного действия для подземных работ", "VI")))
        self.assertNotIn("прим. 78", _thr(_rec("Комбайны проходческие", "VI")))

    def test_pump_parts_get_no_compressor_parts_threshold(self):
        self.assertNotIn("прим. 9", _thr(_rec("Части насосов, части подъемников жидкостей", "XIX"), proc=True))

    def test_own_thresholds_are_not_lost(self):
        """IX/114 и IX/133 при поразрядном правиле теряли свой порог: появлялся второй кандидат-предок."""
        self.assertIn("не менее 200 баллов", _thr(_rec("Многофункциональная консоль управления агрегатами", "IX")))
        self.assertIn("не менее 200 баллов", _thr(_rec("Электронный блок предупреждения об опасном сближении", "IX")))


if __name__ == "__main__":
    unittest.main()
