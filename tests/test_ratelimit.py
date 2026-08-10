"""R12: ограничение частоты запросов — скользящее окно + подключение к эндпоинтам.

Две двери, которые закрывает лимитер: перебор пароля на `/login` (bcrypt замедляет, но не
останавливает) и неограниченный расход токенов DeepSeek через `/api/chat` — единственную платную
статью проекта.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from app.api.ratelimit import SlidingWindow  # noqa: E402


class TestSlidingWindow(unittest.TestCase):
    def test_allows_up_to_limit_then_blocks(self):
        w = SlidingWindow(limit=3, window=60.0)
        self.assertEqual([w.check("k", now=100.0) for _ in range(3)], [True, True, True])
        self.assertFalse(w.check("k", now=100.0))

    def test_window_slides(self):
        w = SlidingWindow(limit=2, window=60.0)
        w.check("k", now=100.0)
        w.check("k", now=100.0)
        self.assertFalse(w.check("k", now=150.0))       # окно ещё не прошло
        self.assertTrue(w.check("k", now=161.0))        # первое событие вышло за окно

    def test_keys_are_independent(self):
        w = SlidingWindow(limit=1, window=60.0)
        self.assertTrue(w.check("a", now=1.0))
        self.assertFalse(w.check("a", now=1.0))
        self.assertTrue(w.check("b", now=1.0))          # чужой ключ не задет

    def test_retry_after_is_positive_and_bounded(self):
        w = SlidingWindow(limit=1, window=60.0)
        w.check("k", now=100.0)
        self.assertEqual(w.retry_after("k", now=100.0), 61)
        self.assertGreaterEqual(w.retry_after("k", now=159.0), 1)
        self.assertEqual(w.retry_after("свободный ключ", now=100.0), 0)

    def test_reset_clears_key(self):
        w = SlidingWindow(limit=1, window=60.0)
        w.check("k", now=1.0)
        self.assertFalse(w.check("k", now=1.0))
        w.reset("k")
        self.assertTrue(w.check("k", now=1.0))

    def test_monotonic_time_is_used_by_default(self):
        # без явного `now` лимитер обязан работать (и не падать на системных часах)
        w = SlidingWindow(limit=1, window=60.0)
        self.assertTrue(w.check("k"))
        self.assertFalse(w.check("k"))

    def test_stale_keys_are_pruned(self):
        from app.api import ratelimit
        w = SlidingWindow(limit=1, window=1.0)
        for i in range(ratelimit._MAX_KEYS + 50):
            w.check(f"k{i}", now=0.0)
        w.check("свежий", now=1000.0)                   # запускает уборку
        self.assertLessEqual(len(w._hits), 10, "старые ключи должны вычищаться")


class TestChatEndpointLimited(unittest.TestCase):
    """Лимит чата срабатывает ДО обращения к БД и движку — иначе он бессмысленен."""

    def setUp(self):
        from app.api import chat as chat_mod
        self.chat = chat_mod
        self._orig = chat_mod._chat_limit
        chat_mod._chat_limit = SlidingWindow(limit=2, window=60.0)

    def tearDown(self):
        self.chat._chat_limit = self._orig

    def test_raises_429_with_retry_after(self):
        self.chat._enforce_chat_limit(7)
        self.chat._enforce_chat_limit(7)
        with self.assertRaises(HTTPException) as cm:
            self.chat._enforce_chat_limit(7)
        self.assertEqual(cm.exception.status_code, 429)
        self.assertIn("Retry-After", cm.exception.headers)

    def test_limit_is_per_user(self):
        self.chat._enforce_chat_limit(1)
        self.chat._enforce_chat_limit(1)
        self.chat._enforce_chat_limit(2)  # другой пользователь не должен быть задет
        with self.assertRaises(HTTPException):
            self.chat._enforce_chat_limit(1)

    def test_both_chat_endpoints_enforce_it(self):
        import inspect
        for fn in (self.chat.chat, self.chat.chat_stream):
            self.assertIn("_enforce_chat_limit", inspect.getsource(fn), fn.__name__)


if __name__ == "__main__":
    unittest.main()
