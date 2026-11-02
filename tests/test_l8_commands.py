"""`L8-1` #153: готовые команды. ТЗ — `docs/L8_COMMANDS_TZ.md`, приёмка — §11. Офлайн.

Главный критерий: 0 уходов на чужую ветку по ВСЕМ командам. Текст запроса собирает сервер, поэтому
тест проверяет ровно то, что уходит в движок, а не формулировку, которую человек мог бы набрать.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from test_auth import _Ans, _GuestBase  # noqa: E402
from test_l1_l2_corpus_choice import _Stubbed  # noqa: E402

import eval_routing  # noqa: E402
from app.api import admin_stats, guest  # noqa: E402
from app.api import auth as auth_mod  # noqa: E402
from app.api import chat as chat_mod  # noqa: E402
from app.api.ratelimit import SlidingWindow  # noqa: E402
from app.core import commands, sensitive  # noqa: E402
from app.core.commands import COMMANDS, SAMPLES, SOON  # noqa: E402
from app.db import engine as engine_mod  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base, Feedback, GuestMessage, Message  # noqa: E402
from app.rag import corpora, meta, procedural, topics, translate  # noqa: E402
from app.rag import pipeline as pl  # noqa: E402
from app.rag.okpd2_ref import extract_tnved_position  # noqa: E402
from app.rag.retriever import Hit  # noqa: E402
from app.tools.navigator import extract_okpd2  # noqa: E402

REQ = commands.find("req")
DOCS = commands.find("docs")
BULLDOZER = "28.92.21.110"


def _samples(cmd):
    return SAMPLES[cmd.field.kind] if cmd.field else ("",)


class TestCatalog(unittest.TestCase):
    def test_keys(self):
        keys = [c.key for c in COMMANDS]
        self.assertEqual(len(keys), len(set(keys)))
        for k in keys:
            self.assertRegex(k, r"^[a-z0-9_]+$")
            self.assertLessEqual(len(commands.held(k)), 32, "колонка messages.command — String(32)")

    def test_owner_decision_twelve_active_and_two_soon(self):
        """Решение 1 и 2: 10 команд макета + 2 новые рабочие, №7 и №17 со «Скоро», остальное скрыто."""
        self.assertEqual(len(COMMANDS), 12)
        self.assertEqual([s.title for s in SOON], ["Проверка по документации", "Сравнение версий документов"])
        hidden = ("Сравнение редакций", "Требования в документации закупки", "Перевод на официально-деловой",
                  "Ответ на запрос Минпромторга", "Перевод технической документации",
                  "Расшифровка записей встреч", "Распознавание текста", "Проверка правописания")
        shown = {c.title for c in COMMANDS} | {s.title for s in SOON}
        self.assertFalse(shown & set(hidden))

    def test_corpus_is_an_explicit_mode(self):
        for c in COMMANDS:
            self.assertIn(c.corpus, corpora.MODES, c.key)
            self.assertNotEqual(c.corpus, corpora.AUTO, f"{c.key}: источник задаёт команда, а не автомат")

    def test_template_has_exactly_one_place_for_value(self):
        for c in COMMANDS:
            self.assertEqual(c.template.count(commands.VALUE), 1 if c.field else 0, c.key)
            rest = c.template.replace(commands.VALUE, "")
            self.assertNotRegex(rest, r"[{}\[\]]", f"{c.key}: скобки в шаблоне")

    def test_home_and_sidebar_counts(self):
        self.assertEqual(sum(c.home for c in COMMANDS), 8)
        pinned = [c.title for c in COMMANDS if c.pinned]
        self.assertEqual(pinned, ["Требования к продукции", "Документы для подтверждения производства",
                                  "Экспертиза ТПП", "Проверка участника закупки", "Пункт простым языком"])

    def test_no_cancelled_procedure_in_any_text(self):
        """Решение 2: «Правила выдачи заключения» утратили силу, «заключение ТПП» — фантом `K15`."""
        for c in COMMANDS:
            blob = " ".join((c.title, c.desc, c.ref, c.template)).lower()
            self.assertNotIn("заключени", blob, c.key)
            self.assertNotIn("правила выдачи", blob, c.key)
            self.assertNotIn("п. 2 постановления", blob, c.key)

    def test_macket_texts_are_verbatim(self):
        """Что не менялось решением владельца, совпадает с макетом дословно."""
        macket = {
            "req": ("Требования к продукции", "Какие операции и компоненты обязательны для вашей продукции"),
            "exp": ("Экспертиза ТПП", "Кто проводит экспертизу и что подтверждает акт"),
            "buy": ("Проверка участника закупки", "Как подтверждается российское производство товара"),
            "plain": ("Пункт простым языком", "Пояснение формулировки выбранного пункта"),
            "prep": ("Подготовка к экспертизе", "Список сведений, которые стоит проверить заранее"),
            "parts": ("Проверка комплектующих", "Допустимость использования иностранных комплектующих в изделии"),
            "ops": ("Технологические операции", "Перечень операций, которые должны выполняться в РФ"),
            "registry": ("Реестр промышленной продукции", "Как продукция попадает в реестр и что указывается в записи"),
        }
        for key, (title, desc) in macket.items():
            cmd = commands.find(key)
            self.assertEqual((cmd.title, cmd.desc), (title, desc), key)

    def test_page_view_has_no_second_copy_of_titles(self):
        view = commands.page_view()
        self.assertEqual([c["key"] for c in view["active"]], [c.key for c in COMMANDS])
        self.assertEqual(view["active"][0]["corpus_title"], corpora.MODE_TITLES[corpora.APPENDIX])
        json.dumps(view, ensure_ascii=False)   # страница кладёт каталог в `tojson`


class TestRender(unittest.TestCase):
    def test_value_goes_into_template(self):
        self.assertIn(f"продукции {BULLDOZER}:", REQ.render(BULLDOZER))

    def test_value_is_one_line(self):
        self.assertIn("продукции бульдозеры гусеничные:", REQ.render("  бульдозеры \n  гусеничные "))

    def test_braces_in_value_are_plain_text(self):
        self.assertIn("{0} {value}", REQ.render("{0} {value}"))

    def test_value_error(self):
        self.assertEqual(commands.value_error(REQ, "   "), "Укажите: наименование или код ОКПД2")
        self.assertEqual(commands.value_error(commands.find("st1"), ""), "Укажите: код ТН ВЭД")
        self.assertIn("200", commands.value_error(REQ, "х" * 201))
        self.assertEqual(commands.value_error(REQ, "х" * 200), "")
        self.assertEqual(commands.value_error(DOCS, ""), "")
        self.assertTrue(commands.value_error(DOCS, "лишний текст"), "значение команды без поля не теряем молча")

    def test_title_of_any_stored_key(self):
        self.assertEqual(commands.title_of("req"), "Требования к продукции")
        self.assertEqual(commands.title_of(commands.held("req")), "Требования к продукции")
        self.assertEqual(commands.title_of("снята"), "снята", "команду убрали, но выбирал не человек")
        self.assertEqual(commands.title_of(None), "")

    def test_held_key_is_not_a_run(self):
        self.assertIsNone(commands.find(commands.held("req")))
        self.assertEqual(commands.held(commands.held("req")), commands.held("req"))

    def test_short_title_shows_the_value(self):
        self.assertEqual(commands.short_title(REQ.render("насосы")), "Требования к продукции: насосы")
        self.assertEqual(commands.short_title(commands.find("st1").render("8471")),
                         "Продукции нет в приложении: 8471")
        self.assertEqual(commands.short_title(DOCS.render()), DOCS.title)
        self.assertEqual(commands.short_title("обычный вопрос"), "обычный вопрос")


class TestRouteOfEveryCommand(_Stubbed):
    """Критерий `L8`: команда × примеры значений — ни одного ухода на чужую ветку."""

    def test_topic_is_the_one_in_catalog(self):
        for c in COMMANDS:
            if c.topic_by_value:
                continue
            for v in _samples(c):
                self.assertEqual(topics.classify(c.render(v)), c.expect_topic, f"{c.key}: {v}")

    def test_point_command_takes_topic_from_the_value(self):
        plain = commands.find("plain")
        self.assertEqual(topics.classify(plain.render("сноска <12(1)>")), "classification")
        self.assertEqual(topics.classify(plain.render("подпункт «г» пункта 1")), topics.ST1_ORIGIN)

    def test_branch_follows_the_command(self):
        for c in COMMANDS:
            for v in _samples(c):
                self.proc_calls.clear()
                planned = pl._plan_answer(c.render(v), corpus=c.corpus)
                label = f"{c.key}: {v}"
                if c.corpus == corpora.APPENDIX:
                    self.assertEqual(self.proc_calls, [], label)
                    self.assertEqual(planned.corpus, corpora.USED_APPENDIX, label)
                else:
                    self.assertEqual(len(self.proc_calls), 1, label)
                    self.assertEqual(self.proc_calls[0]["only_docs"], corpora.ONLY_DOCS[c.corpus], label)
                    self.assertEqual(planned.corpus, corpora.used_for_mode(c.corpus), label)

    def test_product_command_stays_product_in_auto(self):
        for c in COMMANDS:
            if c.corpus == corpora.APPENDIX:
                for v in _samples(c):
                    self.assertFalse(procedural.is_procedural(c.render(v)), f"{c.key}: {v}")

    def test_prep_is_why_the_corpus_is_explicit(self):
        """«Подготовка к экспертизе» в «Авто» уходит в товарную ветку: команда этот пробел закрывает."""
        self.assertFalse(procedural.is_procedural(commands.find("prep").render()))

    def test_templates_are_safe(self):
        for c in COMMANDS:
            for v in _samples(c):
                t = c.render(v)
                self.assertFalse(meta.is_meta(t), c.key)
                self.assertFalse(translate.is_translate(t), c.key)
                self.assertEqual(sensitive.detect(t), [], c.key)
                self.assertLessEqual(len(t), 2000, c.key)

    def test_codes_reach_their_lookups(self):
        self.assertEqual(extract_okpd2(REQ.render(BULLDOZER)), BULLDOZER)
        self.assertTrue(extract_tnved_position(commands.find("st1").render("8471 30 000 0")))

    def test_sweep_is_built_from_the_catalog(self):
        rows = eval_routing.command_rows()
        self.assertEqual({r["key"] for r in rows}, {c.key for c in COMMANDS})
        report = "\n".join(eval_routing.summarize_commands(rows))
        self.assertIn("ТЕМА НЕ ТА, ЧТО В КАТАЛОГЕ (обязано быть 0): 0", report)
        self.assertIn("ТОВАРНАЯ КОМАНДА В «АВТО» УШЛА БЫ НА ПРОЦЕДУРНУЮ ВЕТКУ (обязано быть 0): 0", report)


class TestRequestModel(unittest.TestCase):
    def test_empty_message_without_command_is_rejected(self):
        with self.assertRaises(ValidationError):
            chat_mod.ChatRequest(message="")
        with self.assertRaises(ValidationError):
            chat_mod.ChatRequest(message="", command="")

    def test_command_without_field_may_come_empty(self):
        self.assertEqual(chat_mod.ChatRequest(command="docs").message, "")

    def test_apply_replaces_text_and_source(self):
        req = chat_mod.ChatRequest(message="бульдозеры", command="req", corpus="auto")
        self.assertEqual(chat_mod._apply_command(req), "req")
        self.assertEqual((req.message, req.corpus), (REQ.render("бульдозеры"), corpora.APPENDIX))

    def test_source_comes_from_the_command_not_from_the_client(self):
        req = chat_mod.ChatRequest(command="docs", corpus=corpora.METODREK)
        chat_mod._apply_command(req)
        self.assertEqual(req.corpus, corpora.RULES)

    def test_no_command_changes_nothing(self):
        req = chat_mod.ChatRequest(message="вопрос", corpus=corpora.RULES)
        self.assertIsNone(chat_mod._apply_command(req))
        self.assertEqual((req.message, req.corpus), ("вопрос", corpora.RULES))

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            chat_mod._apply_command(chat_mod.ChatRequest(message="x", command="нет_такой"))
        self.assertEqual(cm.exception.status_code, 422)
        self.assertIsInstance(cm.exception.detail, str, "фронт показывает detail человеку")

    def test_soon_cards_have_no_key_to_run(self):
        self.assertFalse(any(hasattr(s, "key") for s in SOON))
        self.assertTrue(all(c.key not in {s.title for s in SOON} for c in COMMANDS))

    def test_product_command_searches_by_value(self):
        req = chat_mod.ChatRequest(message="  насосы  ", command="req")
        chat_mod._apply_command(req)
        self.assertEqual(req.engine_extra(), {"search_text": "насосы"})

    def test_procedural_command_searches_by_full_text(self):
        req = chat_mod.ChatRequest(message="8471 30 000 0", command="st1")
        chat_mod._apply_command(req)
        self.assertEqual(req.engine_extra(), {})

    def test_client_cannot_set_search_text(self):
        req = chat_mod.ChatRequest(message="вопрос", search_text="другое", _search_text="другое")
        self.assertEqual(req.engine_extra(), {})


class TestRenderHasOneCaller(unittest.TestCase):
    """Сборка — только в `_apply_command`: по `co_names`, а не по тексту, в который попала бы проза."""

    def _names(self, fn):
        code = fn.__code__
        names = set(code.co_names)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                names |= set(const.co_names)
        return names

    def test_handlers_use_the_helper_and_do_not_build_text(self):
        for fn in (chat_mod.chat, chat_mod.chat_stream, guest.guest_chat, guest.guest_chat_stream):
            names = self._names(fn)
            self.assertIn("_apply_command", names, fn.__name__)
            for forbidden in ("render", "template", "clean_value"):
                self.assertNotIn(forbidden, names, f"{fn.__name__}: {forbidden}")

    def test_tariff_is_checked_after_the_command(self):
        """Повтор сверяется по собранному тексту: `_apply_command` обязан стоять раньше тарифа."""
        for fn in (chat_mod.chat, chat_mod.chat_stream):
            names = list(fn.__code__.co_names)
            self.assertLess(names.index("_apply_command"), names.index("_enforce_plan_quota"), fn.__name__)


class _Fake:
    text = "Ответ"
    hits: list = []
    rule_sources: list = []
    low_relevance = False
    unverified_numbers: list = []
    prompt_tokens = 1
    completion_tokens = 1
    input_hint = ""
    metered = True
    phantom_documents: list = []

    def __init__(self, corpus=corpora.USED_RULES):
        self.corpus = corpus


class _ApiBase(unittest.TestCase):
    def setUp(self):
        from main import app

        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session() as db:
            uid = q.create_user(db, "expert1", "h", role="expert").id
            self.user = q.get_user(db, uid)
            db.expunge(self.user)
        self.calls = []

        def fake(message, **kw):
            self.calls.append((message, kw))
            return _Fake()

        def fake_stream(message, **kw):
            self.calls.append((message, kw))
            yield "delta", "Ответ"
            yield "done", _Fake()

        for name, value in (("get_session", self.Session), ("answer", fake), ("answer_stream", fake_stream),
                            ("_chat_limit", SlidingWindow(1000, window=60.0))):
            p = mock.patch.object(chat_mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        app.dependency_overrides[auth_mod.require_user_profiled] = lambda: self.user
        app.dependency_overrides[auth_mod.require_user] = lambda: self.user
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def post(self, **body):
        return self.client.post("/api/chat", json=body)

    def stream(self, **body):
        r = self.client.post("/api/chat/stream", json=body)
        events = [json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: ")]
        return r, events

    def rows(self):
        with self.Session() as db:
            return list(db.execute(select(Message).order_by(Message.id)).scalars())


class TestChatApi(_ApiBase):
    def test_unknown_command_is_422(self):
        self.assertEqual(self.post(message="x", command="нет").status_code, 422)
        r, _ = self.stream(message="x", command="нет")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_empty_value_is_422_with_readable_reason(self):
        r = self.post(message="", command="req")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"], "Укажите: наименование или код ОКПД2")
        self.assertEqual(self.rows(), [], "отказанный вопрос в беседу не идёт")

    def test_long_value_is_422(self):
        self.assertEqual(self.post(message="х" * 201, command="req").status_code, 422)

    def test_empty_message_without_command_is_still_422(self):
        self.assertEqual(self.post(message="").status_code, 422)

    def test_personal_data_in_value_is_checked_after_assembly(self):
        """Два пробела: в сыром значении проверка паспорт не видит, в собранном запросе — видит."""
        value = "паспорт 4510  123456"
        self.assertEqual(sensitive.detect(value), [])
        r = self.post(message=value, command="req")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_chat_answers_with_assembled_question(self):
        r = self.post(message=BULLDOZER, command="req", corpus="auto", session_id="s1")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        want = REQ.render(BULLDOZER)
        self.assertEqual(data["question"], want)
        message, kw = self.calls[0]
        self.assertEqual(message, want)
        self.assertEqual(kw["corpus"], corpora.APPENDIX)
        self.assertEqual(kw["okpd2"], BULLDOZER, "код из значения доходит до буста")
        self.assertEqual(kw["search_text"], BULLDOZER, "поиск видит значение, а не весь шаблон")
        user_row, bot_row = self.rows()
        self.assertEqual((user_row.content, user_row.command), (want, None))
        self.assertEqual((bot_row.command, bot_row.corpus), ("req", f"{corpora.USED_RULES}|appendix"))
        self.assertIn("источник задан командой «Требования к продукции»", data["corpus_label"])

    def test_command_without_field(self):
        r = self.post(command="docs", session_id="s1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls[0][0], DOCS.render())

    def test_stream_writes_the_same(self):
        r, events = self.stream(message=BULLDOZER, command="req", session_id="s1")
        self.assertEqual(r.status_code, 200)
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["question"], REQ.render(BULLDOZER))
        self.assertIn("источник задан командой", done["corpus_label"])
        self.assertEqual(self.calls[0][1]["corpus"], corpora.APPENDIX)
        self.assertEqual(self.calls[0][1]["search_text"], BULLDOZER)
        user_row, bot_row = self.rows()
        self.assertEqual(user_row.content, REQ.render(BULLDOZER))
        self.assertEqual(bot_row.command, "req")

    def test_ordinary_question_keeps_its_label(self):
        data = self.post(message="вопрос", corpus="rules", session_id="s1").json()
        self.assertIn("выбран вручную", data["corpus_label"])
        self.assertEqual(data["question"], "вопрос")
        self.assertIsNone(self.rows()[-1].command)

    def test_reopened_conversation_and_exports_name_the_command(self):
        self.post(message=BULLDOZER, command="req", session_id="s1")
        conv = self.client.get("/api/conversations/s1").json()
        self.assertEqual(conv["messages"][0]["command"], "Требования к продукции")
        self.assertIn("задан командой", conv["messages"][1]["corpus_label"])
        for fmt in ("md", "txt", "json"):
            body = self.client.get(f"/api/conversations/s1/export?fmt={fmt}").text
            self.assertIn("источник задан командой", body, fmt)
            self.assertIn("готовая команда", body, f"{fmt}: шапка объясняет, кто выбрал источник")

    def test_key_removed_from_catalog_keeps_the_truth(self):
        self.post(message="вопрос", corpus="rules", session_id="s1")
        with self.Session() as db:
            db.execute(text("UPDATE messages SET command='снята' WHERE role='assistant'"))
            db.commit()
        conv = self.client.get("/api/conversations/s1").json()
        self.assertIn("задан командой «снята»", conv["messages"][1]["corpus_label"])
        self.assertNotIn("command", conv["messages"][0])

    def test_follow_up_is_labelled_by_the_command_and_is_not_a_run(self):
        self.post(message=BULLDOZER, command="req", session_id="s1")
        r = self.post(message="а для насосов?", corpus=corpora.APPENDIX, session_id="s1").json()
        self.assertIn("источник задан командой «Требования к продукции»", r["corpus_label"])
        self.assertEqual(self.rows()[-1].command, commands.held("req"))
        conv = self.client.get("/api/conversations/s1").json()
        self.assertNotIn("command", conv["messages"][2], "подпись «Команда: …» только у запуска")
        self.assertIn("задан командой", conv["messages"][3]["corpus_label"])

    def test_follow_up_in_stream_too(self):
        self.stream(message=BULLDOZER, command="req", session_id="s1")
        _, events = self.stream(message="а для насосов?", corpus=corpora.APPENDIX, session_id="s1")
        self.assertIn("задан командой", events[-1]["corpus_label"])
        self.assertEqual(self.rows()[-1].command, commands.held("req"))

    def test_source_changed_by_hand_is_manual_again(self):
        self.post(message=BULLDOZER, command="req", session_id="s1")
        r = self.post(message="а по Правилам?", corpus=corpora.RULES, session_id="s1").json()
        self.assertIn("выбран вручную", r["corpus_label"])
        self.assertIsNone(self.rows()[-1].command)

    def test_held_label_survives_several_turns(self):
        self.post(message=BULLDOZER, command="req", session_id="s1")
        self.post(message="а для насосов?", corpus=corpora.APPENDIX, session_id="s1")
        r = self.post(message="а для кранов?", corpus=corpora.APPENDIX, session_id="s1").json()
        self.assertIn("задан командой", r["corpus_label"])

    def test_reopened_conversation_keeps_the_source(self):
        self.post(message=BULLDOZER, command="req", session_id="s1")
        self.assertEqual(self.client.get("/api/conversations/s1").json()["held_corpus"], corpora.APPENDIX)
        self.post(message="вопрос", corpus=corpora.RULES, session_id="s2")
        self.assertIsNone(self.client.get("/api/conversations/s2").json()["held_corpus"])

    def test_sidebar_titles_differ_between_runs(self):
        self.post(message="насосы", command="req", session_id="s1")
        titles = {c["session_id"]: c["title"] for c in self.client.get("/api/conversations").json()}
        self.assertEqual(titles["s1"], "Требования к продукции: насосы")

    def test_answer_event_has_the_key(self):
        with mock.patch.object(chat_mod, "log_answer_event") as ev:
            self.post(message=BULLDOZER, command="req", session_id="s1")
        self.assertEqual(ev.call_args.kwargs["command"], "req")


class TestRepeatOfCommandIsNotCharged(_ApiBase):
    def setUp(self):
        super().setUp()
        self.user.plan = "Старт"
        self.charged = []
        real = q.record_answer_usage
        p = mock.patch.object(q, "record_answer_usage",
                              side_effect=lambda db, uid: (self.charged.append(uid), real(db, uid)))
        p.start()
        self.addCleanup(p.stop)
        st = mock.Mock(exhausted=False)
        p2 = mock.patch.object(chat_mod.quota, "quota_state", return_value=st)
        p2.start()
        self.addCleanup(p2.stop)

    def test_second_run_is_free(self):
        self.post(message="бульдозеры", command="req", session_id="s1")
        self.assertEqual(len(self.charged), 1)
        self.post(message="бульдозеры", command="req", session_id="s1")
        self.assertEqual(len(self.charged), 1, "повтор команды списан второй раз")

    def test_exhausted_plan_stops_a_command(self):
        chat_mod.quota.quota_state.return_value = mock.Mock(exhausted=True, used=100, limit=100)
        with mock.patch.object(chat_mod.quota, "exhausted_message", return_value="лимит"):
            r = self.post(message="бульдозеры", command="req", session_id="s1")
        self.assertEqual(r.status_code, 402)
        self.assertEqual(self.calls, [], "движок не зовётся")
        self.assertEqual(self.rows(), [], "вопрос в беседу не идёт")

    def test_other_value_is_a_new_answer(self):
        self.post(message="бульдозеры", command="req", session_id="s1")
        self.post(message="насосы", command="req", session_id="s1")
        self.assertEqual(len(self.charged), 2)


class TestGuest(_GuestBase):
    def setUp(self):
        super().setUp()
        self.seen = []

        def fake(message, **kw):
            self.seen.append((message, kw))
            return _Ans()

        def fake_stream(message, **kw):
            self.seen.append((message, kw))
            yield "delta", "Ответ"
            yield "done", _Ans()

        for name, value in (("answer", fake), ("answer_stream", fake_stream)):
            p = mock.patch.object(guest, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_command_counts_as_a_trial_question(self):
        r = self.client.post("/api/guest/chat", json={"message": "насосы", "command": "req", "session_id": "g"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["question"], REQ.render("насосы"))
        self.assertEqual(self.seen[0][0], REQ.render("насосы"))
        self.assertEqual(self.seen[0][1]["corpus"], corpora.APPENDIX)
        self.assertEqual(self.charged(), 1)
        q_row, a_row = self.rows()
        self.assertEqual(q_row.content, REQ.render("насосы"))
        self.assertEqual(a_row.command, "req")

    def test_stream(self):
        r = self.client.post("/api/guest/chat/stream",
                             json={"message": "насосы", "command": "req", "session_id": "g"})
        done = [json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: ")][-1]
        self.assertEqual(done["question"], REQ.render("насосы"))
        self.assertEqual(self.rows()[-1].command, "req")

    def test_rejected_value_does_not_spend_the_trial(self):
        r = self.client.post("/api/guest/chat", json={"message": "", "command": "req", "session_id": "g"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.rows(), [])

    def test_repeat_is_served_from_the_journal(self):
        class WithCorpus(_Ans):
            corpus = corpora.USED_APPENDIX   # режим повтора читается из сохранённого «корпус|режим»

        guest.answer = lambda message, **kw: (self.seen.append((message, kw)), WithCorpus())[1]
        body = {"message": "насосы", "command": "req", "session_id": "g"}
        first = self.client.post("/api/guest/chat", json=body).json()
        second = self.client.post("/api/guest/chat", json=body)
        self.assertEqual(len(self.seen), 1, "повтор команды снова позвал движок")
        self.assertEqual(second.json()["answer"], first["answer"])
        self.assertEqual(second.json()["question"], REQ.render("насосы"))
        self.assertEqual(self.charged(), 1)

    def test_trial_is_over_for_a_command_too(self):
        with mock.patch.object(guest.settings, "GUEST_TRIAL_QUESTIONS", 0):
            r = self.client.post("/api/guest/chat", json={"message": "насосы", "command": "req", "session_id": "g"})
        self.assertEqual(r.status_code, 402)
        self.assertEqual(self.seen, [])

    def test_search_text_reaches_the_engine(self):
        self.client.post("/api/guest/chat", json={"message": "насосы", "command": "req", "session_id": "g"})
        self.assertEqual(self.seen[0][1]["search_text"], "насосы")

    def test_follow_up_is_labelled_by_the_command(self):
        class WithCorpus(_Ans):
            corpus = corpora.USED_APPENDIX

        guest.answer = lambda message, **kw: WithCorpus()
        self.client.post("/api/guest/chat", json={"message": "насосы", "command": "req", "session_id": "g"})
        r = self.client.post("/api/guest/chat",
                             json={"message": "а для кранов?", "corpus": "appendix", "session_id": "g"})
        self.assertIn("задан командой", r.json()["corpus_label"])
        self.assertEqual(self.rows()[-1].command, commands.held("req"))

    def test_guest_page_shows_commands(self):
        self.assertIn("window.COMMANDS", self.page.text)
        self.assertIn('data-command="req"', self.page.text)


class TestAdminSlice(_ApiBase):
    def test_rows_count_uses_ratings_and_errors(self):
        for sid in ("s1", "s2"):
            self.post(message=BULLDOZER, command="req", session_id=sid)
        bots = [m for m in self.rows() if m.role == "assistant"]
        with self.Session() as db:
            db.add(Feedback(user_id=self.user.id, kind="answer", message_id=bots[0].id, session_id="s1", rating=4))
            db.add(Feedback(user_id=self.user.id, kind="answer", message_id=bots[1].id, session_id="s2",
                            correction="неверно"))
            db.commit()
            stats = admin_stats.build_admin_view(db)["stats"]
        rows = {r["title"]: r for r in stats["commands"]}
        self.assertEqual(len(rows), len(COMMANDS), "команда без запусков тоже в таблице")
        row = rows["Требования к продукции"]
        self.assertEqual((row["uses"], row["rated"], row["avg"], row["error_pct"]), (2, 1, 4.0, 50))
        self.assertEqual(rows["Экспертиза ТПП"]["uses"], 0)
        self.assertIsNone(rows["Экспертиза ТПП"]["avg"])

    def test_key_outside_catalog_stays_visible(self):
        rows = admin_stats._command_rows({"снята": {"ids": {1, 2, 3}, "ratings": [], "errors": set()}})
        self.assertEqual((rows[-1]["title"], rows[-1]["uses"]), ("снята", 3))

    def test_follow_ups_are_not_runs(self):
        self.post(message=BULLDOZER, command="req", session_id="s1")
        self.post(message="а для насосов?", corpus=corpora.APPENDIX, session_id="s1")
        with self.Session() as db:
            stats = admin_stats.build_admin_view(db)["stats"]
        self.assertEqual(len(stats["commands"]), len(COMMANDS), "уточнение дало отдельную строку")
        rows = {r["title"]: r for r in stats["commands"]}
        self.assertEqual(rows["Требования к продукции"]["uses"], 1)

    def test_rating_outside_the_period_is_not_counted(self):
        """Иначе фильтр периода давал «0 запусков, оценка 4» и «с ошибкой 300 %»."""
        self.post(message=BULLDOZER, command="req", session_id="s1")
        bot = [m for m in self.rows() if m.role == "assistant"][0]
        with self.Session() as db:
            db.add(Feedback(user_id=self.user.id, kind="answer", message_id=bot.id, session_id="s1", rating=4))
            db.execute(text("UPDATE messages SET ts='2026-01-01 12:00:00'"))
            db.commit()
            stats = admin_stats.build_admin_view(db, date_from=date.today(), date_to=date.today())["stats"]
        row = {r["title"]: r for r in stats["commands"]}["Требования к продукции"]
        self.assertEqual((row["uses"], row["rated"], row["avg"]), (0, 0, None))

    def test_admin_export_note_names_the_command(self):
        src = (ROOT / "app" / "api" / "admin.py").read_text(encoding="utf-8")
        self.assertIn("сервис, пользователь или готовая команда", src)


class _SearchStubs(_Stubbed):
    def setUp(self):
        super().setUp()
        self.searched = []
        self.prompts = []
        pl.embed_query = lambda t: (self.searched.append(("embed", t)), [0.1] * 4)[1]
        pl.search = lambda q_, **k: (self.searched.append(("search", q_)), [self.hit])[1]
        real = pl.build_navigator_user_prompt
        self.addCleanup(setattr, pl, "build_navigator_user_prompt", real)

        def spy(*a, **k):
            self.prompts.append((a, k))
            return real(*a, **k)
        pl.build_navigator_user_prompt = spy
        low = pl.product_low_relevance
        self.addCleanup(setattr, pl, "product_low_relevance", low)
        pl.product_low_relevance = lambda s, *a: (self.searched.append(("gate", s)), False)[1]


class TestSearchByValue(_SearchStubs):
    """Находка ревью: текст шаблона в поиске и в гейте релевантности хуже прямого ввода (§11 ТЗ)."""

    hit = Hit(score=0.9, section_roman="I", section_title="Раздел", product_name="Насосы",
              okpd2_codes=[], min_threshold=None, requirement_blocks=[], source_anchor="а")

    def test_search_and_gate_see_only_the_value(self):
        question = REQ.render("насосы")
        pl._plan_answer(question, corpus=corpora.APPENDIX, search_text="насосы")
        self.assertEqual({what: t for what, t in self.searched},
                         {"embed": "насосы", "search": "насосы", "gate": "насосы"})

    def test_model_sees_the_whole_question(self):
        question = REQ.render("насосы")
        pl._plan_answer(question, corpus=corpora.APPENDIX, search_text="насосы")
        args, kw = self.prompts[0]
        self.assertEqual(args[0], question)
        self.assertIsNone(kw["resolved"], "значение команды не выдаётся за переписанный вопрос")

    def test_ordinary_question_is_not_touched(self):
        pl._plan_answer("требования к насосам", corpus=corpora.APPENDIX)
        self.assertEqual({t for _, t in self.searched}, {"требования к насосам"})

    def test_commands_with_a_field_search_by_value_except_st1(self):
        """У `st1` значение — голый код ТН ВЭД: поиску нужны слова шаблона."""
        for c in COMMANDS:
            self.assertEqual(c.search_by_value, bool(c.field) and c.key != "st1", c.key)

    def test_procedural_command_passes_the_value_to_search(self):
        plain = commands.find("plain")
        pl._plan_answer(plain.render("пункт 32 Правил реестра"), corpus=plain.corpus,
                        search_text="пункт 32 Правил реестра")
        self.assertEqual(self.proc_calls[0]["search_query"], "пункт 32 Правил реестра")


class TestMigration(unittest.TestCase):
    def test_existing_tables_get_the_column(self):
        eng = create_engine("sqlite:///:memory:", poolclass=StaticPool)
        Base.metadata.create_all(eng)
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE messages DROP COLUMN command"))
            conn.execute(text("ALTER TABLE guest_messages DROP COLUMN command"))
        with mock.patch.object(engine_mod, "engine", eng):
            engine_mod._ensure_columns()
        with eng.connect() as conn:
            for table in ("messages", "guest_messages"):
                cols = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
                self.assertIn("command", cols, table)


class TestFront(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web = ROOT / "app" / "web"
        cls.js = (web / "static" / "chat.js").read_text(encoding="utf-8")
        cls.html = (web / "templates" / "chat.html").read_text(encoding="utf-8")
        cls.cards = (web / "templates" / "_commands.html").read_text(encoding="utf-8")
        cls.css = (web / "static" / "style.css").read_text(encoding="utf-8")

    def test_macket_texts(self):
        for s in ("Готовые команды", "Показать все",
                  "Сокращают путь от вопроса до ответа — запрос уже сформулирован, остаётся указать продукцию",
                  "Сокращают путь от вопроса до ответа — всё нужное уже настроено"):
            self.assertIn(s, self.html)
        self.assertIn("Добавить в боковое меню", self.cards)
        self.assertIn("Скоро", self.cards)

    def test_both_paths_send_the_command(self):
        self.assertEqual(self.js.count("JSON.stringify(withCommand(pending, { message: text, session_id: sessionId, corpus }))"), 2)

    def test_source_switch_is_locked_and_not_saved(self):
        self.assertIn("corpusSelect.disabled = !!cmd", self.js)
        self.assertIn("Источник задан командой", self.js)
        fn = self.js[self.js.index("function setCommand("):self.js.index("function clearCommand(")]
        self.assertNotIn("localStorage", fn, "команда не перезаписывает выбор человека")

    def test_mode_ends_with_navigation_and_with_sending(self):
        go_home = self.js[self.js.index("function goHome("):self.js.index("const newChat =")]
        self.assertIn("clearCommand()", go_home)
        self.assertIn("leaveCommands()", go_home)
        ask = self.js[self.js.index("async function ask("):self.js.index("// Пробный режим: отказы сервера")]
        self.assertIn("setCommand(null); corpusHeld = true;", ask)

    def test_rejected_command_comes_back(self):
        fn = self.js[self.js.index("async function handleRejected("):self.js.index("async function ask(")]
        self.assertIn("restoreCommand(pending)", fn)

    def test_no_copy_of_templates_in_js(self):
        for c in COMMANDS:
            self.assertNotIn(c.title, self.js, c.key)
            self.assertNotIn(c.template[:30], self.js, c.key)

    def test_stale_run_does_not_touch_the_screen(self):
        """Ответ, пришедший после ухода на новую беседу, не возвращает прошлую (`isCurrent`)."""
        for fn, nxt in (("async function askStream(", "async function askFallback("),
                        ("async function askFallback(", "// Данные, которые нельзя отправлять")):
            body = self.js[self.js.index(fn):self.js.index(nxt)]
            self.assertRegex(body, r"if \(isCurrent\(pending\)\) \{\s+sessionId = ", fn)
            self.assertRegex(body, r"if \(isCurrent\(pending\)\) setHint\(", fn)
            self.assertNotIn("sessionId = null", body, f"{fn}: сброс беседы — только через dropNew")
        self.assertIn("askRun += 1;   // ответ, который ещё идёт", self.js)

    def test_reopened_conversation_holds_the_source(self):
        fn = self.js[self.js.index("async function openConversation("):self.js.index("// --- отправка вопроса ---")]
        self.assertRegex(fn, r"if \(data\.held_corpus && corpusSelect\) \{[^\n]*\n\s+corpusSelect\.value = "
                             r"data\.held_corpus;\s+corpusHeld = true;")

    def test_legal_note_stays_above_the_commands(self):
        self.assertLess(self.html.index('class="composer-disclaimer"'), self.html.index('id="cmd-home"'))

    def test_value_is_substituted_as_plain_text(self):
        self.assertIn('cmd.template.replace("{value}", () => v)', self.js)

    def test_tour_step_and_new_key(self):
        self.assertIn('sel: "#commands-all"', self.js)
        self.assertIn('"tour719Seen_v3"', self.js)

    def test_page_behaves_like_history(self):
        self.assertIn(".main.commands-mode .chat-scroll", self.css)
        self.assertIn('id="commands-page"', self.html)
        self.assertRegex(self.html, r"window\.COMMANDS = \{\{ commands\.active \| tojson \}\}")

    def test_hidden_commands_are_not_runnable_from_soon_cards(self):
        macro = self.cards[self.cards.index("{% macro soon_card"):]
        self.assertNotIn("data-command", macro)
        self.assertNotIn("<button", macro)


@unittest.skipUnless(shutil.which("node"), "node не найден — поведенческая часть пропущена")
class TestFrontStates(unittest.TestCase):
    """Состояния режима команды исполняются на node: подстрока в исходнике их не проверяет
    (ревью: шесть поломок переходов прошли при зелёных тестах). Режется кусок между метками chat.js."""

    STUB = r"""
const assert = require("node:assert");
const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
function box() {
  const cls = new Set();
  return { classList: { toggle(c, on) { if (on) cls.add(c); else cls.delete(c); }, add(c) { cls.add(c); },
                        remove(c) { cls.delete(c); }, contains(c) { return cls.has(c); } },
           textContent: "", title: "", value: "", attrs: {},
           removeAttribute(k) { if (k === "maxlength") delete this.maxLength; } };
}
const els = {};
const document = { getElementById: (id) => (els[id] = els[id] || box()) };
const main = box(), form = box(), input = box();
const corpusSelect = Object.assign(box(), { value: "auto", disabled: false,
                                            options: [{ value: "auto" }, { value: "rules" }, { value: "appendix" }] });
const corpusPick = Object.assign(box(), { title: "обычная" });
const CORPUS_PICK_TITLE = "обычная", CORPUS_KEY = "corpusMode";
let activeCommand = null, corpusHeld = false, askRun = 0, sessionId = null;
let hidden = 0, removed = [];
function hideInputBlock() { hidden += 1; }
function setPlaceholder() {}
function removeHistoryItem(sid) { removed.push(sid); }
function savedCorpus() {
  const saved = localStorage.getItem(CORPUS_KEY);
  return saved && corpusSelect.options.some((o) => o.value === saved) ? saved : "auto";
}
const CMD = { key: "req", title: "Требования", desc: "о", corpus: "appendix", corpus_title: "Приложение",
              template: "вопрос про {value}?", field: { label: "Наименование", max_len: 200 } };
const BARE = { key: "docs", title: "Документы", desc: "о", corpus: "rules", corpus_title: "Правила",
               template: "вопрос без поля", field: null };
function sent(cmd, value, extra) {
  const bubble = { textContent: "старый" };
  return { _sent: { command: cmd, value, sid: "S", run: askRun,
                    userEl: { querySelector: () => bubble }, ...extra }, bubble };
}
"""

    CHECKS = r"""
// режим команды: источник заблокирован, выбор человека не тронут
store.corpusMode = "rules"; corpusSelect.value = "rules";
setCommand(CMD);
assert.strictEqual(corpusSelect.value, "appendix");
assert.strictEqual(corpusSelect.disabled, true);
assert.strictEqual(corpusPick.title, "Источник задан командой");
assert.strictEqual(input.maxLength, 200);
assert.strictEqual(store.corpusMode, "rules");
// крестик возвращает выбор человека
clearCommand();
assert.strictEqual(corpusSelect.value, "rules");
assert.strictEqual(corpusSelect.disabled, false);
assert.strictEqual(corpusPick.title, "обычная");
assert.strictEqual(input.maxLength, undefined);
assert.strictEqual(hidden, 1, "мягкое предупреждение снимается вместе с командой");
// после ответа источник держится, новая беседа его возвращает
setCommand(CMD); setCommand(null); corpusHeld = true;
assert.strictEqual(corpusSelect.value, "appendix");
assert.strictEqual(corpusSelect.disabled, false);
clearCommand();
assert.strictEqual(corpusSelect.value, "rules");
assert.strictEqual(corpusHeld, false);
// выбора не было: clearCommand не трогает переключатель, если держать нечего
corpusSelect.value = "appendix";
clearCommand();
assert.strictEqual(corpusSelect.value, "appendix");
// команда без поля: ввод закрыт, отправить можно сразу
setCommand(BARE);
assert.strictEqual(input.readOnly, true);
assert.strictEqual(form.classList.contains("ready"), true);
clearCommand();
assert.strictEqual(input.readOnly, false);
// значение подставляется как текст, тело запроса несёт ключ и значение
assert.strictEqual(commandText(CMD, "  a   $& b "), "вопрос про a $& b?");
assert.strictEqual(commandTitle(CMD, " насосы "), "Требования: насосы");
assert.strictEqual(commandTitle(BARE, ""), "Документы");
let p = sent(CMD, "насосы");
assert.deepStrictEqual(withCommand(p, { message: "собранный", session_id: "другая", corpus: "appendix" }),
                       { message: "насосы", session_id: "S", corpus: "appendix", command: "req" });
// обычный вопрос не получает чужую команду
p = sent(null, "");
assert.deepStrictEqual(withCommand(p, { message: "вопрос", session_id: "другая", corpus: "auto" }),
                       { message: "вопрос", session_id: "S", corpus: "auto" });
// сверка пузыря идёт в СВОЙ вопрос
p = sent(CMD, "насосы");
syncQuestion(p, "собранный");
assert.strictEqual(p.bubble.textContent, "собранный");
// отказ возвращает команду и значение
input.value = "";
p = sent(CMD, "насосы");
assert.strictEqual(restoreCommand(p), true);
assert.strictEqual(input.value, "насосы");
assert.strictEqual(activeCommand, CMD);
clearCommand();
// человек ушёл дальше: устаревший запрос экран не трогает
input.value = "новый ввод";
p = sent(CMD, "насосы");
askRun += 1;
assert.strictEqual(isCurrent(p), false);
assert.strictEqual(restoreCommand(p), false);
assert.strictEqual(input.value, "новый ввод");
assert.strictEqual(activeCommand, null);
// несостоявшаяся беседа снимается, чужая на экране остаётся
sessionId = "S"; removed = [];
dropNew(sent(null, ""), true);
assert.strictEqual(sessionId, null);
assert.deepStrictEqual(removed, ["S"]);
sessionId = "НОВАЯ";
dropNew(sent(null, ""), true);
assert.strictEqual(sessionId, "НОВАЯ", "старый запрос сбросил новую беседу");
sessionId = "S";
dropNew(sent(null, ""), false);
assert.strictEqual(sessionId, "S");
console.log("ok");
"""

    def test_transitions(self):
        js = (ROOT / "app" / "web" / "static" / "chat.js").read_text(encoding="utf-8")
        start, end = "// L8-1-состояние-начало", "// L8-1-состояние-конец"
        block = js[js.index(start):js.index(end)]
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "states.js"
            script.write_text(self.STUB + block + self.CHECKS, encoding="utf-8")
            r = subprocess.run([shutil.which("node"), str(script)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIn("ok", r.stdout)


class TestDocsOfCatalogStayTrue(unittest.TestCase):
    def test_help_page_lists_commands(self):
        html = (ROOT / "app" / "web" / "templates" / "help.html").read_text(encoding="utf-8")
        self.assertIn("готовые команды", html)
        guide = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
        self.assertRegex(guide, re.compile(r"Готовые команды"))


if __name__ == "__main__":
    unittest.main()
