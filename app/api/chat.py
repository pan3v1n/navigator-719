"""Чат-эндпоинт веб-UI 1.0: вопрос эксперта → движок навигатора → лог диалога в БД → ответ.

Эндпоинт СИНХРОННЫЙ (`def`) — FastAPI исполняет его в threadpool (как `/navigate`); движок
(`pipeline.answer`: e5 + Qdrant + DeepSeek) блокирующий, event loop не держим. Диалог группируется
по `session_id` (одна беседа = один id). Реплики (вопрос + ответ + флаги качества) логируются в БД —
их видит admin. History-aware follow-up («а какой порог?») — отложенный fast-follow: сейчас каждый
вопрос самостоятелен.
"""

from __future__ import annotations

import json
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from app.api.auth import require_user, require_user_profiled
from app.db import queries as q
from app.db.engine import get_session
from app.db.models import User
from app.rag.pipeline import answer, answer_stream
from app.tools.navigator import extract_okpd2

router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None  # None → начать новую беседу


class SourceItem(BaseModel):
    section: str | None = None
    product_name: str
    okpd2: list[str] = []
    source_anchor: str | None = None
    okpd2_match: bool = False
    url: str | None = None  # прямая ссылка на первоисточник (процедурные источники); None → фронт строит по ОКПД2/наим.


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceItem]
    low_relevance: bool
    unverified_numbers: list[str]
    session_id: str
    message_id: int | None = None  # id сохранённой реплики-ответа — для привязки оценки/исправления


def _sources_from_hits(hits) -> list[SourceItem]:
    return [
        SourceItem(
            section=h.section_title or h.section_roman,
            product_name=h.product_name,
            okpd2=h.okpd2_codes or [],
            source_anchor=h.source_anchor,
            okpd2_match=h.okpd2_match,
        )
        for h in hits
    ]


# --- Кликабельные источники ПРОЦЕДУРНОГО ответа (пункты Правил/тела ПП №719/Приказа №52). ----------
# Первоисточники на Контур.Норматив. Правила формирования и ведения реестра — раздел внутри документа
# ПП №719 (якорь #h14240), поэтому та же ссылка, что и на тело, но с якорем раздела.
_KONTUR_719 = "https://normativ.kontur.ru/document?moduleId=1&documentId=506899"
_KONTUR_PRIKAZ_52 = "https://normativ.kontur.ru/document?moduleId=1&documentId=505398"
_RULES_ANCHOR = "#h14240"  # раздел «Правила формирования и ведения реестра» внутри документа 719
_DOC_NAMES = {
    "tpp_order_52": "Приказ ТПП РФ №52",
    "rules_registry": "Правила ведения реестра",
    "decree_body": "Тело ПП №719",
}


def _rule_url(doc_type: str, text: str) -> str:
    """Ссылка на первоисточник пункта + текст-фрагмент (`:~:text=`) для точной прокрутки браузером.
    Приказ №52 — отдельный документ; Правила — раздел документа 719 (якорь h14240); тело — сам 719."""
    if doc_type == "tpp_order_52":
        base, anchor = _KONTUR_PRIKAZ_52, ""
    elif doc_type == "rules_registry":
        base, anchor = _KONTUR_719, _RULES_ANCHOR
    else:  # decree_body и фолбэк
        base, anchor = _KONTUR_719, ""
    snippet = " ".join((text or "").split()[:8]).strip()  # первые ~8 слов пункта — цель прокрутки
    if not snippet:
        return base + anchor
    frag = ":~:text=" + quote(snippet)  # текст-директива добавляется к фрагменту (после якоря или #)
    return base + (anchor + frag if anchor else "#" + frag)


def _sources_from_rules(rules) -> list[SourceItem]:
    """Источники процедурного ответа в порядке [n]: метка пункта (source_anchor) + прямая ссылка на
    первоисточник. ОКПД2 у норм нет — клик ведёт по `url`, а не по коду (см. фронт addSources)."""
    out: list[SourceItem] = []
    for r in rules:
        anchor = (r.get("source_anchor") or "").strip()
        doc_type = r.get("doc_type") or ""
        doc_name = _DOC_NAMES.get(doc_type, "Правила ведения реестра")
        out.append(SourceItem(
            section=doc_name,
            product_name=anchor or doc_name,
            okpd2=[],
            source_anchor=None,  # уже в product_name (метка пункта) — не дублируем в подписи
            okpd2_match=False,
            url=_rule_url(doc_type, r.get("text") or ""),
        ))
    return out


def _load_history(user_id: int, session_id: str, max_msgs: int = 12) -> list[dict]:
    """Последние реплики беседы для мультитёрн-контекста (ответы ассистента усекаем). Окно 12 реплик
    (~6 ходов) — «полный контекст чата» в разумных пределах токенов; для очень длинных бесед позже
    добавить сжатие старых ходов. Пусто для новой беседы — движок ведёт себя как одиночный вопрос."""
    try:
        with get_session() as db:
            prev = q.get_session_messages(db, user_id, session_id)
    except Exception:  # noqa: BLE001 — история не критична, не роняем ответ
        return []
    return [
        {"role": m.role, "content": m.content if m.role == "user" else m.content[:600]}
        for m in prev[-max_msgs:]
    ]


@router.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: User = Depends(require_user_profiled)) -> ChatResponse:
    session_id = req.session_id or uuid.uuid4().hex
    history = _load_history(user.id, session_id)  # мультитёрн: прошлые ходы беседы (пусто для новой)
    okpd2 = extract_okpd2(req.message)  # код ОКПД2 в тексте → авторитетный иерархический буст + правило 3а
    try:
        ans = answer(req.message, okpd2=okpd2, history=history)
    except Exception:  # noqa: BLE001 — наружу дружелюбно, детали в лог (рваная сеть/DeepSeek)
        logger.exception(f"chat: движок упал на запросе от user_id={user.id}")
        raise HTTPException(status_code=503, detail="Сервис временно недоступен, повторите запрос.")

    sources = _sources_from_hits(ans.hits) or _sources_from_rules(ans.rule_sources)
    message_id: int | None = None
    try:
        with get_session() as db:
            q.log_message(db, user_id=user.id, session_id=session_id, role="user", content=req.message)
            asst = q.log_message(
                db, user_id=user.id, session_id=session_id, role="assistant", content=ans.text,
                sources=[s.model_dump() for s in sources],
                low_relevance=ans.low_relevance, unverified=ans.unverified_numbers,
                prompt_tokens=ans.prompt_tokens, completion_tokens=ans.completion_tokens,
            )
            message_id = asst.id
    except Exception:  # noqa: BLE001 — лог не должен ронять ответ эксперту
        logger.exception(f"chat: не удалось записать лог диалога (user_id={user.id})")

    return ChatResponse(
        answer=ans.text, sources=sources, low_relevance=ans.low_relevance,
        unverified_numbers=ans.unverified_numbers, session_id=session_id, message_id=message_id,
    )


def _sse(event: dict) -> str:
    """Одно SSE-событие: `data: <json>\\n\\n`. ensure_ascii=False — кириллица идёт как есть."""
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@router.post("/api/chat/stream")
def chat_stream(req: ChatRequest, user: User = Depends(require_user_profiled)) -> StreamingResponse:
    """Стриминг ответа (T18): SSE-поток `text/event-stream`. События: {"type":"delta","text":…}
    по мере генерации, затем один {"type":"done", message_id, sources, low_relevance,
    unverified_numbers, session_id}. Лог реплики (user+assistant) и message_id — на 'done' (нужен
    полный текст). Обрыв/сбой движка → {"type":"error"} → фронт делает фолбэк на /api/chat.

    Эндпоинт СИНХРОННЫЙ (`def`): Starlette крутит sync-генератор в threadpool (как /api/chat),
    event loop не держим. ⚠ Рваная РФ-сеть: длинный SSE хрупок — на фронте фолбэк обязателен."""
    session_id = req.session_id or uuid.uuid4().hex
    history = _load_history(user.id, session_id)  # мультитёрн: прошлые ходы (пусто для новой беседы)
    okpd2 = extract_okpd2(req.message)

    def gen():
        try:
            for kind, payload in answer_stream(req.message, okpd2=okpd2, history=history):
                if kind == "delta":
                    yield _sse({"type": "delta", "text": payload})
                    continue
                # kind == "done": payload — Answer. Логируем диалог и отдаём метаданные.
                ans = payload
                sources = _sources_from_hits(ans.hits) or _sources_from_rules(ans.rule_sources)
                message_id: int | None = None
                try:
                    with get_session() as db:
                        q.log_message(db, user_id=user.id, session_id=session_id,
                                      role="user", content=req.message)
                        asst = q.log_message(
                            db, user_id=user.id, session_id=session_id, role="assistant",
                            content=ans.text, sources=[s.model_dump() for s in sources],
                            low_relevance=ans.low_relevance, unverified=ans.unverified_numbers,
                            prompt_tokens=ans.prompt_tokens, completion_tokens=ans.completion_tokens,
                        )
                        message_id = asst.id
                except Exception:  # noqa: BLE001 — лог не должен ронять стрим (как в /api/chat)
                    logger.exception(f"chat_stream: не удалось записать лог (user_id={user.id})")
                yield _sse({
                    "type": "done", "message_id": message_id,
                    "sources": [s.model_dump() for s in sources],
                    "low_relevance": ans.low_relevance,
                    "unverified_numbers": ans.unverified_numbers,
                    "session_id": session_id,
                })
        except Exception:  # noqa: BLE001 — движок упал (рваная сеть/DeepSeek) → сигнал фолбэка фронту
            logger.exception(f"chat_stream: движок упал на запросе от user_id={user.id}")
            yield _sse({"type": "error", "detail": "stream_failed"})

    # X-Accel-Buffering:no — отключить буферизацию у обратного прокси (если появится nginx впереди).
    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/conversations")
def list_conversations(user: User = Depends(require_user)) -> list[dict]:
    """Список бесед пользователя для сайдбара (новые сверху)."""
    with get_session() as db:
        sess = q.get_user_sessions(db, user.id)
    return [
        {"session_id": s["session_id"], "title": (s["title"] or "Диалог")[:48],
         "ts": s["ts"].isoformat()}
        for s in sess
    ]


@router.get("/api/conversations/{session_id}")
def get_conversation(session_id: str, user: User = Depends(require_user)) -> dict:
    """Реплики одной беседы для переоткрытия (только свои — фильтр по user_id)."""
    with get_session() as db:
        msgs = q.get_session_messages(db, user.id, session_id)
        out = []
        for m in msgs:
            item = {"role": m.role, "content": m.content}
            if m.sources_json:
                item["sources"] = json.loads(m.sources_json)
            out.append(item)
    return {"session_id": session_id, "messages": out}


@router.delete("/api/conversations/{session_id}")
def delete_conversation(session_id: str, user: User = Depends(require_user)) -> dict:
    """Удалить беседу пользователя (только свою — фильтр по user_id)."""
    with get_session() as db:
        removed = q.delete_session(db, user.id, session_id)
    return {"ok": True, "removed": removed}


def _serialize_messages(msgs, include_sources: bool = True) -> list[dict]:
    out = []
    for m in msgs:
        d = {"role": m.role, "content": m.content, "ts": m.ts.isoformat() if m.ts else None}
        if include_sources and m.sources_json:
            d["sources"] = json.loads(m.sources_json)
        out.append(d)
    return out


def _parse_date(s: str):
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


def _convs_to_markdown(user_name: str, picked) -> str:
    lines = [f"# Экспорт диалогов — {user_name}", ""]
    for s, msgs in picked:
        lines.append(f"## {s['title'] or 'Диалог'}")
        if s["ts"]:
            lines.append(f"_{s['ts'].strftime('%d.%m.%Y %H:%M')}_")
        lines.append("")
        for m in msgs:
            who = "Эксперт" if m.role == "user" else "Ассистент"
            lines += [f"**{who}:**", "", m.content, ""]
        lines += ["---", ""]
    return "\n".join(lines)


def _convs_to_text(user_name: str, picked) -> str:
    lines = [f"Экспорт диалогов — {user_name}", "=" * 40, ""]
    for s, msgs in picked:
        stamp = f"  [{s['ts'].strftime('%d.%m.%Y %H:%M')}]" if s["ts"] else ""
        lines += [(s["title"] or "Диалог") + stamp, "-" * 30]
        for m in msgs:
            who = "Эксперт" if m.role == "user" else "Ассистент"
            lines += [f"{who}:", m.content, ""]
        lines.append("")
    return "\n".join(lines)


def _json_download(payload: dict, filename: str) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _download(content: str, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _conv_to_markdown(title: str, msgs) -> str:
    lines = [f"# {title}", ""]
    for m in msgs:
        who = "Эксперт" if m.role == "user" else "Ассистент"
        lines += [f"**{who}:**", "", m.content, ""]
    return "\n".join(lines)


def _conv_to_text(title: str, msgs) -> str:
    lines = [title, "=" * min(len(title), 60), ""]
    for m in msgs:
        who = "Эксперт" if m.role == "user" else "Ассистент"
        lines += [f"{who}:", m.content, ""]
    return "\n".join(lines)


@router.get("/api/export")
def export_conversations(
    fmt: str = "json", date_from: str = "", date_to: str = "", sources: int = 1,
    user: User = Depends(require_user),
) -> Response:
    """Скачать беседы пользователя с настройками: формат (json/md/txt), период дат, источники."""
    df, dt = _parse_date(date_from), _parse_date(date_to)
    incl_sources = bool(sources)
    picked = []  # список (session-dict, msgs) после фильтра по датам
    with get_session() as db:
        for s in q.get_user_sessions(db, user.id):
            d = s["ts"].date() if s["ts"] else None
            if df and (d is None or d < df):
                continue
            if dt and (d is None or d > dt):
                continue
            picked.append((s, q.get_session_messages(db, user.id, s["session_id"])))

    base = "navigator719-chats"
    if fmt == "md":
        return _download(_convs_to_markdown(user.username, picked), "text/markdown; charset=utf-8", base + ".md")
    if fmt == "txt":
        return _download(_convs_to_text(user.username, picked), "text/plain; charset=utf-8", base + ".txt")
    conversations = [
        {"session_id": s["session_id"], "title": s["title"],
         "messages": _serialize_messages(msgs, incl_sources)}
        for s, msgs in picked
    ]
    return _json_download(
        {"user": user.username,
         "filters": {"date_from": date_from or None, "date_to": date_to or None, "sources": incl_sources},
         "exported_conversations": len(conversations), "conversations": conversations},
        base + ".json",
    )


@router.get("/api/conversations/{session_id}/export")
def export_conversation(session_id: str, fmt: str = "json", user: User = Depends(require_user)) -> Response:
    """Скачать ОДНУ беседу пользователя (только свою). Формат fmt: json | md | txt."""
    with get_session() as db:
        msgs = q.get_session_messages(db, user.id, session_id)
        title = next((m.content for m in msgs if m.role == "user"), "Диалог")
    base = f"navigator719-chat-{session_id[:8]}"
    if fmt == "md":
        return _download(_conv_to_markdown(title, msgs), "text/markdown; charset=utf-8", base + ".md")
    if fmt == "txt":
        return _download(_conv_to_text(title, msgs), "text/plain; charset=utf-8", base + ".txt")
    return _json_download(
        {"user": user.username, "conversation": {
            "session_id": session_id, "title": title, "messages": _serialize_messages(msgs)}},
        base + ".json",
    )
