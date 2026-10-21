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
    def test_corpus_is_on_the_edition_in_force(self):
        edition.corpus_edition.cache_clear()
        self.assertEqual(edition.corpus_edition(), "ред. от 05.10.2026 N 1297")
        self.assertTrue(edition.kontur_719_url().endswith("documentId=508767"))

    def test_next_edition_did_not_leak_in(self):
        """N 1257 вступает 31.10.2026 (`A7` #154). Текст собран из вёрстки Контура 508574, где он уже
        есть, — значит его вычитание обязано быть полным: корпус стоит на ДЕЙСТВУЮЩЕЙ редакции."""
        for p in [KB / "pp719_full.txt", *sorted((KB / "chunks").glob("*.txt"))]:
            self.assertNotIn("N 1257", p.read_text(encoding="utf-8"), f"{p.name}: акт N 1257 до 31.10")


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
        self.assertIn("— Губки гемостатические рассасывающиеся: 2026 год — не менее 85 баллов", thr)
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
