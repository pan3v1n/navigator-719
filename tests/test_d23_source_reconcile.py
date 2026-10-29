"""`D23` #251 — сверка «код → перечень» с первоисточником (`scripts/d23_groups.py`) и её следы в данных.

Инструмент читает HTML Контура (в репозитории его нет, 9 МБ), поэтому механизм проверяется на синтетических
таблицах, а результат — на самом корпусе: те позиции, ради которых правка делалась, обязаны нести полный
перечень своей ячейки. `D19` #210 п. 2 (примечание раздела) и `D18` #208 (служебная строка Контура) —
здесь же: они уехали тем же ночным релизом данных."""

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

    def test_union_better_than_every_member_is_split(self):
        """Раунд 3 ревью, H-1: мебель XI — у членов покрытие 0.56 и 0.53, у объединения 0.88; без этой ветки
        вердикт «низкое покрытие» и перечень не правился."""
        # члены покрывают РАВНЫЕ половины ячейки (0.5 и 0.5): старые ветки дали бы «низкое покрытие»
        html = _table(
            '<tr><td>31.01.12</td><td>Мебель А</td><td rowspan="2">раскрой древесных;<br>сверление заготовок;<br>'
            'облицовывание кромок;<br>сборка изделия</td></tr>'
            '<tr><td>31.02.10</td><td>Мебель Б</td></tr>')
        a = _rec("Мебель А", [("раскрой древесных", None), ("сверление заготовок", None)], 1)
        b = _rec("Мебель Б", [("облицовывание кромок", None), ("сборка изделия", None)], 2)
        rep_ = d.analyse({"T": [d.grid(_tables(html)[0])]}, {"T": [a, b]}, 0.6, 0.1)
        covers = [m["cover"] for m in rep_["T"]["shared"][0]["members"] if m["product"].startswith("Мебель")]
        self.assertEqual(len(set(covers)), 1)                 # члены равны — старые ветки раскола не видели
        self.assertLess(covers[0], 0.6)
        self.assertEqual([x["verdict"] for x in rep_["T"]["shared"]], ["расколот"])

    def test_single_parse_taken_when_it_carries_everything(self):
        full = _rec("Полный", [("операция один", None), ("операция два", None)], 1)
        part = _rec("Часть", [("операция один", None)], 2)
        blocks, how = d.full_list([full, part], "операция один;\nоперация два")
        self.assertTrue(how.startswith("разбор"), how)
        self.assertEqual(len([o for b in blocks for o in b["operations"]]), 2)

    def test_dated_variant_in_block_label_is_not_covered(self):
        """M1 ревью: у VII-35 вариант «до 1 января 2019 г.: … не более 50 процентов» стоял в подписи блока, а его
        операции «покрывал» блок «с 1 января 2019 г.» донора — вариант терялся."""
        ops = [(f"операция {w}", None) for w in ("сборка", "наладка", "испытания", "упаковка")]
        donor = _rec("Донор", ops, 37, comp="с 1 января 2019 г.: соблюдение доли не более 70 процентов")
        donor["requirement_blocks"].append({"component": "прочие условия", "operations": [
            {"text": f"дополнительная операция {i}", "points": None} for i in range(6)]})
        other = _rec("Член", ops, 35, comp="до 1 января 2019 г.: соблюдение доли не более 50 процентов")
        text = ("до 1 января 2019 г.: соблюдение доли не более 50 процентов\nс 1 января 2019 г.: соблюдение доли не "
                "более 70 процентов\n" + "\n".join(t for t, _ in ops) + "\n"
                + "\n".join(f"дополнительная операция {i}" for i in range(6)))
        blocks, how = d.full_list([other, donor], text)
        self.assertEqual(how, "объединение блоков членов")
        self.assertIn("до 1 января 2019 г.: соблюдение доли не более 50 процентов",
                      [b.get("component") for b in blocks])

    def test_pointless_list_keeps_member_type(self):
        donor = {**_rec("Донор", [("сборка", None), ("наладка", None)], 1), "requirement_type": "mixed"}
        self.assertIsNone(d._rtype(donor["requirement_blocks"], [donor, _rec("Член", [], 2)]))
        pts = {**_rec("Донор", [("сборка", 5)], 1), "requirement_type": "operations"}
        self.assertEqual(d._rtype(pts["requirement_blocks"], [pts]), "mixed")

    def test_before_and_since_dates_are_different_variants(self):
        """Раунд 2 ревью, M1: у VII-35 и VII-37 подписи «до 1 января 2019 г.» и «Требования с 1 января 2019 г.» —
        числа те же, слова совпадают на 4 из 5; различает только «до» / «с»."""
        self.assertFalse(d._op_covered(d.norm("до 1 января 2019 г."), [d.norm("Требования с 1 января 2019 г.")]))
        self.assertTrue(d._op_covered(d.norm("с 1 января 2019 г."), [d.norm("Требования с 1 января 2019 г.")]))

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
    """Контур режет ячейку на куски по строкам кода; законченная ячейка кончается без знака, оборванная — на
    «;», «:» или «,». Голова новой ячейки (права на документацию / частая голова раздела) склейку останавливает."""

    @staticmethod
    def _grp(code, req):
        return {"code": code, "codes": [code], "rows": [{"req": req, "name": code}], "excluded": False}

    def _join(self, a, b, heads=None):
        return d._join_continuations([self._grp("1", a), self._grp("2", b)], {} if heads is None else heads)

    def test_comma_tail_joins(self):
        self.assertEqual(len(self._join(f"{OPENER};\nправа использования товарного знака,",
                                        "правообладателем которого является субъект")), 1)

    def test_lowercase_after_unfinished_phrase_joins(self):
        self.assertEqual(len(self._join(f"{OPENER};\nнаправлена на комплексное предотвращение",
                                        "и (или) минимизацию негативного воздействия")), 1)

    def test_points_operation_after_semicolon_joins(self):
        """Суда XVIII — одна ячейка в 7 кусках: «… монтаж электрооборудования (95 баллов);» → «монтаж судовой
        системы хранения сжиженного газа (250 баллов);» (ревью PR D23, H2: прежнее правило резало её на части)."""
        self.assertEqual(len(self._join(f"{OPENER};\nмонтаж электрооборудования (95 баллов);",
                                        "монтаж судовой системы хранения сжиженного газа (250 баллов);")), 1)

    def test_same_opener_with_other_subject_joins(self):
        """Аддитивные установки I: «… на срок не менее 5 лет;» → «наличие у субъекта … в уставном капитале доли
        резидентов РФ:» — зачин тот же, но речь не о правах на документацию."""
        self.assertEqual(len(self._join(
            f"{OPENER} на срок не менее 5 лет;",
            "наличие у субъекта деятельности в сфере промышленности - налогового резидента в уставном капитале "
            "доли резидентов Российской Федерации:")), 1)

    def test_rights_head_after_semicolon_starts_new_cell(self):
        """«Смолы эпоксидные» XXI кончаются на «;» (неряшливость источника), а следом своя ячейка с правами."""
        self.assertEqual(len(self._join("наличие разрешительных документов на объект;", f"{OPENER} на срок 5 лет;")), 2)
        self.assertEqual(len(self._join("сборка;", "наличие у изготовителя учтенных копий конструкторской "
                                                   "документации по ЕСКД;")), 2)

    def test_learned_section_head_after_colon_starts_new_cell(self):
        """IX: «… расчет баллов осуществляется по формуле:» → «1. соответствие заявителя …» — частая голова раздела."""
        heads = {d.head_key("1. соответствие заявителя следующим обязательным условиям:"): 19}
        self.assertEqual(len(self._join("при этом расчет баллов осуществляется по формуле:",
                                        "1. соответствие заявителя следующим обязательным условиям:", heads)), 2)

    def test_quote_closed_amendment_tail_is_finished(self):
        """«… (3 балла)";» — закрывающая кавычка вставки поправки: ячейка закончена."""
        self.assertEqual(len(self._join('сборка (обязательная операция) (3 балла)";', "сборка изделия;")), 2)

    def test_finished_sentence_does_not_join(self):
        self.assertEqual(len(self._join(f"{OPENER}.", "сборка изделия")), 2)
        self.assertEqual(len(self._join("контрольно-измерительные операции (10 баллов)", "сборка изделия")), 2)

    def test_preamble_only_chunk_continues(self):
        """Раунд 2 ревью, M2: мебель XI — кусок из одних прав («… на срок не менее 5 лет») продолжается куском
        «осуществление на территории РФ следующих операций»; бельё XVII, склады III («… 5 лет <6>»)."""
        self.assertEqual(len(self._join(f"{OPENER} на срок не менее 5 лет",
                                        "осуществление на территории Российской Федерации следующих операций:")), 1)
        self.assertEqual(len(self._join(f"{OPENER} на срок не менее 5 лет <6>",
                                        "наличие на территории одной из стран - членов ЕАЭС сервисного центра")), 1)

    def test_preamble_only_chunk_then_head_starts_new_cell(self):
        """Законченная ячейка из одних прав бывает (V, VI, XVI) — её защищает только `_is_head`."""
        self.assertEqual(len(self._join(f"{OPENER} на срок не менее 5 лет", f"{OPENER} на срок 3 года;")), 2)

    def test_chunk_with_operations_ending_in_word_is_finished(self):
        """Законченная ячейка тоже кончается словом («… упаковка») — кусок с операциями не продолжается."""
        self.assertEqual(len(self._join(f"{OPENER};\nсборка;\nупаковка",
                                        "осуществление на территории Российской Федерации следующих операций:")), 2)

    def test_or_start_continues(self):
        """VII: сфигмоманометры «… <4>» → капнографы «или\nналичие у юридического лица …» — альтернатива."""
        self.assertEqual(len(self._join("до 1 января 2019 г.:\nсоблюдение доли - не более 50 процентов <4>",
                                        f"или\n{OPENER}")), 1)
        # и тогда, когда предыдущий кусок — с операциями (правило вводного куска здесь не сработало бы)
        self.assertEqual(len(self._join("сборка;\nупаковка <4>", f"или\n{OPENER}")), 1)

    def test_without_heads_only_comma_and_broken_phrase(self):
        out = d._join_continuations([self._grp("1", "сборка;"), self._grp("2", "покраска;")], None)
        self.assertEqual(len(out), 2)

    def test_joined_chunk_does_not_open_new_list(self):
        """Стык склейки не режется обратно на перечни по зачину (см. `groups_of`): кусок I-121 начинается с
        «наличие у субъекта …»."""
        rows = d.grid(_tables(_table(
            f'<tr><td>28.41.34.100</td><td>Изделие А</td><td>{OPENER} на срок не менее 5 лет;</td></tr>'
            '<tr><td colspan="3">(в ред. Постановления Правительства РФ от 01.01.2025 N 1)</td></tr>'
            '<tr><td>28.96.10.121</td><td>Изделие Б</td><td>наличие у субъекта деятельности в сфере промышленности - '
            'налогового резидента в уставном капитале доли резидентов Российской Федерации:</td></tr>'
            '<tr><td>28.96.10.122</td><td>Изделие В</td><td>для акционерных обществ - не менее 75 процентов</td></tr>'))[0])
        groups = d.groups_of(rows, heads={})
        mine = [g for g in groups if "28.41.34.100" in g["codes"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["codes"], ["28.41.34.100", "28.96.10.121", "28.96.10.122"])
        self.assertEqual(len(mine[0]["segments"]), 1)
        self.assertEqual(mine[0]["segments"][0]["products"], ["Изделие А", "Изделие Б", "Изделие В"])


class TestCellThreshold(unittest.TestCase):
    """Порог самой ячейки из `min_threshold` члена — соседям без своего порога (раунд 3 ревью, H-2 / M-1)."""

    CELL = ("наличие прав на документацию;\nс 1 января 2016 г. не менее 8, с 1 января 2018 г. - не менее 10, "
            "с 1 января 2020 г. - не менее 12 из следующих операций:\nсборка;\nсварка")
    THR = "не менее 8, с 1 января 2018 г. - не менее 10, с 1 января 2020 г. - не менее 12"

    def _recs(self, thr):
        donor = {**_rec("Шасси", [("сборка", None)], 116), "min_threshold": thr}
        return [donor, _rec("Шасси", [], 117)]

    def test_threshold_of_cell_found(self):
        self.assertEqual(d.cell_threshold(self._recs(self.THR), self.CELL), self.THR)

    def test_operation_with_points_is_not_a_threshold(self):
        """III-61: «с 1 января 2024 г. - на уровне не менее 70 процентов (25 баллов)» — операция ячейки."""
        thr = "с 1 января 2024 г. - на уровне не менее 70 процентов (25 баллов)"
        self.assertIsNone(d.cell_threshold(self._recs(thr), self.CELL + "\n" + thr))

    def test_threshold_absent_from_cell_is_not_shared(self):
        self.assertIsNone(d.cell_threshold(self._recs("не менее 90 баллов"), self.CELL))

    def test_two_candidates_share_nothing(self):
        a = {**_rec("А", [("сборка", None)], 1), "min_threshold": "не менее 10"}
        b = {**_rec("Б", [("сборка", None)], 2), "min_threshold": "не менее 12"}
        self.assertIsNone(d.cell_threshold([a, b], self.CELL))


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

    def test_group_of_several_lists_shares_nothing(self):
        """L4 ревью: коды сравниваются с кодами ВСЕЙ группы — при нескольких перечнях в группе это не
        доказательство для одного из них."""
        rep, qual = self._report("из 26.11.22.200 | из 26.11.22.210 | 26.11.22.216")
        rep["IV"]["shared"][0]["group_segments"] = 2
        self.assertEqual(d.share_cell_thresholds(rep, self.NOTES), [])

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

    def _texts(self, r):
        return [(o.get("text") or "", o.get("points")) for o in self._ops(r)]

    def _all_text(self, r):
        return " ".join((b.get("component") or "") + " " + (b.get("note") or "") + " "
                        + " ".join(o.get("text") or "" for o in b.get("operations") or [])
                        for b in r.get("requirement_blocks") or [])

    # Факты ниже — ЦИТАТЫ ячеек HTML Контура 508574, а не вывод инструмента: равенство блоков членов
    # `reconcile` обеспечивает сам по построению и как проверка ничего не доказывает (ревью PR D23).

    def test_combines_carry_whole_list(self):
        """«Комбайны зерноуборочные» несли 1 операцию из перечня, остальное — «Машинокомплекты»."""
        a = self._one("III", "Комбайны зерноуборочные")
        self.assertGreaterEqual(len(self._ops(a)), 170)

    def test_ships_carry_one_list_from_head_to_tail(self):
        """Суда XVIII — одна ячейка в 7 кусках (H2 ревью): у газовоза, сухогруза и ледокола — от «закладка судна
        (обязательное условие)» до «сдача судна (обязательное условие)», с газовой системой на 250 баллов."""
        for name in ("Суда наливные морские", "Суда сухогрузные речные самоходные", "Ледоколы", "Суда круизные морские"):
            r = self._one("XVIII", name)
            joined = self._all_text(r)
            self.assertIn("закладка судна (обязательное условие)", joined, name)
            self.assertIn("сдача судна (обязательное условие)", joined, name)
            self.assertIn(("монтаж судовой системы хранения сжиженного газа", 250),
                          [(t[:48], p) for t, p in self._texts(r)], name)

    def test_pesticides_carry_operations_of_the_cell(self):
        """XXI 20.20.1…20.20.15 — одна ячейка (H2): у «Гербицидов» есть лаборатория (кусок 20.20.13) и
        операции куска 20.20.15."""
        joined = self._all_text(self._one("XXI", "Гербициды"))
        self.assertIn("аналитической лаборатории", joined)
        self.assertIn("измельчение", joined)

    def test_additive_installations_carry_operations_of_the_cell(self):
        """I-24…I-27 — одна ячейка (H3): у «фотополимеризации» — доля в уставном капитале И «сборка (3 балла)»."""
        # «для акционерных обществ - не менее 75 процентов …» разборщик положил у I-26 в `min_threshold` — это
        # класс `D24` #267 (текст ячейки не в своём поле), блоками его правка не переносит
        r = self._one("I", "Аддитивные установки фотополимеризации")
        self.assertIn("доли резидентов Российской Федерации", self._all_text(r))
        self.assertIn(("сборка", 3), [(t.strip(), p) for t, p in self._texts(r)])

    def test_record_thresholds_not_copied_between_members(self):
        """H1 ревью: порог записи одного члена перекрывал верный порог примечания у соседей."""
        from app.rag import thresholds as T

        def eff(sec, prefix):
            r = self._one(sec, prefix)
            return r.get("min_threshold") or T.lookup_threshold(r["okpd2_codes"], r["product_name"], sec) or ""

        self.assertIn("[прим. 26]", eff("IX", "Машины вычислительные электронные цифровые, поставляемые"))
        self.assertIn("[прим. 26]", eff("IX", "Машины вычислительные электронные цифровые прочие"))
        self.assertIn("[прим. 32", eff("XXVII", "Игры прочие"))
        self.assertIn("[прим. 56", eff("I", "Аддитивные установки фотополимеризации"))

    def test_group_threshold_kept_after_own_list(self):
        """Раунд 2 ревью, H1: насосы XIX-2/3/9 показывали «порог группы» (условие о числе операций у XIX-1) через
        наследование; со своим перечнем наследование не срабатывает — условие обязано остаться."""
        for name in ("Насосы смазочные", "Насосы для охлаждающей жидкости", "Насосы питательные и конденсатные"):
            self.assertIn("не менее 4 технологических операций", self._one("XIX", name).get("min_threshold") or "", name)

    def test_cell_threshold_given_to_members_without_one(self):
        """Раунд 3 ревью: III-117 — «не менее 8 / 10 / 12 из» (без него стояла ложная R7), XIX-4/10 — условие о
        числе операций, VI-39 «Дробилки прочие» — 60 / 80 / 100 баллов."""
        iii117 = next(r for r in self.recs["III"] if r["source_anchor"].endswith("позиция 117"))
        self.assertIn("не менее 12", iii117.get("min_threshold") or "")
        for name in ("Насосы возвратно-поступательные объемного действия", "Насосы двухстороннего входа"):
            self.assertIn("не менее 4 технологических операций", self._one("XIX", name).get("min_threshold") or "", name)
        self.assertIn("не менее 100 баллов", self._one("VI", "Дробилки прочие").get("min_threshold") or "")

    def test_furniture_cells_carry_operations(self):
        """Раунд 3 ревью, H-1: у XI-4 «Мебель деревянная для офисов» были только права и ложная строка R7."""
        r = self._one("XI", "Мебель деревянная для офисов")
        self.assertTrue([o for o in self._ops(r) if o.get("points")], "у XI-4 нет операций с баллами")

    def test_type_not_overwritten_by_pointless_list(self):
        """Раунд 2 ревью, L3: у ОФЭКТ VII-6 тип донора «mixed» снимал строку R7 «Порог: не предусмотрен»."""
        self.assertEqual(self._one("VII", "Системы однофотонной эмиссионной").get("requirement_type"), "operations")

    def test_point_transfer_guard_skips_only_unified_lists(self):
        """Раунд 3 ревью, L-2: «лишние» баллы не считаются только у перечней, сведённых к ОДНОМУ (`same_list`);
        у нетронутых «целых» чужой балл обязан оставаться видимым."""
        import tempfile
        from scripts import verify_structured as vs
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(json.dumps({"lists": [{"same_list": True, "members": [{"anchor": "A"}, {"anchor": "B"}]},
                                               {"same_list": False, "members": [{"anchor": "C"}]}]}), encoding="utf-8")
            self.assertEqual(vs._shared_cell_anchors(p), {"A", "B"})

    def test_point_transfer_guard_is_quiet_on_corpus(self):
        """Раунд 2 ревью, M3: сторож D8 (`check_records`) после правки давал 115 ложных «лишних» у членов общих
        ячеек; у них «лишние» законны — на корпусе обязан быть 0."""
        from scripts import verify_structured as vs
        romans = sorted({f.name.split("_")[0] for f in vs.STRUCT.glob("*.json")})
        self.assertEqual(sum(vs.check_records(sec)[1] for sec in romans), 0)

    def test_transmitters_44_45_get_list_of_43(self):
        iv = {r["source_anchor"].rsplit(" ", 1)[-1]: r for r in self.recs["IV"]}
        self.assertTrue(self._ops(iv["43"]))
        self.assertIn("сборка приемо-передающих устройств", self._all_text(iv["44"]))
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

    def test_freons_without_neighbour_labels(self):
        a = self._one("XXI", "Хладон-218")
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
    """Пометка «перечень неполный» снимается только у групп, полнота которых доказана манифестом сверки
    (`shared_cells.json`), — а состав группы и роли строк остаются."""

    def test_reconciled_only_with_manifest_proof(self):
        import diag_orphan_requirements as G
        from app.rag import fragments
        groups = json.loads((ROOT / "knowledge_base/pp719/fragmented_requirements.json").read_text(encoding="utf-8"))
        verified = G._manifest_lists()
        self.assertTrue(verified, "манифест сверки пуст или отсутствует")

        def norm(n):
            return " ".join(n.split("\n")[0].split()).lower()

        for g in groups:
            if not g.get("reconciled"):
                continue
            names = {norm(p["product_name"]) for p in g["positions"]}
            proof = [m for m in verified if m["section"] == g["section"]
                     and all(any(norm(x["product_name"]).startswith(n) for x in m["members"]) for n in names)]
            self.assertTrue(proof, (g["section"], sorted(names)))
        # аддитивные установки I: до `D24` в перечне не было «не менее 75 процентов … трех четвертей» (лежали в
        # `min_threshold` I-26) — полнота не доказана, пометка стояла (раунд 2 ревью, H2). `D24` вернул строки в блоки:
        # полнота против ячейки 1.0, чисел не хватает 0 — группа сверена ДАННЫМИ манифеста, а не копией перечня
        add = [m for m in verified if any(x["product_name"].startswith("Аддитивные установки фотополимеризации")
                                          for x in m["members"])]
        self.assertEqual([(m["recall"], m["numbers_missing"]) for m in add], [(1.0, [])])
        self.assertFalse(fragments.is_fragmented("Аддитивные установки фотополимеризации в ванне"))
        # EV6/EV7: обрывок и продукт одной ячейки — по-прежнему одна группа, квалификатор — квалификатор
        self.assertEqual(fragments.group_of("Светодиоды (в части светодиодов белого диапазона)"),
                         fragments.group_of("Светодиоды белого диапазона"))
        self.assertTrue(fragments.is_scope_qualifier("Светодиоды (в части светодиодов белого диапазона)"))


class TestReconciledMarkInGenerator(unittest.TestCase):
    """`diag_orphan_requirements._mark_reconciled`: одинаковый перечень — НЕ доказательство (его даёт сама правка);
    нужен перечень манифеста с полнотой против ячейки, одноимённая позиция признака не даёт."""

    def test_mark(self):
        import diag_orphan_requirements as G
        same = [{"component": None, "operations": [{"text": "сборка", "points": 1}]}]
        recs = [{**_rec("А", sec="T"), "requirement_blocks": same}, {**_rec("Б", sec="T"), "requirement_blocks": same},
                {**_rec("В", sec="T"), "requirement_blocks": [{"operations": [{"text": "иное"}]}]},
                {**_rec("Д", sec="T"), "requirement_blocks": same}, {**_rec("Д", sec="T"), "requirement_blocks": same},
                {**_rec("Е", sec="T"), "requirement_blocks": same}, {**_rec("Ж", sec="T"), "requirement_blocks": same}]
        manifest = [{"section": "T", "recall": 1.0, "members": [{"product_name": n} for n in "АБВД"]}]
        groups = [{"section": "T", "positions": [{"product_name": "А"}, {"product_name": "Б"}]},
                  {"section": "T", "positions": [{"product_name": "А"}, {"product_name": "В"}]},
                  {"section": "T", "positions": [{"product_name": "А"}, {"product_name": "Д"}]},
                  {"section": "T", "positions": [{"product_name": "А"}, {"product_name": "нет такой"}]},
                  {"section": "T", "positions": [{"product_name": "Е"}, {"product_name": "Ж"}]}]
        self.assertEqual(G._mark_reconciled(groups, recs, manifest), 1)
        self.assertEqual([bool(g.get("reconciled")) for g in groups], [True, False, False, False, False])
        self.assertEqual(G._mark_reconciled(groups, recs, []), 0)          # без манифеста — ни одной пометки

    def test_manifest_floor(self):
        import tempfile
        import diag_orphan_requirements as G
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(json.dumps({"min_recall": 0.9, "lists": [
                {"section": "T", "recall": 0.89, "members": []},
                {"section": "T", "recall": 0.95, "members": []},
                {"section": "T", "recall": 0.97, "members": [], "numbers_missing": ["75"]}]}), encoding="utf-8")
            self.assertEqual([m["recall"] for m in G._manifest_lists(p)], [0.95])

    def test_numbers_are_a_multiset(self):
        """Раунд 3 ревью, M-2: одно «2018» в другой операции не «покрывает» потерянную метку даты у доли."""
        r = _rec("А", [("с 1 января 2018 г. не менее 5 из операций", None), ("доля не более 40 процентов", None)], 1)
        cell = "с 1 января 2018 г. не менее 5 из операций; с 1 января 2018 г. доля не более 40 процентов"
        self.assertEqual(sorted((d._cell_numbers(cell) - d._record_numbers(r)).elements()), ["1", "2018"])

    def test_item_numbering_is_not_a_number(self):
        """Раунд 3 ревью, L-1: «1.1.1.11» — номер пункта, не число ячейки."""
        self.assertEqual(d._numbers("1.1.1.11 наличие сервисного центра; 2.4 сборка (5 баллов)"), ["5"])

    def test_numbers_of_cell_counted_with_points(self):
        """Числа ячейки ищутся и в тексте блоков, и в баллах операций; даты поправок «(в ред. …)» не считаются."""
        r = _rec("А", [("сборка", 25), ("доля не менее 75 процентов", None)], 1)
        cell = "сборка (25 баллов); доля не менее 75 процентов; трех четвертей 3 (в ред. Постановления от 15.12.2025 N 2033)"
        self.assertEqual(sorted((d._cell_numbers(cell) - d._record_numbers(r)).elements()), ["3"])


class TestIncompleteThresholdListCountsOnlyLost(unittest.TestCase):
    """Пометка «пороги показаны не полностью» — о ПОТЕРЯННЫХ баллах. После `D23` у 64 членов общих перечней
    есть баллы соседних строк чанка («лишние» по строке, законные по ячейке) — в список они не идут."""

    def test_corpus_list_is_empty(self):
        from scripts import verify_structured as vs
        romans = sorted({f.name.split("_")[0] for f in vs.STRUCT.glob("*.json")})
        self.assertEqual([(x["section"], x["product_name"][:40]) for x in vs.collect_incomplete(romans)], [])


class TestD24LostLines(unittest.TestCase):
    """`D24` #267: строки ячейки, которых нет в записи, встают в неё по месту; совпавшие — не задваиваются."""

    @staticmethod
    def _apply(r, text):
        units, skipped = d.lost_units(r, text)
        n = d.apply_units(r, units)
        return n, skipped

    @staticmethod
    def _flat(r):
        out = []
        for b in r["requirement_blocks"]:
            if b.get("component"):
                out.append(("▸", b["component"]))
            out += [("•", o["text"], o.get("points")) for o in b.get("operations") or []]
        return out

    def test_lost_preamble_goes_first(self):
        """«Бульдозеры» III-2: права, сервисный центр, условие <18> — до первого совпавшего блока."""
        r = {**_rec("Бульдозеры", [("литье заготовок несущей рамы", 4)], 2, comp="несущая рама"), "requirement_type": "points"}
        text = (f"{OPENER};\nналичие сервисного центра, уполномоченного осуществлять ремонт;\n"
                "несущая рама:\nлитье заготовок несущей рамы (4 балла);")
        n, _ = self._apply(r, text)
        self.assertEqual(n, 2)
        self.assertEqual([b.get("component") for b in r["requirement_blocks"]][:2],
                         [OPENER, "наличие сервисного центра, уполномоченного осуществлять ремонт"])
        self.assertEqual(r["requirement_blocks"][2]["component"], "несущая рама")

    def test_lost_header_with_points_becomes_block(self):
        """«Автомобили для гольфа» III: «несущая рама (корпус):» + операции с баллами — новый блок."""
        r = _rec("Гольф", [(OPENER, None)], 1)
        # перед заголовком — потерянное требование без «;»: заголовок не приклеивается к нему переносом строки
        text = (f"{OPENER};\nналичие сервисного центра на территории России\nнесущая рама (корпус):\n"
                "использование металлопроката для рамы (3 балла);\nлитье заготовок рамы\n(2 балла);\n"
                "сварка заготовок рамы - 4 балла")
        self._apply(r, text)
        self.assertEqual(r["requirement_blocks"][-2]["component"], "наличие сервисного центра на территории России")
        blk = r["requirement_blocks"][-1]
        self.assertEqual(blk["component"], "несущая рама (корпус):")
        self.assertEqual([(o["text"], o["points"]) for o in blk["operations"]],
                         [("использование металлопроката для рамы", 3), ("литье заготовок рамы", 2),
                          ("сварка заготовок рамы", 4)])

    def test_header_with_group_points_is_header(self):
        """XXIII-1 «… всех следующих операций (при наличии) (13 баллов):» — баллы за весь список: подпись блока, не
        операция на 13 баллов рядом с условием «13 баллов за выполнение всех операций»."""
        r = _rec("Башня", [("наличие сервисного центра", None)], 1)
        self._apply(r, "наличие сервисного центра;\nосуществление всех следующих технологических операций (13 баллов):\n"
                       "вальцевание обечаек башни;\nсварка обечаек башни")
        blk = r["requirement_blocks"][-1]
        self.assertEqual(blk["component"], "осуществление всех следующих технологических операций (13 баллов):")
        self.assertEqual([(o["text"], o["points"]) for o in blk["operations"]],
                         [("вальцевание обечаек башни", None), ("сварка обечаек башни", None)])

    def test_split_and_truncated_lines_are_not_lost(self):
        """Строку разборщик разбил на две операции / снабдил пометкой — это не потеря, дубля нет; обрезал — обрывок
        получает текст строки (ревью D24, M4: «элемент внутри строки» засчитывал строку целиком)."""
        r = _rec("А", [("литье ковка штамповка заготовок рамы", 4), ("раскрой гибка сварка заготовок рамы", 4),
                       ("применение электронных модулей российского производства при этом расчет баллов", None),
                       ("применение печатных плат произведенных в России: не менее одной печатной платы", 5)], 1)
        text = ("литье ковка штамповка (4 балла), раскрой гибка сварка (4 балла) заготовок рамы;\n"
                "применение электронных модулей российского производства, при этом расчет баллов осуществляется "
                "по формуле со сложными коэффициентами;\n"
                "2.1.1. не менее одной печатной платы (5 баллов, обязательное требование)")
        n, _ = self._apply(r, text)
        self.assertEqual(n, 1)
        ops = r["requirement_blocks"][0]["operations"]
        self.assertEqual(len(ops), 4)                                                   # дубля нет
        self.assertEqual(ops[2]["text"], "применение электронных модулей российского производства, при этом расчет "
                                         "баллов осуществляется по формуле со сложными коэффициентами")

    def test_line_split_into_two_short_operations_is_not_lost(self):
        """Обе половины короче четырёх слов — их узнаёт только пара соседних элементов."""
        r = _rec("А", [("литье ковка штамповка", 4), ("раскрой гибка сварка", 4)], 1)
        self.assertEqual(self._apply(r, "литье, ковка, штамповка, раскрой, гибка, сварка;")[0], 0)

    def test_formula_and_its_header_are_skipped(self):
        r = _rec("Принтер", [("сборка изделия", 10)], 1)
        text = "сборка изделия (10 баллов);\nрасчет баллов по формуле:\nгде К - количество модулей,\nделенное на общее количество"
        n, skipped = self._apply(r, text)
        self.assertEqual(n, 0)
        self.assertEqual([w for w, _ in skipped], ["формула (заголовок)", "формула", "формула"])

    def test_several_points_in_one_line_are_not_split(self):
        r = _rec("Погрузчик", [("сборка", 1)], 1)
        n, skipped = self._apply(r, "сборка (1 балл);\nпроизводство гидроцилиндров стрелы (3 балла), рукояти (3 балла)")
        self.assertEqual(n, 0)
        self.assertEqual([w for w, _ in skipped], ["несколько баллов"])

    def test_operations_joined_by_semicolon_are_split(self):
        """XXII-11: три операции одной строкой через «;» — по строке на каждую; уже записанная не задваивается."""
        r = _rec("Колонка", [("изготовление блока управления колонки", 30)], 1)
        n, skipped = self._apply(r, "изготовление блока управления колонки (30 баллов); производство заправочного "
                                    "устройства колонки (10 баллов); производство заправочных рукавов (10 баллов)")
        self.assertEqual((n, skipped), (2, []))
        self.assertEqual([(o["text"], o["points"]) for o in r["requirement_blocks"][0]["operations"]],
                         [("изготовление блока управления колонки", 30), ("производство заправочного устройства колонки", 10),
                          ("производство заправочных рукавов", 10)])

    def test_items_under_matched_header_join_its_block(self):
        """«…доли резидентов РФ:» есть в записи, «для акционерных обществ …» — нет: становится операцией блока."""
        r = _rec("А", [], 1)
        r["requirement_blocks"] = [{"component": "наличие в уставном капитале доли резидентов Российской Федерации:",
                                    "operations": []}]
        self._apply(r, "наличие в уставном капитале доли резидентов Российской Федерации:\n"
                       "для акционерных обществ - не менее 75 процентов;\nдля обществ с ограниченной ответственностью")
        ops = r["requirement_blocks"][0]["operations"]
        self.assertEqual([o["text"] for o in ops], ["для акционерных обществ - не менее 75 процентов",
                                                    "для обществ с ограниченной ответственностью"])

    def test_items_under_header_whose_colon_parser_dropped(self):
        """I-26: разборщик снял «:» у «…доли резидентов Российской Федерации», в ячейке оно есть — пункты под ним
        остаются пунктами этого требования, а не тремя отдельными «обязательными»."""
        r = _rec("А", [], 1)
        r["requirement_blocks"] = [{"component": "наличие в уставном капитале доли резидентов Российской Федерации",
                                    "operations": []}]
        self._apply(r, "наличие в уставном капитале доли резидентов Российской Федерации:\n"
                       "для акционерных обществ - не менее 75 процентов;\nдля обществ с ограниченной ответственностью")
        self.assertEqual(len(r["requirement_blocks"]), 1)
        self.assertEqual(len(r["requirement_blocks"][0]["operations"]), 2)

    def test_requirement_lines_without_semicolon_are_not_glued(self):
        """III (32 позиции): у строки сервисного центра в HTML нет «;», следующая «при производстве осуществляется
        комплекс …» — отдельное требование, а не перенос строки."""
        r = _rec("Комбайны", [("сборка комбайна", 5)], 1)
        text = ("наличие сервисного центра, уполномоченного осуществлять ремонт и обслуживание продукции\n"
                "при производстве осуществляется комплекс операций по изготовлению компонентов\nсборка комбайна (5 баллов)")
        self._apply(r, text)
        comps = [b.get("component") for b in r["requirement_blocks"]]
        self.assertEqual(comps[:2], ["наличие сервисного центра, уполномоченного осуществлять ремонт и обслуживание "
                                     "продукции", "при производстве осуществляется комплекс операций по изготовлению "
                                                  "компонентов"])

    def test_wrapped_line_of_one_requirement_is_glued(self):
        """Перенос строки внутри одного требования (VII-111: «… Требования для целей регулирования"» + «или
        соответствующего международного стандарта ISO 13485») — одна строка блока."""
        r = _rec("Иглы", [("сборка игл", 5)], 1)
        text = ('наличие сертификата системы менеджмента качества ГОСТ "Изделия медицинские"\n'
                "или соответствующего международного стандарта ISO 13485 (если применимо);\nсборка игл (5 баллов)")
        self._apply(r, text)
        self.assertEqual(len(r["requirement_blocks"]), 2)
        self.assertIn("ISO 13485", r["requirement_blocks"][0]["component"])

    def test_idempotent(self):
        r = _rec("Бульдозеры", [("литье заготовок несущей рамы", 4)], 2, comp="несущая рама")
        text = f"{OPENER};\nнесущая рама:\nлитье заготовок несущей рамы (4 балла);\nсварка рамы (6 баллов)"
        self._apply(r, text)
        before = json.dumps(r, ensure_ascii=False)
        self.assertEqual(self._apply(r, text)[0], 0)
        self.assertEqual(json.dumps(r, ensure_ascii=False), before)


def _blk(comp, ops=(), note=None):
    return {"component": comp, "operations": [{"text": t, "points": p} for t, p in ops], "note": note}


class TestD24Review4(unittest.TestCase):
    """Ревью D24, раунд 4: выравнивание по порядку, склейки HTML, порог в поле порога, вводные на своём месте."""

    _apply = staticmethod(TestD24LostLines._apply)

    @staticmethod
    def _r(*blocks, thr=None):
        r = _rec("А", [], 1)
        r["requirement_blocks"], r["min_threshold"] = list(blocks), thr
        return r

    @staticmethod
    def _comps(r):
        return [b.get("component") for b in r["requirement_blocks"]]

    def test_identical_lines_align_in_order(self):
        """XXVI-8: одинаковая строка у нескольких узлов вставала к ПОСЛЕДНЕМУ из равных — и потерянная строка
        первого узла уходила в чужой узел."""
        r = self._r(_blk("рама", [("заготовительные операции узла", 10)]),
                    _blk("кабина", [("заготовительные операции узла", 10)]))
        self._apply(r, "рама:\nзаготовительные операции узла (10 баллов);\nсварка рамы изделия (5 баллов);\n"
                       "кабина:\nзаготовительные операции узла (10 баллов)")
        self.assertEqual([o["text"] for o in r["requirement_blocks"][0]["operations"]],
                         ["заготовительные операции узла", "сварка рамы изделия"])
        self.assertEqual(len(r["requirement_blocks"][1]["operations"]), 1)

    def test_points_choose_the_operation(self):
        """Баллы строки и операции не совпали — это не пара: «литье заготовок (6 баллов)» — операция кабины."""
        r = self._r(_blk("рама", [("литье заготовок", 4)]), _blk("кабина", [("литье заготовок", 6)]))
        self._apply(r, "литье заготовок (6 баллов);\nсварка кабины изделия (2 балла)")
        self.assertEqual([o["text"] for o in r["requirement_blocks"][1]["operations"]],
                         ["литье заготовок", "сварка кабины изделия"])

    def test_general_requirement_is_not_taken_by_node_requirement(self):
        """XXVI-8: общее «наличие … прав на документацию» засчитывалось по требованию узла муфты, и общего не было."""
        node = ("наличие у юридического лица - налогового резидента прав на технологию, конструкторскую и техническую "
                "документацию в объеме, достаточном для производства")
        r = self._r(_blk("муфта", [(node, 150), ("сварка муфты изделия", 50)]))
        general = ("наличие у юридического лица - налогового резидента прав на конструкторскую и техническую "
                   "документацию в объеме, достаточном для производства")
        n, _ = self._apply(r, f"{general};\nмуфта:\n{node} (150 баллов);\nсварка муфты изделия (50 баллов)")
        self.assertEqual(n, 1)
        self.assertEqual(self._comps(r), [general, "муфта"])

    def test_repeated_schedule_of_next_node_is_restored(self):
        """XX-12: график повторён для каждой установки — копия второй установки не «находится» в первой."""
        r = self._r(_blk("установка первая", [("соблюдение доли иностранных деталей установки первой", None),
                                              ("до 31 декабря 2027 г. - не более 35 процентов", None),
                                              ("с 1 января 2028 г. - не более 25 процентов", None)]),
                    _blk("установка вторая", [("соблюдение доли иностранных деталей установки второй", None)]))
        cell = ("установка первая:\nсоблюдение доли иностранных деталей установки первой:\n"
                "до 31 декабря 2027 г. - не более 35 процентов;\nс 1 января 2028 г. - не более 25 процентов;\n"
                "установка вторая:\nсоблюдение доли иностранных деталей установки второй:\n"
                "до 31 декабря 2027 г. - не более 35 процентов;\nс 1 января 2028 г. - не более 25 процентов")
        self.assertEqual(self._apply(r, cell)[0], 2)
        # пункты под требованием «…:» — его текстом, как у разборщика: операциями график вытеснял за кап баллы
        self.assertEqual([o["text"] for o in r["requirement_blocks"][1]["operations"]],
                         ["соблюдение доли иностранных деталей установки второй: до 31 декабря 2027 г. - не более 35 "
                          "процентов; с 1 января 2028 г. - не более 25 процентов"])
        self.assertEqual(len(r["requirement_blocks"][0]["operations"]), 3)

    def test_glued_line_of_existing_elements_is_not_inserted(self):
        """IX-111: у HTML пропал перенос — три строки записи одной строкой; четвёртой операции на 20 баллов нет. Ни
        одна часть не покрывает и половины строки — узнаёт только окно; «Российской Федерации» строки против «РФ»
        записи не считается."""
        r = self._r(_blk("Технологическая операция",
                         [("выполнение на территории РФ монтажа чип-компонентов печатной платы", None)]),
                    _blk("Дополнительные требования к изделию заявителя",
                         [("установка на территории РФ программного обеспечения встроенного контроллера", 20),
                          ("сборка корпуса", 20)]))
        cell = ("выполнение на территории Российской Федерации монтажа чип-компонентов печатной платы дополнительных "
                "требований к изделию заявителя: установка на территории Российской Федерации программного обеспечения "
                "встроенного контроллера (20 баллов);\nсборка корпуса (20 баллов)")
        self.assertEqual(self._apply(r, cell)[0], 0)

    def test_threshold_lines_in_threshold_field_are_not_inserted(self):
        """VI-5: график порога разборщик положил в поле порога — строками требований он вставал второй раз."""
        thr = "до 31 декабря 2023 г. - не менее 60 баллов; с 1 января 2024 г. - не менее 80 баллов"
        cell = ("производство следующих операций:\nдо 31 декабря 2023 г. - не менее 60 баллов;\n"
                "с 1 января 2024 г. - не менее 80 баллов\nмеханическая обработка поковок (10 баллов)")
        r = self._r(_blk("производство следующих операций:", [("механическая обработка поковок", 10)]), thr=thr)
        units, _ = d.lost_units(r, cell, thr)
        self.assertEqual(units, [])
        self.assertTrue(d.lost_units(r, cell)[0])                     # положительный контроль: без порога — вставка

    def test_bounds_must_be_in_element(self):
        """Числа после «не менее» — в элементе: график порога позиции XXVI-8 не пара графику узла."""
        r = self._r(_blk("узел", [("сварка узла", 10)],
                         note="с 1 января 2024 г. - не менее 120 баллов, с 1 января 2025 г. - не менее 170 баллов"))
        thr = "с 1 января 2024 г. - не менее 1050 баллов, с 1 января 2025 г. - не менее 1300 баллов"
        lines, _, res = d._d24_resolve(d._d24_items(r), f"{thr}:\nузел:\nсварка узла (10 баллов)", thr)
        self.assertEqual(res[0], (-2, "thr"))

    def test_wrapped_heading_with_semicolon_in_parentheses_is_one(self):
        """III-65: вводная перенесена внутри скобки после «;» — одна вводная, а не требование и вводная."""
        r = self._r(_blk("рама", [("сварка рамы изделия", 4)]))
        units, _ = d.lost_units(r, "осуществление следующих операций (при неприменении компонента баллы не "
                                   "начисляются;\nпри отсутствии операции требование не предъявляется):\n"
                                   "рама:\nсварка рамы изделия (4 балла)")
        self.assertEqual([u["component"] for u in units],
                         ["осуществление следующих операций (при неприменении компонента баллы не начисляются; при "
                          "отсутствии операции требование не предъявляется):"])

    def test_stale_threshold_clause_is_removed_from_heading(self):
        """XXVII-12…14: «… оцениваются не менее 50 баллов:» при пороге прим. 32 — 55/65/70: оговорка снимается."""
        r = self._r(_blk("для пластмассовых игрушек", [("литье деталей игрушки", 10)]))
        cell = ("осуществление следующих операций, которые в совокупности оцениваются не менее 50 баллов:\n"
                "для пластмассовых игрушек:\nлитье деталей игрушки (10 баллов)")
        units, skipped = d.lost_units(r, cell, None, "2024 год — не менее 55 баллов; 2025 год — не менее 65 баллов")
        self.assertEqual(units[0]["component"], "осуществление следующих операций:")
        self.assertIn("порог вводной ≠ порогу позиции", [w for w, _ in skipped])
        units, _ = d.lost_units(r, cell, None, "не менее 50 баллов")
        self.assertIn("не менее 50 баллов", units[0]["component"])

    def test_requirement_and_heading_in_one_line_are_split(self):
        """III-2: «выполнение одного из условий <18> …; осуществление … :» одной строкой — требование и вводная."""
        r = self._r(_blk("рама", [("сварка рамы изделия", 4)]))
        units, _ = d.lost_units(r, "выполнение одного из условий <18> при производстве продукции; осуществление "
                                   "следующих операций (условий):\nрама:\nсварка рамы изделия (4 балла)")
        self.assertEqual([u["component"] for u in units],
                         ["выполнение одного из условий <18> при производстве продукции",
                          "осуществление следующих операций (условий):"])

    def test_heading_and_item_in_one_line_are_split(self):
        """XII-5: вводная и первый пункт одной строкой — вводная встаёт над пунктами, пункт не переписывается; и когда
        пункт узнан как обрывок строки («элемент внутри строки»)."""
        r = self._r(_blk("Технологические операции", [("изготовление тележек грузовых вагонов для железных дорог", None),
                                                      ("сварка несущих конструкций вагона", None)]))
        units, _ = d.lost_units(r, "осуществление в полном объеме следующих операций: изготовление тележек грузовых "
                                   "вагонов для железных дорог;\nсварка несущих конструкций вагона")
        self.assertEqual([(u.get("component"), "retext" in u) for u in units],
                         [("осуществление в полном объеме следующих операций:", False)])

    def test_formula_heading_is_skipped(self):
        """IX-23, IX-29: «… рассчитывается … по формуле:» без формулы (в HTML — картинка) — не требование."""
        r = self._r(_blk("модули", [("применение модулей изделия", 5)]))
        units, skipped = d.lost_units(r, "применение модулей изделия (5 баллов);\nпри этом расчет баллов "
                                         "осуществляется по формуле:\nKt - весовой коэффициент (0,4);")
        self.assertEqual(units, [])
        self.assertEqual([w for w, _ in skipped], ["формула (заголовок)", "формула"])

    def test_one_word_lines_are_aligned(self):
        """«раскрой (10 баллов);» раньше пропускалась молча: найденная не задваивается, потерянная — встаёт."""
        r = self._r(_blk("корпус:", [("раскрой", 10)]))
        self.assertEqual(self._apply(r, "корпус:\nраскрой (10 баллов);\nсклейка (5 баллов)")[0], 1)
        self.assertEqual([(o["text"], o["points"]) for o in r["requirement_blocks"][0]["operations"]],
                         [("раскрой", 10), ("склейка", 5)])

    def test_points_forms(self):
        """«(Втоп = 7 баллов)», «(15 баллов дополнительно)», «(5 баллов, обязательное требование)», «20 баллов - …»."""
        for line, pts in (("применение печатных плат (Втоп = 7 баллов), расчет баллов", [7]),
                          ("всех печатных плат (15 баллов дополнительно);", [15]),
                          ("не менее одной печатной платы (5 баллов, обязательное требование)", [5]),
                          ("20 баллов - при изготовлении печатной платы", [20])):
            self.assertEqual(d._d24_points(line), pts, line)

    def test_explanation_goes_to_note(self):
        """I-13, XXIII-1: пояснение — note блока, к которому относится, а не обязательное требование."""
        r = self._r(_blk("документация", [("наличие прав на конструкторскую документацию", None)]))
        self._apply(r, "наличие прав на конструкторскую документацию;\nподтверждением наличия документации является "
                       "приказ о ее разработке")
        self.assertEqual(self._comps(r), ["документация"])
        self.assertEqual(r["requirement_blocks"][0]["note"], "подтверждением наличия документации является приказ о "
                                                              "ее разработке")

    def test_date_variants_without_labels_are_not_reconciled(self):
        """VII-15: варианты «до/с 1 января 2019 г.:», а в записи их меток нет — строки одного выровнялись бы с другим."""
        r = self._r(_blk("операции", [("сборка процессоров", None), ("упаковка изделия", None)]))
        units, skipped = d.lost_units(r, "до 1 января 2019 г.:\nсборка процессоров;\nс 1 января 2019 г.:\n"
                                         "сборка процессоров;\nупаковка изделия")
        self.assertEqual((units, [w for w, _ in skipped]), ([], ["варианты по датам"]))

    def test_wrap_tail_is_skipped(self):
        """IX-72: «… на срок не менее 5 лет в следующем» + «составе:» — хвост переноса, а не вводная."""
        r = self._r(_blk("права", [("прав на конструкторскую документацию на срок не менее 5 лет", None),
                                   ("технические условия изделия", None)]))
        units, skipped = d.lost_units(r, "прав на конструкторскую документацию на срок не менее 5 лет в следующем\n"
                                         "составе:\nтехнические условия изделия")
        self.assertEqual((units, [w for w, _ in skipped]), ([], ["хвост переноса"]))

    def test_subheading_glued_into_operations_is_found(self):
        """IX-43: «2.1. применение печатных плат …:» разборщик вклеил в каждую операцию — пункт «2.1.1.» не потерян:
        вводная операцию не занимает, и пункт находит её (без этого вводная съедала операцию пункта)."""
        req = ("удовлетворяющих требованиям к интегральной схеме первого уровня или второго уровня, предъявляемым в целях "
               "отнесения к продукции")
        head = f"применение в изделии интегральных схем, {req}"
        r = self._r(_blk("операции", [(f"{head}: не менее одной интегральной схемы", 5),
                                      (f"{head}: не менее одной интегральной схемы на основной плате", 5),
                                      (f"{head}: интегральной схемы центрального процессора", 15)]))
        cell = (f"2.2. {head}:\n2.2.1. не менее одной интегральной схемы, {req} (5 баллов);\n2.2.2. не менее одной "
                f"интегральной схемы на основной плате, {req} (5 баллов дополнительно);\n2.2.3. интегральной схемы "
                f"центрального процессора, {req} (15 баллов)")
        self.assertEqual(self._apply(r, cell)[0], 0)

    def test_merged_label_lines_are_found(self):
        """I-13: подпись разборщика склеила две строки ячейки — вторая находит НЕзанятые слова подписи."""
        r = self._r(_blk("изготовление основных корпусных деталей: изготовление станины, основания, рамы",
                         [("литье станины", 3)]))
        self.assertEqual(self._apply(r, "изготовление основных корпусных деталей:\nизготовление станины, основания, "
                                        "рамы:\nлитье станины (3 балла)")[0], 0)

    def test_stay_only_after_matched_line(self):
        """IX-23…25: строка встаёт к элементу ПРЕДЫДУЩЕЙ строки, только если та совпала: «где:» после строки без пары
        вставало к формуле, и строка «использование программного обеспечения …» вставлялась второй раз."""
        sw = "использование программного обеспечения в реестре российских программ"
        r = self._r(_blk("модули", [("применение прочей компонентной базы, расчет по формуле, где К - количество "
                                     "типономиналов компонентной базы", None)]),
                    _blk("ПО", [(sw, 15)]))
        self._apply(r, f"применение прочей компонентной базы, расчет по формуле;\n{sw} (15 баллов);\nитоговое "
                       f"количество баллов рассчитывается по формуле:\nгде:\nKp - весовой коэффициент за {sw}")
        self.assertEqual(sum(sw in (b.get("component") or "") + " ".join(o["text"] for o in b["operations"])
                             for b in r["requirement_blocks"]), 1)

    def test_heading_does_not_lend_its_words_to_next_item(self):
        """XX-11: вводная «компоненты контейнера высокого давления:» совпала с операцией своего пункта — пункт не встаёт
        к ней же вторым: его слова вводная уже заняла, и вводная — потерянная строка над ним."""
        r = self._r(_blk("компоненты капсул:", [("другие компоненты капсулирования", 5),
                                                ("контейнер для передачи высокого давления", 60)]))
        self._apply(r, "компоненты капсул:\nдругие компоненты капсулирования (5 баллов);\nкомпоненты контейнера высокого "
                       "давления:\nконтейнер для передачи высокого давления (60 баллов)")
        self.assertEqual([(b["component"], len(b["operations"])) for b in r["requirement_blocks"]],
                         [("компоненты капсул:", 1), ("компоненты контейнера высокого давления:", 1)])

    def test_formula_lines_stay_on_merged_operation(self):
        """IX-28: разборщик склеил операцию, «расчет баллов по формуле:» и «где К - …» — строки встают к ней подряд,
        и операция блоков питания не вставляется второй раз."""
        r = self._r(_blk("операции", [
            ("изготовление шасси изделия, расчет баллов по формуле: где К - количество шасси изделия, деленное на общее "
             "количество шасси", None),
            ("изготовление блоков питания изделия, расчет баллов по формуле: где К - количество блоков питания, "
             "деленное на общее количество блоков", None)]))
        cell = ("изготовление шасси изделия (Втоп = 20 баллов),\nрасчет баллов по формуле:\n,\nгде К - количество шасси "
                "изделия, деленное на общее количество шасси;\nизготовление блоков питания изделия (Втоп = 10 баллов),\n"
                "расчет баллов по формуле:\n,\nгде К - количество блоков питания, деленное на общее количество блоков")
        self.assertEqual(self._apply(r, cell)[0], 0)

    def test_item_of_subheading_with_shortened_tail_is_found(self):
        """IX-62: у пункта «2.2.1.» разборщик сократил хвост, и пункт уходил к операции пункта «2.2.2.» — вставлялся
        дублем; пункт узнаётся по своей части операции после подзаголовка."""
        req = "удовлетворяющей требованиям к интегральной схеме первого уровня или интегральной схеме второго уровня"
        head = f"применение схем, {req}:"
        r = self._r(_blk("операции", [(f"{head} не менее одной интегральной схемы", 5),
                                      (f"{head} не менее одной интегральной схемы на основной плате изделия, "
                                       "произведенной на территории страны", 5)]))
        cell = (f"2.2. {head}\n2.2.1. не менее одной интегральной схемы, {req}, предъявляемым в целях отнесения к "
                "продукции, произведенной на территории страны (5 баллов);\n2.2.2. не менее одной интегральной схемы на "
                "основной плате изделия, произведенной на территории страны (5 баллов)")
        self.assertEqual(self._apply(r, cell)[0], 0)

    def test_label_with_points_becomes_operation(self):
        """VII-33: «функциональное тестирование продукции (15 баллов)» разборщик оставил подписью без операций —
        обязательным требованием без баллов; это операция предыдущего списка."""
        r = self._r(_blk("операции:", [("сборка изделия", 10)]), _blk("функциональное тестирование продукции"))
        self._apply(r, "операции:\nсборка изделия (10 баллов);\nфункциональное тестирование продукции (15 баллов)")
        self.assertEqual(self._comps(r), ["операции:"])
        self.assertEqual(r["requirement_blocks"][0]["operations"][-1],
                         {"text": "функциональное тестирование продукции", "points": 15})

    def test_heading_stands_over_reordered_block(self):
        """XXI-110: разборщик поставил «Общие требования» после операций — вводная висела в конце записи."""
        r = self._r(_blk("Технологические операции", [("компримирование", 100), ("помол", 50)]),
                    _blk("Общие требования", [("наличие прав на документацию на срок 5 лет", None)]))
        head = "осуществление на территории Российской Федерации следующих технологических операций:"
        units, _ = d.lost_units(r, f"наличие прав на документацию на срок 5 лет;\n{head}\nкомпримирование - 100 баллов;"
                                   "\nпомол - 50 баллов")
        d.apply_units(r, units)
        d.fold_headings(r, head)
        self.assertEqual(self._comps(r), [head, "Общие требования"])

    def test_heading_splits_block_before_its_operations(self):
        """XXVI-3: «корпус насоса:» вставал после своих операций, над «рабочим колесом»."""
        r = self._r(_blk("насос:", [("литье корпуса", 60), ("сварка корпуса", 30), ("литье колеса", 50)]))
        self._apply(r, "насос:\nкорпус насоса:\nлитье корпуса (60 баллов);\nсварка корпуса (30 баллов);\n"
                       "рабочее колесо:\nлитье колеса (50 баллов)")
        self.assertEqual([(b["component"], len(b["operations"])) for b in r["requirement_blocks"]],
                         [("насос:", 0), ("корпус насоса:", 2), ("рабочее колесо:", 1)])

    def test_parser_label_inside_heading_is_relabelled(self):
        """XVIII-10: подпись разборщика целиком внутри вводной ячейки — на её место встаёт вводная."""
        r = self._r(_blk("Комплектующие и материалы, если предусмотрено конструкцией оборудования",
                         [("резина для кранцев", 10)]))
        head = ("производство на территориях государств следующих комплектующих и материалов (если предусмотрено "
                "конструкцией оборудования):")
        self._apply(r, f"{head}\nрезина для кранцев (10 баллов)")
        self.assertEqual(self._comps(r), [head])

    def test_heading_is_not_put_after_label_eaten_by_pair(self):
        """XVIII-73: пара «сервисный центр» + следующая подпись съедала подпись, и вводная вставала после неё."""
        r = self._r(_blk("Общие требования", [("наличие сервисного центра на территории страны", None)]),
                    _blk("Технологические операции при наличии", [("сборка изделия", 5)]))
        head = "осуществление на территории следующих технологических операций (при наличии):"
        units, _ = d.lost_units(r, f"наличие сервисного центра на территории страны;\n{head}\nсборка изделия (5 баллов)")
        d.apply_units(r, units)
        d.fold_headings(r, head)
        self.assertEqual(self._comps(r), ["Общие требования", head])

    def test_heading_is_not_inside_a_window(self):
        """Вводная «…:» — своя строка ячейки: в окно склеенной строки она не входит (VII-2, повторный прогон)."""
        r = self._r(_blk("узел", [("установка программного обеспечения изделия", 5)]),
                    _blk("выполнение следующих операций:"), _blk("сборка", [("сборка готового изделия", 5)]))
        items = d._d24_items(r)
        x = d._d24_info("установка программного обеспечения изделия выполнение следующих операций сборка готового "
                        "изделия", items)
        heads = {k for k, it in enumerate(items) if it[6]}
        self.assertTrue(heads)
        self.assertFalse([c for c in x["cands"] if c[3] == "win" and heads & set(range(c[0], c[1] + 1))])

    def test_threshold_preamble_is_not_a_heading(self):
        """VI-25: «… могут быть отнесены к продукции …, при обеспечении:» + график из поля порога — вводная порога,
        а не списка: висела в конце записи."""
        thr = "до 31 декабря 2023 г. - не менее 90 баллов; с 1 января 2024 г. - не менее 120 баллов"
        r = self._r(_blk("операции", [("производство става", 25)]), thr=thr)
        units, skipped = d.lost_units(r, "производство става (25 баллов);\nконвейеры скребковые могут быть отнесены к "
                                         "продукции, произведенной на территории страны, при обеспечении:\n"
                                         "до 31 декабря 2023 г. - не менее 90 баллов;\n"
                                         "с 1 января 2024 г. - не менее 120 баллов", thr)
        self.assertEqual((units, [w for w, _ in skipped]), ([], ["вводная порога"]))

    def test_wrapped_lines_are_joined(self):
        """Перенос HTML на служебном слове (IX-36 «… Российской Федерации не» + «менее трех …») и внутри «Российской
        Федерации» (V-2) — одна строка."""
        self.assertEqual(d._d24_lines("осуществление на территории не\nменее трех операций:\nсборка"),
                         ["осуществление на территории не менее трех операций:", "сборка"])
        self.assertEqual(d._d24_lines("использование произведенных на территории Российской\nФедерации комплектующих:"),
                         ["использование произведенных на территории Российской Федерации комплектующих:"])
        self.assertEqual(d._d24_lines("сборка изделия\nсварка рамы"), ["сборка изделия", "сварка рамы"])

    def test_bare_group_points_threshold_is_cell_requirement(self):
        """H1: «20 баллов» в поле порога — баллы заголовка группы требований (IX-6, 10, 11 …), «140 баллов» — группы
        «(в совокупности 140 баллов):» (IX-85…133); у `toks` такой строки слов нет, прежняя проверка её не видела."""
        cell = ("выполнение обязательных требований, в совокупности предоставляющих заявителю 20 баллов:\n"
                "наличие прав;\nвыполнение требований (в совокупности 140 баллов):\nсборка (20 баллов)")
        ct = set(d.toks(cell))
        for t, want in (("20 баллов", True), ("140 баллов", True), ("не менее 20 баллов", False), ("7 баллов", False)):
            self.assertEqual(d._threshold_is_cell_requirement(t, cell, ct), want, t)
        self.assertFalse(d._threshold_is_cell_requirement("20 баллов", "наличие прав;\nсборка (20 баллов)", ct))


class TestD24Golden(unittest.TestCase):
    """Реальные ячейки Контура 508574 и записи D23 (XV-4, XXVI-3, XXI-110, VI-4, IX-36, IX-62, IX-28, XX-12, IX-111,
    XXVI-8, XII-5, III-65, VII-33, IX-43, XVIII-10) → ровно та запись, что в корпусе: сторож правил выравнивания,
    которых синтетика не различает (окно из трёх элементов, «Российской Федерации» против «РФ», незанятые слова
    элемента, цена окна)."""

    def test_records_rebuild_to_corpus(self):
        data = json.loads((ROOT / "tests" / "d24_golden.json").read_text(encoding="utf-8"))["records"]
        self.assertEqual(len(data), 15)
        for x in data:
            r = json.loads(json.dumps(x["before"]))
            sec = r["section_roman"]
            d.restore_lost_lines({sec: {"all": [{"text": x["cell"], "_recs": [r], "code": "x"}]}})
            for f, v in x["after"].items():
                self.assertEqual(r.get(f), v, (r["source_anchor"], f))


class TestD24Manifest(unittest.TestCase):
    """Манифест общих перечней после D24: порог записи — текст ячейки; оговорка порога вводной — не другой перечень."""

    @staticmethod
    def _manifest(recs, text):
        return d.shared_manifest({"T": {"shared": [{"code": "x", "text": text, "_recs": recs}]}}, "0", "t")["lists"][0]

    def test_threshold_field_counts_as_cell_text(self):
        """VI-33…39: график порога в поле порога записи — не «потерянные числа» перечня (ревью D24, M1)."""
        r = _rec("А", [("сборка изделия", 10)], 1)
        r["min_threshold"] = "до 31 декабря 2023 г. - не менее 60 баллов"
        m = self._manifest([r], "сборка изделия (10 баллов);\nдо 31 декабря 2023 г. - не менее 60 баллов")
        self.assertEqual((m["recall"], m["numbers_missing"]), (1.0, []))

    def test_stale_threshold_clause_keeps_one_list(self):
        """XXVII-11…14: у 11 оговорка «оцениваются не менее 50 баллов» вводной остаётся, у 12…14 — снята: перечень
        один, и сторож D8 у его членов молчит, как прежде."""
        a, b = _rec("А", [("сборка", 1)], 1), _rec("Б", [("сборка", 1)], 2)
        a["requirement_blocks"].insert(0, {"component": "операции, которые в совокупности оцениваются не менее 50 "
                                                        "баллов:", "operations": []})
        b["requirement_blocks"].insert(0, {"component": "операции:", "operations": []})
        self.assertTrue(self._manifest([a, b], "операции:\nсборка (1 балл)")["same_list"])
        b["requirement_blocks"][0]["component"] = "другие операции:"
        self.assertFalse(self._manifest([a, b], "операции:\nсборка (1 балл)")["same_list"])


class TestD24FoldHeadings(unittest.TestCase):
    """Вводная списка встаёт на место ярлыка разборщика — и только ярлыка."""

    HEAD = "осуществление на территории Российской Федерации следующих технологических операций:"

    def _r(self, label):
        return {"requirement_blocks": [{"component": self.HEAD, "operations": []},
                                       {"component": label, "operations": [{"text": "сборка", "points": 3}]}]}

    def test_paraphrase_label_and_empty_label_give_way(self):
        for label in ("Технологические операции", None, "Операции на территории РФ"):
            r = self._r(label)
            self.assertEqual(d.fold_headings(r, self.HEAD + "\nсборка (3 балла)"), 1, label)
            self.assertEqual([b["component"] for b in r["requirement_blocks"]], [self.HEAD.rstrip()], label)
            self.assertEqual(d.fold_headings(r, self.HEAD + "\nсборка (3 балла)"), 0)          # повтор — без правки

    def test_node_and_cell_text_stay(self):
        """«станина» — узел, не пересказ вводной; подпись, которая есть в ячейке, — текст первоисточника."""
        for label, cell in (("станина", ""), ("технологические операции", "технологические операции:\n")):
            r = self._r(label)
            self.assertEqual(d.fold_headings(r, self.HEAD + "\n" + cell + "сборка (3 балла)"), 0, label)
            self.assertEqual(len(r["requirement_blocks"]), 2)


class TestD24Headings(unittest.TestCase):
    """Рантайм: вводная без операций — строка «▸», не обязательное требование и не место в капе."""

    @staticmethod
    def _hit(n_ops):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_rag import make_hit
        blocks = [{"component": "наличие сервисного центра", "operations": []},
                  {"component": "осуществление на территории Российской Федерации следующих операций:", "operations": []}]
        blocks += [{"component": f"узел {i}", "operations": [{"text": f"сварка узла номер {i}", "points": 1}]}
                   for i in range(n_ops - 1)]
        return make_hit(requirement_blocks=blocks, okpd2_match=True)

    def test_heading_is_not_required_and_not_counted(self):
        from app.rag.pipeline import MAX_OPS_TARGET, _hit_operations, _n_operations, _shown_operations
        h = self._hit(MAX_OPS_TARGET)
        ops = _hit_operations(h)
        self.assertEqual([("_required" in o, "_heading" in o) for o in ops[:2]], [(True, False), (False, True)])
        self.assertEqual(_n_operations(h), MAX_OPS_TARGET)
        shown, total = _shown_operations(ops, "сварка")
        self.assertEqual((len(shown), total), (MAX_OPS_TARGET + 1, MAX_OPS_TARGET))   # кап не сработал

    def test_heading_printed_as_intro_line(self):
        from app.rag.pipeline import MAX_OPS_TARGET, format_context, source_text
        h = self._hit(MAX_OPS_TARGET)
        ctx = format_context([h], None, None)
        self.assertIn("      ▸ осуществление на территории Российской Федерации следующих операций:", ctx)
        self.assertNotIn("следующих операций: — баллы не приведены", ctx)
        self.assertNotIn("СПИСОК ОПЕРАЦИЙ НЕПОЛНЫЙ", ctx)
        self.assertIn("▸ осуществление на территории Российской Федерации следующих операций:", source_text(h))

    def test_truncated_count_ignores_headings(self):
        from app.rag.pipeline import MAX_OPS_TARGET, format_context
        ctx = format_context([self._hit(MAX_OPS_TARGET + 1)], "сварка", None)
        self.assertIn(f"показаны {MAX_OPS_TARGET} из {MAX_OPS_TARGET + 1} операций", ctx)
        self.assertEqual(ctx.count("• "), MAX_OPS_TARGET)                      # вводная — не пункт и не место
        head = ctx.index("▸ осуществление на территории Российской Федерации следующих операций:")
        self.assertLess(head, ctx.index("• сварка узла"))                       # над пунктами и после пересортировки

    def test_points_table_follows_same_selection(self):
        from app.rag.pipeline import MAX_OPS_TARGET, points_table
        self.assertNotIn("перечень неполный", points_table(self._hit(MAX_OPS_TARGET), "сварка"))
        self.assertIn("перечень неполный", points_table(self._hit(MAX_OPS_TARGET + 1), "сварка"))

    def test_inherited_heading_keeps_flag(self):
        import diag_orphan_requirements as G
        rec = {"requirement_blocks": [{"component": "осуществление следующих операций:", "operations": []},
                                      {"component": "наличие прав", "operations": []}]}
        self.assertEqual([(o.get("_heading"), o.get("_required")) for o in G.record_operations(rec)],
                         [(True, None), (None, True)])
        self.assertEqual(G.n_operations(rec), 1)


class TestD24RestoreRecord(unittest.TestCase):
    """`restore_lost_lines`: что меняется в записи помимо строк — тип, порог-требование, notes."""

    @staticmethod
    def _run(r, text, lookup="70 баллов [прим. 56]"):
        from unittest import mock
        rep = {"T": {"all": [{"text": text, "_recs": [r], "code": "x"}]}}
        with mock.patch("app.rag.thresholds.lookup_threshold", return_value=lookup):
            return d.restore_lost_lines(rep)

    def test_requirement_in_threshold_field_is_dropped(self):
        r = _rec("А", [("сборка", None)], 1)
        r["min_threshold"] = "для акционерных обществ - не менее 75 процентов"
        self._run(r, "сборка;\nдля акционерных обществ - не менее 75 процентов")
        self.assertIsNone(r["min_threshold"])

    def test_group_points_in_threshold_field_are_dropped(self):
        """IX-11: «Порог: 20 баллов» — баллы группы обязательных требований; порог позиции — прим. 26."""
        cell = "выполнение обязательных требований, в совокупности предоставляющих заявителю 20 баллов:\nсборка;"
        for lookup, want in (("140 баллов [прим. 26]", None), (None, "20 баллов")):
            r = _rec("А", [("сборка", None)], 1)
            r["min_threshold"] = "20 баллов"
            self._run(r, cell, lookup)
            self.assertEqual(r["min_threshold"], want, lookup)

    def test_threshold_statement_and_no_note_threshold_are_kept(self):
        for thr, lookup in (("не менее 50 баллов", "70 баллов"), ("для акционерных обществ - не менее 75 процентов", None)):
            r = _rec("А", [("сборка", None)], 1)
            r["min_threshold"] = thr
            self._run(r, f"сборка;\n{thr}", lookup)
            self.assertEqual(r["min_threshold"], thr)

    def test_points_make_type_mixed(self):
        r = _rec("А", [("наличие прав на документацию", None)], 1)
        self._run(r, "наличие прав на документацию;\nсварка корпуса (5 баллов)")
        self.assertEqual(r["requirement_type"], "mixed")
        r = _rec("А", [("наличие прав на документацию", None)], 1)
        self._run(r, "наличие прав на документацию;\nналичие сервисного центра в России")
        self.assertEqual(r["requirement_type"], "operations")

    def test_notes_with_numbers_not_in_blocks_are_kept(self):
        """XX-12: слова notes «были» в блоках после первой копии графика, а чисел копии — нет: notes не снимается."""
        r = _rec("А", [("сборка корпуса изделия", 2)], 1, notes="до 31 декабря 2027 г. - не более 35 процентов")
        self._run(r, "сборка корпуса изделия (2 балла);\nсварка рамы изделия (3 балла);\n"
                     "до 31 декабря 2027 г. - не более 35 процентов", None)
        r2 = _rec("А", [("сборка корпуса изделия", 2), ("до 31 декабря 2029 г. - не более 20 процентов", None)], 1,
                  notes="до 31 декабря 2027 г. - не более 35 процентов")
        self._run(r2, "сборка корпуса изделия (2 балла);\nдо 31 декабря 2029 г. - не более 20 процентов;\n"
                      "сварка рамы изделия (3 балла)", None)
        self.assertIsNone(r["notes"])                                    # положительный контроль: числа в блоках
        self.assertEqual(r2["notes"], "до 31 декабря 2027 г. - не более 35 процентов")

    def test_notes_with_cell_text_move_to_blocks(self):
        r = _rec("А", [("сборка корпуса изделия", 2)], 1, notes="наличие сервисного центра на территории России")
        self._run(r, "сборка корпуса изделия (2 балла);\nналичие сервисного центра на территории России")
        self.assertIsNone(r["notes"])
        r = _rec("А", [("сборка корпуса изделия", 2)], 1, notes="позиция исключена с 2027 года")
        self._run(r, "сборка корпуса изделия (2 балла);\nналичие сервисного центра на территории России")
        self.assertEqual(r["notes"], "позиция исключена с 2027 года")

    def test_lost_heading_takes_place_of_parser_label(self):
        r = _rec("А", [("сборка корпуса изделия", 3)], 1, comp="Технологические операции")
        self._run(r, "осуществление на территории Российской Федерации следующих технологических операций:\n"
                     "сборка корпуса изделия (3 балла)")
        self.assertEqual([b["component"] for b in r["requirement_blocks"]],
                         ["осуществление на территории Российской Федерации следующих технологических операций:"])

    def test_record_without_own_operations_is_left_to_inheritance(self):
        r = _rec("А", [], 1)
        r["requirement_blocks"] = [{"component": "наличие прав", "operations": []}]
        log, _ = self._run(r, "наличие прав;\nсварка корпуса изделия (5 баллов)")
        self.assertEqual(log, [])
        self.assertEqual(len(r["requirement_blocks"]), 1)


class TestD24Runtime(unittest.TestCase):
    """Обязательные требования без баллов при усечении длинного перечня не уступают место операциям."""

    def test_required_first_when_truncated(self):
        from app.rag.pipeline import MAX_OPS_TARGET, _hit_operations, _rank_operations
        sys.path.insert(0, str(ROOT / "tests"))
        from test_rag import make_hit
        blocks = [{"component": f"операции узла {i}", "operations": [{"text": f"сварка узла номер {i}", "points": 1}]}
                  for i in range(MAX_OPS_TARGET + 5)]
        blocks.append({"component": "наличие сервисного центра на территории России", "operations": []})
        ops = _hit_operations(make_hit(requirement_blocks=blocks))
        self.assertTrue(ops[-1].get("_required"))
        for query in ("сварка узла", None):
            ranked = _rank_operations(ops, query)
            self.assertEqual(ranked[0]["text"], "наличие сервисного центра на территории России", query)
            self.assertEqual([o["text"] for o in ranked[1:]], [o["text"] for o in ops[:-1]], query)


class TestD24Corpus(unittest.TestCase):
    """Корпус после `D24`: позиции, ради которых правка делалась."""

    @classmethod
    def setUpClass(cls):
        cls.recs = d.load_records()

    def _one(self, sec, prefix):
        hit = [r for r in self.recs[sec] if r["product_name"].startswith(prefix)]
        self.assertEqual(len(hit), 1, prefix)
        return hit[0]

    @staticmethod
    def _text(r):
        return " ".join((b.get("component") or "") + " " + " ".join(o.get("text") or "" for o in b.get("operations") or [])
                        for b in r.get("requirement_blocks") or [])

    def test_bulldozers_have_mandatory_requirements(self):
        """Права на КД, сервисный центр и условие <18> — обязательные требования в рантайме, не вводные «▸»
        (ревью D24: <18> в склейке «…; осуществление … :» становилось заголовком, подстрока это не видела)."""
        sys.path.insert(0, str(ROOT / "tests"))
        from app.rag.pipeline import _hit_operations
        from test_rag import make_hit
        r = self._one("III", "Бульдозеры на гусеничных тракторах")
        ops = _hit_operations(make_hit(requirement_blocks=r["requirement_blocks"], okpd2_match=True))
        for frag in ("сервисного центра", "одного из условий <18>", "прав на конструкторскую"):
            hit = [o for o in ops if frag in (o.get("text") or "")]
            self.assertTrue(hit and all(o.get("_required") for o in hit), frag)

    def test_service_centre_and_components_are_separate(self):
        for prefix, start in (("Комбайны зерноуборочные", "при производстве осуществляется комплекс"),
                              ("Бульдозеры на гусеничных тракторах", "при производстве осуществление комплекса"),
                              ("Грейдеры самоходные", "при производстве осуществление комплекса")):
            r = self._one("III", prefix)
            comps = [b.get("component") or "" for b in r["requirement_blocks"]]
            svc = [c for c in comps if "сервисного центра" in c]
            self.assertEqual(len(svc), 1, prefix)
            self.assertNotIn("комплекс", svc[0], prefix)
            self.assertTrue(any(c.startswith(start) for c in comps), prefix)

    def test_golf_carts_list_restored(self):
        r = self._one("III", "Автомобили для перевозки игроков в гольф")
        pts = [o for b in r["requirement_blocks"] for o in b.get("operations") or [] if o.get("points")]
        self.assertGreaterEqual(len(pts), 30)

    def test_requirement_in_threshold_field_gives_way_to_note(self):
        """IX-5 «20 баллов» (заголовок группы требований) и I-26 «для акционерных обществ …» стояли в поле порога и
        перекрывали порог примечания."""
        from app.rag import thresholds as T
        for sec, prefix, note in (("IX", "Компьютеры портативные массой", "[прим. 26]"),
                                  ("I", "Аддитивные установки экструзии", "[прим. 56")):
            r = self._one(sec, prefix)
            eff = r.get("min_threshold") or T.lookup_threshold(r["okpd2_codes"], r["product_name"], sec) or ""
            self.assertIn(note, eff, prefix)

    def test_toys_heading_does_not_truncate_list(self):
        """XXVII-11: вводная «… в совокупности оцениваются не менее 50 баллов:» стоит над «для пластмассовых …»
        отдельной строкой «▸» и не вытесняет за кап операцию с баллами."""
        from app.rag.pipeline import format_context
        from app.rag.retriever import Hit
        r = next(x for x in self.recs["XXVII"] if x["source_anchor"].endswith("позиция 11"))
        h = Hit(score=1.0, section_roman="XXVII", section_title="", product_name=r["product_name"],
                okpd2_codes=r["okpd2_codes"], min_threshold=r.get("min_threshold"),
                requirement_blocks=r["requirement_blocks"], source_anchor=r["source_anchor"], okpd2_match=True, payload=r)
        ctx = format_context([h], None, None)
        self.assertNotIn("СПИСОК ОПЕРАЦИЙ НЕПОЛНЫЙ", ctx)
        self.assertIn("      ▸ осуществление на территории Российской Федерации следующих операций", ctx)
        self.assertIn("печать настольно-печатных игр, произведенных на территории Российской Федерации по договору", ctx)

    def test_parser_labels_replaced_by_source_headings(self):
        r = self._one("I", "Аддитивные установки экструзии")
        comps = [b.get("component") or "" for b in r["requirement_blocks"]]
        self.assertNotIn("Компоненты и технологические операции", comps)
        self.assertTrue(any(c.startswith("производство (осуществление) на территории Российской Федерации следующих")
                            and r["requirement_blocks"][i].get("operations") for i, c in enumerate(comps)))

    def test_no_dangling_formula_headers(self):
        """Ни одной вводной «… по формуле:» без формулы во всём корпусе (IX-16, IX-23, IX-29)."""
        bad = [(sec, r["source_anchor"]) for sec, rr in self.recs.items() for r in rr
               for b in r.get("requirement_blocks") or [] if not b.get("operations")
               and d._D24_FORMULA_HEAD_RE.search((b.get("component") or "").rstrip())]
        self.assertEqual(bad, [])

    def test_no_record_ends_with_list_heading(self):
        """Ревью D24, M2: вводная без единой операции после неё висела в конце 29 записей."""
        bad = [(sec, r["source_anchor"].split()[-1]) for sec, rr in self.recs.items() for r in rr
               if any(b.get("operations") for b in r.get("requirement_blocks") or [])
               and not r["requirement_blocks"][-1].get("operations")
               and (r["requirement_blocks"][-1].get("component") or "").rstrip().endswith(":")]
        self.assertEqual(bad, [])

    def test_group_points_are_not_position_threshold(self):
        """H1: «20 баллов» у IX-6, 10, 11, 16, 18, 28, 71 и «140 баллов» у IX-85…133 — баллы группы требований, не
        порог: порог позиции — прим. 26 и прим. 38."""
        from app.rag import thresholds as T
        for pos, note in (("6", "[прим. 26]"), ("11", "[прим. 26]"), ("71", "[прим. 26]"), ("85", "[прим. 38]"),
                          ("111", "[прим. 38]"), ("133", "[прим. 38]")):
            r = next(x for x in self.recs["IX"] if x["source_anchor"].endswith(f"позиция {pos}"))
            self.assertIsNone(r.get("min_threshold"), pos)
            self.assertIn(note, T.lookup_threshold(r["okpd2_codes"], r["product_name"], "IX") or "", pos)

    def test_glued_line_is_not_a_second_operation(self):
        """H2: склеенная строка HTML у IX-111 не вставлена четвёртой операцией на 20 баллов, у IV-30 — на 3 балла."""
        r = next(x for x in self.recs["IX"] if x["source_anchor"].endswith("позиция 111"))
        ops = [o["text"] for b in r["requirement_blocks"] for o in b.get("operations") or []]
        self.assertEqual(sum("установка программного обеспечения" in t for t in ops), 1)
        r = next(x for x in self.recs["IV"] if x["source_anchor"].endswith("позиция 30"))
        ops = [o["text"] for b in r["requirement_blocks"] for o in b.get("operations") or []]
        self.assertFalse([t for t in ops if "сервисного центра" in t and "платы управления" in t])
        for pos in ("23", "24", "25"):                     # строка «наличие ПО … в реестре» — одна (правило «подряд»)
            r = next(x for x in self.recs["IX"] if x["source_anchor"].endswith(f"позиция {pos}"))
            ops = [o["text"] for b in r["requirement_blocks"] for o in b.get("operations") or []]
            self.assertEqual(sum(t.startswith("наличие программного обеспечения, входящего в единый реестр")
                                 for t in ops), 1, pos)

    def test_threshold_schedule_is_not_a_requirement(self):
        """M1: график порога «до 31 декабря 2023 г. - не менее 60 баллов …» VI-5 — в поле порога, не в блоках."""
        for sec, pos in (("VI", "5"), ("VI", "33"), ("V", "47"), ("IV", "31")):
            r = next(x for x in self.recs[sec] if x["source_anchor"].endswith(f"позиция {pos}"))
            text = self._text(r)
            self.assertFalse(re.search(r"(?:до|с) \d+ \w+ 20\d\d (?:г\. |года )?- не менее \d+ балл", text), (sec, pos))

    def test_toys_heading_keeps_only_consistent_threshold(self):
        """M5: у XXVII-12…14 (порог прим. 32) вводная без «не менее 50 баллов»; у XXVII-11 (порог 50) — с ней."""
        for pos, kept in (("11", True), ("12", False), ("13", False), ("14", False)):
            r = next(x for x in self.recs["XXVII"] if x["source_anchor"].endswith(f"позиция {pos}"))
            heads = [b["component"] for b in r["requirement_blocks"]
                     if (b.get("component") or "").startswith("осуществление на территории")]
            self.assertTrue(heads, pos)
            self.assertEqual(any("не менее 50 баллов" in h for h in heads), kept, pos)


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
