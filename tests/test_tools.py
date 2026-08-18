"""Юнит-тесты инструментов навигатора (без Qdrant, сети, модели и DeepSeek).

Покрывают: извлечение кода ОКПД2 из свободного текста и сборку чек-листа документов.
Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tools.checklist import checklist_for  # noqa: E402
from app.tools.navigator import build_checklist, extract_okpd2  # noqa: E402

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
    """R25: чек-лист строится из раздела 4 Приказа ТПП РФ №52, а не из зашитого списка.

    Прежний `BASE_CHECKLIST` — шесть правдоподобных, но ВЫДУМАННЫХ пунктов без ссылки на норму:
    ровно то, за что проект ругает языковую модель. Теперь у каждого пункта есть номер, по
    которому эксперт сверится с первоисточником."""

    BASE = ("4.2.1", "4.2.2", "4.2.3")

    def _points(self, items):
        return {m for i in items for m in re.findall(r"п\. (4(?:\.\d+)+)", i)}

    def test_base_points_always_present_and_sourced(self):
        for items in (build_checklist(None), build_checklist(make_hit())):
            self.assertTrue(set(self.BASE) <= self._points(items))
            for i in items:
                if "Расчёт набранных баллов" in i:
                    continue  # прикладное напоминание, помечено как не-пункт Приказа
                self.assertIn("Приказ ТПП РФ №52", i, i[:60])

    def test_service_center_requirement_adds_its_point(self):
        hit = make_hit(requirement_blocks=[{"operations": [
            {"text": "наличие сервисного центра, уполномоченного осуществлять ремонт", "points": None}
        ]}], payload={"requirement_type": "operations"})
        self.assertIn("4.3.4", self._points(build_checklist(hit)))

    def test_kd_td_requirement_adds_its_point(self):
        hit = make_hit(requirement_blocks=[{"operations": [
            {"text": "наличие прав на конструкторскую и технологическую документацию", "points": None}
        ]}], payload={"requirement_type": "operations"})
        self.assertIn("4.3.1", self._points(build_checklist(hit)))

    def test_component_only_block_is_read_too(self):
        # обязательные условия часто лежат в `component` без операций (см. pipeline._hit_operations),
        # и раньше подбор условных пунктов их бы не увидел
        hit = make_hit(requirement_blocks=[
            {"component": "наличие сервисного центра на территории ЕАЭС", "operations": []}
        ], payload={"requirement_type": "operations"})
        self.assertIn("4.3.4", self._points(build_checklist(hit)))

    def test_block_note_is_read_too(self):
        """D9: у 12 позиций признак условного пункта живёт ТОЛЬКО в `note` блока.

        «Процентная доля», «доля массы», баллы — по ним включаются п. 4.3.5/4.3.6/4.3.7, и без
        чтения `note` чек-лист /navigate молча приходил неполным (напр. «Асфальтоукладчики» III,
        «Установка для обезвреживания медицинских отходов» VII)."""
        hit = make_hit(requirement_blocks=[{
            "component": "сборка",
            "note": "процентная доля стоимости иностранных материалов не более 30 процентов",
            "operations": [{"text": "сборка", "points": None}]}],
            payload={"requirement_type": "operations"})
        self.assertIn("4.3.6", self._points(build_checklist(hit)))

    def test_points_model_adds_operations_point_and_threshold(self):
        hit = make_hit(min_threshold="не менее 25 баллов",
                       requirement_blocks=[{"operations": [{"text": "сварка", "points": 5}]}],
                       payload={"requirement_type": "points"})
        items = build_checklist(hit)
        self.assertIn("4.3.5", self._points(items))
        self.assertTrue(any("не менее 25 баллов" in i for i in items))

    def test_plain_operations_do_not_pull_unrelated_points(self):
        hit = make_hit(requirement_blocks=[{"operations": [{"text": "сборка узла", "points": None}]}],
                       payload={"requirement_type": "operations"})
        pts = self._points(build_checklist(hit))
        self.assertNotIn("4.3.4", pts)   # сервисного центра в требованиях нет
        self.assertNotIn("4.3.8", pts)   # адвалорной доли тоже

    def test_missing_source_yields_empty_not_invented(self):
        # нет первоисточника → пустой список; выдумывать документы нельзя
        import app.tools.checklist as ch
        orig = ch._points
        ch._points = lambda: {}
        try:
            self.assertEqual(checklist_for("что угодно", has_points=True), [])
        finally:
            ch._points = orig


if __name__ == "__main__":
    unittest.main()

class TestExtractOkpd2All(unittest.TestCase):
    """`EV8` #88: из вопроса достаются ВСЕ коды, а не первый."""

    def test_all_codes_in_order_without_duplicates(self):
        from app.tools.navigator import extract_okpd2, extract_okpd2_all
        q = "можешь сравнить требования по нашему коду 28.13.14 и по 26.30.50"
        self.assertEqual(extract_okpd2(q), "28.13.14")          # ретрив бустится первым
        self.assertEqual(extract_okpd2_all(q), ["28.13.14", "26.30.50"])
        self.assertEqual(extract_okpd2_all("28.13 и снова 28.13"), ["28.13"])
        self.assertEqual(extract_okpd2_all("кодов нет вовсе"), [])
