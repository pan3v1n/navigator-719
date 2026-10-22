"""Актуализация корпуса под ред. ПП №719 от 05.10.2026 N 1297 (действует с 16.10.2026, A8 #206).

Каждый тест закрепляет то, что эта актуализация нашла и починила, и проверен мутацией: правка
отменяется — названный тест краснеет. Числа — дословно из первоисточника (RTF/HTML Контура,
documentId 508767; спорные места вёрстки — по акту на publication.pravo.gov.ru).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from app.rag import edition, thresholds  # noqa: E402

KB = ROOT / "knowledge_base" / "pp719"


def _vii() -> dict[str, dict]:
    recs = json.loads((KB / "structured" / "VII_medizdeliya.json").read_text(encoding="utf-8"))
    return {r["source_anchor"].rsplit(" ", 1)[1]: r for r in recs}


def _thr(rec: dict) -> str | None:
    return thresholds.lookup_threshold(rec["okpd2_codes"], rec["product_name"], rec["section_roman"])


class TestCorpusEdition(unittest.TestCase):
    def test_edition_row_kept_for_rollback(self):
        """С `A7` #154 корпус на редакции, действующей с 31.10.2026 (N 1297 + N 1257): «корпус на
        действующей» проверяет `test_edition_2026_10_31`. Строка этой редакции остаётся ради отката."""
        self.assertEqual(edition.KONTUR_719_BY_EDITION["ред. от 05.10.2026 N 1297"], "508767")


class TestExcludedRowsParser(unittest.TestCase):
    """N 1297 размечает исключения голым «- Исключено.» в ячейке кода."""

    def _classify(self, row: str):
        import structure_kb as sk

        return sk.classify(sk.split_fields(row))

    def test_bare_excluded_mark_in_the_code_cell(self):
        for row in ("из 26.60.14.120 - Исключено.|до 1 января 2019 г.: наличие у юридического лица|",
                    "из 32.50.13.190 -Исключено.|",
                    "из 28.30.31 - Позиции исключены.|"):
            with self.subTest(row=row[:30]):
                self.assertEqual(self._classify(row), ("empty", "excluded"))

    def test_exclusion_inside_a_name_is_not_a_mark(self):
        kind, payload = self._classify("из 32.50.13.190|Наборы (за исключением полумасок)|сборка;|")
        self.assertEqual(kind, "product")
        self.assertEqual(payload["name"], "Наборы (за исключением полумасок)")

    def test_amendment_of_an_exclusion_does_not_stick_to_the_previous_position(self):
        """До правки «Изделия из резины прочие» получали шесть чужих «в ред. N 2519», а неизменённые
        позиции VII — «в ред. N 1297» соседнего исключения."""
        import structure_kb as sk

        text = "\n".join([
            "# VII. Медицинские изделия|",
            "из 32.50.13.190|Изделия А|требование А|",
            "(в ред. Постановления Правительства РФ от 01.01.2020 N 1)|",
            "из 32.50.13.190 -Исключено.|",
            "(в ред. Постановления Правительства РФ от 05.10.2026 N 1297)|",
            "из 32.50.13.191|Изделия Б|требование Б|",
            "(в ред. Постановления Правительства РФ от 02.02.2021 N 2)|",
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            _h, products = sk.parse_section(text)
        self.assertEqual([p.name_raw for p in products], ["Изделия А", "Изделия Б"])
        self.assertEqual(products[0].amendments, ["(в ред. Постановления Правительства РФ от 01.01.2020 N 1)"])
        self.assertEqual(products[1].amendments, ["(в ред. Постановления Правительства РФ от 02.02.2021 N 2)"])


class TestSectionVIIExclusions(unittest.TestCase):
    """Две вёрстки Контура одной правки расходятся в 47 местах; арбитр — акт (стр. 2–4)."""

    def test_no_artifact_positions(self):
        for anchor, r in _vii().items():
            name = (r["product_name"] or "").strip()
            self.assertTrue(name, f"VII-{anchor}: позиция без имени (строка исключения)")
            self.assertFalse(name.lower().startswith("до 1 января"),
                             f"VII-{anchor}: имя из ячейки требований исключённой позиции")

    def test_act_excluded_one_transfusion_position_and_kept_the_other(self):
        names = [r["product_name"] for r in _vii().values()]
        self.assertFalse(any(n.startswith("Устройства для переливания крови, кровезаменителей и инфузионных "
                                          "растворов, в том числе и с микрофильтром") for n in names),
                         "исключённая N 1297 позиция «из 32.50.13.190 Устройства для переливания крови…» на месте")
        self.assertTrue(any(n.startswith("Аппараты и одноразовые системы для переливания крови") for n in names),
                        "позицию, которую акт оставил, сняла вёрстка 508767")

    def test_names_equal_source_cells(self):
        import restore_source_names as rs

        for sec in ("VII", "XI", "XXII"):
            with self.subTest(section=sec), contextlib.redirect_stdout(io.StringIO()):
                _path, _recs, fixes = rs.plan(sec)
                self.assertEqual([(i, was[:40]) for i, was, _ in fixes], [],
                                 "имя записи расходится с ячейкой первоисточника")


class TestNote80_1GroupAndVariants(unittest.TestCase):
    """Прим. 80.1: строка-группа и под ней виды со своими графиками."""

    def test_table_is_parsed_with_groups(self):
        ts = [t for t in thresholds._tables() if t["note"] == "80.1"]
        self.assertEqual(len(ts), 1, "таблица 80.1 не разобрана")
        t = ts[0]
        self.assertEqual(t["years"], ["2026 год", "2027 год", "2028 год", "2029 год", "2030 год", "с 2031 года"])
        self.assertTrue(all(r.get("strict_name") for r in t["rows"]), "строка 80.1 без признака strict_name")
        self.assertGreater(sum(1 for r in t["rows"] if r.get("variants")), 50)

    def test_group_with_one_schedule_gets_it(self):
        """«Гипс медицинский» до правки получал групповой порог прим. 7 по коду 23.52."""
        r = _vii()["141"]
        self.assertEqual(r["product_name"], "Гипс медицинский")
        thr = _thr(r)
        self.assertIn("[прим. 80.1]", thr)
        self.assertIn("2026 год — 100 баллов", thr)
        self.assertNotIn("прим. 7]", thr)

    def test_group_lists_its_variants_including_differently_named_ones(self):
        """Виды по началу имени НЕ отбираются: «Губки гемостатические…» — законный вид группы."""
        r = _vii()["126"]
        self.assertEqual(r["product_name"], "Средства гемостатические")
        thr = _thr(r)
        self.assertTrue(thr.startswith("по видам продукции [прим. 80.1]:"))
        self.assertIn("— Губки гемостатические рассасывающиеся: 2026–2028 гг. — не менее 85 баллов; "
                      "с 2029 года — 105 баллов", thr)
        self.assertIn("— Бинты, салфетки, повязки со средствами гемостатическими рассасывающимися однослойные", thr)

    def test_position_without_own_group_takes_its_variants_from_the_neighbour_group(self):
        """«… со стояночной системой торможения» своей строки-группы нет: её три вида стоят под
        группой «… без стояночной системы». Берутся только они — по имени позиции."""
        r = _vii()["154"]
        self.assertIn("со стояночной системой торможения", r["product_name"])
        thr = _thr(r)
        self.assertIsNotNone(thr, "виды под чужой группой не найдены")
        variants = [ln for ln in thr.split("\n") if ln.strip().startswith("—")]
        self.assertEqual(len(variants), 3)
        self.assertTrue(all("со стояночной системой торможения в колесах" in v for v in variants))
        self.assertNotIn("без стояночной", thr)

    def test_word_overlap_does_not_hand_out_foreign_rows(self):
        """⚠ Правило «≥2 общих слова» раздавало строки 80.1 позициям, которых в таблице нет."""
        v = _vii()
        for pos, start in (("19", "Аппараты и одноразовые системы для переливания крови"),
                           ("47", "Аппараты медицинские сшивающие")):
            with self.subTest(pos=pos):
                self.assertTrue(v[pos]["product_name"].startswith(start))
                self.assertIsNone(_thr(v[pos]), f"VII-{pos} получила чужую строку 80.1")


class TestClassifierForeignSectionTable(unittest.TestCase):
    """N 1297 дала XXII-1 (26.51.52) операции и долю компонентов — позиция вошла в срез «mixed», и
    судовая строка прим. 17(3) (код 26.51) стала «нашим дефектом». Рантайм отказывается ВЕРНО:
    таблица, назвавшая раздел XVIII, к XXII не относится."""

    def test_xxii_1_is_not_our_defect(self):
        import classify_missing_thresholds as C

        rec = json.loads((KB / "structured" / "XXII_pribory_izmereniya.json").read_text(encoding="utf-8"))[0]
        self.assertEqual(rec["okpd2_codes"], ["26.51.52"])
        self.assertIsNone(thresholds.lookup_threshold(rec["okpd2_codes"], rec["product_name"], "XXII"))
        got = C.classify(rec, C.note_rows())
        self.assertEqual((got["class"], got["note"]), ("note_other_product", "17(3)"))


def _table(note: str, *rows: dict) -> dict:
    return {"note": note, "section": None, "years": ["2026 год"], "rows": list(rows)}


def _row(name: str, value: str | None, strict: bool = False, variants=None) -> dict:
    return {"name": name, "codes": ["99.99.99.999"], "strict_name": strict,
            "by_year": {"2026 год": value} if value else {}, "variants": variants or []}


class TestReviewPR209(unittest.TestCase):
    """Ревью PR #209 (medium, 15 находок): правки, исправленные до `main`."""

    def test_schedule_runs_collapse_and_keep_cells_verbatim(self):
        c = thresholds._collapse_schedule
        self.assertEqual(c({"2026 год": "не менее 45 баллов", "2027 год": "не менее 45 баллов",
                            "2028 год": "не менее 45 баллов", "2029 год": "65 баллов",
                            "2030 год": "65 баллов", "с 2031 года": "65 баллов"}),
                         "2026–2028 гг. — не менее 45 баллов; с 2029 года — 65 баллов")
        # «не менее 65» и «65» — разные ячейки первоисточника, не сливаются.
        self.assertEqual(c({"2029 год": "не менее 65 баллов", "2030 год": "65 баллов"}),
                         "2029 год — не менее 65 баллов; 2030 год — 65 баллов")
        self.assertEqual(c({"до 1 января 2029 г.": "35 баллов", "с 1 января 2029 г.": "35 баллов"}),
                         "до 1 января 2029 г., с 1 января 2029 г. — 35 баллов")

    @staticmethod
    def _panel(rec: dict) -> str:
        import dataclasses

        from app.rag import pipeline
        from app.rag.retriever import Hit

        names = {f.name for f in dataclasses.fields(Hit)}
        return pipeline.source_text(Hit(**{k: v for k, v in {**rec, "score": 1.0}.items() if k in names}))

    def test_panel_shows_requirements_of_a_group_with_many_kinds(self):
        """VII-127 «Заплаты сердечно-сосудистые…»: порог по видам съедал кап панели целиком."""
        from app.rag import pipeline

        lines = (st := self._panel(_vii()["127"])).split("\n")
        self.assertTrue(lines[0].startswith("Порог: по видам продукции"), "порядок строк панели сменился")
        self.assertGreater(sum(ln.startswith("• ") for ln in lines), 10, "требования не видны")
        self.assertLessEqual(len(st), pipeline.SOURCE_TEXT_CAP)
        self.assertIn("… ещё видов:", st, "у 48 видов VII-127 перечень обязан быть урезан с пометкой")

    def test_panel_that_fits_is_unchanged(self):
        """Раунд 2: перенос порога вниз задевал «⚠ ИНОЙ порог» (16 позиций V и XVIII) и 60 групп 80.1,
        которым кап не грозит. Панель, укладывающаяся в кап, — порог первой строкой, как прежде."""
        from app.rag import pipeline

        sec_v = json.loads((KB / "structured" / "V_energomash_elektrotehnika.json").read_text(encoding="utf-8"))
        contactors = next(r for r in sec_v if r["source_anchor"].endswith("позиция 34"))
        self.assertTrue(contactors["product_name"].startswith("Контакторы и пускатели"))
        for rec, head in ((_vii()["154"], "Порог: по видам продукции [прим. 80.1]:"), (contactors, "Порог: ")):
            with self.subTest(pos=rec["source_anchor"][-12:]):
                st = self._panel(rec)
                self.assertLessEqual(len(st), pipeline.SOURCE_TEXT_CAP)
                self.assertTrue(st.startswith(head), st[:80])
                self.assertNotIn("… ещё видов:", st)
        self.assertIn("ИНОЙ", self._panel(contactors).split("\n• ")[0], "контроль: у контакторов — «⚠ ИНОЙ порог»")

    def test_schedule_span_needs_adjacent_years(self):
        """Пустая ячейка выпадает из графика — 2026 и 2028 без 2027 отрезком не склеиваются."""
        self.assertEqual(thresholds._collapse_schedule({"2026 год": "10 баллов", "2028 год": "10 баллов"}),
                         "2026 год, 2028 год — 10 баллов")
        self.assertEqual(thresholds._collapse_schedule({"2029 год": "5 баллов", "с 2031 года": "5 баллов"}),
                         "2029 год, с 2031 года — 5 баллов")

    def test_lookup_kind_needs_a_word_boundary(self):
        """Путь `_lookup` → `_own_variants`, а не только сама функция (раунд 2: мутация выживала)."""
        tables = [_table("80.1", _row("Шприцы медицинские", None, strict=True, variants=[
            {"name": "Шприцы инъекционные", "by_year": {"2026 год": "70 баллов"}},
            {"name": "Кресла-коляски", "by_year": {"2026 год": "80 баллов"}}]))]
        with unittest.mock.patch.object(thresholds, "_tables", lambda: tables):
            self.assertIsNone(thresholds.lookup_threshold(["99.99.99.999"], "Шприц", "VII"))
            self.assertIsNone(thresholds.lookup_threshold(["99.99.99.999"], "Кресла", "VII"))

    def test_classifier_picks_same_name_row_of_its_own_section(self):
        import classify_missing_thresholds as C

        rec = {"product_name": "Изделие Гамма", "okpd2_codes": ["99.99.99.999"], "section_roman": "VII",
               "requirement_type": "points"}
        rows = [{"codes": ["11.11.11.111"], "names": ["Изделие Гамма"], "note": "17(3)", "quote": "q1",
                 "kind": "таблица", "strict_name": False, "section": "XVIII"},
                {"codes": ["22.22.22.222"], "names": ["Изделие Гамма"], "note": "38", "quote": "q2",
                 "kind": "таблица", "strict_name": False, "section": None}]
        got = C.classify(rec, rows)
        self.assertEqual((got["class"], got["note"]), ("defect_name_match", "38"))

    def test_classifier_sees_an_unattached_kind_as_our_defect(self):
        """Вид 80.1, названный именем позиции, который рантайм НЕ привязал, — наш дефект."""
        import classify_missing_thresholds as C

        rec = {"product_name": "Шприц инъекционный", "okpd2_codes": ["99.99.99.999"], "section_roman": "VII",
               "requirement_type": "points"}
        rows = [{"codes": ["99.99.99.999"], "names": ["Шприц инъекционный малый"], "note": "80.1", "quote": "q",
                 "kind": "таблица, вид группы", "strict_name": False, "table_strict": "variant",
                 "section": None}]
        self.assertEqual(C.classify(rec, rows)["class"], "defect_unattached")

    def test_kind_of_a_position_starts_with_its_whole_word(self):
        self.assertFalse(thresholds._variant_of("Шприцы инъекционные однократного применения", "Шприц"))
        self.assertTrue(thresholds._variant_of("Шприц инъекционный", "Шприц"))
        self.assertFalse(thresholds._variant_of("Кресла-коляски с ручным приводом", "Кресла"))
        self.assertTrue(thresholds._variant_of(
            "Кресла-стулья для инвалидов с санитарным оснащением и с колесами, со стояночной системой "
            "торможения в колесах, без опоры для ног", _vii()["154"]["product_name"]))

    def test_strict_row_differing_by_separator_wins_the_tie_break(self):
        """Строка 80.1 «Ось; вал; шкив» и позиция «Ось, вал, шкив»: ключ тот же, `_norm` — нет, а
        значимое слово одно («шкив»), и правило «≥2 общих слова» отдало бы None."""
        tables = [_table("28", _row("Шкив", "не менее 10 баллов")),
                  _table("80.1", _row("Ось; вал; шкив", "не менее 20 баллов", strict=True))]
        with unittest.mock.patch.object(thresholds, "_tables", lambda: tables):
            got = thresholds.lookup_threshold(["99.99.99.999"], "Ось, вал, шкив", "VII")
        self.assertEqual(got, "2026 год — не менее 20 баллов [прим. 80.1]")

    def test_exact_named_row_beats_a_kind_of_another_group(self):
        """Строка, названная ровно как позиция, конкретнее вида чужой группы с тем же началом имени."""
        tables = [_table("33", _row("Светильник операционный", "не менее 50 баллов")),
                  _table("80.1", _row("Светильники медицинские", None, strict=True, variants=[
                      {"name": "Светильник операционный потолочный", "by_year": {"2026 год": "90 баллов"}}]))]
        with unittest.mock.patch.object(thresholds, "_tables", lambda: tables):
            got = thresholds.lookup_threshold(["99.99.99.999"], "Светильник операционный", "VII")
        self.assertEqual(got, "2026 год — не менее 50 баллов [прим. 33]")

    def test_own_strict_row_beats_the_list_verdict(self):
        """Позиция-перечень «Альфа; Бета» с СОБСТВЕННОЙ строкой-группой: вид «Альфа», стоящий в
        таблице отдельной строкой, не отнимает у неё порог."""
        tables = [_table("80.1", _row("Альфа", "не менее 10 баллов", strict=True),
                         _row("Альфа; Бета", None, strict=True, variants=[
                             {"name": "Альфа малая", "by_year": {"2026 год": "30 баллов"}},
                             {"name": "Бета большая", "by_year": {"2026 год": "40 баллов"}}]))]
        with unittest.mock.patch.object(thresholds, "_tables", lambda: tables):
            got = thresholds.lookup_threshold(["99.99.99.999"], "Альфа; Бета", "VII")
        self.assertTrue((got or "").startswith("по видам продукции [прим. 80.1]:"), got)

    def test_classifier_reads_80_1_the_way_the_runtime_does(self):
        import classify_missing_thresholds as C

        rows = C.note_rows()
        recs = {r["source_anchor"]: r for r in json.loads(
            (KB / "structured" / "VII_medizdeliya.json").read_text(encoding="utf-8"))}
        vii19 = recs["Приложение к ПП №719, Раздел VII, позиция 19"]
        self.assertNotIn(C.classify(vii19, rows)["class"], C.DEFECT_CLASSES,
                         "VII-19: строке 80.1 рантайм отказывает ВЕРНО, а классификатор зовёт дефектом")
        self.assertTrue(any(r.get("table_strict") == "variant" for r in rows), "виды 80.1 не видны")

    def test_reuse_key_includes_the_code_cell(self):
        import restructure_changed as rc
        import structure_kb as sk

        a = sk.Product(okpd2_field="30.11.33.190", name_raw="Суда промерные", component_cells=["x"])
        b = sk.Product(okpd2_field="30.11.33.191", name_raw="Суда промерные", component_cells=["x"])
        self.assertEqual(a.raw_block(), b.raw_block())
        self.assertNotEqual(rc.reuse_key(a), rc.reuse_key(b), "смена одного кода прошла бы как «та же позиция»")
        self.assertIsNone(rc.json_indent("[]"))
        self.assertEqual(rc.json_indent('[\n{\n"a": 1\n}\n]'), 0)


class TestRestructureKeepsFileForm(unittest.TestCase):
    def test_json_indent_is_read_from_the_file(self):
        import restructure_changed as rc

        self.assertEqual(rc.json_indent('[\n {\n  "a": 1\n }\n]'), 1)
        self.assertEqual(rc.json_indent('[\n  {\n    "a": 1\n  }\n]'), 2)
        for f in ("XI_mebel_derevoobrabotka.json", "VII_medizdeliya.json"):
            text = (KB / "structured" / f).read_text(encoding="utf-8")
            with self.subTest(file=f):
                self.assertEqual(json.dumps(json.loads(text), ensure_ascii=False,
                                            indent=rc.json_indent(text)), text.replace("\r\n", "\n"),
                                 "файл раздела записан не в своей форме — дифф на весь файл")


if __name__ == "__main__":
    unittest.main()
