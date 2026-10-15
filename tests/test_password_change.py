"""Смена пароля самим пользователем в кабинете (просьба владельца 15.10.2026).

Что закрепляем:
* новый пароль — только по верному текущему; правила длины, повтора и «не тот же самый»;
* после смены входы на ДРУГИХ устройствах гаснут (эпоха входа), а этот браузер остаётся в сессии,
  и его «запомнить меня» продлевается с новой эпохой, а не обрывается;
* попытки под лимитом (форма проверяет текущий пароль — без лимита это перебор из открытой сессии);
* пароли не попадают в журнал.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import auth, web  # noqa: E402
from app.api.auth import verify_password  # noqa: E402
from tests.test_admin_panel import PW, _PanelDB  # noqa: E402

NEW = "Nov-Parol-2026"


class TestPasswordChange(_PanelDB):
    def setUp(self):
        super().setUp()
        web._password_limit.reset()
        self.addCleanup(web._password_limit.reset)
        self.u = self._mk("kursk.expert1", role="expert")

    def _change(self, client, current=PW, new=NEW, repeat=None):
        return client.post("/profile/password", data={"current_password": current, "new_password": new,
                                                       "repeat_password": new if repeat is None else repeat},
                           follow_redirects=False)

    def test_change_keeps_this_browser_and_ends_the_others(self):
        here = self._login("kursk.expert1", remember=True)
        other = self._login("kursk.expert1", remember=True)
        with mock.patch.object(web.logger, "info") as log:
            r = self._change(here)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/profile?password=1#password"))
        self.assertTrue(verify_password(NEW, self._fresh(self.u.id).password_hash))
        self.assertEqual(self._fresh(self.u.id).auth_epoch, 1)
        for call in log.call_args_list:
            self.assertNotIn(NEW, str(call), "новый пароль в журнале")
            self.assertNotIn(PW, str(call), "старый пароль в журнале")
        self.assertTrue(self._signed_in(here), "смена пароля выкинула из сервиса этот браузер")
        self.assertIn("Пароль изменён", here.get("/profile?password=1").text)
        here.cookies.pop("session", None)                      # остаётся «запомнить меня» — с новой эпохой
        self.assertTrue(self._signed_in(here), "«запомнить меня» этого браузера оборвалось")
        self.assertFalse(self._signed_in(other), "вход на другом устройстве пережил смену пароля")
        other.cookies.pop("session", None)
        self.assertFalse(self._signed_in(other), "«запомнить меня» другого устройства пережило смену пароля")
        old = self.TestClient(self.app).post("/login", data={"username": "kursk.expert1", "password": PW},
                                             follow_redirects=False)
        self.assertEqual(old.status_code, 401, "старый пароль всё ещё открывает вход")
        ok = self.TestClient(self.app).post("/login", data={"username": "kursk.expert1", "password": NEW},
                                            follow_redirects=False)
        self.assertEqual(ok.status_code, 302)

    def test_without_remember_no_cookie_is_issued(self):
        here = self._login("kursk.expert1")
        r = self._change(here)
        self.assertNotIn(auth.REMEMBER_COOKIE, r.headers.get("set-cookie", ""),
                         "«запомнить меня» выдано браузеру, который об этом не просил")
        self.assertTrue(self._signed_in(here))

    def test_rejections_change_nothing(self):
        here = self._login("kursk.expert1")
        for kw, why in (({"current": "не-тот"}, "Текущий пароль указан неверно"),
                        ({"current": ""}, "Текущий пароль указан неверно"),
                        ({"new": "Kor0tk1"}, "не короче 8"),
                        ({"new": "я" * 37}, "слишком длинный"),             # 37 символов, 74 байта > 72
                        ({"new": " " * 10}, "из пробелов"),
                        ({"repeat": NEW + "x"}, "не совпадают"),
                        ({"new": PW}, "совпадает с текущим")):
            with self.subTest(kw=kw):
                web._password_limit.reset()
                r = self._change(here, **kw)
                self.assertEqual(r.status_code, 400)
                self.assertIn(why, r.text)
                self.assertNotIn(f'value="{kw.get("new", NEW)}"', r.text, "пароль вернулся в разметку")
                got = self._fresh(self.u.id)
                self.assertTrue(verify_password(PW, got.password_hash))
                self.assertEqual(got.auth_epoch, 0)
        self.assertTrue(self._signed_in(here), "отказ выкинул из сервиса")

    def test_attempts_are_limited(self):
        here = self._login("kursk.expert1")
        with mock.patch.object(web._password_limit, "limit", 3):
            codes = [self._change(here, current=f"неверный-{i}").status_code for i in range(4)]
            self.assertEqual(codes, [400, 400, 400, 429])
            self.assertEqual(self._change(here).status_code, 429, "лимит пропустил и верный пароль")
        self.assertTrue(verify_password(PW, self._fresh(self.u.id).password_hash))

    def test_anonymous_goes_to_login(self):
        r = self._change(self.TestClient(self.app))
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/login"))

    def test_panel_is_on_the_profile_tab(self):
        html = self._login("kursk.expert1").get("/profile").text
        self.assertIn('action="/profile/password#password"', html)
        self.assertIn('autocomplete="current-password"', html)


if __name__ == "__main__":
    unittest.main()
