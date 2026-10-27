"""D22 #235: строка примечания, ограниченного РАЗДЕЛОМ, к позициям других разделов не относится.

До правки фильтр раздела был только у таблиц (`_tables`), а у строк-списков — нет, и порог уходил по коду
в чужой раздел (так на бою): закупочные прим. 52 (вводная «…продукции, включенной в раздел XI…», мебель)
и прим. 53 («…в раздел XVII…», лёгкая промышленность) доставались противопролежневым матрацам и подушкам
VII, мебели судовой XVIII и медицинским маскам VII; общий порог строки прим. 7 «22.21 "Плиты, листы, трубы
и профили пластмассовые, за исключением продукции, предусмотренной разделом "X. …"…"» — позиции раздела X.
Радиус по 1573 позициям: изменились ровно эти 8, все теряют чужой порог. Офлайн."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from app.rag import thresholds as t

ROOT = Path(__file__).resolve().parents[1]
RECORDS = [r for f in sorted((ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json"))
           for r in json.loads(f.read_text(encoding="utf-8")) if r.get("record_type") != "section_methodology"]


def _rec(start: str, section: str) -> dict:
    found = [r for r in RECORDS if r["product_name"].startswith(start) and r["section_roman"] == section]
    assert found, (start, section)
    return found[0]


def _thr(r: dict) -> str | None:
    return t.lookup_threshold(r["okpd2_codes"], r["product_name"], r["section_roman"])


def _proc(r: dict) -> str | None:
    return t.lookup_procurement_threshold(r["okpd2_codes"], r["product_name"], r["section_roman"])


class TestForeignSectionIsRejected(unittest.TestCase):
    def test_procurement_notes_of_other_sections(self):
        for start, sec in (("Медицинские маски (за исключением", "VII"),
                           ("Противопролежневые матрацы, в том числе", "VII"),
                           ("Противопролежневые матрацы воздушные", "VII"),
                           ("Противопролежневые подушки, в том числе", "VII"),
                           ("Противопролежневые подушки воздушные", "VII"),
                           ("Мебель металлическая судовая", "XVIII"),
                           ("Мебель неметаллическая судовая", "XVIII")):
            self.assertIsNone(_proc(_rec(start, sec)), start)

    def test_row_excluding_section(self):
        self.assertIsNone(_thr(_rec("Плиты, листы теплоизоляционные из вспененного", "X")))


class TestOwnSectionKeepsThreshold(unittest.TestCase):
    def test_note_section_rows_still_apply_inside(self):
        self.assertIn("[прим. 52]", _proc(_rec("Мебель металлическая для офисов", "XI")) or "")
        self.assertIn("[прим. 53]", _proc(_rec("Пряжа шерстяная", "XVII")) or "")

    def test_section_exclusion_applies_only_to_that_section(self):
        self.assertIn("не менее 75 баллов [прим. 7]",
                      _thr(_rec("Плиты, листы, трубы и профили пластмассовые", "XXI")) or "")

    def test_unknown_section_is_not_filtered(self):
        """Раздел позиции неизвестен — строку не отбрасываем (как у таблиц)."""
        r = _rec("Мебель металлическая судовая", "XVIII")
        self.assertTrue(t.lookup_procurement_threshold(r["okpd2_codes"], r["product_name"], None))


class TestParsing(unittest.TestCase):
    def test_rows_carry_note_section(self):
        by_note = {}
        for r in t._flat_thresholds():
            by_note.setdefault(r["note"], set()).add(r.get("section"))
        self.assertEqual(by_note["52"], {"XI"})
        self.assertEqual(by_note["53"], {"XVII"})
        self.assertEqual(by_note["7"], {None})

    def test_rows_carry_excluded_sections(self):
        got = {r["codes"][0]: r["excluded_sections"] for r in t._flat_thresholds() if r.get("excluded_sections")}
        self.assertEqual(got, {"22.21": ["X"], "22.22": ["VII"], "22.29": ["VII"], "20.20.14.000": ["VII"]})

    def test_section_phrase_outside_exclusion_does_not_exclude(self):
        self.assertEqual(t._row_excluded_sections(
            '22.21 "Плиты, предусмотренные разделом "X. Продукция …"" - не менее 75 баллов'), [])
        self.assertEqual(t._row_excluded_sections(
            '22.21 "Плиты, кроме продукции, предусмотренной разделом "X. Продукция …"" - не менее 75 баллов'), ["X"])

    def test_subnote_does_not_inherit_parent_section(self):
        """Вводная прим. 80 называет раздел V (коэффициенты СЭЗ), а 80.1 — таблица раздела VII:
        подпункт берёт раздел только из своей вводной."""
        secs = t._note_sections(t._chunk_text().split("\n"))
        self.assertEqual(secs.get("80"), "V")
        self.assertNotIn("80.1", secs)


class TestSyntheticRows(unittest.TestCase):
    """Ветка 1а (строка по имени, без кодов) — в текущем корпусе строк с разделом там нет."""

    def test_named_row_respects_section(self):
        row = {"codes": [], "names": ["Кресло судовое"], "threshold": "не менее 40 баллов", "exception": None,
               "note": "17", "section": "XVIII"}
        with mock.patch.object(t, "_flat_thresholds", lambda: [row]), mock.patch.object(t, "_tables", lambda: []):
            self.assertIsNone(t.lookup_threshold([], "Кресло судовое", "XI"))
            self.assertTrue(t.lookup_threshold([], "Кресло судовое", "XVIII"))

    def test_row_in_section(self):
        self.assertTrue(t._row_in_section({"section": "XI"}, None))
        self.assertFalse(t._row_in_section({"section": "XI"}, "VII"))
        self.assertFalse(t._row_in_section({"excluded_sections": ["X"]}, "X"))
        self.assertTrue(t._row_in_section({"excluded_sections": ["X"]}, "XXI"))


class TestClassifier(unittest.TestCase):
    def test_section_excluded_row_is_evidence_not_defect(self):
        """Строка, исключающая раздел позиции, — улика «порог другой продукции», а не наш дефект привязки
        (гейт `attachment_defects == 0`)."""
        from scripts import classify_missing_thresholds as c
        rows = c.note_rows()
        self.assertEqual(c.classify(_rec("Плиты, листы теплоизоляционные из вспененного", "X"), rows)["class"],
                         "note_other_product")


if __name__ == "__main__":
    unittest.main()
