"""Витрина тарифов (решение владельца 09.10.2026): «как у Нейроюриста, но с плейсхолдерами вместо сумм».

Закрепляем: лендинг (статичный) и кабинет (шаблон) показывают ОДНО описание из `app/core/pricing.py` —
названия, объёмы (= лимиты #160), опцию, пробу за 1 ₽ и сноски по вкладкам; суммы — только
плейсхолдер «X XXX», ни одной выдуманной цены; выбранные на карточке опции доезжают до заявки — и
с лендинга (`/api/leads`), и из кабинета (`/api/plan-request`, заявка привязана к учётке).
"""

from __future__ import annotations

import re
import sys
import unittest
from html import unescape
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.api import admin as adm  # noqa: E402
from app.api import leads as leads_api  # noqa: E402
from app.api import web  # noqa: E402
from app.core import pricing  # noqa: E402
from app.core.plans import PLAN_LIMITS  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base  # noqa: E402

LANDING = (ROOT / "landing" / "index.html").read_text(encoding="utf-8")
GOOD = {"tariff": "Стандарт", "name": "Иван Иванов", "org": "ООО «Предприятие»", "inn": "4632000000",
        "email": "ivan@example.ru", "phone": "+7 (912) 345-67-89", "promo": "", "consent": True}


def _text(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _inline(fragment: str) -> str:
    """Текст строки со строчными тегами (`₽/<span class="per">мес</span>`): теги — без пробела."""
    return " ".join(unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _panel(html: str, kind: str) -> str:
    """Карточки одной вкладки: от её `data-panel` до следующего `data-panel`."""
    i = html.index(f'data-panel="{kind}"')
    j = html.find("data-panel=", i + 1)
    return html[i:j if j != -1 else len(html)]


def _cards(panel: str) -> list[tuple[str, str]]:
    return [(_text(n), _text(d)) for n, d in re.findall(
        r'class="(?:plan|tcard)-name">(.*?)</div>\s*<div class="(?:plan|tcard)-desc">(.*?)</div>', panel, re.S)]


def _request(query: str = ""):
    from urllib.parse import quote

    qs = "&".join(f"{k}={quote(v)}" for k, v in (part.split("=", 1) for part in query.split("&") if part))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": qs.encode(),
                    "session": {}, "app": None})


class TestCatalog(unittest.TestCase):
    def test_volumes_are_the_plan_limits(self):
        self.assertEqual({p["name"]: p["limit"] for p in pricing.PLANS}, PLAN_LIMITS)
        for p in pricing.PLANS:
            self.assertTrue(p["desc"].startswith(f"До {p['limit']} запросов в месяц "), p["desc"])
        self.assertEqual([p["name"] for p in pricing.PLANS if p["trial"]], ["Старт"])

    def test_options_summary_and_rules(self):
        cases = [
            (dict(tariff="Стандарт"), None),
            (dict(tariff="Старт", trial=True, addon=True), "пробная неделя за 1 ₽ · доступ к источникам в ответах Навигатора"),
            (dict(tariff="Профи", kind="corporate", period="year", seats=12), "корпоративный · год · 12 польз."),
            (dict(tariff="Для организаций", addon=True), None),
        ]
        for kw, want in cases:
            with self.subTest(**kw):
                self.assertEqual(pricing.parse_options(kw.pop("tariff"), **kw), (want, {}))
        for kw, field in ((dict(tariff="Стандарт", trial=True), "trial"),
                          (dict(tariff="Старт", kind="corporate", trial=True), "trial"),
                          (dict(tariff="Профи", seats=0), "seats"), (dict(tariff="Профи", seats=501), "seats"),
                          (dict(tariff="Профи", seats=True), "seats"),
                          (dict(tariff="Профи", period="week"), "period"), (dict(tariff="Профи", kind="vip"), "kind")):
            with self.subTest(**kw):
                opts, errs = pricing.parse_options(kw.pop("tariff"), **kw)
                self.assertIsNone(opts)
                self.assertIn(field, errs)


class TestLandingShowcase(unittest.TestCase):
    def test_both_tabs_show_the_catalog(self):
        want = [(p["name"], p["desc"]) for p in pricing.PLANS]
        self.assertEqual(_cards(_panel(LANDING, "individual")), want + [("Для организаций", pricing.ORG_DESC)])
        self.assertEqual(_cards(_panel(LANDING, "corporate")), want + [("Особые условия", pricing.SPECIAL_DESC)])
        self.assertIn('data-goto="corporate">Посмотреть тарифы', LANDING)
        self.assertEqual(LANDING.count(f"<b>{pricing.ADDON}</b>"), 6, "опция — на каждой тарифной карточке")
        self.assertEqual(LANDING.count("data-seats"), 3, "число пользователей — у корпоративных")

    def test_trial_only_on_individual_start(self):
        ind, corp = _panel(LANDING, "individual"), _panel(LANDING, "corporate")
        self.assertEqual(ind.count("Предложение ограничено"), 1)
        self.assertIn(f'<div class="price">{pricing.TRIAL_TEXT}</div>', ind)
        self.assertIn(f'data-pick="Старт" data-trial="1">Подключить за {pricing.TRIAL_PRICE}', ind)
        self.assertNotIn("data-trial", corp)
        self.assertNotIn(pricing.TRIAL_TEXT, corp)

    def test_addon_like_neurolegal(self):
        """Опция — как у Нейроюриста (решение владельца 09.10.2026): «Доступ к источникам в ответах …»,
        у «Старта» «Бесплатно первый месяц, далее + … за Гарант Лайт», у остальных «+ … за Гарант Лайт»."""
        notes = [_inline(n) for n in re.findall(r"<b>" + re.escape(pricing.ADDON) + r"</b><small>(.*?)</small>", LANDING, re.S)]
        free = f"{pricing.ADDON_FREE}, далее + {pricing.PRICE} ₽/мес за {pricing.ADDON_SOURCE}"
        paid = f"+ {pricing.PRICE} ₽/мес за {pricing.ADDON_SOURCE}"
        self.assertEqual(notes, [free, paid, paid, paid, paid, paid])

    def test_footnotes_per_tab(self):
        for kind in ("individual", "corporate"):
            with self.subTest(kind=kind):
                block = LANDING[LANDING.index(f'<div class="fine" data-panel="{kind}"'):]
                block = block[:block.index("</div>")]
                self.assertEqual([_text(p) for p in re.findall(r"<p>(.*?)</p>", block, re.S)],
                                 pricing.FOOTNOTES[kind])

    def test_only_placeholder_prices(self):
        """Цены не утверждены: везде «X XXX ₽». Цифры при рубле допустимы только у пробы за 1 ₽."""
        section = LANDING[LANDING.index('id="pricing"'):LANDING.index('id="audience"')]
        visible = _text(re.sub(r"<!--.*?-->", "", section, flags=re.S))
        self.assertIn("X XXX ₽/мес", visible)
        self.assertEqual(re.findall(r"\d[\d\s]*₽", visible), ["1 ₽"] * visible.count("1 ₽"),
                         "на витрине появилась настоящая сумма — цены не утверждены")

    def test_landing_script_speaks_the_catalog(self):
        """Ревью PR #184: строка «Выбрано: …» и потолок пользователей в скрипте лендинга — копия
        `pricing`; держим их равными описанию, как карточки."""
        js = (ROOT / "landing" / "app.js").read_text(encoding="utf-8")
        for piece in (f"'{pricing.KINDS['corporate']}'", f"'пробная неделя за {pricing.TRIAL_PRICE}'",
                      f"'{pricing.ADDON[0].lower() + pricing.ADDON[1:]}'", f", {pricing.MAX_SEATS})) : 1"):
            self.assertIn(piece, js)
        self.assertEqual(LANDING.count(f'max="{pricing.MAX_SEATS}"'), 3)

    def test_card_options_reach_the_lead_form(self):
        js = (ROOT / "landing" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="tariff-opts"', LANDING)
        self.assertIn("if (opts) { payload.kind = opts.kind; payload.addon = opts.addon; payload.trial = opts.trial;", js)
        self.assertIn("payload.period = opts.period; payload.seats = opts.seats;", js)
        self.assertIn("chips.forEach(function (c) { c.addEventListener('click', function () { setTariff(c.textContent); }); });",
                      js, "чип в форме — тариф без опций карточки")


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        for p in (mock.patch.object(leads_api, "get_session", self.Session),
                  mock.patch.object(web, "get_session", self.Session),
                  mock.patch.object(adm, "get_session", self.Session),
                  mock.patch.object(web, "_plan_request_limit", web.SlidingWindow(1000, window=3600.0))):
            p.start()
            self.addCleanup(p.stop)
        leads_api._lead_limit.reset()

    def leads(self):
        with self.Session() as db:
            return q.list_leads(db)


class TestLandingLeadOptions(_Db):
    def post(self, **extra):
        from fastapi.testclient import TestClient

        from main import app

        return TestClient(app).post("/api/leads", json={**GOOD, **extra})

    def test_options_are_stored_with_the_lead(self):
        r = self.post(tariff="Профи", kind="corporate", period="year", seats=7, addon=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.leads()[0].options, "корпоративный · год · 7 польз. · доступ к источникам в ответах Навигатора")

    def test_old_form_without_options_still_works(self):
        self.assertEqual(self.post().status_code, 200)
        self.assertIsNone(self.leads()[0].options)

    def test_bad_options_rejected_before_saving(self):
        r = self.post(tariff="Профи", trial=True)
        self.assertEqual(r.status_code, 422)
        self.assertIn("options", r.json()["errors"])
        self.assertEqual(self.leads(), [])

    def test_foreign_option_types_get_the_form_error_shape(self):
        """Ревью PR #184: строгие типы отдавали стандартный 422 FastAPI `{"detail": …}`, которого форма
        не понимает. Чужие типы — ошибка опций тем же ответом `errors`."""
        for extra in (dict(seats="abc"), dict(seats=None), dict(addon="yes"), dict(trial=1),
                      dict(kind=["corporate"]), dict(period={"x": 1}), dict(seats=True)):
            with self.subTest(**{k: repr(v) for k, v in extra.items()}):
                r = self.post(**extra)
                self.assertEqual(r.status_code, 422, r.text)
                self.assertIn("options", r.json()["errors"])
        self.assertEqual(self.leads(), [])


class TestCabinetShowcase(_Db):
    def _user(self, **profile):
        with self.Session() as db:
            u = q.create_user(db, f"kursk.user{len(q.list_users(db)) + 1}", "h", role="user")
            q.update_profile(db, u.id, full_name="Иван Петров", region="Курская область", telegram="",
                             consent=True, email="i.petrov@tpp.ru", phone="+7 900 000-00-00", **profile)
            q.set_user_org(db, u.id, "ООО «Станкозавод»", "4632000000")
            u = q.get_user(db, u.id)
            db.expunge(u)
            return u

    def _page(self, user, query="", **kw):
        with mock.patch.object(web, "current_user", return_value=user):
            return web.profile_page(_request(query), tab="plan", **kw).body.decode("utf-8")

    def _ask(self, user, **form):
        fields = dict(tariff="Стандарт", kind="individual", addon="", period="month", seats="1", trial_flag="",
                      consent="1")
        fields.update(form)
        with mock.patch.object(web, "current_user", return_value=user):
            return web.plan_request(_request(), **fields)

    def test_plan_tab_shows_the_same_catalog(self):
        html = self._page(self._user())
        notes = [_inline(n) for n in re.findall(r"<b>" + re.escape(pricing.ADDON) + r"</b>\s*<small>(.*?)</small>", html, re.S)]
        free = f"{pricing.ADDON_FREE}, далее + {pricing.PRICE} ₽/мес за {pricing.ADDON_SOURCE}"
        paid = f"+ {pricing.PRICE} ₽/мес за {pricing.ADDON_SOURCE}"
        self.assertEqual(notes, [free, paid, paid, paid, paid, paid], "опция в кабинете — не как на лендинге")
        want = [(p["name"], p["desc"]) for p in pricing.PLANS]
        self.assertEqual(_cards(_panel(html, "individual")), want + [("Для организаций", pricing.ORG_DESC)])
        self.assertEqual(_cards(_panel(html, "corporate")), want + [("Особые условия", pricing.SPECIAL_DESC)])
        for kind in ("individual", "corporate"):
            block = html[html.index(f'class="tariffs-fine" data-panel="{kind}"'):]
            block = block[:block.index("</div>")]
            self.assertEqual([_text(p) for p in re.findall(r"<p>(.*?)</p>", block, re.S)], pricing.FOOTNOTES[kind])
        self.assertIn('href="#tariffs">Сменить тариф', self._page(self._user_with_plan()))

    def _user_with_plan(self):
        from app.core import plans

        u = self._user()
        with self.Session() as db:
            q.set_user_plan(db, u.id, "Старт", plans.anchor_from_date(plans.msk_today()))
            u = q.get_user(db, u.id)
            db.expunge(u)
            return u

    def test_request_becomes_a_lead_linked_to_the_account(self):
        u = self._user()
        r = self._ask(u, tariff="Профи", kind="corporate", period="year", seats="5", addon="1")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/profile?tab=plan&requested=%D0%9F%D1%80%D0%BE%D1%84%D0%B8"))
        lead = self.leads()[0]
        self.assertEqual((lead.tariff, lead.user_id, lead.org, lead.inn, lead.email, lead.phone, lead.name),
                         ("Профи", u.id, "ООО «Станкозавод»", "4632000000", "i.petrov@tpp.ru", "+7 900 000-00-00",
                          "Иван Петров"))
        self.assertEqual(lead.options, "из личного кабинета · корпоративный · год · 5 польз. · "
                                       "доступ к источникам в ответах Навигатора")
        page = self._page(u, requested="Профи")
        self.assertIn("Заявка на тариф «Профи» отправлена", page)
        self.assertNotIn("Заявка на тариф «", self._page(u, requested="Безлимит"),
                         "подтверждение — только для тарифа из списка, а не для любого текста из адреса")

    def test_trial_request_and_bad_input(self):
        u = self._user()
        self._ask(u, tariff="Старт", trial_flag="1")
        self.assertEqual(self.leads()[0].options, "из личного кабинета · пробная неделя за 1 ₽")
        for form, err in ((dict(tariff="Безлимит"), "tariff"), (dict(tariff="Профи", trial_flag="1"), "options"),
                          (dict(tariff="Профи", kind="corporate", seats="9999"), "options"),
                          (dict(tariff="Профи", kind="corporate", seats="-3"), "options"),
                          (dict(tariff="Профи", kind="corporate", seats="²"), "options")):   # «²».isdigit() — 500
            with self.subTest(**form):
                r = self._ask(u, **form)
                # ревью PR #184: ошибка — редирект на вкладку (post/redirect/get), не страница на адресе API;
                # корпоративная возвращается на свою вкладку
                tab = "&kind=corporate" if form.get("kind") == "corporate" else ""
                self.assertEqual((r.status_code, r.headers["location"]), (303, f"/profile?tab=plan&err={err}{tab}#tariffs"))
                self.assertIn(web.PLAN_REQUEST_ERRORS[err], self._page(u, err=err))
        self.assertEqual(len(self.leads()), 1, "неверная заявка записана")
        self.assertNotIn('role="alert"', self._page(u, err="<script>"), "ошибка — только из списка кодов")

    def _without(self, **profile):
        u = self._user()
        with self.Session() as db:
            row = q.get_user(db, u.id)
            for k, v in profile.items():      # анкета согласие не снимает — ставим напрямую
                setattr(row, k, v)
            db.commit()
            u = q.get_user(db, u.id)
            db.expunge(u)
        return u

    def test_no_consent_or_contacts_no_lead(self):
        """Ревью PR #184 (152-ФЗ): заявка ставит время согласия. Согласие — галочкой на подтверждении
        (согласие анкеты дано на другую цель — тестирование); без контактов с человеком не связаться."""
        u = self._user()
        self.assertEqual(self._ask(u, consent="").headers["location"], "/profile?tab=plan&err=consent#tariffs")
        for profile in (dict(email=""), dict(phone="")):
            with self.subTest(**profile):
                r = self._ask(self._without(**profile))
                self.assertEqual(r.headers["location"], "/profile?tab=plan&err=profile#tariffs")
        self.assertEqual(self.leads(), [])
        self._ask(self._without(consent=False))      # согласие на заявку — своё, анкетное не нужно
        self.assertEqual(len(self.leads()), 1)

    def test_connect_opens_a_confirmation_with_consent(self):
        """«Подключить» — подтверждение: условия, данные из профиля, обязательная галочка согласия; заявку
        создаёт только оно. Ошибка и подтверждение — внутри витрины, к которой ведёт `#tariffs`."""
        u = self._user()
        html = self._page(u, "confirm=1&tariff=Профи&kind=corporate&period=year&seats=5&addon=1")
        box = html[html.index('class="tconfirm"'):html.index("</form>", html.index('class="tconfirm"'))]
        self.assertIn("Заявка на тариф «Профи»", box)
        self.assertIn("Корпоративный · год · 5 польз. · доступ к источникам в ответах Навигатора", box)
        self.assertIn("ООО «Станкозавод», ИНН 4632000000, i.petrov@tpp.ru, +7 900 000-00-00", _text(box))
        self.assertIn('name="consent" value="1" required', box)
        for k, v in (("tariff", "Профи"), ("kind", "corporate"), ("period", "year"), ("seats", "5"), ("addon", "1")):
            self.assertIn(f'name="{k}" value="{v}"', box)
        self.assertLess(html.index('id="tariffs"'), html.index('class="tconfirm"'), "подтверждение вне витрины")
        self.assertIn('data-panel="corporate" role="tabpanel">', html, "корпоративная вкладка не открыта")
        self.assertEqual(self.leads(), [], "подтверждение само заявку не создаёт")
        self.assertEqual(html.count('method="get" action="/profile#tariffs"'), 8, "карточка ведёт не на подтверждение")
        self.assertEqual(html.count('action="/api/plan-request"'), 1, "заявку шлёт что-то кроме подтверждения")

    def test_bad_choice_or_no_contacts_show_the_error_in_the_showcase(self):
        u = self._user()
        for query, err in (("confirm=1&tariff=Профи&kind=corporate&seats=", "options"),      # пустое поле — не «1»
                           ("confirm=1&tariff=Профи&trial=1", "options"), ("confirm=1&tariff=Безлимит", "tariff")):
            with self.subTest(query=query):
                html = self._page(u, query)
                self.assertNotIn('class="tconfirm"', html)
                self.assertLess(html.index('id="tariffs"'), html.index(web.PLAN_REQUEST_ERRORS[err]),
                                "ошибка над карточкой тарифа — за краем экрана при переходе к витрине")
        html = self._page(self._without(phone=""), "confirm=1&tariff=Старт&trial=1")
        self.assertNotIn('class="tconfirm"', html)
        self.assertIn(web.PLAN_REQUEST_ERRORS["profile"], html)

    def test_cabinet_lead_keeps_the_retention_promise(self):
        """Ревью PR #184: срок хранения заявок (12 месяцев) исполняется и на пути заявки из кабинета."""
        from datetime import timedelta

        from app.db.models import Lead, _utcnow

        with self.Session() as db:
            old = q.create_lead(db, tariff="Старт", name="Старый", org="О", inn="4632000000", email="o@x.ru",
                                phone="+7 900 000-00-00")
            db.query(Lead).filter(Lead.id == old.id).update({"created_at": _utcnow() - timedelta(days=400)})
            db.commit()
        self._ask(self._user())
        self.assertEqual([lead.name for lead in self.leads()], ["Иван Петров"], "просроченная заявка пережила новую")

    def test_rate_limit_and_anonymous(self):
        u = self._user()
        with mock.patch.object(web, "_plan_request_limit", web.SlidingWindow(1, window=3600.0)):
            self.assertEqual(self._ask(u).headers["location"], "/profile?tab=plan&requested=%D0%A1%D1%82%D0%B0%D0%BD%D0%B4%D0%B0%D1%80%D1%82")
            r = self._ask(u)
        self.assertEqual(r.headers["location"], "/profile?tab=plan&err=limit#tariffs")
        self.assertEqual(len(self.leads()), 1)
        with mock.patch.object(web, "current_user", return_value=None):
            r = web.plan_request(_request(), tariff="Стандарт")
        self.assertEqual(r.headers["location"], "/login")

    def test_admin_sees_the_options(self):
        u = self._user()
        self._ask(u, tariff="Старт", trial_flag="1", addon="1")
        admin = mock.Mock(id=99, role="admin", username="adm")
        with mock.patch.object(adm, "current_user", return_value=admin):
            html = adm.admin_leads_page(Request({"type": "http", "method": "GET", "path": "/admin/leads", "headers": [],
                                                 "query_string": b"", "session": {}, "app": None})).body.decode("utf-8")
        self.assertIn("тариф «Старт» · из личного кабинета · пробная неделя за 1 ₽ · доступ к источникам в ответах Навигатора", html)
        self.assertIn(f'href="/admin/users/{u.id}">kursk.user1</a>', html, "заявка из кабинета — уже с учёткой")
