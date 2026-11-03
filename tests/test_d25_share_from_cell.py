"""D25 #285: требование о доле иностранных товаров — из `notes` в блоки записи (`scripts/d25_share_from_cell.py`).

`notes` в контекст модели не идёт, поэтому обязательное требование оттуда эксперт в ответе не видел (у 27 позиций
раздела XXV — «… - не более 30 процентов цены товара <4>»). Тесты офлайн: данные корпуса и чанк источника из репо.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import d25_share_from_cell as d25  # noqa: E402

STRUCTURED = ROOT / "knowledge_base" / "pp719" / "structured"
XXV = STRUCTURED / "XXV_muz_instrumenty_zvuk.json"


def _records() -> list[dict]:
    out = []
    for f in sorted(STRUCTURED.glob("*.json")):
        out += [r for r in json.loads(f.read_text(encoding="utf-8")) if r.get("record_type") != "section_methodology"]
    return out


class TestSentences(unittest.TestCase):
    def test_wrap_inside_sentence_is_joined(self):
        cells = ["до 31 декабря 2020 г. соблюдение процентной доли стоимости использованных при производстве "
                 "иностранных товаров - не более 55 процентов цены товара <4>;\n"
                 "с 1 января 2021 г. соблюдение процентной доли стоимости использованных\n"
                 "при производстве иностранных товаров - не более 50 процентов цены товара <4>;"]
        self.assertEqual(d25.share_sentences(cells), [
            "до 31 декабря 2020 г. соблюдение процентной доли стоимости использованных при производстве иностранных "
            "товаров - не более 55 процентов цены товара <4>",
            "с 1 января 2021 г. соблюдение процентной доли стоимости использованных при производстве иностранных "
            "товаров - не более 50 процентов цены товара <4>"])

    def test_sentence_ending_mid_line_does_not_swallow_list_intro(self):
        # Фортепиано XXV: «<4>. Выполнение на территории Российской» — одна строка ячейки; без деления по «.» перед
        # заглавной к доле приклеилась бы вводная перечня с порогом и стала бы заголовком «▸» в контексте.
        cells = ["с 1 января 2024 г. соблюдение процентной доли стоимости использованных при производстве иностранных "
                 "товаров - не более 30 процентов цены товара <4>. Выполнение на территории Российской\n"
                 "Федерации следующих операций для каждой единицы продукции, оцениваемых в совокупности суммарным "
                 "количеством баллов, с 1 января 2024 г. - не менее 110 баллов:\nизготовление футора (10 баллов);"]
        self.assertEqual(d25.share_sentences(cells), [
            "с 1 января 2024 г. соблюдение процентной доли стоимости использованных при производстве иностранных "
            "товаров - не более 30 процентов цены товара <4>"])

    def test_new_share_sentence_starts_new_phrase_without_terminator(self):
        # «Альты» XXV-30: источник не поставил «;» после «<4>» — две фразы не должны слиться в одну
        cells = ["до 31 декабря 2020 г. соблюдение процентной доли - не более 55 процентов цены товара <4>\n"
                 "с 1 января 2021 г. соблюдение процентной доли - не более 50 процентов цены товара <4>;"]
        self.assertEqual(d25.share_sentences(cells), [
            "до 31 декабря 2020 г. соблюдение процентной доли - не более 55 процентов цены товара <4>",
            "с 1 января 2021 г. соблюдение процентной доли - не более 50 процентов цены товара <4>"])

    def test_only_share_sentences_are_taken(self):
        cells = ["изготовление корпуса (20 баллов);\nсоблюдение процентной доли стоимости - не более 30 процентов;"]
        self.assertEqual(d25.share_sentences(cells), ["соблюдение процентной доли стоимости - не более 30 процентов"])


class TestPlanRecord(unittest.TestCase):
    CELLS = ["с 15 ноября 2020 г. соблюдение процентной доли стоимости использованных при производстве иностранных "
             "товаров - не более 30 процентов цены товара <4>.\nВыполнение операций:\nсборка (10 баллов);"]

    def rec(self, notes, blocks=None):
        return {"product_name": "Гитары", "notes": notes,
                "requirement_blocks": blocks if blocks is not None else
                [{"component": "Гитары", "operations": [{"text": "сборка", "points": 10}], "note": None}]}

    def test_moves_share_and_clears_notes(self):
        add, notes, issues = d25.plan_record(self.rec(
            "с 15 ноября 2020 г. соблюдение процентной доли стоимости использованных при производстве иностранных "
            "товаров - не более 30 процентов цены товара <4>."), self.CELLS)
        self.assertEqual(len(add), 1)
        self.assertIsNone(notes)
        self.assertEqual(issues, [])

    def test_keeps_caveat_for_whole_position(self):
        caveat = "Выданные до 25.02.2024 заключения действительны до окончания срока их действия"
        add, notes, _ = d25.plan_record(self.rec(
            "с 15 ноября 2020 г. соблюдение процентной доли - не более 30 процентов цены товара; " + caveat),
            self.CELLS)
        self.assertEqual(len(add), 1)
        self.assertEqual(notes, caveat)

    def test_share_piece_with_foreign_number_stays_and_is_reported(self):
        # число куска notes («25 процентов») не подтверждено фразами ячейки — кусок остаётся, есть замечание
        add, notes, issues = d25.plan_record(self.rec(
            "с 15 ноября 2020 г. соблюдение процентной доли - не более 25 процентов цены товара"), self.CELLS)
        self.assertEqual(len(add), 1)
        self.assertIn("25 процентов", notes)
        self.assertTrue(issues)

    def test_share_missing_from_record_is_restored_and_notes_kept(self):
        # фразы нет ни в блоках, ни в notes — встаёт блоком, оговорка в notes остаётся
        add, notes, issues = d25.plan_record(self.rec("оговорка о сроке"), self.CELLS)
        self.assertEqual(len(add), 1)
        self.assertEqual(notes, "оговорка о сроке")
        self.assertEqual(issues, [])

    def test_share_in_position_threshold_moves_to_blocks(self):
        # раунд 6, LOW 7 (XXV-3, 11, 13–15): доля — требование, а не порог; в пороге остаётся только часть с баллами
        r = self.rec(None)
        r["min_threshold"] = d25.share_sentences(self.CELLS)[0] + ". Выполнение операций - не менее 90 баллов"
        self.assertEqual(len(d25.plan_record(r, self.CELLS)[0]), 1)
        self.assertEqual(d25.threshold_without_share(r["min_threshold"]), "Выполнение операций - не менее 90 баллов")
        self.assertIsNone(d25.threshold_without_share(d25.share_sentences(self.CELLS)[0]))

    def test_undated_operation_gets_cell_sentence(self):
        # раунд 7, MED 1 (XXV-29, 34, 35): доля операцией без даты, даты нигде в записи нет — текст фразы ячейки с датой
        body = "соблюдение процентной доли стоимости использованных при производстве иностранных товаров - не более 30 процентов цены товара"
        blocks = [{"component": "Процентная доля иностранных товаров", "note": None,
                   "operations": [{"text": body, "points": None}]}]
        fixes = d25.undated_in_record(self.rec(None, blocks), self.CELLS)
        self.assertEqual(len(fixes), 1)
        self.assertTrue(fixes[0][2].startswith("с 15 ноября 2020 г."))
        blocks[0]["note"] = "с 15 ноября 2020 г."                  # дата в note блока — текст не трогается
        self.assertEqual(d25.undated_in_record(self.rec(None, blocks), self.CELLS), [])

    def test_share_without_date_in_operation_is_present(self):
        # раунд 6, MED 2 (XXV-25, 28…40): доля стоит операцией без даты, дата — в note блока; второй раз не ставим
        body = "соблюдение процентной доли стоимости использованных при производстве иностранных товаров - не более 30 процентов цены товара"
        blocks = [{"component": "Процентная доля иностранных товаров", "note": "с 15 ноября 2020 г.",
                   "operations": [{"text": body, "points": None}]}]
        self.assertEqual(d25.plan_record(self.rec(None, blocks), self.CELLS)[0], [])

    def test_already_in_blocks_is_not_duplicated(self):
        s = d25.share_sentences(self.CELLS)[0]
        blocks = [{"component": s, "operations": [], "note": None}]
        self.assertEqual(d25.plan_record(self.rec("соблюдение процентной доли - не более 30 процентов", blocks),
                                         self.CELLS)[0], [])


class TestCorpus(unittest.TestCase):
    """Данные корпуса: класс D25 «доля иностранных товаров только в notes» закрыт и не возвращается."""

    @classmethod
    def setUpClass(cls):
        cls.recs = _records()
        cls.xxv = json.loads(XXV.read_text(encoding="utf-8"))
        cls.cells = d25.product_cells("XXV")

    def test_no_share_requirement_left_in_notes(self):
        left = [r["source_anchor"] for r in self.recs
                if re.search(r"процентн\w*\s+доли|не более \d+ процент", r.get("notes") or "", re.I)]
        self.assertEqual(left, [])

    def test_every_share_sentence_of_xxv_cell_reaches_record(self):
        # строка ячейки о доле — в блоках записи или в её пороге (так у XXV-3, 11, 13–15: рантайм печатает порог)
        missing, checked = [], 0
        for r in self.xxv:
            cells = d25.find_cells(r, self.cells)
            if cells is None:
                continue
            have = d25.blocks_text(r)
            for s in d25.share_sentences(cells):
                checked += 1
                body = d25.norm(re.sub(r"^(?:до|с|по)\s+\d{1,2}\s+\w+\s+\d{4}\s*г\.?\s*,?\s*", "", s))
                if d25.norm(s) not in have and body not in have:
                    missing.append((r["source_anchor"], s[:60]))
            self.assertFalse(re.search(r"соблюдение\s+процентной\s+доли", r.get("min_threshold") or "", re.I),
                             r["source_anchor"])                                            # раунд 6, LOW 7
        self.assertEqual(missing, [])
        self.assertGreaterEqual(checked, 60)       # положительный контроль: фраз в ячейках XXV — десятки

    def test_known_position_carries_all_dates(self):
        piano = next(r for r in self.xxv if r["product_name"] == "Фортепиано")
        comps = [b["component"] for b in piano["requirement_blocks"] if not b["operations"]]
        for pct in ("50", "45", "40", "30"):
            self.assertTrue(any(f"не более {pct} процентов цены товара" in c for c in comps), pct)
        self.assertIsNone(piano.get("notes"))

    def test_no_share_duplicate_in_record(self):
        """Раунд 6, MED 2: у XXV-25, 28…31, 34, 35, 37, 39, 40 доля стояла дважды — операцией без даты и блоком D25."""
        for r in self.xxv:
            texts = [d25.norm(b.get("component")) for b in r["requirement_blocks"]] + [
                d25.norm(o.get("text")) for b in r["requirement_blocks"] for o in b.get("operations") or []]
            shares = [re.sub(r"^(?:до|с|по) \d{1,2} \w+ \d{4} г ", "", t) for t in texts if "процентной доли" in t]
            self.assertEqual(len(shares), len(set(shares)), r["source_anchor"])

    def test_idempotent(self):
        log, issues = d25.run(write=False)
        self.assertEqual(log, [])
        self.assertEqual(issues, [])

    def test_share_dates_reach_expert_output(self):
        """Раунд 7, MED 1: дата каждой фразы о доле из ячейки — в выводе для эксперта (`source_text`), не только
        текст без даты (у XXV-29, 34, 35 оставались голые 55 / 50 / 45 % без «до 31 декабря 2020 г.» и т. п.)."""
        from app.rag import pipeline, retriever
        missing, checked = [], 0
        for r in self.xxv:
            cells = d25.find_cells(r, self.cells)
            if cells is None:
                continue
            h = retriever.Hit(score=1.0, section_roman="XXV", section_title=r.get("section_title") or "",
                              product_name=r["product_name"], okpd2_codes=r["okpd2_codes"],
                              min_threshold=r.get("min_threshold"), requirement_blocks=r["requirement_blocks"],
                              source_anchor=r.get("source_anchor"), okpd2_match=True, payload=r)
            shown = d25.norm(pipeline.source_text(h, cap=10 ** 7))
            for s in d25.share_sentences(cells):
                m = re.match(r"^((?:до|с|по)\s+\d{1,2}\s+\w+\s+\d{4}\s*г\.?)", s)
                if m:
                    checked += 1
                    if d25.norm(m.group(1)) not in shown:
                        missing.append((r["source_anchor"], m.group(1)))
        self.assertEqual(missing, [])
        self.assertGreaterEqual(checked, 50)

    def test_reaches_model_context(self):
        from app.rag import pipeline, retriever
        piano = next(r for r in self.xxv if r["product_name"] == "Фортепиано")
        h = retriever.Hit(score=1.0, section_roman="XXV", section_title=piano["section_title"],
                          product_name=piano["product_name"], okpd2_codes=piano["okpd2_codes"],
                          min_threshold=piano.get("min_threshold"), requirement_blocks=piano["requirement_blocks"],
                          source_anchor=piano.get("source_anchor"), okpd2_match=True, payload=piano)
        ctx = pipeline.format_context([h], query="Фортепиано", code=piano["okpd2_codes"][0])
        self.assertIn("не более 30 процентов цены товара", ctx)
        # доля — требование, а не заголовок перечня «▸» (вводная с порогом к ней не приклеилась)
        self.assertNotRegex(ctx, r"▸[^\n]*процентной доли")


if __name__ == "__main__":
    unittest.main()
