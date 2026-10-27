"""D21 #230: порог строки с ПРОТИВОПОЛОЖНОЙ оговоркой области позиции не достаётся.

Строка с точным кодом шла в ответ без проверки наименования, и позиция получала чужой порог (так на
бою до правки): «Принтеры (кроме принтеров для печати этикеток …)» — порог строки «Принтеры (только в
отношении принтеров для печати этикеток …)»; «Сканеры биометрические» — порог «Сканеров (только в
отношении сканеров штрихкодов)»; «Аппараты для ингаляционного наркоза (за исключением аппаратов ИВЛ)» —
порог строки «Аппараты искусственной вентиляции легких». Своих строк у них в примечаниях нет — порога
нет. Радиус по всем позициям: изменились ровно эти три. Офлайн."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.rag import thresholds as t

ROOT = Path(__file__).resolve().parents[1]
RECORDS = [r for f in sorted((ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json"))
           for r in json.loads(f.read_text(encoding="utf-8")) if r.get("record_type") != "section_methodology"]


def _rec(start: str) -> dict:
    found = [r for r in RECORDS if r["product_name"].startswith(start)]
    assert found, start
    return found[0]


def _thr(r: dict) -> str | None:
    return t.lookup_threshold(r["okpd2_codes"], r["product_name"], r["section_roman"])


def _proc(r: dict) -> str | None:
    return t.lookup_procurement_threshold(r["okpd2_codes"], r["product_name"], r["section_roman"])


class TestForeignScopeIsRejected(unittest.TestCase):
    def test_three_positions_lose_the_foreign_threshold(self):
        for start in ("Принтеры (кроме принтеров для печати этикеток",
                      "Сканеры биометрические",
                      "Аппараты для ингаляционного наркоза (за исключением аппаратов искусственной"):
            self.assertIsNone(_thr(_rec(start)), start)

    def test_own_scope_keeps_its_threshold(self):
        """Те же строки своим позициям достаются по-прежнему."""
        self.assertIn("не менее 70 баллов", _thr(_rec("Принтеры (только в отношении")) or "")
        self.assertIn("не менее 50 баллов", _thr(_rec("Сканеры (только в отношении сканеров штрихкодов)")) or "")


class TestRoundTwo(unittest.TestCase):
    """Ревью D21: класс шире трёх позиций — оговорки ВНЕ кавычек, «и кроме» внутри «только»,
    перечень, разделённый строками, служебные пункты, своё «только» позиции, ветка подкода."""

    def test_row_928_exclusions_outside_quotes(self):
        """«из 26.30.11 "…" (кроме системы мониторинга окружающей среды, …» — без закрывающей скобки."""
        for start in ("Система мониторинга окружающей среды", "Система радиочастотной идентификации",
                      "Щит автоматического управления генератором", "Терминал экстренной связи",
                      "Волоконно-оптический приемопередатчик", "Корпус шкафа размещения"):
            self.assertIsNone(_thr(_rec(start)), start)

    def test_and_except_inside_only_clause(self):
        """«(только в отношении … и кроме турникетов биометрических…)» — турникеты исключены, а сама
        позиция с той же оговоркой свой порог сохраняет."""
        self.assertIsNone(_thr(_rec("Турникеты биометрические")))
        self.assertIn("не менее 40 баллов", _thr(_rec("Устройства охранной или пожарной сигнализации")) or "")

    def test_list_split_between_rows_has_no_common_threshold(self):
        """«Маммографы, рентгеновские аппараты …» ← строки «Маммографы» (100/110) и «Рентгеновские
        аппараты … (за исключением маммографов)» (100/100): общего порога нет — не гадаем."""
        self.assertIsNone(_thr(_rec("Маммографы, рентгеновские аппараты")))
        self.assertTrue(t._excludes_list_item(["Рентгеновские аппараты (за исключением маммографов)"],
                                              "Маммографы, рентгеновские аппараты передвижные"))
        self.assertFalse(t._excludes_list_item(["Рентгеновские аппараты (за исключением маммографов)"],
                                               "Рентгеновские аппараты передвижные"))

    def test_generic_items_do_not_conflict(self):
        """Пункт «продукции, классифицируемой кодом …» — служебный: коды решает `D20`."""
        self.assertFalse(t._scope_conflict(["Аппаратура радиолокационная"],
                                           "Аппаратура радиолокационная, классифицируемая кодом 26.51.20",
                                           None, ["продукции, классифицируемой кодом 26.51.20.121"]))

    def test_rule_b_counts_position_own_only_clause(self):
        self.assertFalse(t._scope_conflict(["Сканеры (только в отношении сканеров штрихкодов)"],
                                           "Сканеры (только в отношении сканеров штрихкодов ручных)"))

    def test_subcode_branch_respects_exclusions(self):
        """Подкод 15.20.32.131 исключён строкой «Обувь детская прочая (кроме … "Тапочки детские меховые")»."""
        self.assertIsNone(t.lookup_procurement_threshold(["15.20.32.131"], "Тапочки детские меховые", "XVII"))

    def test_classifier_keeps_conflict_row_as_evidence(self):
        """Ревью D21 (LOW-6): строка с противоположной оговоркой — улика «порог другой продукции», а не
        «улик нет»."""
        from scripts import classify_missing_thresholds as c
        rows = c.note_rows()
        for start in ("Принтеры (кроме принтеров", "Сканеры биометрические", "Система мониторинга окружающей"):
            self.assertEqual(c.classify(_rec(start), rows).get("class"), "note_other_product", start)


class TestSyntheticRows(unittest.TestCase):
    """Плоские строки, которых в текущем корпусе нет, но которые даст следующая редакция."""

    def _lookup(self, rows, codes, name, section="IX"):
        from unittest import mock
        with mock.patch.object(t, "_flat_thresholds", lambda: rows), mock.patch.object(t, "_tables", lambda: []):
            return t.lookup_threshold(codes, name, section)

    def test_flat_list_split_refuses(self):
        row = {"codes": ["26.60.11.113"], "names": ["Рентгеновские аппараты (за исключением маммографов)"],
               "subject_names": ["Рентгеновские аппараты (за исключением маммографов)"], "excluded": [],
               "exclusions": [], "threshold": "не менее 100 баллов", "exception": None, "note": "33"}
        self.assertIsNone(self._lookup([row], ["26.60.11.113"], "Маммографы, рентгеновские аппараты"))
        self.assertTrue(self._lookup([row], ["26.60.11.113"], "Рентгеновские аппараты передвижные"))

    def test_subcode_branch_skips_excluded_code(self):
        row = {"codes": ["15.20.32.130"], "names": ["Тапочки детские меховые"],
               "subject_names": ["Тапочки детские меховые"], "excluded": ["15.20.32.131"], "exclusions": [],
               "threshold": "не менее 90 баллов", "exception": None, "note": "7"}
        self.assertIsNone(self._lookup([row], ["15.20.32.131"], "Тапочки детские меховые"))
        row_ok = {**row, "excluded": []}
        self.assertTrue(self._lookup([row_ok], ["15.20.32.131"], "Тапочки детские меховые"))


class TestNarrowerScopeIsNotAConflict(unittest.TestCase):
    """Позиция, чья оговорка исключает ПОДВИД, остаётся при пороге строки — первая редакция правила
    ошибочно снимала его у всех этих позиций (замер радиуса)."""

    def test_general_thresholds_kept(self):
        self.assertIn("не менее 300 баллов", _thr(_rec("Краны грузоподъемные прочие (за исключением")) or "")
        self.assertTrue(_thr(_rec("Слуховые аппараты неимплантируемые (за исключением")))
        self.assertIn("120 баллов", _thr(_rec("Тазобедренный модуль с механическим управлением")) or "")
        self.assertTrue((_thr(_rec("Чехлы (лайнеры) на культи")) or "").startswith(t.BY_KIND))

    def test_procurement_thresholds_kept(self):
        self.assertTrue(_proc(_rec("Обувь детская прочая (кроме ОКПД 2")))
        self.assertTrue(_proc(_rec("Обувь защитная и прочая")))
        self.assertTrue(_proc(_rec("Спецодежда (кроме ОКПД 2")))


class TestRule(unittest.TestCase):
    def test_a_position_excludes_row_subject(self):
        self.assertTrue(t._scope_conflict(["Аппараты искусственной вентиляции легких"],
                                          "Аппараты для наркоза (за исключением аппаратов искусственной вентиляции легких)"))

    def test_b_row_only_about_something_else(self):
        self.assertTrue(t._scope_conflict(["Сканеры (только в отношении сканеров штрихкодов)"],
                                          "Сканеры биометрические"))
        self.assertFalse(t._scope_conflict(["Сканеры (только в отношении сканеров штрихкодов)"],
                                           "Сканеры штрихкодов ручные"))

    def test_c_row_excludes_position(self):
        self.assertTrue(t._scope_conflict(["Устройства ввода (кроме сканеров биометрических, сканеров штрихкодов)"],
                                          "Сканеры биометрические"))

    def test_excluded_subtype_is_not_a_conflict(self):
        self.assertFalse(t._scope_conflict(["Модуль механический (за исключением модуля механического с полимерами)"],
                                           "Модуль механический"))
        self.assertFalse(t._scope_conflict(["Слуховые аппараты"],
                                           "Слуховые аппараты (за исключением слуховых аппаратов костной проводимости)"))

    def test_same_name_never_conflicts(self):
        nm = "Чехлы (лайнеры) (за исключением чехлов трикотажных)"
        self.assertFalse(t._scope_conflict([nm], nm))

    def test_quoted_name_is_one_item(self):
        """Запятая внутри закавыченного имени пункт не делит: «не включенная в другие группировки» —
        хвост чужого имени, а не исключение предмета строки."""
        self.assertFalse(t._scope_conflict(
            ["Обувь защитная и прочая, не включенная в другие группировки"],
            'Обувь защитная и прочая, не включенная в другие группировки (кроме ОКПД 2 15.20.32.139 '
            '"Обувь детская прочая, не включенная в другие группировки")'))

    def test_subject_names_skip_exclusion_clause(self):
        line = ('из 15.20.32.130 "Обувь детская прочая" (кроме ОКПД 2 15.20.32.131 "Тапочки детские '
                'меховые"; 15.20.32.132 "Тапочки детские шубные"): - не менее 90 баллов')
        self.assertEqual(t._subject_names(line), ["Обувь детская прочая"])

    def test_unclosed_exclusion_is_stripped_from_subject(self):
        self.assertFalse(t._scope_conflict(
            ['Спецодежда (кроме ОКПД 2 14.12.30.150 "Рукавицы, перчатки производственные'],
            'Спецодежда (кроме ОКПД 2 14.12.30.150 "Рукавицы, перчатки производственные"), в том числе одежда',
            ['Спецодежда (кроме ОКПД 2 14.12.30.150 "Рукавицы, перчатки производственные']))


if __name__ == "__main__":
    unittest.main()
