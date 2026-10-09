"""Юнит-тесты профиля + согласия на ПДн (Workstream B): справочник регионов, гейт профиля
(profile_complete/needs_profile) и сохранение профиля (update_profile, фиксация consent_at)."""

import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.auth import needs_profile, profile_complete  # noqa: E402
from app.core.regions import REGIONS, region_from_username  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base, User  # noqa: E402


class TestRegionPrefix(unittest.TestCase):
    def test_known_prefixes(self):
        self.assertEqual(region_from_username("moscow.expert1"), "Москва")
        self.assertEqual(region_from_username("mosobl.expert1"), "Московская область")
        self.assertEqual(region_from_username("irkutsk.expert2"), "Иркутская область")

    def test_unknown_or_dotless(self):
        self.assertEqual(region_from_username("expert1"), "")     # бесточечный (легаси)
        self.assertEqual(region_from_username("admin"), "")
        self.assertEqual(region_from_username("atlantis.expert1"), "")  # неизвестный префикс
        self.assertEqual(region_from_username(""), "")

    def test_regions_list_nonempty_and_prefill_present(self):
        self.assertIn("Москва", REGIONS)
        self.assertIn("Пермский край", REGIONS)


class TestProfileGate(unittest.TestCase):
    def _user(self, **kw):
        base = dict(username="x.expert1", role="user", consent=True,
                    full_name="Иван Иванов", region="Москва", telegram="@ivan")
        base.update(kw)
        return User(**base)

    def test_complete_user_passes(self):
        u = self._user()
        self.assertTrue(profile_complete(u))
        self.assertFalse(needs_profile(u))

    def test_telegram_is_optional(self):
        # решение владельца 09.10.2026: Telegram — по желанию; анкета без него не должна
        # возвращать человека на себя же
        u = self._user(telegram=None)
        self.assertTrue(profile_complete(u))
        self.assertFalse(needs_profile(u))

    def test_missing_name_or_region_gated(self):
        self.assertTrue(needs_profile(self._user(full_name=None)))
        self.assertTrue(needs_profile(self._user(region=None)))

    def test_existing_user_without_email_and_phone_keeps_working(self):
        # «спрашивать при сохранении»: учётки, заполнившие анкету до появления email и телефона,
        # в чат ходят как раньше (у _user их нет вовсе)
        u = self._user()
        self.assertIsNone(u.email)
        self.assertFalse(needs_profile(u))

    def test_no_consent_gated(self):
        u = self._user(consent=False)
        self.assertTrue(needs_profile(u))

    def test_expert_and_admin_exempt(self):
        # внутренние роли профиль не заполняют, даже с пустыми полями
        self.assertFalse(needs_profile(self._user(role="expert", consent=False,
                                                  full_name=None, region=None, telegram=None)))
        self.assertFalse(needs_profile(self._user(role="admin", consent=False,
                                                  full_name=None, region=None, telegram=None)))


class TestUpdateProfile(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_update_and_consent_timestamp_once(self):
        with self.Session() as db:
            u = q.create_user(db, "moscow.expert1", "h", role="user")
            self.assertFalse(profile_complete(u))  # свежий аккаунт не заполнен

            u = q.update_profile(db, u.id, full_name="Иван Иванов", region="Москва",
                                 telegram="@ivan", consent=True)
            self.assertTrue(profile_complete(u))
            self.assertIsNotNone(u.consent_at)
            first_ts = u.consent_at

            # повторное сохранение (правка ника) НЕ сбрасывает момент согласия
            u = q.update_profile(db, u.id, full_name="Иван Иванов", region="Москва",
                                 telegram="@ivan2", consent=True)
            self.assertEqual(u.consent_at, first_ts)
            self.assertEqual(u.telegram, "@ivan2")

    def test_contacts_none_keeps_empty_clears(self):
        """Контакты кабинета: None — не трогать (старые вызовы без них ничего не стирают),
        пустая строка — очистить (пользователь стёр поле в форме)."""
        with self.Session() as db:
            u = q.create_user(db, "kursk.user1", "h", role="user")
            base = dict(full_name="Иван Иванов", region="Курская область", telegram="@ivan", consent=True)
            u = q.update_profile(db, u.id, position="Эксперт", email="i@tpp.ru",
                                 phone="+7 900 000-00-00", **base)
            self.assertEqual((u.position, u.email, u.phone), ("Эксперт", "i@tpp.ru", "+7 900 000-00-00"))
            u = q.update_profile(db, u.id, **base)            # вызов без контактов — как до кабинета
            self.assertEqual((u.position, u.email, u.phone), ("Эксперт", "i@tpp.ru", "+7 900 000-00-00"))
            u = q.update_profile(db, u.id, position="", email=" ", phone="", **base)
            self.assertEqual((u.position, u.email, u.phone), (None, None, None))

    def test_profile_update_never_touches_org(self):
        with self.Session() as db:
            u = q.create_user(db, "kursk.user2", "h", role="user")
            q.set_user_org(db, u.id, "Союз «Курская ТПП»", "4632000000")
            u = q.update_profile(db, u.id, full_name="Иван Иванов", region="Курская область",
                                 telegram="@ivan", consent=True, email="i@tpp.ru")
            self.assertEqual((u.org, u.inn), ("Союз «Курская ТПП»", "4632000000"))
            u = q.set_user_org(db, u.id, "  ", "")
            self.assertEqual((u.org, u.inn), (None, None))
            self.assertIsNone(q.set_user_org(db, 999, "X", ""))


# --------------------------------------------------------------------------- #
# Личный кабинет (макет account, решения владельца 09.10.2026): страница, сохранение, admin.
# --------------------------------------------------------------------------- #
from datetime import timedelta  # noqa: E402
from unittest import mock  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.api import admin as adm  # noqa: E402
from app.api import web  # noqa: E402
from app.core import plans  # noqa: E402
from app.db.models import AnswerUsage  # noqa: E402

COMPLETE = dict(full_name="Иван Петров", region="Курская область", telegram="@ivan", consent=True,
                email="i.petrov@tpp.ru", phone="+7 900 000-00-00")


def _request(path="/profile"):
    return Request({"type": "http", "method": "GET", "path": path, "headers": [],
                    "query_string": b"", "session": {}, "app": None})


class _CabinetDB(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        for p in (mock.patch.object(web, "get_session", self.Session),
                  mock.patch.object(adm, "get_session", self.Session)):
            p.start()
            self.addCleanup(p.stop)

    def _user(self, role="user", complete=True, plan=None, **profile):
        with self.Session() as db:
            u = q.create_user(db, f"kursk.{role}{len(q.list_users(db))}", "h", role=role)
            if complete:
                q.update_profile(db, u.id, **{**COMPLETE, **profile})
            if plan:
                q.set_user_plan(db, u.id, plan, plans.anchor_from_date(plans.msk_today()))
            return self._fresh(u.id)

    def _fresh(self, uid):
        with self.Session() as db:
            u = q.get_user(db, uid)
            db.expunge(u)
            return u

    def _page(self, user, **kw):
        with mock.patch.object(web, "current_user", return_value=user):
            return web.profile_page(_request(), **kw).body.decode("utf-8")

    def _submit(self, user, **form):
        fields = dict(consent="1", full_name=COMPLETE["full_name"], region=COMPLETE["region"],
                      telegram=COMPLETE["telegram"], position="", email=COMPLETE["email"],
                      phone=COMPLETE["phone"])
        fields.update(form)
        with mock.patch.object(web, "current_user", return_value=user):
            return web.profile_submit(_request(), **fields)


class TestCabinetPage(_CabinetDB):
    def test_every_role_gets_the_cabinet(self):
        """Кабинет — у всех трёх ролей (раньше ссылка была только у `user`)."""
        for role in ("user", "expert", "admin"):
            with self.subTest(role=role):
                html = self._page(self._user(role=role))
                self.assertIn("Данные пользователя", html)
                self.assertIn('href="/logout"', html, "кнопка «Выйти» из макета")
                self.assertIn('href="/profile?tab=plan"', html)
                self.assertIn('href="/profile?edit=1">Редактировать', html)

    def test_filled_profile_is_locked_until_edit(self):
        """Просьба владельца 09.10.2026: форма — просмотр, правка по «Редактировать»; обязательные поля и
        согласие — со звёздочкой, согласие обязательно."""
        u = self._user()
        locked = self._page(u)
        self.assertIn('<fieldset class="fields-lock" disabled>', locked)
        self.assertNotIn('type="submit"', locked[locked.index('<form class="panel"'):], "в просмотре форму можно отправить")
        edit = self._page(u, edit="1")
        self.assertIn('<fieldset class="fields-lock">', edit)
        self.assertIn("Сохранить изменения", edit)
        self.assertIn('<a class="ds-link" href="/profile">Отменить</a>', edit)
        for label in ("ФИО", "Рабочий email", "Телефон", "Регион"):
            self.assertRegex(edit, label + r' <b class="req" aria-hidden="true">\*</b></span>')
        self.assertNotRegex(edit, r'Должность <b class="req"', "необязательное поле со звёздочкой")
        consent = edit[edit.index('name="consent"'):]
        self.assertIn('value="1"  required>', consent[:80].replace("checked", ""))
        self.assertIn('<b class="req" aria-hidden="true">*</b> Я даю согласие', consent)

    def test_form_opens_by_itself_when_there_is_something_to_fix(self):
        self.assertIn('<fieldset class="fields-lock">', self._page(self._user(complete=False)), "гейт до чата закрыт")
        r = self._submit(self._user(), consent="")
        self.assertIn('<fieldset class="fields-lock">', r.body.decode("utf-8"), "ошибка сохранения в закрытой форме")

    def test_org_and_inn_are_read_only(self):
        u = self._user()
        with self.Session() as db:
            q.set_user_org(db, u.id, "Союз «Курская ТПП»", "4632000000")
        html = self._page(self._fresh(u.id))
        self.assertIn("Союз «Курская ТПП» · ИНН 4632000000", html, "организация под именем, как в макете")
        for value in ("Союз «Курская ТПП»", "4632000000"):
            tag = html[html.rindex("<input", 0, html.index(f'value="{value}"')):]
            tag = tag[:tag.index(">")]
            self.assertIn("disabled", tag, f"поле «{value}» редактируемо")
            self.assertNotIn("name=", tag, f"поле «{value}» уходит с формой")
        self.assertEqual(html.count("Меняется через администратора сервиса"), 2)

    def test_first_fill_keeps_the_gate_wording(self):
        """Пока профиль не заполнен, кабинет — это гейт: уйти к сервису некуда."""
        html = self._page(self._user(complete=False))
        self.assertIn("Сохранить и продолжить", html)
        self.assertNotIn("Вернуться к сервису", html)

    def test_plan_tab_shows_usage_and_change_link(self):
        u = self._user(plan="Старт")
        with self.Session() as db:
            ts = plans.anchor_from_date(plans.msk_today()) + timedelta(minutes=1)
            db.add_all([AnswerUsage(user_id=u.id, ts=ts) for _ in range(37)])
            db.commit()
        with mock.patch.object(web.settings, "PUBLIC_DOMAIN", "xn--719--83dani8b8bqyy.xn--p1ai"):
            html = self._page(u, tab="plan")
        self.assertIn("Использовано 37 из 100 запросов", html)
        self.assertIn("Лимит обновится", html)
        self.assertIn('href="#tariffs">Сменить тариф', html, "витрина тарифов — на этой же вкладке (09.10.2026)")
        self.assertNotIn("Данные пользователя", html, "вкладка «Тариф» не рисует форму профиля")
        for absent in ("Автопродление", "Документы", "Скачать PDF", "Изменить реквизиты"):
            self.assertNotIn(absent, html, "биллинга нет — заглушек макета быть не должно")

    def test_plan_tab_without_plan(self):
        # домен задан явно: без него ссылки на лендинг нет вовсе, и тест не зависел бы от тарифа
        with mock.patch.object(web.settings, "PUBLIC_DOMAIN", "xn--719--83dani8b8bqyy.xn--p1ai"):
            html = self._page(self._user(role="expert"), tab="plan")
        self.assertIn("Без тарифа", html)
        self.assertNotIn("Сменить тариф", html)

    def test_unknown_tab_falls_back_to_profile(self):
        self.assertIn("Данные пользователя", self._page(self._user(), tab="<script>"))


class TestCabinetSave(_CabinetDB):
    def test_first_fill_goes_to_chat_edit_stays(self):
        u = self._user(complete=False)
        r = self._submit(u)
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/chat"), "гейт: после анкеты — в работу")
        r = self._submit(self._fresh(u.id), position="Эксперт", email="i@tpp.ru", phone="+7 (900) 000-00-00")
        self.assertEqual(r.headers["location"], "/profile?saved=1", "правка в кабинете — остаёмся на месте")
        u = self._fresh(u.id)
        self.assertEqual((u.position, u.email, u.phone), ("Эксперт", "i@tpp.ru", "+7 (900) 000-00-00"))
        self.assertIn("Изменения сохранены", self._page(u, saved="1"))

    def test_internal_roles_stay_in_cabinet(self):
        for role in ("expert", "admin"):
            with self.subTest(role=role):
                r = self._submit(self._user(role=role, complete=False))
                self.assertEqual(r.headers["location"], "/profile?saved=1")

    def test_required_fields_still_gate(self):
        u = self._user(complete=False)
        for missing in ("consent", "full_name", "region", "email", "phone"):
            with self.subTest(missing=missing):
                r = self._submit(u, **{missing: ""})
                self.assertEqual(r.status_code, 400)
                self.assertIn("Заполните ФИО, регион, рабочий email и телефон", r.body.decode("utf-8"))
        self.assertFalse(profile_complete(self._fresh(u.id)), "непрошедшая форма ничего не сохранила")

    def test_telegram_optional_email_phone_required_in_form(self):
        u = self._user(complete=False)
        r = self._submit(u, telegram="")
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/chat"), "без Telegram анкета не прошла")
        self.assertIsNone(self._fresh(u.id).telegram)
        html = self._page(self._fresh(u.id))
        for name, required in (("email", True), ("phone", True), ("telegram", False), ("position", False)):
            tag = html[html.index(f'name="{name}"'):]
            tag = tag[:tag.index(">")]
            self.assertEqual("required" in tag, required, f"поле {name}")

    def test_existing_user_must_add_contacts_on_save(self):
        u = self._user(email=None, phone=None)            # заполнял анкету до появления полей
        r = self._submit(u, email="", phone="")
        self.assertEqual(r.status_code, 400, "сохранение без email и телефона прошло")

    def test_bad_contacts_rerender_with_input(self):
        u = self._user()
        for field, value, msg in (("email", "не-почта", "Проверьте правильность email"),
                                  ("phone", "12345", "Укажите телефон полностью"),
                                  ("position", "д" * 129, "Слишком длинное название должности"),
                                  ("telegram", "@" + "t" * 128, "Слишком длинное ФИО или ник")):
            with self.subTest(field=field):
                r = self._submit(u, **{field: value})
                body = r.body.decode("utf-8")
                self.assertEqual(r.status_code, 400)
                self.assertIn(msg, body)
                self.assertIn(f'value="{value}"', body, "введённое не должно теряться")
                self.assertIn("Вернуться к сервису", body, "ре-рендер ошибки теряет выход (ревью 08.10)")
        self.assertEqual(self._fresh(u.id).email, COMPLETE["email"], "ошибочная форма изменила email")

    def test_region_only_from_the_list(self):
        u = self._user()
        r = self._submit(u, region="Курская обл")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Выберите регион из списка", r.body.decode("utf-8"))
        self.assertEqual(self._fresh(u.id).region, COMPLETE["region"], "регион вне справочника сохранён")

    def test_region_combobox_keeps_select_fallback(self):
        """Поиск по региону (просьба владельца 09.10.2026) — поверх обычного <select>: без JS
        работает он, и значение формы всегда уходит из него."""
        html = self._page(self._user())
        self.assertIn('<select name="region" id="region-select"', html)
        self.assertIn('id="region-input" type="text" role="combobox"', html)
        self.assertIn('<option value="Курская область" selected>', html)
        script = html[html.index('getElementById("region-select")'):]
        self.assertIn('replace(/ё/g, "е")', script, "«ё» и «е» в поиске различаются")
        self.assertIn("words.every", script, "совпадение не по каждому слову запроса")
        self.assertIn('sel.required = false', script, "скрытый обязательный select блокирует отправку")
        self.assertNotIn('id="region-input"', self._page(self._user(), tab="plan"))

    def test_position_hint_fits_the_field(self):
        """Замечание владельца: подсказка «Должности» не помещалась в поле (ширина колонки ~220 px)."""
        html = self._page(self._user())
        tag = html[html.index('name="position"'):]
        hint = tag[tag.index('placeholder="') + 13:]
        hint = hint[:hint.index('"')]
        self.assertLessEqual(len(hint), 20, hint)

    def test_form_cannot_write_org(self):
        """Организацию и ИНН ведёт admin: поля `org`/`inn` в POST /profile отбрасываются."""
        from fastapi.testclient import TestClient

        from main import app

        u = self._user()
        with self.Session() as db:
            q.set_user_org(db, u.id, "Союз «Курская ТПП»", "4632000000")
        u = self._fresh(u.id)
        with mock.patch.object(web, "current_user", return_value=u):
            r = TestClient(app).post("/profile", follow_redirects=False, data={
                "consent": "1", **{k: v for k, v in COMPLETE.items() if k != "consent"},
                "org": "ООО «Чужая»", "inn": "7700000000"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual((self._fresh(u.id).org, self._fresh(u.id).inn), ("Союз «Курская ТПП»", "4632000000"))


class TestAdminSetsOrg(_CabinetDB):
    def setUp(self):
        super().setUp()
        self.uid = self._user().id
        self.admin = mock.Mock(id=1)

    def _set(self, org, inn, uid=None):
        return adm.admin_set_org(uid or self.uid, org=org, inn=inn, admin=self.admin)

    def test_assign_and_clear(self):
        self.assertEqual(self._set(" Союз «Курская ТПП» ", "4632000000").status_code, 303)
        self.assertEqual((self._fresh(self.uid).org, self._fresh(self.uid).inn), ("Союз «Курская ТПП»", "4632000000"))
        self._set("ИП Петров", "463200000000")                       # 12 цифр — ИНН ИП
        self.assertEqual(self._fresh(self.uid).inn, "463200000000")
        self._set("", "")
        self.assertEqual((self._fresh(self.uid).org, self._fresh(self.uid).inn), (None, None))

    def test_rejects_bad_input(self):
        for org, inn in (("ТПП", "12345"), ("ТПП", "46320000001"), ("ТПП", "463200000a"), ("x" * 301, "")):
            with self.subTest(org=org[:10], inn=inn), self.assertRaises(HTTPException) as cm:
                self._set(org, inn)
            self.assertEqual(cm.exception.status_code, 422)
        self.assertIsNone(self._fresh(self.uid).org)

    def test_missing_user_is_404(self):
        with self.assertRaises(HTTPException) as cm:
            self._set("ТПП", "", uid=999)
        self.assertEqual(cm.exception.status_code, 404)

    def test_route_requires_admin(self):
        from fastapi.testclient import TestClient

        from main import app

        client = TestClient(app)
        r = client.post(f"/api/admin/users/{self.uid}/org", data={"org": "X"}, follow_redirects=False)
        self.assertIn(r.status_code, (401, 403), "смена организации доступна без входа")
        from app.api import auth
        for role in ("user", "expert"):   # вошёл, но не admin — ровно тот, кто захочет «поправить» себе ИНН
            with self.subTest(role=role), \
                 mock.patch.object(auth, "current_user", return_value=self._fresh(self.uid) if role == "user"
                                   else self._user(role="expert")):
                r = client.post(f"/api/admin/users/{self.uid}/org", data={"org": "X", "inn": ""},
                                follow_redirects=False)
                self.assertEqual(r.status_code, 403)
        self.assertIsNone(self._fresh(self.uid).org)

    def test_admin_page_has_org_form(self):
        self._set("Союз «Курская ТПП»", "4632000000")
        admin = mock.Mock(id=99, role="admin", username="adm")
        with mock.patch.object(adm, "current_user", return_value=admin):
            card = adm.admin_user_card(_request("/admin"), self.uid).body.decode("utf-8")
            listing = adm.admin_users(_request("/admin/users")).body.decode("utf-8")
        self.assertIn(f'action="/api/admin/users/{self.uid}/org"', card)
        self.assertIn('value="Союз «Курская ТПП»"', card)
        self.assertIn('value="4632000000"', card)
        self.assertIn("ИНН 4632000000", listing, "организация в списке пользователей")


class TestAdminCreatesAccount(_CabinetDB):
    """Учётки заводит admin (решение владельца 09.10.2026: регистрации пока нет). Пароль генерирует
    сервер и показывает ОДИН раз; в базе и журнале его нет."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from main import app
        from app.api import auth

        self.client = TestClient(app)
        self.admin = self._user(role="admin")
        self.as_user = self.admin
        for p in (mock.patch.object(auth, "current_user", side_effect=lambda r: self.as_user),
                  mock.patch.object(adm, "current_user", side_effect=lambda r: self.as_user)):
            p.start()
            self.addCleanup(p.stop)

    def _post(self, username, role="user"):
        return self.client.post("/api/admin/users", data={"username": username, "role": role})

    def _by_login(self, login):
        with self.Session() as db:
            return q.get_user_by_username(db, login)

    def test_creates_with_one_time_password(self):
        from app.api.auth import verify_password

        r = self._post(" Kursk.Expert2 ")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Учётка создана.", r.text)             # ответ — карточка новой учётки
        self.assertIn("<h1>kursk.expert2</h1>", r.text)
        self.assertIn("Региональный участник", r.text)
        pw = r.text.split('id="new-password">')[1].split("<")[0]
        self.assertGreaterEqual(len(pw), 12)
        u = self._by_login("kursk.expert2")
        self.assertEqual(u.role, "user")
        self.assertTrue(verify_password(pw, u.password_hash), "показанный пароль не подходит")
        self.assertNotIn(pw, u.password_hash, "пароль лежит в базе открыто")
        r2 = self._post("kursk.expert3")
        self.assertNotEqual(pw, r2.text.split('id="new-password">')[1].split("<")[0])

    def test_password_never_logged(self):
        from loguru import logger

        lines = []
        sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
        self.addCleanup(logger.remove, sink)
        r = self._post("perm.expert1", role="expert")
        pw = r.text.split('id="new-password">')[1].split("<")[0]
        log = "".join(lines)
        self.assertIn("perm.expert1", log, "создание учётки не попало в журнал")
        self.assertNotIn(pw, log, "пароль записан в журнал")

    def test_bad_input_creates_nothing(self):
        existing = self._user()
        with self.Session() as db:
            before = len(q.list_users(db))
        for login, role, msg in ((existing.username, "user", "уже занят"),
                                 ("иван", "user", "Логин — латинские"),
                                 ("ab", "user", "Логин — латинские"),
                                 ("a b c", "user", "Логин — латинские"),
                                 ("new.user1", "root", "Неизвестная роль")):
            with self.subTest(login=login, role=role):
                r = self._post(login, role)
                self.assertEqual(r.status_code, 400)
                self.assertIn(msg, r.text)
                self.assertNotIn('id="new-password"', r.text)
        with self.Session() as db:
            self.assertEqual(len(q.list_users(db)), before)

    def test_only_admin_creates(self):
        self.as_user = None
        self.assertEqual(self._post("x.expert1").status_code, 401)
        for role in ("user", "expert"):
            with self.subTest(role=role):
                self.as_user = self._user(role=role)
                self.assertEqual(self._post(f"{role}.made1").status_code, 403)
        self.assertIsNone(self._by_login("user.made1"))
        self.as_user = self.admin
        html = self.client.get("/admin/users").text
        self.assertIn('action="/api/admin/users"', html)
        self.assertIn('<option value="expert"', html)


class TestCabinetLinksAndPolicy(unittest.TestCase):
    def _render(self, name, **kw):
        return web.templates.get_template(name).render(**web._ctx(_request(), **kw))

    def test_chat_links_cabinet_for_every_role(self):
        for role in ("user", "expert", "admin"):
            with self.subTest(role=role):
                html = self._render("chat.html", user=User(username=f"kursk.{role}", role=role, org="Союз «Курская ТПП»"),
                                    kontur_719_url="#", input_hint="")
                self.assertIn('class="user-chip" href="/profile"', html)
                self.assertIn('class="avatar head-avatar" href="/profile"', html)
                self.assertIn('<span class="urole">Союз «Курская ТПП»</span>', html, "под именем — организация")

    def test_policy_lists_every_cabinet_field(self):
        """Две таблицы про одно: поля кабинета и категории в политике. Новое поле без строки
        в политике — обработка ПДн без названного состава."""
        policy = self._render("privacy.html", page_title="", active="privacy", updated="", user=None)

        def row(title):  # строка таблицы категорий: «email» и «телефон» есть и у заявок лендинга
            start = policy.index(f"<tr><td>{title}</td>")
            return policy[start:policy.index("</tr>", start)]

        participant = row("Данные участника")
        consent = self._render("profile.html", user=User(username="u", role="user"),
                               form={"region": ""}, regions=[], tab="profile", can_leave=True)
        consent = consent[consent.index('name="consent"'):]
        for term in ("ФИО", "регион", "Telegram", "должность", "email", "телефон"):
            with self.subTest(term=term):
                self.assertIn(term, participant, "категория «Данные участника» не называет поле кабинета")
                self.assertIn(term, consent, "текст согласия не называет поле кабинета")
        self.assertIn("организация и её ИНН", row("Учётные данные"))


class TestCabinetColumnsMigrate(unittest.TestCase):
    def test_old_users_table_gets_cabinet_columns(self):
        import tempfile

        from app.db import engine as engine_mod

        with tempfile.TemporaryDirectory() as tmp:
            old = create_engine(f"sqlite:///{tmp}/old.db")
            with old.begin() as c:
                c.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64))")
                c.exec_driver_sql("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
                c.exec_driver_sql("CREATE TABLE feedback (id INTEGER PRIMARY KEY)")
            with mock.patch.object(engine_mod, "engine", old):
                engine_mod.init_db()                      # как на старте боевого процесса
            with old.connect() as c:
                cols = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(users)")}
            old.dispose()
        self.assertTrue({"position", "email", "phone", "org", "inn"} <= cols, cols)


if __name__ == "__main__":
    unittest.main()
