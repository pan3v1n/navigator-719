"""D16 #173: пометка «из» (прим. 3 к приложению) и «за исключением» у кодов позиции.

Корень: `structure_kb` разбирал «из 32.50.22.110» в `okpd2_codes: ["32.50.22.110"]` — у 977 позиций из
1573 модель не знала, что требования относятся ТОЛЬКО к товарам, названным в наименовании; у четырёх
позиций исключённые коды стояли в `okpd2_codes` наравне со своими. Канал — рантайм-файл
`code_cells.json` (`scripts/build_code_cells.py`), без переиндексации.
Офлайн: Qdrant и ключ не нужны."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

from app.rag import code_cells, pipeline, retriever
from app.rag.code_cells import EXCEPT, IZ, PLAIN
from app.rag.retriever import Hit
from scripts import build_code_cells as build

ROOT = Path(__file__).resolve().parents[1]
STRUCTURED = ROOT / "knowledge_base" / "pp719" / "structured"
_HIT_FIELDS = {f.name for f in fields(Hit)}


def _records() -> list[dict]:
    out = []
    for f in sorted(STRUCTURED.glob("*.json")):
        out += [r for r in json.loads(f.read_text(encoding="utf-8"))
                if r.get("record_type") != "section_methodology"]
    return out


RECORDS = _records()


def _rec(section: str, name_start: str, code: str | None = None) -> dict:
    found = [r for r in RECORDS if r["section_roman"] == section
             and r["product_name"].startswith(name_start)
             and (code is None or code in (r.get("okpd2_codes") or []))]
    assert len(found) >= 1, (section, name_start, code)
    return found[0]


def _hit(rec: dict, match: bool = True) -> Hit:
    return Hit(**{k: v for k, v in {**rec, "score": 1.0, "okpd2_match": match}.items()
                  if k in _HIT_FIELDS})


def _marks(rec: dict) -> list[str] | None:
    return code_cells.marks(rec["section_roman"], rec["product_name"], rec.get("okpd2_codes"))


class TestFileMatchesCorpus(unittest.TestCase):
    def test_file_is_regenerated_from_current_corpus(self):
        """Файл отстал от `structured/` или `chunks/` — пометки привязаны к прошлой редакции. При
        актуализации: `.venv\\Scripts\\python scripts/build_code_cells.py`."""
        data, _ = build.build()
        self.assertEqual(build.dump(data), build.OUT.read_text(encoding="utf-8"),
                         "code_cells.json отстал от корпуса — перегенерировать build_code_cells.py")

    def test_every_position_has_marks_or_a_reason(self):
        """Критерий приёмки #173: флаг у каждой позиции либо явное «не определено» с причиной."""
        positions = json.loads(build.OUT.read_text(encoding="utf-8"))["positions"]
        for r in RECORDS:
            k = code_cells.key(r["section_roman"], r["product_name"], r.get("okpd2_codes"))
            self.assertIn(k, positions, k[:80])
            e = positions[k]
            if e["marks"] is None:
                self.assertTrue(e.get("reason"), k[:80])
            else:
                self.assertEqual(len(e["marks"]), len(r.get("okpd2_codes") or []), k[:80])
                self.assertTrue(set(e["marks"]) <= {IZ, PLAIN, EXCEPT}, k[:80])

    def test_population(self):
        """Радиус на дату сборки: у 977 из 1573 позиций хотя бы один код с «из», не определено 3.
        Сдвиг — повод посмотреть, что изменилось в корпусе, а не поправить число."""
        ms = [_marks(r) for r in RECORDS]
        self.assertEqual(sum(m is None for m in ms), 3)
        self.assertEqual(sum(bool(m) and IZ in m for m in ms), 977)
        self.assertEqual(sum(bool(m) and EXCEPT in m for m in ms), 4)

    def test_code_only_match_is_undetermined(self):
        """Ревью D16, LOW-2: запись, чьё наименование со строкой первоисточника не совпало ничем
        (заголовок раздела вместо пустой графы, пересказ перечня), пометки не получает — пояснение
        «только товары, названные в наименовании» отсылало бы к имени, которого в первоисточнике нет."""
        positions = json.loads(build.OUT.read_text(encoding="utf-8"))["positions"]
        for sec, start in (("III", "Продукция отрасли специального машиностроения"),
                           ("IX", "Продукция радиоэлектроники"),
                           ("XV", "Отдельные виды технологического оборудования")):
            r = _rec(sec, start)
            self.assertIsNone(_marks(r), start)
            e = positions[code_cells.key(r["section_roman"], r["product_name"], r["okpd2_codes"])]
            self.assertIn("сопоставлено только по коду", e["reason"])

    def test_marks_agree_with_raw_text(self):
        """НЕЗАВИСИМЫЙ ОРАКУЛ — голый текст раздела, без разборщика строк. Каждый код с «из» обязан
        встречаться в разделе как «из <код>», каждый код без пометки — без «из» перед ним.

        ⚠ СИЛА ОРАКУЛА — НЕ ПОЛНАЯ (ревью D16, LOW-4). Он смотрит на множества кодов РАЗДЕЛА, и у кода,
        который в разделе стоит и с «из», и без (132 из 1322 кодов с «из», 34 из 614 без), перепутанную
        пометку не видит. Такие пары держат точечные тесты (`test_same_code_both_ways_in_section`)."""
        chunks = {}
        for f in build.CHUNKS.glob("*.txt"):
            m = build._CHUNK_STEM.match(f.stem)
            if m:
                chunks[m.group(1)] = f.read_text(encoding="utf-8")
        by_section = {}
        for f in sorted(STRUCTURED.glob("*.json")):
            for r in json.loads(f.read_text(encoding="utf-8")):
                by_section.setdefault(r["section_roman"], f.stem)
        seen: dict[str, tuple[set, set]] = {}     # раздел → (коды с «из», коды без)
        for stem, text in chunks.items():
            with_iz, without = set(), set()
            for m in re.finditer(r"(?<![\w.])(из\s+)?(\d{2}(?:\.\d+)*)(?![\d.])", text, re.I):
                (with_iz if m.group(1) else without).add(m.group(2))
            seen[stem] = (with_iz, without)
        bad = []
        for r in RECORDS:
            with_iz, without = seen[by_section[r["section_roman"]]]
            for c, mk in zip(r.get("okpd2_codes") or [], _marks(r) or []):
                if (mk == IZ and c not in with_iz) or (mk == PLAIN and c not in without):
                    bad.append((r["section_roman"], c, mk, r["product_name"][:40]))
        self.assertEqual(bad, [])


class TestSourceCells(unittest.TestCase):
    """Ячейки кода, которые прежний разбор (`diag_orphan_requirements.parse_chunk`) видел неверно."""

    def test_multiline_code_cell(self):
        """«из 32.50.13.130,\\nиз 32.50.13.190,\\n…\\nиз 32.50.50.190|Имплантаты…» — девять кодов с «из».
        Прежний разбор строки без трубы отдавал в хвост ПРЕДЫДУЩЕЙ строки и видел один код."""
        r = _rec("VII", "Имплантаты для остеосинтеза")
        self.assertEqual(len(r["okpd2_codes"]), 9)
        self.assertEqual(_marks(r), [IZ] * 9)

    def test_two_codes_on_one_line(self):
        self.assertEqual(_marks(_rec("XVI", "Снежная пушка")), [IZ, IZ])            # «из A из B|»
        self.assertEqual(_marks(_rec("V", "Блочно-модульные котельные")), [IZ, IZ])  # «из A, из B|»

    def test_mixed_cell_keeps_order(self):
        """«30.12.19.141\\nиз 30.12.19.141|Шлюпки спасательные…» — один код ДВАЖДЫ, без «из» и с ним."""
        r = _rec("XVIII", "Шлюпки спасательные\n")
        self.assertEqual(r["okpd2_codes"], ["30.12.19.141", "30.12.19.141"])
        self.assertEqual(_marks(r), [PLAIN, IZ])
        self.assertEqual(code_cells.render(r["okpd2_codes"], _marks(r)), "30.12.19.141, из 30.12.19.141")
        self.assertIsNone(code_cells.mark_of("30.12.19.141", r["okpd2_codes"], _marks(r)))

    def test_excepted_codes(self):
        r = _rec("IX", "Компьютеры портативные")
        self.assertEqual(r["okpd2_codes"], ["26.20.11", "26.20.11.120", "26.20.11.130"])
        self.assertEqual(_marks(r), [PLAIN, EXCEPT, EXCEPT])
        self.assertEqual(code_cells.render(r["okpd2_codes"], _marks(r)),
                         "26.20.11 (за исключением 26.20.11.120, 26.20.11.130)")
        s = _rec("VIII", "Субстанции фармацевтические")
        self.assertEqual(code_cells.render(s["okpd2_codes"], _marks(s)), "из 21.10 (за исключением 21.10.51)")

    def test_merged_code_cell_takes_its_own_owner(self):
        """«из 20.14.19.140|Хладон-318…» и под ней «|Жидкость Б-1|…» — ячейка кода объединена.
        Пометка — от СВОЕЙ строки-владельца, а не от первой строки раздела с тем же кодом."""
        rows = build.parse_rows((build.CHUNKS / "121_XXI_neftegazohimiya.txt").read_text(encoding="utf-8"))
        row = next(x for x in rows if x["name"] == "Жидкость Б-1")
        owner = next(x for x in rows if x["line"] == row["merged_from"])
        self.assertTrue(owner["name"].startswith("Хладон-318"))
        self.assertEqual(_marks(_rec("XXI", "Жидкость Б-1")), [IZ])
        self.assertEqual(_marks(_rec("XIV", "Катализаторы гидрокрекинга")), [IZ])   # «Из 20|» заглавная

    def test_same_code_both_ways_in_section(self):
        """Класс, слепой для оракула по разделу: «30.11.21.120 Суда речные пассажирские» и ниже
        «из 30.11.21.120 … несамоходные» — один код, две позиции, разные пометки."""
        self.assertEqual(_marks(_rec("XVIII", "Суда речные пассажирские", "30.11.21.120")), [PLAIN])
        self.assertEqual(_marks(_rec("XVIII", "Суда речные пассажирские несамоходные")), [IZ])

    def test_excepted_code_attaches_to_its_own_code(self):
        """Ревью D16, LOW-5: «(за исключением …)» — к своему коду-предку, а не в конец списка."""
        self.assertEqual(code_cells.render(["26.20.11", "27.11", "26.20.11.120"], [PLAIN, IZ, EXCEPT]),
                         "26.20.11 (за исключением 26.20.11.120), из 27.11")
        self.assertEqual(code_cells.render(["27.11", "99.9"], [PLAIN, EXCEPT]), "27.11 (за исключением 99.9)")

    def test_plain_code(self):
        self.assertEqual(_marks(_rec("I", "Оправки для крепления деталей на станках")), [PLAIN])
        self.assertEqual(_marks(_rec("XXI", "Углеводороды ациклические насыщенные")), [PLAIN])


class TestRuntimeDegrades(unittest.TestCase):
    def tearDown(self):
        code_cells._positions.cache_clear()

    def test_unknown_position_has_no_marks(self):
        self.assertIsNone(code_cells.marks("XXI", "Нет такой позиции", ["20.14.11.110"]))
        self.assertEqual(code_cells.render(["20.14.11.110", "20.14"], None), "20.14.11.110, 20.14")
        self.assertEqual(code_cells.note(None), [])

    def test_missing_or_broken_file_means_no_marks(self):
        r = _rec("VII", "Имплантаты для остеосинтеза")
        with tempfile.TemporaryDirectory() as d:
            broken = Path(d) / "code_cells.json"
            broken.write_text("{не json", encoding="utf-8")
            for path in (Path(d) / "нет.json", broken):
                code_cells._positions.cache_clear()
                with mock.patch.object(code_cells, "_PATH", path):
                    self.assertIsNone(_marks(r))

    def test_marks_of_wrong_length_are_ignored(self):
        with mock.patch.object(code_cells, "_positions",
                               lambda: {code_cells.key("I", "X", ["01", "02"]): {"marks": [IZ]}}):
            self.assertIsNone(code_cells.marks("I", "X", ["01", "02"]))

    def test_note_by_kind(self):
        self.assertEqual(code_cells.note([PLAIN, PLAIN]), [])
        self.assertIn("с пометкой «из»", code_cells.note([IZ])[0])
        self.assertIn("часть кодов", code_cells.note([PLAIN, IZ])[0])
        self.assertIn("за исключением", code_cells.note([PLAIN, EXCEPT])[-1])
        self.assertEqual(len(code_cells.note([IZ, EXCEPT])), 2)


class TestContext(unittest.TestCase):
    def test_marks_add_no_numbers_to_context(self):
        """Ревью D16, MED-1: пояснение «(прим. 3 к приложению)» добавило в контекст число 3 у 583
        позиций — и выдуманное «не менее 3 баллов» переставало ловиться гардом. Пометки и пояснения
        не добавляют в контекст ни одного числа, которого там не было: по всем позициям корпуса."""
        num = re.compile(r"(?<!\d)\d+(?:[.,]\d+)?(?!\d)")
        bad = []
        with mock.patch.object(retriever, "has_exact_position", lambda c: False):
            for r in RECORDS:
                if not r.get("okpd2_codes"):
                    continue
                h = _hit(r)
                after = pipeline.format_context([h], "вопрос", r["okpd2_codes"][0])
                with mock.patch.object(code_cells, "marks", lambda *a, **k: None):
                    before = pipeline.format_context([h], "вопрос", r["okpd2_codes"][0])
                extra = set(num.findall(after)) - set(num.findall(before))
                if extra:
                    bad.append((r["product_name"][:40], sorted(extra)))
        self.assertEqual(bad, [])

    def test_target_with_iz_gets_code_line_and_note(self):
        """Случай ⑤ отчёта 22.09: «Бедренная ножка» 32.50.22.110. С ред. N 1297 позицию «Протезы
        верхних конечностей» исключили — код стоит у «Эндопротезов суставов» с «из»."""
        r = _rec("VII", "Эндопротезы суставов конечностей", "32.50.22.110")
        ctx = pipeline.format_context([_hit(r)], "Бедренная ножка SL-Stem", "32.50.22.110")
        self.assertIn("ОКПД2: из 32.50.13.130, из 32.50.13.190, из 32.50.22.110", ctx)
        self.assertIn("⚠ Код указан с пометкой «из» (примечания к приложению): требования этой позиции "
                      "распространяются только на товары, названные в её наименовании", ctx)

    def test_plain_target_gets_no_note(self):
        r = _rec("I", "Оправки для крепления деталей на станках")
        ctx = pipeline.format_context([_hit(r)], "вопрос", r["okpd2_codes"][0])
        self.assertIn(f"ОКПД2: {r['okpd2_codes'][0]}\n", ctx)
        self.assertNotIn("пометкой «из»", ctx)

    def test_candidate_shows_marks_but_no_note(self):
        """У нецелевого кандидата требований в контексте нет (EV7) — пояснять пометку нечего, а сам
        код показывается как в первоисточнике."""
        target = _rec("I", "Оправки для крепления деталей на станках")
        cand = _rec("VII", "Имплантаты для остеосинтеза")
        ctx = pipeline.format_context([_hit(target), _hit(cand, match=False)], "вопрос",
                                      target["okpd2_codes"][0])
        self.assertIn("(ОКПД2 из 32.50.13.130,", ctx)
        self.assertNotIn("пометкой «из»", ctx)

    def test_excepted_codes_in_context(self):
        r = _rec("IX", "Компьютеры портативные")
        ctx = pipeline.format_context([_hit(r)], "вопрос", "26.20.11.120")
        self.assertIn("ОКПД2: 26.20.11 (за исключением 26.20.11.120, 26.20.11.130)", ctx)
        self.assertIn("под эту позицию не подпадает", ctx)


class TestGroupNote(unittest.TestCase):
    """D14: строка «код входит в группировку» говорит, КАКАЯ пометка у охватывающего кода."""

    def _ctx(self, rec, code):
        with mock.patch.object(retriever, "has_exact_position", lambda c: False):
            return pipeline.format_context([_hit(rec)], "вопрос", code)

    def test_plain_grouping_covers_all(self):
        ctx = self._ctx(_rec("XXI", "Углеводороды ациклические насыщенные"), "20.14.11.112")
        self.assertIn("20.14.11.112 ВХОДИТ В ГРУППИРОВКУ ОКПД2 20.14.11.110", ctx)
        self.assertIn("код позиции указан без пометки «из»: позиция охватывает всю группировку", ctx)
        self.assertNotIn("при пометке «из»", ctx)

    def test_iz_grouping_restricts_to_named_goods(self):
        ctx = self._ctx(_rec("V", "Трансформаторы электрические", "27.11.4"), "27.11.41")
        self.assertIn("27.11.41 ВХОДИТ В ГРУППИРОВКУ ОКПД2 27.11.4", ctx)
        self.assertIn("ТОЛЬКО если продукция относится к товарам, названным в наименовании позиции "
                      "(код позиции указан с пометкой «из»", ctx)

    def test_unknown_mark_keeps_conditional_wording(self):
        rec = {**_rec("XXI", "Углеводороды ациклические насыщенные"), "section_roman": "?"}
        ctx = self._ctx(rec, "20.14.11.112")
        self.assertIn("(при пометке «из» — только к ним).", ctx)

    def test_grouping_with_exceptions_is_not_whole(self):
        """Ревью D16, LOW-3: «Компьютеры портативные» 26.20.11 (за исключением …120, …130) —
        позиция-группировка, но охватывает её не всю."""
        ctx = self._ctx(_rec("IX", "Компьютеры портативные"), "26.20.11.110")
        self.assertIn("26.20.11.110 ВХОДИТ В ГРУППИРОВКУ ОКПД2 26.20.11", ctx)
        self.assertIn("охватывает группировку, кроме кодов, указанных «за исключением»", ctx)
        self.assertNotIn("охватывает всю группировку", ctx)

    def test_no_group_note_under_excepted_code(self):
        """Код вопроса под исключённым кодом — позиция на него НЕ распространяется: строки нет,
        даже если охватывающим окажется свой код (защита в коде, а не в данных)."""
        r = _rec("IX", "Компьютеры портативные")
        with mock.patch.object(pipeline, "covers", lambda codes, name, q: "26.20.11"):
            ctx = self._ctx(r, "26.20.11.121")
        self.assertNotIn("ГРУППИРОВКУ", ctx)


class TestPanel(unittest.TestCase):
    def test_panel_shows_iz_after_threshold(self):
        r = _rec("VII", "Эндопротезы суставов конечностей", "32.50.22.110")
        text = pipeline.source_text(_hit(r))
        self.assertTrue(text.startswith("Порог:"))
        self.assertIn("Код в приложении: из 32.50.13.130", text)
        self.assertIn("только на товары, названные в её наименовании", text)

    def test_panel_without_threshold_starts_with_code_line(self):
        r = _rec("IX", "Компьютеры портативные")
        with mock.patch.object(pipeline, "lookup_threshold", lambda *a, **k: None), \
                mock.patch.object(Hit, "min_threshold", None, create=True):
            text = pipeline.source_text(_hit({**r, "min_threshold": None}))
        self.assertTrue(text.startswith("Код в приложении: 26.20.11 (за исключением"), text[:80])

    def test_plain_panel_has_no_code_line(self):
        r = _rec("I", "Оправки для крепления деталей на станках")
        self.assertNotIn("Код в приложении", pipeline.source_text(_hit(r)))


class TestCheckMode(unittest.TestCase):
    def test_check_fails_on_stale_file(self):
        with tempfile.TemporaryDirectory() as d:
            stale = Path(d) / "code_cells.json"
            stale.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(build, "OUT", stale), mock.patch("sys.argv", ["x", "--check"]):
                self.assertEqual(build.main(), 1)
            stale.write_text(build.dump(build.build()[0]), encoding="utf-8")
            with mock.patch.object(build, "OUT", stale), mock.patch("sys.argv", ["x", "--check"]):
                self.assertEqual(build.main(), 0)


if __name__ == "__main__":
    unittest.main()
