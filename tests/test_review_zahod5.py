"""Находки ревью захода 5 (PR #112, `K13` + `K15`), 26.08.2026. Офлайн, Qdrant подменён.

⚠ Каждая находка проверена ВОСПРОИЗВЕДЕНИЕМ на текущем коде до правки: ревью показывает код,
каким он был на момент PR, а с тех пор его правили и заход 5.5, и пакет 26.08.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag import retriever as R  # noqa: E402


class _FakePoint:
    """Точка выдачи Qdrant — минимум, который читает `search_rules`.

    `id` обязателен: порог релевантности сопоставляет гибридную выдачу с dense-пробой ПО ID."""

    def __init__(self, payload: dict, score: float = 1.0, id: int = 1):  # noqa: A002
        self.payload = payload
        self.score = score
        self.id = id


def _pt(doc_type: str, point: str, text: str, id: int, score: float):  # noqa: A002
    return _FakePoint({"doc_type": doc_type, "point": point, "text": text,
                       "section_roman": "4" if point.startswith("4") else "2",
                       "source_anchor": f"{doc_type}, п. {point}", "status": "действует"},
                      score=score, id=id)


class TestDocumentSlotIsNotHijacked(unittest.TestCase):
    """⚠⚠ НОМЕР ПУНКТА НЕ УНИКАЛЕН МЕЖДУ ДОКУМЕНТАМИ, а ключ был по одному номеру.

    `parse_metodrek` нумерует куски как «раздел.кусок» и даёт 4.1–4.4 — РОВНО те же строки, что
    пункты 4.1–4.4 Приказа №52, а `RULES_DOC_LIST_GROUPS` = («4.1», «4.2», «4.3»). Словарь
    `known` строился по `point` в одиночку, и при коллизии побеждал ПОСЛЕДНИЙ вошедший в пул:
    добор, нашедший пункт ПРИКАЗА, помечал `_doc_list` у куска МЕТОДРЕКОМЕНДАЦИЙ. Тот получал
    место в перечне и лимит 4000 знаков, а настоящий п. 4.1 с составом документов в окно не
    попадал вовсе — регрессия того самого `P2` на кластере жалоб №1.

    ⚠ На живом корпусе это НЕ срабатывало: проверено прогоном, Приказ п. 4.1 был в окне с
    `_doc_list`. Но выигрыш доставался ПОРЯДКОМ в пуле, то есть рангом. Здесь порядок задан
    опасный — метод-кусок идёт ПОСЛЕ пункта Приказа, — и тест ловит именно его."""

    ORDER_52 = "tpp_order_52"
    METODREK = "metodrek_tpp"

    def setUp(self):
        self._orig = (R._client, R._hybrid, R.embed_query)
        # Основной пул: пункт Приказа 4.1 — ПЕРВЫМ, метод-кусок 4.1 — ПОСЛЕ него (опасный порядок).
        pool = [_pt(self.ORDER_52, "4.1", "Состав документов, прилагаемых к заявлению.", 1, 0.9),
                _pt(self.METODREK, "4.1", "Разъяснение о документах.", 2, 0.8)]
        # Добор раздела 4 отфильтрован до Приказа — возвращает ЕГО пункт.
        extra = [_pt(self.ORDER_52, "4.1", "Состав документов, прилагаемых к заявлению.", 1, 0.9)]
        calls = {"n": 0}

        def fake_hybrid(*a, **kw):
            calls["n"] += 1
            return pool if calls["n"] == 1 else extra

        # ⚠ Клиент обязан отвечать `collection_exists` = True. Заглушка `lambda: None` роняла
        # `search_rules` в except и возвращала ПУСТО — тест «работал» бы, не исполнив ни одной
        # проверяемой строки. Урок проекта: заглушка, возвращающая пусто, обрывает путь.
        class _FakeClient:
            def collection_exists(self, _name):
                return True

        R._hybrid = fake_hybrid
        R.embed_query = lambda *a, **kw: [0.0]
        R._client = lambda: _FakeClient()
        self.calls = calls

    def tearDown(self):
        R._client, R._hybrid, R.embed_query = self._orig

    def test_doc_list_slot_points_at_the_order_not_at_metodrek(self):
        got = R.search_rules("какие документы подготовить", limit=6)
        marked = [p for p in got if p.get("_doc_list")]
        self.assertTrue(marked, "перечень не собран вовсе")
        for p in marked:
            with self.subTest(anchor=p.get("source_anchor")):
                self.assertEqual(p.get("doc_type"), self.ORDER_52,
                                 "место в перечне занял кусок Методрекомендаций")

    def test_the_collision_is_real_in_the_data(self):
        """Отрицательный контроль: если бы номера не совпадали, чинить было бы нечего."""
        self.assertIn("4.1", R.RULES_DOC_LIST_GROUPS)
        got = R.search_rules("какие документы подготовить", limit=6)
        points = {(p.get("doc_type"), str(p.get("point"))) for p in got}
        self.assertIn((self.ORDER_52, "4.1"), points)
        self.assertIn((self.METODREK, "4.1"), points)


class TestExcludeFragmentsHasSomeoneToHonorIt(unittest.TestCase):
    """⚠⚠ ПРАВИЛО, КОТОРОЕ НЕКОМУ ИСПОЛНИТЬ, — ХУЖЕ ОТСУТСТВУЮЩЕГО: оно ещё и документировано.

    `cut_fragments` зовётся ровно из одного парсера (`parse_metodrek`); остальные принимают `doc`
    и правило молча игнорируют, а `load_manifest` посторонних ключей не проверяет. Манифест при
    этом подаёт `exclude_fragments` как ОБЩИЙ механизм — с обоснованием «несовпавшее правило
    останавливает загрузку». Отменённая норма вернулась бы в индекс ровно тем способом, от
    которого `exclude_fragments` и заводилась."""

    def _mod(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import load_rules_kb

        return load_rules_kb

    def test_every_declared_rule_has_a_parser_that_cuts(self):
        m = self._mod()
        from app.core import manifest as kb

        # ⚠⚠ ТЕ ЖЕ ДВА ФИЛЬТРА, ЧТО У ГАРДА (ревью PR #127, раунд 4). Гард в `load_rules_kb`
        # видит только документы СВОЕЙ коллекции и только ДЕЙСТВУЮЩИЕ — второе сделано
        # намеренно: у документа, который в индекс не идёт, `exclude_fragments` исполнять
        # НЕКОМУ по законной причине. Тест без этих фильтров КРАСНЕЕТ на верном коде, стоит
        # дописать правило утратившему силу документу или записи другого корпуса. Проверка,
        # строже защищаемого ею кода, — предохранитель, бьющий по верному состоянию.
        declared = {d["doc_type"] for d in kb.load_manifest()["documents"]
                    if d.get("exclude_fragments")
                    and d.get("collection") == m.COLLECTION
                    and d.get("status") == kb.ACTIVE}
        orphan = declared - m.SUPPORTS_EXCLUDE_FRAGMENTS
        self.assertEqual(orphan, set(),
                         f"правило объявлено, а резать его некому: {sorted(orphan)}")

    def test_the_set_is_not_wider_than_reality(self):
        """Отрицательный контроль: список поддерживающих не должен раздуваться «на всякий случай»."""
        import inspect

        m = self._mod()
        for dt in m.SUPPORTS_EXCLUDE_FRAGMENTS:
            with self.subTest(doc_type=dt):
                src = inspect.getsource(m.PARSERS[dt])
                self.assertIn("cut_fragments", src,
                              f"{dt} числится режущим, но cut_fragments не зовёт")

    def test_declaring_it_for_an_unsupported_doc_stops_the_load(self):
        """Предохранитель обязан стоять НА ПУТИ действия, а не рядом с ним."""
        m = self._mod()
        man = {"documents": [{"doc_type": "rules_registry", "collection": m.COLLECTION,
                              "status": "действует", "exclude_fragments": [{"reason": "x"}],
                              "sources": []}]}
        with self.assertRaises(SystemExit) as cm:
            m.load_records(man)
        self.assertIn("exclude_fragments", str(cm.exception))


class TestDocumentsTableIsHonestAboutItself(unittest.TestCase):
    """⚠ Докстринг обещал «печатает КОД, а не модель» — а функцию не зовёт ни один модуль продукта.

    Опаснее самой недоделки было то, что тесты вокруг неё читались как подтверждение подключения:
    перечень в ответе воспроизводит МОДЕЛЬ, и гарантии «дословно из данных» у него нет."""

    def test_table_is_not_wired_into_the_answer(self):
        import re

        used = []
        for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            if re.search(r"documents_table\s*\(", src) and path.name != "documents_ref.py":
                used.append(str(path.relative_to(ROOT)))
        if used:
            self.fail(f"функция ПОДКЛЮЧЕНА ({used}) — перепишите докстринг "
                      f"`documents_table` и этот тест: обещание про «печатает КОД» стало правдой")

    def test_docstring_says_so(self):
        from app.rag import documents_ref

        self.assertIn("В ОТВЕТ НЕ ПОДКЛЮЧЕНА", documents_ref.documents_table.__doc__ or "")


class TestKnownIsKeyedByDocumentAndPoint(unittest.TestCase):
    """Ключ обязан быть парой. Проверка структурная — намеренно, в дополнение к поведенческой:
    поведенческая ловит дефект на ОДНОМ порядке, а ключ отвечает за все."""

    def test_key_is_a_pair(self):
        import inspect

        src = inspect.getsource(R.search_rules)
        self.assertIn('(p.payload or {}).get("doc_type"), (p.payload or {}).get("point")', src,
                      "ключ добора снова не различает документы")


if __name__ == "__main__":
    unittest.main()
