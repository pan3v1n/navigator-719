"""Юнит-тесты сборки данных админ-панели (app/api/admin_stats.build_admin_view):
приёмочный скоркард + сегментация, срез по регионам, триаж плохих ответов (≤2★),
изоляция когорты по датам, фильтры регион/роль, хронология дневного графика."""

import unittest
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.admin_stats import build_admin_view
from app.db import queries as q
from app.db.models import Base, Feedback, Message


class TestAdminStats(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    # --- хелперы: сообщения/фидбек с явным ts (для дат) ---
    def _user(self, db, name, role="user", region=None):
        u = q.create_user(db, name, "h", role=role)
        if region is not None:
            u.region = region
            db.commit()
        return u

    def _msg(self, db, u, sid, role, content, ts, pt=0, ct=0, unverified=None, low=False):
        m = Message(user_id=u.id, session_id=sid, role=role, content=content, ts=ts,
                    prompt_tokens=pt, completion_tokens=ct,
                    unverified_json=unverified, low_relevance=low)
        db.add(m)
        db.commit()
        db.refresh(m)
        return m

    def _fb(self, db, u, ts, **kw):
        f = Feedback(user_id=u.id, ts=ts, **kw)
        db.add(f)
        db.commit()
        db.refresh(f)
        return f

    def test_scorecard_regions_and_bad_answers(self):
        with self.Session() as db:
            ua = self._user(db, "u_a", region="Курская область")
            ub = self._user(db, "u_b", region="Белгородская область")
            t = datetime(2026, 7, 20, 12, 0)
            # A: хороший ответ (5★)
            self._msg(db, ua, "sa", "user", "Локализация насосов?", t)
            aa = self._msg(db, ua, "sa", "assistant", "Ответ про насосы", t, pt=100, ct=50)
            self._fb(db, ua, t, kind="answer", rating=5, message_id=aa.id, session_id="sa")
            # B: плохой ответ (1★) с непроверенным числом и комментарием
            self._msg(db, ub, "sb", "user", "Порог по фарме?", t)
            ab = self._msg(db, ub, "sb", "assistant", "Плохой ответ", t, pt=80, ct=40,
                           unverified='["50"]')
            self._fb(db, ub, t, kind="answer", rating=1, message_id=ab.id,
                     session_id="sb", comment="неверно")
            st = build_admin_view(db)["stats"]

        self.assertEqual(st["answer_ratings"], 2)
        self.assertEqual(st["accept_user"], 50)      # 1 из 2 ответов ≥4★
        self.assertFalse(st["gate_pass"])            # 50% < порога 70%
        regions = {r["region"]: r for r in st["regions"]}
        self.assertEqual(regions["Курская область"]["accept_pct"], 100)
        self.assertEqual(regions["Белгородская область"]["accept_pct"], 0)
        # триаж: единственный плохой ответ с восстановленным вопросом и флагом
        self.assertEqual(st["bad_count"], 1)
        bad = st["bad_answers"][0]
        self.assertEqual((bad["rating"], bad["question"], bad["answer"]),
                         (1, "Порог по фарме?", "Плохой ответ"))
        self.assertEqual(bad["comment"], "неверно")
        self.assertTrue(bad["unverified"])
        self.assertIn("Курская область", st["all_regions"])

    def test_bad_answers_sorted_worst_first(self):
        with self.Session() as db:
            u = self._user(db, "u", region="Курская область")
            t = datetime(2026, 7, 20, 9, 0)
            for i, r in enumerate([2, 0, 1, 5]):  # 5★ не должен попасть в триаж
                self._msg(db, u, f"s{i}", "user", f"вопрос {i}", t)
                a = self._msg(db, u, f"s{i}", "assistant", f"ответ {i}", t)
                self._fb(db, u, t, kind="answer", rating=r, message_id=a.id, session_id=f"s{i}")
            st = build_admin_view(db)["stats"]
        self.assertEqual(st["bad_count"], 3)  # 0,1,2 — да; 5 — нет
        self.assertEqual([b["rating"] for b in st["bad_answers"]], [0, 1, 2])  # худшие первыми

    def test_date_filter_isolates_cohort(self):
        with self.Session() as db:
            u = self._user(db, "u", region="Курская область")
            old, new = datetime(2026, 7, 2, 9, 0), datetime(2026, 7, 20, 9, 0)
            self._msg(db, u, "s1", "user", "старый вопрос", old)
            a1 = self._msg(db, u, "s1", "assistant", "старый ответ", old)
            self._fb(db, u, old, kind="answer", rating=5, message_id=a1.id, session_id="s1")
            self._msg(db, u, "s2", "user", "новый вопрос", new)
            a2 = self._msg(db, u, "s2", "assistant", "новый ответ", new)
            self._fb(db, u, new, kind="answer", rating=1, message_id=a2.id, session_id="s2")

            self.assertEqual(build_admin_view(db)["stats"]["answer_ratings"], 2)  # без фильтра — обе
            v = build_admin_view(db, date_from=date(2026, 7, 20), date_to=date(2026, 7, 20))["stats"]
        self.assertEqual(v["answer_ratings"], 1)  # только когорта 20.07
        self.assertEqual(v["accept_user"], 0)     # эта оценка — 1★
        self.assertEqual(v["requests"], 1)
        self.assertEqual(v["filter_from"], "2026-07-20")

    def test_region_and_role_filters(self):
        with self.Session() as db:
            ua = self._user(db, "a", role="user", region="Курская область")
            ex = self._user(db, "e", role="expert", region="Курская область")
            t = datetime(2026, 7, 20, 9, 0)
            self._msg(db, ua, "sa", "user", "q1", t)
            self._msg(db, ex, "se", "user", "q2", t)
            self.assertEqual(build_admin_view(db)["stats"]["requests"], 2)
            self.assertEqual(build_admin_view(db, role="user")["stats"]["requests"], 1)
            self.assertEqual(
                build_admin_view(db, region="Курская область")["stats"]["requests"], 2)
            self.assertEqual(build_admin_view(db, region="Нет такого")["stats"]["requests"], 0)

    def test_recent_feed_and_demand(self):
        with self.Session() as db:
            u = self._user(db, "u", region="Курская область")
            t0 = datetime(2026, 7, 20, 9, 0)
            self._msg(db, u, "s1", "user", "Локализация насосов?", t0)                       # товарный
            self._msg(db, u, "s2", "user", "Как внести продукцию в реестр Минпромторга?",     # процедурный
                      t0 + timedelta(hours=1))
            st = build_admin_view(db)["stats"]
        self.assertEqual(st["demand_procedural"], 1)
        self.assertEqual(st["demand_product"], 1)   # 2 запроса, 1 из них процедурный
        self.assertEqual(len(st["recent"]), 2)
        self.assertEqual(st["recent"][0]["q"], "Как внести продукцию в реестр Минпромторга?")  # новее — сверху

    def test_trends_vs_previous_window(self):
        with self.Session() as db:
            u = self._user(db, "u", region="Курская область")
            # ref «сегодня» = 21.07 → текущее окно 15–21, предыдущее 08–14
            self._msg(db, u, "s1", "user", "q1", datetime(2026, 7, 20, 9, 0))  # текущее
            self._msg(db, u, "s2", "user", "q2", datetime(2026, 7, 18, 9, 0))  # текущее
            self._msg(db, u, "s3", "user", "q3", datetime(2026, 7, 10, 9, 0))  # предыдущее
            st = build_admin_view(db, generated_at=datetime(2026, 7, 21, 12, 0))["stats"]
        tr = st["trends"]["requests"]                       # cur=2, prev=1 → +100%, рост запросов = хорошо
        self.assertEqual((tr["delta"], tr["cls"], tr["label"]), (100, "tr-good", "100%"))
        self.assertEqual(st["trend_label"], "7 дней vs предыдущие 7")

    def test_system_health_defensive(self):
        from app.api.admin_stats import system_health
        h = system_health()  # Qdrant в тестах обычно недоступен → функция не должна падать
        self.assertEqual(len(h["collections"]), 3)
        self.assertIn("version", h)
        self.assertIn("qdrant_ok", h)

    def test_day_bucket_chronological_across_months(self):
        """Регресс на баг сортировки: ключ «%d.%m» как строка ставил 01.07 раньше 30.06."""
        with self.Session() as db:
            u = self._user(db, "u", region="Курская область")
            self._msg(db, u, "s", "user", "июнь", datetime(2026, 6, 30, 9, 0))
            self._msg(db, u, "s", "user", "июль", datetime(2026, 7, 1, 9, 0))
            days = [d["date"] for d in build_admin_view(db)["stats"]["per_day"]]
        self.assertEqual(days, ["30.06", "01.07"])  # 30 июня раньше 1 июля


if __name__ == "__main__":
    unittest.main()
