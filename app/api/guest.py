"""Пробный режим без входа (решение владельца 09.10.2026; макет «Navigator 719 Service», режим guest).

Гость задаёт `GUEST_TRIAL_QUESTIONS` вопросов (по умолчанию 3), дальше — «Войдите, чтобы
продолжить работу». Ответ дешевле пользовательского: меньше позиций приложения в контексте и
просьба отвечать кратко с потолком длины (`pipeline.answer(..., brief=True)`). Источники — без
полного текста (ссылки на пункты остаются), истории, экспорта и оценок у гостя нет.

Кто такой гость. Случайный `guest_id` в ПОДПИСАННОЙ куке `guest719` (`app/api/trial.py`), которую
ставит страница `/chat`. Без куки API не отвечает: счёт пробных вопросов держится на ней. Подделать её нельзя
(подпись `SESSION_SECRET`), удалить — можно, поэтому сверху ещё два потолка:
  * по IP — `GUEST_IP_PER_DAY` за сутки, в памяти процесса; адрес в базу не пишется;
  * на всех гостей — `GUEST_DAILY_TOTAL` засчитанных ответов за сутки по Москве, по базе. Это и
    есть худший случай расхода токенов DeepSeek, сколько бы браузеров ни пришло.

Что засчитывается — как у тарифов (#160): ответ движка (`Answer.metered`); приветствия и сбои — нет.
Вопрос бронируется засчитанным ДО движка и возвращается, если ответ не засчитан (`_reserve`,
ревью PR #182). Повтор уже отвеченного вопроса в той же беседе (фолбэк фронта после стрима) получает
СОХРАНЁННЫЙ ответ без движка. Признак повтора берётся из базы, а не от клиента: клиентский ключ дал
бы бесплатные вопросы (урок ревью PR #161).

Реплики гостей пишутся в отдельную таблицу `guest_messages` — обезличенно, без IP; их видит admin.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from app.api.chat import (
    ChatRequest,
    ChatResponse,
    _reject_sensitive,
    _sources_from_hits,
    _sources_from_rules,
    _sse,
)
from app.api.ratelimit import SlidingWindow, client_ip
from app.api.trial import guest_id, trial_view
from app.core import plans
from app.core.config import settings
from app.db import queries as q
from app.db.engine import get_session
from app.rag.pipeline import answer, answer_stream
from app.tools.navigator import extract_okpd2

router = APIRouter()

TRIAL_OVER = "Пробные вопросы закончились. Войдите, чтобы продолжить работу."
DAILY_OVER = ("Пробный доступ на сегодня исчерпан. Войдите в аккаунт организации, чтобы "
              "продолжить, или вернитесь завтра.")
IP_OVER = ("С этого адреса сегодня задано слишком много пробных вопросов. Войдите в аккаунт "
           "организации, чтобы продолжить.")

# Окно повтора — как у тарифов: фолбэк фронта ждёт стрим 120 с.
REPEAT_WINDOW = timedelta(minutes=5)
_ip_limit = SlidingWindow(settings.GUEST_IP_PER_DAY, window=24 * 3600.0)


def _msk_day_start(now: datetime | None = None) -> datetime:
    """Начало текущих суток по Москве — в наивном UTC, как время лежит в базе."""
    return plans.anchor_from_date(plans.msk_today(now))


def _require_guest(request: Request) -> str:
    if not settings.GUEST_TRIAL_ENABLED:
        raise HTTPException(status_code=404, detail="Пробный режим выключен")
    gid = guest_id(request)
    if gid is None:
        # Кука ставится страницей /chat. Без неё не на чем держать счёт пробных вопросов.
        raise HTTPException(status_code=403, detail="guest_cookie_required")
    return gid


def _stored_repeat(gid: str, req: ChatRequest):
    """Повтор отвеченного вопроса (фолбэк фронта после стрима, дошедшего до конца на сервере) —
    СОХРАНЁННЫЙ ответ: движок не зовётся, строк не прибавляется, потолки не нужны.

    ⚠ Ревью PR #182: прежде повтор лишь не засчитывался, а движок звался заново — тот же вопрос в
    цикле давал безлимитные вызовы DeepSeek мимо всех трёх потолков."""
    if not req.session_id:
        return None
    with get_session() as db:
        return q.guest_answered_repeat(
            db, gid, req.session_id, req.message,
            since=plans.naive_utc(datetime.now(timezone.utc)) - REPEAT_WINDOW)


def _reserve(request: Request, gid: str, session_id: str, message: str) -> int:
    """Бронь пробного вопроса ДО движка: строка вопроса пишется сразу засчитанной, и лишь затем
    считаются потолки — ВМЕСТЕ с ней. Не прошла — бронь снимается. Возвращает id строки.

    ⚠ Ревью PR #182, почему не «проверить, потом записать». Проверка по старому счёту пропускала
    параллельные запросы (10 одновременных — 10 ответов при пробе в 3), а запись на финале стрима
    не наступала вовсе, если клиент обрывал соединение после текста. С бронью обслужен может быть
    лишь тот, кто увидел счёт со своей строкой в пределах потолка, — больше потолка их не бывает.

    Порядок: личный счёт гостя (402 — фронт ведёт на вход), общий суточный потолок (429) и
    последним — IP: `check` занимает слот, тратить его на отклонённый вопрос нельзя."""
    with get_session() as db:
        row = q.log_guest_message(db, guest_id=gid, session_id=session_id, role="user",
                                  content=message, charged=True)
        over = None
        if q.count_guest_answers(db, gid) > settings.GUEST_TRIAL_QUESTIONS:
            over = HTTPException(status_code=402, detail=TRIAL_OVER)
        elif q.count_guest_answers_since(db, _msk_day_start()) > settings.GUEST_DAILY_TOTAL:
            logger.warning("гость: общий суточный потолок пробного режима исчерпан")
            over = HTTPException(status_code=429, detail=DAILY_OVER)
        if over is not None:
            q.delete_guest_message(db, row.id)
            raise over
    key = f"guest:{client_ip(request)}"
    if not _ip_limit.check(key):
        logger.warning("гость: потолок по адресу за сутки исчерпан")  # адрес в журнал не пишем
        _drop(row.id)
        raise HTTPException(status_code=429, detail=IP_OVER,
                            headers={"Retry-After": str(_ip_limit.retry_after(key))})
    return row.id


def _drop(question_id: int) -> None:
    try:
        with get_session() as db:
            q.delete_guest_message(db, question_id)
    except Exception:  # noqa: BLE001 — лишняя засчитанная строка дешевле упавшего ответа
        logger.exception("гость: не удалось снять бронь")


def _refund(question_id: int) -> None:
    """Ответ не засчитывается (приветствие, сбой движка) — вопрос остаётся в журнале, бронь снята."""
    try:
        with get_session() as db:
            q.set_guest_charged(db, question_id, False)
    except Exception:  # noqa: BLE001 — недосписание дешевле упавшего ответа
        logger.exception("гость: не удалось снять бронь")


def _load_history(gid: str, session_id: str, max_msgs: int = 12) -> list[dict]:
    """Мультитёрн гостя — как у пользователя (`chat._load_history`), по паре (гость, беседа)."""
    try:
        with get_session() as db:
            prev = q.get_guest_session_messages(db, gid, session_id)
    except Exception:  # noqa: BLE001 — история не критична
        return []
    return [{"role": m.role, "content": m.content if m.role == "user" else m.content[:600]}
            for m in prev[-max_msgs:]]


def _guest_sources(ans) -> list:
    """Источники без полного текста: гость видит, на какой пункт опирается ответ, и ссылку на
    первоисточник; текст пункта в панели — после входа (макет: «видеть полный текст источников»)."""
    sources = _sources_from_hits(ans.hits) or _sources_from_rules(ans.rule_sources)
    for s in sources:
        s.text = None
    return sources


def _log(gid: str, session_id: str, role: str, content: str, **kw) -> None:
    try:
        with get_session() as db:
            q.log_guest_message(db, guest_id=gid, session_id=session_id, role=role,
                                content=content, **kw)
    except Exception:  # noqa: BLE001 — лог не должен ронять ответ
        logger.exception("гость: не удалось записать реплику")


class GuestChatResponse(ChatResponse):
    trial: dict


def _engine_kwargs(req: ChatRequest, history: list[dict]) -> dict:
    return {"okpd2": extract_okpd2(req.message), "history": history,
            "limit": settings.GUEST_CONTEXT_LIMIT, "brief": True}


def _trial(gid: str) -> dict:
    with get_session() as db:
        return trial_view(db, gid)


def _stored_payload(row, gid: str, session_id: str) -> dict:
    """Сохранённый ответ гостю в форме ответа движка (источники в базе уже без полного текста)."""
    return {"answer": row.content, "sources": json.loads(row.sources_json or "[]"),
            "low_relevance": bool(row.low_relevance), "unverified_numbers": [],
            "session_id": session_id, "message_id": None, "input_hint": "", "trial": _trial(gid)}


@router.post("/api/guest/chat", response_model=GuestChatResponse)
def guest_chat(req: ChatRequest, request: Request) -> GuestChatResponse:
    gid = _require_guest(request)
    _reject_sensitive(req.message, "guest")   # до счёта: отклонённый вопрос пробу не тратит
    if (stored := _stored_repeat(gid, req)) is not None:
        logger.info("гость: повтор отвеченного вопроса — сохранённый ответ, без движка")
        return GuestChatResponse(**_stored_payload(stored, gid, req.session_id))
    session_id = req.session_id or uuid.uuid4().hex
    history = _load_history(gid, session_id)   # до брони: своя строка в историю не попадает
    question_id = _reserve(request, gid, session_id, req.message)   # R26: вопрос — до движка
    try:
        ans = answer(req.message, **_engine_kwargs(req, history))
    except Exception:  # noqa: BLE001
        logger.exception("гость: движок упал")
        _refund(question_id)
        raise HTTPException(status_code=503, detail="Сервис временно недоступен, повторите запрос.")
    if not ans.metered:
        _refund(question_id)
    sources = _guest_sources(ans)
    _log(gid, session_id, "assistant", ans.text, sources=[s.model_dump() for s in sources],
         low_relevance=ans.low_relevance, prompt_tokens=ans.prompt_tokens,
         completion_tokens=ans.completion_tokens)
    return GuestChatResponse(
        answer=ans.text, sources=sources, low_relevance=ans.low_relevance,
        unverified_numbers=ans.unverified_numbers, session_id=session_id, message_id=None,
        input_hint=ans.input_hint, trial=_trial(gid))


@router.post("/api/guest/chat/stream")
def guest_chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    """SSE как у `/api/chat/stream`; на 'done' — ещё остаток пробных вопросов. Вопрос бронируется
    ДО стрима: клиент, оборвавший соединение после текста, вопрос уже потратил. Упал стрим —
    бронь снимается целиком: фолбэк фронта придёт на `/api/guest/chat` и запишет вопрос сам."""
    gid = _require_guest(request)
    _reject_sensitive(req.message, "guest")
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if (stored := _stored_repeat(gid, req)) is not None:
        logger.info("гость: повтор отвеченного вопроса — сохранённый ответ, без движка")
        done = {"type": "done", **_stored_payload(stored, gid, req.session_id)}
        done.pop("answer")
        return StreamingResponse(iter([_sse({"type": "delta", "text": stored.content}), _sse(done)]),
                                 media_type="text/event-stream", headers=headers)
    session_id = req.session_id or uuid.uuid4().hex
    history = _load_history(gid, session_id)
    question_id = _reserve(request, gid, session_id, req.message)

    def gen():
        try:
            for kind, payload in answer_stream(req.message, **_engine_kwargs(req, history)):
                if kind == "delta":
                    yield _sse({"type": "delta", "text": payload})
                    continue
                ans = payload
                if not ans.metered:
                    _refund(question_id)
                sources = _guest_sources(ans)
                _log(gid, session_id, "assistant", ans.text,
                     sources=[s.model_dump() for s in sources], low_relevance=ans.low_relevance,
                     prompt_tokens=ans.prompt_tokens, completion_tokens=ans.completion_tokens)
                yield _sse({
                    "type": "done", "message_id": None,
                    "sources": [s.model_dump() for s in sources],
                    "low_relevance": ans.low_relevance,
                    "unverified_numbers": ans.unverified_numbers,
                    "session_id": session_id, "input_hint": ans.input_hint, "trial": _trial(gid),
                })
        except Exception:  # noqa: BLE001 — сигнал фолбэка фронту
            logger.exception("гость: стрим упал")
            _drop(question_id)
            yield _sse({"type": "error", "detail": "stream_failed"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)
