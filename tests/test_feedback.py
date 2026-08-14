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


class TestFeedbackIntegrity(unittest.TestCase):
    """R2: целостность приёмочной метрики — оценить можно только СВОЙ ответ, а удаление беседы
    уносит её оценки. Без этого метрика, которой гейтится 1.0, портится обычными действиями."""

    def setUp(self):
        from app.api import web
        self.web = web
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        # expire_on_commit=False — как в боевом SessionLocal: объекты переживают commit
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._orig_get_session = web.get_session
        web.get_session = lambda: self.Session()  # эндпоинт ходит в тестовую БД, не в боевую

        with self.Session() as db:
            self.alice_id = q.create_user(db, "alice", "h", role="user").id
            self.bob_id = q.create_user(db, "bob", "h", role="user").id
            self.ask_id = q.log_message(db, user_id=self.alice_id, session_id="sa",
                                        role="user", content="вопрос").id
            self.ans_id = q.log_message(db, user_id=self.alice_id, session_id="sa",
                                        role="assistant", content="ответ движка").id

    def tearDown(self):
        self.web.get_session = self._orig_get_session

    def _user(self, uid):
        with self.Session() as db:
            u = q.get_user(db, uid)
            db.expunge(u)
            return u

    def _submit(self, user, **kw):
        return self.web.submit_feedback(self.web.FeedbackIn(**kw), user=user)

    def test_own_answer_can_be_rated(self):
        res = self._submit(self._user(self.alice_id), kind="answer", rating=5, message_id=self.ans_id)
        self.assertTrue(res["ok"])

    def test_foreign_answer_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:  # bob оценивает ответ alice
            self._submit(self._user(self.bob_id), kind="answer", rating=5, message_id=self.ans_id)
        self.assertEqual(cm.exception.status_code, 403)
        with self.Session() as db:
            self.assertEqual(q.get_feedback_for_user(db, self.bob_id), [])

    def test_missing_message_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._submit(self._user(self.alice_id), kind="answer", rating=5, message_id=999999)
        self.assertEqual(cm.exception.status_code, 403)

    def test_rating_on_own_question_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:  # реплика есть и своя, но это ВОПРОС
            self._submit(self._user(self.alice_id), kind="answer", rating=5, message_id=self.ask_id)
        self.assertEqual(cm.exception.status_code, 422)

    def test_delete_session_removes_its_feedback(self):
        with self.Session() as db:
            q.save_feedback(db, user_id=self.alice_id, kind="answer", rating=5,
                            message_id=self.ans_id, session_id="sa")
            q.save_feedback(db, user_id=self.alice_id, kind="dialog", comment="к беседе",
                            session_id="sa")
            q.save_feedback(db, user_id=self.alice_id, rating=4, matched="да")  # service — не трогать
            removed, removed_fb = q.delete_session(db, self.alice_id, "sa")
            rest = q.get_feedback_for_user(db, self.alice_id)
        self.assertEqual(removed, 2)        # вопрос + ответ
        self.assertEqual(removed_fb, 2)     # оценка ответа + комментарий к диалогу
        self.assertEqual([f.kind for f in rest], ["service"])  # глобальный отзыв уцелел

    def test_delete_session_does_not_touch_other_users(self):
        with self.Session() as db:
            q.log_message(db, user_id=self.bob_id, session_id="sa", role="user", content="чужой")
            q.save_feedback(db, user_id=self.bob_id, kind="dialog", comment="чужой", session_id="sa")
            q.delete_session(db, self.alice_id, "sa")  # тот же session_id, другой владелец
            self.assertEqual(len(q.get_messages_for_user(db, self.bob_id)), 1)
            self.assertEqual(len(q.get_feedback_for_user(db, self.bob_id)), 1)


if __name__ == "__main__":
    unittest.main()
