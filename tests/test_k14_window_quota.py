"""`K14` #35: РАЗДАЧА КВОТЫ ОКНА ПО КРУГУ. Офлайн — Qdrant подменён заглушкой.

⚠⚠ ЗАЧЕМ ЗАГЛУШКА, А НЕ ЖИВОЙ КОРПУС. Первые редакции этих утверждений (ревью PR #137, раунды
3–4) звали `search_rules` против живого Qdrant. Локально они зеленели, а CI их валил: там Qdrant
нет, `search_rules` честно деградирует до `[]`, и окно пустое. Это ровно класс `O3` #104 —
«тест, которому понадобятся Qdrant или ключ, упадёт, и это правильный сигнал» (шапка `tests.yml`),
— и я завёл его собственными руками, при зелёной локальной батарее из 1118 тестов.

⚠ Заглушка не «слабее» живого прогона, а СИЛЬНЕЕ для этой цели: она пиняет СЦЕНАРИЙ (документ темы
отсутствует в широком пуле и приходит добором, мест меньше, чем претензий), а не сегодняшнюю
выдачу корпуса, которая завтра изменится. Живое утверждение о настоящем корпусе стоит там, где ему
и место, — в релизной проверке `scripts/deploy/checks/v0.5.0-test23/`, она идёт на боевой машине.

ЧТО ПИНИТСЯ. Квота раздавалась ОДНИМ проходом (`chosen.update(idxs[:quota])`), а `order[:limit]`
резал по индексу пула. Документ, пришедший ДОБОРОМ, дописывается в конец `points`, получает самые
высокие индексы и режется ПЕРВЫМ — ровно тогда, когда добор и понадобился. На главном вопросе
задачи («моей продукции нет в приложении 719, можно ли получить СТ-1») из окна выпадало тело
ПП №719, где живёт норма подпункта «г».

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag import retriever  # noqa: E402


def _point(doc_type: str, n: int):
    """Точка Qdrant в том виде, в каком её читает `search_rules`."""
    return SimpleNamespace(
        id=f"{doc_type}-{n}",
        score=1.0,
        payload={"doc_type": doc_type, "point": str(n), "text": f"{doc_type} пункт {n}",
                 "section_roman": "I", "source_anchor": f"{doc_type}, п. {n}"},
    )


# Широкий пул: тематического документа `decree_body` в нём НЕТ — он мал и по рангу не доходит.
WIDE = ([_point("tpp_order_52", i) for i in range(1, 6)]
        + [_point("sng_origin_rules", i) for i in range(1, 4)]
        + [_point("prikaz14_tpp", i) for i in range(1, 4)]
        + [_point("metodrek_tpp", i) for i in range(1, 3)])


def _fake_hybrid(query, limit, qfilter=None, collection=None, qvec=None):
    """Широкий пул — без `decree_body`; добор по фильтру документа — возвращает его.

    ⚠ Различаем вызовы по НАЛИЧИЮ фильтра: так же, как это делает сам `search_rules`
    (широкий запрос идёт с `alive_only()`, добор — с условием по `doc_type`).
    """
    conditions = getattr(qfilter, "must", None) or []
    for c in conditions:
        key = getattr(c, "key", None)
        if key == "doc_type":
            doc = getattr(getattr(c, "match", None), "value", None)
            return [_point(doc, i) for i in range(1, 4)]
    return list(WIDE[:limit])


class TestDoborDocumentSurvivesTheCut(unittest.TestCase):
    """Документ, пришедший добором, обязан получить место — иначе добор бессмыслен."""

    def _window(self, primary_docs):
        # ⚠⚠ КЛИЕНТ ТОЖЕ ПОДМЕНЁН, И ЭТО НЕ ПЕДАНТИЗМ. `search_rules` начинает с
        # `client.collection_exists(...)` — без живого Qdrant он возвращает `[]` ДО всякой квоты,
        # и тест «проходил» бы, ничего не проверив. Поймано прогоном батареи с недоступным
        # Qdrant (`QDRANT_URL` в никуда) — той самой проверкой, которой мне и не хватало,
        # когда CI покраснел.
        fake_client = unittest.mock.Mock()
        fake_client.collection_exists.return_value = True
        with unittest.mock.patch.object(retriever, "_client", lambda: fake_client), \
             unittest.mock.patch.object(retriever, "_hybrid", _fake_hybrid), \
             unittest.mock.patch.object(retriever, "rules_topic", lambda _q: "tpp_order_52"), \
             unittest.mock.patch.object(retriever, "asks_document_list", lambda _q: False):
            hits = retriever.search_rules("вопрос", limit=6, primary_docs=primary_docs)
        return [h.get("doc_type") for h in hits]

    def test_every_topic_document_gets_a_slot(self):
        """Три документа темы при окне 6: претензий 3+2+2+2 = 9, мест 6 — но каждому по одному."""
        docs = self._window(("sng_origin_rules", "prikaz14_tpp", "decree_body"))
        self.assertEqual(len(docs), 6)
        for want in ("sng_origin_rules", "prikaz14_tpp", "decree_body"):
            with self.subTest(doc=want):
                self.assertIn(want, docs, f"{want} вытеснен из окна: {docs}")

    def test_lexical_primary_keeps_the_majority(self):
        """⚠ Круговая раздача не должна отнимать у первого источника его преимущество.

        `K9` показал: ответ строится вокруг ПЕРВОГО источника, и уравнять всех было бы регрессией.
        """
        docs = self._window(("sng_origin_rules", "prikaz14_tpp", "decree_body"))
        self.assertGreaterEqual(docs.count("tpp_order_52"), 2,
                                f"документ по лексической теме потерял вес: {docs}")

    def test_two_topic_documents_still_work(self):
        """Положительный контроль: прежний состав из двух документов не сломан."""
        docs = self._window(("decree_body", "tpp_order_52"))
        self.assertIn("decree_body", docs)
        self.assertIn("tpp_order_52", docs)

    def test_without_the_topic_nothing_is_reserved(self):
        """Без темы окно набирается общим рангом — добора нет, `decree_body` не появляется."""
        docs = self._window(())
        self.assertNotIn("decree_body", docs, f"добор сработал там, где темы нет: {docs}")


if __name__ == "__main__":
    unittest.main()
