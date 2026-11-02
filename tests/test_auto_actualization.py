"""`A10`: автоматическая актуализация. Офлайн: без сети, модели и Qdrant.

Фрагменты страниц — настоящая разметка Контура (02.11.2026, документы 508574, 508767 и акт N 1035):
шаблоны разбора писались вслепую и один раз уже промахнулись по кавычкам, поэтому тесты идут на живой форме.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import actualize_edition as ae  # noqa: E402
import auto_actualize as aa  # noqa: E402
import edition_source as es  # noqa: E402
import watch_edition as we  # noqa: E402
from app.core.prompts import KB_PARSER_SYSTEM_PROMPT  # noqa: E402

ACTUAL = """
<div class="rev_info-status" id="js-revisions-status" data-status="Actual" data-status-type="General"><b>Редакция от 05.10.2026 &mdash; Действует с 31.10.2026</b></div>
<div class="doc_info-title2">Примечания</div>
<div class="doc_info-text dt-list_colon-separated">
<P class="dt-p" data-wi="0">ИЗМЕНЕН&nbsp;</P><P class="dt-p" data-wi="1"></P>
<P class="dt-p" data-wi="1"><A HREF="https://www.kontur-extern.ru/info/normativ/document/1/501304-x#l0" class="C" TARGET="_blank">Постановление Правительства РФ от 27.08.2025 N 1284</A> (с&nbsp;01.06.2027)</P>
<P class="dt-p" data-wi="1"><A HREF="https://www.kontur-extern.ru/info/normativ/document/1/508840-x#l0" class="C" TARGET="_blank">Постановление Правительства РФ от 15.10.2026 N 1353</A> (с&nbsp;01.01.2027)</P>
</div></div>
"""
OUTDATED = ("""<div class="rev_info-status" id="js-revisions-status" data-status="NotActual" data-status-type="General">"""
            """<b>Редакция от 05.10.2026 &mdash; Не действует</b> <a class='rev_info-actual_doc_link js-actualDocLink' """
            """href='/info/normativ/document/1/508574-postanovlenie-pravitelstva-rf-ot-17-07-2015-n-719' """
            """target='_blank'>Перейти в действующую</a></div>""")
FUTURE = ("""<div class="rev_info-status" id="js-revisions-status" data-status="NotInForce" data-status-type="General">"""
          """<b>Редакция от 20.08.2026 &mdash; Вступит в силу с 01.01.2027</b></div>""")

DOC = 'x<div id="js-doc-frame-container">{}</div>'
ACTUAL_PAGE = ACTUAL + DOC.format("текст документа")
OUTDATED_PAGE = OUTDATED + DOC.format("текст документа")
TEXT = "Постановление N 719. " + "Баллы 0,4 и (30 баллов). " * 3
TAIL = "<a>напечатать</a> скачать поделиться ссылкой"
TODAY = (2026, 11, 2)
KNOWN = {"N 1284 от 27.08.2025": "01.06.2027", "N 1353 от 15.10.2026": "01.01.2027"}


class TestParsePage(unittest.TestCase):
    def test_actual_status_and_pending_acts(self):
        info = es.parse_page(ACTUAL)
        self.assertTrue(info.is_actual)
        self.assertEqual(info.title, "Редакция от 05.10.2026 — Действует с 31.10.2026")
        self.assertEqual((info.edition_date, info.in_force_from), ("05.10.2026", "31.10.2026"))
        self.assertEqual([(a.key, a.effective) for a in info.pending],
                         [("N 1284 от 27.08.2025", "01.06.2027"), ("N 1353 от 15.10.2026", "01.01.2027")])

    def test_outdated_page_link_in_single_quotes(self):
        info = es.parse_page(OUTDATED)
        self.assertFalse(info.is_actual)
        self.assertEqual(info.jumps, {"508574": "Перейти в действующую"})

    def test_future_edition(self):
        info = es.parse_page(FUTURE)
        self.assertTrue(info.is_future)
        self.assertFalse(info.is_actual)
        self.assertEqual(info.in_force_from, "01.01.2027")

    def test_unknown_markup_is_none_not_empty(self):
        self.assertIsNone(es.parse_page("<html>капча</html>"))
        self.assertIsNone(es.parse_page('<div id="js-revisions-status" data-status="Actual">что-то</div>'))

    def test_body_separator(self):
        page = '<div id="js-doc-frame-container"><td>от 13.12.2023 N 2139</td><td>28</td></div>'
        self.assertEqual(we.acts(es.document_body(page, sep=" ")), {(2023, 12, 13, 2139)})
        self.assertEqual(we.acts(es.document_body(page)), {(2023, 12, 13, 213928)})

    def test_no_document_container_gives_empty_body(self):
        self.assertEqual(es.document_body(ACTUAL), "")


class TestFetch(unittest.TestCase):
    def test_incomplete_read_is_a_refusal_not_a_crash(self):
        with mock.patch("urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"x")):
            data, why = es.fetch("https://x.invalid", tries=1)
        self.assertIsNone(data)
        self.assertIn("IncompleteRead", why)


class TestRestoreLostBlocks(unittest.TestCase):
    PRE = "а" * 239 + "\n"
    POST = "б" * 240

    def test_block_taken_between_boundaries(self):
        old = self.PRE + "ТАБЛИЦА IV\n" + self.POST
        done, restored = es.restore_lost_blocks(self.PRE + es.LOST_MARK + self.POST, old)
        self.assertEqual(done, old)
        self.assertEqual(restored[0].chars, len("ТАБЛИЦА IV\n"))

    def test_ambiguous_boundary_refused(self):
        old = self.PRE + "x" + self.PRE + "ТАБЛИЦА" + self.POST
        with self.assertRaises(es.SourceError):
            es.restore_lost_blocks(self.PRE + es.LOST_MARK + self.POST, old)

    def test_boundary_absent_refused(self):
        with self.assertRaises(es.SourceError):
            es.restore_lost_blocks(self.PRE + es.LOST_MARK + self.POST, "другой текст " * 50)

    def test_reversed_boundaries_refused(self):
        with self.assertRaises(es.SourceError):
            es.restore_lost_blocks(self.PRE + es.LOST_MARK + self.POST, self.POST + "x" + self.PRE)

    def test_mark_at_document_edge_refused(self):
        with self.assertRaises(es.SourceError):
            es.restore_lost_blocks(es.LOST_MARK + self.POST, self.POST)


class TestWitness(unittest.TestCase):
    def page(self, text: str = TEXT, tail: str = TAIL) -> str:
        return DOC.format(f"<p>{text}</p>{tail}")

    def test_equal_ignores_markup_and_page_tail(self):
        self.assertTrue(es.witness(TEXT, self.page()).equal)

    def test_subscript_has_no_gap(self):
        self.assertTrue(es.witness("формула Yt = 1 " + TEXT, self.page("формула Y<sub>t</sub> = 1 " + TEXT)).equal)

    def test_digit_comma_and_extra_text_fail(self):
        for bad in (TEXT.replace("30", "31", 1), TEXT.replace("0,4", "04", 1), TEXT + " лишнее"):
            self.assertFalse(es.witness(bad, self.page()).equal, bad[:40])

    def test_separator_between_digits_is_seen(self):
        page = self.page("ОК 034-2014 (КПЕС) " + TEXT)
        self.assertTrue(es.witness("ОК 034-2014 (КПЕС) " + TEXT, page).equal)
        self.assertFalse(es.witness("ОК 03-42014 (КПЕС) " + TEXT, page).equal)

    def test_unknown_tail_fails(self):
        self.assertFalse(es.witness(TEXT, self.page(tail="<p>ещё один абзац нормы</p>")).equal)

    def test_foreign_document_fails(self):
        self.assertFalse(es.witness(TEXT, DOC.format("<p>совсем другой документ о другом</p>")).equal)

    def test_mismatch_points_to_the_place(self):
        w = es.witness(TEXT.replace("30", "31", 1), self.page())
        self.assertIn("(31баллов)", w.ours)
        self.assertIn("(30баллов)", w.theirs)


class TestBanner(unittest.TestCase):
    def test_banner_removed_only_at_start(self):
        line = "Редакция данного документа вступает в силу с 31.10.2026."
        self.assertEqual(es.BANNER_RE.sub("", f"{line}\nТекст"), "Текст")
        self.assertEqual(es.BANNER_RE.sub("", f"Текст\n{line}"), f"Текст\n{line}")


class TestScrub(unittest.TestCase):
    def test_settings_error_and_key_are_hidden(self):
        self.assertNotIn("секрет", ae.scrub("Extra inputs [input_value='секрет', input_type=str]"))
        self.assertNotIn("sk-abcdef12345", ae.scrub("ключ sk-abcdef12345 в тексте"))

    def test_env_value_hidden_in_any_form(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "ZZTOPSECRET"}):
            self.assertNotIn("ZZTOPSECRET", ae.scrub("вывод ZZTOPSECRET конец"))

    def test_failed_command_message_is_scrubbed(self):
        done = SimpleNamespace(returncode=1, stdout="", stderr="input_value='секрет'")
        with mock.patch.object(ae.subprocess, "run", return_value=done), self.assertRaises(ae.Stop) as ctx:
            ae.run(["x"])
        self.assertNotIn("секрет", str(ctx.exception))

    def test_report_is_scrubbed(self):
        steps = [{"шаг": "control", "итог": "стоп", "деталь": "input_value='секрет'"}]
        self.assertNotIn("секрет", ae.render_report("1", "ОСТАНОВЛЕНО: input_value='секрет'", steps, {}))


class TestRecords(unittest.TestCase):
    REC = {"product_name": "Магниты", "okpd2_codes": ["25.99.29.110"], "source_anchor": "XXIII, позиция 3"}

    def test_renumber_is_not_a_change(self):
        moved = {**self.REC, "source_anchor": "XXIII, позиция 4", "amendments": ["N 1257"], "section_roman": "XXIII"}
        before = {"a": {**self.REC, "section_roman": "XXIII"}}
        self.assertEqual(ae.changed_anchors(before, {"b": moved}, frozenset({"XXIII"})), set())

    def test_new_and_edited_records_are_changes(self):
        self.assertEqual(ae.changed_anchors({"a": self.REC}, {"a": {**self.REC, "notes": "другое"}}), {"a"})

    def test_duplicates_by_name_and_codes_do_not_glue(self):
        one, two = {**self.REC, "notes": "первая"}, {**self.REC, "notes": "вторая"}
        self.assertEqual(ae.changed_anchors({"a": one, "b": two}, {"a": one, "b": two}), set())

    def test_foreign_threshold_caught_even_after_renumber(self):
        moved = {**self.REC, "source_anchor": "XXIII, позиция 4"}
        bad = ae.foreign_thresholds({"a": "70 баллов"}, {"b": "70 баллов; Иглы - 115 баллов"},
                                    {"a": self.REC}, {"b": moved})
        self.assertEqual(len(bad), 1)
        self.assertIn("115", bad[0])

    def test_edited_record_may_change_threshold(self):
        edited = {**self.REC, "notes": "правка акта"}
        self.assertEqual(ae.foreign_thresholds({"a": "70"}, {"a": "80"}, {"a": self.REC}, {"a": edited}), [])

    def test_new_record_is_not_foreign(self):
        self.assertEqual(ae.foreign_thresholds({}, {"a": "115"}, {}, {"a": self.REC}), [])

    def test_source_points_skip_threshold(self):
        raw = "сварка (20 баллов)\nсборка (20 баллов)\nне менее 115 баллов\nпокраска 5 баллов"
        self.assertEqual(ae.source_points(raw), Counter({20: 2, 5: 1}))

    def test_record_points_skip_requirements_without_points(self):
        rec = {"requirement_blocks": [{"operations": [{"text": "а", "points": 20}, {"text": "б", "points": None}]}]}
        self.assertEqual(ae.record_points(rec), Counter({20: 1}))
        self.assertEqual(len(ae.record_operations(rec)), 2)


class TestReviewRows(unittest.TestCase):
    OPS = [("сварка рамы", 20), ("сборка узла", 2)]

    def rec(self, ops=None, notes=None):
        return {"product_name": "Иглы", "section_roman": "XXIII", "okpd2_codes": ["25.93"], "notes": notes,
                "requirement_blocks": [{"operations": [{"text": t, "points": n} for t, n in (ops or self.OPS)]}]}

    def rows(self, rec, cells=("сварка рамы 20 баллов", "сборка узла 2 балла")):
        # Настоящий `Product`: заглушка с атрибутом `raw_block` скрыла, что это метод, и реплей упал на шлюзах.
        import structure_kb as sk

        product = sk.Product(okpd2_field="25.93", name_raw="Иглы", okpd2_codes=["25.93"], component_cells=list(cells))
        chunk = SimpleNamespace(read_text=lambda encoding: "")
        with mock.patch("structure_kb.parse_section", return_value=("", [product])), \
                mock.patch("structure_kb.find_chunk", return_value=chunk):
            return ae.review_rows({"a"}, {"a": rec}, {"a": "не менее 115"})

    def rows_with_previous(self, previous, rec=None):
        import structure_kb as sk

        cell = "сварка рамы 20 баллов\nналичие прав на конструкторскую документацию у изготовителя"
        product = sk.Product(okpd2_field="25.93", name_raw="Иглы", okpd2_codes=["25.93"], component_cells=[cell])
        chunk = SimpleNamespace(read_text=lambda encoding: "")
        rec = rec or self.rec([("сварка рамы", 20)])
        with mock.patch("structure_kb.parse_section", return_value=("", [product])), \
                mock.patch("structure_kb.find_chunk", return_value=chunk):
            return ae.review_rows({"a"}, {"a": rec}, {}, previous)

    def test_requirement_dropped_by_new_record_is_addressed(self):
        was = self.rec([("сварка рамы", 20), ("наличие прав на конструкторскую документацию у изготовителя", None)])
        _, notes = self.rows_with_previous({"старый якорь": was})
        self.assertEqual(len(notes), 1)
        self.assertIn("a: требования из ячейки не нашлись", notes[0])

    def test_gap_of_both_records_is_one_summary_line(self):
        _, notes = self.rows_with_previous({"старый якорь": self.rec([("сварка рамы", 20)])})
        self.assertEqual(len(notes), 1)
        self.assertIn("Ещё 1 записей", notes[0])
        self.assertIn("['a']", notes[0])

    def test_new_position_reports_every_lost_requirement(self):
        _, notes = self.rows_with_previous({})
        self.assertIn("a: требования из ячейки не нашлись", notes[0])

    def test_previous_record_prefers_same_codes(self):
        same_name = {"x": {"product_name": "Иглы", "okpd2_codes": ["1"], "id": 1},
                     "y": {"product_name": "Иглы", "okpd2_codes": ["2"], "id": 2}}
        self.assertEqual(ae.previous_record("Иглы", ["2"], same_name)["id"], 2)
        self.assertEqual(ae.previous_record("Иглы", ["3"], same_name)["id"], 1)
        self.assertIsNone(ae.previous_record("Нет", ["1"], same_name))

    def test_name_of_the_position_is_not_a_lost_line(self):
        name = "Подшипники шариковые или роликовые особого назначения"
        all_lines, absent = ae.source_lines(f"{name}\nнастоящая длинная строка требования источника", {"product_name": name})
        self.assertEqual(len(all_lines), 2)
        self.assertEqual(len(absent), 1)

    def test_clean_record_has_no_notes(self):
        rows, notes = self.rows(self.rec())
        self.assertEqual(notes, [])
        self.assertIn("22 / 22", rows[0])

    def test_swapped_points_are_seen(self):
        _, notes = self.rows(self.rec([("сварка рамы", 2), ("сборка узла", 20)]))
        self.assertEqual(len(notes), 1)
        self.assertIn("не подтверждена у 2", notes[0])

    def test_lost_points_flagged(self):
        _, notes = self.rows(self.rec([("сварка рамы", 20)]))
        self.assertIn("нет в записи [2.0]", notes[0])

    def test_extra_points_flagged(self):
        _, notes = self.rows(self.rec([*self.OPS, ("выдумка", 7)]))
        self.assertIn("лишние [7.0]", notes[0])

    def test_operations_with_points_are_compared(self):
        rows, _ = self.rows(self.rec())
        self.assertIn("2 (2 / 2)", rows[0])

    def test_lost_requirement_without_points_is_visible(self):
        long_line = "наличие сервисного центра на территории страны союза"
        rows, notes = self.rows(self.rec(), cells=("сварка рамы 20 баллов", "сборка узла 2 балла", long_line))
        self.assertIn("0 / 1", rows[0])
        self.assertIn("требования из ячейки не нашлись", notes[0])

    def test_kept_requirement_is_quiet(self):
        long_line = "наличие сервисного центра на территории страны союза"
        rec = self.rec([*self.OPS, (long_line, None)])
        _, notes = self.rows(rec, cells=("сварка рамы 20 баллов", "сборка узла 2 балла", long_line))
        self.assertEqual(notes, [])

    def test_table_row_survives_line_breaks_and_pipes(self):
        self.assertEqual(ae.table_row("a\nb", "c|d"), "| a b | c/d |")

    def test_requirement_in_notes_flagged(self):
        _, notes = self.rows(self.rec(notes="наличие прав на документацию"))
        self.assertTrue(any("notes" in n for n in notes))

    def test_unmatched_record_flagged(self):
        rec = {**self.rec(), "product_name": "Другое"}
        rows, notes = self.rows(rec)
        self.assertIn("не сопоставлена", notes[0])
        self.assertIn("?", rows[0])

    def test_decimal_points(self):
        self.assertEqual(ae.source_points("операция 0,5 балла"), Counter({0.5: 1}))
        self.assertTrue(ae.pair_confirmed("операция 0,5 балла", "операция", 0.5))
        self.assertFalse(ae.pair_confirmed("операция 0,5 балла", "операция", 5))
        self.assertEqual(ae.record_points({"requirement_blocks": [{"operations": [{"points": "2,5"}]}]}), Counter({2.5: 1}))


def make_ctx(**over) -> ae.Ctx:
    args = SimpleNamespace(sources=".", rtf=None, page=None, max_llm=60, allow_outdated=False)
    ctx = ae.Ctx(doc_id="508574", args=args)
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


class TestGuard(unittest.TestCase):
    def test_protected_branches_refused(self):
        # База чистая: остановить обязана именно проверка ветки, а не соседняя.
        for branch in (*ae.PROTECTED_BRANCHES, "HEAD"):
            with mock.patch.object(ae, "git", return_value=f"{branch}\n"), \
                    mock.patch.object(ae, "kb_changes", return_value=[]), \
                    self.assertRaisesRegex(ae.Stop, "только на своей ветке"):
                ae.step_guard(make_ctx())

    def test_own_branch_allowed(self):
        with mock.patch.object(ae, "git", return_value="auto/edition-1\n"), \
                mock.patch.object(ae, "kb_changes", return_value=[]):
            self.assertIn("auto/edition-1", ae.step_guard(make_ctx()))

    def test_dirty_base_refused(self):
        with mock.patch.object(ae, "git", return_value="auto/edition-1\n"), \
                mock.patch.object(ae, "kb_changes", return_value=["knowledge_base/x"]), \
                self.assertRaisesRegex(ae.Stop, "база уже изменена"):
            ae.step_guard(make_ctx())


class TestSourceStep(unittest.TestCase):
    def step(self, html: str, allow: bool = False):
        with tempfile.TemporaryDirectory() as d:
            page = Path(d) / "p.html"
            page.write_text(html, encoding="utf-8")
            args = SimpleNamespace(sources=d, rtf="a.rtf", page=str(page), allow_outdated=allow)
            return ae.step_source(ae.Ctx(doc_id="508767", args=args))

    def test_outdated_page_refused_without_flag(self):
        with self.assertRaisesRegex(ae.Stop, "не действующая редакция"):
            self.step(OUTDATED)
        self.assertIn("Не действует", self.step(OUTDATED, allow=True))

    def test_future_edition_accepted(self):
        self.assertIn("Вступит в силу", self.step(FUTURE))

    def test_unparsed_page_stops(self):
        with self.assertRaisesRegex(ae.Stop, "не разобрана"):
            self.step("<html>капча</html>")

    def test_download_failure_stops(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ae.es, "fetch", return_value=(None, "timed out")):
            args = SimpleNamespace(sources=d, rtf=None, page=None, allow_outdated=False)
            with self.assertRaisesRegex(ae.Stop, "не проверено"):
                ae.step_source(ae.Ctx(doc_id="1", args=args))


class TestTextStep(unittest.TestCase):
    HEAD = "(в ред. от 01.01.2026 N 1, от 02.02.2026 N 2) Правительство постановляет: "

    def step(self, new_text: str, old_text: str | None = None, page_text: str | None = None):
        with tempfile.TemporaryDirectory() as d:
            rtf, page, full = Path(d) / "a.rtf", Path(d) / "a.html", Path(d) / "full.txt"
            rtf.write_bytes(b"x")
            page.write_text(DOC.format(f"<p>{page_text or new_text}</p>{TAIL}"), encoding="utf-8")
            full.write_text(old_text or self.HEAD + "старый текст", encoding="utf-8")
            ctx = make_ctx()
            ctx.args.rtf, ctx.args.page = str(rtf), str(page)
            with mock.patch.object(ae.es, "rtf_to_corpus_text", return_value=new_text), mock.patch.object(ae, "FULL", full):
                return ctx, ae.step_text(ctx)

    def test_equal_text_goes_on(self):
        ctx, _ = self.step(self.HEAD + TEXT)
        self.assertEqual(ctx.new_text, self.HEAD + TEXT)

    def test_mismatch_stops(self):
        with self.assertRaisesRegex(ae.Stop, "не совпал с HTML"):
            self.step(self.HEAD + TEXT, page_text=self.HEAD + TEXT.replace("30", "99", 1))

    def test_new_acts_are_named(self):
        ctx, _ = self.step(self.HEAD.replace("N 2)", "N 2, от 03.03.2026 N 3)") + TEXT)
        self.assertEqual(ctx.facts["новые акты"], ["от 03.03.2026 N 3"])

    def test_older_document_is_a_rollback(self):
        old = self.HEAD.replace("N 2)", "N 2, от 03.03.2026 N 3)") + "текст"
        with self.assertRaisesRegex(ae.Stop, "откат"):
            self.step(self.HEAD + TEXT, old_text=old)

    def test_unparsed_header_stops(self):
        with self.assertRaisesRegex(ae.Stop, "шапка"):
            self.step(TEXT)


class TestPostStep(unittest.TestCase):
    def step(self, backfill="", cells=""):
        out = {"backfill_block_thresholds.py": backfill, "build_code_cells.py": cells}
        ctx = make_ctx()
        with mock.patch.object(ae, "py", side_effect=lambda s, *a: SimpleNamespace(stdout=out.get(s, ""))) as py:
            ae.step_post(ctx)
        return ctx, [c.args[0] for c in py.call_args_list]

    def test_order_from_backfill_output(self):
        _, calls = self.step()
        self.assertEqual(calls[0], "backfill_block_thresholds.py")
        self.assertEqual(calls[-1], "build_code_cells.py")
        self.assertEqual(calls.count("diag_orphan_requirements.py"), 2, "два режима — двумя запусками")
        self.assertIn("verify_structured.py", calls)

    def test_rollback_line_reaches_the_report(self):
        ctx, _ = self.step(backfill="  ⚠ ОТКАТ [VII] Имя: причина\nобычная строка")
        self.assertEqual(ctx.warnings, ["⚠ ОТКАТ [VII] Имя: причина"])

    def test_undefined_codes_are_counted(self):
        ctx, _ = self.step(cells="   НЕ ОПРЕДЕЛЕНО а\n   НЕ ОПРЕДЕЛЕНО б\n")
        self.assertEqual(ctx.facts["кодов не определено"], 2)


class TestWriteChunks(unittest.TestCase):
    def run_write(self, sections):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "chunks").mkdir()
            full, chunk = base / "full.txt", base / "chunks" / "00_a.txt"
            chunk.write_text("# Вводная часть\n\nпрежнее", encoding="utf-8")
            with mock.patch.object(ae, "FULL", full), mock.patch.object(ae, "CHUNKS", base / "chunks"), mock.patch.object(ae, "py"), \
                    mock.patch("parse_rtf.split_sections", return_value=sections):
                ae.write_chunks("текст")
            return full.read_text(encoding="utf-8"), chunk.read_text(encoding="utf-8")

    def test_chunk_is_header_and_body(self):
        full, chunk = self.run_write([("Вводная часть", "новое")])
        self.assertEqual(full, "текст")
        self.assertEqual(chunk, "# Вводная часть\n\nновое")

    def test_missing_or_ambiguous_header_stops(self):
        for sections in ([], [("Вводная часть", "а"), ("Вводная часть", "б")]):
            with self.assertRaisesRegex(ae.Stop, "заголовок"):
                self.run_write(sections)


class TestKbChanges(unittest.TestCase):
    def test_new_files_count(self):
        answers = {"diff": "knowledge_base/a\n", "ls-files": "knowledge_base/b\n"}
        with mock.patch.object(ae, "git", side_effect=lambda *a: answers[a[2]]):
            self.assertEqual(ae.kb_changes(), ["knowledge_base/a", "knowledge_base/b"])


class TestSaveState(unittest.TestCase):
    def test_interrupted_write_keeps_the_old_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            aa.save_state(path, {"a": 1})
            with mock.patch.object(aa.os, "replace", side_effect=OSError("обрыв")), self.assertRaises(OSError):
                aa.save_state(path, {"a": 2})
            self.assertEqual(aa.load_state(path), {"a": 1})


class TestControlStep(unittest.TestCase):
    def test_changed_chunks_roll_back_and_stop(self):
        with mock.patch.object(ae, "write_chunks"), mock.patch.object(ae, "FULL") as full, \
                mock.patch.object(ae, "kb_changes", return_value=["x.txt"]), mock.patch.object(ae, "git") as git:
            full.read_text.return_value = "t"
            with self.assertRaisesRegex(ae.Stop, "не воспроизводит"):
                ae.step_control(make_ctx())
        git.assert_called_once_with("checkout", "--", "knowledge_base")


class TestSections(unittest.TestCase):
    def test_new_section_stops(self):
        with self.assertRaisesRegex(ae.Stop, "новые \\['XXX'\\]"):
            ae.check_sections(["I", "XXX"], {"I"})

    def test_lost_section_stops(self):
        with self.assertRaisesRegex(ae.Stop, "пропавшие \\['II'\\]"):
            ae.check_sections(["I"], {"I", "II"})

    def test_same_sections_pass(self):
        ae.check_sections(["I", "II"], {"I", "II"})


class TestChunksStep(unittest.TestCase):
    """Защиты вызываются из шага: функции без вызова ничего не защищают."""

    def step(self, found=("I",), lines=3):
        ctx = make_ctx(new_text="новый", before={"разделы": {"I"}, "вне чанков": 3})
        full = mock.Mock()
        full.read_text.return_value = "старый"
        with mock.patch.object(ae, "FULL", full), mock.patch.object(ae, "write_chunks") as write, \
                mock.patch("rechunk_appendix.find_appendix_sections", return_value=[(r, "", "") for r in found]), \
                mock.patch.object(ae, "uncovered_lines", return_value=lines), \
                mock.patch.object(ae, "kb_changes", return_value=[]):
            ae.step_chunks(ctx)
        return write

    def test_clean_run(self):
        self.step().assert_called_once_with("новый")

    def test_new_section_stops_before_writing(self):
        with self.assertRaisesRegex(ae.Stop, "новые"):
            self.step(found=("I", "XXX"))

    def test_lost_lines_stop(self):
        with self.assertRaisesRegex(ae.Stop, "вне чанков"):
            self.step(lines=4)

    def test_same_text_is_no_change(self):
        ctx = make_ctx(new_text="один")
        full = mock.Mock()
        full.read_text.return_value = "один"
        with mock.patch.object(ae, "FULL", full), self.assertRaisesRegex(ae.Stop, "НЕТ ИЗМЕНЕНИЙ"):
            ae.step_chunks(ctx)


class TestRegressions(unittest.TestCase):
    def test_only_growth_counts(self):
        self.assertEqual(ae.regressions({"a": 1, "b": 2}, {"a": 1, "b": 1}), [])
        self.assertEqual(ae.regressions({"a": 1}, {"a": 2}), ["a: 1 → 2"])


class TestVerifySummary(unittest.TestCase):
    SECTION = "Потеряно полностью (нет нигде в JSON): НЕТ\nВыдуманные баллы (нет в законе): НЕТ"
    OK = (SimpleNamespace(stdout="ИТОГО: 0 расхождений на 9 позиций"), SimpleNamespace(stdout=SECTION),
          SimpleNamespace(stdout="ИТОГО чисто: 29/29"))

    def summary(self, section):
        with mock.patch.object(ae, "py", side_effect=(self.OK[0], SimpleNamespace(stdout=section), self.OK[2])):
            return ae.verify_summary()

    def test_numbers_from_output(self):
        self.assertEqual(set(self.summary(self.SECTION).values()), {0})

    def test_lost_points_counted(self):
        self.assertEqual(self.summary(self.SECTION.replace("JSON): НЕТ", "JSON): {12}"))["разделов с потерями"], 1)

    def test_fabricated_points_counted(self):
        self.assertEqual(self.summary(self.SECTION.replace("законе): НЕТ", "законе): [23.0]"))["разделов с выдуманными баллами"], 1)

    def test_changed_output_format_stops(self):
        for i in range(3):
            broken = list(self.OK)
            broken[i] = SimpleNamespace(stdout="другая печать")
            with mock.patch.object(ae, "py", side_effect=broken), self.assertRaisesRegex(ae.Stop, "не состоялась"):
                ae.verify_summary()


class TestPlanStep(unittest.TestCase):
    def ctx(self, d, llm=1):
        ctx = make_ctx(sections=["XXIII"])
        ctx.args.sources = d
        (Path(d) / "508574").mkdir()
        self.plan = {"режим": "renumber", "в модель": ["x"] * llm, "без модели": [], "исключены": []}
        return ctx

    def fake(self, ctx, keep_code=1):
        def run(script, roman, flag, mode, opt, path, check=True):
            if mode == "keep":
                return SimpleNamespace(returncode=keep_code)
            Path(path).write_text(json.dumps(self.plan), encoding="utf-8")
            return SimpleNamespace(returncode=0)
        return run

    def test_new_position_falls_back_to_renumber(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self.ctx(d)
            with mock.patch.object(ae, "py", side_effect=self.fake(ctx)):
                self.assertIn("renumber", ae.step_plan(ctx))
            self.assertEqual(ctx.modes, {"XXIII": "renumber"})

    def test_budget_stops(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self.ctx(d, llm=61)
            with mock.patch.object(ae, "py", side_effect=self.fake(ctx)), self.assertRaisesRegex(ae.Stop, "61 позиций"):
                ae.step_plan(ctx)

    def test_budget_edge_passes(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self.ctx(d, llm=60)
            with mock.patch.object(ae, "py", side_effect=self.fake(ctx)):
                ae.step_plan(ctx)

    def test_missing_plan_stops_instead_of_zero(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self.ctx(d)
            with mock.patch.object(ae, "py", return_value=SimpleNamespace(returncode=0)), \
                    self.assertRaisesRegex(ae.Stop, "план не записан"):
                ae.step_plan(ctx)


class TestGatesStep(unittest.TestCase):
    REC = {"product_name": "Магниты", "okpd2_codes": ["1"], "section_roman": "I"}
    CLEAN = {"расхождений в записях": 0, "разделов с потерями": 0, "разделов не чисто": 0}

    def step(self, after_recs=None, after_thr=None, summary=None, consistent=True, llm=(), gone=(), metho=None, modes=None):
        ctx = make_ctx(before={"пороги": {"a": "70"}, "записи": {"a": self.REC}, "сверки": self.CLEAN, "методички": {"I": "м"}},
                       plans={"I": {"в модель": list(llm), "без модели": [], "исключены": list(gone)}}, modes=modes or {})
        out = json.dumps({"корпус": {"части согласованы": consistent}})
        with mock.patch.object(ae, "threshold_snapshot", return_value=after_thr or {"a": "70"}), \
                mock.patch.object(ae, "records_by_anchor", return_value={"a": self.REC} if after_recs is None else after_recs), \
                mock.patch.object(ae, "verify_summary", return_value=summary or self.CLEAN), \
                mock.patch.object(ae, "py", return_value=SimpleNamespace(stdout=out)), \
                mock.patch.object(ae, "methodology_snapshot", return_value=metho or {"I": "м"}), \
                mock.patch.object(ae, "review_rows", return_value=(["строка"], ["замечание"])):
            return ctx, ae.step_gates(ctx)

    def test_clean_run_passes_and_keeps_notes(self):
        ctx, text = self.step()
        self.assertIn("чужих порогов 0", text)
        self.assertEqual(ctx.warnings, ["замечание"])

    def test_foreign_threshold_stops(self):
        with self.assertRaisesRegex(ae.Stop, "чужие пороги"):
            self.step(after_thr={"a": "115"})

    def test_unplanned_change_stops(self):
        with self.assertRaisesRegex(ae.Stop, "а в плане 0"):
            self.step(after_recs={"a": {**self.REC, "product_name": "Тарелки"}})

    def test_planned_change_passes(self):
        self.step(after_recs={"a": {**self.REC, "notes": "правка"}}, llm=["Магниты"])

    def test_change_of_another_record_stops(self):
        with self.assertRaisesRegex(ae.Stop, "вне плана"):
            self.step(after_recs={"a": {**self.REC, "notes": "правка"}}, llm=["Гармони"])

    def test_position_without_name_in_cell_skips_the_name_check(self):
        self.step(after_recs={"a": {**self.REC, "notes": "правка"}}, llm=[""])

    def test_empty_name_does_not_excuse_other_records(self):
        two = {"a": {**self.REC, "notes": "правка"}, "b": {**self.REC, "notes": "ещё"}}
        with self.assertRaisesRegex(ae.Stop, "вне плана"):
            self.step(after_recs=two, llm=["", "Гармони"])

    def test_unplanned_loss_stops(self):
        with self.assertRaisesRegex(ae.Stop, "записей было 1, стало 0"):
            self.step(after_recs={})

    def test_planned_loss_passes(self):
        self.step(after_recs={}, gone=["Магниты"])

    def test_verify_regression_stops(self):
        with self.assertRaisesRegex(ae.Stop, "сверки ухудшились"):
            self.step(summary={**self.CLEAN, "разделов с потерями": 1})

    def test_disagreement_stops(self):
        with self.assertRaisesRegex(ae.Stop, "не согласованы"):
            self.step(consistent=False)

    def test_methodology_change_is_reported(self):
        ctx, _ = self.step(metho={"I": "другая"})
        self.assertIn("методички разделов изменились: ['I']", ctx.warnings)

    def test_swap_of_records_seen_without_renumber(self):
        one, two = {**self.REC, "notes": "а"}, {**self.REC, "notes": "б"}
        self.assertEqual(ae.changed_anchors({"a": one, "b": two}, {"a": two, "b": one}), {"a", "b"})
        self.assertEqual(ae.changed_anchors({"a": one, "b": two}, {"a": two, "b": one}, frozenset({"I"})), set())


class TestTestsStep(unittest.TestCase):
    def test_green(self):
        with mock.patch.object(ae, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            self.assertIn("зелёная", ae.step_tests(make_ctx()))

    def red(self, *names):
        ctx = make_ctx()
        err = "".join(f"FAIL: t ({n})\n" for n in names)
        with mock.patch.object(ae, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr=err)) as run:
            ae.step_tests(ctx)
        return ctx, run

    def test_pinned_failure_is_expected(self):
        ctx, run = self.red("test_edition_2026_10_31.T.t", "tests.test_deploy_script.T.t",
                            "test_k15_metodrek_corpus.TestPassport.test_radius_editions_of_older_documents_unchanged")
        self.assertEqual(ctx.surprises, 0)
        self.assertEqual(ctx.warnings, [])
        self.assertEqual(len(ctx.facts["тесты, закреплённые за редакцией"]), 3)
        self.assertIs(run.call_args.kwargs["utf8"], False)

    def test_unexpected_failure_changes_the_verdict(self):
        ctx, _ = self.red("tests.test_rag.T.t", "test_edition_2026_10_31.T.t")
        self.assertEqual(ctx.surprises, 1)
        self.assertEqual(ctx.facts["неожиданные падения тестов"], ["tests.test_rag.T.t"])
        self.assertTrue(ae.verdict_of(None, "tests", ctx.warnings, ctx.surprises).startswith("НУЖЕН РАЗБОР"))

    def test_crash_without_names_stops(self):
        done = SimpleNamespace(returncode=3, stdout="", stderr="ImportError")
        with mock.patch.object(ae, "run", return_value=done), self.assertRaisesRegex(ae.Stop, "упала без списка"):
            ae.step_tests(make_ctx())


class TestRetargetLabel(unittest.TestCase):
    OLD, NEW = "ред. от 05.10.2026 N 1297", "ред. от 20.12.2026 N 1500"
    MANIFEST = (f'documents:\n  - doc_type: decree_body\n    edition_expected: "{OLD}"\n    sources:\n'
                f'      - pp719/chunks/a.txt\n  - doc_type: appendix_footnotes\n    edition_expected: "{OLD}"\n    sources:\n'
                '      - pp719/chunks/b.txt\n  - doc_type: rules\n    edition_expected: "ред. от 13.04.2026 N 400"\n    sources:\n'
                '      - pp719/chunks/c.txt\n  - doc_type: sng\n    edition_expected: "ред. от 08.06.2023"\n    sources:\n'
                '      - pp719/sng.txt\n')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        chunks = base / "knowledge_base" / "pp719" / "chunks"
        chunks.mkdir(parents=True)
        (chunks / "a.txt").write_text("в ред. от 20.12.2026 N 1500", encoding="utf-8")
        (chunks / "b.txt").write_text("в ред. от 05.10.2026 N 1297", encoding="utf-8")
        (chunks / "c.txt").write_text("в ред. от 20.12.2026 N 1500", encoding="utf-8")
        (chunks.parent / "sng.txt").write_text("ссылка от 18.06.2010 N 324", encoding="utf-8")
        self.man, self.land = base / "m.yaml", base / "i.html"
        self.man.write_text(self.MANIFEST, encoding="utf-8")
        self.land.write_text("ред. от 05.10.2026 № 1297<br>ред. от 05.10.2026</div>редакция от 05.10.2026 № 1297.", encoding="utf-8")
        self.patches = [mock.patch.object(ae, "MANIFEST", self.man), mock.patch.object(ae, "LANDING", self.land),
                        mock.patch.object(ae, "ROOT", base)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def text(self):
        return self.man.read_text(encoding="utf-8")

    def test_each_changed_document_gets_its_own_label(self):
        self.assertTrue(ae.retarget_manifest(self.NEW, ["knowledge_base/pp719/chunks/a.txt", "knowledge_base/pp719/chunks/b.txt"]))
        self.assertEqual(self.text().count(self.NEW), 1, "сноски акт не задел: их подпись остаётся")
        self.assertEqual(self.text().count(self.OLD), 1)

    def test_rules_follow_their_own_text(self):
        ae.retarget_manifest(self.NEW, ["knowledge_base/pp719/chunks/c.txt"])
        self.assertNotIn("N 400", self.text())

    def test_untouched_document_stays(self):
        self.assertFalse(ae.retarget_manifest(self.NEW, []))
        self.assertEqual(self.text(), self.MANIFEST)

    def test_document_outside_chunks_stays_even_if_listed(self):
        ae.retarget_manifest(self.NEW, ["knowledge_base/pp719/sng.txt"])
        self.assertIn("ред. от 08.06.2023", self.text())

    def test_landing_follows_the_label(self):
        self.assertTrue(ae.retarget_landing(self.OLD, self.NEW))
        self.assertNotIn("05.10.2026", self.land.read_text(encoding="utf-8"))

    def test_unparsed_label_stops(self):
        with self.assertRaises(ae.Stop):
            ae.retarget_landing("редакция не определена", self.NEW)

    def test_real_manifest_with_glob_sources(self):
        # Настоящий манифест: выдуманная фикстура скрыла источник-маску, и реплей N 1297 упал на шаге ссылки.
        for p in self.patches:
            p.stop()
        real = ROOT / "knowledge_base" / "manifest.yaml"
        self.man.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch.object(ae, "MANIFEST", self.man):
            self.assertTrue(ae.retarget_manifest(self.NEW, []))
        self.assertEqual(self.text().count(f'edition_expected: "{self.NEW}"'), 1, "товарный корпус с маской идёт за редакцией")
        for p in self.patches:
            p.start()

    def test_real_manifest_labels_match_the_text(self):
        # Положительный контроль: на нынешнем корпусе пересчёт не меняет ни одной подписи.
        for p in self.patches:
            p.stop()
        real = ROOT / "knowledge_base" / "manifest.yaml"
        self.man.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
        chunks = sorted(f"knowledge_base/pp719/chunks/{x.name}" for x in ae.CHUNKS.glob("*.txt"))
        from app.rag import edition

        with mock.patch.object(ae, "MANIFEST", self.man):
            self.assertFalse(ae.retarget_manifest(edition.corpus_edition(), chunks))
        for p in self.patches:
            p.start()

    def step(self, label, known, before=None):
        ctx = make_ctx(before={"подпись": before or self.OLD})
        with mock.patch.object(ae, "corpus_label", return_value=(label, label, known)), \
                mock.patch.object(ae, "kb_changes", return_value=["knowledge_base/pp719/chunks/a.txt"]):
            return ae.step_link(ctx)

    def test_link_step_moves_manifest_and_landing(self):
        text = self.step(self.NEW, "508574")
        self.assertIn("m.yaml", text)
        self.assertIn("i.html", text)

    def test_link_step_leaves_landing_when_label_is_the_same(self):
        self.assertNotIn("i.html", self.step(self.NEW, "508574", before=self.NEW))

    def test_link_step_stops_on_foreign_document(self):
        with self.assertRaisesRegex(ae.Stop, "LATER_IN_FORCE"):
            self.step(self.NEW, "111111", before=self.NEW)


class TestVerdict(unittest.TestCase):
    def test_partial_run_is_not_ready(self):
        self.assertIn("ЧАСТИЧНЫЙ", ae.verdict_of(None, "guard", []))

    def test_stop_wins(self):
        self.assertTrue(ae.verdict_of("gates: x", "gates", [], 3).startswith("ОСТАНОВЛЕНО"))

    def test_surprises_are_not_ready(self):
        self.assertTrue(ae.verdict_of(None, "tests", ["a"], 2).startswith("НУЖЕН РАЗБОР"))

    def test_warnings_are_counted(self):
        self.assertIn("ЗАМЕЧАНИЙ: 2", ae.verdict_of(None, "tests", ["a", "b"]))

    def test_clean(self):
        self.assertEqual(ae.verdict_of(None, "tests", []), "ГОТОВО К ПРОВЕРКЕ ЧЕЛОВЕКОМ")


class TestPipelineMain(unittest.TestCase):
    def run_main(self, handlers, until="tests"):
        with tempfile.TemporaryDirectory() as d:
            argv = ["x", "--doc-id", "1", "--sources", d, "--until", until]
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(ae.HANDLERS, handlers), \
                    mock.patch.object(ae, "enable_utf8"), self.assertRaises(SystemExit) as code:
                ae.main()
            return code.exception.code, json.loads((Path(d) / "1" / "report.json").read_text(encoding="utf-8"))

    def ok(self):
        return {name: (lambda ctx: "ok") for name in ae.STEPS}

    def test_all_steps_ok(self):
        code, report = self.run_main(self.ok())
        self.assertEqual((code, report["вердикт"]), (0, "ГОТОВО К ПРОВЕРКЕ ЧЕЛОВЕКОМ"))

    def test_crash_leaves_report(self):
        def boom(ctx):
            raise subprocess.TimeoutExpired("cmd", 1)

        code, report = self.run_main({**self.ok(), "structure": boom})
        self.assertEqual(code, 1)
        self.assertIn("TimeoutExpired", report["вердикт"])
        self.assertEqual(report["последний шаг"], "structure")

    def test_stop_ends_the_run(self):
        calls = []

        def stop(ctx):
            raise ae.Stop("причина")

        code, report = self.run_main({**self.ok(), "text": stop, "chunks": lambda ctx: calls.append(1)})
        self.assertEqual((code, calls), (1, []))

    def test_old_reports_are_gone_when_run_is_interrupted(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "1"
            src.mkdir()
            (src / "report.json").write_text("{}", encoding="utf-8")
            (src / "report.md").write_text("старый", encoding="utf-8")
            ae.clear_report(d, "1")
            self.assertEqual(list(src.iterdir()), [])
            ae.clear_report(d, "нет такого")

    def test_main_clears_report_before_the_first_step(self):
        cleared = []

        def guard(ctx):
            self.assertEqual(cleared, ["1"])
            return "ok"

        with mock.patch.object(ae, "clear_report", side_effect=lambda sources, doc: cleared.append(doc)):
            self.run_main({**self.ok(), "guard": guard})
        self.assertEqual(cleared, ["1"])

    def test_partial_run_is_marked(self):
        code, report = self.run_main(self.ok(), until="guard")
        self.assertEqual(code, 0)
        self.assertIn("ЧАСТИЧНЫЙ", report["вердикт"])


class TestReport(unittest.TestCase):
    def test_report_names_what_stays_with_human(self):
        steps = [{"шаг": "text", "итог": "стоп", "деталь": "таблица | изменилась\nподробно"}]
        text = ae.render_report("509000", "ОСТАНОВЛЕНО: text", steps, {"разделы": ["IV"]}, ["замечание"])
        self.assertIn("ОСТАНОВЛЕНО: text", text)
        self.assertIn("таблица \\| изменилась подробно", text)
        self.assertIn("Что остаётся человеку", text)
        self.assertLess(text.index("замечание"), text.index("| Шаг |"))

    def test_positions_are_named(self):
        facts = {"изменённые записи": ["| Иглы | XXIII | 16 | 145 / 145 | 1 / 1 | 115 |"], "исключённые позиции": ["Прежняя"]}
        text = ae.render_report("1", "x", [], facts)
        self.assertIn("| Иглы |", text)
        self.assertIn("Прежняя", text)

    def test_act_check_is_unconditional_and_names_acts(self):
        text = ae.render_report("509000", "x", [], {"новые акты": ["от 01.12.2026 N 1500"]})
        self.assertIn("Сверить изменённые и исключённые позиции с текстом акта: от 01.12.2026 N 1500", text)
        self.assertIn("509000-postanovlenie", text)
        self.assertNotIn("При расхождении", text)


class TestCheckKontur(unittest.TestCase):
    def check(self, page, known=None, doc_id="508574", text=""):
        return we.check_kontur(doc_id, KNOWN if known is None else known,
                               fetch=lambda url: (page.encode("utf-8"), ""), full_text=text, today=TODAY)

    def test_nothing_new(self):
        self.assertEqual(self.check(ACTUAL_PAGE)["исход"], "нового нет")

    def test_new_document(self):
        r = self.check(OUTDATED_PAGE, known={}, doc_id="508767")
        self.assertEqual((r["исход"], r["новые документы"]), ("новое", {"508574": "Перейти в действующую"}))

    def test_corpus_on_future_edition_is_not_a_rollback(self):
        # Настоящая разметка: на будущей редакции «Перейти в действующую» ведёт НАЗАД.
        page = FUTURE.replace("</b></div>", "</b> <a class='js-actualDocLink' href='/info/normativ/document/1/508767-x-n-719'>"
                              "Перейти в действующую</a></div>") + DOC.format("текст")
        r = self.check(page, known={})
        self.assertEqual((r["исход"], r["новые документы"]), ("нового нет", {}))

    def test_future_page_pointing_to_unknown_document_is_news(self):
        page = FUTURE.replace("</b></div>", "</b> <a href='/info/normativ/document/1/509999-x-n-719'>Перейти в редакцию</a></div>") + DOC.format("т")
        self.assertEqual(self.check(page, known={})["новые документы"], {"509999": "Перейти в редакцию"})

    def test_new_pending_act(self):
        r = self.check(ACTUAL_PAGE, known={"N 1284 от 27.08.2025": "01.06.2027"})
        self.assertEqual(r["новые отложенные акты"], ["N 1353 от 15.10.2026 (с 01.01.2027)"])

    def test_shifted_date_is_news(self):
        r = self.check(ACTUAL_PAGE, known={**KNOWN, "N 1353 от 15.10.2026": "01.07.2026"})
        self.assertEqual(r["исход"], "новое")
        self.assertEqual(r["сроки изменились"], ["N 1353 от 15.10.2026: было 01.07.2026, стало 01.01.2027"])

    def test_act_absent_from_corpus(self):
        page = ACTUAL + DOC.format("в ред. Постановления от 01.11.2026 N 1500")
        self.assertEqual(self.check(page)["актов нет в тексте"], ["от 01.11.2026 N 1500"])

    def test_not_checked_is_a_third_outcome(self):
        net = we.check_kontur("508574", {}, fetch=lambda url: (None, "URLError"), full_text="")
        self.assertEqual(net["исход"], "не проверено")
        self.assertEqual(self.check("<html>капча</html>")["исход"], "не проверено")
        stale = OUTDATED_PAGE.replace("/document/1/508574-", "/other/")
        self.assertEqual(self.check(stale, known={}, doc_id="508767")["исход"], "не проверено")

    def test_renamed_document_container_is_not_new_acts(self):
        r = self.check(ACTUAL)
        self.assertEqual(r["исход"], "не проверено")
        self.assertIn("разметка", r["причина"])

    def test_vanished_future_act_is_not_calm(self):
        r = self.check(ACTUAL_PAGE.replace("Примечания", "Сведения"))
        self.assertEqual(r["исход"], "не проверено")
        self.assertIn("исчезли", r["причина"])

    def test_act_already_in_corpus_may_vanish(self):
        known = {**KNOWN, "N 1257 от 29.09.2026": "31.12.2026"}
        self.assertEqual(self.check(ACTUAL_PAGE, known=known, text="в ред. от 29.09.2026 N 1257")["исход"], "нового нет")

    def test_new_document_outranks_vanished_acts(self):
        r = self.check(OUTDATED_PAGE, known={"N 1353 от 15.10.2026": "01.01.2027"}, doc_id="508767")
        self.assertEqual(r["новые документы"], {"508574": "Перейти в действующую"})

    def test_act_that_took_effect_may_leave_the_list(self):
        known = {**KNOWN, "N 1257 от 29.09.2026": "31.10.2026"}
        self.assertEqual(self.check(ACTUAL_PAGE, known=known)["исход"], "нового нет")

    def test_known_pending_file_format(self):
        known = we.known_pending()
        self.assertTrue(known)
        for key, when in known.items():
            self.assertRegex(key, r"^N \d+ от \d{2}\.\d{2}\.\d{4}$")
            self.assertRegex(when, r"^\d{2}\.\d{2}\.\d{4}$")


def answer(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class TestIssues(unittest.TestCase):
    def test_existing_title_not_created(self):
        runner = mock.Mock(return_value=answer(out='[{"number": 1}]'))
        self.assertIn("уже заведено", aa.Issues(False, runner).once("t", "b"))
        self.assertEqual(runner.call_count, 1)

    def test_new_title_created_and_counted(self):
        runner = mock.Mock(side_effect=[answer(out="[]"), answer(out="https://x/1\n")])
        issues = aa.Issues(False, runner)
        self.assertEqual(issues.once("t", "b"), "https://x/1")
        self.assertEqual(issues.made, 1)

    def test_search_failure_creates_nothing(self):
        runner = mock.Mock(return_value=answer(code=1, err="TLS handshake timeout"))
        self.assertIn("повтор завтра", aa.Issues(False, runner).once("t", "b"))
        self.assertEqual(runner.call_count, 1)

    def test_limit_per_run(self):
        runner = mock.Mock()
        issues = aa.Issues(False, runner, limit=0)
        self.assertIn("потолок", issues.once("t", "b"))
        runner.assert_not_called()

    def test_limit_counts_created(self):
        runner = mock.Mock(side_effect=[answer(out="[]"), answer(out="u\n")])
        issues = aa.Issues(False, runner, limit=1)
        issues.once("a", "b")
        self.assertIn("потолок", issues.once("c", "d"))

    def test_urgent_ignores_limit(self):
        runner = mock.Mock(side_effect=[answer(out="[]"), answer(out="u\n")])
        self.assertEqual(aa.Issues(False, runner, limit=0).once("t", "b", urgent=True), "u")

    def test_reopen_looks_at_open_only(self):
        runner = mock.Mock(side_effect=[answer(out="[]"), answer(out="u\n")])
        aa.Issues(False, runner).once("t", "b", reopen=True)
        self.assertEqual(runner.call_args_list[0].args[3], "open")

    def test_dry_run_touches_nothing(self):
        runner = mock.Mock()
        self.assertIn("была бы создана", aa.Issues(True, runner).once("t", "b"))
        runner.assert_not_called()


class TestHandleDocument(unittest.TestCase):
    STOPPED = (False, {"последний шаг": "gates", "вердикт": "ОСТАНОВЛЕНО: input_value='секрет'"})

    def run_doc(self, built=False, result=(True, {}), opened=False, publish=None, fetched=None):
        issues = mock.Mock(dry_run=False)
        with mock.patch.object(aa, "edition_built", return_value=built), \
                mock.patch.object(aa, "read_report", return_value=result) as read, \
                mock.patch.object(aa, "build_edition", return_value=result) as build, \
                mock.patch.object(aa, "pr_opened", return_value=opened), \
                mock.patch.object(aa, "prefetch", return_value=fetched) as prefetch, \
                mock.patch.object(aa, "publish", **(publish or {"return_value": "https://x/pr/1"})) as pub:
            log = aa.handle_document("509000", "ссылка", Path("."), issues)
        return SimpleNamespace(log=log, issues=issues, build=build, read=read, pub=pub, prefetch=prefetch)

    def test_new_edition_is_built_and_published(self):
        r = self.run_doc()
        r.prefetch.assert_called_once()
        r.build.assert_called_once()
        self.assertEqual(r.log, "https://x/pr/1")

    def test_existing_build_is_not_rebuilt(self):
        r = self.run_doc(built=True, opened=True)
        r.build.assert_not_called()
        r.prefetch.assert_not_called()
        r.pub.assert_not_called()
        self.assertIn("PR уже открыт", r.log)

    def test_published_build_without_pr_is_retried(self):
        r = self.run_doc(built=True, opened=False)
        r.pub.assert_called_once()

    def test_github_silence_retries_tomorrow(self):
        r = self.run_doc(built=True, opened=None)
        r.pub.assert_not_called()
        r.issues.once.assert_not_called()
        self.assertIn("повтор завтра", r.log)

    def test_stopped_pipeline_makes_short_urgent_issue_not_pr(self):
        r = self.run_doc(result=self.STOPPED)
        r.pub.assert_not_called()
        title, body = r.issues.once.call_args.args
        self.assertIn("gates", title)
        self.assertNotIn("секрет", title + body)
        self.assertTrue(r.issues.once.call_args.kwargs["urgent"])
        self.assertTrue(r.issues.once.call_args.kwargs["reopen"], "закрытая задача не глушит новую остановку")

    def test_stop_is_repeated_next_day(self):
        r = self.run_doc(built=True, result=self.STOPPED)
        r.build.assert_not_called()
        r.issues.once.assert_called_once()

    def test_download_failure_builds_nothing_and_speaks_up(self):
        r = self.run_doc(fetched="URLError: секретный хвост")
        r.build.assert_not_called()
        title, body = r.issues.once.call_args.args
        self.assertIn("не скачивается", title)
        self.assertNotIn("секретный", title + body)
        self.assertTrue(r.issues.once.call_args.kwargs["urgent"])
        self.assertTrue(r.issues.once.call_args.kwargs["reopen"], "закрытая задача не глушит непрекращающийся отказ")

    def test_publish_failure_makes_urgent_issue(self):
        r = self.run_doc(publish={"side_effect": subprocess.CalledProcessError(1, "git push")})
        self.assertIn("PR не создан", r.issues.once.call_args.args[0])
        self.assertTrue(r.issues.once.call_args.kwargs["urgent"])
        self.assertTrue(r.issues.once.call_args.kwargs["reopen"])

    def test_human_commits_on_the_branch_make_issue(self):
        r = self.run_doc(publish={"side_effect": aa.HumanCommits("на ветке коммиты человека")})
        self.assertIn("PR не создан", r.issues.once.call_args.args[0])
        self.assertIn("HumanCommits", r.issues.once.call_args.args[1])

    def test_gh_pr_failure_makes_urgent_issue(self):
        r = self.run_doc(publish={"side_effect": RuntimeError("gh pr create")})
        self.assertIn("PR не создан", r.issues.once.call_args.args[0])

    def test_missing_tree_makes_issue(self):
        r = self.run_doc(publish={"side_effect": NotADirectoryError("нет каталога")})
        self.assertIn("PR не создан", r.issues.once.call_args.args[0])

    def test_nothing_in_the_module_deletes(self):
        src = Path(aa.__file__).read_text(encoding="utf-8")
        for word in ("rmtree", '"-D"', "worktree\", \"remove", "--force"):
            self.assertNotIn(word, src, "автомат ничего не удаляет: уборка уже однажды сносила работу человека")


class TestHandleFound(unittest.TestCase):
    EMPTY = {"новые документы": {}, "новые отложенные акты": [], "сроки изменились": [], "актов нет в тексте": []}

    def test_dry_run_builds_nothing(self):
        result = {**self.EMPTY, "новые отложенные акты": ["N 1 от 01.01.2027"], "новые документы": {"509000": "x"}}
        with mock.patch.object(aa, "build_edition") as build, mock.patch.object(aa, "edition_built") as built:
            log = aa.handle_found(result, Path("."), aa.Issues(True, mock.Mock()))
        build.assert_not_called()
        built.assert_not_called()
        self.assertEqual(len(log), 2)

    def test_document_goes_first(self):
        order = []
        result = {**self.EMPTY, "новые отложенные акты": ["акт"], "новые документы": {"509000": "x"}}
        issues = mock.Mock(once=lambda *a, **k: order.append("акт"))
        with mock.patch.object(aa, "handle_document", side_effect=lambda *a: order.append("документ")):
            aa.handle_found(result, Path("."), issues)
        self.assertEqual(order, ["документ", "акт"])

    def test_every_kind_of_news_reaches_an_issue(self):
        result = {**self.EMPTY, "новые отложенные акты": ["а"], "сроки изменились": ["б"], "актов нет в тексте": ["в"]}
        issues = mock.Mock()
        aa.handle_found(result, Path("."), issues)
        self.assertEqual(issues.once.call_count, 3)


class TestRunOnce(unittest.TestCase):
    LAST = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc).isoformat()

    def run_with(self, result, state, moment=None):
        issues = mock.Mock(dry_run=False)
        with mock.patch.object(aa.we, "check_kontur", return_value=result), \
                mock.patch.object(aa.we, "current_doc_id", return_value="1"), \
                mock.patch.object(aa.we, "known_pending", return_value={}), \
                mock.patch.object(aa, "now", return_value=moment or dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)), \
                mock.patch.object(aa, "handle_found", return_value=[]) as found:
            aa.run_once(Path("."), state, issues)
        return issues, found

    def test_success_moves_the_date(self):
        state = {"последняя успешная проверка": self.LAST}
        _, found = self.run_with({"исход": "нового нет"}, state)
        self.assertIn("2026-11-10", state["последняя успешная проверка"])
        found.assert_not_called()

    def test_new_goes_to_handler(self):
        _, found = self.run_with({"исход": "новое"}, {})
        found.assert_called_once()

    def test_week_of_silence_makes_urgent_issue(self):
        issues, _ = self.run_with({"исход": "не проверено", "причина": "сеть"}, {"последняя успешная проверка": self.LAST})
        self.assertIn("молчит", issues.once.call_args.args[0])
        self.assertTrue(issues.once.call_args.kwargs["urgent"])
        self.assertTrue(issues.once.call_args.kwargs["reopen"])

    def test_short_silence_is_quiet(self):
        last = dt.datetime(2026, 11, 8, tzinfo=dt.timezone.utc).isoformat()
        issues, _ = self.run_with({"исход": "не проверено", "причина": "сеть"}, {"последняя успешная проверка": last})
        issues.once.assert_not_called()

    def test_first_failure_starts_the_clock(self):
        state: dict = {}
        self.run_with({"исход": "не проверено", "причина": "сеть"}, state)
        self.assertIn("последняя успешная проверка", state)


class TestDailyMain(unittest.TestCase):
    def test_crash_makes_issue_and_keeps_state(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["x", "--sources", d]

            def boom(sources, state, issues):
                state["последняя успешная проверка"] = "дата"
                raise KeyError("секрет в тексте")

            with mock.patch.object(sys, "argv", argv), mock.patch.object(aa, "enable_utf8"), \
                    mock.patch.object(aa, "run_once", side_effect=boom), \
                    mock.patch.object(aa.Issues, "once", return_value="ok") as once, \
                    mock.patch("traceback.print_exc"):
                aa.main()
            state = json.loads((Path(d) / "watch_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["последняя успешная проверка"], "дата")
            title, body = once.call_args.args
            self.assertIn("KeyError", title)
            self.assertNotIn("секрет", title + body)
            self.assertTrue(once.call_args.kwargs["reopen"])

    def test_missing_gh_does_not_lose_state(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "argv", ["x", "--sources", d]), mock.patch.object(aa, "enable_utf8"), \
                    mock.patch.object(aa, "run_once", side_effect=KeyError("x")), \
                    mock.patch.object(aa.Issues, "once", side_effect=FileNotFoundError("gh")), \
                    mock.patch("traceback.print_exc"):
                aa.main()
            self.assertTrue((Path(d) / "watch_state.json").exists())

    def test_broken_state_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "watch_state.json"
            path.write_text("{обрыв", encoding="utf-8")
            self.assertEqual(aa.load_state(path), {})

    def test_dry_run_writes_no_state(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "argv", ["x", "--sources", d, "--dry-run"]), mock.patch.object(aa, "enable_utf8"), \
                    mock.patch.object(aa, "run_once"):
                aa.main()
            self.assertFalse((Path(d) / "watch_state.json").exists())


class TestBuild(unittest.TestCase):
    def test_built_when_only_remote_branch_exists(self):
        calls = []

        def git(*args, **kw):
            calls.append(args[0])
            return "abc refs/heads/auto/edition-1\n" if args[0] == "ls-remote" else ""

        with tempfile.TemporaryDirectory() as d, mock.patch.object(aa, "git", side_effect=git):
            self.assertTrue(aa.edition_built("1", Path(d)))
        self.assertEqual(calls, ["branch", "ls-remote"])

    def test_not_built(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(aa, "git", return_value=""):
            self.assertFalse(aa.edition_built("1", Path(d)))

    def test_built_from_fresh_dev_with_the_key_only(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(aa, "git") as git, \
                mock.patch.object(aa.subprocess, "run") as run, \
                mock.patch.object(aa, "model_env", return_value={"DEEPSEEK_API_KEY": "k"}):
            aa.build_edition("1", Path(d))
        self.assertEqual(git.call_args_list[0].args[:2], ("fetch", "origin"))
        self.assertEqual(git.call_args_list[1].args[-1], "origin/dev")
        self.assertEqual(run.call_args.kwargs["env"], {"DEEPSEEK_API_KEY": "k"})

    def report(self, base, **data):
        (base / "1").mkdir(exist_ok=True)
        (base / "1" / "report.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    GOOD = {"последний шаг": "tests", "вердикт": "ГОТОВО", "факты": {"ветка": "auto/edition-1"}}

    def test_only_a_full_run_on_its_own_branch_counts(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            self.assertEqual(aa.read_report("1", base), (False, {}))
            for data, ok in ((self.GOOD, True), ({**self.GOOD, "вердикт": "НУЖЕН РАЗБОР: 1"}, True),
                             ({**self.GOOD, "вердикт": "ОСТАНОВЛЕНО: x"}, False),
                             ({**self.GOOD, "последний шаг": "chunks"}, False),
                             ({**self.GOOD, "факты": {"ветка": "другая"}}, False)):
                self.report(base, **data)
                self.assertEqual(aa.read_report("1", base)[0], ok, data)

    def test_crashed_pipeline_does_not_read_an_old_report(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            self.report(base, **self.GOOD)
            with mock.patch.object(aa, "git"), mock.patch.object(aa.subprocess, "run", return_value=answer(code=1)), \
                    mock.patch.object(aa, "model_env", return_value={}):
                self.assertEqual(aa.build_edition("1", base), (False, {}))

    def test_failed_exit_code_is_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)

            def run(*a, **k):
                self.report(base, **self.GOOD)
                return answer(code=1)

            with mock.patch.object(aa, "git"), mock.patch.object(aa.subprocess, "run", side_effect=run), \
                    mock.patch.object(aa, "model_env", return_value={}):
                self.assertFalse(aa.build_edition("1", base)[0])

    def test_pipeline_gets_the_downloaded_files(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(aa, "git"), \
                mock.patch.object(aa.subprocess, "run", return_value=answer()) as run, \
                mock.patch.object(aa, "model_env", return_value={}):
            aa.build_edition("1", Path(d))
        cmd = run.call_args.args[0]
        self.assertTrue(cmd[cmd.index("--rtf") + 1].endswith("edition.rtf"))
        self.assertTrue(cmd[cmd.index("--page") + 1].endswith("edition.html"))

    def test_prefetch(self):
        with tempfile.TemporaryDirectory() as d:
            real = [(b"{\\rtf1 x", ""), (b'<div id="js-doc-frame-container">', "")]
            with mock.patch.object(aa.es, "fetch", side_effect=real):
                self.assertIsNone(aa.prefetch("1", Path(d)))
            self.assertTrue((Path(d) / "1" / "edition.rtf").exists())
            self.assertTrue((Path(d) / "1" / "edition.html").exists())
            with mock.patch.object(aa.es, "fetch", return_value=(None, "timed out")):
                self.assertEqual(aa.prefetch("2", Path(d)), "timed out")

    def test_prefetch_rejects_answers_that_are_not_documents(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(aa.es, "fetch", return_value=(b"", "")):
                self.assertEqual(aa.prefetch("1", Path(d)), "пустой ответ")
            with mock.patch.object(aa.es, "fetch", return_value=(b"<html>sorry</html>", "")):
                self.assertIn("edition.rtf", aa.prefetch("1", Path(d)))
            page = [(b"{\\rtf1 x", ""), (b"<html>sorry</html>", "")]
            with mock.patch.object(aa.es, "fetch", side_effect=page):
                self.assertIn("edition.html", aa.prefetch("1", Path(d)))

    def pr(self, prs, tip):
        runner = mock.Mock(return_value=answer(out=json.dumps(prs)))
        with mock.patch.object(aa, "git", return_value=tip):
            return aa.pr_opened("1", runner)

    def test_closed_pr_is_matched_by_commit(self):
        self.assertTrue(self.pr([{"headRefOid": "abc", "state": "CLOSED"}], "abc\n"))
        self.assertFalse(self.pr([{"headRefOid": "old", "state": "CLOSED"}], "abc\n"), "PR прошлой сборки не глушит пересборку")
        self.assertFalse(self.pr([{"headRefOid": "old", "state": "MERGED"}], "abc\n"))
        self.assertFalse(self.pr([], "abc\n"))

    def test_open_pr_stands_even_if_the_branch_moved(self):
        self.assertTrue(self.pr([{"headRefOid": "old", "state": "OPEN"}], "abc\n"), "PR правит человек")
        self.assertTrue(self.pr([{"headRefOid": "old", "state": "CLOSED"}, {"headRefOid": "x", "state": "OPEN"}], "abc\n"))

    def test_github_is_asked_for_the_state(self):
        runner = mock.Mock(return_value=answer(out="[]"))
        with mock.patch.object(aa, "git", return_value=""):
            aa.pr_opened("1", runner)
        self.assertIn("headRefOid,state", runner.call_args.args)

    def test_pr_without_local_branch(self):
        self.assertTrue(self.pr([{"headRefOid": "abc", "state": "CLOSED"}], ""))

    def test_pr_silence(self):
        self.assertIsNone(aa.pr_opened("1", mock.Mock(return_value=answer(code=1))))


class TestModelEnv(unittest.TestCase):
    def test_only_the_key_is_taken(self):
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            env_file.write_text('OTHER=1\nDEEPSEEK_API_KEY="abc"\nSESSION_SECRET=zz\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                env = aa.model_env(env_file)
            self.assertEqual(env, {"DEEPSEEK_API_KEY": "abc"})


class TestPublish(unittest.TestCase):
    """Настоящий git на временных репозиториях: `gh` подменён, остальное живое."""

    def git(self, *args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.remote, self.tree = base / "remote.git", base / "tree"
        self.git("init", "--bare", "-q", str(self.remote), cwd=base)
        self.git("clone", "-q", str(self.remote), str(self.tree), cwd=base)
        for key, value in (("user.email", "t@example.invalid"), ("user.name", "t"), ("core.hooksPath", "/dev/null")):
            self.git("config", key, value, cwd=self.tree)
        for name in ("knowledge_base/a.txt", "app/rag/edition.py", "landing/index.html"):
            (self.tree / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tree / name).write_text("1", encoding="utf-8")
        self.git("add", ".", cwd=self.tree)
        self.git("commit", "-q", "-m", "база", cwd=self.tree)
        self.git("push", "-q", "origin", "HEAD:dev", cwd=self.tree)
        self.git("fetch", "-q", "origin", cwd=self.tree)
        self.git("checkout", "-q", "-b", "auto/edition-1", cwd=self.tree)
        (self.tree / "knowledge_base" / "a.txt").write_text("2", encoding="utf-8")
        (self.tree / "stray.txt").write_text("чужое", encoding="utf-8")
        self.report = base / "report.md"
        self.report.write_text("отчёт", encoding="utf-8")
        self.real_run = subprocess.run

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, gh_code=0):
        calls = []

        def run(cmd, **kw):
            if cmd[0] == "gh":
                calls.append(cmd)
                return answer(code=gh_code, out="https://x/pr/1\n")
            return self.real_run(cmd, **kw)

        with mock.patch.object(aa.subprocess, "run", side_effect=run):
            return aa.publish("1", self.tree, self.report), calls

    def test_draft_pr_with_only_allowed_paths(self):
        out, calls = self.publish()
        self.assertEqual(out, "https://x/pr/1")
        self.assertIn("--draft", calls[0])
        self.assertEqual(calls[0][calls[0].index("--base") + 1], "dev")
        shown = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.tree,
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(shown, ["knowledge_base/a.txt"])
        pushed = subprocess.run(["git", "ls-remote", "--heads", str(self.remote), "auto/edition-1"],
                                capture_output=True, text=True).stdout
        self.assertIn("auto/edition-1", pushed)

    def test_gh_failure_raises(self):
        with self.assertRaises(RuntimeError):
            self.publish(gh_code=1)

    def test_retry_after_failed_pr_does_not_need_a_new_commit(self):
        with self.assertRaises(RuntimeError):
            self.publish(gh_code=1)
        out, _ = self.publish()
        self.assertEqual(out, "https://x/pr/1")

    def remote_tip(self):
        return subprocess.run(["git", "ls-remote", "--heads", str(self.remote), "auto/edition-1"],
                              capture_output=True, text=True).stdout.split()[0]

    def test_commit_of_a_human_is_never_published(self):
        self.publish()
        pushed = self.remote_tip()
        (self.tree / "knowledge_base" / "a.txt").write_text("правка человека", encoding="utf-8")
        self.git("add", ".", cwd=self.tree)
        self.git("commit", "-q", "-m", "WIP человека", cwd=self.tree)
        with self.assertRaises(aa.HumanCommits):
            self.publish()
        self.assertEqual(self.remote_tip(), pushed)

    def test_uncommitted_edit_after_our_commit_stays_local(self):
        with self.assertRaises(RuntimeError):
            self.publish(gh_code=1)
        (self.tree / "knowledge_base" / "a.txt").write_text("правка человека", encoding="utf-8")
        self.publish()
        shown = subprocess.run(["git", "show", "origin/auto/edition-1:knowledge_base/a.txt"], cwd=self.tree,
                               capture_output=True, text=True, encoding="utf-8").stdout
        self.assertEqual(shown, "2")
        self.assertIn("правка человека", (self.tree / "knowledge_base" / "a.txt").read_text(encoding="utf-8"))


class TestParserPrompt(unittest.TestCase):
    def test_mandatory_requirement_goes_to_a_block(self):
        self.assertIn('"component": "Общие требования"', KB_PARSER_SYSTEM_PROMPT)
        self.assertIn('В "notes" их', KB_PARSER_SYSTEM_PROMPT)


class TestRestructurePlan(unittest.TestCase):
    def test_plan_file(self):
        import restructure_changed as rc

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p.json"
            rc.write_plan(str(path), "keep", ["а"], ["б"], ["в"], ["г"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {"режим": "keep", "в модель": ["а"], "без модели": ["б"], "исключены": ["в"], "прежние имена": ["г"]})
            rc.write_plan(None, "keep", [], [], [])


class TestRestructureNames(unittest.TestCase):
    """Имя записи из модели заменяется ячейкой источника, и шаг это делает, а не только функция умеет."""

    def product(self):
        import structure_kb as sk

        return sk.Product(okpd2_field="1", name_raw="Иглы из ячейки", okpd2_codes=["1"], component_cells=["x"])

    def fresh(self, *a, **k):
        return {"product_name": "Иглы, пересказ модели", "source_anchor": "", "okpd2_codes": ["1"]}

    def run_patch(self, name_raw=None):
        import restructure_changed as rc

        p = self.product()
        if name_raw is not None:
            p.name_raw = name_raw
        old = [{"product_name": "Было", "source_anchor": "XXIII, позиция 1", "okpd2_codes": ["1"]},
               {"record_type": "section_methodology", "text": "прежняя"}]
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x.json"
            chunk = mock.Mock(stem="x")
            chunk.read_text.return_value = "текст"
            with mock.patch.object(rc.sk, "find_chunk", return_value=chunk), \
                    mock.patch.object(rc.sk, "OUT_DIR", Path(d)), mock.patch.object(rc.sk, "out_name", return_value="x.json"), \
                    mock.patch.object(rc, "git_text", return_value=json.dumps(old)), \
                    mock.patch.object(rc.sk, "parse_section", return_value=("шапка", [p])), \
                    mock.patch.object(rc.sk, "section_title", return_value="Раздел"), \
                    mock.patch.object(rc, "patch_jobs", return_value=([(0, p, "llm")], [])), \
                    mock.patch.object(rc.sk, "make_client"), mock.patch.object(rc.sk, "normalize_product", side_effect=self.fresh), \
                    mock.patch.object(rc.sk, "collect_section_thresholds", return_value=["новая"]), \
                    mock.patch.object(rc.sk, "methodology_record", return_value={"record_type": "section_methodology", "text": "новая"}), \
                    mock.patch.object(rc, "ROOT", Path(d)), contextlib.redirect_stdout(io.StringIO()):
                rc.run_patch("XXIII", "HEAD", True, 1, 9000)   # печать вне cp1251: без перехвата падает вне батареи
            return json.loads(out.read_text(encoding="utf-8"))

    def test_keep_takes_name_from_the_cell(self):
        self.assertEqual(self.run_patch()[0]["product_name"], "Иглы из ячейки")

    def test_keep_refreshes_methodology(self):
        self.assertEqual(self.run_patch()[-1]["text"], "новая")

    def test_keep_empty_cell_keeps_the_model_name(self):
        self.assertEqual(self.run_patch(name_raw="")[0]["product_name"], "Иглы, пересказ модели")

    def run_renumber(self, name_raw=None):
        import restructure_changed as rc

        p = self.product()
        p.position = "7"
        if name_raw is not None:
            p.name_raw = name_raw
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x.json"
            planned = (mock.Mock(), "Раздел", out, [], [], [(p, "llm", None)], [], "текст", 2)
            with mock.patch.object(rc, "plan", return_value=planned), mock.patch.object(rc.sk, "make_client"), \
                    mock.patch.object(rc.sk, "normalize_product", side_effect=self.fresh), mock.patch.object(rc, "ROOT", Path(d)), \
                    mock.patch.object(sys, "argv", ["x", "XXIII", "--anchors", "renumber", "--write"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc.main()
            return json.loads(out.read_text(encoding="utf-8"))

    def test_renumber_takes_name_from_the_cell(self):
        self.assertEqual(self.run_renumber()[0]["product_name"], "Иглы из ячейки")

    def test_renumber_empty_cell_keeps_the_model_name(self):
        self.assertEqual(self.run_renumber(name_raw="")[0]["product_name"], "Иглы, пересказ модели")


class TestThresholdSnapshot(unittest.TestCase):
    def test_snapshot_sees_procurement_threshold(self):
        snapshot = ae.threshold_snapshot()
        self.assertGreater(len(snapshot), 1000)
        self.assertTrue(any(v.split("закупка:")[1].strip() for v in snapshot.values()), "закупочных порогов нет в снимке")


class TestWitnessCells(unittest.TestCase):
    def test_neighbouring_cells_keep_their_boundary(self):
        page = DOC.format(f"<table><tr><td>N 2139</td><td>28</td></tr></table><p>{TEXT}</p>{TAIL}")
        self.assertTrue(es.witness(f"N 2139|28|{TEXT}", page).equal)
        self.assertFalse(es.witness(f"N 213928|{TEXT}", page).equal)


if __name__ == "__main__":
    unittest.main()
