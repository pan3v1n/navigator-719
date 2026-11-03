"""R31-3 #247: слияние RRF основного пула (`retriever._hybrid`) — на нашей стороне.

После R31-2 #245 целевая позиция между прогонами не плавала, а хвост окна плавал: два прогона
`eval_okpd2_climb` подряд меняли порядок окна у 16 own / 19 climb / 3 control вопросов. Причина та
же, что у R31-2, только в пуле: `FusionQuery(RRF)` раздаёт ничьим внутри списка dense или sparse
разные ранги в произвольном порядке, и состав пула на границе `limit` зависел от случая. `_hybrid`
— общий путь всех веток (пул и доборы товарной, процедурная, кейсы), поэтому правка там.

Офлайн: Qdrant и модель не нужны — `_client` и `embed_query` подменены. Подмена Qdrant отдаёт
ничьи списка в ЗАДАННОМ порядке: им и проверяется, что выдача от него не зависит."""
from __future__ import annotations

import random
import unittest
from fractions import Fraction
from types import SimpleNamespace
from unittest import mock

from app.rag import retriever


def _rrf(*ranks: int) -> float:
    return float(sum(Fraction(1, retriever._RRF_K + r) for r in ranks))


def _tie(start: int, n: int) -> Fraction:
    """Вклад ничьей из `n` точек на местах `start…start+n-1` — средний по этим местам."""
    return sum(Fraction(1, retriever._RRF_K + r) for r in range(start, start + n)) / n


# b и c: ничья dense на местах 1–2 и ничья sparse на местах 0–1.
BC = float(_tie(1, 2) + _tie(0, 2))


class _Qdrant:
    """Подмена Qdrant: `dense`/`sparse` — списки `(id, score)`; точки с равным score отдаются в
    порядке, перемешанном `seed` (так раздавал ничьи `FusionQuery`). Payload — полный словарь по id;
    список отдаёт только поля, которые просит `with_payload` (список имён), как Qdrant."""

    def __init__(self, dense, sparse, payloads, seed=0):
        self.dense, self.sparse, self.payloads = dense, sparse, payloads
        self.rng = random.Random(seed)
        self.batches: list = []

    def _shuffled(self, rows):
        groups: dict = {}
        for pid, s in rows:
            groups.setdefault(s, []).append(pid)
        out = []
        for s in sorted(groups, reverse=True):
            ids = groups[s][:]
            self.rng.shuffle(ids)
            out += [(pid, s) for pid in ids]
        return out

    def _payload(self, pid, sel):
        if sel is False or sel is None:
            return None
        full = self.payloads[pid]
        return dict(full) if sel is True else {k: full[k] for k in sel if k in full}

    def query_batch_points(self, collection_name, requests):
        self.batches.append((collection_name, requests))
        out = []
        for req in requests:
            if req.using == retriever.DENSE:
                rows = self.dense
            elif req.using == retriever.SPARSE:
                rows = self.sparse
            else:
                raise ValueError(f"нет вектора {req.using!r}")      # Qdrant ответил бы 400
            out.append(SimpleNamespace(points=[
                SimpleNamespace(id=pid, score=s, version=7, payload=self._payload(pid, req.with_payload))
                for pid, s in self._shuffled(rows)[:req.limit]]))
        return out

    def retrieve(self, *a, **kw):
        # Обращение к Qdrant на гибрид — одно: второй запрос за payload замерен и отвергнут (латентность,
        # без keep-alive к localhost — второе TCP-соединение на каждый поиск).
        raise AssertionError("payload — в списках, а не вторым запросом")

    def query_points(self, **kw):
        # Чистый dense-зонд кейсов (`_case_dense_scores`) — законен; слияние через `FusionQuery` — нет.
        if kw.get("prefetch") is not None or kw.get("query") is None:
            raise AssertionError("гибрид снова через FusionQuery")
        return SimpleNamespace(points=[SimpleNamespace(id=pid, score=s, payload=None)
                                       for pid, s in self.dense[:kw.get("limit", 10)]])

    def collection_exists(self, name):  # noqa: ARG002
        return True


def _payload(pid, anchor):
    return {"source_anchor": anchor, "product_name": pid, "section_roman": "I", "section_title": "т",
            "okpd2_codes": [], "requirement_blocks": [{"text": f"требования {pid}"}]}


# Ничьи в ОБОИХ списках: dense b = c (0.8), sparse c = b (5.0). Ранги dense a0 b1 c1 d3, sparse c0 b0 e2 a3.
DENSE = [("a", 0.9), ("b", 0.8), ("c", 0.8), ("d", 0.7)]
SPARSE = [("c", 5.0), ("b", 5.0), ("e", 4.0), ("a", 1.0)]
# Якорь решает ничью сумм, и порядок якорей ОБРАТЕН порядку id: «c» раньше «b» только по якорю.
PAYLOADS = {"a": _payload("a", "В"), "b": _payload("b", "Б"), "c": _payload("c", "А"),
            "d": _payload("d", "Д"), "e": _payload("e", "Г")}


class TestHybridTies(unittest.TestCase):

    def _run(self, limit=5, seed=0, dense=DENSE, sparse=SPARSE, payloads=PAYLOADS, **kw):
        fake = _Qdrant(dense, sparse, payloads, seed=seed)
        with mock.patch.object(retriever, "_client", return_value=fake), \
                mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
            pts = retriever._hybrid("вопрос", limit, qvec=kw.pop("qvec", None), **kw)
        return pts, fake

    def test_tie_order_in_list_does_not_change_result(self):
        """Главное: порядок, в котором Qdrant отдал ничьи списка, на выдачу не влияет. С рангом по
        месту (как `FusionQuery`) у b и c суммы разные и зависят от перемешивания."""
        want = [("c", BC), ("b", BC), ("a", _rrf(0, 3)), ("e", _rrf(2)), ("d", _rrf(3))]
        for seed in range(24):
            pts, _ = self._run(seed=seed)
            self.assertEqual([(p.id, p.score) for p in pts], want, f"seed {seed}")

    def test_cut_at_limit_decided_by_anchor_not_by_answer_order(self):
        """Ничья сумм на границе `limit`: в пул входит запись с меньшим якорем (как у `_order_key`),
        а не та, что Qdrant отдал раньше. id упорядочены обратно якорям — тай-брейк по id дал бы «b»."""
        for seed in range(24):
            pts, _ = self._run(limit=2, seed=seed)
            self.assertEqual([p.id for p in pts], ["c", "b"], f"seed {seed}")
            pts, _ = self._run(limit=1, seed=seed)
            self.assertEqual([p.id for p in pts], ["c"], f"seed {seed}")

    def test_scores_without_ties_are_qdrant_rrf(self):
        """Без ничьих сумма та же, что у `FusionQuery` (k = 2, ранг с нуля); точка только из одного
        списка получает вклад только его."""
        pts, _ = self._run(dense=[("a", 0.9), ("b", 0.8), ("c", 0.7)], sparse=[("c", 3.0), ("a", 1.0)])
        self.assertEqual([(p.id, p.score) for p in pts],
                         [("a", _rrf(0, 1)), ("c", _rrf(2, 0)), ("b", _rrf(1))])

    def test_request_shape(self):
        """Оба списка: свой вектор, ТОТ ЖЕ фильтр, глубина max(limit, 20), с payload, одно обращение;
        коллекция — переданная. У точек выдачи — полный payload и слитый score."""
        from qdrant_client import models
        flt = models.Filter(must=[models.FieldCondition(key="doc_type", match=models.MatchValue(value="x"))])
        for limit, depth in ((5, 20), (40, 40)):
            pts, fake = self._run(limit=limit, qfilter=flt, collection="col", qvec=[0.5] * 8)
            [(name, reqs)] = fake.batches
            self.assertEqual(name, "col")
            self.assertEqual([r.using for r in reqs], [retriever.DENSE, retriever.SPARSE])
            self.assertEqual(reqs[0].query, [0.5] * 8)                       # вектор не пересчитан
            for r in reqs:
                self.assertEqual(r.filter, flt)
                self.assertEqual(r.limit, depth)
                self.assertIs(r.with_payload, True)
                self.assertIsNone(r.params)                                  # не замер — HNSW
            self.assertTrue(all(p.payload == PAYLOADS[p.id] for p in pts))  # полный payload
            self.assertTrue(all(p.version == 7 for p in pts))

    def test_exact_search_reaches_both_lists(self):
        with mock.patch.object(retriever, "EXACT_SEARCH", True):
            _, fake = self._run()
        for r in fake.batches[0][1]:
            self.assertTrue(r.params.exact)

    def test_without_payload(self):
        pts, fake = self._run(with_payload=False)
        self.assertTrue(all(r.with_payload is False for r in fake.batches[0][1]))
        self.assertEqual([p.id for p in pts][:2], ["b", "c"])          # без якоря — по id
        self.assertTrue(all(p.payload is None for p in pts))


class TestTieShareIsMean(unittest.TestCase):
    """Вклад ничьей — средний по её местам (ожидание при случайном порядке `FusionQuery`), а не лучший
    ранг: лучший задаром поднимал всю группу ничьей. Замер по базе — recall@1 по наименованию 1501 →
    1498 при лучшем ранге, 1501 → 1501 при среднем вкладе."""

    def test_tie_group_does_not_overtake_named_position(self):
        """Как «Катализаторы гидроочистки»: позиция, названная дословно, — первая в dense и седьмая в
        sparse; соседи по разделу — dense 1–4 и ничья sparse на местах 1–4. С лучшим рангом сосед
        получал 1/3 + 1/3 и обходил её (1/2 + 1/8)."""
        dense = [("x", 0.90), ("y1", 0.85), ("y2", 0.84), ("y3", 0.83), ("y4", 0.82)]
        sparse = [("w", 9.0), ("y1", 5.0), ("y2", 5.0), ("y3", 5.0), ("y4", 5.0), ("v", 4.0), ("x", 3.0)]
        got = retriever._rrf_tied([dense, sparse])
        self.assertEqual(got["x"], _rrf(0, 6))
        self.assertEqual(got["y1"], float(Fraction(1, 3) + _tie(1, 4)))
        self.assertGreater(got["x"], max(got[k] for k in ("y1", "y2", "y3", "y4")))

    def test_tie_keeps_total_of_its_places(self):
        """Ничья делит вклад своих мест поровну: сумма по группе та же, что при любом порядке прихода."""
        sparse = [("a", 5.0), ("b", 5.0), ("c", 5.0), ("d", 1.0)]
        got = retriever._rrf_tied([sparse])
        self.assertEqual({got["a"], got["b"], got["c"]}, {float(_tie(0, 3))})
        self.assertAlmostEqual(sum(got[k] for k in "abc"),
                               float(sum(Fraction(1, retriever._RRF_K + r) for r in range(3))), places=12)
        self.assertEqual(got["d"], _rrf(3))                    # после ничьей трёх — четвёртое место


class TestBranchesGoThroughOwnFusion(unittest.TestCase):
    """Все ветки — товарная, процедурная и кейсы — ищут через `_hybrid` со слиянием на нашей стороне:
    подмена без `FusionQuery` им отвечает, и их выдача не зависит от порядка ничьих."""

    def _with(self, payloads, seed):
        fake = _Qdrant(DENSE, SPARSE, payloads, seed=seed)
        return fake, mock.patch.object(retriever, "_client", return_value=fake)

    def test_product_search_stable(self):
        seen = set()
        for seed in range(12):
            fake, patch = self._with(PAYLOADS, seed)
            with patch, mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
                hits = retriever.search("вопрос", limit=3)
            self.assertTrue(fake.batches)
            seen.add(tuple(h.source_anchor for h in hits))
        self.assertEqual(seen, {("А", "Б", "В")})

    def test_rules_search_stable(self):
        rules = {pid: {"doc_type": "rules_registry", "point": str(i), "section_roman": "II",
                       "section_title": "т", "text": f"пункт {pid}", "source_anchor": p["source_anchor"]}
                 for i, (pid, p) in enumerate(sorted(PAYLOADS.items()))}
        seen = set()
        for seed in range(12):
            fake, patch = self._with(rules, seed)
            with patch, mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
                got = retriever.search_rules("порядок внесения в реестр", limit=3)
            self.assertTrue(fake.batches)
            seen.add(tuple(r.get("text") for r in got))
        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(len(next(iter(seen))), 3)

    def test_cases_search_stable(self):
        cases = {pid: {"query": f"кейс {pid}", "expert_answer": "ответ", "source_anchor": p["source_anchor"]}
                 for pid, p in PAYLOADS.items()}
        seen = set()
        for seed in range(12):
            fake, patch = self._with(cases, seed)
            with patch, mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8), \
                    mock.patch.object(retriever.settings, "CASE_RELEVANCE_MIN", 0.0):
                got = retriever.search_cases("вопрос", limit=2)
            self.assertTrue(fake.batches)
            seen.add(tuple((c["query"], c["_score"]) for c in got))
        self.assertEqual(seen, {(("кейс c", BC), ("кейс b", BC))})


if __name__ == "__main__":
    unittest.main()
