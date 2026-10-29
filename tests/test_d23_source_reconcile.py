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
        # аддитивные установки I: в перечне нет «не менее 75 процентов … трех четвертей» (они в `min_threshold` I-26,
        # класс D24) — полнота не доказана, пометка «перечень неполный» остаётся (раунд 2 ревью, H2)
        self.assertTrue(fragments.is_fragmented("Аддитивные установки фотополимеризации в ванне"))
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
