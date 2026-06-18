"""Юнит-тесты инструментов навигатора (без Qdrant, сети, модели и DeepSeek).

Покрывают: извлечение кода ОКПД2 из свободного текста и сборку чек-листа документов.
Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tools.navigator import BASE_CHECKLIST, build_checklist, extract_okpd2  # noqa: E402

from tests.test_rag import make_hit  # noqa: E402  — переиспользуем фабрику Hit


class TestExtractOkpd2(unittest.TestCase):
    def test_code_inside_text(self):
        self.assertEqual(
            extract_okpd2("Производим пеностекло, код ОКПД2 23.19.12.160"),
            "23.19.12.160",
        )

    def test_plain_codes_various_depth(self):
        self.assertEqual(extract_okpd2("29.20.23"), "29.20.23")
        self.assertEqual(extract_okpd2("28.41"), "28.41")

    def test_first_match_returned(self):
        self.assertEqual(extract_okpd2("коды 26.20.11 и 28.15.10"), "26.20.11")

    def test_none_when_absent(self):
        self.assertIsNone(extract_okpd2("производим хлеб без кода"))
        self.assertIsNone(extract_okpd2(""))


class TestBuildChecklist(unittest.TestCase):
    def test_none_hit_returns_base(self):
        self.assertEqual(build_checklist(None), BASE_CHECKLIST)

    def test_points_type_adds_score_item(self):
        hit = make_hit(payload={"requirement_type": "points"})
        items = build_checklist(hit)
        self.assertTrue(any("Расчёт набранных баллов" in i for i in items))

    def test_threshold_adds_item(self):
        hit = make_hit(min_threshold="не менее 25 баллов",
                       payload={"requirement_type": "mixed"})
        items = build_checklist(hit)
        self.assertTrue(any("не менее 25 баллов" in i for i in items))

    def test_kd_td_item_when_operations_mention_docs(self):
        hit = make_hit(requirement_blocks=[{"operations": [
            {"text": "разработка конструкторской документации", "points": None}
        ]}], payload={"requirement_type": "operations"})
        items = build_checklist(hit)
        self.assertTrue(any("конструкторскую и техническую документацию" in i for i in items))

    def test_no_extra_items_for_plain_operations(self):
        hit = make_hit(requirement_blocks=[{"operations": [
            {"text": "сборка узла", "points": None}
        ]}], payload={"requirement_type": "operations"})
        self.assertEqual(build_checklist(hit), BASE_CHECKLIST)


if __name__ == "__main__":
    unittest.main()
