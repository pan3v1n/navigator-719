"""D20 #225: коды «за исключением» — не коды позиции.

У четырёх позиций ячейка кода первоисточника несёт оговорку («26.20.11 (за исключением 26.20.11.120,
26.20.11.130)»), а `okpd2_codes` записи — все коды подряд. Итог до правки:
  * по исключённому коду позиция становилась целевой ТОЧНЫМ совпадением, вместе со своей
    («Компьютеры портативные…» и «Портативные персональные компьютеры» на 26.20.11.120);
  * порог позиции брался из строки примечания ИСКЛЮЧЁННОГО кода: строки «X (за исключением Y) "…": …»
    разбор не узнавал, и «Устройства ввода или вывода» получали порог принтеров 70/85/120 вместо своих
    55/65/90, «Устройства запоминающие и прочие» — 23/25/55 вместо 110/120/150.
Офлайн: Qdrant и ключ не нужны."""
from __future__ import annotations

import json
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.rag import code_cells, pipeline, retriever
from app.rag import thresholds as t
from app.rag.retriever import Hit

ROOT = Path(__file__).resolve().parents[1]
_HIT_FIELDS = {f.name for f in fields(Hit)}


def _records() -> list[dict]:
    out = []
    for f in sorted((ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json")):
        out += [r for r in json.loads(f.read_text(encoding="utf-8"))
                if r.get("record_type") != "section_methodology"]
    return out


RECORDS = _records()


def _rec(start: str, code: str | None = None) -> dict:
    found = [r for r in RECORDS if r["product_name"].startswith(start)
             and (code is None or code in r.get("okpd2_codes", []))]
    assert found, start
    return found[0]


def _from_qdrant(r: dict) -> Hit:
    """Хит так, как его строит рантайм из точки Qdrant (payload = запись)."""
    return retriever._to_hit(SimpleNamespace(id=r["source_anchor"], score=1.0, payload=r))


def _targets(code: str) -> list[str]:
    """Целевые по коду на всём корпусе — тем же путём, что `search` + `target_hits` (без ранга)."""
    hits = [_from_qdrant(r) for r in RECORDS if retriever.okpd2_match(r.get("okpd2_codes") or [], code)]
    for h in hits:
        h.okpd2_match = retriever.code_match(h, code)
    retriever._set_code_tiers(hits, code)
    hits.sort(key=retriever._order_key)
    return [h.product_name for h in pipeline.target_hits(hits, code)]


LAPTOPS = "Компьютеры портативные"
IO = "Устройства ввода или вывода"
STORAGE = "Устройства запоминающие и прочие"


class TestHit(unittest.TestCase):
    def test_excluded_codes_leave_okpd2_codes(self):
        r = _rec(LAPTOPS)
        h = _from_qdrant(r)
        self.assertEqual(h.okpd2_codes, ["26.20.11"])
        self.assertEqual(h.excluded_codes, ["26.20.11.120", "26.20.11.130"])
        self.assertEqual(h.payload["okpd2_codes"], r["okpd2_codes"])       # payload не тронут

    def test_ordinary_position_untouched(self):
        r = _rec("Портативные персональные компьютеры")
        h = _from_qdrant(r)
        self.assertEqual((h.okpd2_codes, h.excluded_codes), (r["okpd2_codes"], []))

    def test_code_match_respects_exclusions(self):
        h = _from_qdrant(_rec(LAPTOPS))
        self.assertTrue(retriever.code_match(h, "26.20.11"))
        self.assertTrue(retriever.code_match(h, "26.20.11.110"))
        self.assertFalse(retriever.code_match(h, "26.20.11.120"))
        self.assertFalse(retriever.code_match(h, "26.20.11.121"))         # подкод исключённого
        self.assertFalse(retriever.code_match(h, "26.20.12"))


class TestTargets(unittest.TestCase):
    def test_excluding_position_is_not_a_target(self):
        """Радиус 27.10.2026 (коды справочника под исключёнными и под своими кодами четырёх позиций,
        85 кодов): целевая сменилась ровно у этих семи, у каждого ушла исключающая позиция."""
        cases = {
            "26.20.11.120": ("Портативные персональные компьютеры", LAPTOPS),
            "26.20.11.130": ("Планшетные компьютеры", LAPTOPS),
            "26.20.16.120": ("Принтеры", IO),
            "26.20.21": ("Устройства запоминающие", STORAGE),
            "26.20.22": ("Устройства запоминающие полупроводниковые", STORAGE),
            "21.10.51": ("Препараты лекарственные", "Субстанции фармацевтические"),
        }
        for code, (own, excluding) in cases.items():
            got = _targets(code)
            self.assertTrue(any(n.startswith(own) for n in got), (code, got))
            self.assertFalse(any(n.startswith(excluding) for n in got), (code, got))

    def test_own_code_still_targets_the_position(self):
        self.assertTrue(any(n.startswith(LAPTOPS) for n in _targets("26.20.11")))
        self.assertTrue(any(n.startswith(STORAGE) for n in _targets("26.20.2")))


class TestSearch(unittest.TestCase):
    """Путь `retriever.search`: фильтр Qdrant видит payload, а в нём и исключённые коды."""

    def _search(self, pool_has_laptops: bool):
        laptops, own = _rec(LAPTOPS), _rec("Портативные персональные компьютеры")
        pts = [SimpleNamespace(id=r["source_anchor"], score=s, payload=r) for r, s in ((laptops, 0.9), (own, 0.5))]

        def fake(query, limit, qfilter=None, collection=None, qvec=None, **kw):
            if qfilter is None:                       # пул: по тексту — только «Компьютеры портативные»
                return pts[:1] if pool_has_laptops else []
            return pts                                # доборы и пересчёт: фильтр видит payload целиком

        with mock.patch.object(retriever, "_hybrid", side_effect=fake), \
                mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
            hits = retriever.search("ноутбук", okpd2="26.20.11.120", limit=8)
        return {h.product_name.split(" ")[0]: h for h in hits}, hits

    def test_pool_hit_does_not_match_by_excluded_code(self):
        by, hits = self._search(pool_has_laptops=True)
        self.assertFalse(by["Компьютеры"].okpd2_match)
        self.assertTrue(by["Портативные"].okpd2_match)
        self.assertEqual([h.product_name.split(" ")[0] for h in pipeline.target_hits(hits, "26.20.11.120")],
                         ["Портативные"])

    def test_branch_fetch_does_not_bring_excluding_position(self):
        by, _hits = self._search(pool_has_laptops=False)
        self.assertNotIn("Компьютеры", by)
        self.assertTrue(by["Портативные"].okpd2_match)


class TestSearchD14Fetch(unittest.TestCase):
    """Ревью D20 (LOW-3): добор точных форм кода (D14) тоже не берёт позицию по исключённому коду.
    Пул пуст; добор по ветке (`should`) приносит только исключающую позицию — она не совпала и не
    взята; тогда идёт добор точных форм (`must`), и он не должен вернуть её совпавшей."""

    def test_exact_forms_fetch_skips_excluding_position(self):
        laptops, own = _rec(LAPTOPS), _rec("Портативные персональные компьютеры")
        pt = lambda r, s: SimpleNamespace(id=r["source_anchor"], score=s, payload=r)  # noqa: E731

        def fake(query, limit, qfilter=None, collection=None, qvec=None, **kw):
            if qfilter is None:
                return []
            if getattr(qfilter, "should", None):
                return [pt(laptops, 0.9)]
            return [pt(laptops, 0.9), pt(own, 0.5)]

        with mock.patch.object(retriever, "_hybrid", side_effect=fake),                 mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
            hits = retriever.search("ноутбук", okpd2="26.20.11.120", limit=8)
        names = [h.product_name.split(" ")[0] for h in hits]
        self.assertIn("Портативные", names)
        self.assertNotIn("Компьютеры", names)


class TestContext(unittest.TestCase):
    def test_context_shows_cell_of_a_qdrant_hit(self):
        """Хит из Qdrant несёт свои коды, а ячейку — по полному списку из payload (`code_cells.of_hit`)."""
        h = _from_qdrant(_rec(LAPTOPS))
        h.okpd2_match = True
        ctx = pipeline.format_context([h], "вопрос", "26.20.11")
        self.assertIn("ОКПД2: 26.20.11 (за исключением 26.20.11.120, 26.20.11.130)", ctx)
        self.assertIn("под эту позицию не подпадает", ctx)
        self.assertIn("Код в приложении: 26.20.11 (за исключением", pipeline.source_text(h))


class TestThresholds(unittest.TestCase):
    def _thr(self, r: dict, codes: list[str] | None = None) -> str | None:
        return t.lookup_threshold(codes if codes is not None else r["okpd2_codes"],
                                  r["product_name"], r["section_roman"])

    def test_rows_with_exclusions_are_parsed(self):
        got = {tuple(r["codes"]): r["excluded"] for r in t._flat_thresholds() if r.get("excluded")}
        # «(за исключением …)» и «(кроме [ОКПД 2] …)» — в списке кодов и внутри закавыченного имени
        self.assertEqual(got, {
            ("26.20.11",): ["26.20.11.120", "26.20.11.130"],
            ("26.20.16",): ["26.20.16.120"],
            ("26.20.2",): ["26.20.21", "26.20.22"],
            ("26.20.22",): ["26.20.22.110", "26.20.22.120", "26.20.22.160"],
            ("26.30.2",): ["26.30.23.140", "26.30.23.141", "26.30.23.142"],
            ("26.30.11",): ["26.30.11.110", "26.30.11.121", "26.30.11.122", "26.30.11.125", "26.30.11.150"],
            ("26.40.20",): ["26.40.20.122"],
            ("26.51.20",): ["26.51.20.121"],
            ("14.12", "32.99.11.140"): ["14.12.30.150"],
            ("15.20.14",): ["15.20.14.120", "15.20.14.130", "15.20.14.141", "15.20.14.142", "15.20.14.143"],
            ("15.20.3",): ["15.20.32.130", "15.20.32.131", "15.20.32.132", "15.20.32.139"],
            ("15.20.32.130",): ["15.20.32.131", "15.20.32.132"],
        })

    def test_krome_form_gives_own_procurement_threshold(self):
        """Ревью D20: «(кроме ОКПД 2 …)» прим. 53 — коды оговорки становились кодами строки, и обувь
        получала закупочный порог строки, которая её исключает (80/110/90 вместо своих)."""
        def proc(start, code):
            r = _rec(start, code)
            return t.lookup_procurement_threshold(r["okpd2_codes"], r["product_name"], r["section_roman"])
        self.assertTrue(proc("Обувь валяная", "15.20.14.130").startswith("не менее 60 баллов"))
        self.assertTrue(proc("Обувь детская валяная", "15.20.14.143").startswith("не менее 60 баллов"))
        self.assertIn("не менее 90 баллов", proc("Обувь детская прочая", "15.20.32.130"))

    def test_krome_in_note_26_1(self):
        """Общая строка «из 26.30.11 … (кроме …, классифицируемых кодами 26.30.11.122, …150 …)» отдавала
        свои 40/55/72 позициям, у которых своя строка: 35/50/72 и 22/47/72."""
        for start, code, own in (("Оборудование коммутации и маршрутизации", "26.30.11.122", "не менее 35 баллов"),
                                 ("Средства связи радиоэлектронные", "26.30.11.150", "не менее 22 баллов")):
            r = _rec(start, code)
            self.assertTrue((self._thr(r) or "").startswith(own), (start, self._thr(r)))

    def test_scanner_ignores_parens_inside_quotes(self):
        """Непарная скобка внутри закавыченного имени не закрывает оговорку раньше времени."""
        line = '26.30.11 "А" (кроме 26.30.11.110 "Б :) скобка"; 26.30.11.121 "В"): - не менее 40 баллов'
        self.assertEqual(t._row_excluded(line), ["26.30.11.110", "26.30.11.121"])

    def test_under_never_matches_empty_key(self):
        self.assertFalse(code_cells.under("26.20.11.120", ""))
        self.assertFalse(code_cells.under("", "26.20"))
        self.assertTrue(code_cells.under("26.20.11.121", "26.20.11.120"))

    def test_own_threshold_instead_of_excluded_codes(self):
        io = self._thr(_rec(IO))
        self.assertIn("не менее 55 баллов", io)
        self.assertNotIn("70 баллов", io)                       # порог принтеров 26.20.16.120
        st = self._thr(_rec(STORAGE))
        self.assertIn("не менее 110 баллов", st)
        self.assertNotIn("23 баллов", st)                       # порог 26.20.21

    def test_excluded_code_positions_keep_their_own(self):
        """Позиции исключённых кодов строку «X (за исключением …)» не получают."""
        self.assertIn("не менее 70 баллов", self._thr(_rec("Принтеры (только", "26.20.16.120")))
        radionav = _rec("Аппаратура радионавигационная", "26.51.20.121")
        self.assertNotIn("не менее 72 баллов", self._thr(radionav) or "")

    def test_lookup_drops_excluded_codes_itself(self):
        """Когда наименование ничью не разрешает, исключённый код дал бы строке «26.20.16.120 Принтеры»
        точное совпадение наравне со своей строкой — и порог не нашёлся бы. `_lookup` снимает
        исключённые коды позиции сам (по `code_cells`), и остаётся своя строка."""
        full = ["26.20.16", "26.20.16.120"]
        with mock.patch.object(code_cells, "marks",
                               lambda s, n, c: ["", code_cells.EXCEPT] if list(c) == full else None):
            got = t.lookup_threshold(full, "Периферия", "IX")
        self.assertIn("не менее 55 баллов", got or "")

    def test_note_parsers_are_cached(self):
        """Разбор примечаний — на процесс, а не на вызов. Первая редакция D20 вставила `_row_excluded`
        между `@lru_cache` и `_flat_thresholds`: кэш ушёл к новой функции, и каждый поиск порога
        заново разбирал все примечания (~0.07 с на вызов; модуль K2-3 — 162 с вместо 0.5)."""
        for fn in (t._flat_thresholds, t._tables, t._note_scopes, t._kind_schedules):
            self.assertTrue(hasattr(fn, "cache_info"), fn.__name__)

    def test_full_and_own_codes_give_the_same_threshold(self):
        """`_lookup` снимает исключённые коды сам: полный список записи и свой список хита — одно."""
        for start in (LAPTOPS, IO, STORAGE, "Субстанции фармацевтические"):
            r = _rec(start)
            self.assertEqual(self._thr(r), self._thr(r, _from_qdrant(r).okpd2_codes), start)


if __name__ == "__main__":
    unittest.main()
