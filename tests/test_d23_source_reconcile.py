"""`D23` #251 — сверка «код → перечень» с первоисточником (`scripts/d23_groups.py`) и её следы в данных.

Инструмент читает HTML Контура (в репозитории его нет, 9 МБ), поэтому механизм проверяется на синтетических
таблицах, а результат — на самом корпусе: те позиции, ради которых правка делалась, обязаны нести полный
перечень своей ячейки. `D19` #210 п. 2 (примечание раздела) и `D18` #208 (служебная строка Контура) —
здесь же: они уехали тем же ночным релизом данных."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import d23_groups as d  # noqa: E402

OPENER = ("наличие у юридического лица - налогового резидента стран - членов Евразийского экономического "
          "союза прав на конструкторскую документацию")


def _rec(name, ops=(), pos=1, sec="T", comp=None, notes=None, rtype="operations"):
    return {"section_roman": sec, "product_name": name, "okpd2_codes": [], "requirement_type": rtype,
            "min_threshold": None, "notes": notes, "source_anchor": f"Приложение к ПП №719, Раздел {sec}, позиция {pos}",
            "requirement_blocks": ([{"component": comp, "operations": [{"text": t, "points": p} for t, p in ops]}]
                                   if ops else [])}


def _tables(html: str):
    p = d._Tables()
    p.feed(html)
    return p.tables


def _table(rows: str) -> str:
    return f"<table><tr><td>Код</td><td>Наименование</td><td>Требования</td></tr>{rows}</table>"


class TestRowspanSharesOneList(unittest.TestCase):
    """Строки под ROWSPAN-ячейкой делят ОДИН перечень: разбор раздал его по строкам — правка собирает."""

    HTML = _table(
        f'<tr><td>11.11</td><td>Изделие А</td><td rowspan="3">{OPENER};<br>сварка корпуса;<br>'
        'покраска корпуса;<br>сборка изделия</td></tr>'
        '<tr><td>11.12</td><td>Изделие Б</td></tr>'
        '<tr><td>11.13</td><td>Изделие В</td></tr>')

    def _report(self, recs):
        g = d.grid(_tables(self.HTML)[0])
        return d.analyse({"T": [g]}, {"T": recs}, 0.6, 0.1)

    def test_split_list_is_reconciled_for_every_member(self):
        a = _rec("Изделие А", [(OPENER, None), ("сварка корпуса", None)], 1)
        b = _rec("Изделие Б", [("покраска корпуса", None), ("сборка изделия", None)], 2)
        c = _rec("Изделие В", [], 3)
        rep = self._report([a, b, c])
        self.assertEqual([x["verdict"] for x in rep["T"]["shared"]], ["расколот"])
        log = d.reconcile(rep)
        self.assertEqual(len(log), 3)
        for r in (a, b, c):
            texts = [o["text"] for bl in r["requirement_blocks"] for o in bl["operations"]]
            self.assertEqual(texts, [OPENER, "сварка корпуса", "покраска корпуса", "сборка изделия"], r["product_name"])
        # идемпотентно: второй проход ничего не меняет
        self.assertEqual(d.reconcile(self._report([a, b, c])), [])


class TestDuplicateNames(unittest.TestCase):
    """Одноимённые записи раздела — разные позиции: имя их не разрешает, порядок таблицы — да."""

    def setUp(self):
        self.first = _rec("Трансмиттеры", [(OPENER, None), ("сборка", None)], 43)
        self.second = _rec("Трансмиттеры", [], 45)
        self.other = _rec("Когерентные трансмиттеры", [], 44)
        self.find = d.match_records([self.second, self.other, self.first])   # порядок файла — не порядок позиций

    def test_find_refuses_ambiguous_name(self):
        self.assertIsNone(self.find("T", "Трансмиттеры"))
        self.assertIs(self.find("T", "Когерентные трансмиттеры"), self.other)

    def test_claim_hands_out_in_position_order(self):
        self.assertIs(self.find.claim("T", "Трансмиттеры"), self.first)
        self.assertIs(self.find.claim("T", "Трансмиттеры"), self.second)

    def test_parent_resolved_by_operations(self):
        parent = {"product_name": "Трансмиттеры", "operations": [{"text": OPENER}, {"text": "сборка"}]}
        self.assertIs(self.find.parent("T", parent), self.first)
        self.assertIsNone(self.find.parent("T", {"product_name": "Трансмиттеры", "operations": [{"text": "x"}]}))


class TestFullList(unittest.TestCase):

    def test_single_parse_not_taken_when_other_member_has_unique_ops(self):
        """Станки I: 8 операций у I-13, 204 у I-19 — разбор одного I-19 «покрывал» ячейку словами, а первые
        операции терялись. Перечень — объединение."""
        # слова головной операции у донора ВСЕ есть (перечень повторяет их), но ни одна операция донора её не
        # покрывает — покрытие по словам случаи не различает, различают только операции
        head = _rec("Станки лазерные", [("наличие управляющего программно-аппаратного комплекса", 25)], 13)
        tail_ops = [("наличие станины", 1), ("обработка управляющего узла", 1),
                    ("сборка программно-аппаратного модуля", 1), ("испытания комплекса", 1)]
        tail = _rec("Части станков", tail_ops, 19)
        text = ("наличие управляющего программно-аппаратного комплекса (25 баллов);\n"
                + "\n".join(f"{t} (1 балл);" for t, _ in tail_ops))
        blocks, how = d.full_list([head, tail], text)
        ops = [o["text"] for b in blocks for o in b["operations"]]
        self.assertIn("наличие управляющего программно-аппаратного комплекса", ops)
        self.assertEqual(len(ops), 5)
        self.assertEqual(how, "объединение блоков членов")

    def test_single_parse_taken_when_it_carries_everything(self):
        full = _rec("Полный", [("операция один", None), ("операция два", None)], 1)
        part = _rec("Часть", [("операция один", None)], 2)
        blocks, how = d.full_list([full, part], "операция один;\nоперация два")
        self.assertTrue(how.startswith("разбор"), how)
        self.assertEqual(len([o for b in blocks for o in b["operations"]]), 2)

    def test_numbers_tell_operations_apart(self):
        """Ячейка гамма-камер и МР-томографов VII: «не более 50 процентов» ИЛИ «не более 70 процентов»."""
        op50 = d.norm("соблюдение процентной доли иностранных товаров - не более 50 процентов цены товара")
        op70 = d.norm("соблюдение процентной доли иностранных товаров - не более 70 процентов цены товара")
        self.assertFalse(d._op_covered(op50, [op70]))
        self.assertTrue(d._op_covered(op50, [op50]))

    def test_member_name_labels_are_removed(self):
        """Подпись `component` = имя СВОЕЙ позиции в общем перечне читалась бы «требования к соседу»."""
        a = _rec("Хладон-318", [("пиролиз", 100)], 1, comp="Хладон-318")
        b = _rec("Жидкость Б-1", [("ректификация", 100)], 2, comp="Жидкость Б-1")
        blocks, _ = d.full_list([a, b], "пиролиз;\nректификация")
        self.assertEqual([bl.get("component") for bl in blocks], [None, None])
        self.assertEqual([o["text"] for bl in blocks for o in bl["operations"]], ["пиролиз", "ректификация"])


class TestContinuationJoin(unittest.TestCase):
    """Ячейку рвут строки поправок во всю ширину; продолжение узнаётся по оборванной фразе."""

    @staticmethod
    def _grp(code, req):
        return {"code": code, "codes": [code], "rows": [{"req": req, "name": code}], "excluded": False}

    def test_comma_tail_joins(self):
        out = d._join_continuations([self._grp("1", f"{OPENER};\nправа использования товарного знака,"),
                                     self._grp("2", "правообладателем которого является субъект")])
        self.assertEqual(len(out), 1)

    def test_lowercase_after_unfinished_phrase_joins(self):
        out = d._join_continuations([self._grp("1", f"{OPENER};\nнаправлена на комплексное предотвращение"),
                                     self._grp("2", "и (или) минимизацию негативного воздействия")])
        self.assertEqual(len(out), 1)

    def test_points_list_after_semicolon_does_not_join(self):
        """Газовозы XVIII законно начинают свой перечень операцией с баллами."""
        out = d._join_continuations([self._grp("1", f"{OPENER};\nмонтаж (10 баллов);"),
                                     self._grp("2", "монтаж судовой системы (15 баллов);")])
        self.assertEqual(len(out), 2)

    def test_finished_sentence_does_not_join(self):
        out = d._join_continuations([self._grp("1", f"{OPENER}."), self._grp("2", "сборка изделия")])
        self.assertEqual(len(out), 2)


class TestSectionNoteMoved(unittest.TestCase):
    """`D19` #210 п. 2: примечание о целом разделе — в методику раздела, а не у случайной позиции."""

    NOTE = ("Заключения, выданные до 10.05.2024 в отношении продукции, включенной в раздел VII (в ред. "
            "Постановления Правительства РФ от 06.05.2024 N 590), действительны до окончания их срока действия "
            "(пункт 2 Постановления Правительства РФ от 06.05.2024 N 590).")

    def test_note_moves_once(self):
        meth = {"section_roman": "VII", "record_type": "section_methodology", "product_name": "Методика",
                "notes": "Пороги раздела."}
        a = _rec("Плазмаферез", [("сборка", None)], 22, sec="VII", notes=self.NOTE)
        b = _rec("Морозильники", [("сборка", None)], 37, sec="VII", notes=self.NOTE)
        alien = _rec("Чужое", [("сборка", None)], 5, sec="VI", notes=self.NOTE)
        files = {Path("VII.json"): [meth, a, b], Path("VI.json"): [alien]}
        log = d.move_section_notes(files)
        self.assertEqual(len(log), 2)
        self.assertIsNone(a["notes"])
        self.assertIsNone(b["notes"])
        self.assertEqual(meth["notes"].count("Заключения, выданные до 10.05.2024"), 1)
        self.assertEqual(alien["notes"], self.NOTE)      # примечание о VII у позиции VI — не трогаем
        self.assertEqual(d.move_section_notes(files), [])


class TestShareCellThresholds(unittest.TestCase):
    """Порог кодового примечания — членам ячейки без порога, только если коды примечания = коды ячейки."""

    NOTES = ("71. Продукция, классифицируемая кодами по ОК 034-2014 (КПЕС 2008) из 26.11.22.200, из 26.11.22.210, "
             "26.11.22.216 и включенная в раздел IV настоящего приложения, может быть отнесена … "
             "(в ред. Постановления Правительства РФ от 15.12.2025 N 2033)\n"
             "73. Продукция, классифицируемая кодом из 26.11.22.200 \"Светодиоды особые\" и включенная в раздел IV …\n")
    THR = "с 1 января 2026 г. - не менее 32 баллов [прим. 71]"

    def _report(self, code, thr=THR):
        donor = _rec("Светодиоды белого диапазона", [("сборка", 2)], 3, sec="IV")
        donor["min_threshold"] = thr
        qual = _rec("Светодиоды (в части белого диапазона)", [("сборка", 2)], 2, sec="IV")
        return {"IV": {"shared": [{"code": code, "_recs": [qual, donor]}]}}, qual

    def test_equal_code_lists_share_threshold(self):
        rep, qual = self._report("из 26.11.22.200 | из 26.11.22.210 | 26.11.22.216")
        log = d.share_cell_thresholds(rep, self.NOTES)
        self.assertEqual(len(log), 1)
        self.assertEqual(qual["min_threshold"], self.THR)

    def test_different_code_lists_share_nothing(self):
        """В общем случае члены ячейки законно имеют разные пороги (V «из 27.12»)."""
        rep, qual = self._report("из 26.11.22.200 | 26.11.22.216")
        self.assertEqual(d.share_cell_thresholds(rep, self.NOTES), [])
        self.assertIsNone(qual["min_threshold"])

    def test_note_naming_a_product_is_not_a_code_note(self):
        rep, qual = self._report("из 26.11.22.200", thr="не менее 40 баллов [прим. 73]")
        self.assertEqual(d.share_cell_thresholds(rep, self.NOTES), [])

    def test_date_of_amendment_is_not_a_code(self):
        self.assertEqual(d._note_codes("71", self.NOTES),
                         ["26.11.22.216", "из 26.11.22.200", "из 26.11.22.210"])
        # дата поправки в самой голове примечания — тоже не код
        dated = "74. Продукция (в ред. от 15.12.2025) кодами из 26.11.22.200, 26.11.22.216 и включенная в раздел IV\n"
        self.assertEqual(d._note_codes("74", dated), ["26.11.22.216", "из 26.11.22.200"])


class TestCorpusAfterD23(unittest.TestCase):
    """Корпус после правки: позиции, ради которых она делалась, несут полный перечень своей ячейки."""

    @classmethod
    def setUpClass(cls):
        cls.recs = d.load_records()

    def _one(self, sec, prefix):
        hit = [r for r in self.recs[sec] if r["product_name"].startswith(prefix)]
        self.assertEqual(len(hit), 1, f"{sec}: «{prefix}» — {len(hit)} записей")
        return hit[0]

    def _blocks(self, r):
        return json.dumps(r.get("requirement_blocks"), ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _ops(r):
        return [o for b in r.get("requirement_blocks") or [] for o in b.get("operations") or []]

    def test_combines_carry_whole_list(self):
        """«Комбайны зерноуборочные» несли 1 операцию из перечня, остальное — «Машинокомплекты»."""
        a = self._one("III", "Комбайны зерноуборочные")
        b = self._one("III", "Машинокомплекты комбайнов зерноуборочных")
        self.assertGreaterEqual(len(self._ops(a)), 170)
        self.assertEqual(self._blocks(a), self._blocks(b))

    def test_transmitters_44_45_get_list_of_43(self):
        iv = {r["source_anchor"].rsplit(" ", 1)[-1]: r for r in self.recs["IV"]}
        self.assertTrue(self._ops(iv["43"]))
        self.assertEqual(self._blocks(iv["44"]), self._blocks(iv["43"]))
        self.assertEqual(self._blocks(iv["45"]), self._blocks(iv["43"]))
        self.assertNotIn("доразбор", iv["44"].get("notes") or "")

    def test_machine_tools_keep_head_of_list(self):
        r = self._one("I", "Станки токарные, расточные и фрезерные")
        hit = [o for o in self._ops(r) if "программно-аппаратного комплекса" in (o.get("text") or "")]
        self.assertTrue(hit)
        self.assertEqual(hit[0].get("points"), 25)

    def test_gamma_cameras_keep_both_variants(self):
        r = self._one("VII", "Системы однофотонной эмиссионной компьютерной томографии")
        texts = " ".join(o.get("text") or "" for o in self._ops(r))
        self.assertIn("не более 50 процентов", texts)
        self.assertIn("не более 70 процентов", texts)

    def test_freons_share_one_list_without_neighbour_labels(self):
        a = self._one("XXI", "Хладон-218")
        b = self._one("XXI", "Хладон-125 ХП")
        self.assertEqual(self._blocks(a), self._blocks(b))
        comps = {(bl.get("component") or "") for bl in a["requirement_blocks"]}
        self.assertFalse({c for c in comps if c.startswith(("Хладон", "Мономер"))}, comps)

    def test_led_qualifiers_carry_threshold_of_their_cell(self):
        """Квалификаторы IV-1, 2 (белые) — порог прим. 71, IV-4, 5 (небелые) — прим. 72: тот же, что рантайм
        отдаёт соседу по ячейке. Сторож устаревания: новая редакция прим. 71/72 изменит порог соседа, а запись
        квалификатора — нет, и тест покраснеет (перезапустить `d23_groups.py --write`)."""
        from app.rag import thresholds as T
        iv = {r["source_anchor"].rsplit(" ", 1)[-1]: r for r in self.recs["IV"]}

        def runtime(r):
            return T.lookup_threshold(r["okpd2_codes"], r["product_name"], "IV")

        for q, donor, note in (("1", "3", "71"), ("2", "3", "71"), ("4", "6", "72"), ("5", "6", "72")):
            self.assertEqual(iv[q]["min_threshold"], runtime(iv[donor]), q)
            self.assertIn(f"[прим. {note}]", iv[q]["min_threshold"], q)

    def test_section_note_lives_in_methodology(self):
        files = d.load_files()
        vii = next(v for f, v in files.items() if f.name.startswith("VII_"))
        meth = next(r for r in vii if r.get("record_type") == "section_methodology")
        self.assertIn("Заключения, выданные до 10.05.2024", meth.get("notes") or "")
        stray = [r["source_anchor"] for r in vii if r.get("record_type") != "section_methodology"
                 and "Заключения, выданные до 10.05.2024" in (r.get("notes") or "")]
        self.assertEqual(stray, [])


class TestR29AfterD23(unittest.TestCase):
    """Группы R29 сверены: пометка «перечень неполный» снята, а состав группы и роли строк остались."""

    def test_reconciled_groups_lose_incomplete_notice_but_keep_roles(self):
        from app.rag import fragments
        groups = json.loads((ROOT / "knowledge_base/pp719/fragmented_requirements.json").read_text(encoding="utf-8"))
        self.assertTrue(groups)
        self.assertTrue(all(g.get("reconciled") for g in groups), [g["section"] for g in groups if not g.get("reconciled")])
        self.assertFalse(fragments.is_fragmented("Хладон-218 (октафторпропан)"))
        self.assertFalse(fragments.is_fragmented("Аддитивные установки экструзии материала"))
        # EV6/EV7: обрывок и продукт одной ячейки — по-прежнему одна группа, квалификатор — квалификатор
        self.assertIsNotNone(fragments.group_of("Светодиоды белого диапазона"))
        self.assertEqual(fragments.group_of("Светодиоды (в части светодиодов белого диапазона)"),
                         fragments.group_of("Светодиоды белого диапазона"))
        self.assertTrue(fragments.is_scope_qualifier("Светодиоды (в части светодиодов белого диапазона)"))


class TestReconciledMarkInGenerator(unittest.TestCase):
    """`diag_orphan_requirements._mark_reconciled`: признак — из данных, одноимённая позиция его не даёт."""

    def test_mark(self):
        import diag_orphan_requirements as G
        same = [{"component": None, "operations": [{"text": "сборка", "points": 1}]}]
        recs = [{**_rec("А", sec="T"), "requirement_blocks": same}, {**_rec("Б", sec="T"), "requirement_blocks": same},
                {**_rec("В", sec="T"), "requirement_blocks": [{"operations": [{"text": "иное"}]}]},
                {**_rec("Д", sec="T"), "requirement_blocks": same}, {**_rec("Д", sec="T"), "requirement_blocks": same}]
        groups = [{"section": "T", "positions": [{"product_name": "А"}, {"product_name": "Б"}]},
                  {"section": "T", "positions": [{"product_name": "А"}, {"product_name": "В"}]},
                  {"section": "T", "positions": [{"product_name": "А"}, {"product_name": "Д"}]},
                  {"section": "T", "positions": [{"product_name": "А"}, {"product_name": "нет такой"}]}]
        self.assertEqual(G._mark_reconciled(groups, recs), 1)
        self.assertEqual([bool(g.get("reconciled")) for g in groups], [True, False, False, False])


class TestIncompleteThresholdListCountsOnlyLost(unittest.TestCase):
    """Пометка «пороги показаны не полностью» — о ПОТЕРЯННЫХ баллах. После `D23` у 64 членов общих перечней
    есть баллы соседних строк чанка («лишние» по строке, законные по ячейке) — в список они не идут."""

    def test_corpus_list_is_empty(self):
        from scripts import verify_structured as vs
        romans = sorted({f.name.split("_")[0] for f in vs.STRUCT.glob("*.json")})
        self.assertEqual([(x["section"], x["product_name"][:40]) for x in vs.collect_incomplete(romans)], [])


class TestD18KonturServiceLine(unittest.TestCase):
    """`D18` #208: «В электронном документе нумерация сносок соответствует внесенным изменениям.» — пометка
    правовой системы, а не норма; в записи сносок она не попадает."""

    def test_service_line_dropped_from_footnotes(self):
        import load_rules_kb as L
        raw = L._normalize(L.FOOTNOTES_PATH.read_text(encoding="utf-8"))
        n = sum(1 for ln in raw.split("\n") if L.KONTUR_SERVICE_LINE_RE.match(ln))
        self.assertEqual(n, 3, "положительный контроль: в файле сносок три служебные строки")
        recs = L.parse_footnotes(L.FOOTNOTES_PATH)
        self.assertEqual([r["point"] for r in recs if "нумерация сносок" in r["text"]], [])
        by = {r["point"]: r["text"] for r in recs}
        for k in ("<53>", "<56>"):
            self.assertTrue(by[k].rstrip().endswith(")"), (k, by[k][-80:]))


if __name__ == "__main__":
    unittest.main()
