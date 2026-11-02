"""`A10`: автоматическая актуализация. Офлайн: без сети, модели и Qdrant.

Фрагменты страниц — настоящая разметка Контура (02.11.2026, документы 508574, 508767 и акт N 1035):
шаблоны разбора писались вслепую и один раз уже промахнулись по кавычкам, поэтому тесты идут на живой форме.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
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
        moved = {**self.REC, "source_anchor": "XXIII, позиция 4", "amendments": ["N 1257"]}
        self.assertEqual(ae.changed_anchors({"a": self.REC}, {"b": moved}), set())

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
        self.assertEqual(ae.operations_count(rec), 2)


class TestReviewRows(unittest.TestCase):
    def rec(self, points, notes=None):
        return {"product_name": "Иглы", "section_roman": "XXIII", "okpd2_codes": ["25.93"], "notes": notes,
                "requirement_blocks": [{"operations": [{"text": "x", "points": n} for n in points]}]}

    def rows(self, rec):
        # Настоящий `Product`: заглушка с атрибутом `raw_block` скрыла, что это метод, и реплей упал на бою шлюзов.
        import structure_kb as sk

        product = sk.Product(okpd2_field="25.93", name_raw="Иглы", okpd2_codes=["25.93"],
                             component_cells=["сварка 20 баллов", "сборка 2 балла"])
        chunk = SimpleNamespace(read_text=lambda encoding: "")
        with mock.patch("structure_kb.parse_section", return_value=("", [product])), \
                mock.patch("structure_kb.find_chunk", return_value=chunk):
            return ae.review_rows({"a"}, {"a": rec}, {"a": "не менее 115"})

    def test_swapped_points_inside_set_are_seen_as_sum_only(self):
        rows, notes = self.rows(self.rec([20, 2]))
        self.assertEqual(notes, [])
        self.assertIn("22 / 22", rows[0])

    def test_lost_points_flagged(self):
        _, notes = self.rows(self.rec([20]))
        self.assertIn("нет в записи [2]", notes[0])

    def test_extra_points_flagged(self):
        _, notes = self.rows(self.rec([20, 2, 7]))
        self.assertIn("лишние [7]", notes[0])

    def test_operations_with_points_are_compared(self):
        rows, _ = self.rows(self.rec([20, 2]))
        self.assertIn("2 (2 / 2)", rows[0])

    def test_table_row_survives_line_breaks_and_pipes(self):
        self.assertEqual(ae.table_row("a\nb", "c|d"), "| a b | c/d |")

    def test_requirement_in_notes_flagged(self):
        _, notes = self.rows(self.rec([20, 2], notes="наличие прав на документацию"))
        self.assertTrue(any("notes" in n for n in notes))

    def test_unmatched_record_flagged(self):
        rec = {**self.rec([20, 2]), "product_name": "Другое"}
        rows, notes = self.rows(rec)
        self.assertIn("не сопоставлена", notes[0])
        self.assertIn("?", rows[0])


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
    def step(self, page_text: str, witness_text: str = TEXT):
        with tempfile.TemporaryDirectory() as d:
            rtf, page = Path(d) / "a.rtf", Path(d) / "a.html"
            rtf.write_bytes(b"x")
            page.write_text(DOC.format(f"<p>{page_text}</p>{TAIL}"), encoding="utf-8")
            ctx = make_ctx()
            ctx.args.rtf, ctx.args.page = str(rtf), str(page)
            with mock.patch.object(ae.es, "rtf_to_corpus_text", return_value=witness_text), \
                    mock.patch.object(ae, "FULL", Path(d) / "full.txt"):
                (Path(d) / "full.txt").write_text("старый", encoding="utf-8")
                return ctx, ae.step_text(ctx)

    def test_equal_text_goes_on(self):
        ctx, _ = self.step(TEXT)
        self.assertEqual(ctx.new_text, TEXT)

    def test_mismatch_stops(self):
        with self.assertRaisesRegex(ae.Stop, "не совпал с HTML"):
            self.step(TEXT.replace("30", "99", 1))


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


class TestRegressions(unittest.TestCase):
    def test_only_growth_counts(self):
        self.assertEqual(ae.regressions({"a": 1, "b": 2}, {"a": 1, "b": 1}), [])
        self.assertEqual(ae.regressions({"a": 1}, {"a": 2}), ["a: 1 → 2"])


class TestVerifySummary(unittest.TestCase):
    OK = (SimpleNamespace(stdout="ИТОГО: 0 расхождений на 9 позиций"),
          SimpleNamespace(stdout="Потеряно полностью (нет нигде в JSON): НЕТ"),
          SimpleNamespace(stdout="ИТОГО чисто: 29/29"))

    def test_numbers_from_output(self):
        with mock.patch.object(ae, "py", side_effect=self.OK):
            self.assertEqual(ae.verify_summary(), {"расхождений в записях": 0, "разделов с потерями": 0, "разделов не чисто": 0})

    def test_lost_points_counted(self):
        lost = (self.OK[0], SimpleNamespace(stdout="Потеряно полностью (нет нигде в JSON): {12}"), self.OK[2])
        with mock.patch.object(ae, "py", side_effect=lost):
            self.assertEqual(ae.verify_summary()["разделов с потерями"], 1)

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
    REC = {"product_name": "Магниты", "okpd2_codes": ["1"]}
    CLEAN = {"расхождений в записях": 0, "разделов с потерями": 0, "разделов не чисто": 0}

    def step(self, after_recs=None, after_thr=None, summary=None, consistent=True, llm=(), gone=()):
        ctx = make_ctx(before={"пороги": {"a": "70"}, "записи": {"a": self.REC}, "сверки": self.CLEAN},
                       plans={"I": {"в модель": list(llm), "без модели": [], "исключены": list(gone)}})
        out = json.dumps({"корпус": {"части согласованы": consistent}})
        with mock.patch.object(ae, "threshold_snapshot", return_value=after_thr or {"a": "70"}), \
                mock.patch.object(ae, "records_by_anchor", return_value={"a": self.REC} if after_recs is None else after_recs), \
                mock.patch.object(ae, "verify_summary", return_value=summary or self.CLEAN), \
                mock.patch.object(ae, "py", return_value=SimpleNamespace(stdout=out)), \
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


class TestTestsStep(unittest.TestCase):
    def test_green(self):
        with mock.patch.object(ae, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            self.assertIn("зелёная", ae.step_tests(make_ctx()))

    def test_red_is_a_warning_with_names(self):
        ctx = make_ctx()
        done = SimpleNamespace(returncode=1, stdout="", stderr="FAIL: test_x (tests.test_m.T.test_x)\n")
        with mock.patch.object(ae, "run", return_value=done) as run:
            ae.step_tests(ctx)
        self.assertEqual(ctx.facts["упавшие тесты"], ["tests.test_m.T.test_x"])
        self.assertTrue(ctx.warnings)
        self.assertIs(run.call_args.kwargs["utf8"], False)

    def test_crash_without_names_stops(self):
        done = SimpleNamespace(returncode=3, stdout="", stderr="ImportError")
        with mock.patch.object(ae, "run", return_value=done), self.assertRaisesRegex(ae.Stop, "упала без списка"):
            ae.step_tests(make_ctx())


class TestRetargetLabel(unittest.TestCase):
    def test_manifest_and_landing_follow_the_label(self):
        with tempfile.TemporaryDirectory() as d:
            man, land = Path(d) / "m.yaml", Path(d) / "i.html"
            man.write_text('edition_expected: "ред. от 05.10.2026 N 1297"\nedition_expected: "ред. от 13.04.2026 N 400"\n', encoding="utf-8")
            land.write_text('ред. от 05.10.2026 № 1297<br>ред. от 05.10.2026</div>редакция от 05.10.2026 № 1297.', encoding="utf-8")
            with mock.patch.object(ae, "MANIFEST", man), mock.patch.object(ae, "LANDING", land):
                done = ae.retarget_label("ред. от 05.10.2026 N 1297", "ред. от 20.12.2026 N 1500")
            self.assertEqual(len(done), 2)
            self.assertIn('"ред. от 20.12.2026 N 1500"', man.read_text(encoding="utf-8"))
            self.assertIn('"ред. от 13.04.2026 N 400"', man.read_text(encoding="utf-8"))
            self.assertNotIn("05.10.2026", land.read_text(encoding="utf-8"))

    def test_unparsed_label_stops(self):
        with self.assertRaises(ae.Stop):
            ae.retarget_label("редакция не определена", "ред. от 20.12.2026 N 1500")


class TestVerdict(unittest.TestCase):
    def test_partial_run_is_not_ready(self):
        self.assertIn("ЧАСТИЧНЫЙ", ae.verdict_of(None, "guard", []))

    def test_stop_wins(self):
        self.assertTrue(ae.verdict_of("gates: x", "gates", []).startswith("ОСТАНОВЛЕНО"))

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
        facts = {"изменённые записи": ["| Иглы | XXIII | 16 / 16 | 145 / 145 | 115 |"], "исключённые позиции": ["Прежняя"]}
        text = ae.render_report("1", "x", [], facts)
        self.assertIn("| Иглы |", text)
        self.assertIn("Прежняя", text)


class TestCheckKontur(unittest.TestCase):
    def check(self, page, known=None, doc_id="508574"):
        return we.check_kontur(doc_id, KNOWN if known is None else known,
                               fetch=lambda url: (page.encode("utf-8"), ""), full_text="", today=TODAY)

    def test_nothing_new(self):
        self.assertEqual(self.check(ACTUAL_PAGE)["исход"], "нового нет")

    def test_new_document(self):
        r = self.check(OUTDATED_PAGE, known={}, doc_id="508767")
        self.assertEqual((r["исход"], r["новые документы"]), ("новое", {"508574": "Перейти в действующую"}))

    def test_new_pending_act(self):
        r = self.check(ACTUAL_PAGE, known={"N 1284 от 27.08.2025": "01.06.2027"})
        self.assertEqual(r["новые отложенные акты"], ["N 1353 от 15.10.2026 (с 01.01.2027)"])

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
        page = ACTUAL_PAGE.replace("Примечания", "Сведения")
        r = self.check(page)
        self.assertEqual(r["исход"], "не проверено")
        self.assertIn("исчезли", r["причина"])

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

    def test_dry_run_touches_nothing(self):
        runner = mock.Mock()
        self.assertIn("была бы создана", aa.Issues(True, runner).once("t", "b"))
        runner.assert_not_called()


class TestHandleFound(unittest.TestCase):
    NEW_DOC = {"новые отложенные акты": [], "актов нет в тексте": [], "новые документы": {"509000": "Перейти"}}

    def test_dry_run_builds_nothing(self):
        result = {**self.NEW_DOC, "новые отложенные акты": ["N 1 от 01.01.2027"]}
        issues = aa.Issues(True, mock.Mock())
        with mock.patch.object(aa, "build_edition") as build:
            log = aa.handle_found(result, Path("."), issues)
        build.assert_not_called()
        self.assertEqual(len(log), 2)

    def test_existing_build_is_skipped(self):
        with mock.patch.object(aa, "edition_built", return_value=True), mock.patch.object(aa, "build_edition") as build:
            log = aa.handle_found(self.NEW_DOC, Path("."), aa.Issues(False, mock.Mock()))
        build.assert_not_called()
        self.assertIn("ждёт разбора", log[0])

    def test_stopped_pipeline_makes_short_issue_not_pr(self):
        issues = mock.Mock(dry_run=False)
        report = {"последний шаг": "gates", "вердикт": "ОСТАНОВЛЕНО: input_value='секрет'"}
        with mock.patch.object(aa, "edition_built", return_value=False), \
                mock.patch.object(aa, "build_edition", return_value=(False, report)), \
                mock.patch.object(aa, "publish") as publish:
            aa.handle_found(self.NEW_DOC, Path("."), issues)
        publish.assert_not_called()
        title, body = issues.once.call_args.args
        self.assertIn("gates", title)
        self.assertNotIn("секрет", title + body)

    def test_publish_failure_makes_issue(self):
        issues = mock.Mock(dry_run=False)
        with mock.patch.object(aa, "edition_built", return_value=False), \
                mock.patch.object(aa, "build_edition", return_value=(True, {})), \
                mock.patch.object(aa, "publish", side_effect=subprocess.CalledProcessError(1, "git push")):
            aa.handle_found(self.NEW_DOC, Path("."), issues)
        self.assertIn("PR не создан", issues.once.call_args.args[0])


class TestRunOnce(unittest.TestCase):
    def run_with(self, result, state, moment=None):
        issues = mock.Mock(dry_run=False)
        with mock.patch.object(aa.we, "check_kontur", return_value=result), \
                mock.patch.object(aa.we, "current_doc_id", return_value="1"), \
                mock.patch.object(aa.we, "known_pending", return_value={}), \
                mock.patch.object(aa, "now", return_value=moment or dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)), \
                mock.patch.object(aa, "handle_found", return_value=[]) as found:
            aa.run_once(SimpleNamespace(), Path("."), state, issues)
        return issues, found

    LAST = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc).isoformat()

    def test_success_moves_the_date(self):
        state = {"последняя успешная проверка": self.LAST}
        _, found = self.run_with({"исход": "нового нет"}, state)
        self.assertIn("2026-11-10", state["последняя успешная проверка"])
        found.assert_not_called()

    def test_new_goes_to_handler(self):
        _, found = self.run_with({"исход": "новое"}, {})
        found.assert_called_once()

    def test_week_of_silence_makes_issue(self):
        issues, _ = self.run_with({"исход": "не проверено", "причина": "сеть"}, {"последняя успешная проверка": self.LAST})
        self.assertIn("молчит", issues.once.call_args.args[0])

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

            def boom(args, sources, state, issues):
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

    def test_dry_run_writes_no_state(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "argv", ["x", "--sources", d, "--dry-run"]), mock.patch.object(aa, "enable_utf8"), \
                    mock.patch.object(aa, "run_once"):
                aa.main()
            self.assertFalse((Path(d) / "watch_state.json").exists())


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


class TestParserPrompt(unittest.TestCase):
    def test_mandatory_requirement_goes_to_a_block(self):
        self.assertIn('"component": "Общие требования"', KB_PARSER_SYSTEM_PROMPT)
        self.assertIn('В "notes" их', KB_PARSER_SYSTEM_PROMPT)


class TestRestructurePlan(unittest.TestCase):
    def test_plan_file(self):
        import restructure_changed as rc

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p.json"
            rc.write_plan(str(path), "keep", ["а"], ["б"], ["в"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {"режим": "keep", "в модель": ["а"], "без модели": ["б"], "исключены": ["в"]})
            rc.write_plan(None, "keep", [], [], [])


if __name__ == "__main__":
    unittest.main()
