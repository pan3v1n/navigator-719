"""Юнит-тесты обратной связи v2: единая таблица Feedback + дискриминатор kind
(answer — звёзды 0..5 + опц. исправление; dialog — комментарий; service — глобальная форма)."""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import queries as q
from app.db.models import Base


class TestFeedbackKinds(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_three_kinds_persist(self):
        with self.Session() as db:
            u = q.create_user(db, "e", "h", role="expert")
            q.save_feedback(db, user_id=u.id, kind="answer", rating=5, message_id=7, session_id="s")
            q.save_feedback(db, user_id=u.id, kind="answer", correction="верно 100 баллов", message_id=7, session_id="s")
            q.save_feedback(db, user_id=u.id, kind="dialog", comment="полезно", session_id="s")
            q.save_feedback(db, user_id=u.id, rating=4, matched="да", comment="ок")  # service (kind по умолчанию)
            rows = q.get_feedback_for_user(db, u.id)
            self.assertEqual(len(rows), 4)
            self.assertEqual(sorted(r.kind for r in rows), ["answer", "answer", "dialog", "service"])
            ans = [r for r in rows if r.kind == "answer"]
            self.assertTrue(any(r.rating == 5 and r.message_id == 7 for r in ans))
            self.assertTrue(any(r.correction == "верно 100 баллов" for r in ans))
            svc = next(r for r in rows if r.kind == "service")
            self.assertEqual((svc.rating, svc.matched), (4, "да"))

    def test_service_default_kind_backward_compatible(self):
        with self.Session() as db:
            u = q.create_user(db, "e2", "h", role="expert")
            f = q.save_feedback(db, user_id=u.id, rating=3, comment="c")  # старый вызов (без kind)
            self.assertEqual(f.kind, "service")
            self.assertIsNone(f.message_id)


if __name__ == "__main__":
    unittest.main()
