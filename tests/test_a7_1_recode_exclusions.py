"""#212 `A7-1`: инструменты актуализации перед ред. N 1257 (`A7` #154).

1. `scripts/restructure_changed.py`: позиция, которой редакция сменила ТОЛЬКО код (N 1002: суда
   30.11.33.190 → .191–.197 при тех же требованиях), — прежняя запись с новыми кодами, без модели.
   До правки `renumber` отдавал её модели и объявлял старую запись исключённой (якорь → None в карте),
   `keep` останавливался с советом «это раздел для renumber».
2. `thresholds._tables`: строки исключений («из X - Исключено.|») и поправок («(в ред. …)|») не
   приклеиваются к следующей живой строке таблицы — у «Игла двусторонняя…» прим. 28 в кодах стояли
   коды исключённых позиций и «05.10.2026».

Каждый тест проверен мутацией: правка отменяется — названный тест краснеет.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from app.rag import thresholds  # noqa: E402

HEAD = "# XVIII. Продукция судостроения|"
AMEND_OLD = "(в ред. Постановления Правительства РФ от 01.01.2020 N 1)|"
AMEND_1002 = "(в ред. Постановления Правительства РФ от 10.08.2026 N 1002)|"


def _parse(*lines: str) -> list:
    import structure_kb as sk

    with contextlib.redirect_stdout(io.StringIO()):
        _h, products = sk.parse_section("\n".join([HEAD, *lines]))
    return products


def _rec(pos: int, name: str, codes: list[str]) -> dict:
    return {"product_name": name, "okpd2_codes": codes, "section_roman": "XVIII",
            "source_anchor": f"Приложение к ПП №719, Раздел XVIII, позиция {pos}",
            "requirement_blocks": [{"component": "x", "operations": [{"text": "требование", "points": 10}]}],
            "amendments": [AMEND_OLD.rstrip("|")]}


class TestRestructureRecode(unittest.TestCase):
    """Сценарий N 1002: код сменился, имя и требования побайтно те же."""

    def setUp(self):
        self.old = _parse("30.11.33.190|Суда промерные|требование А - 10 баллов|", AMEND_OLD,
                          "30.11.33.200|Суда прочие|требование Б - 5 баллов|")
        self.new = _parse("30.11.33.191, 30.11.33.192|Суда промерные|требование А - 10 баллов|", AMEND_1002,
                          "30.11.33.200|Суда прочие|требование Б - 5 баллов|")
        self.recs = [_rec(1, "Суда промерные", ["30.11.33.190"]), _rec(2, "Суда прочие", ["30.11.33.200"])]
        self.assertEqual(self.new[0].okpd2_codes, ["30.11.33.191", "30.11.33.192"])

    def test_renumber_keeps_the_record_with_new_codes(self):
        import restructure_changed as rc

        steps, dropped = rc.plan_steps(self.new, self.old, self.recs, "renumber")
        self.assertEqual([(k, j) for _p, k, j in steps], [("recode", 0), ("reuse", 1)])
        self.assertEqual(dropped, [], "запись позиции со сменённым кодом объявлена исключённой")
        out, amap = rc.renumber_records(steps, self.recs, {}, "XVIII")   # модель не нужна вовсе
        self.assertEqual(out[0]["okpd2_codes"], ["30.11.33.191", "30.11.33.192"])
        self.assertEqual(out[0]["product_name"], "Суда промерные")
        self.assertEqual(out[0]["requirement_blocks"], self.recs[0]["requirement_blocks"])
        self.assertIn("N 1002", " ".join(out[0]["amendments"]), "поправка смены кода потеряна")
        self.assertEqual(out[1], self.recs[1], "неизменённая позиция тронута")
        self.assertEqual(amap[self.recs[0]["source_anchor"]], self.recs[0]["source_anchor"],
                         "карта якорей: позиция со сменённым кодом не «исключена»")

    def test_keep_patches_only_the_codes(self):
        import restructure_changed as rc

        jobs, removed = rc.patch_jobs(self.new, self.old, self.recs)
        self.assertEqual([(j, k) for j, _p, k in jobs], [(0, "recode")])
        self.assertEqual(removed, [])
        out = rc.patch_records(self.recs, jobs, removed, {})
        self.assertEqual(out[0]["okpd2_codes"], ["30.11.33.191", "30.11.33.192"])
        self.assertEqual(out[0]["product_name"], "Суда промерные")
        self.assertEqual(out[0]["source_anchor"], self.recs[0]["source_anchor"])
        self.assertEqual(out[1], self.recs[1])

    def test_ambiguous_block_is_not_a_code_change(self):
        """Два одинаковых блока с другими кодами — какая старая позиция стала какой новой, не видно."""
        import restructure_changed as rc

        old = _parse("30.11.33.190|Суда|требование - 1 балл|", "30.11.33.290|Суда|требование - 1 балл|")
        new = _parse("30.11.33.191|Суда|требование - 1 балл|", "30.11.33.291|Суда|требование - 1 балл|")
        self.assertEqual(rc.recode_pairs(new, old), {})
        self.assertEqual(rc.recode_pairs(new[:1], old[:1]), {0: 0}, "контроль: однозначная пара")

    def test_changed_requirements_are_not_a_code_change(self):
        import restructure_changed as rc

        old = _parse("30.11.33.190|Суда промерные|требование А - 10 баллов|")
        new = _parse("30.11.33.191|Суда промерные|требование А - 15 баллов|")
        self.assertEqual(rc.recode_pairs(new, old), {})

    def test_record_claimed_twice_is_a_refusal_not_a_crash(self):
        """XVIII ред. 29.09: два кандидата с теми же кодами и близкими именами брали одну запись —
        скрипт падал на `assert`. Теперь спорная запись не достаётся никому."""
        import restructure_changed as rc
        import structure_kb as sk

        cands = [sk.Product(okpd2_field="30.11.33.190", name_raw="Суда промерные малые", okpd2_codes=["30.11.33.190"]),
                 sk.Product(okpd2_field="30.11.33.190", name_raw="Суда промерные малые.", okpd2_codes=["30.11.33.190"])]
        recs = [_rec(1, "Суда промерные малые", ["30.11.33.190"]), _rec(2, "Буксиры", ["30.11.33.190"])]
        self.assertEqual(rc.match_records(cands, recs), {})


class TestNoteTableExclusionRows(unittest.TestCase):
    def test_needle_row_of_note_28_has_only_its_own_code(self):
        rows = [r for t in thresholds._tables() if t["note"] == "28" for r in t["rows"]
                if r["name"] == "Игла двусторонняя для взятия венозной крови"]
        self.assertEqual([r["codes"] for r in rows], [["32.50.13.110"]])

    def test_no_table_row_carries_a_date_as_a_code(self):
        date = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
        bad = [(t["note"], r["name"][:40], c) for t in thresholds._tables() for r in t["rows"]
               for c in r["codes"] if date.match(c)]
        self.assertEqual(bad, [])

    def test_exclusion_and_amendment_lines_do_not_glue(self):
        chunk = "\n".join([
            "28. Продукция … при условии достижения следующего количества баллов:",
            "Код по ОК 034-2014|Наименование|2026 год|2027 год|",
            "из 11.11.11.111 - Исключено.|",
            "(в ред. Постановления Правительства РФ от 05.10.2026 N 1297)|",
            "из 22.22.22.220",
            "из 22.22.22.221 - Исключено.|",
            "(в ред. Постановления Правительства РФ от 05.10.2026 N 1297)|",
            "из 33.33.33.330|Изделие А|не менее 10 баллов|не менее 20 баллов|",
            "из 44.44.44.440",
            "из 44.44.44.441|Изделие Б|не менее 5 баллов|не менее 6 баллов|",
            "",
        ])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "chunk.txt"
            path.write_text(chunk, encoding="utf-8")
            orig = thresholds._CHUNK
            try:
                thresholds._CHUNK = path
                thresholds._tables.cache_clear()
                rows = {r["name"]: r["codes"] for t in thresholds._tables() for r in t["rows"]}
            finally:
                thresholds._CHUNK = orig
                thresholds._tables.cache_clear()
        self.assertEqual(rows["Изделие А"], ["33.33.33.330"])
        # Контроль: настоящая многострочная ячейка кода по-прежнему склеивается.
        self.assertEqual(rows["Изделие Б"], ["44.44.44.440", "44.44.44.441"])

    def test_exclusion_mark_is_the_one_of_the_section_parser(self):
        """Два разбора — разделов (`structure_kb`) и таблиц примечаний — обязаны узнавать исключение
        одинаково: иначе одна форма исключения починится в одном месте и останется в другом."""
        import structure_kb as sk

        self.assertEqual(thresholds._EXCLUDED_ROW_RE.pattern, sk._EXCLUDED_MARK_RE.pattern)


if __name__ == "__main__":
    unittest.main()
