r"""Инкрементальная переиндексация по документу — `K11` (#41). Офлайн, без Qdrant.

ЗАЧЕМ. `recreate_collection` сносит коллекцию целиком: добавление одного приказа переиндексирует
все 311 пунктов, то есть прогоняет e5 по всему корпусу. Дальше в очереди четыре задачи, которые
ДОБАВЛЯЮТ документы (`K15`, `K14`, `K16`, `K17`) — без `K11` каждая платит полной переиндексацией.

⚠⚠ ГЛАВНЫЙ ТЕСТ ЗДЕСЬ — ПРО `avgdl`, И ОН НЕ ОЧЕВИДЕН. `document_vector` ЗАПЕКАЕТ среднюю длину
документа в значения sparse-вектора (нормировка BM25 по длине). Посчитай её по одному документу —
и его вектора окажутся в другом масштабе, чем у соседей по коллекции: у Приказа №52 пункты
длинные, у сносок короткие, средние отличаются кратно. Ранжирование поедет у ВСЕХ, а числа
останутся правдоподобными — ни один тест выдачи такого не покажет. Поэтому частичная загрузка
разбирает весь корпус и считает `avgdl` по нему.

⚠ Qdrant подменён. Проверяется то, что действительно во власти теста: КАКИЕ точки уходят в
upsert, с каким `avgdl`, и КАКОЙ фильтр идёт в delete. Урок `EV16`: тест, которому нужен живой
сервис, ломает офлайновость батареи.

Запуск:  .venv\Scripts\python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


loader = _load("load_rules_kb_k11", "scripts/load_rules_kb.py")


class _FakeQdrant:
    """Считает обращения и запоминает, ЧТО именно ушло. Без сети."""

    def __init__(self, exists: bool = True):
        self.exists = exists
        self.upserted: list = []
        self.deletes: list = []
        self.recreated = 0

    def collection_exists(self, name):
        return self.exists

    def upsert(self, collection_name, points):
        self.upserted.extend(points)

    def delete(self, collection_name, points_selector):
        self.deletes.append(points_selector)

    def delete_collection(self, name):
        self.recreated += 1

    def get_collection(self, name):
        return type("Info", (), {"points_count": len(self.upserted)})()


class K11Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with redirect_stdout(io.StringIO()):
            cls.recs, _ = loader.load_records()
        # e5 в батарее не запускаем: вектор для теста не важен, важен состав точек и avgdl.
        cls._orig_embed = sys.modules.get("app.rag.embeddings")

    def setUp(self):
        self.client = _FakeQdrant()
        self.seen_avgdl: list[float] = []
        self._orig_index_all = loader.index_all

        def spy(client, recs, batch=64, avgdl=None):
            self.seen_avgdl.append(avgdl)
            self._orig_index_all_args = list(recs)
            for r in recs:
                client.upsert(collection_name="x", points=[type("P", (), {
                    "id": loader.point_id(r), "payload": r})()])

        loader.index_all = spy

    def tearDown(self):
        loader.index_all = self._orig_index_all


class TestOnlyTargetDocumentIsTouched(K11Base):
    def test_upserts_only_the_target_document(self):
        loader.reindex_document(self.client, "appendix_footnotes", self.recs)
        docs = {p.payload["doc_type"] for p in self.client.upserted}
        self.assertEqual(docs, {"appendix_footnotes"})
        self.assertEqual(len(self.client.upserted),
                         sum(1 for r in self.recs if r["doc_type"] == "appendix_footnotes"))

    def test_collection_is_not_recreated(self):
        """Критерий приёмки `K11`: точки остальных документов не меняются. Пересоздание
        коллекции — прямое их уничтожение."""
        loader.reindex_document(self.client, "appendix_footnotes", self.recs)
        self.assertEqual(self.client.recreated, 0)

    def test_ids_of_other_documents_are_stable_by_construction(self):
        """Идентификатор — uuid5 от содержания (`point_id`), значит точки нетронутых документов
        сохраняют свои id при любой переиндексации соседа. Проверяем это, а не верим на слово."""
        before = {loader.point_id(r) for r in self.recs if r["doc_type"] != "appendix_footnotes"}
        loader.reindex_document(self.client, "appendix_footnotes", self.recs)
        after = {loader.point_id(r) for r in self.recs if r["doc_type"] != "appendix_footnotes"}
        self.assertEqual(before, after)
        self.assertFalse(before & {p.id for p in self.client.upserted},
                         "в upsert уехали точки чужих документов")


class TestAvgdlIsCorpusWide(K11Base):
    """⚠⚠ Тот самый неочевидный инвариант, ради которого этот файл и написан."""

    def test_avgdl_is_taken_over_the_whole_corpus(self):
        loader.reindex_document(self.client, "appendix_footnotes", self.recs)
        self.assertEqual(self.seen_avgdl, [loader.corpus_avgdl(self.recs)])

    def test_document_own_avgdl_would_be_different(self):
        """Положительный контроль: если бы значения совпадали, тест выше ничего не проверял бы.

        Сноски — короткие определения, Приказ №52 — длинные пункты; корпусная средняя обязана
        заметно отличаться от средней любого из них."""
        only = [r for r in self.recs if r["doc_type"] == "appendix_footnotes"]
        corpus, own = loader.corpus_avgdl(self.recs), loader.corpus_avgdl(only)
        self.assertGreater(abs(corpus - own) / corpus, 0.1,
                           f"корпусная {corpus:.1f} и своя {own:.1f} слишком близки — "
                           "образец перестал показывать разницу масштабов")

    def test_full_load_still_computes_avgdl_itself(self):
        """Полная загрузка `avgdl` не передаёт — считает сама. Иначе правка K11 меняла бы и её."""
        loader.index_all = self._orig_index_all
        import inspect

        sig = inspect.signature(loader.index_all)
        self.assertIsNone(sig.parameters["avgdl"].default)


class TestStalePointsAreCleaned(K11Base):
    def test_delete_targets_only_this_document_and_spares_fresh_points(self):
        """Уборка обязана быть точечной: `doc_type` этого документа И НЕ входящие в новую выдачу.

        Без `must_not` по свежим id удаление снесло бы только что загруженное; без условия по
        `doc_type` — чужие документы."""
        loader.reindex_document(self.client, "appendix_footnotes", self.recs)
        self.assertEqual(len(self.client.deletes), 1)
        flt = self.client.deletes[0].filter
        self.assertEqual([c.match.value for c in flt.must], ["appendix_footnotes"])
        fresh = {loader.point_id(r) for r in self.recs if r["doc_type"] == "appendix_footnotes"}
        self.assertEqual(set(flt.must_not[0].has_id), fresh)

    def test_upsert_happens_before_delete(self):
        """⚠ Порядок не косметика: «сначала удалить, потом загрузить» даёт окно, в котором
        пункты документа отсутствуют, — а сервис в это время отвечает."""
        order: list[str] = []
        self.client.upsert = lambda collection_name, points: order.append("upsert")
        self.client.delete = lambda collection_name, points_selector: order.append("delete")
        loader.reindex_document(self.client, "appendix_footnotes", self.recs)
        self.assertEqual(order[-1], "delete")
        self.assertIn("upsert", order[:-1])


class TestRefusesWhenItCannotBeCorrect(K11Base):
    def test_missing_collection_stops_instead_of_creating_a_partial_one(self):
        """Частичная загрузка в несуществующую коллекцию дала бы корпус из одного документа —
        и сервис отвечал бы, как будто всё на месте."""
        self.client.exists = False
        with self.assertRaises(SystemExit):
            loader.reindex_document(self.client, "appendix_footnotes", self.recs)

    def test_unknown_document_stops(self):
        with self.assertRaises(SystemExit):
            loader.reindex_document(self.client, "нет-такого-документа", self.recs)


if __name__ == "__main__":
    unittest.main()
