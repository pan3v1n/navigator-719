"""R26: вопрос эксперта должен переживать падение движка.

Раньше вопрос и ответ писались одной транзакцией ПОСЛЕ успешной генерации, и при 503 (рваная
сеть, недоступный DeepSeek, таймаут) вопрос исчезал бесследно — терялись ровно те случаи, которые
нужнее всего для разбора. Побочный полезный эффект: расхождение «Запросы» и «ответов» в админке
делает долю сбоев видимой.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api import chat as chat_mod  # noqa: E402
from app.api.ratelimit import SlidingWindow  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base  # noqa: E402


class TestQuestionSurvivesEngineFailure(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._orig_session = chat_mod.get_session
        self._orig_answer = chat_mod.answer
        self._orig_limit = chat_mod._chat_limit
        chat_mod.get_session = lambda: self.Session()
        chat_mod._chat_limit = SlidingWindow(limit=1000, window=60.0)
        with self.Session() as db:
            self.uid = q.create_user(db, "expert1", "h", role="expert").id
            self.user = q.get_user(db, self.uid)
            db.expunge(self.user)

    def tearDown(self):
        chat_mod.get_session = self._orig_session
        chat_mod.answer = self._orig_answer
        chat_mod._chat_limit = self._orig_limit

    def _messages(self):
        with self.Session() as db:
            return q.get_messages_for_user(db, self.uid)

    def test_question_is_saved_when_engine_raises(self):
        def boom(*a, **k):
            raise RuntimeError("DeepSeek недоступен")

        chat_mod.answer = boom
        req = chat_mod.ChatRequest(message="Требования к чиллерам?", session_id="s1")
        with self.assertRaises(HTTPException) as cm:
            chat_mod.chat(req, user=self.user)
        self.assertEqual(cm.exception.status_code, 503)

        msgs = self._messages()
        self.assertEqual([m.role for m in msgs], ["user"], "вопрос обязан сохраниться")
        self.assertEqual(msgs[0].content, "Требования к чиллерам?")

    def test_successful_answer_logs_question_once(self):
        class _Ans:
            text = "Ответ движка"
            hits = []
            cases = []
            rule_sources = []
            low_relevance = False
            unverified_numbers = []
            prompt_tokens = 10
            completion_tokens = 5
            input_hint = ""  # U5

        chat_mod.answer = lambda *a, **k: _Ans()
        req = chat_mod.ChatRequest(message="Вопрос", session_id="s2")
        resp = chat_mod.chat(req, user=self.user)
        self.assertEqual(resp.answer, "Ответ движка")
        roles = [m.role for m in self._messages()]
        self.assertEqual(roles, ["user", "assistant"], "вопрос не должен задваиваться")

    def test_history_does_not_include_current_question(self):
        """Порядок важен: историю грузим ДО записи вопроса, иначе он задвоится в мультитёрне."""
        seen = {}

        class _Ans:
            text = "ok"
            hits = []
            cases = []
            rule_sources = []
            low_relevance = False
            unverified_numbers = []
            prompt_tokens = 0
            completion_tokens = 0
            input_hint = ""  # U5

        def spy(msg, okpd2=None, history=None):
            seen["history"] = list(history or [])
            return _Ans()

        chat_mod.answer = spy
        chat_mod.chat(chat_mod.ChatRequest(message="первый", session_id="s3"), user=self.user)
        chat_mod.chat(chat_mod.ChatRequest(message="второй", session_id="s3"), user=self.user)
        texts = [m["content"] for m in seen["history"]]
        self.assertIn("первый", texts)
        self.assertNotIn("второй", texts, "текущий вопрос не должен попадать в свою же историю")

    def test_stream_endpoint_does_not_prelog(self):
        """Стрим намеренно НЕ пишет вопрос заранее: при его сбое фронт уходит на /api/chat,
        который уже пишет, и предзапись в обоих давала бы дубль вопроса в беседе."""
        import inspect
        src = inspect.getsource(chat_mod.chat_stream)
        self.assertNotIn("_log_question", src)
        self.assertIn("_log_question", inspect.getsource(chat_mod.chat))


if __name__ == "__main__":
    unittest.main()
