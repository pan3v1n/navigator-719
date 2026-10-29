"""Организация по ИНН из ЕГРЮЛ через DaData (решение владельца 29.10.2026): модуль поиска, адрес
`/api/org-lookup`, кабинет и карточка пользователя в `/admin`. Офлайн: DaData подменена `httpx.MockTransport`.

Главное, что держат тесты: ИНН из 12 цифр (ИП, физлицо — ПДн) наружу не уходит НИКОГДА; «не проверено»
не выдаётся за «не найдена» и не кэшируется; из ответа не берётся ФИО руководителя."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import admin as adm  # noqa: E402
from app.api import org_lookup as api  # noqa: E402
from app.api import web  # noqa: E402
from app.api.ratelimit import SlidingWindow  # noqa: E402
from app.core import org_registry as reg  # noqa: E402
from app.db import queries as q  # noqa: E402
from tests.test_profile import _CabinetDB, _request  # noqa: E402

INN = "7707083893"            # организация, контрольные цифры верны
INN_IP = "500100732259"       # ИП — 12 цифр, контрольные цифры верны
INN_BAD = "7707083894"        # опечатка: контрольная цифра не сходится
DIRECTOR = "Грефов Герман Оскарович"

PARTY = {"suggestions": [{
    "value": 'ПАО СБЕРБАНК',
    "data": {"inn": INN, "kpp": "773601001", "ogrn": "1027700132195", "type": "LEGAL", "branch_type": "MAIN",
             "name": {"short_with_opf": 'ПАО СБЕРБАНК', "full_with_opf": 'ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "СБЕРБАНК РОССИИ"',
                      "short": "СБЕРБАНК", "full": "СБЕРБАНК РОССИИ"},
             "state": {"status": "ACTIVE", "liquidation_date": None},
             "management": {"name": DIRECTOR, "post": "ПРЕЗИДЕНТ"},
             "address": {"value": "г Москва, ул Вавилова, д 19"}},
}]}


class _DaData(unittest.TestCase):
    """Подменённая DaData: ответ — `self.reply` (dict, код ответа или исключение), запросы — в `self.calls`."""

    def setUp(self):
        self.calls: list[httpx.Request] = []
        self.reply = PARTY

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            if isinstance(self.reply, Exception):
                raise self.reply
            if isinstance(self.reply, int):
                return httpx.Response(self.reply, json={"message": "x"})
            if isinstance(self.reply, str):
                return httpx.Response(200, text=self.reply)
            return httpx.Response(200, json=self.reply)

        for p in (mock.patch.object(reg, "_transport", httpx.MockTransport(handler)),
                  mock.patch.object(reg.settings, "DADATA_API_KEY", "test-key")):
            p.start()
            self.addCleanup(p.stop)
        reg.clear_cache()
        self.addCleanup(reg.clear_cache)


class TestLookup(_DaData):
    def test_found_takes_requisites_only(self):
        res = reg.lookup(INN)
        self.assertEqual(res.outcome, reg.FOUND)
        o = res.org
        self.assertEqual((o.name, o.kpp, o.ogrn, o.address), ('ПАО СБЕРБАНК', "773601001", "1027700132195",
                                                              "г Москва, ул Вавилова, д 19"))
        self.assertTrue(o.active)
        self.assertEqual(o.status_text, "действующая")
        self.assertNotIn(DIRECTOR, repr(o), "ФИО руководителя — ПДн, сервису не нужно")

    def test_request_shape(self):
        reg.lookup(INN)
        (r,) = self.calls
        self.assertEqual(str(r.url), reg.PARTY_URL)
        self.assertEqual(r.headers["Authorization"], "Token test-key")
        self.assertEqual(json.loads(r.content), {"query": INN, "type": "LEGAL", "branch_type": "MAIN", "count": 1})

    def test_ip_inn_never_leaves(self):
        """Решение владельца: ИНН ИП и физлица (ПДн) не отправляется — ни в каком виде."""
        for inn in (INN_IP, f" {INN_IP} ", INN_BAD, "770708389", "", "77070838９3"):
            with self.subTest(inn=inn):
                self.assertIsNone(reg.lookup(inn))
        self.assertEqual(self.calls, [], "номер ушёл в DaData")

    def test_disabled_without_key(self):
        with mock.patch.object(reg.settings, "DADATA_API_KEY", ""):
            self.assertFalse(reg.enabled())
            self.assertIsNone(reg.lookup(INN))
        self.assertEqual(self.calls, [])

    def test_not_found_is_cached(self):
        self.reply = {"suggestions": []}
        self.assertEqual(reg.lookup(INN).outcome, reg.NOT_FOUND)
        self.assertEqual(reg.lookup(INN).outcome, reg.NOT_FOUND)
        self.assertEqual(len(self.calls), 1, "повтор того же ИНН — из кэша")

    def test_unavailable_is_not_not_found_and_not_cached(self):
        """«Не проверено» об организации не говорит ничего: не «не найдена» и не на сутки в кэш."""
        for reply in (401, 403, 429, 500, httpx.ConnectTimeout("t"), httpx.ConnectError("x"),
                      "<html>не json</html>", {"oops": 1}, {"suggestions": "x"}, {"suggestions": [None]}):
            with self.subTest(reply=repr(reply)[:30]):
                reg.clear_cache()
                self.calls.clear()
                self.reply = reply
                self.assertEqual(reg.lookup(INN).outcome, reg.UNAVAILABLE)
                self.reply = PARTY
                self.assertEqual(reg.lookup(INN).outcome, reg.FOUND, "сбой закэшировался")
                self.assertEqual(len(self.calls), 2)

    def test_foreign_record_is_not_found_org(self):
        other = json.loads(json.dumps(PARTY))
        other["suggestions"][0]["data"]["inn"] = "4632000002"
        self.reply = other
        res = reg.lookup(INN)
        self.assertEqual(res.outcome, reg.UNAVAILABLE, "чужая запись выдана за найденную по нашему ИНН")
        self.assertIsNone(res.org)

    def test_found_cached_until_ttl(self):
        reg.lookup(INN, now=1000.0)
        reg.lookup(INN, now=1000.0 + reg.CACHE_TTL_S - 1)
        self.assertEqual(len(self.calls), 1)
        reg.lookup(INN, now=1000.0 + reg.CACHE_TTL_S + 1)
        self.assertEqual(len(self.calls), 2, "просроченная запись кэша")

    def test_liquidated(self):
        dead = json.loads(json.dumps(PARTY))
        dead["suggestions"][0]["data"]["state"] = {"status": "LIQUIDATED", "liquidation_date": 1615507200000}
        self.reply = dead
        o = reg.lookup(INN).org
        self.assertFalse(o.active)
        self.assertEqual(o.status_text, "ликвидирована 12.03.2021")

    def test_unknown_status_is_not_active(self):
        odd = json.loads(json.dumps(PARTY))
        odd["suggestions"][0]["data"]["state"] = {"status": "SOMETHING_NEW"}
        self.reply = odd
        o = reg.lookup(INN).org
        self.assertFalse(o.active)
        self.assertEqual(o.status_text, "")


class TestNameMatches(unittest.TestCase):
    org = reg.Org(inn=INN, name='ООО "РОМАШКА"', full_name='ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "РОМАШКА"',
                  kpp="", ogrn="", status="ACTIVE", liquidated_on="", address="",
                  names=('ООО "РОМАШКА"', 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "РОМАШКА"', "РОМАШКА"))

    def test_same_name_other_spelling(self):
        for name in ("ООО «Ромашка»", "ооо  ромашка", "Ромашка", "Общество с ограниченной ответственностью «Ромашка»"):
            with self.subTest(name=name):
                self.assertTrue(reg.name_matches(self.org, name))

    def test_other_name(self):
        for name in ("ООО «Ромашка-Курск»", "ООО «Лютик»", "", "   "):
            with self.subTest(name=name):
                self.assertFalse(reg.name_matches(self.org, name))


class TestEndpoint(_DaData):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from app.api import auth
        from main import app

        self.client = TestClient(app)
        self.user = mock.Mock(id=7, role="user")
        p = mock.patch.object(auth, "current_user", side_effect=lambda r: self.user)
        p.start()
        self.addCleanup(p.stop)
        lim = mock.patch.object(api, "_limit", SlidingWindow(api.LOOKUPS_PER_HOUR, window=3600.0))
        lim.start()
        self.addCleanup(lim.stop)

    def post(self, **body):
        return self.client.post("/api/org-lookup", json=body)

    def test_requires_login(self):
        self.user = None
        self.assertEqual(self.post(inn=INN).status_code, 401)
        self.assertEqual(self.calls, [])

    def test_found_with_match(self):
        r = self.post(inn=INN, name="ПАО «Сбербанк»")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["outcome"], "found")
        self.assertEqual(d["org"]["name"], "ПАО СБЕРБАНК")
        self.assertEqual(d["org"]["status_text"], "действующая")
        self.assertTrue(d["matches"])
        self.assertNotIn(DIRECTOR, r.text)
        self.assertFalse(self.post(inn=INN, name="ООО «Лютик»").json()["matches"])
        self.assertIsNone(self.post(inn=INN).json()["matches"], "сверять не с чем")

    def test_ip_inn_and_typo_rejected_before_dadata(self):
        r = self.post(inn=INN_IP, name="ИП Петров")
        self.assertEqual(r.status_code, 422)
        self.assertIn("ИП", r.json()["detail"])
        self.assertEqual(self.post(inn=INN_BAD).status_code, 422)
        self.assertEqual(self.post(inn="").status_code, 422)
        self.assertEqual(self.calls, [], "номер ушёл в DaData")

    def test_disabled_and_unavailable(self):
        with mock.patch.object(reg.settings, "DADATA_API_KEY", ""):
            self.assertEqual(self.post(inn=INN).json(), {"outcome": "unavailable"})
        self.reply = 403
        self.assertEqual(self.post(inn=INN).json(), {"outcome": "unavailable"})

    def test_not_found(self):
        self.reply = {"suggestions": []}
        self.assertEqual(self.post(inn=INN).json(), {"outcome": "not_found"})

    def test_rate_limited(self):
        with mock.patch.object(api, "_limit", SlidingWindow(2, window=3600.0)):
            self.assertEqual(self.post(inn=INN).status_code, 200)
            self.assertEqual(self.post(inn=INN).status_code, 200)
            r = self.post(inn=INN)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["outcome"], "unavailable")
        self.assertIn("Retry-After", r.headers)


class TestPages(_CabinetDB):
    """Поиск виден, только если задан ключ: в кабинете — при правке анкеты, в карточке — по ИНН организации."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(reg.settings, "DADATA_API_KEY", "test-key")
        p.start()
        self.addCleanup(p.stop)

    def _card(self, uid):
        admin = mock.Mock(id=99, role="admin", username="adm")
        with mock.patch.object(adm, "current_user", return_value=admin):
            return adm.admin_user_card(_request("/admin"), uid).body.decode("utf-8")

    def test_cabinet_edit_has_lookup(self):
        u = self._user()
        html = self._page(u, edit="1")
        self.assertIn('id="inn-lookup"', html)
        self.assertIn('fetch("/api/org-lookup"', html)
        self.assertIn('id="org-input"', html)
        self.assertNotIn('id="inn-lookup"', self._page(u), "в просмотре анкеты поле не правится — искать незачем")
        with mock.patch.object(reg.settings, "DADATA_API_KEY", ""):
            html = self._page(u, edit="1")
        self.assertNotIn('id="inn-lookup"', html)
        self.assertNotIn("/api/org-lookup", html)

    def test_first_fill_has_lookup(self):
        """Анкета до чата (гейт профиля) открыта на правку — поиск там тоже."""
        self.assertIn('id="inn-lookup"', self._page(self._user(complete=False)))

    def test_admin_card(self):
        u = self._user()
        with self.Session() as db:
            q.set_user_org(db, u.id, "ПАО «Сбербанк»", INN)
        card = self._card(u.id)
        self.assertIn(f'id="egrul" data-inn="{INN}" data-name="ПАО «Сбербанк»"', card)
        self.assertIn('id="adm-org"', card)
        with mock.patch.object(reg.settings, "DADATA_API_KEY", ""):
            self.assertNotIn('id="egrul"', self._card(u.id))

    def test_admin_card_ip_inn_not_looked_up(self):
        u = self._user()
        with self.Session() as db:
            q.set_user_org(db, u.id, "ИП Петров", INN_IP)
        card = self._card(u.id)
        self.assertNotIn('id="egrul"', card)
        self.assertIn("в реестре автоматически не ищется", card)

    def test_policy_names_dadata(self):
        policy = web.templates.get_template("privacy.html").render(
            **web._ctx(_request(), page_title="", active="privacy", updated="", user=None))
        self.assertIn("DaData", policy)
        self.assertIn("ООО «Дейта Кью»", policy)       # оператор по ЕГРЮЛ (ИНН 7721581040), сверено 29.10.2026
        self.assertIn("ИНН индивидуального предпринимателя", policy)


if __name__ == "__main__":
    unittest.main()
