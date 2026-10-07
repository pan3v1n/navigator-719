"""#160: лимиты запросов по тарифам — период от даты подключения, блокировка по исчерпании.

Решения владельца 07.10.2026: период — месяц от даты подключения; по исчерпании запросы
блокируются. Списывается ответ движка; приветствия (meta) и сбои — нет.

Что закрепляем:
* период считается от даты подключения каждый раз заново — прижатие конца месяца не копится;
* граница периода — 00:00 МСК: секунда до неё ещё старый период, сама граница — новый;
* на последней единице вопрос проходит, на исчерпанном — 402 ДО движка, вопрос не пишется;
* стрим блокируется тем же 402 до старта потока;
* удаление бесед расход не обнуляет (расход — отдельная таблица);
* admin не ограничен никогда, пользователь без тарифа — тоже;
* `/navigate` закрыт тарифному пользователю — иначе через него лимит обходится целиком;
* сбой счёта не блокирует клиента.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api import chat as chat_mod  # noqa: E402
from app.api import quota  # noqa: E402
from app.api.ratelimit import SlidingWindow  # noqa: E402
from app.core import plans  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import AnswerUsage, Base  # noqa: E402

MSK = plans.MSK_OFFSET


def msk(y, m, d, hh=0, mm=0, ss=0) -> datetime:
    """Московское время → наивное UTC (в такой форме время лежит в БД)."""
    return datetime(y, m, d, hh, mm, ss) - MSK


class TestPeriodBounds(unittest.TestCase):
    def test_month_from_connection_date(self):
        a = plans.anchor_from_date(date(2026, 10, 7))
        self.assertEqual(a, msk(2026, 10, 7))
        self.assertEqual(plans.period_bounds(a, msk(2026, 10, 20, 15)), (msk(2026, 10, 7), msk(2026, 11, 7)))

    def test_boundary_is_midnight_moscow(self):
        a = plans.anchor_from_date(date(2026, 10, 7))
        self.assertEqual(plans.period_bounds(a, msk(2026, 11, 6, 23, 59, 59))[0], msk(2026, 10, 7))
        self.assertEqual(plans.period_bounds(a, msk(2026, 11, 7))[0], msk(2026, 11, 7))

    def test_month_end_clamps_without_drift(self):
        a = plans.anchor_from_date(date(2028, 1, 31))
        self.assertEqual(plans.period_bounds(a, msk(2028, 3, 15)), (msk(2028, 2, 29), msk(2028, 3, 31)))
        # третий период — 31.03, а не 29.03: считаем от даты подключения, а не от прошлого периода
        self.assertEqual(plans.period_bounds(a, msk(2028, 4, 15))[0], msk(2028, 3, 31))

    def test_year_rollover(self):
        a = plans.anchor_from_date(date(2026, 12, 15))
        self.assertEqual(plans.period_bounds(a, msk(2027, 1, 20)), (msk(2027, 1, 15), msk(2027, 2, 15)))

    def test_connection_in_the_future_starts_the_first_period_there(self):
        a = plans.anchor_from_date(date(2026, 12, 1))
        self.assertEqual(plans.period_bounds(a, msk(2026, 10, 7)), (msk(2026, 12, 1), msk(2027, 1, 1)))


class TestPlanLimit(unittest.TestCase):
    def _u(self, role, plan):
        return mock.Mock(role=role, plan=plan)

    def test_limits(self):
        self.assertEqual(plans.plan_limit(self._u("user", "Старт")), 100)
        self.assertEqual(plans.plan_limit(self._u("expert", "Профи")), 400)
        self.assertIsNone(plans.plan_limit(self._u("user", None)))
        self.assertIsNone(plans.plan_limit(self._u("user", "Для организаций")))
        self.assertIsNone(plans.plan_limit(self._u("admin", "Старт")), "admin не ограничен никогда")


class _Ans:
    text = "Ответ движка"
    hits = []
    cases = []
    rule_sources = []
    low_relevance = False
    unverified_numbers = []
    prompt_tokens = 10
    completion_tokens = 5
    input_hint = ""
    metered = True


class _MetaAns(_Ans):
    text = "Здравствуйте!"
    metered = False


class TestQuotaInChat(unittest.TestCase):
    """Подключение — СЕГОДНЯ по Москве: эндпоинт списывает ответ настоящим временем, и период
    обязан его содержать в любой день прогона (зашитая дата покраснела бы через месяц)."""

    def setUp(self):
        self.NOW_DAY = plans.msk_today()
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        patches = [
            mock.patch.object(chat_mod, "get_session", self.Session),
            mock.patch.object(chat_mod, "_chat_limit", SlidingWindow(limit=10_000, window=60.0)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.calls = 0

    def _user(self, role="user", plan="Старт", started=None):
        with self.Session() as db:
            u = q.create_user(db, f"{role}{plan}{started}", "h", role=role)
            if plan:
                q.set_user_plan(db, u.id, plan, plans.anchor_from_date(started or self.NOW_DAY))
            u = q.get_user(db, u.id)
            db.expunge(u)
            return u

    def _spend(self, user, n, at=None):
        with self.Session() as db:
            ts = at or (plans.anchor_from_date(self.NOW_DAY) + timedelta(minutes=1))
            db.add_all([AnswerUsage(user_id=user.id, ts=ts) for _ in range(n)])
            db.commit()

    def _used(self, user):
        with self.Session() as db:
            return len(db.execute(select(AnswerUsage).where(AnswerUsage.user_id == user.id)).all())

    def _engine(self, ans=_Ans):
        def fake(*a, **k):
            self.calls += 1
            return ans()
        return fake

    def _ask(self, user, ans=_Ans, sid="s1"):
        with mock.patch.object(chat_mod, "answer", self._engine(ans)):
            return chat_mod.chat(chat_mod.ChatRequest(message="Требования к станкам?", session_id=sid),
                                 user=user)

    def test_last_unit_passes_then_blocked_before_the_engine(self):
        u = self._user()
        self._spend(u, 99)
        self._ask(u)
        self.assertEqual(self._used(u), 100, "ответ на последней единице обязан списаться")
        with self.assertRaises(HTTPException) as cm:
            self._ask(u)
        self.assertEqual(cm.exception.status_code, 402)
        self.assertIn("«Старт»", cm.exception.detail)
        self.assertIn("100 из 100", cm.exception.detail)
        nxt = plans.add_months(datetime(*self.NOW_DAY.timetuple()[:3]), 1).date()
        self.assertIn(f"{nxt:%d.%m.%Y}", cm.exception.detail, "дата начала нового периода")
        self.assertEqual(self.calls, 1, "на исчерпанном тарифе движок не зовётся")
        with self.Session() as db:
            questions = [m for m in q.get_messages_for_user(db, u.id) if m.role == "user"]
        self.assertEqual(len(questions), 1, "отклонённый по тарифу вопрос в беседу не пишется")

    def test_stream_is_blocked_before_the_stream_starts(self):
        u = self._user()
        self._spend(u, 100)
        with self.assertRaises(HTTPException) as cm:
            chat_mod.chat_stream(chat_mod.ChatRequest(message="Вопрос"), user=u)
        self.assertEqual(cm.exception.status_code, 402)

    def test_stream_spends_on_done(self):
        u = self._user()
        ans = _Ans()

        def fake_stream(*a, **k):
            yield "delta", ans.text
            yield "done", ans

        async def drain(resp):   # тот же приём, что в test_followup: тело стрима — async-итератор
            return [chunk async for chunk in resp.body_iterator]

        with mock.patch.object(chat_mod, "answer_stream", fake_stream):
            asyncio.run(drain(chat_mod.chat_stream(chat_mod.ChatRequest(message="Вопрос"), user=u)))
        self.assertEqual(self._used(u), 1)

    def test_greeting_is_free(self):
        u = self._user()
        self._ask(u, ans=_MetaAns)
        self.assertEqual(self._used(u), 0)

    def test_engine_failure_is_not_spent(self):
        u = self._user()

        def boom(*a, **k):
            raise RuntimeError("DeepSeek недоступен")

        with mock.patch.object(chat_mod, "answer", boom), self.assertRaises(HTTPException):
            chat_mod.chat(chat_mod.ChatRequest(message="Вопрос", session_id="s9"), user=u)
        self.assertEqual(self._used(u), 0)

    def test_previous_period_does_not_count(self):
        u = self._user()
        self._spend(u, 100, at=plans.anchor_from_date(self.NOW_DAY) - timedelta(seconds=1))  # до подключения
        self._spend(u, 99)
        self._ask(u)   # в периоде 99 — проходит
        self.assertEqual(self.calls, 1)

    def test_deleting_conversations_does_not_reset_usage(self):
        u = self._user()
        self._ask(u, sid="del")
        with self.Session() as db:
            q.delete_session(db, u.id, "del")
        self.assertEqual(self._used(u), 1)

    def test_admin_and_no_plan_are_never_blocked(self):
        for u in (self._user(role="admin"), self._user(plan=None)):
            self._spend(u, 500)
            self._ask(u)
        self.assertEqual(self.calls, 2)

    def test_counting_failure_does_not_block_the_client(self):
        u = self._user()
        self._spend(u, 100)
        with mock.patch.object(quota, "quota_state", side_effect=RuntimeError("БД")):
            self._ask(u)
        self.assertEqual(self.calls, 1)


class TestPipelineMarksFreeAnswers(unittest.TestCase):
    """Бесплатность решает ПАЙПЛАЙН — эндпоинт лишь читает флаг (тесты выше берут подделку)."""

    def test_greeting_is_not_metered_but_an_answer_is(self):
        from app.rag import pipeline

        self.assertFalse(pipeline.answer("Привет").metered)
        self.assertFalse(pipeline.answer("Спасибо").metered)
        self.assertTrue(pipeline.Answer(text="x", hits=[]).metered, "по умолчанию ответ списывается")


class TestChatJsHandles402(unittest.TestCase):
    """Без обработки 402 исчерпанный тариф выглядел бы как «сервис недоступен» — стрим не стартует,
    фолбэк получает тот же 402 и печатает общую ошибку вместо причины и даты нового периода."""

    def test_both_send_paths_show_the_reason(self):
        js = (ROOT / "app" / "web" / "static" / "chat.js").read_text(encoding="utf-8")
        for fn in ("async function askStream(", "async function askFallback("):
            start = js.index(fn)
            body = js[start:js.index("\nasync function", start + 1) if "\nasync function" in js[start + 1:]
                      else len(js)]
            branch = body[body.index("r.status === 402"):]
            self.assertIn("handleRejected(r, text, pending)", branch[:200], fn)


class TestNavigateClosedForPlans(unittest.TestCase):
    def test_plan_user_cannot_bypass_through_navigate(self):
        from app.api import routes
        from app.api.schemas import NavigateRequest

        called = []
        with mock.patch.object(routes, "navigate", side_effect=lambda *a, **k: called.append(1)):
            with self.assertRaises(HTTPException) as cm:
                routes.navigate_endpoint(NavigateRequest(query="станки"),
                                         user=mock.Mock(role="user", plan="Старт"))
        self.assertEqual(cm.exception.status_code, 403)
        self.assertEqual(called, [], "движок не зовётся")

    def test_no_plan_user_still_reaches_navigate(self):
        from app.api import routes
        from app.api.schemas import NavigateRequest

        with mock.patch.object(routes, "navigate", side_effect=RuntimeError("дошёл до движка")):
            with self.assertRaises(HTTPException) as cm:
                routes.navigate_endpoint(NavigateRequest(query="станки"),
                                         user=mock.Mock(role="expert", plan=None))
        self.assertEqual(cm.exception.status_code, 500)


class TestAdminSetsPlan(unittest.TestCase):
    def setUp(self):
        from app.api import web

        self.web = web
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        p = mock.patch.object(web, "get_session", self.Session)
        p.start()
        self.addCleanup(p.stop)
        with self.Session() as db:
            self.uid = q.create_user(db, "client1", "h", role="user").id
        self.admin = mock.Mock(id=1)

    def _set(self, plan, started=""):
        return self.web.admin_set_plan(self.uid, plan=plan, started=started, admin=self.admin)

    def _row(self):
        with self.Session() as db:
            return q.get_user(db, self.uid)

    def test_assign_and_clear(self):
        r = self._set("Стандарт", "2026-10-01")
        self.assertEqual(r.status_code, 303)
        u = self._row()
        self.assertEqual((u.plan, u.plan_started_at), ("Стандарт", msk(2026, 10, 1)))
        self._set("")
        u = self._row()
        self.assertEqual((u.plan, u.plan_started_at), (None, None))

    def test_default_date_is_today_in_moscow(self):
        self._set("Старт")
        self.assertEqual(plans.msk_date(self._row().plan_started_at), plans.msk_today())

    def test_rejects_bad_input(self):
        tomorrow = (plans.msk_today() + timedelta(days=1)).isoformat()
        for plan, started in (("Безлимит", ""), ("Старт", "07.10.2026"), ("Старт", tomorrow)):
            with self.subTest(plan=plan, started=started), self.assertRaises(HTTPException) as cm:
                self._set(plan, started)
            self.assertEqual(cm.exception.status_code, 422)
        self.assertIsNone(self._row().plan)

    def test_missing_user_is_404(self):
        with self.assertRaises(HTTPException) as cm:
            self.web.admin_set_plan(999, plan="Старт", started="", admin=self.admin)
        self.assertEqual(cm.exception.status_code, 404)

    def _request(self, path):
        from starlette.requests import Request

        return Request({"type": "http", "method": "GET", "path": path, "headers": [],
                        "query_string": b"", "session": {}, "app": None})

    def test_admin_page_and_profile_show_usage(self):
        self._set("Старт")
        with self.Session() as db:
            db.add_all([AnswerUsage(user_id=self.uid) for _ in range(37)])
            db.commit()
            client = q.get_user(db, self.uid)
            db.expunge(client)
        admin = mock.Mock(id=99, role="admin", username="adm")
        with mock.patch.object(self.web, "current_user", return_value=admin), \
             mock.patch.object(self.web, "system_health", return_value={}):
            html = self.web.admin_page(self._request("/admin")).body.decode("utf-8")
        self.assertIn('data-tab="plans"', html)
        panel = html[html.index('id="tab-plans"'):]
        self.assertIn("<b>37 из 100</b>", panel)
        self.assertIn('<option value="Старт" selected>', panel)
        self.assertIn(f'value="{plans.msk_today().isoformat()}"', panel, "дата подключения в форме")
        with mock.patch.object(self.web, "current_user", return_value=client):
            page = self.web.profile_page(self._request("/profile")).body.decode("utf-8")
        self.assertIn("Тариф «Старт»: использовано 37 из 100 запросов", page)
        self.assertNotIn("(http)", page, "сервис с test26 на HTTPS — прежняя пометка была ложной")

    def test_route_requires_admin(self):
        from fastapi.testclient import TestClient

        from main import app

        r = TestClient(app).post(f"/api/admin/users/{self.uid}/plan", data={"plan": "Профи"},
                                 follow_redirects=False)
        self.assertIn(r.status_code, (401, 403), "назначение тарифа доступно без входа")


class TestPlanColumnsMigrate(unittest.TestCase):
    def test_old_users_table_gets_plan_columns(self):
        import tempfile

        from app.db import engine as engine_mod

        with tempfile.TemporaryDirectory() as tmp:
            old = create_engine(f"sqlite:///{tmp}/old.db")
            with old.begin() as c:
                c.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64))")
                c.exec_driver_sql("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
                c.exec_driver_sql("CREATE TABLE feedback (id INTEGER PRIMARY KEY)")
            with mock.patch.object(engine_mod, "engine", old):
                engine_mod._ensure_columns()
            with old.connect() as c:
                cols = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(users)")}
            old.dispose()
        self.assertTrue({"plan", "plan_started_at"} <= cols, cols)


if __name__ == "__main__":
    unittest.main()
