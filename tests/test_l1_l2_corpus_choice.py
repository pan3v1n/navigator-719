"""`L1` #146 (явный выбор корпуса) и `L2` #147 (подпись «по чему собран ответ»). Офлайн.

Критерии ТЗ (`docs/DUAL_CORPUS_TZ.md` §4):
* ручной выбор корпуса норм даёт процедурный ответ на трёх вопросах `K16`, которые гейт не
  пропускает, — 3/3;
* в режиме «Авто» маршрут совпадает с гейтом `is_procedural` на ВСЁМ своде свипа маршрута;
* подпись корпуса есть в ответе, в переоткрытой беседе и в шапке/репликах всех трёх выгрузок.
"""

from __future__ import annotations

import collections
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api import admin as admin_mod  # noqa: E402
from app.api import chat as chat_mod  # noqa: E402
from app.api import web as web_mod  # noqa: E402
from app.api.ratelimit import SlidingWindow  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.rag import corpora, meta, procedural, topics, translate  # noqa: E402
from app.rag import pipeline as pl  # noqa: E402
from app.rag.pipeline import Answer  # noqa: E402
from app.rag.retriever import Hit  # noqa: E402

GAPS = json.loads((ROOT / "scripts" / "eval_route_gaps.json").read_text(encoding="utf-8"))["cases"]


class _Stubbed(unittest.TestCase):
    """Пайплайн без сети: процедурная ветка записывает, с чем её позвали; поиск — заглушки."""

    def setUp(self):
        self._orig = {n: getattr(pl, n) for n in
                      ("_answer_procedural", "embed_query", "search", "search_cases", "search_rules",
                       "dense_top1")}
        self._flags = (pl.settings.RERANK_ENABLED, pl.settings.PROCEDURAL_DEFLECT_ENABLED)
        pl.settings.RERANK_ENABLED = False
        pl.settings.PROCEDURAL_DEFLECT_ENABLED = True
        self.proc_calls = []

        def proc(query, search_query, history=None, brief=False, only_docs=(), used=corpora.USED_RULES):
            self.proc_calls.append({"query": query, "only_docs": only_docs, "used": used})
            return Answer(text="процедурный ответ", hits=[], corpus=used)

        pl._answer_procedural = proc
        pl.embed_query = lambda text: [0.1] * 4
        pl.search = lambda *a, **k: []
        pl.search_cases = lambda *a, **k: []
        pl.dense_top1 = lambda *a, **k: 0.9
        self.rules_calls = []

        def rules(*a, **k):
            self.rules_calls.append(k)
            return [{"doc_type": "tpp_order_52", "point": "4.2.1", "section_roman": "4",
                     "section_title": "Документы", "text": "Перечень документов для акта экспертизы.",
                     "source_anchor": "Приказ ТПП РФ №52, п. 4.2.1"}]
        pl.search_rules = rules

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(pl, n, f)
        pl.settings.RERANK_ENABLED, pl.settings.PROCEDURAL_DEFLECT_ENABLED = self._flags


class TestRouteByChosenCorpusL1(_Stubbed):
    PROCEDURAL_Q = "как внести продукцию в реестр российской промышленной продукции"
    PRODUCT_Q = "требования к бульдозерам гусеничным"

    def test_preconditions(self):
        self.assertTrue(procedural.is_procedural(self.PROCEDURAL_Q))
        self.assertFalse(procedural.is_procedural(self.PRODUCT_Q))

    def test_auto_keeps_the_gate(self):
        pl._plan_answer(self.PROCEDURAL_Q)
        self.assertEqual(len(self.proc_calls), 1)
        self.assertEqual(self.proc_calls[0]["only_docs"], ())
        planned = pl._plan_answer(self.PRODUCT_Q)
        self.assertEqual(len(self.proc_calls), 1, "товарный вопрос ушёл на процедурную ветку")
        self.assertEqual(planned.corpus, corpora.USED_APPENDIX)

    def test_appendix_skips_the_gate(self):
        planned = pl._plan_answer(self.PROCEDURAL_Q, corpus=corpora.APPENDIX)
        self.assertEqual(self.proc_calls, [], "при выборе приложения гейт не спрашивается")
        self.assertEqual(planned.corpus, corpora.USED_APPENDIX)

    def test_rules_routes_even_a_product_question(self):
        ans = pl._plan_answer(self.PRODUCT_Q, corpus=corpora.RULES)
        self.assertEqual(self.proc_calls[-1]["only_docs"], ())
        self.assertEqual(ans.corpus, corpora.USED_RULES)

    def test_metodrek_filters_to_its_document(self):
        ans = pl._plan_answer(self.PRODUCT_Q, corpus=corpora.METODREK)
        self.assertEqual(self.proc_calls[-1]["only_docs"], ("metodrek_tpp",))
        self.assertEqual(ans.corpus, corpora.USED_METODREK)

    def test_manual_choice_ignores_the_deflect_flag(self):
        """Флаг выключает АВТОМАТ, а не явный выбор пользователя."""
        pl.settings.PROCEDURAL_DEFLECT_ENABLED = False
        pl._plan_answer(self.PRODUCT_Q, corpus=corpora.RULES)
        self.assertEqual(len(self.proc_calls), 1)

    def test_greeting_has_no_corpus_in_any_mode(self):
        for mode in corpora.MODES:
            ans = pl._plan_answer("привет", corpus=mode)
            self.assertIsInstance(ans, Answer, mode)
            self.assertEqual(ans.corpus, "", mode)
        self.assertEqual(self.proc_calls, [])

    def test_translate_is_signed_as_classifier(self):
        ans = pl._plan_answer("переведи ТН ВЭД 8471 30 000 0 в ОКПД2")
        self.assertEqual(ans.corpus, corpora.USED_CLASSIFIER)


class TestAcceptanceK16Gaps(_Stubbed):
    """Критерий `L1`: три вопроса `K16`, не проходящие гейт, при ручном выборе — процедурные 3/3."""

    def test_manual_rules_choice_answers_all_three(self):
        self.assertEqual(len(GAPS), 3)
        for case in GAPS:
            with self.subTest(q=case["query"]):
                before = len(self.proc_calls)
                pl._plan_answer(case["query"], corpus=corpora.RULES)
                self.assertEqual(len(self.proc_calls), before + 1)

    def test_auto_follows_the_gate_on_them(self):
        """Автомат не меняется: где гейт пропускает — процедурный, где нет — товарный, как до L1."""
        for case in GAPS:
            with self.subTest(q=case["query"]):
                before = len(self.proc_calls)
                pl._plan_answer(case["query"])
                went = len(self.proc_calls) > before
                self.assertEqual(went, procedural.is_procedural(case["query"], has_code=False))


class TestAutoEqualsGateOnTheWholeSweep(_Stubbed):
    """Критерий `L1`: в режиме «Авто» 0 расхождений с гейтом на всём своде `eval_routing.py`."""

    def test_zero_divergence(self):
        import eval_routing

        rows = eval_routing.collect()
        self.assertGreater(len(rows), 200, "свип пуст — сверка ничего не проверяет")
        diverged = []
        for r in rows:
            qq = r["query"]
            expect = (not meta.is_meta(qq) and not translate.is_translate(qq) and r["procedural"])
            before = len(self.proc_calls)
            pl._plan_answer(qq)
            if (len(self.proc_calls) > before) != expect:
                diverged.append(qq)
        self.assertEqual(diverged, [], f"«Авто» разошёлся с гейтом на {len(diverged)} вопросах")


class TestMixedQuestionInAppendixMode(_Stubbed):
    """Смешанный вопрос: в «Авто» пункты Приказа №52 добираются, в «Приложении» — нет (подпись L2).

    Вопрос — кейс 48 приёмочного набора: смешанный товарный+документный, обязан оставаться товарным
    (`K12`, `eval_routing.EXPECTED_DOCUMENTS_ROUTE`)."""

    Q = next(c["query"] for c in json.loads(
        (ROOT / "scripts" / "eval_golden.json").read_text(encoding="utf-8"))["cases"] if c["id"] == 48)

    def test_precondition_stays_on_product_route(self):
        self.assertFalse(procedural.is_procedural(self.Q, has_code=False))

    def _hit(self):
        return Hit(score=0.9, section_roman="XIX", section_title="Насосы",
                   product_name="Насосы гидравлические", okpd2_codes=["28.13.14"],
                   min_threshold="не менее 300 баллов", requirement_blocks=[],
                   source_anchor="Раздел XIX", okpd2_match=True)

    def test_precondition_is_a_documents_question(self):
        self.assertEqual(topics.classify(self.Q), topics.DOCUMENTS)

    def test_auto_adds_order52_points(self):
        pl.search = lambda *a, **k: [self._hit()]
        planned = pl._plan_answer(self.Q)
        self.assertIsInstance(planned, pl._Plan)
        self.assertTrue(any(c.get("only_docs") == ("tpp_order_52",) for c in self.rules_calls))
        self.assertEqual(planned.corpus, corpora.USED_APPENDIX_ORDER52)

    def test_appendix_does_not(self):
        pl.search = lambda *a, **k: [self._hit()]
        planned = pl._plan_answer(self.Q, corpus=corpora.APPENDIX)
        self.assertIsInstance(planned, pl._Plan)
        self.assertEqual(self.rules_calls, [], "в режиме «Приложение» корпус норм не трогается")
        self.assertEqual(planned.corpus, corpora.USED_APPENDIX)


class TestOrder52PointsNumberedAfterPositionsUI1(_Stubbed):
    """`UI-1` #165: пункты Приказа №52 в товарном ответе нумеруются ПРОДОЛЖЕНИЕМ позиций и стоят в
    источниках ответа на тех же местах — кнопка [N] открывает ровно то, на что сослалась модель.

    Прежде блок документов начинался с [1], а источники были «позиции ИЛИ пункты»: ссылка на пункт
    Приказа как [1] открывала в панели «Источник» позицию приложения №1."""

    Q = TestMixedQuestionInAppendixMode.Q
    _hit = TestMixedQuestionInAppendixMode._hit

    def _two_hits(self):
        h1 = self._hit()
        h2 = Hit(score=0.8, section_roman="XIX", section_title="Насосы", product_name="Насосы прочие",
                 okpd2_codes=["28.13.19"], min_threshold=None, requirement_blocks=[],
                 source_anchor="Раздел XIX, позиция 2")
        return [h1, h2]

    def test_context_numbers_order_points_after_positions(self):
        pl.search = lambda *a, **k: self._two_hits()
        planned = pl._plan_answer(self.Q)
        user = planned.messages[-1]["content"]
        self.assertIn("[3] Приказ ТПП РФ №52, п. 4.2.1", user, "пункт Приказа — следующий номер после позиций")
        self.assertNotIn("[1] Приказ ТПП РФ №52", user)
        self.assertEqual([p["point"] for p in planned.doc_points], ["4.2.1"])

    def test_answer_sources_put_points_on_the_same_places(self):
        from app.api.chat import _answer_sources
        pl.search = lambda *a, **k: self._two_hits()
        planned = pl._plan_answer(self.Q)
        ans = Answer(text="", hits=planned.hits, rule_sources=planned.doc_points)
        src = _answer_sources(ans)
        self.assertEqual([x.kind for x in src], ["product", "product", "rule"],
                         "[3] обязан открывать пункт Приказа, а не позицию")
        self.assertIn("4.2.1", src[2].product_name)

    def test_procedural_answer_sources_unchanged(self):
        """Контроль: у процедурного ответа позиций нет — источники пункты с [1], как прежде."""
        from app.api.chat import _answer_sources
        rules = [{"doc_type": "rules", "source_anchor": "Правила, п. 7", "text": "текст"}]
        src = _answer_sources(Answer(text="", hits=[], rule_sources=rules))
        self.assertEqual([s.product_name for s in src], ["Правила, п. 7"])

    def test_answer_carries_doc_points_as_rule_sources(self):
        """И синхронный, и потоковый ответ отдают пункты в `rule_sources` (иначе источники их не знают).
        Поведением: план с пунктами, LLM подменена."""
        from types import SimpleNamespace
        from unittest import mock
        pts = [{"doc_type": "tpp_order_52", "source_anchor": "Приказ ТПП РФ №52, п. 4.2.1", "text": "т"}]
        plan = pl._Plan(messages=[{"role": "user", "content": "в"}], grounding="", hits=[self._hit()],
                        cases=[], low_relevance=False, doc_points=pts)
        msg = SimpleNamespace(content="Ответ [2].")
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)

        def create(**kw):
            if kw.get("stream"):
                delta = SimpleNamespace(content="Ответ [2].")
                return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
                                             usage=None),
                             SimpleNamespace(choices=[], usage=usage)])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(pl, "_plan_answer", return_value=plan),                 mock.patch.object(pl, "_client", return_value=client):
            sync = pl.answer("в")
            done = [p for k, p in pl.answer_stream("в") if k == "done"][0]
        for ans in (sync, done):
            self.assertEqual(ans.rule_sources, pts)
            self.assertEqual(len(ans.hits), 1)


class TestOnlyDocsReachesTheSearch(unittest.TestCase):
    """Фильтр документов доходит до поиска: настоящие `_answer_procedural` и `plan_procedural`.

    ⚠ Тесты маршрута выше подменяют процедурную ветку целиком — передачу `only_docs` ниже по
    стеку они не видят по построению. Здесь подменён только поиск."""

    def setUp(self):
        self._orig = (pl.search_rules, pl.plan_procedural)
        self.kw = []

    def tearDown(self):
        pl.search_rules, pl.plan_procedural = self._orig

    def test_plan_procedural_passes_only_docs(self):
        def rules(*a, **k):
            self.kw.append(k)
            return []
        pl.search_rules = rules
        self.assertIsNone(pl.plan_procedural("вопрос", "вопрос", only_docs=("metodrek_tpp",)))
        self.assertEqual(self.kw[-1].get("only_docs"), ("metodrek_tpp",))

    def test_answer_procedural_passes_only_docs(self):
        seen = {}

        def plan(query, search_query, only_docs=()):
            seen["only_docs"] = only_docs
            return None  # корпус «пуст» → честный дефер, до модели не доходим
        pl.plan_procedural = plan
        ans = pl._answer_procedural("вопрос", "вопрос", only_docs=("metodrek_tpp",),
                                    used=corpora.USED_METODREK)
        self.assertEqual(seen["only_docs"], ("metodrek_tpp",))
        self.assertEqual(ans.corpus, "", "дефер — не ответ по корпусу, подписывать его нечем")


class TestReviewFixes(_Stubbed):
    """Находки ревью диффа L1/L2 (27.10.2026) — каждая закреплена тестом."""

    ST1_Q = next(c["query"] for c in json.loads(
        (ROOT / "scripts" / "eval_st1_route.json").read_text(encoding="utf-8"))["cases"] if c["id"] == 16)

    def test_translate_only_in_auto(self):
        """LOW-3: при ручном выборе перевод кодов не подменяет выбор пользователя."""
        q_tr = "переведи ТН ВЭД 8471 30 000 0 в ОКПД2"
        self.assertTrue(translate.is_translate(q_tr))
        self.assertEqual(pl._plan_answer(q_tr).corpus, corpora.USED_CLASSIFIER)
        pl._plan_answer(q_tr, corpus=corpora.RULES)
        self.assertEqual(len(self.proc_calls), 1, "в «Правилах» вопрос ушёл не в выбранный корпус")
        self.assertNotEqual(pl._plan_answer(q_tr, corpus=corpora.APPENDIX).corpus, corpora.USED_CLASSIFIER)

    def _plan(self, only_docs):
        return pl.plan_procedural(self.ST1_Q, self.ST1_Q, only_docs=only_docs)  # поиск — заглушка

    def test_narrowed_corpus_gets_no_foreign_material(self):
        """LOW-4: «Методрекомендации» — без Перечня СТ-1 и без фрагмента темы, называющего чужие
        документы. Положительный контроль: в полном корпусе норм то и другое есть."""
        self.assertEqual(topics.classify(self.ST1_Q), topics.ST1_ORIGIN)
        fragment = topics.fragment(topics.ST1_ORIGIN)
        self.assertTrue(fragment)
        full = self._plan(())
        self.assertNotEqual(full.grounding, full.ctx, "контроль: в полном корпусе блок СТ-1 есть")
        self.assertIn(fragment, full.user)
        narrow = self._plan(("metodrek_tpp",))
        self.assertEqual(narrow.grounding, narrow.ctx, "в «Методрекомендациях» блок СТ-1 не нужен")
        self.assertNotIn(fragment, narrow.user)


class TestRepeatIsTextAndMode(unittest.TestCase):
    """MED-1, MED-2: повтор вопроса — тот же текст И тот же режим источника."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session() as db:
            self.uid = q.create_user(db, "expert1", "h", role="expert").id

    def _since(self):
        from datetime import datetime, timedelta
        return datetime.utcnow() - timedelta(minutes=5)

    def test_chat_repeat_needs_the_same_mode(self):
        with self.Session() as db:
            q.log_message(db, user_id=self.uid, session_id="s", role="user", content="Q")
            q.log_message(db, user_id=self.uid, session_id="s", role="assistant", content="A",
                          corpus=corpora.stored(corpora.USED_RULES, corpora.RULES))
            self.assertTrue(q.is_answered_repeat(db, self.uid, "s", "Q", self._since(), mode=corpora.RULES))
            self.assertFalse(q.is_answered_repeat(db, self.uid, "s", "Q", self._since(), mode=corpora.AUTO),
                             "тот же текст в другом источнике — новый платный ответ, не повтор")

    def test_unmarked_answer_counts_as_auto(self):
        with self.Session() as db:
            q.log_message(db, user_id=self.uid, session_id="s", role="user", content="Q")
            q.log_message(db, user_id=self.uid, session_id="s", role="assistant", content="A")
            self.assertTrue(q.is_answered_repeat(db, self.uid, "s", "Q", self._since()))
            self.assertFalse(q.is_answered_repeat(db, self.uid, "s", "Q", self._since(), mode=corpora.METODREK))

    def test_guest_switching_source_gets_a_new_answer(self):
        from app.api import guest as guest_mod

        orig = guest_mod.get_session
        guest_mod.get_session = lambda: self.Session()
        try:
            with self.Session() as db:
                q.log_guest_message(db, guest_id="g", session_id="s", role="user", content="Q")
                q.log_guest_message(db, guest_id="g", session_id="s", role="assistant", content="A",
                                    corpus=corpora.stored(corpora.USED_APPENDIX, corpora.AUTO))
            same = guest_mod._last_same("g", chat_mod.ChatRequest(message="Q", session_id="s"))
            self.assertIsNotNone(same[1], "тот же режим — повтор, отдаём сохранённое")
            switched = guest_mod._last_same(
                "g", chat_mod.ChatRequest(message="Q", session_id="s", corpus=corpora.RULES))
            self.assertEqual(switched, (None, None), "другой источник — новый вопрос, движок обязан звать")
            payload = guest_mod._stored_payload(same[1], "g", "s")
            self.assertEqual(payload["corpus_label"],
                             corpora.answered_by(corpora.USED_APPENDIX, corpora.AUTO),
                             "LOW-5: сохранённый ответ гостю — тоже с подписью")
        finally:
            guest_mod.get_session = orig


class TestCorporaModule(unittest.TestCase):
    def test_modes_and_titles_agree(self):
        self.assertEqual(set(corpora.MODES), set(corpora.MODE_TITLES))
        self.assertTrue(set(corpora.ONLY_DOCS) <= set(corpora.MODES))
        self.assertNotIn(corpora.AUTO, corpora.ONLY_DOCS, "«Авто» не должен фильтровать корпус")

    def test_only_docs_names_real_documents(self):
        from app.core.manifest import load_manifest

        known = {d["doc_type"] for d in load_manifest()["documents"]}
        for docs in corpora.ONLY_DOCS.values():
            self.assertTrue(set(docs) <= known, docs)

    def test_labels(self):
        auto = corpora.answered_by(corpora.USED_APPENDIX, corpora.AUTO)
        self.assertIn("приложению к ПП №719", auto)
        self.assertIn("автоматически", auto)
        self.assertIn("вручную", corpora.answered_by(corpora.USED_RULES, corpora.RULES))
        for used in corpora.USED_LABELS:
            self.assertTrue(corpora.answered_by(used, corpora.AUTO), used)
        self.assertEqual(corpora.answered_by("", corpora.AUTO), "")
        self.assertEqual(corpora.answered_by("неизвестно", corpora.AUTO), "")

    def test_stored_roundtrip(self):
        s = corpora.stored(corpora.USED_METODREK, corpora.METODREK)
        self.assertEqual(corpora.label_from_stored(s),
                         corpora.answered_by(corpora.USED_METODREK, corpora.METODREK))
        self.assertIsNone(corpora.stored("", corpora.AUTO))
        self.assertEqual(corpora.label_from_stored(None), "")


class _FakeAns:
    hits: list = []
    rule_sources: list = []
    low_relevance = False
    unverified_numbers: list = []
    phantom_documents: list = []
    prompt_tokens = completion_tokens = 0
    input_hint = ""
    metered = True

    def __init__(self, corpus: str, text: str = "Ответ движка"):
        self.corpus, self.text = corpus, text


class TestCorpusLabelThroughAPIAndExportsL2(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._orig = (chat_mod.get_session, chat_mod.answer, chat_mod._chat_limit)
        chat_mod.get_session = lambda: self.Session()
        chat_mod._chat_limit = SlidingWindow(limit=1000, window=60.0)
        with self.Session() as db:
            uid = q.create_user(db, "expert1", "h", role="expert").id
            self.user = q.get_user(db, uid)
            db.expunge(self.user)

    def tearDown(self):
        chat_mod.get_session, chat_mod.answer, chat_mod._chat_limit = self._orig

    def _ask(self, corpus_mode: str, used: str, sid: str = "s1"):
        seen = {}

        def fake(msg, okpd2=None, history=None, corpus="auto"):
            seen["corpus"] = corpus
            return _FakeAns(used)
        chat_mod.answer = fake
        resp = chat_mod.chat(chat_mod.ChatRequest(message="вопрос", session_id=sid, corpus=corpus_mode),
                             user=self.user)
        return resp, seen

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValidationError):
            chat_mod.ChatRequest(message="вопрос", corpus="интернет")

    def test_default_mode_is_auto(self):
        self.assertEqual(chat_mod.ChatRequest(message="вопрос").corpus, corpora.AUTO)

    def test_mode_reaches_the_engine_and_label_comes_back(self):
        resp, seen = self._ask(corpora.RULES, corpora.USED_RULES)
        self.assertEqual(seen["corpus"], corpora.RULES)
        self.assertIn("Правилам реестра", resp.corpus_label)
        self.assertIn("вручную", resp.corpus_label)

    def test_label_survives_reopen_and_every_export(self):
        self._ask(corpora.AUTO, corpora.USED_APPENDIX)
        label = corpora.answered_by(corpora.USED_APPENDIX, corpora.AUTO)
        conv = chat_mod.get_conversation("s1", user=self.user)
        asst = [m for m in conv["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(asst["corpus_label"], label)
        for fmt in ("md", "txt", "json"):
            with self.subTest(fmt=fmt):
                body = chat_mod.export_conversation("s1", fmt=fmt, user=self.user).body.decode("utf-8")
                self.assertIn(chat_mod._CORPUS_NOTE if fmt != "json" else "corpus_note", body)
                self.assertIn(label, body)
                body_all = chat_mod.export_conversations(fmt=fmt, user=self.user).body.decode("utf-8")
                self.assertIn(label, body_all)

    def test_answer_without_corpus_has_no_label(self):
        resp, _ = self._ask(corpora.AUTO, "")
        self.assertEqual(resp.corpus_label, "")
        md = chat_mod.export_conversation("s1", fmt="md", user=self.user).body.decode("utf-8")
        self.assertNotIn("Ответ собран по", md)

    def test_admin_export_carries_the_label(self):
        label = corpora.answered_by(corpora.USED_RULES, corpora.RULES)
        row = {"ts": "27.10.2026", "user": "u", "region": "Курская", "role": "expert", "rating": 5,
               "question": "в", "answer": "о", "unverified": None, "low_relevance": False,
               "comment": "", "correction": "", "corpus": label}
        st = collections.defaultdict(int, {"answer_rows": [row], "generated_at": "27.10.2026"})
        orig = admin_mod._stats_view
        admin_mod._stats_view = lambda *a, **k: {"stats": st}
        try:
            csv_body = admin_mod.admin_export(fmt="csv", admin=self.user).body.decode("utf-8")
            json_body = admin_mod.admin_export(fmt="json", admin=self.user).body.decode("utf-8")
        finally:
            admin_mod._stats_view = orig
        self.assertIn("Источник ответа", csv_body)
        self.assertIn(label, csv_body)
        self.assertIn("corpus_note", json_body)
        self.assertIn(label, json_body)


class TestInterfaceWiring(unittest.TestCase):
    """Фронт: переключатель берёт режимы из `corpora`, оба запроса шлют выбор, подпись рисуется."""

    def test_template_modes_come_from_corpora(self):
        self.assertEqual(web_mod.CORPUS_MODES, [(m, corpora.MODE_TITLES[m]) for m in corpora.MODES])
        html = (ROOT / "app" / "web" / "templates" / "chat.html").read_text(encoding="utf-8")
        self.assertIn('id="corpus-select"', html)
        self.assertIn("{% for value, title in corpus_modes %}", html)

    def test_both_requests_send_the_choice_and_label_is_drawn(self):
        js = (ROOT / "app" / "web" / "static" / "chat.js").read_text(encoding="utf-8")
        self.assertEqual(js.count("session_id: sessionId, corpus })"), 2, "стрим и фолбэк обязаны слать выбор")
        self.assertEqual(js.count("addCorpusLine("), 4, "определение + стрим + фолбэк + переоткрытие")

    def test_choice_is_fixed_at_submit(self):
        """LOW-6: выбор читается ОДИН раз при отправке и тот же уходит в фолбэк."""
        js = (ROOT / "app" / "web" / "static" / "chat.js").read_text(encoding="utf-8")
        self.assertEqual(js.count("currentCorpus()"), 2, "определение + одно чтение при отправке")
        self.assertIn("askStream(text, pending, bubble, isNew, corpus)", js)
        self.assertIn("askFallback(text, pending, bubble, isNew, corpus)", js)


if __name__ == "__main__":
    unittest.main()
