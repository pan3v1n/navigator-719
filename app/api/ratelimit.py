"""Ограничение частоты запросов (R12) — скользящее окно в памяти процесса.

ЗАЧЕМ. Две незакрытые двери на публично доступном сервисе:
  * `POST /login` без троттлинга — перебор паролей ничем не ограничен (bcrypt замедляет, но не
    останавливает), а пароли у нас 8-символьные, розданные людям;
  * `/api/chat` и `/api/chat/stream` без лимита — один залогиненный пользователь (или его
    забытая вкладка с автообновлением) может неограниченно жечь токены DeepSeek. Это
    единственная платная статья проекта.

ПОЧЕМУ В ПАМЯТИ, А НЕ REDIS. Сервис однопроцессный (один uvicorn на VM), пользователей ≤30.
Внешнее хранилище здесь добавило бы зависимость и точку отказа ради задачи, которой хватает
словаря с блокировкой. Если появится второй воркер — лимит станет per-worker, и это надо будет
переделать; отмечено в docs/REVIEW_TZ.md.

ВРЕМЯ — `time.monotonic()`, а не wall-clock: перевод системных часов не должен ни открывать
лимит досрочно, ни блокировать пользователя надолго.
"""

from __future__ import annotations

import threading
import time
from collections import deque

# Ключей в памяти держим не больше — защита от разрастания словаря при переборе с разных IP.
_MAX_KEYS = 4096


class SlidingWindow:
    """Счётчик «не более `limit` событий за `window` секунд» по ключу."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Выбрасываем ключи, у которых окно давно пусто (вызывается под замком)."""
        stale = [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window]
        for k in stale:
            del self._hits[k]

    def check(self, key: str, now: float | None = None) -> bool:
        """True — запрос разрешён (событие зачтено); False — лимит исчерпан."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._hits) > _MAX_KEYS:
                self._prune(now)
            q = self._hits.setdefault(key, deque())
            edge = now - self.window
            while q and q[0] <= edge:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Через сколько секунд освободится слот (для заголовка Retry-After). Минимум 1."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.get(key)
            if not q or len(q) < self.limit:
                return 0
            return max(1, int(q[0] + self.window - now) + 1)

    def reset(self, key: str | None = None) -> None:
        """Сброс: ключа или всего счётчика. Нужен после успешного входа и в тестах."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
