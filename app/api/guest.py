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

Что засчитывается — как у тарифов (#160): ответ движка (`Answer.metered`); приветствия, сбои и
повтор уже отвеченного вопроса в той же беседе (фолбэк фронта после стрима) — нет. Признак повтора
берётся из базы, а не от клиента: клиентский ключ дал бы бесплатные вопросы (урок ревью PR #161).

Реплики гостей пишутся в отдельную таблицу `guest_messages` — обезличенно, без IP; их видит admin.
"""

from __future__ import annotations

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


def _enforce_trial(request: Request, gid: str, req: ChatRequest) -> bool:
    """Потолки пробного режима ДО движка и ДО записи вопроса. Возвращает, ЗАСЧИТЫВАТЬ ли ответ.

    Порядок важен: повтор отвеченного — бесплатно и без проверок; затем личный счёт гостя (402 —
    фронт ведёт на вход), общий суточный потолок (429) и последним — IP: `check` занимает слот,
    и тратить его на вопрос, который отклонят и так, нельзя."""
    with get_session() as db:
        if req.session_id and q.is_guest_answered_repeat(
                db, gid, req.session_id, req.message,
                since=plans.naive_utc(datetime.now(timezone.utc)) - REPEAT_WINDOW):
            logger.info("гость: повтор отвеченного вопроса не засчитывается")
            return False
        if q.count_guest_answers(db, gid) >= settings.GUEST_TRIAL_QUESTIONS:
            raise HTTPException(status_code=402, detail=TRIAL_OVER)
        if q.count_guest_answers_since(db, _msk_day_start()) >= settings.GUEST_DAILY_TOTAL:
            logger.warning("гость: общий суточный потолок пробного режима исчерпан")
            raise HTTPException(status_code=429, detail=DAILY_OVER)
    key = f"guest:{client_ip(request)}"
    if not _ip_limit.check(key):
        logger.warning("гость: потолок по адресу за сутки исчерпан")  # адрес в журнал не пишем
        raise HTTPException(status_code=429, detail=IP_OVER,
                            headers={"Retry-After": str(_ip_limit.retry_after(key))})
    return True


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


@router.post("/api/guest/chat", response_model=GuestChatResponse)
def guest_chat(req: ChatRequest, request: Request) -> GuestChatResponse:
    gid = _require_guest(request)
    _reject_sensitive(req.message, "guest")   # до счёта: отклонённый вопрос пробу не тратит
    charge = _enforce_trial(request, gid, req)
    session_id = req.session_id or uuid.uuid4().hex
    history = _load_history(gid, session_id)
    _log(gid, session_id, "user", req.message)  # R26: вопрос — до движка, как в /api/chat
    try:
        ans = answer(req.message, **_engine_kwargs(req, history))
    except Exception:  # noqa: BLE001
        logger.exception("гость: движок упал")
        raise HTTPException(status_code=503, detail="Сервис временно недоступен, повторите запрос.")
    sources = _guest_sources(ans)
    _log(gid, session_id, "assistant", ans.text, sources=[s.model_dump() for s in sources],
         low_relevance=ans.low_relevance, prompt_tokens=ans.prompt_tokens,
         completion_tokens=ans.completion_tokens, charged=charge and ans.metered)
    with get_session() as db:
        trial = trial_view(db, gid)
    return GuestChatResponse(
        answer=ans.text, sources=sources, low_relevance=ans.low_relevance,
        unverified_numbers=ans.unverified_numbers, session_id=session_id, message_id=None,
        input_hint=ans.input_hint, trial=trial)


@router.post("/api/guest/chat/stream")
def guest_chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    """SSE как у `/api/chat/stream`; на 'done' — ещё остаток пробных вопросов. Вопрос и ответ
    пишутся на финале (сбой стрима фронт отрабатывает фолбэком, который вопрос запишет сам)."""
    gid = _require_guest(request)
    _reject_sensitive(req.message, "guest")
    charge = _enforce_trial(request, gid, req)
    session_id = req.session_id or uuid.uuid4().hex
    history = _load_history(gid, session_id)

    def gen():
        try:
            for kind, payload in answer_stream(req.message, **_engine_kwargs(req, history)):
                if kind == "delta":
                    yield _sse({"type": "delta", "text": payload})
                    continue
                ans = payload
                sources = _guest_sources(ans)
                _log(gid, session_id, "user", req.message)
                _log(gid, session_id, "assistant", ans.text,
                     sources=[s.model_dump() for s in sources], low_relevance=ans.low_relevance,
                     prompt_tokens=ans.prompt_tokens, completion_tokens=ans.completion_tokens,
                     charged=charge and ans.metered)
                with get_session() as db:
                    trial = trial_view(db, gid)
                yield _sse({
                    "type": "done", "message_id": None,
                    "sources": [s.model_dump() for s in sources],
                    "low_relevance": ans.low_relevance,
                    "unverified_numbers": ans.unverified_numbers,
                    "session_id": session_id, "input_hint": ans.input_hint, "trial": trial,
                })
        except Exception:  # noqa: BLE001 — сигнал фолбэка фронту
            logger.exception("гость: стрим упал")
            yield _sse({"type": "error", "detail": "stream_failed"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
