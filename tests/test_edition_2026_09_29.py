"""Актуализация корпуса под ред. ПП №719 от 29.09.2026 (действует с 30.09.2026).

Каждый тест закрепляет то, что эта актуализация нашла и починила, и проверен мутацией: правка
отменяется — названный тест краснеет. Числа — дословно из первоисточника (RTF/HTML Контура,
documentId 508572).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from app.rag import edition, thresholds  # noqa: E402

KB = ROOT / "knowledge_base" / "pp719"


def _thr(codes, name, section):
    return thresholds.lookup_threshold(codes, name, section)


class TestCorpusEdition(unittest.TestCase):
    def test_corpus_is_on_the_edition_in_force(self):
        edition.corpus_edition.cache_clear()
        self.assertEqual(edition.corpus_edition(), "ред. от 29.09.2026 N 1254")
        self.assertTrue(edition.kontur_719_url().endswith("documentId=508572"))


class TestSectionIVRestored(unittest.TestCase):
    """RTF Контура после N 1026 вместо таблицы раздела IV пишет ошибку экспортёра."""

    def test_no_exporter_error_in_corpus(self):
        err = "Невозможно сохранить выбранный фрагмент таблицы"
        for p in [KB / "pp719_full.txt", *sorted((KB / "chunks").glob("*.txt"))]:
            self.assertNotIn(err, p.read_text(encoding="utf-8"), f"{p.name}: текст ошибки экспортёра")

    def test_cables_carry_the_n1026_requirements(self):
        text = (KB / "chunks" / "05_IV_fotonika_svetotehnika.txt").read_text(encoding="utf-8")
        self.assertIn("IV. Продукция отрасли фотоники и светотехники", text)
        self.assertIn("27.31.11.000|Кабели волоконно-оптические, состоящие из волокон с индивидуальными "
                      "оболочками|соблюдение процентной доли стоимости использованных при производстве "
                      "иностранных товаров - не более 70 процентов цены товара <4>,\nс 1 января 2028 г. - "
                      "не более 40 процентов цены товара <4>;|", text)
        self.assertIn("с 30 июня 2027 г. осуществление на территории Российской Федерации производства", text)
        self.assertNotIn("N 2242, действительны до окончания их срока действия", text,
                         "сноска, снятая N 1026, осталась в разделе")


class TestUnitHeaderTable(unittest.TestCase):
    """Прим. 17(3): шапка на пяти строках, единица в подписи, голые числа, объединённые ячейки."""

    def _table(self):
        ts = [t for t in thresholds._tables() if t["note"] == "17(3)"]
        self.assertEqual(len(ts), 1, "таблица 17(3) не разобрана")
        return ts[0]

    def test_table_is_parsed_whole(self):
        t = self._table()
        self.assertEqual(t["section"], "XVIII")
        self.assertEqual(t["years"], ["до 1 января 2029 г.", "с 1 января 2029 г.", "с 1 января 2031 г."])
        self.assertEqual(len(t["rows"]), 295, "эталон из HTML Контура — 295 наименований")

    def test_values_are_verbatim(self):
        thr = _thr(["13.92.29.130"], "Жилеты спасательные", "XVIII")
        self.assertEqual(thr, "до 1 января 2029 г. — не менее 35 баллов; с 1 января 2029 г. — не менее "
                              "40 баллов; с 1 января 2031 г. — не менее 45 баллов [прим. 17(3) к разд. XVIII]")

    def test_merged_value_cells_inherit_from_the_row_above(self):
        """«Шлюпки спасательные» 105/115/125 объединены (ROWSPAN) и на «Дежурные шлюпки»."""
        for name, code in (("Дежурные шлюпки", "30.12.19.143"),
                           ("Шлюпки спасательные свободнопадающие", "30.12.19.142")):
            thr = _thr([code], name, "XVIII")
            self.assertIsNotNone(thr, f"{name}: объединённая ячейка значения прочитана как пустая")
            self.assertIn("не менее 105 баллов", thr)
            self.assertIn("не менее 125 баллов", thr)

    def test_foreign_group_threshold_no_longer_wins(self):
        """До правки «Гидротермокостюмы» получали «не менее 120 баллов [прим. 7]» по группе 22.19."""
        thr = _thr(["22.19.60.191"], "Гидротермокостюмы", "XVIII")
        self.assertIn("[прим. 17(3) к разд. XVIII]", thr)
        self.assertNotIn("прим. 7", thr)


class TestNamedSectionTableStaysInItsSection(unittest.TestCase):
    """Таблица, назвавшая раздел, по коду к ЧУЖОМУ разделу не относится."""

    def test_section_v_keeps_its_own_note(self):
        thr = _thr(["27.12.10.110"], "Выключатели силовые высоковольтные напряжением 6 кВ и выше", "V")
        self.assertIsNotNone(thr)
        self.assertIn("прим. 27", thr)
        self.assertNotIn("17(3)", thr)

    def test_section_ix_does_not_get_shipbuilding(self):
        thr = _thr(["26.30.11"], "Система мониторинга окружающей среды", "IX") or ""
        self.assertNotIn("17(3)", thr)


class TestListPositionWithMixedThresholds(unittest.TestCase):
    """Позиция-перечень, чьи виды стоят в 17(3) с разными порогами, общего порога не имеет."""

    def test_winches_have_no_single_threshold(self):
        name = ("Швартовные лебедки;\nтраловые лебедки;\nгиневые лебедки;\n"
                "вспомогательные лебедки промысловой палубы")
        self.assertIsNone(_thr(["28.22.12.190"], name, "XVIII"),
                          "виду «траловые» отдан график «швартовных» (65/75/80 против 65/70/75)")

    def test_table_row_that_is_itself_a_list_is_seen_per_item(self):
        name = "Иллюминаторы;\nкрышки люков;\nтрубы вентиляционные;\nгорловины судовые"
        self.assertIsNone(_thr(["28.99.39.190"], name, "XVIII"),
                          "65/70/75 у иллюминаторов против 25/30/35 у труб — порог не общий")

    def test_list_with_equal_thresholds_keeps_it(self):
        name = ("Шлюпки спасательные\nШлюпки спасательные (пассажирские);\n"
                "шлюпки спасательные арктического исполнения")
        thr = _thr(["30.12.19.141"], name, "XVIII")
        self.assertIsNotNone(thr)
        self.assertIn("не менее 105 баллов", thr)


class TestSubcodeOfGroupWithExactName(unittest.TestCase):
    """N 1002 дал судам коды 30.11.33.191–197, а прим. 17 оставило «из 30.11.33.190 "…"»."""

    def test_rule(self):
        self.assertTrue(thresholds._subcode_of_group("30.11.33.190", "30.11.33.197"))
        self.assertFalse(thresholds._subcode_of_group("30.11.33.190", "30.11.33.190"))
        self.assertFalse(thresholds._subcode_of_group("30.11.33.100", "30.11.33.191"), "другая категория")
        self.assertFalse(thresholds._subcode_of_group("30.11.33.190", "30.11.34.191"), "другой вид")

    def test_supply_vessels_get_the_note_17_threshold(self):
        thr = _thr(["30.11.33.197"], "Суда снабжения", "XVIII")
        self.assertIsNotNone(thr)
        self.assertIn("с 1 июля 2029 г. - не менее 3750 баллов", thr)
        self.assertIn("группы кодов", thr, "порог группы обязан называть себя групповым")

    def test_subcode_without_the_same_name_gets_nothing(self):
        """⚠ Имя ПОХОЖЕЕ, а не то же: без требования дословности правило «≥2 общих слова» ниже
        отдало бы «Судам снабжения портовым» график «Судов снабжения» (проверено мутацией: на
        непохожем имени тест зеленел и без требования имени — там спасало «не гадаем»)."""
        # 30.11.33.198 — кода нет ни в одной строке таблиц (30.11.33.199 — настоящий: «Катера; …»).
        self.assertIsNone(_thr(["30.11.33.198"], "Суда снабжения портовые", "XVIII"))


class TestNamesComeFromTheSource(unittest.TestCase):
    """LLM пересказывала наименование: «Лебедки судовые» вместо перечня из десяти видов."""

    def test_xviii_names_equal_source_cells(self):
        import restore_source_names as rs

        _path, _recs, fixes = rs.plan("XVIII")
        self.assertEqual([(i, was[:40]) for i, was, _ in fixes], [],
                         "имя записи расходится с ячейкой первоисточника — scripts/restore_source_names.py")

    def test_form_differences_are_not_differences(self):
        import restore_source_names as rs

        self.assertTrue(rs.same_name("Перо руля;\nбаллер руля", "Перо руля; баллер руля"))
        self.assertTrue(rs.same_name("Ледоколы <9>", "ледоколы"))
        self.assertFalse(rs.same_name("Лебедки судовые", "Швартовные лебедки;\nтраловые лебедки"))
        self.assertEqual(rs.source_name("Жилеты <9>\n  Круги  "), "Жилеты\nКруги")


if __name__ == "__main__":
    unittest.main()
