"""Тариф «Тестировщик» на витрине и подтверждение разработчиками (14.10.2026).

Решение владельца: новая категория витрины — вкладка «Тестирование»; тариф «Тестировщик» бесплатный, без
лимита запросов, подключается только после подтверждения разработчиками сервиса (admin) и показывается
отдельным разделом админки «Тестировщики». Закрепляем:
* витрина (лендинг и кабинет) — третья вкладка из того же описания `pricing`;
* заявка на «Тестировщика» — вид «tester» данными, и с лендинга (карточкой и чипом), и из кабинета;
  чужие опции к ней — ошибка, как у остальных;
* раздел «Тестировщики»: очередь заявок (без ПДн заявки — они в «Заявках»), «Подтвердить» даёт учётке
  тариф с сегодняшнего дня и пишет КТО и КОГДА подтвердил; «Отклонить», «Снять тариф»;
* подтверждение пишется при ЛЮБОМ назначении «Тестировщика» admin'ом (карточка, учётка из заявки) и
  снимается сменой тарифа; повторное сохранение тестировщика его не перезаписывает.
"""

from __future__ import annotations

import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.api import admin as adm  # noqa: E402
from app.api import web  # noqa: E402
from app.core import plans, pricing  # noqa: E402
from app.db import queries as q  # noqa: E402
from tests.test_pricing import GOOD, LANDING, _cards, _Db, _panel, _request, _text  # noqa: E402

TESTER = plans.TESTER_PLAN
ADMIN = mock.Mock(id=901, role="admin", username="dev.admin")


def _get(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b"",
                    "session": {}, "app": None})


class TestCatalog(unittest.TestCase):
    def test_tester_is_the_internal_unlimited_plan(self):
        self.assertEqual(pricing.TESTER_TARIFF, "Тестировщик")
        self.assertIsNone(plans.ALL_PLANS[TESTER], "тестировщик — без лимита запросов")
        self.assertNotIn(TESTER, plans.PLAN_LIMITS, "не платный тариф: лендинг сверяет PLAN_LIMITS с карточками")
        self.assertEqual(list(pricing.TABS), ["individual", "corporate", "tester"])
        self.assertEqual(pricing.TABS["tester"][0], "Тестирование")

    def test_price_line(self):
        cases = [
            (dict(tariff="Стандарт"), "14 900 ₽/мес"),
            (dict(tariff="Старт", trial=True), "1 ₽ первая неделя, далее 9 900 ₽/мес"),
            (dict(tariff="Стандарт", kind="corporate", seats=3), "44 700 ₽/мес"),
            (dict(tariff="Стандарт", kind="corporate", period="year"), "178 800 ₽/год"),
            (dict(tariff="Профи", kind="corporate", period="year", seats=500), "119 400 000 ₽/год"),
            (dict(tariff="Для организаций"), "от 240 000 ₽/год"),
            (dict(tariff=TESTER), "Бесплатно"),
            (dict(tariff="Безлимит"), None),
        ]
        for kw, want in cases:
            with self.subTest(**kw):
                self.assertEqual(pricing.price_line(kw.pop("tariff"), **kw), want)

    def test_lead_kind(self):
        for args, want in (((TESTER, "tester"), "tester"), ((TESTER, "individual"), "tester"),
                           (("Профи", "corporate"), "corporate"), (("Для организаций", "individual"), "corporate"),
                           (("Стандарт", "individual"), None), (("Стандарт", ""), None)):
            with self.subTest(args=args):
                self.assertEqual(pricing.lead_kind(*args), want)

    def test_tester_options(self):
        self.assertEqual(pricing.parse_options(TESTER, kind="tester"), (None, {}))
        self.assertEqual(pricing.parse_options(TESTER), (None, {}), "чип формы шлёт тариф без вида")
        for kw, field in ((dict(tariff=TESTER, kind="tester", trial=True), "trial"),
                          (dict(tariff=TESTER, kind="corporate"), "kind"),
                          (dict(tariff="Стандарт", kind="tester"), "kind")):
            with self.subTest(**kw):
                opts, errs = pricing.parse_options(kw.pop("tariff"), **kw)
                self.assertIsNone(opts)
                self.assertIn(field, errs)


class TestLandingTab(unittest.TestCase):
    def test_tab_card_and_chip(self):
        self.assertIn('id="ptab-tester" aria-selected="false" aria-controls="plans-tester" data-kind="tester" '
                      f'data-title="{pricing.TABS["tester"][1]}" tabindex="-1">{pricing.TABS["tester"][0]}</button>', LANDING)
        panel = _panel(LANDING, "tester")
        self.assertIn('id="plans-tester" role="tabpanel"', LANDING)
        self.assertIn(f'<div class="price">{pricing.TESTER_PRICE}</div><div class="price-sub">{pricing.TESTER_NOTE}</div>', panel)
        self.assertIn(f'data-pick="{TESTER}" data-tester="1">Подать заявку</button>', panel)
        steps = re.search(r'<ol class="org-list org-steps">(.*?)</ol>', panel, re.S).group(1)
        self.assertEqual([_text(s) for s in re.findall(r"<li>(.*?)</li>", steps)], pricing.HOWTO_STEPS)
        chips = LANDING[LANDING.index('id="tariff-chips"'):]
        self.assertIn(f'<button type="button" aria-pressed="false">{TESTER}</button>', chips[:chips.index("</div>")])

    def test_script_sends_the_tester_kind(self):
        js = (ROOT / "landing" / "app.js").read_text(encoding="utf-8")
        self.assertIn("tester = b.hasAttribute('data-tester')", js)
        self.assertIn("o = { kind: corp ? 'corporate' : tester ? 'tester' : 'individual'", js)


class TestLandingLead(_Db):
    def post(self, **extra):
        from fastapi.testclient import TestClient

        from main import app

        return TestClient(app).post("/api/leads", json={**GOOD, **extra})

    def test_tester_lead_from_card_and_chip(self):
        self.assertEqual(self.post(tariff=TESTER, kind="tester").status_code, 200)
        self.assertEqual(self.post(tariff=TESTER).status_code, 200)              # чип формы
        self.assertEqual([(lead.tariff, lead.kind, lead.options) for lead in self.leads()],
                         [(TESTER, "tester", None)] * 2)

    def test_foreign_options_rejected(self):
        for extra in (dict(tariff=TESTER, kind="tester", trial=True), dict(tariff=TESTER, kind="corporate"),
                      dict(tariff="Стандарт", kind="tester")):
            with self.subTest(**extra):
                r = self.post(**extra)
                self.assertEqual(r.status_code, 422, r.text)
                self.assertIn("options", r.json()["errors"])
        self.assertEqual(self.leads(), [])


class _Accounts(_Db):
    def user(self, login="kursk.tester1", plan=None):
        with self.Session() as db:
            u = q.create_user(db, login, "h", role="user")
            q.update_profile(db, u.id, full_name="Ольга Тестова", region="Курская область", telegram="",
                             consent=True, email="o.testova@tpp.ru", phone="+7 900 222-22-22")
            q.set_user_org(db, u.id, "Курская ТПП", "4629000000")
            if plan:
                q.set_user_plan(db, u.id, plan, plans.anchor_from_date(plans.msk_today()), verified_by=ADMIN.id)
            u = q.get_user(db, u.id)
            db.expunge(u)
            return u

    def get(self, uid):
        with self.Session() as db:
            u = q.get_user(db, uid)
            db.expunge(u)
            return u


class TestCabinet(_Accounts):
    def page(self, user, query=""):
        with mock.patch.object(web, "current_user", return_value=user):
            return web.profile_page(_request(query), tab="plan").body.decode("utf-8")

    def ask(self, user, **form):
        fields = dict(tariff=TESTER, kind="tester", period="month", seats="1", trial_flag="", consent="1",
                      renew="", message="")
        fields.update(form)
        with mock.patch.object(web, "current_user", return_value=user):
            return web.plan_request(_request(), **fields)

    def test_tab_is_the_catalog(self):
        html = self.page(self.user())
        self.assertIn(f'data-kind="tester"', html)
        self.assertEqual(_cards(_panel(html, "tester")), [(TESTER, pricing.TESTER_DESC),
                                                           (pricing.HOWTO_NAME, pricing.HOWTO_DESC)])
        self.assertIn('name="kind" value="tester"', _panel(html, "tester"))
        self.assertIn(">Подать заявку</button>", _panel(html, "tester"))
        self.assertIn(f'<h2 id="tariffs-h">{pricing.TABS["tester"][1]}</h2>', self.page(self.user("kursk.t2"), "kind=tester"),
                      "вкладка из адреса — тестирование")

    def test_request_is_a_tester_lead_with_its_own_confirmation(self):
        u = self.user()
        html = self.page(u, f"confirm=1&tariff={TESTER}&kind=tester")
        box = html[html.index('class="tconfirm"'):html.index("</form>", html.index('class="tconfirm"'))]
        self.assertIn(f"Заявка на тариф «{TESTER}»", box)
        self.assertIn("Подключают его разработчики сервиса после проверки заявки", _text(box))
        self.assertIn('name="consent" value="1" required', box)
        self.assertNotIn('class="pop-back"', html)
        self.assertEqual(self.ask(u).status_code, 303)
        lead = self.leads()[0]
        self.assertEqual((lead.tariff, lead.kind, lead.user_id, lead.options), (TESTER, "tester", u.id, "из личного кабинета"))
        self.assertEqual(self.ask(u, consent="").headers["location"], "/profile?tab=plan&err=consent&kind=tester#tariffs",
                         "ошибка — на вкладку «Тестирование»")

    def test_connected_tester_has_no_button(self):
        panel = _panel(self.page(self.user(plan=TESTER)), "tester")
        self.assertIn(">Подключён</button>", panel)
        self.assertNotIn("Подать заявку", panel)


class TestAdminTesters(_Accounts):
    def page(self, ok=""):
        with mock.patch.object(adm, "current_user", return_value=ADMIN):
            return adm.admin_testers(_get("/admin/testers"), ok=ok).body.decode("utf-8")

    def lead(self, user=None, tariff=TESTER, kind="tester", email="lead.secret@example.ru", phone="+7 900 333-33-33"):
        with self.Session() as db:
            return q.create_lead(db, tariff=tariff, name="Секретное Имя", org="ООО «Проверка»", inn="4632000000",
                                 email=email, phone=phone, user_id=user.id if user else None, kind=kind).id

    def lead_status(self, lead_id):
        with self.Session() as db:
            return q.get_lead(db, lead_id).status

    def test_section_in_the_sidebar_and_admin_only(self):
        self.assertIn(("testers", "/admin/testers", "Тестировщики"), adm.SECTIONS)
        for who, where in ((None, "/login"), (mock.Mock(id=5, role="expert"), "/chat")):
            with self.subTest(role=getattr(who, "role", None)):
                with mock.patch.object(adm, "current_user", return_value=who):
                    r = adm.admin_testers(_get("/admin/testers"))
                self.assertEqual((r.status_code, r.headers["location"]), (302, where))

    def test_queue_shows_only_tester_requests_without_lead_pii(self):
        u = self.user()
        cabinet = self.lead(u)
        landing = self.lead()
        other = self.lead(tariff="Стандарт", kind=None)
        old_style = self.lead(kind=None)          # заявка без вида — по тарифу
        html = self.page()
        for lid in (cabinet, landing, old_style):
            self.assertIn(f'id="tester-lead-{lid}"', html)
        self.assertNotIn(f'id="tester-lead-{other}"', html)
        self.assertIn(f'action="/api/admin/testers/{cabinet}/approve"', html)
        self.assertNotIn(f'action="/api/admin/testers/{landing}/approve"', html, "без учётки подтверждать некого")
        self.assertIn(f'href="/admin/leads?lead={landing}#lead-{landing}">Создать учётку</a>', html)
        for pii in ("lead.secret@example.ru", "+7 900 333-33-33", "Секретное Имя"):
            self.assertNotIn(pii, html, "ПДн заявки читаются только в «Заявках»")

    def test_approve_connects_and_records_who_and_when(self):
        u = self.user()
        lid = self.lead(u)
        until = plans.msk_today() + timedelta(days=30)
        r = adm.admin_tester_approve(lid, expires=until.isoformat(), admin=ADMIN)
        self.assertEqual(r.headers["location"], "/admin/testers?ok=approved")
        got = self.get(u.id)
        self.assertEqual((got.plan, got.tester_verified_by), (TESTER, ADMIN.id))
        self.assertIsNotNone(got.tester_verified_at)
        self.assertEqual(plans.msk_date(got.plan_started_at), plans.msk_today())
        self.assertEqual(plans.msk_date(got.plan_expires_at), until)
        self.assertEqual(self.lead_status(lid), "connected")
        with self.Session() as db:          # имя подтвердившего — в строке тестировщика
            q.create_user(db, "dev.admin", "h", role="admin")
            admin_id = q.get_user_by_username(db, "dev.admin").id
            q.get_user(db, u.id).tester_verified_by = admin_id
            db.commit()
        html = self.page(ok="approved")
        self.assertIn(pricing.TESTER_TARIFF, html)
        self.assertIn(adm.TESTER_DONE["approved"], html)
        self.assertNotIn(f'id="tester-lead-{lid}"', html, "подтверждённая — не в очереди")
        row = html[html.index(f'href="/admin/users/{u.id}">kursk.tester1</a>'):]
        self.assertIn("· dev.admin", row[:row.index("</tr>")])
        self.assertIn(f"до {until:%d.%m.%Y}", row[:row.index("</tr>")])

    def test_approve_guards(self):
        u = self.user()
        for lid, kw, code in ((self.lead(u, tariff="Стандарт", kind=None), {}, 404),
                              (self.lead(), {}, 422),                                   # с лендинга — учётки нет
                              (self.lead(u), {"expires": "завтра"}, 422),
                              (self.lead(u), {"expires": plans.msk_today().isoformat()}, 422),
                              (987654, {}, 404)):
            with self.subTest(lead=lid, **kw):
                with self.assertRaises(HTTPException) as e:
                    adm.admin_tester_approve(lid, admin=ADMIN, **{"expires": "", **kw})
                self.assertEqual(e.exception.status_code, code)
        self.assertIsNone(self.get(u.id).plan, "отказ не подключил тариф")

    def test_reject_and_revoke(self):
        u = self.user()
        lid = self.lead(u)
        self.assertEqual(adm.admin_tester_reject(lid, admin=ADMIN).headers["location"], "/admin/testers?ok=rejected")
        self.assertEqual(self.lead_status(lid), "rejected")
        self.assertNotIn(f'id="tester-lead-{lid}"', self.page())
        with self.assertRaises(HTTPException):
            adm.admin_tester_reject(self.lead(tariff="Профи", kind=None), admin=ADMIN)
        t = self.user("kursk.tester2", plan=TESTER)
        before = self.get(t.id)
        self.assertIn(f'action="/api/admin/testers/users/{t.id}/revoke"', self.page())
        adm.admin_tester_revoke(t.id, admin=ADMIN)
        # Ревью 15.10.2026 (MED): отзыв снимал тариф, а «без тарифа» — безлимит без срока, то есть отзыв
        # давал больше доступа, чем бездействие. Теперь доступ закрыт: тариф тот же, срок истёк сегодня.
        got = self.get(t.id)
        self.assertEqual(got.plan, TESTER)
        self.assertTrue(plans.plan_expired(got), "после отзыва у тестировщика остался доступ")
        self.assertEqual(plans.msk_date(got.plan_expires_at), plans.msk_today())
        self.assertEqual((got.plan_started_at, got.tester_verified_at), (before.plan_started_at, before.tester_verified_at))
        from app.api import chat
        with self.assertRaises(HTTPException) as e:
            chat._enforce_plan_quota(got, mock.Mock(session_id=None, message="вопрос"))
        self.assertEqual(e.exception.status_code, 402, "вопросы после отзыва не закрыты")
        page = self.page("revoked")
        self.assertIn("Доступ тестировщика закрыт", page)
        self.assertNotIn(f'action="/api/admin/testers/users/{t.id}/revoke"', page, "истёкшему — снова «Закрыть доступ»")
        # повторный отзыв уже закрытого не сдвигает дату
        with self.Session() as db:
            q.set_user_plan(db, t.id, TESTER, got.plan_started_at, plans.anchor_from_date(plans.msk_today() - timedelta(days=3)))
        adm.admin_tester_revoke(t.id, admin=ADMIN)
        self.assertEqual(plans.msk_date(self.get(t.id).plan_expires_at), plans.msk_today() - timedelta(days=3))
        with self.assertRaises(HTTPException):
            adm.admin_tester_revoke(u.id, admin=ADMIN)          # не тестировщик — закрывать нечего

    def test_revoke_does_not_stamp_a_legacy_tester(self):
        """Ревью 15.10.2026, LOW: тестировщик, назначенный до test38, — без отметки подтверждения. Закрытие
        доступа шло через `set_user_plan` и записывало подтверждающим того, кто закрывает."""
        from app.db.models import User

        t = self.user("kursk.tester3", plan=TESTER)
        with self.Session() as db:
            row = db.get(User, t.id)
            row.tester_verified_at = row.tester_verified_by = None     # как после миграции test38
            db.commit()
        adm.admin_tester_revoke(t.id, admin=ADMIN)
        got = self.get(t.id)
        self.assertTrue(plans.plan_expired(got))
        self.assertEqual((got.tester_verified_at, got.tester_verified_by), (None, None),
                         "закрывший доступ записан подтверждающим")

    def test_card_assignment_records_and_plan_change_clears(self):
        u = self.user()
        adm.admin_set_plan(u.id, plan=TESTER, started="", expires="", kind="individual", admin=ADMIN)
        first = self.get(u.id)
        self.assertEqual(first.tester_verified_by, ADMIN.id)
        other = mock.Mock(id=902, role="admin", username="other")
        adm.admin_set_plan(u.id, plan=TESTER, started="", expires=(plans.msk_today() + timedelta(days=9)).isoformat(),
                           kind="individual", admin=other)
        again = self.get(u.id)
        self.assertEqual((again.tester_verified_by, again.tester_verified_at), (ADMIN.id, first.tester_verified_at),
                         "смена срока — не новое подтверждение")
        adm.admin_set_plan(u.id, plan="Старт", started="", expires="", kind="individual", admin=ADMIN)
        self.assertEqual((self.get(u.id).tester_verified_at, self.get(u.id).tester_verified_by), (None, None))

    def test_account_from_landing_lead_records_the_verifier(self):
        lid = self.lead()
        with mock.patch.object(adm, "current_user", return_value=ADMIN):
            adm.admin_lead_account(_get("/admin/leads"), lid, username="new.tester", role="user", plan=TESTER, admin=ADMIN)
        with self.Session() as db:
            made = q.get_user_by_username(db, "new.tester")
            self.assertEqual((made.plan, made.tester_verified_by, made.plan_kind), (TESTER, ADMIN.id, None))
        self.assertEqual(self.lead_status(lid), "connected")

    def test_user_card_shows_the_verification(self):
        u = self.user(plan=TESTER)
        with mock.patch.object(adm, "current_user", return_value=ADMIN):
            html = adm.admin_user_card(_get(f"/admin/users/{u.id}"), u.id).body.decode("utf-8")
        self.assertIn("Тестировщик подтверждён:", html)
        self.assertIn('<a href="/admin/testers">все тестировщики</a>', html)


class TestReviewR38(_Accounts):
    """`R38` #200 — шесть LOW облачного ревью `feat/tester-plan` (15.10.2026)."""

    page, lead = TestAdminTesters.page, TestAdminTesters.lead   # хелперы — без чужих тестов

    def test_decided_lead_is_not_approved_again(self):
        """П. 1: устаревшая вкладка не подключает тариф по отклонённой заявке и не затирает срок."""
        u = self.user()
        lid = self.lead(u)
        until = plans.msk_today() + timedelta(days=30)
        adm.admin_tester_approve(lid, expires=until.isoformat(), admin=ADMIN)
        started = self.get(u.id).plan_started_at
        with self.assertRaises(HTTPException) as e:
            adm.admin_tester_approve(lid, expires="", admin=ADMIN)
        self.assertEqual(e.exception.status_code, 409)
        got = self.get(u.id)
        self.assertEqual((got.plan_started_at, plans.msk_date(got.plan_expires_at)), (started, until),
                         "повторное «Подтвердить» затёрло срок")
        other = self.user("kursk.tester2")
        rejected = self.lead(other)
        adm.admin_tester_reject(rejected, admin=ADMIN)
        with self.assertRaises(HTTPException) as e:
            adm.admin_tester_approve(rejected, expires="", admin=ADMIN)
        self.assertEqual(e.exception.status_code, 409)
        self.assertIsNone(self.get(other.id).plan, "отклонённая заявка подключила тариф")

    def test_date_field_starts_tomorrow(self):
        """П. 2: поле не пускает сегодняшний день — сервер его не примет."""
        self.lead(self.user())
        tomorrow = (plans.msk_today() + timedelta(days=1)).isoformat()
        self.assertIn(f'name="expires" min="{tomorrow}"', self.page())

    def test_counter_counts_the_whole_queue(self):
        """П. 6: очередь на странице усечена — счётчик и пометка об этом, а не длина списка."""
        u = self.user()
        for _ in range(3):
            self.lead(u)
        with mock.patch.object(adm, "LEADS_ON_PAGE", 2):
            html = self.page()
        self.assertIn("заявок ждут решения: 3 (показаны первые 2)", " ".join(html.split()))

    def test_lead_link_opens_that_lead(self):
        """П. 6: «Создать учётку» ведёт на страницу ОДНОЙ заявки — в усечённом списке якоря могло не быть."""
        lid = self.lead()
        other = self.lead()
        self.assertIn(f'href="/admin/leads?lead={lid}#lead-{lid}">Создать учётку</a>', self.page())
        with mock.patch.object(adm, "current_user", return_value=ADMIN):
            html = adm.admin_leads_page(_get("/admin/leads"), lead=lid).body.decode("utf-8")
        self.assertIn(f'id="lead-{lid}"', html)
        self.assertNotIn(f'id="lead-{other}"', html)
        self.assertIn(f"показана заявка №{lid}", html)
        with mock.patch.object(adm, "current_user", return_value=ADMIN):
            gone = adm.admin_leads_page(_get("/admin/leads"), lead=987654).body.decode("utf-8")
        self.assertIn("заявки №987654 нет", gone)

    def test_reject_of_decided_lead_is_refused(self):
        """Ревью этапа 2: «Отклонить» в устаревшей вкладке не помечает отказом подключённую заявку."""
        u = self.user()
        lid = self.lead(u)
        adm.admin_tester_approve(lid, expires="", admin=ADMIN)
        with self.assertRaises(HTTPException) as e:
            adm.admin_tester_reject(lid, admin=ADMIN)
        self.assertEqual(e.exception.status_code, 409)
        with self.Session() as db:
            self.assertEqual(q.get_lead(db, lid).status, "connected")

    def test_expired_tester_request_is_not_marked_as_tester(self):
        """Истёкший тестировщик подаёт заявку снова — отметка «уже тестировщик» её не порочит."""
        u = self.user(plan=TESTER)
        with self.Session() as db:
            q.get_user(db, u.id).plan_expires_at = plans.anchor_from_date(plans.msk_today() - timedelta(days=1))
            db.commit()
        lid = self.lead(u)
        row = self.page()
        row = row[row.index(f'id="tester-lead-{lid}"'):]
        self.assertNotIn("уже тестировщик", row[:row.index("</tr>")])


class TestCabinetR38(_Accounts):
    page = TestCabinet.page   # хелпер — без чужих тестов

    def test_expired_tester_can_apply_again(self):
        """П. 5: истёкший тестировщик видит «Подать заявку», а не «Подключён» без кнопки."""
        u = self.user(plan=TESTER)
        with self.Session() as db:
            q.get_user(db, u.id).plan_expires_at = plans.anchor_from_date(plans.msk_today() - timedelta(days=1))
            db.commit()
        panel = _panel(self.page(self.get(u.id)), "tester")
        self.assertIn(">Подать заявку</button>", panel)
        self.assertNotIn(">Подключён</button>", panel)

    def test_corporate_popup_shows_the_price(self):
        """П. 3: цена корпоративного тарифа видна и в попапе связи с заказчиком."""
        html = self.page(self.user(), "confirm=1&tariff=Профи&kind=corporate&period=year&seats=5")
        pop = html[html.index('class="pop-back"'):]
        pop = pop[:pop.index("</form>")]
        price = pricing.price_line("Профи", kind="corporate", period="year", seats=5)
        self.assertTrue(price)
        self.assertIn(f"Стоимость — {price} без НДС", " ".join(pop.split()))

    def test_sum_is_recomputed_on_load(self):
        """П. 4: сумма пересчитывается при загрузке и при возврате «Назад» — в кабинете и на лендинге."""
        profile = (ROOT / "app" / "web" / "templates" / "profile.html").read_text(encoding="utf-8")
        landing = (ROOT / "landing" / "app.js").read_text(encoding="utf-8")
        self.assertIn('window.addEventListener("pageshow", reprice);', profile)
        self.assertIn("window.addEventListener('pageshow', reprice);", landing)


class TestSchema(unittest.TestCase):
    def test_columns_migrate_on_start(self):
        src = (ROOT / "app" / "db" / "engine.py").read_text(encoding="utf-8")
        self.assertIn('("tester_verified_at", "DATETIME")', src)
        self.assertIn('("tester_verified_by", "INTEGER")', src)


if __name__ == "__main__":
    unittest.main()
