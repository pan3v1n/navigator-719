"""Юнит-тесты auth-примитивов (bcrypt) — офлайн, без БД/сети.

Полный login/web-флоу (редиректы, гейты ролей, лог диалога, фидбек, админ-вью) проверяется
интеграционно через TestClient (см. историю сборки); здесь — чистая криптологика хэша пароля.
Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.auth import hash_password, verify_password  # noqa: E402


class TestAuthPasswords(unittest.TestCase):
    def test_hash_verify_roundtrip(self):
        h = hash_password("СекретныйПароль123")
        self.assertTrue(verify_password("СекретныйПароль123", h))

    def test_hash_is_salted(self):
        # соль внутри → два хэша одного пароля различаются, но оба верифицируются
        h1, h2 = hash_password("pw"), hash_password("pw")
        self.assertNotEqual(h1, h2)
        self.assertTrue(verify_password("pw", h1))
        self.assertTrue(verify_password("pw", h2))

    def test_verify_rejects_wrong(self):
        h = hash_password("right")
        self.assertFalse(verify_password("wrong", h))

    def test_verify_handles_bad_hash(self):
        # не-bcrypt строка не должна ронять verify (возвращаем False)
        self.assertFalse(verify_password("pw", "не-хэш"))


# =========================================================================== #
# Пробный режим без входа (решение владельца 09.10.2026) — доступ без авторизации.
#
# Закрепляем: N вопросов на браузер (кука), потолки по IP и общий суточный, повтор отвеченного
# бесплатен, гостевые реплики — в своей таблице и без IP, ответ дешевле (контекст, brief), текста
# источника гость не получает, страница и вход знают о режиме, выключатель закрывает всё.
# =========================================================================== #
import json  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from sqlalchemy import create_engine, inspect, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api import admin as admin_mod  # noqa: E402
from app.api import guest, trial, web  # noqa: E402
from app.api.ratelimit import SlidingWindow  # noqa: E402
from app.core import plans  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base, GuestMessage, Message  # noqa: E402
from app.rag import pipeline  # noqa: E402

RULE = {"source_anchor": "п. 7 Правил", "doc_type": "rules_registry",
        "text": "Заявки для выдачи акта экспертизы рассматривает Торгово-промышленная палата."}


class _Ans:
    text = "Ответ движка"
    hits = []
    cases = []
    rule_sources = [RULE]
    low_relevance = False
    unverified_numbers = []
    prompt_tokens = 10
    completion_tokens = 5
    input_hint = ""
    metered = True


class _MetaAns(_Ans):
    text = "Здравствуйте!"
    metered = False
    rule_sources = []


class _GuestBase(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from main import app

        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.calls = []
        patches = [mock.patch.object(guest, "get_session", self.Session),
                   mock.patch.object(web, "get_session", self.Session),
                   mock.patch.object(admin_mod, "get_session", self.Session),
                   mock.patch.object(guest, "_ip_limit", SlidingWindow(1000, window=86400.0)),
                   mock.patch.object(guest, "answer", self._engine(_Ans)),
                   mock.patch.object(guest, "answer_stream", self._stream(_Ans))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)
        self.page = self.client.get("/chat")       # гость открывает сервис — ставится кука

    def _engine(self, ans):
        def fake(message, **kw):
            self.calls.append(kw)
            return ans()
        return fake

    def _stream(self, ans):
        def fake(message, **kw):
            self.calls.append(kw)
            a = ans()
            yield "delta", a.text
            yield "done", a
        return fake

    def ask(self, text="Требования к станкам?", sid="s1", client=None):
        return (client or self.client).post("/api/guest/chat", json={"message": text, "session_id": sid})

    def rows(self, model=GuestMessage):
        with self.Session() as db:
            return list(db.execute(select(model).order_by(model.id)).scalars())

    def charged(self):
        return sum(1 for m in self.rows() if m.charged)


class TestLoginDemo(unittest.TestCase):
    """Демо-чат на синей панели входа (как на лендинге, 09.10.2026). Страница публичная: каждый
    пример ссылается на пункт и обязан стоять на его тексте — цитату ищем в корпусе внутри ЭТОГО
    пункта, а не во всём документе."""

    KB = ROOT / "knowledge_base" / "pp719"

    @staticmethod
    def _norm(s):
        return " ".join(s.replace("ё", "е").split())

    def test_every_quote_is_in_its_point(self):
        import re

        for d in web.LOGIN_DEMO:
            with self.subTest(ref=d["ref"]):
                text = (self.KB / d["src"]).read_text(encoding="utf-8")
                start = text.index(d["start"])
                # пункт кончается на следующем номере («\n8. », «\n4.2. ») или строке позиции приложения
                nxt = re.compile(r"\n(\d+(\.\d+)*\.? |(из )?\d{2}\.\d{2}[\d.]*\|)").search(text, start + 1)
                point = text[start:nxt.start() if nxt else len(text)]
                self.assertIn(self._norm(d["quote"]), self._norm(point), "цитата не из этого пункта")

    def test_no_phrases_the_points_do_not_have(self):
        for d in web.LOGIN_DEMO:
            self.assertNotIn("территориальн", d["a"], "«через территориальные палаты» в пунктах нет")
            self.assertNotIn("конструкторск", d["a"], "перечня документов к заявке в пунктах нет")

    def test_page_starts_with_the_checked_example_and_animates(self):
        from fastapi.testclient import TestClient

        from main import app

        html = TestClient(app).get("/login").text
        self.assertIn('id="auth-demo-ref">п. 7 Правил</span>', html, "без JS — первый, сверенный пример")
        self.assertIn("var DEMO = ", html)
        self.assertIn("prefers-reduced-motion", html, "анимация не уважает «уменьшить движение»")
        self.assertEqual(html.count('<span class="on"></span>') + html.count("<span></span>") >= len(web.LOGIN_DEMO), True)
        self.assertNotIn("quote", html, "служебные поля сверки ушли на страницу")


class TestGuestCookie(unittest.TestCase):
    def _req(self, value):
        return SimpleNamespace(cookies={trial.GUEST_COOKIE: value} if value is not None else {})

    def test_roundtrip_and_forgery(self):
        from fastapi import Response

        gid = trial.new_guest_id()
        resp = Response()
        trial.set_guest_cookie(resp, gid)
        raw = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        self.assertEqual(trial.guest_id(self._req(raw)), gid)
        self.assertIn("httponly", resp.headers["set-cookie"].lower())
        self.assertIsNone(trial.guest_id(self._req(None)))
        self.assertIsNone(trial.guest_id(self._req(gid)), "неподписанный id принят — счёт подделывается")
        self.assertIsNone(trial.guest_id(self._req(raw[:-2] + "xx")), "подпись не проверяется")
        forged = trial._signer.dumps("../../etc")
        self.assertIsNone(trial.guest_id(self._req(forged)), "id не того вида принят")


class TestGuestPage(_GuestBase):
    def test_anonymous_chat_is_the_trial(self):
        r = self.page
        self.assertEqual(r.status_code, 200)
        self.assertIn(trial.GUEST_COOKIE, r.cookies, "страница не поставила куку гостя")
        html = r.text
        self.assertIn("Вы используете пробный доступ", html)
        self.assertIn("Доступно 3 вопроса без входа", html)
        self.assertIn('class="ds-btn head-login" href="/login"', html)
        self.assertIn('"remaining": 3', html)
        for absent in ('id="export-btn"', 'id="fb-open"', 'id="history-all"', 'href="/profile"', 'href="/logout"'):
            self.assertNotIn(absent, html, f"гостю показан {absent}")
        self.assertIn('class="history hidden" id="history"', html, "chat.js берёт из контейнера заголовок беседы")
        again = self.client.get("/chat")
        self.assertNotIn(trial.GUEST_COOKIE, again.cookies, "кука гостя перевыпущена — счёт обнулился бы")

    def test_switch_off_sends_to_login(self):
        from fastapi.testclient import TestClient

        from main import app

        with mock.patch.object(settings, "GUEST_TRIAL_ENABLED", False):
            r = TestClient(app).get("/chat", follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (302, "/login"))
            self.assertEqual(self.ask().status_code, 404, "ручки пробного режима открыты при выключателе")
            self.assertNotIn("Попробовать без входа", self.client.get("/login").text)

    def test_logged_in_user_sees_no_trial(self):
        user = web.User(id=1, username="kursk.expert1", role="expert", full_name="Иван Петров")
        with mock.patch.object(web, "current_user", return_value=user), \
             mock.patch.object(web.quota, "quota_view", return_value=None):
            html = self.client.get("/chat").text
        self.assertNotIn("пробный доступ", html)
        self.assertIn("window.GUEST = null", html)
        self.assertIn('id="fb-open"', html)

    def test_login_page_has_no_tariff_lead(self):
        """Решение владельца 09.10.2026: подзаголовка «Для сотрудников организаций с подключённым
        тарифом» на входе нет — его не было и в макете, а с пробным режимом он ещё и неправда."""
        html = self.client.get("/login").text
        self.assertIn("Вход в Навигатор", html)
        self.assertNotIn("подключённым тарифом", html)

    def test_login_page_knows_the_trial(self):
        html = self.client.get("/login").text
        self.assertIn('href="/chat">Попробовать без входа — 3', html)
        over = self.client.get("/login?trial_over=1").text
        self.assertIn("Пробные вопросы закончились. Войдите, чтобы продолжить работу.", over)
        self.assertNotIn("Попробовать без входа", over, "после исчерпания звать обратно в пробу незачем")


class TestGuestTrialLimits(_GuestBase):
    def test_three_answers_then_login(self):
        for i in range(3):
            r = self.ask(f"Вопрос {i}", sid=f"s{i}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["trial"], {"limit": 3, "used": i + 1, "remaining": 2 - i})
        r = self.ask("Четвёртый", sid="s9")
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["detail"], guest.TRIAL_OVER)
        self.assertEqual(len(self.calls), 3, "на исчерпанной пробе движок не зовётся")
        self.assertNotIn("Четвёртый", [m.content for m in self.rows()], "отклонённый вопрос записан")

    def test_greeting_is_free(self):
        with mock.patch.object(guest, "answer", self._engine(_MetaAns)):
            self.ask("Привет")
        self.assertEqual(self.charged(), 0)

    def test_answered_repeat_is_free_even_when_exhausted(self):
        """Фолбэк фронта после стрима, дошедшего до конца на сервере: тот же вопрос в той же беседе."""
        for i in range(3):
            self.ask(f"Вопрос {i}", sid=f"s{i}")
        r = self.ask("Вопрос 2", sid="s2")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.charged(), 3)
        self.assertEqual(self.ask("Вопрос 2", sid="s7").status_code, 402, "повтор в ДРУГОЙ беседе — не повтор")

    def test_daily_cap_for_all_guests(self):
        with self.Session() as db:
            for i in range(2):
                q.log_guest_message(db, guest_id=f"{i:032x}", session_id="x", role="assistant",
                                    content="ответ", charged=True)
            q.log_guest_message(db, guest_id="f" * 32, session_id="y", role="assistant", content="вчера",
                                charged=True)
            db.query(GuestMessage).filter(GuestMessage.content == "вчера").update(
                {"ts": plans.anchor_from_date(plans.msk_today()) - timedelta(seconds=1)})
            db.commit()
        with mock.patch.object(settings, "GUEST_DAILY_TOTAL", 2):
            r = self.ask()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["detail"], guest.DAILY_OVER)
        with mock.patch.object(settings, "GUEST_DAILY_TOTAL", 3):
            self.assertEqual(self.ask().status_code, 200, "вчерашний ответ засчитан в сегодняшний потолок")

    def test_ip_cap_survives_a_new_cookie(self):
        from fastapi.testclient import TestClient

        from main import app

        with mock.patch.object(guest, "_ip_limit", SlidingWindow(2, window=86400.0)):
            self.assertEqual(self.ask().status_code, 200)
            other = TestClient(app)
            other.get("/chat")                                   # «очистил cookies» — новый гость
            self.assertEqual(self.ask("Ещё", sid="o1", client=other).status_code, 200)
            r = self.ask("И ещё", sid="o2", client=other)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["detail"], guest.IP_OVER)

    def test_no_cookie_no_answer(self):
        from fastapi.testclient import TestClient

        from main import app

        r = TestClient(app).post("/api/guest/chat", json={"message": "Вопрос"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_sensitive_question_rejected_before_counting(self):
        r = self.ask("Мой паспорт 4510 123456, что делать?")
        self.assertEqual(r.status_code, 422)
        self.assertEqual((self.calls, self.rows()), ([], []))


class TestGuestAnswer(_GuestBase):
    def test_cheaper_engine_call_and_no_source_text(self):
        r = self.ask().json()
        self.assertEqual(self.calls[0]["limit"], settings.GUEST_CONTEXT_LIMIT)
        self.assertLess(settings.GUEST_CONTEXT_LIMIT, 8, "контекст гостя не короче пользовательского")
        self.assertIs(self.calls[0]["brief"], True)
        src = r["sources"][0]
        self.assertEqual(src["product_name"], "п. 7 Правил")
        self.assertIsNone(src["text"], "гость получил полный текст пункта")
        self.assertTrue(src["url"], "ссылка на первоисточник у гостя остаётся")
        self.assertIsNone(r["message_id"], "оценивать ответ гость не может")

    def test_stored_apart_and_without_ip(self):
        self.ask()
        self.assertEqual(self.rows(Message), [], "гостевая реплика попала в журнал пользователей")
        rows = self.rows()
        self.assertEqual([m.role for m in rows], ["user", "assistant"])
        self.assertTrue(rows[1].charged)
        self.assertIsNone(json.loads(rows[1].sources_json)[0]["text"])
        cols = {c["name"] for c in inspect(self.engine).get_columns("guest_messages")}
        self.assertFalse({"ip", "ip_address", "client_ip", "user_id"} & cols, cols)
        dump = " ".join(str(getattr(m, c)) for m in rows for c in cols)
        self.assertNotIn("testclient", dump, "адрес клиента записан в базу")

    def test_multiturn_is_scoped_to_the_guest(self):
        from fastapi.testclient import TestClient

        from main import app

        self.ask("Первый", sid="shared")
        self.ask("Второй", sid="shared")
        self.assertEqual([h["content"] for h in self.calls[1]["history"]], ["Первый", "Ответ движка"])
        other = TestClient(app)
        other.get("/chat")
        self.ask("Чужой", sid="shared", client=other)
        self.assertEqual(self.calls[2]["history"], [], "чужая беседа подтянулась по session_id")

    def test_stream_reports_trial_and_charges(self):
        with self.client.stream("POST", "/api/guest/chat/stream",
                                json={"message": "Вопрос", "session_id": "st"}) as r:
            body = "".join(r.iter_text())
        events = [json.loads(x[6:]) for x in body.split("\n\n") if x.startswith("data: ")]
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["trial"]["remaining"], 2)
        self.assertIsNone(done["sources"][0]["text"])
        self.assertIs(self.calls[0]["brief"], True)
        self.assertEqual(self.charged(), 1)

    def test_stream_blocked_before_start(self):
        for i in range(3):
            self.ask(f"Вопрос {i}", sid=f"s{i}")
        r = self.client.post("/api/guest/chat/stream", json={"message": "Ещё", "session_id": "z"})
        self.assertEqual(r.status_code, 402)


class TestBriefGeneration(unittest.TestCase):
    """Дешёвый ответ — флаг `brief` финальной генерации: краткость в системном промпте, потолок
    токенов, честная пометка об обрыве. Без флага вызов модели тот же, что и до правки."""

    def _plan(self):
        return pipeline._Plan(messages=[{"role": "system", "content": "СИСТЕМА"},
                                        {"role": "user", "content": "вопрос"}],
                              grounding="", hits=[], cases=[], low_relevance=False)

    def _client(self, finish, seen):
        def create(**kw):
            seen.append(kw)
            msg = SimpleNamespace(content="Ответ без чисел", )
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)],
                                   usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2))
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def _answer(self, brief, finish):
        seen, plan = [], self._plan()
        with mock.patch.object(pipeline, "_plan_answer", return_value=plan), \
             mock.patch.object(pipeline, "_client", return_value=self._client(finish, seen)):
            ans = pipeline.answer("вопрос", brief=brief)
        return ans, seen[0], plan

    def test_default_call_unchanged(self):
        ans, kw, plan = self._answer(False, "length")
        self.assertIs(kw["messages"], plan.messages)
        self.assertNotIn("max_tokens", kw)
        self.assertNotIn("сокращён", ans.text, "пометка пробного режима у пользователя")

    def test_brief_call(self):
        ans, kw, plan = self._answer(True, "stop")
        self.assertEqual(kw["max_tokens"], settings.GUEST_MAX_TOKENS)
        self.assertTrue(kw["messages"][0]["content"].endswith(pipeline.BRIEF_NOTE))
        self.assertEqual(plan.messages[0]["content"], "СИСТЕМА", "план генерации испорчен")
        self.assertEqual(kw["messages"][1:], plan.messages[1:])
        self.assertNotIn("сокращён", ans.text)
        cut, _, _ = self._answer(True, "length")
        self.assertTrue(cut.text.endswith(pipeline.BRIEF_CUT_NOTE), "обрыв по потолку не помечен")

    def test_stream_marks_the_cut(self):
        seen = []

        def create(**kw):
            seen.append(kw)
            chunk = lambda text, fin=None: SimpleNamespace(  # noqa: E731
                usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=fin)])
            return iter([chunk("Начало ответа"), chunk("", "length")])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(pipeline, "_plan_answer", return_value=self._plan()), \
             mock.patch.object(pipeline, "_client", return_value=client):
            out = list(pipeline.answer_stream("вопрос", brief=True))
        self.assertEqual(seen[0]["max_tokens"], settings.GUEST_MAX_TOKENS)
        self.assertTrue(out[-1][1].text.endswith(pipeline.BRIEF_CUT_NOTE))
        self.assertEqual(out[-2], ("delta", "…" + pipeline.BRIEF_CUT_NOTE), "пометка не дошла до экрана")


class TestGuestAdminAndPolicy(_GuestBase):
    def _request(self):
        from starlette.requests import Request

        return Request({"type": "http", "method": "GET", "path": "/admin", "headers": [],
                        "query_string": b"", "session": {}, "app": None})

    def test_admin_sees_guest_questions_and_old_ones_purged(self):
        self.ask("Что такое акт экспертизы?")
        with self.Session() as db:
            q.log_guest_message(db, guest_id="a" * 32, session_id="old", role="user", content="Древний вопрос")
            db.query(GuestMessage).filter(GuestMessage.content == "Древний вопрос").update(
                {"ts": datetime.now() - timedelta(days=q.GUEST_RETENTION_DAYS + 1)})
            db.commit()
        admin = mock.Mock(id=99, role="admin", username="adm")
        with mock.patch.object(admin_mod, "current_user", return_value=admin):
            html = admin_mod.admin_trial(self._request()).body.decode("utf-8")   # раздел /admin/trial
        panel = html[html.index('id="tab-guests"'):]
        self.assertIn("Что такое акт экспертизы?", panel)
        self.assertIn("Ответ движка", panel)
        self.assertIn("<b>1 из 300</b>", panel)
        self.assertNotIn("Древний вопрос", panel)
        self.assertNotIn("Древний вопрос", [m.content for m in self.rows()], "срок хранения не исполняется")

    def test_policy_describes_the_trial(self):
        policy = self.client.get("/privacy").text
        section = policy[policy.index("Пробный режим без входа"):]
        self.assertIn(trial.GUEST_COOKIE, section)
        self.assertIn("IP-адрес", section)
        self.assertIn("не дольше 6 месяцев", section)
        self.assertTrue(180 <= q.GUEST_RETENTION_DAYS <= 184, "срок в коде разошёлся с политикой")


class TestGuestFrontContracts(unittest.TestCase):
    """Фронт: гостевые ручки, отказы пробного режима, ни одного обращения к ручкам пользователя."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app" / "web" / "static" / "chat.js").read_text(encoding="utf-8")

    def test_endpoints_and_rejections(self):
        self.assertIn('const STREAM_URL = GUEST ? "/api/guest/chat/stream" : "/api/chat/stream"', self.js)
        self.assertIn("fetch(STREAM_URL", self.js)
        self.assertIn("fetch(CHAT_URL", self.js)
        self.assertNotIn('fetch("/api/chat', self.js, "ручка пользователя зашита мимо GUEST")
        self.assertEqual(self.js.count("GUEST && await guestRejected(r, text, pending)"), 2,
                         "отказ пробного режима разобран не на обоих путях (стрим и фолбэк)")
        self.assertIn('window.location = "/login?trial_over=1"', self.js)

    def test_user_only_calls_are_guarded(self):
        self.assertIn("if (!GUEST) loadConversations();", self.js)
        self.assertIn("if (!GUEST) refreshQuota();", self.js)
        self.assertIn('if (fbOpen) fbOpen.addEventListener', self.js, "без кнопки отзыва скрипт падал бы")


if __name__ == "__main__":
    unittest.main()
