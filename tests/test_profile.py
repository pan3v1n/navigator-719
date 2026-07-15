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

    def test_missing_telegram_gated(self):
        u = self._user(telegram=None)
        self.assertFalse(profile_complete(u))
        self.assertTrue(needs_profile(u))

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


if __name__ == "__main__":
    unittest.main()
