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

    def test_word_form_without_brackets(self):
        """Ревью D15: «сноска 38(3) к требованию» — маркер без угловых скобок."""
        self.assertEqual(retriever.footnote_forms("что означает сноска 38(3) к требованию"),
                         [("<38(3)>", "<38.3>")])
        self.assertEqual(retriever.footnote_forms("сноска N 44"), [("<44>",)])
        self.assertEqual(retriever.footnote_forms("<44> и сноска 44"), [("<44>",)])

    def test_several_and_repeated(self):
        self.assertEqual(retriever.footnote_forms("<12(1)> и <44>, и снова <12(1)>"),
                         [("<12(1)>", "<12.1>"), ("<44>",)])
        self.assertEqual(retriever.footnote_forms("без сносок, код 26.20.11"), [])


def _pt(i, doc="rules_registry", point=None, score=None):
    return SimpleNamespace(id=f"{doc}{i}", score=score if score is not None else 1.0 - i / 100, payload={
        "doc_type": doc, "section_roman": "II", "section_title": "т", "point": point or f"{i}",
        "text": "текст пункта достаточной длины для фильтра пустышек", "source_anchor": f"{doc}, {point or i}"})


class TestRoute(unittest.TestCase):
    """Ревью D15 (MED-1): маркер с точкой читался кодом ОКПД2, и вопрос уходил в товарную ветку,
    где сносок нет. В тексте приложения «<38.N>» встречается даже чаще, чем «<38(N)>»."""

    def test_footnote_mark_is_not_an_okpd2_code(self):
        from app.rag import okpd2_ref, procedural
        self.assertEqual(okpd2_ref.extract_codes("что означает <38.12>"), [])
        self.assertFalse(okpd2_ref.has_okpd2_code("что значит <22.1>"))
        self.assertTrue(procedural.is_procedural("что означает <38.12>"))
        # словами, без угловых скобок: «сноска 38.12» — тоже не код
        self.assertEqual(okpd2_ref.extract_codes("что означает сноска 38.12"), [])
        self.assertTrue(procedural.is_procedural("что означает сноска 38.12"))
        # настоящий код рядом с маркером — остаётся кодом, в том числе после слова «сноска»
        self.assertEqual(okpd2_ref.extract_codes("требования к 26.20.11 и сноска <38.12>"), ["26.20.11"])
        self.assertEqual(okpd2_ref.extract_codes("в сноске 26.20.11"), ["26.20.11"])


class TestSearchRules(unittest.TestCase):
    FOOTNOTES = {"<38.3>": _pt(90, "appendix_footnotes", "<38.3>", 0.1),
                 "<12(1)>": _pt(91, "appendix_footnotes", "<12(1)>", 0.1),
                 # двойник-омоним: своя форма обязана победить, даже когда есть запись другой формы
                 "<12.1>": _pt(92, "appendix_footnotes", "<12.1>", 0.5)}
    TWINS = [_pt(93, "appendix_footnotes", "<25>", 0.3), _pt(94, "appendix_footnotes", "<25>", 0.3)]

    def _run(self, query, only_docs=(), fail=False, pool_extra=(), limit=6):
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
                return [self.FOOTNOTES[f] for f in forms if f in self.FOOTNOTES] +                     [p for p in self.TWINS if p.payload["point"] in forms]
            # пул: чужая сноска <38.13> с высоким рангом и пункты Правил — своей <38.3> в пуле нет
            return [_pt(0, "appendix_footnotes", "<38.13>", 0.99)] + list(pool_extra) +                 [_pt(i) for i in range(1, 8)]

        with mock.patch.object(retriever, "_hybrid", side_effect=fake_hybrid), \
                mock.patch.object(retriever, "_client", return_value=fake_client), \
                mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
            return retriever.search_rules(query, limit=limit, only_docs=only_docs)

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

    def test_two_marks_keep_question_order(self):
        """<12(1)> уже в пуле (индекс меньше), <38.3> — добором (в конце): порядок — как в вопросе."""
        pool_12 = _pt(96, "appendix_footnotes", "<12(1)>", 0.2)
        pool_12.id = self.FOOTNOTES["<12(1)>"].id
        w = self._run("что значит <38(3)> и <12(1)>", pool_extra=[pool_12])
        self.assertEqual([p["point"] for p in w[:2]], ["<38.3>", "<12(1)>"])

    def test_same_key_different_footnotes_both_reserved(self):
        """Под ключом «<25>» две разные сноски — берутся обе, а не одна из словаря."""
        w = self._run("что означает <25>")
        self.assertEqual(sorted(p["source_anchor"] for p in w[:2]),
                         sorted(p.payload["source_anchor"] for p in self.TWINS))

    def test_footnote_already_in_pool_is_not_duplicated(self):
        pool_own = _pt(95, "appendix_footnotes", "<38.3>", 0.2)
        pool_own.id = self.FOOTNOTES["<38.3>"].id                    # та же точка Qdrant
        w = self._run("что значит <38(3)>", pool_extra=[pool_own])
        self.assertEqual([p["point"] for p in w].count("<38.3>"), 1)
        self.assertEqual(w[0]["point"], "<38.3>")

    def test_missing_marks_do_not_eat_the_limit(self):
        """«<7>, <2> и <38(3)>»: несуществующие сноски не съедают предел, своя <38.3> добирается."""
        w = self._run("что значит <7>, <2>, <1> и <38(3)>")
        self.assertEqual(w[0]["point"], "<38.3>")

    def test_word_form_reaches_the_window(self):
        w = self._run("что означает сноска 38(3)")
        self.assertEqual(w[0]["point"], "<38.3>")

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
