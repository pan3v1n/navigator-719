"""D14 #141: иерархия ОКПД2 поразрядная, а не посегментная; целевая — ближайшая по коду позиция.

Бой, сентябрь 2026, два эксперта: н-бутан 20.14.11.112 → «требования 20.14.11.110 применять нельзя»,
хладон 20.14.11.122 → катализаторы. Корень — два дефекта на одном пути:
  1) `okpd2_match` сравнивал коды по сегментам и не видел, что «…YZ0» — группировка «…YZW»
     (а также подгруппа «XX.XX.X» и подкласс «XX.X» — замер `scripts/diag_okpd2_climb.py`);
  2) среди совпавших по коду целевой становилась первая по score, а не ближайшая по коду:
     14 «Катализаторов» с кодом класса «20» обходили подкатегорию 20.14.11.110.
Офлайн: Qdrant и ключ не нужны (`_hybrid`, `embed_query`, `_client` подменены)."""
from __future__ import annotations

import json
import random
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.rag import okpd2_ref, pipeline, retriever
from app.rag.retriever import Hit, code_relation, okpd2_match

ROOT = Path(__file__).resolve().parents[1]


def _corpus_codes() -> list[str]:
    out = []
    for f in sorted((ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json")):
        for r in json.loads(f.read_text(encoding="utf-8")):
            out += r.get("okpd2_codes") or []
    return sorted(set(out))


def _old_match(rec: str, q: str) -> bool:
    """Правило до D14 — посегментный префикс в любую сторону (копия для проверки надмножества)."""
    a = [s for s in rec.split(".") if s]
    b = [s for s in q.split(".") if s]
    n = min(len(a), len(b))
    return bool(n) and a[:n] == b[:n]


def _seg_prefixes(code: str) -> list[str]:
    s = [x for x in code.split(".") if x]
    return [".".join(s[: i + 1]) for i in range(len(s))]


class TestHierarchy(unittest.TestCase):
    def test_key_strips_only_ninth_level_padding(self):
        self.assertEqual(okpd2_ref.okpd2_key("20.14.11.110"), "20141111")
        self.assertEqual(okpd2_ref.okpd2_key("27.12.31.000"), "271231")
        self.assertEqual(okpd2_ref.okpd2_key("01.11.31.100"), "0111311")
        # ⚠ ноль вида — разряд, а не заполнитель: «27.12.10» не равен подгруппе «27.12.1»
        self.assertEqual(okpd2_ref.okpd2_key("27.12.10"), "271210")
        self.assertEqual(okpd2_ref.okpd2_key("не код"), "")

    def test_relation_on_expert_cases(self):
        rel = okpd2_ref.okpd2_relation
        self.assertEqual(rel("20.14.11.110", "20.14.11.112"), 1)     # н-бутан → подкатегория
        self.assertEqual(rel("20.14.11.120", "20.14.11.122"), 1)     # хладон (пропилен)
        self.assertEqual(rel("20.14.11.112", "20.14.11.110"), -1)    # обратное — потомок
        self.assertEqual(rel("27.12.31", "27.12.31.000"), 0)         # «…000» — тот же вид
        self.assertEqual(rel("27.3", "27.32.13.110"), 5)             # подкласс: «273» → «27321311»
        self.assertEqual(rel("29.10.2", "29.10.21.000"), 1)          # подгруппа: легковые
        # по разрядам «…131» — подкатегория «…13», а не «…12»: подъёма к 120 нет
        self.assertIsNone(rel("20.14.11.120", "20.14.11.131"))
        self.assertIsNone(rel("27.12.10", "27.12.11"))               # соседние виды
        self.assertTrue(okpd2_match(["20.14.11.110"], "20.14.11.112"))
        self.assertTrue(okpd2_match(["29.20.23"], "29.20.23.110"))   # прежний подъём цел

    def test_new_rule_is_superset_of_old_on_whole_classifier(self):
        """Ни одна связь, которую видело старое правило, не потеряна (весь справочник × корпус)."""
        corpus = _corpus_codes()
        by_class: dict[str, list[str]] = {}
        for c in corpus:
            by_class.setdefault(c[:2], []).append(c)
        lost = [(rc, q) for q in okpd2_ref._okpd2_names() for rc in by_class.get(q[:2], [])
                if _old_match(rc, q) and not okpd2_match([rc], q)]
        self.assertEqual(lost, [])

    def test_filter_forms_agree_with_relation(self):
        """⚠ ДВЕ ТАБЛИЦЫ ПРО ОДНО: формы для фильтров Qdrant (`okpd2_codes` ∋ предок,
        `okpd2_prefixes` ∋ форма потомка) обязаны находить РОВНО те позиции, которые
        `okpd2_relation` считает предками и потомками. Иначе отношение верное, а добор слепой."""
        corpus = _corpus_codes()
        names = list(okpd2_ref._okpd2_names())
        sample = random.Random(141).sample(names, 3000) + [
            "20.14.11.112", "20.14.11.122", "27.12.31.000", "29.10.21", "27.3", "20.60.1"]
        bad = []
        for q in sample:
            anc, desc = set(okpd2_ref.okpd2_ancestor_forms(q)), set(okpd2_ref.okpd2_descendant_forms(q))
            for rc in corpus:
                if rc[:2] != q[:2]:
                    continue
                r = okpd2_ref.okpd2_relation(rc, q)
                if (rc in anc) != (r is not None and r >= 0):
                    bad.append(("предок", rc, q))
                if bool(desc & set(_seg_prefixes(rc))) != (r is not None and r <= 0):
                    bad.append(("потомок", rc, q))
        self.assertEqual(bad[:10], [])

    def test_okpd2_name_fallback_goes_to_nearest_ancestor(self):
        names = okpd2_ref._okpd2_names()
        self.assertEqual(okpd2_ref.okpd2_name("20.14.11.118"), names["20.14.11.110"])
        self.assertIsNone(okpd2_ref.okpd2_name("00.00.00"))


def _hit(anchor, codes, score, match=True, name=None):
    return Hit(score=score, section_roman="XXI", section_title="т", product_name=name or anchor,
               okpd2_codes=codes, min_threshold=None, requirement_blocks=[], source_anchor=anchor,
               okpd2_match=match)


class TestTargetChoice(unittest.TestCase):
    def test_nearest_ancestor_beats_higher_score(self):
        hits = [_hit("катализатор", ["20"], 0.9), _hit("подкатегория", ["20.14.11.110"], 0.2)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "20.14.11.112")],
                         ["подкатегория"])

    def test_exact_beats_nearer_ancestor(self):
        hits = [_hit("категория", ["26.11.22.210"], 0.9), _hit("своя", ["26.11.22.216"], 0.1)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "26.11.22.216")],
                         ["своя"])

    def test_padded_vid_is_exact(self):
        hits = [_hit("реле", ["27.12"], 0.9), _hit("панели", ["27.12.31", "27.12.32"], 0.1)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "27.12.31.000")],
                         ["панели"])

    def test_only_descendants_keep_rank_order(self):
        """Код шире позиций («28.13») — выбор прежний: первая совпавшая по рангу."""
        hits = [_hit("насос А", ["28.13.14.110"], 0.9), _hit("насос Б", ["28.13.1"], 0.1)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "28.13")], ["насос А"])

    def test_broad_code_with_descendants_ignores_class_ancestor(self):
        """Ревью PR #177: на «20.14» ближайшим предком был класс «20», и «Катализаторы» вытесняли
        настоящие 20.14.x. Есть потомки — предок не поднимается, выбор по рангу."""
        hits = [_hit("углеводороды", ["20.14.11.110"], 0.9), _hit("катализатор", ["20"], 0.1)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "20.14")], ["углеводороды"])
        self.assertEqual(retriever.code_tiers([h.okpd2_codes for h in hits], "20.14"), [2, 2])

    def test_covering_ancestor_gives_all_its_records(self):
        """Ревью PR #177: расколотая ячейка 26.11.22.210 — две записи; подкод получает обе, как
        точный код. Мелкий предок («из 20 Катализаторы») — по-прежнему одну."""
        split = [_hit("210a", ["26.11.22.210"], 0.9), _hit("210b", ["26.11.22.210"], 0.5)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(split, "26.11.22.219")],
                         ["210a", "210b"])
        cats = [_hit("кат1", ["20"], 0.9), _hit("кат2", ["20"], 0.5)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(cats, "20.59.59.190")], ["кат1"])


def _point(anchor, codes, score):
    return SimpleNamespace(id=anchor, score=score, payload={
        "section_roman": "XXI", "section_title": "т", "product_name": anchor,
        "okpd2_codes": codes, "source_anchor": anchor, "requirement_blocks": []})


class TestSearchFetchesNewRelations(unittest.TestCase):
    """`search` добирает новые связи ОТДЕЛЬНЫМ запросом и ставит ближайшего предка первым."""

    def _run(self, code):
        calls = []
        catalysts = [_point(f"катализатор {i}", ["20"], 0.9 - i / 100) for i in range(14)]

        def fake_hybrid(query, limit, qfilter=None, collection=None, qvec=None):
            forms = []
            for cond in (getattr(qfilter, "should", None) or []):
                forms += list(cond.match.any)
            calls.append(forms)
            if qfilter is None:
                return catalysts
            if "20.14.11.110" in forms:
                return [_point("подкатегория", ["20.14.11.110"], 0.05)]
            return catalysts[:limit]

        with mock.patch.object(retriever, "_hybrid", side_effect=fake_hybrid), \
                mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8):
            hits = retriever.search("Н-бутан очищенный", okpd2=code, limit=8)
        return hits, calls

    def test_subcode_pulls_and_ranks_its_category_first(self):
        hits, calls = self._run("20.14.11.112")
        self.assertEqual(hits[0].source_anchor, "подкатегория")
        self.assertEqual(hits[0].code_tier, 1)
        self.assertTrue(all(h.okpd2_match for h in hits))
        # прежний добор не изменился: в нём нет новых форм
        self.assertNotIn("20.14.11.110", calls[1])
        self.assertIn("20.14.11.110", calls[2])

    def test_class_code_makes_no_extra_call(self):
        _hits, calls = self._run("20")
        self.assertEqual(len(calls), 2)        # пул + прежний добор, нового запроса нет

    def test_exact_position_found_makes_no_extra_call(self):
        """Ревью PR #177: запрос новых связей (~43 мс) не нужен, когда точная позиция уже найдена."""
        hits, calls = self._run("20.14.11.110")
        self.assertEqual(hits[0].source_anchor, "подкатегория")
        self.assertEqual(len(calls), 2)


class TestGroupNoteInContext(unittest.TestCase):
    def _ctx(self, code, dialog=None, codes=("20.14.11.110",)):
        h = _hit("Приложение, Раздел XXI, позиция 142", list(codes), 0.5,
                 name="Углеводороды ациклические насыщенные")
        return pipeline.format_context([h], "вопрос", code, dialog_codes=dialog)

    def test_subcode_in_question_gets_note(self):
        ctx = self._ctx("20.14.11.112")
        self.assertIn("20.14.11.112 ВХОДИТ В ГРУППИРОВКУ ОКПД2 20.14.11.110", ctx)

    def test_dialog_code_gets_note(self):
        ctx = self._ctx("20.14.11.110", dialog=["20.14.11.112"])
        self.assertIn("названный ранее в диалоге, 20.14.11.112 ВХОДИТ В ГРУППИРОВКУ", ctx)

    def test_no_note_for_shallow_ancestor(self):
        """Ревью PR #177: «из 20 Катализаторы» не группировка н-бутана — строки быть не должно."""
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("20.14.11.112", codes=("20",)))

    def test_dialog_code_does_not_attach_to_a_new_product(self):
        """Ревью PR #177: «шкаф 27.12.31.000», затем «требования у реле 27.12» — реле не получает
        строку «распространяется на шкаф» (27.12 — группа, в приложении это строки «из»)."""
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("27.12", dialog=["27.12.31.000"], codes=("27.12",)))

    def test_dialog_code_only_when_question_gave_none(self):
        ctx = self._ctx("20.14.11.112", dialog=["20.14.11.111"])
        self.assertIn("из вопроса 20.14.11.112", ctx)
        self.assertNotIn("20.14.11.111", ctx)

    def test_no_note_for_exact_or_unrelated(self):
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("20.14.11.110"))
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("20.14.11.110", dialog=["27.12.31.000"]))
        # у позиции И точный код, И код-предок: совпадение точное, строка о группировке лжёт бы
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("27.12.31", codes=("27.12", "27.12.31")))


class TestDialogCodeReachesPrompt(unittest.TestCase):
    """⚠ ПУТЬ, А НЕ КОМПОНЕНТ: строку о группировке видит модель, только если `_plan_answer`
    передал код продукции из истории в `format_context`. Тест `format_context` напрямую этого
    не проверяет — на бою «нельзя» прозвучало именно на втором ходе диалога."""

    def test_second_turn_with_group_code_mentions_product_code(self):
        hit = _hit("Приложение, Раздел XXI, позиция 142", ["20.14.11.110"], 0.5,
                   name="Углеводороды ациклические насыщенные")
        history = [{"role": "user", "content": "Н-бутан очищенный 20.14.11.112"},
                   {"role": "assistant", "content": "Точной позиции не нашёл."}]
        with mock.patch.object(pipeline, "embed_query", lambda *a, **k: [0.0] * 8), \
                mock.patch.object(pipeline, "search", lambda *a, **k: [hit]), \
                mock.patch.object(pipeline, "search_cases", lambda *a, **k: []), \
                mock.patch.object(pipeline, "dense_top1", lambda *a, **k: 0.95), \
                mock.patch.object(pipeline.settings, "RERANK_ENABLED", False):
            plan = pipeline._plan_answer("можно ли применять к нему требования позиции 20.14.11.110?",
                                         okpd2="20.14.11.110", history=history)
        self.assertIn("названный ранее в диалоге, 20.14.11.112 ВХОДИТ В ГРУППИРОВКУ",
                      plan.messages[-1]["content"])


class TestExpertSetIsGrounded(unittest.TestCase):
    """EV23: ожидания набора экспертов сверены с корпусом и первоисточником, а не с ответом."""

    def test_anchors_exist_and_numbers_are_in_source(self):
        cases = json.loads((ROOT / "scripts" / "eval_golden_experts.json").read_text(encoding="utf-8"))["cases"]
        anchors = set()
        for f in (ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json"):
            anchors |= {r.get("source_anchor") for r in json.loads(f.read_text(encoding="utf-8"))}
        source = (ROOT / "knowledge_base" / "pp719" / "pp719_full.txt").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(cases), 9)
        for c in cases:
            for a in (c.get("expect_target_any") or []) + (c.get("expect_window_all") or []):
                self.assertIn(a, anchors, c["id"])
            for needle in c.get("expect_prompt") or []:
                # число (порог) обязано стоять в первоисточнике как «не менее N»; код — в справочнике
                if "." in needle and okpd2_ref.okpd2_key(needle):
                    self.assertIn(needle, okpd2_ref._okpd2_names(), c["id"])
                else:
                    self.assertTrue(f"не менее {needle}" in source, f"{c['id']}: «не менее {needle}»")

    def test_d14_targets_are_ancestors_of_question_code(self):
        """Ожидаемая позиция D14 действительно предок кода вопроса — по корпусу, не по набору."""
        recs = {}
        for f in (ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json"):
            for r in json.loads(f.read_text(encoding="utf-8")):
                recs[r["source_anchor"]] = r
        cases = json.loads((ROOT / "scripts" / "eval_golden_experts.json").read_text(encoding="utf-8"))["cases"]
        for c in cases:
            if c["task"] != "#141" or c["id"].startswith("control"):
                continue
            q = okpd2_ref.extract_codes(c["query"])[0]
            for a in c["expect_target_any"]:
                rel = code_relation(recs[a]["okpd2_codes"], q)
                self.assertIsNotNone(rel, c["id"])
                self.assertGreaterEqual(rel, 0, c["id"])


if __name__ == "__main__":
    unittest.main()
