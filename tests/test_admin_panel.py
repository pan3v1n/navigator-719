"""Админ-панель `/admin` — пересборка 09.10.2026 (решения владельца): разделы, «Пользователи» с
карточкой и действиями, и то, на чём держатся сброс пароля и блокировка, — эпоха входа.

Что закрепляем:
* сброс пароля и блокировка гасят УЖЕ ОТКРЫТЫЕ сессии и куку «запомнить меня» (а не ждут, пока
  человек выйдет сам); старая кука с голым user_id живёт до первого сброса;
* заблокированный не входит, причина называется только после верного пароля;
* список: поиск и фильтры; карточка: роль, тариф, организация, сброс, блокировка, удаление;
* admin не трогает себя и не оставляет сервис без действующего администратора;
* панель и действия — только admin; ссылки «Логи диалогов» в чате нет.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api import admin as adm  # noqa: E402
from app.api import auth, web  # noqa: E402
from app.api.auth import hash_password, make_remember_token, verify_password  # noqa: E402
from app.core import plans  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import AnswerUsage, Base, Message  # noqa: E402

PW = "pw-Secret-123"


class _PanelDB(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from main import app

        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        for mod in (auth, web, adm):
            p = mock.patch.object(mod, "get_session", self.Session)
            p.start()
            self.addCleanup(p.stop)
        self.app = app
        self.TestClient = TestClient
        self.admin = self._mk("boss", role="admin")
        self.as_admin = self._login("boss")

    def _mk(self, login, role="user", **profile):
        with self.Session() as db:
            u = q.create_user(db, login, hash_password(PW), role=role)
            if profile:
                q.update_profile(db, u.id, **{"full_name": "", "region": "", "telegram": "", "consent": False,
                                              **profile})
            return q.get_user(db, u.id)

    def _login(self, login, remember=False):
        c = self.TestClient(self.app)
        r = c.post("/login", data={"username": login, "password": PW, **({"remember": "1"} if remember else {})},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 302, r.text[:200])
        return c

    def _fresh(self, uid):
        with self.Session() as db:
            return q.get_user(db, uid)

    def _signed_in(self, client) -> bool:
        """Сессия жива: /profile открывается, а не уводит на вход."""
        return client.get("/profile", follow_redirects=False).status_code == 200


class TestEpochAndBlocking(_PanelDB):
    def test_password_reset_ends_open_sessions_and_remember_cookies(self):
        u = self._mk("kursk.expert1")
        browser = self._login("kursk.expert1", remember=True)
        self.assertTrue(self._signed_in(browser))
        r = self.as_admin.post(f"/api/admin/users/{u.id}/password")
        self.assertEqual(r.status_code, 200)
        new_pw = r.text.split('id="new-password">')[1].split("<")[0]
        self.assertTrue(verify_password(new_pw, self._fresh(u.id).password_hash))
        self.assertFalse(self._signed_in(browser), "сессия пережила сброс пароля")
        browser.cookies.pop("session", None)               # остаётся только «запомнить меня»
        self.assertFalse(self._signed_in(browser), "кука «запомнить меня» пережила сброс пароля")
        self.assertEqual(self._fresh(u.id).auth_epoch, 1)

    def test_old_remember_cookie_lives_until_first_reset(self):
        u = self._mk("kursk.expert2")
        c = self.TestClient(self.app)
        c.cookies.set(auth.REMEMBER_COOKIE, auth._remember.dumps(u.id))      # формат до 09.10.2026
        self.assertTrue(self._signed_in(c), "старая кука перестала работать без сброса")
        with self.Session() as db:
            q.reset_password(db, u.id, hash_password(PW))
        c2 = self.TestClient(self.app)
        c2.cookies.set(auth.REMEMBER_COOKIE, auth._remember.dumps(u.id))
        self.assertFalse(self._signed_in(c2), "старая кука пережила сброс пароля")
        c3 = self.TestClient(self.app)
        c3.cookies.set(auth.REMEMBER_COOKIE, make_remember_token(u.id, 1))
        self.assertTrue(self._signed_in(c3), "кука с актуальной эпохой не работает")

    def test_block_ends_sessions_and_login_names_the_reason_only_after_password(self):
        u = self._mk("kursk.expert3")
        browser = self._login("kursk.expert3")
        # второй браузер во время блокировки молчит: его сессию не очистит запрос заблокированного
        idle = self._login("kursk.expert3")
        r = self.as_admin.post(f"/api/admin/users/{u.id}/block", data={"blocked": "1"}, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, f"/admin/users/{u.id}?ok=blocked"))
        self.assertFalse(self._signed_in(browser), "заблокированный остался в сессии")
        c = self.TestClient(self.app)
        bad = c.post("/login", data={"username": "kursk.expert3", "password": "wrong"}, follow_redirects=False)
        self.assertEqual(bad.status_code, 401)
        self.assertNotIn("заблокирована", bad.text, "блокировка видна без пароля — подсказка о логине")
        ok = c.post("/login", data={"username": "kursk.expert3", "password": PW}, follow_redirects=False)
        self.assertEqual(ok.status_code, 403)
        self.assertIn("Учётная запись заблокирована", ok.text)
        self.as_admin.post(f"/api/admin/users/{u.id}/block", data={"blocked": "0"})
        # Ось «эпоха»: разблокировка не воскрешает сессию, открытую до блокировки, — войти заново
        # человек должен сам. Держит ТОЛЬКО подъём эпохи при блокировке (отметка уже снята).
        self.assertFalse(self._signed_in(idle), "разблокировка воскресила сессию, открытую до блокировки")
        self.assertTrue(self._signed_in(self._login("kursk.expert3")), "после разблокировки вход закрыт")

    def test_block_mark_alone_closes_the_session(self):
        """Ось «отметка»: блокировка, поставленная в базе в обход панели (эпоха та же), всё равно
        закрывает уже открытую сессию — проверка идёт на каждом запросе."""
        from app.db.models import User, _utcnow

        u = self._mk("kursk.expert4")
        browser = self._login("kursk.expert4")
        with self.Session() as db:
            db.get(User, u.id).blocked_at = _utcnow()
            db.commit()
        self.assertFalse(self._signed_in(browser), "сессия заблокированного жива")

    def test_lapsed_login_goes_to_login_not_to_the_trial(self):
        """Ревью PR #182: `current_user` чистит погасшую сессию, и `/chat` открывал пробный режим —
        заблокированный спрашивал гостем, эксперт после сброса пароля молча получал урезанные ответы."""
        def chat(client):
            r = client.get("/chat", follow_redirects=False)
            return r.status_code, r.headers.get("location")

        u = self._mk("kursk.expert5", role="expert")
        blocked = self._login("kursk.expert5")
        remembered = self._login("kursk.expert5", remember=True)
        self.as_admin.post(f"/api/admin/users/{u.id}/block", data={"blocked": "1"})
        self.assertEqual(chat(blocked), (302, "/login"), "заблокированный попал в пробный режим")
        remembered.cookies.pop("session", None)                 # осталась только «запомнить меня»
        self.assertEqual(chat(remembered), (302, "/login"))
        self.assertEqual(chat(self.TestClient(self.app))[0], 200, "аноним без входа — пробный режим")
        self.assertEqual(chat(blocked), (302, "/login"), "со второго открытия заблокированный — в пробе")
        blocked.get("/logout")
        self.assertEqual(chat(blocked)[0], 200, "после выхода — обычный аноним, проба открыта")


class TestUsersList(_PanelDB):
    def setUp(self):
        super().setUp()
        self.a = self._mk("kursk.expert1", full_name="Иван Петров", region="Курская область",
                          telegram="", consent=True, email="i.petrov@tpp.ru", phone="")
        self.b = self._mk("perm.expert1", role="expert")
        self.c = self._mk("orel.expert1")
        with self.Session() as db:
            q.set_user_org(db, self.a.id, "Союз «Курская ТПП»", "4632000000")
            q.set_blocked(db, self.b.id, True)
            q.set_user_plan(db, self.c.id, "Старт", plans.anchor_from_date(plans.msk_today()),
                            plans.anchor_from_date(plans.msk_today()))          # уже истёк
            q.log_message(db, user_id=self.c.id, session_id="s", role="user", content="вопрос")

    def _logins(self, query=""):
        html = self.as_admin.get("/admin/users" + query).text
        return [x for x in ("kursk.expert1", "perm.expert1", "orel.expert1", "boss") if f">{x}</a>" in html]

    def test_search_and_filters(self):
        for query, expect in (("?q=петров", ["kursk.expert1"]),
                              ("?q=4632000000", ["kursk.expert1"]),
                              ("?q=tpp.ru", ["kursk.expert1"]),
                              ("?q=курская тпп", ["kursk.expert1"]),
                              ("?role=expert", ["perm.expert1"]),
                              ("?status=blocked", ["perm.expert1"]),
                              ("?status=no_profile", ["orel.expert1"]),
                              ("?status=expired", ["orel.expert1"]),
                              ("?plan=Старт", ["orel.expert1"])):
            with self.subTest(query=query):
                self.assertEqual(self._logins(query), expect)
        self.assertEqual(len(self._logins()), 4)

    def test_no_profile_follows_the_chat_gate(self):
        """Ревью PR #182: «профиль не заполнен» в фильтре и в Сводке был копией правила гейта —
        следующая правка гейта развела бы их. Меняем гейт — фильтр и счётчик идут за ним."""
        from app.api import admin_summary

        with mock.patch.object(auth, "profile_complete", lambda user: False):
            self.assertEqual(self._logins("?status=no_profile"), ["kursk.expert1", "orel.expert1"])
            with self.Session() as db:
                self.assertEqual(admin_summary.summary_view(db)["accounts"]["no_profile"], 2)

    def test_recent_activity_first_and_badges(self):
        html = self.as_admin.get("/admin/users").text
        self.assertLess(html.index(">orel.expert1</a>"), html.index(">kursk.expert1</a>"), "активные — сверху")
        row = html[html.index(">perm.expert1</a>"):]
        self.assertIn("заблокирована", row[:400])
        self.assertIn("истёк", html[html.index(">orel.expert1</a>"):][:900])


class TestCardActions(_PanelDB):
    def test_role_change_and_self_protection(self):
        u = self._mk("kursk.expert1")
        r = self.as_admin.post(f"/api/admin/users/{u.id}/role", data={"role": "expert"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], f"/admin/users/{u.id}?ok=role")
        self.assertEqual(self._fresh(u.id).role, "expert")
        self.assertIn("Роль изменена.", self.as_admin.get(f"/admin/users/{u.id}?ok=role").text)
        for path, data in ((f"/api/admin/users/{self.admin.id}/role", {"role": "user"}),
                           (f"/api/admin/users/{self.admin.id}/block", {"blocked": "1"}),
                           (f"/api/admin/users/{self.admin.id}/delete", {"confirm": "boss"})):
            with self.subTest(path=path):
                r = self.as_admin.post(path, data=data)
                self.assertEqual(r.status_code, 400)
                self.assertIn("собственную учётку", r.text)
        boss = self._fresh(self.admin.id)
        self.assertEqual((boss.role, boss.blocked_at), ("admin", None))

    def test_last_active_admin_is_kept(self):
        """Прямой вызов от имени admin'а, которого нет в базе: проверка «последнего» — независимо
        от проверки «себя»."""
        from starlette.requests import Request

        outsider = mock.Mock(id=999, role="admin")
        req = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"",
                       "session": {}, "app": None})
        for call in (lambda: adm.admin_set_role(req, self.admin.id, role="user", admin=outsider),
                     lambda: adm.admin_block(req, self.admin.id, blocked="1", admin=outsider),
                     lambda: adm.admin_delete_user(req, self.admin.id, confirm="boss", admin=outsider)):
            r = call()
            self.assertEqual(r.status_code, 400)
            self.assertIn("последнего действующего администратора", r.body.decode("utf-8"))
        second = self._mk("boss2", role="admin")
        r = adm.admin_set_role(req, second.id, role="user", admin=outsider)
        self.assertEqual(r.status_code, 303, "второго администратора можно понизить")

    def test_delete_needs_exact_login_and_removes_everything(self):
        u = self._mk("kursk.expert9")
        with self.Session() as db:
            q.log_message(db, user_id=u.id, session_id="s", role="user", content="вопрос")
            q.record_answer_usage(db, u.id)
        r = self.as_admin.post(f"/api/admin/users/{u.id}/delete", data={"confirm": "kursk.expert"})
        self.assertEqual(r.status_code, 400)
        self.assertIsNotNone(self._fresh(u.id), "удалено без точного подтверждения")
        r = self.as_admin.post(f"/api/admin/users/{u.id}/delete", data={"confirm": "kursk.expert9"},
                               follow_redirects=False)
        self.assertEqual(r.headers["location"], "/admin/users?ok=deleted&login=kursk.expert9")
        self.assertIsNone(self._fresh(u.id))
        with self.Session() as db:
            self.assertEqual(db.execute(select(Message).where(Message.user_id == u.id)).all(), [])
            self.assertEqual(db.execute(select(AnswerUsage).where(AnswerUsage.user_id == u.id)).all(), [])
        self.assertIn("удалена вместе с профилем и диалогами",
                      self.as_admin.get("/admin/users?ok=deleted&login=kursk.expert9").text)

    def test_org_verification(self):
        """Вечер 09.10.2026: организацию вписывает пользователь, admin подтверждает («чтобы не было
        фейков»); вписанное самим admin'ом — сразу подтверждено; пустое подтвердить нельзя."""
        from app.api import admin_summary

        u = self._mk("kursk.expert7", full_name="Иван", region="Курская область", telegram="", consent=True)
        with self.Session() as db:
            q.update_profile(db, u.id, full_name="Иван", region="Курская область", telegram="", consent=True,
                             org="ООО «Станкозавод»", inn="7707083893")
        self.assertIsNone(self._fresh(u.id).org_verified_at)
        self.assertIn(">kursk.expert7</a>", self.as_admin.get("/admin/users?status=org_unverified").text)
        card = self.as_admin.get(f"/admin/users/{u.id}").text
        self.assertIn("не подтверждена", card)
        self.assertIn("Подтвердить организацию", card)
        with self.Session() as db:
            self.assertEqual(admin_summary.summary_view(db)["accounts"]["org_unverified"], 1)
        r = self.as_admin.post(f"/api/admin/users/{u.id}/org-verify", data={"verified": "1"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], f"/admin/users/{u.id}?ok=verified")
        u = self._fresh(u.id)
        self.assertEqual(u.org_verified_by, self.admin.id)
        self.assertIn("подтверждена</span>", self.as_admin.get(f"/admin/users/{u.id}").text)
        self.assertNotIn(">kursk.expert7</a>", self.as_admin.get("/admin/users?status=org_unverified").text)
        self.as_admin.post(f"/api/admin/users/{u.id}/org-verify", data={"verified": "0"})
        self.assertIsNone(self._fresh(u.id).org_verified_at)
        empty = self._mk("kursk.expert8")
        self.assertEqual(self.as_admin.post(f"/api/admin/users/{empty.id}/org-verify",
                                            data={"verified": "1"}).status_code, 422)
        self.as_admin.post(f"/api/admin/users/{empty.id}/org", data={"org": "АО «Прибор»", "inn": "7707083893"})
        self.assertEqual(self._fresh(empty.id).org_verified_by, self.admin.id, "вписанное admin'ом — не подтверждено")
        self.assertEqual(self.as_admin.post(f"/api/admin/users/{u.id}/org-verify", data={"verified": "1"},
                                            headers={"x": "1"}, cookies={}).status_code, 200)
        anon = self.TestClient(self.app)
        self.assertEqual(anon.post(f"/api/admin/users/{u.id}/org-verify", data={"verified": "0"}).status_code, 401)

    def test_plan_kind(self):
        u = self._mk("kursk.expert9")
        self.as_admin.post(f"/api/admin/users/{u.id}/plan", data={"plan": "Профи", "kind": "corporate"})
        self.assertEqual(self._fresh(u.id).plan_kind, "corporate")
        self.assertIn('<option value="corporate" selected>', self.as_admin.get(f"/admin/users/{u.id}").text)
        self.as_admin.post(f"/api/admin/users/{u.id}/plan", data={"plan": "Профи"})
        self.assertIsNone(self._fresh(u.id).plan_kind, "без вида — личный")
        self.as_admin.post(f"/api/admin/users/{u.id}/plan", data={"plan": "", "kind": "corporate"})
        self.assertIsNone(self._fresh(u.id).plan_kind, "снятый тариф оставил вид")

    def test_card_shows_profile_plan_org_and_activity(self):
        u = self._mk("kursk.expert5", full_name="Анна Смирнова", region="Курская область", telegram="",
                     consent=True, email="a@tpp.ru", phone="+7 900 000-00-00", position="Эксперт")
        with self.Session() as db:
            q.log_message(db, user_id=u.id, session_id="sid1", role="user", content="Требования к станкам?")
        html = self.as_admin.get(f"/admin/users/{u.id}").text
        for piece in ("Анна Смирнова", "a@tpp.ru", "+7 900 000-00-00", "Эксперт", "дано",
                      f'action="/api/admin/users/{u.id}/plan"', f'action="/api/admin/users/{u.id}/org"',
                      f'action="/api/admin/users/{u.id}/password"', f'action="/api/admin/users/{u.id}/delete"',
                      "/admin/dialogs/sid1", "Требования к станкам?"):
            self.assertIn(piece, html)
        own = self.as_admin.get(f"/admin/users/{self.admin.id}").text
        self.assertIn("Это ваша учётка", own)
        self.assertNotIn(f'action="/api/admin/users/{self.admin.id}/delete"', own)


class TestSummary(_PanelDB):
    """Сводка (этап 2): работает ли сервис и что требует внимания — сегодня и с начала месяца."""

    def setUp(self):
        super().setUp()
        from datetime import datetime, timedelta, timezone

        from app.api import admin_summary
        from app.db.models import GuestMessage, Lead

        self.summary = admin_summary
        self.now = datetime.now(timezone.utc)
        now_n = plans.naive_utc(self.now)
        today = plans.anchor_from_date(plans.msk_today(self.now))
        month = admin_summary._month_start(self.now)
        self.a, self.b, self.c = (self._mk(n) for n in ("kursk.a", "kursk.b", "kursk.c"))
        with self.Session() as db:
            start = plans.anchor_from_date(plans.msk_today() - timedelta(days=5))
            q.set_user_plan(db, self.a.id, "Старт", start, plans.anchor_from_date(plans.msk_today() + timedelta(days=3)))
            q.set_user_plan(db, self.b.id, "Профи", start, plans.anchor_from_date(plans.msk_today()))   # истёк
            q.set_user_plan(db, self.c.id, "Старт", start)
            db.add_all([AnswerUsage(user_id=self.c.id, ts=now_n - timedelta(minutes=30)) for _ in range(100)])

            def say(uid, sid, role, text, ago, pt=None, ct=None):
                m = q.log_message(db, user_id=uid, session_id=sid, role=role, content=text,
                                  prompt_tokens=pt, completion_tokens=ct)
                m.ts = now_n - ago
                db.commit()

            say(self.a.id, "s1", "user", "Отвеченный вопрос", timedelta(minutes=50))
            say(self.a.id, "s1", "assistant", "ответ", timedelta(minutes=49), 1_000_000, 0)  # 0.27 $ × 90 = 24.3 ₽
            say(self.a.id, "s2", "user", "Упавший вопрос", timedelta(minutes=40))            # без ответа
            say(self.c.id, "s3", "user", "Первый без ответа", timedelta(minutes=30))
            say(self.c.id, "s3", "user", "Переспросил", timedelta(minutes=29))
            say(self.c.id, "s3", "assistant", "ответ", timedelta(minutes=28))
            say(self.c.id, "s4", "user", "Только что спросил", timedelta(seconds=20))          # моложе FRESH
            say(self.b.id, "s5", "user", "Прошлый месяц", now_n - month + timedelta(minutes=1))
            if today != month:   # реплика «вчера или раньше, но в этом месяце» — в месяц, не в сегодня
                say(self.b.id, "s6", "user", "Ранее в этом месяце", now_n - today + timedelta(minutes=1))
                say(self.b.id, "s6", "assistant", "ответ", now_n - today + timedelta(seconds=30))
            gm = GuestMessage(guest_id="g" * 32, session_id="g1", role="assistant", content="x",
                              prompt_tokens=1_000_000, completion_tokens=0, charged=True)
            db.add(gm)
            db.add_all([Lead(tariff="Старт", name="Н", org="О", inn="4632000000", email="e@x.ru", phone="+7",
                             consent_at=now_n, created_at=now_n - timedelta(days=d)) for d in (2, 30)])
            db.commit()
        self.today_is_month_start = today == month

    def test_counts_and_unanswered(self):
        with self.Session() as db:
            s = self.summary.summary_view(db, self.now)
        today = s["periods"]["today"]
        self.assertEqual((today["questions"], today["answers"]), (5, 2), "вопрос прошлого месяца или ответ лишний")
        self.assertEqual(today["unanswered_n"], 2, "без ответа: упавший и «первый» в паре подряд; свежий — нет")
        self.assertEqual(today["users"], 2)
        self.assertAlmostEqual(today["rub"], 24.3, places=2)
        self.assertAlmostEqual(today["rub_total"], 48.6, places=2, msg="гости в расход не попали")
        self.assertEqual((today["guest"]["charged"]), 1)
        self.assertEqual({f["text"] for f in s["failures"]}, {"Упавший вопрос", "Первый без ответа"})
        month = s["periods"]["month"]
        if not self.today_is_month_start:
            self.assertEqual((month["questions"], month["answers"]), (6, 3),
                             "реплика прошлого месяца попала в месяц или ранняя реплика месяца потерялась")

    def test_attention_lists(self):
        from datetime import timedelta

        with self.Session() as db:    # тариф admin'а не закрывается (plan_expired) — не «истекает»
            q.set_user_plan(db, self.admin.id, "Администратор", plans.anchor_from_date(plans.msk_today()),
                            plans.anchor_from_date(plans.msk_today() - timedelta(days=1)))
            s = self.summary.summary_view(db, self.now)
        self.assertEqual([u["username"] for u in s["expiring"]], ["kursk.a"])
        self.assertEqual([u["username"] for u in s["expired"]], ["kursk.b"])
        self.assertEqual([u["username"] for u in s["exhausted"]], ["kursk.c"])
        self.assertEqual(s["accounts"]["no_profile"], 3)
        self.assertEqual((s["leads"]["total"], s["leads"]["week"]), (2, 1))
        self.assertEqual(s["trial"]["today"], 1)

    def test_page(self):
        with mock.patch.object(adm, "system_health", return_value={"collections": [], "server_time": "—"}):
            html = self.as_admin.get("/admin").text
        self.assertIn('<a href="/admin" class="on" aria-current="page">Сводка</a>', html)
        self.assertIn("/admin/dialogs/s2", html, "сбой без ссылки на беседу")
        self.assertIn("Упавший вопрос", html)
        self.assertIn(f'href="/admin/users/{self.a.id}">kursk.a</a> · Старт · до', html)
        self.assertIn('href="/admin/users?status=no_profile"', html)


class TestLeadsToAccounts(_PanelDB):
    """Заявки → учётки (этап 3): статус и заметка, «Создать учётку из заявки» с переносом
    организации, ИНН и контактов и тарифом, заявка становится «Подключена»."""

    def setUp(self):
        super().setUp()
        with self.Session() as db:
            self.lead = q.create_lead(db, tariff="Старт", name="Иван", org="ООО «Станкозавод»",
                                      inn="4632000000", email="I.Petrov@zavod.ru", phone="+7 900 000-00-00").id
            self.other = q.create_lead(db, tariff="Для организаций", name="Анна", org="АО «Прибор»",
                                       inn="4632111111", email="anna@pribor.ru", phone="+7 900 111-11-11").id

    def _lead(self, lid):
        with self.Session() as db:
            return q.get_lead(db, lid)

    def test_status_note_and_filter(self):
        self.assertEqual(self._lead(self.lead).status, "new")
        r = self.as_admin.post(f"/api/admin/leads/{self.lead}/status",
                               data={"status": "in_work", "note": "звонил 09.10"}, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, f"/admin/leads#lead-{self.lead}"))
        lead = self._lead(self.lead)
        self.assertEqual((lead.status, lead.note), ("in_work", "звонил 09.10"))
        self.assertIsNotNone(lead.status_at)
        self.assertEqual(self.as_admin.post(f"/api/admin/leads/{self.lead}/status",
                                            data={"status": "lost"}).status_code, 422)
        page = self.as_admin.get("/admin/leads?status=in_work").text
        self.assertIn("ООО «Станкозавод»", page)
        self.assertNotIn("АО «Прибор»", page, "фильтр статуса не работает")
        self.assertIn("В работе · 1", page)

    def test_account_from_lead(self):
        page = self.as_admin.get("/admin/leads").text
        self.assertIn('name="username" value="i.petrov"', page, "логин не предложен из email")
        r = self.as_admin.post(f"/api/admin/leads/{self.lead}/account",
                               data={"username": "i.petrov", "role": "user", "plan": "Старт"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"Учётка создана из заявки №{self.lead}", r.text)
        pw = r.text.split('id="new-password">')[1].split("<")[0]
        with self.Session() as db:
            u = q.get_user_by_username(db, "i.petrov")
        self.assertTrue(verify_password(pw, u.password_hash))
        self.assertEqual((u.org, u.inn, u.email, u.phone, u.role), ("ООО «Станкозавод»", "4632000000",
                                                                    "I.Petrov@zavod.ru", "+7 900 000-00-00", "user"))
        self.assertEqual((u.plan, plans.msk_date(u.plan_started_at)), ("Старт", plans.msk_today()))
        self.assertFalse(u.consent, "согласие за пользователя не ставится — его даёт анкета")
        lead = self._lead(self.lead)
        self.assertEqual((lead.status, lead.user_id), ("connected", u.id))
        again = self.as_admin.post(f"/api/admin/leads/{self.lead}/account", data={"username": "other.login"})
        self.assertEqual(again.status_code, 400)
        self.assertIn("уже создана: i.petrov", again.text)
        self.assertIn(f'href="/admin/users/{u.id}">i.petrov</a>', self.as_admin.get("/admin/leads").text)

    def test_status_filter_reaches_past_the_page_limit(self):
        """Ревью PR #182: фильтр статуса шёл ПОСЛЕ лимита страницы — старые новые заявки
        были недостижимы, хотя счётчик на чипе их считал."""
        self.as_admin.post(f"/api/admin/leads/{self.other}/status", data={"status": "in_work"})
        with mock.patch.object(adm, "LEADS_ON_PAGE", 1):
            page = self.as_admin.get("/admin/leads?status=new").text
            everything = self.as_admin.get("/admin/leads").text
        self.assertIn("ООО «Станкозавод»", page, "старая новая заявка не видна под фильтром")
        self.assertIn("Новая · 1", page)
        self.assertNotIn("показаны последние", page, "под фильтром показаны все новые")
        self.assertIn("2 всего · показаны последние 1", everything)

    def test_deleted_account_releases_its_lead(self):
        """Ревью PR #182: заявка держала id удалённой учётки — «уже создана» навсегда, а id,
        который SQLite отдаст следующей учётке, показал бы на заявке чужого человека."""
        self.as_admin.post(f"/api/admin/leads/{self.lead}/account", data={"username": "i.petrov"})
        uid = self._lead(self.lead).user_id
        self.as_admin.post(f"/api/admin/users/{uid}/delete", data={"confirm": "i.petrov"})
        self.assertIsNone(self._lead(self.lead).user_id, "заявка ссылается на удалённую учётку")
        again = self.as_admin.post(f"/api/admin/leads/{self.lead}/account", data={"username": "i.petrov"})
        self.assertEqual(again.status_code, 200, "из заявки нельзя создать учётку заново")

    def test_suggestion_avoids_taken_login_and_bad_input_keeps_the_lead(self):
        self._mk("i.petrov")
        self.assertIn('name="username" value="i.petrov2"', self.as_admin.get("/admin/leads").text)
        for data, msg in (({"username": "i.petrov"}, "уже занят"), ({"username": "Иван"}, "Логин — латинские"),
                          ({"username": "ok.login", "plan": "Безлимит"}, "Неизвестный тариф")):
            with self.subTest(data=data):
                r = self.as_admin.post(f"/api/admin/leads/{self.lead}/account", data=data)
                self.assertEqual(r.status_code, 400)
                self.assertIn(msg, r.text)
        self.assertEqual(self._lead(self.lead).status, "new")
        self.assertIsNone(self._lead(self.lead).user_id)

    def test_only_admin(self):
        anon = self.TestClient(self.app)
        for path in (f"/api/admin/leads/{self.lead}/status", f"/api/admin/leads/{self.lead}/account"):
            self.assertEqual(anon.post(path, data={"status": "new", "username": "x.y"}).status_code, 401)
        self.assertEqual(self._lead(self.lead).status, "new")

    def test_summary_counts_new_leads(self):
        from app.api import admin_summary

        self.as_admin.post(f"/api/admin/leads/{self.lead}/status", data={"status": "rejected"})
        with self.Session() as db:
            self.assertEqual(admin_summary.summary_view(db)["leads"]["new"], 1)


class TestOrganizations(_PanelDB):
    """Организации (этап 4): учётки по ИНН, без ИНН — по названию; суммы и заявки с тем же ИНН."""

    def setUp(self):
        super().setUp()
        from datetime import timedelta

        self.a = self._mk("kursk.a")
        self.b = self._mk("kursk.b")
        self.c = self._mk("orel.c")
        self.d = self._mk("perm.d")
        self.e = self._mk("kursk.e")
        today = plans.msk_today()
        with self.Session() as db:
            q.set_user_org(db, self.a.id, "ООО «Станкозавод»", "4632000000")
            q.set_user_org(db, self.b.id, "Станкозавод", "4632000000")         # другое написание, тот же ИНН
            q.set_user_org(db, self.c.id, "АО «Прибор»", "")
            q.set_user_plan(db, self.a.id, "Старт", plans.anchor_from_date(today),
                            plans.anchor_from_date(today + timedelta(days=20)))
            q.set_user_plan(db, self.b.id, "Старт", plans.anchor_from_date(today),
                            plans.anchor_from_date(today + timedelta(days=5)))
            q.set_blocked(db, self.b.id, True)
            q.set_user_org(db, self.e.id, "ООО «Станкозавод»", "4632000000")
            q.set_user_plan(db, self.e.id, "Тестировщик", plans.anchor_from_date(today - timedelta(days=9)),
                            plans.anchor_from_date(today - timedelta(days=1)))      # истёк: не «ближайший»
            db.add_all([AnswerUsage(user_id=self.a.id) for _ in range(7)])
            q.log_message(db, user_id=self.a.id, session_id="s", role="user", content="вопрос 1")
            q.log_message(db, user_id=self.b.id, session_id="t", role="user", content="вопрос 2")
            q.log_message(db, user_id=self.a.id, session_id="s", role="assistant", content="ответ — не вопрос")
            db.commit()
            self.lead = q.create_lead(db, tariff="Старт", name="Секретный Контакт", org="Станкозавод",
                                      inn="4632000000", email="secret@zavod.ru", phone="+7 999").id

    def test_grouping_and_totals(self):
        from datetime import timedelta

        with self.Session() as db:
            rows = adm._org_rows(db)
        by_name = {r["name"]: r for r in rows}
        plant = next(r for r in rows if r["inn"] == "4632000000")
        self.assertEqual({p["username"] for p in plant["people"]}, {"kursk.a", "kursk.b", "kursk.e"},
                         "ИНН не склеил учётки")
        self.assertEqual((plant["asked_month"], plant["used"], plant["limit"], plant["blocked"]), (2, 7, 200, 1))
        self.assertEqual(plant["plans"], [("Старт", 2), ("Тестировщик", 1)])
        self.assertEqual(plant["soonest"], f"{plans.msk_today() + timedelta(days=5):%d.%m.%Y}")
        self.assertEqual(plant["leads"], [{"id": self.lead, "status": "Новая"}])
        self.assertIn("АО «Прибор»", by_name, "без ИНН — по названию")
        self.assertEqual(rows[-1]["kind"], "none", "учётки без организации — в конце")
        self.assertEqual({p["username"] for p in rows[-1]["people"]}, {"perm.d", "boss"})

    def test_page_search_and_no_lead_pii(self):
        html = self.as_admin.get("/admin/orgs").text
        self.assertIn("ИНН 4632000000", html)
        self.assertIn(f'href="/admin/leads#lead-{self.lead}">№{self.lead} · Новая</a>', html)
        for pii in ("Секретный Контакт", "secret@zavod.ru", "+7 999"):
            self.assertNotIn(pii, html, "ПДн заявки вне раздела «Заявки»")
        found = self.as_admin.get("/admin/orgs?q=прибор").text
        self.assertIn("АО «Прибор»", found)
        self.assertNotIn("ИНН 4632000000", found)


class TestDialogs(_PanelDB):
    """Диалоги (этап 5): беседы новые сверху, постранично; поиск по вопросам и ответам (кириллица
    без учёта регистра, «ё» = «е», каждое слово); фильтры; страница беседы с оценками экспертов."""

    def setUp(self):
        super().setUp()
        from datetime import datetime, timedelta, timezone

        self.u = self._mk("kursk.expert1")
        self.v = self._mk("perm.expert1")
        now = plans.naive_utc(datetime.now(timezone.utc))
        with self.Session() as db:
            def say(uid, sid, role, text, ago, **kw):
                m = q.log_message(db, user_id=uid, session_id=sid, role=role, content=text, **kw)
                m.ts = now - ago
                db.commit()
                return m

            say(self.u.id, "old", "user", "Требования к СТАНКАМ с ЧПУ?", timedelta(days=3))
            say(self.u.id, "old", "assistant", "Ответ про Ёмкости и баллы.", timedelta(days=3), low_relevance=True)
            say(self.v.id, "mid", "user", "Какие документы для реестра?", timedelta(days=1))
            self.ans = say(self.v.id, "mid", "assistant", "Заявка через ГИСП.", timedelta(days=1),
                           unverified=["25"], prompt_tokens=1000, completion_tokens=100)
            say(self.u.id, "new", "user", "Срок действия заключения?", timedelta(minutes=5))
            q.save_feedback(db, user_id=self.v.id, kind="answer", rating=1, matched=None, comment="не то",
                            correction="Через ТПП, а не ГИСП", session_id="mid", message_id=self.ans.id)

    def _sids(self, query=""):
        html = self.as_admin.get("/admin/dialogs" + query).text
        return [s for s in ("new", "mid", "old") if f'href="/admin/dialogs/{s}?u=' in html], html

    def test_order_and_pages(self):
        sids, html = self._sids()
        self.assertEqual(sids, ["new", "mid", "old"])
        with mock.patch.object(adm, "DIALOGS_PER_PAGE", 2):
            first, page1 = self._sids()
            second, page2 = self._sids("?page=2")
        self.assertEqual((first, second), (["new", "mid"], ["old"]))
        self.assertIn("Дальше →", page1)
        self.assertIn("← Назад", page2)

    def test_search_questions_and_answers(self):
        for query, expect in (("?q=станкам", ["old"]),             # регистр кириллицы
                              ("?q=емкости", ["old"]),             # «ё» = «е», и ищется в ответе
                              ("?q=станкам баллы", ["old"]),       # слова — из вопроса и ответа одной беседы
                              ("?q=станкам гисп", []),             # слова из разных бесед — не совпадение
                              ("?q=гисп", ["mid"])):
            with self.subTest(query=query):
                self.assertEqual(self._sids(query)[0], expect)

    def test_filters(self):
        self.assertEqual(self._sids("?user=perm.expert1")[0], ["mid"])
        sids, html = self._sids("?user=nobody")
        self.assertEqual(sids, [])
        self.assertIn("Пользователя «nobody» нет", html)
        self.assertEqual(self._sids("?flagged=1")[0], ["mid", "old"], "флаги: низкая релевантность и числа")
        today = plans.msk_today()
        self.assertEqual(self._sids(f"?date_from={today.isoformat()}")[0], ["new"])

    def test_thread_page(self):
        html = self.as_admin.get(f"/admin/dialogs/mid?u={self.v.id}").text
        for piece in ("Какие документы для реестра?", "Заявка через ГИСП.", "непроверенные числа: 25",
                      "Оценка эксперта: <b>1★</b>", "Исправление: <b>Через ТПП, а не ГИСП</b>",
                      f'href="/admin/users/{self.v.id}">perm.expert1</a>'):
            self.assertIn(piece, html)
        self.assertEqual(self.as_admin.get("/admin/dialogs/нет-такой").status_code, 404)
        anon = self.TestClient(self.app)
        self.assertEqual(anon.get("/admin/dialogs/mid", follow_redirects=False).headers["location"], "/login")

    def test_same_session_id_of_two_users_stays_apart(self):
        """Ревью PR #182: список группирует по паре (session_id, пользователь), а страница беседы
        брала всё по session_id — две беседы с одним id открывались склейкой под логином первого."""
        with self.Session() as db:
            q.log_message(db, user_id=self.u.id, session_id="mid", role="user", content="Чужой вопрос в той же беседе")
        html = self.as_admin.get("/admin/dialogs").text
        for uid in (self.u.id, self.v.id):
            self.assertIn(f'href="/admin/dialogs/mid?u={uid}"', html)
        theirs = self.as_admin.get(f"/admin/dialogs/mid?u={self.v.id}").text
        mine = self.as_admin.get(f"/admin/dialogs/mid?u={self.u.id}").text
        self.assertNotIn("Чужой вопрос", theirs, "реплики другого пользователя склеены в беседу")
        self.assertIn("Заявка через ГИСП.", theirs)
        self.assertIn("Чужой вопрос в той же беседе", mine)
        self.assertIn(f'href="/admin/users/{self.u.id}">kursk.expert1</a>', mine)
        self.assertNotIn("Заявка через ГИСП.", mine)
        bare = self.as_admin.get("/admin/dialogs/mid").text      # старая ссылка без владельца
        self.assertNotIn("Чужой вопрос", bare, "без владельца — беседа первого автора, не склейка")
        for junk in ("²", "99999999999999999999", "-1"):
            with self.subTest(u=junk):
                self.assertEqual(self.as_admin.get(f"/admin/dialogs/mid?u={junk}").status_code, 200)


class TestQualityInMoscowTime(_PanelDB):
    """«Качество» показывает и фильтрует по Москве, как Сводка и Диалоги: один и тот же вопрос не
    должен стоять в разделах панели с разницей в три часа (найдено на скриншоте 09.10.2026)."""

    def test_late_utc_evening_is_next_moscow_day(self):
        from datetime import date, datetime

        from app.api.admin_stats import build_admin_view

        u = self._mk("kursk.late")
        with self.Session() as db:
            m = q.log_message(db, user_id=u.id, session_id="late", role="user", content="Поздний вопрос")
            m.ts = datetime(2026, 7, 20, 22, 30)              # UTC → 21.07 01:30 МСК
            db.commit()
            st = build_admin_view(db)["stats"]
            self.assertEqual([r["ts"] for r in st["recent"]], ["21.07.2026 01:30"])
            self.assertEqual([d["iso"] for d in st["per_day"]], ["2026-07-21"])
            on_21 = build_admin_view(db, date_from=date(2026, 7, 21), date_to=date(2026, 7, 21))["stats"]
            self.assertEqual(on_21["requests"], 1, "фильтр периода — не по московской дате")


class TestAccessAndNavigation(_PanelDB):
    PAGES = ("/admin", "/admin/users", "/admin/orgs", "/admin/leads", "/admin/quality", "/admin/dialogs",
             "/admin/trial")

    def test_only_admin(self):
        anon = self.TestClient(self.app)
        expert = self._login(self._mk("perm.expert1", role="expert").username)
        for path in self.PAGES + (f"/admin/users/{self.admin.id}",):
            with self.subTest(path=path):
                self.assertEqual(anon.get(path, follow_redirects=False).headers.get("location"), "/login")
                self.assertEqual(expert.get(path, follow_redirects=False).headers.get("location"), "/chat")
        for action in ("role", "password", "block", "delete", "plan", "org"):
            with self.subTest(action=action):
                self.assertEqual(anon.post(f"/api/admin/users/{self.admin.id}/{action}").status_code, 401)
                self.assertEqual(expert.post(f"/api/admin/users/{self.admin.id}/{action}").status_code, 403)

    def test_sections_render_with_the_current_one_marked(self):
        with mock.patch.object(adm, "system_health", return_value={}):
            for key, href, label in adm.SECTIONS:
                with self.subTest(section=key):
                    r = self.as_admin.get(href)
                    self.assertEqual(r.status_code, 200)
                    self.assertIn(f'<a href="{href}" class="on" aria-current="page">{label}</a>', r.text)
        self.assertEqual(adm.SECTIONS[0][:2], ("summary", "/admin"), "панель открывается Сводкой")

    def test_chat_has_no_dialog_logs_link(self):
        with mock.patch.object(web.quota, "quota_view", return_value=None):
            html = self.as_admin.get("/chat").text
        self.assertNotIn("Логи диалогов", html)
        self.assertNotIn('href="/admin"', html)


if __name__ == "__main__":
    unittest.main()
