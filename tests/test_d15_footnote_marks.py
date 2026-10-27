"""D15 #155: сноска по маркеру из вопроса — своя запись первой.

Определения сносок 38 записаны «<38.1>»…«<38.14>», а ссылки в требованиях приложения — «<38(1)>»…,
и пользователь пишет так, как видит в требовании. Замер 27.10.2026 по всем 76 сноскам корпуса
(«что означает <маркер>» в форме ссылки): своя сноска первой в окне — 34 из 76 до правки (сбоили и
простые «<10>» → <38.10>, «<1>» → <56>), 76 из 76 после. Офлайн: Qdrant и ключ не нужны."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.rag import retriever


class TestForms(unittest.TestCase):
    def test_paren_and_dot_are_twins(self):
        self.assertEqual(retriever.footnote_forms("что значит <38(3)>"), [("<38(3)>", "<38.3>")])
        self.assertEqual(retriever.footnote_forms("а <38.12>?"), [("<38.12>", "<38(12)>")])
        self.assertEqual(retriever.footnote_forms("сноска <44>"), [("<44>",)])

    def test_several_and_repeated(self):
        self.assertEqual(retriever.footnote_forms("<12(1)> и <44>, и снова <12(1)>"),
                         [("<12(1)>", "<12.1>"), ("<44>",)])
        self.assertEqual(retriever.footnote_forms("без сносок, код 26.20.11"), [])


def _pt(i, doc="rules_registry", point=None, score=None):
    return SimpleNamespace(id=f"{doc}{i}", score=score if score is not None else 1.0 - i / 100, payload={
        "doc_type": doc, "section_roman": "II", "section_title": "т", "point": point or f"{i}",
        "text": "текст пункта достаточной длины для фильтра пустышек", "source_anchor": f"{doc}, {point or i}"})


class TestSearchRules(unittest.TestCase):
    FOOTNOTES = {"<38.3>": _pt(90, "appendix_footnotes", "<38.3>", 0.1),
                 "<12(1)>": _pt(91, "appendix_footnotes", "<12(1)>", 0.1),
                 # двойник-омоним: своя форма обязана победить, даже когда есть запись другой формы
                 "<12.1>": _pt(92, "appendix_footnotes", "<12.1>", 0.5)}

    def _run(self, query, only_docs=(), fail=False):
        fake_client = mock.Mock()
        fake_client.collection_exists.return_value = True
        self.asked = []

        def fake_hybrid(query, limit, collection=None, qvec=None, qfilter=None, **kw):
            conds = getattr(qfilter, "must", None) or []
            points = [c for c in conds if getattr(c, "key", "") == "point"]
            if points:
                forms = list(points[0].match.any)
                self.asked.append(forms)
                if fail:
                    raise RuntimeError("Qdrant недоступен")
                return [self.FOOTNOTES[f] for f in forms if f in self.FOOTNOTES]
            # пул: чужая сноска <38.13> с высоким рангом и пункты Правил — своей <38.3> в пуле нет
            return [_pt(0, "appendix_footnotes", "<38.13>", 0.99)] + [_pt(i) for i in range(1, 8)]

        with mock.patch.object(retriever, "_hybrid", side_effect=fake_hybrid), \
                mock.patch.object(retriever, "_client", return_value=fake_client), \
                mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
            return retriever.search_rules(query, limit=6, only_docs=only_docs)

    def test_own_footnote_first_by_twin_form(self):
        w = self._run("что значит <38(3)>")
        self.assertEqual(self.asked, [["<38(3)>", "<38.3>"]])
        self.assertEqual(w[0]["point"], "<38.3>")
        self.assertEqual(len(w), 6)

    def test_exact_form_preferred(self):
        w = self._run("что означает <12(1)>")
        self.assertEqual(w[0]["point"], "<12(1)>")

    def test_not_a_footnote_question_keeps_order(self):
        """Маркер в вопросе о другом (тема — не сноски): место сноске гарантировано, но первой она не
        становится — ответ строится вокруг первого источника (урок K9)."""
        w = self._run("какие сроки рассмотрения заявления по требованию с отметкой <38(3)>")
        points = [p["point"] for p in w]
        self.assertIn("<38.3>", points)
        self.assertNotEqual(points[0], "<38.3>")

    def test_no_marker_no_extra_query(self):
        self._run("какие сроки рассмотрения заявления")
        self.assertEqual(self.asked, [])

    def test_only_docs_without_footnotes_skips_fetch(self):
        self._run("что значит <38(3)>", only_docs=("rules_registry",))
        self.assertEqual(self.asked, [])

    def test_failed_fetch_keeps_the_answer(self):
        w = self._run("что значит <38(3)>", fail=True)
        self.assertEqual(self.asked, [["<38(3)>", "<38.3>"]])
        self.assertTrue(w)
        self.assertNotIn("<38.3>", [p["point"] for p in w])


if __name__ == "__main__":
    unittest.main()
