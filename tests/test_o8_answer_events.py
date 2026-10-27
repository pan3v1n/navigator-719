"""`O8` #144: одна структурная строка в журнале на ответ — и ни слова из вопроса. Офлайн.

Критерий: по журналу восстанавливаются доля процедурной ветки, срабатывания гарда и p50/p95 времени
ответа; в строке нет текста вопроса (152-ФЗ).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from loguru import logger  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import answer_events  # noqa: E402
from app.api import chat as chat_mod  # noqa: E402
from app.api import events  # noqa: E402
from app.api.ratelimit import SlidingWindow  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.rag import corpora  # noqa: E402

QUESTION = "Секретный вопрос про насосы ООО «Ромашка» ИНН 4632000000"


class _Ans:
    text = "Ответ движка"
    hits: list = []
    rule_sources: list = []
    low_relevance = False
    unverified_numbers = ["90"]
    phantom_documents: list = []
    prompt_tokens, completion_tokens = 1200, 300
    input_hint = ""
    metered = True
    corpus = corpora.USED_RULES


class TestAnswerEventIsWritten(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._orig = (chat_mod.get_session, chat_mod.answer, chat_mod.answer_stream, chat_mod._chat_limit)
        chat_mod.get_session = lambda: self.Session()
        chat_mod._chat_limit = SlidingWindow(limit=1000, window=60.0)
        with self.Session() as db:
            uid = q.create_user(db, "expert.secret", "h", role="expert").id
            self.user = q.get_user(db, uid)
            db.expunge(self.user)
        self.lines: list[str] = []
        self.sink = logger.add(lambda m: self.lines.append(m.record["message"]), level="INFO")

    def tearDown(self):
        logger.remove(self.sink)
        chat_mod.get_session, chat_mod.answer, chat_mod.answer_stream, chat_mod._chat_limit = self._orig

    def _events(self) -> list[dict]:
        return answer_events.parse(self.lines)

    def _no_pii(self):
        joined = "\n".join(line for line in self.lines if events.PREFIX in line)
        for secret in (QUESTION, "Ромашка", "4632000000", "expert.secret", _Ans.text):
            self.assertNotIn(secret, joined, "в событие попал текст вопроса, ответа или логин")

    def test_chat_writes_one_event_without_pii(self):
        chat_mod.answer = lambda *a, **k: _Ans()
        resp = chat_mod.chat(chat_mod.ChatRequest(message=QUESTION, session_id="s1", corpus=corpora.RULES),
                             user=self.user)
        evs = self._events()
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual((ev["endpoint"], ev["mode"], ev["corpus"], ev["guest"]),
                         ("chat", corpora.RULES, corpora.USED_RULES, False))
        self.assertEqual(ev["unverified"], 1)
        self.assertEqual(ev["message_id"], resp.message_id)
        self.assertIsInstance(ev["total_ms"], int)
        self._no_pii()

    def test_stream_writes_first_token_time(self):
        def stream(*a, **k):
            yield "delta", "Отв"
            yield "delta", "ет"
            yield "done", _Ans()
        chat_mod.answer_stream = stream
        resp = chat_mod.chat_stream(chat_mod.ChatRequest(message=QUESTION, session_id="s2"), user=self.user)

        async def drain():
            return [chunk async for chunk in resp.body_iterator]
        asyncio.run(drain())
        evs = self._events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["endpoint"], "stream")
        self.assertIsNotNone(evs[0]["first_token_ms"])
        self._no_pii()

    def test_broken_answer_does_not_break_the_reply(self):
        class _Broken:
            corpus = "rules"   # прочих полей нет — событие не собирается
        events.log_answer_event(_Broken(), endpoint="chat", mode="auto", started=0.0, sources=0)
        self.assertEqual(self._events(), [], "битое событие не пишется, но и не роняет ответ")


class TestDailySummary(unittest.TestCase):
    LINES = [
        'INFO uvicorn: "POST /api/chat HTTP/1.1" 200',
        'INFO app: answer_event {"endpoint": "stream", "guest": false, "mode": "auto", "corpus": "appendix", '
        '"unverified": 0, "phantom": 0, "low_relevance": false, "total_ms": 8000, "first_token_ms": 2000}',
        'INFO app: answer_event {"endpoint": "chat", "guest": true, "mode": "rules", "corpus": "rules", '
        '"unverified": 1, "phantom": 0, "low_relevance": false, "total_ms": 12000, "first_token_ms": null}',
        'INFO app: answer_event {"endpoint": "stream", "guest": false, "mode": "auto", "corpus": "-", '
        '"unverified": 0, "phantom": 1, "low_relevance": true, "total_ms": 200, "first_token_ms": 150}',
        'INFO app: answer_event {битый json',
    ]

    def test_summary_restores_route_share_guards_and_latency(self):
        s = answer_events.summary(answer_events.parse(self.LINES))
        self.assertEqual(s["answers"], 3, "строки без события и битый JSON не считаются")
        self.assertEqual(s["procedural_share"], round(1 / 3, 3))
        self.assertEqual((s["unverified"], s["phantom"], s["low_relevance"], s["guest"]), (1, 1, 1, 1))
        self.assertEqual((s["total_ms_p50"], s["total_ms_p95"]), (8000, 12000))
        self.assertEqual(s["first_token_ms_p50"], 150)
        self.assertEqual(s["mode"], {"auto": 2, "rules": 1})

    def test_empty_log(self):
        s = answer_events.summary([])
        self.assertEqual(s["answers"], 0)
        self.assertIsNone(s["procedural_share"])
        self.assertIsNone(s["total_ms_p50"])


if __name__ == "__main__":
    unittest.main()
