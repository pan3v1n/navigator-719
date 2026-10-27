"""Одна структурная строка в журнале на каждый ответ — `O8` #144.

ЗАЧЕМ. В stdout боя был только access-лог uvicorn: по журналу нельзя было восстановить ни долю
процедурной ветки, ни срабатывания гарда, ни время ответа (в базе реплики пишутся одним моментом,
в access-логе длительности нет). Расхождение «5 стримов в логе против 3 ответов в базе» 18.09
объяснить было нечем.

ЧТО ПИШЕМ. Строка `answer_event {json}`: точка входа, гость или нет, режим источника (`L1`), корпус
ответа (`L2`), списан ли, число источников, сработавшие проверки, токены, время до первого куска и
полное. Сводку за день считает `scripts/answer_events.py` по `docker compose logs app`.

⚠ 152-ФЗ: НИ текста вопроса, ни текста ответа, ни логина. Идентификатор — только номер сохранённой
реплики (по нему разбор идёт уже в базе, под доступом админа). Сбой записи события ответ не роняет.
"""

from __future__ import annotations

import json
import time

from loguru import logger

PREFIX = "answer_event"


def log_answer_event(ans, *, endpoint: str, mode: str, started: float, sources: int,
                     first_token: float | None = None, message_id: int | None = None,
                     guest: bool = False) -> None:
    """`started`/`first_token` — `time.monotonic()` начала запроса и первого куска потока."""
    try:
        now = time.monotonic()
        ev = {
            "endpoint": endpoint, "guest": guest, "mode": mode,
            "corpus": ans.corpus or "-", "metered": bool(ans.metered),
            "sources": sources,
            "unverified": len(ans.unverified_numbers or []),
            "phantom": len(ans.phantom_documents or []),
            "low_relevance": bool(ans.low_relevance),
            "prompt_tokens": ans.prompt_tokens or 0, "completion_tokens": ans.completion_tokens or 0,
            "first_token_ms": round((first_token - started) * 1000) if first_token else None,
            "total_ms": round((now - started) * 1000),
            "message_id": message_id,
        }
        logger.info("{} {}", PREFIX, json.dumps(ev, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — журнал не должен ронять ответ
        logger.exception("answer_event: не удалось записать событие")
