"""Страница «История запросов» (макет «Navigator 719 Service», 08.10.2026, `test32`).

Беседы пользователя одним списком: поиск по вопросам И ответам, фильтр по периоду, группы по датам
(«Сегодня», «Вчера», «На этой неделе», «Ранее»), у каждой беседы — первый вопрос, начало первого
ответа, источники, на которые он сослался, и время последней активности.

Почему на сервере, а не фильтром списка в браузере: искать нужно и по ОТВЕТАМ, а тексты ответов в
сайдбар не грузятся намеренно (R24 — они основной объём). Страницу открывают редко, поэтому полный
проход по репликам пользователя здесь допустим, а на каждом открытии чата — нет.

Время — по Москве, как у расхода тарифа (#160): так его читают и пользователь, и admin.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from app.api.auth import require_user
from app.core import plans
from app.db.engine import get_session
from app.db.models import Message, User
from sqlalchemy import select

router = APIRouter()

PERIODS = ("all", "today", "week", "earlier")
GROUPS = ("Сегодня", "Вчера", "На этой неделе", "Ранее")
# Какие группы показывает фильтр — как в макете: «Неделя» = сегодня + вчера + неделя.
PERIOD_GROUPS = {"all": {0, 1, 2, 3}, "today": {0}, "week": {0, 1, 2}, "earlier": {3}}
MAX_REFS = 3
EXCERPT_LEN = 220
# [3] или [1, 2] — та же форма, что размечает фронт (chat.js, REF_RE)
REF_RE = re.compile(r"\[(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]")


@dataclass
class _Conv:
    session_id: str
    question: str = ""
    answer: str = ""
    sources: list = field(default_factory=list)
    last_ts: datetime | None = None
    texts: list = field(default_factory=list)   # всё, что ищется: вопросы и ответы беседы


def group_index(day: date, today: date) -> int:
    """Номер группы по календарным дням (МСК): 0 сегодня, 1 вчера, 2 — до недели назад, 3 — раньше."""
    delta = (today - day).days
    if delta <= 0:
        return 0
    if delta == 1:
        return 1
    if delta < 7:
        return 2
    return 3


def excerpt(text: str, n: int = EXCERPT_LEN) -> str:
    """Начало ответа одной строкой: без таблиц, разметки и номеров источников."""
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("|"):
            continue
        s = REF_RE.sub("", s).replace("**", "").replace("_", " ")
        s = re.sub(r"^[•\-*]\s+", "", s)
        lines.append(s)
    out = re.sub(r"\s+", " ", " ".join(lines)).strip()
    out = re.sub(r"\s+([.,;:])", r"\1", out)   # «позиция [1].» → «позиция.», а не «позиция .»
    return out if len(out) <= n else out[: n - 1].rstrip() + "…"


def ref_label(src: dict) -> str:
    """Короткая подпись источника для чипа: пункт документа — его меткой, позиция приложения —
    «Разд. III, поз. 2» (наименование продукции в чип не помещается)."""
    if src.get("kind") == "rule" or (src.get("url") and not src.get("okpd2")):
        return (src.get("product_name") or "").strip()[:48]
    anchor = src.get("source_anchor") or ""
    m = re.search(r"Раздел\s+([IVXLC]+),\s*позиция\s+(\d+)", anchor)
    if m:
        return f"Разд. {m.group(1)}, поз. {m.group(2)}"
    name = (src.get("product_name") or "").strip()
    return name if len(name) <= 40 else name[:39] + "…"


def cited_refs(answer: str, sources: list) -> list[str]:
    """Подписи источников, на которые ответ СОСЛАЛСЯ ([N]), в порядке первой ссылки, без повторов."""
    out: list[str] = []
    for m in REF_RE.finditer(answer or ""):
        for x in re.split(r"[,;]", m.group(1)):
            i = int(x) - 1
            if 0 <= i < len(sources):
                label = ref_label(sources[i])
                if label and label not in out:
                    out.append(label)
    return out[:MAX_REFS]


def build_history(messages, q: str = "", period: str = "all", now: datetime | None = None) -> dict:
    """Группы истории из реплик пользователя (по возрастанию времени). Чистая функция — без БД."""
    convs: dict[str, _Conv] = {}
    for m in messages:
        c = convs.setdefault(m.session_id, _Conv(m.session_id))
        c.texts.append(m.content or "")
        if m.role == "user" and not c.question:
            c.question = m.content or ""
        elif m.role == "assistant" and not c.answer:
            c.answer = m.content or ""
            try:
                c.sources = json.loads(m.sources_json) if m.sources_json else []
            except ValueError:
                c.sources = []
        if c.last_ts is None or (m.ts and m.ts > c.last_ts):
            c.last_ts = m.ts

    needle = (q or "").strip().lower()
    allow = PERIOD_GROUPS.get(period, PERIOD_GROUPS["all"])
    today = plans.msk_today(now)
    groups: list[list[dict]] = [[] for _ in GROUPS]
    for c in sorted(convs.values(), key=lambda x: x.last_ts or datetime.min, reverse=True):
        if not c.question and not c.answer:
            continue
        if needle and not any(needle in t.lower() for t in c.texts):
            continue
        day = plans.msk_date(c.last_ts) if c.last_ts else today
        gi = group_index(day, today)
        if gi not in allow:
            continue
        local = plans.naive_utc(c.last_ts) + plans.MSK_OFFSET if c.last_ts else None
        if local is None:
            when = ""
        elif gi <= 1:
            when = f"{local:%H:%M}"
        else:
            when = f"{local:%d.%m}" if local.year == today.year else f"{local:%d.%m.%y}"
        groups[gi].append({
            "session_id": c.session_id,
            "q": (c.question or "Диалог").strip(),
            "a": excerpt(c.answer),
            "refs": cited_refs(c.answer, c.sources),
            "time": when,
        })
    return {"groups": [{"label": GROUPS[i], "items": items} for i, items in enumerate(groups) if items]}


@router.get("/api/history")
def history(q: str = "", period: str = "all", user: User = Depends(require_user)) -> dict:
    """Б3: история запросов пользователя — только свои беседы (фильтр по `user_id`)."""
    with get_session() as db:
        msgs = list(db.execute(
            select(Message).where(Message.user_id == user.id).order_by(Message.ts, Message.id)
        ).scalars())
        return build_history(msgs, q=q[:200], period=period if period in PERIODS else "all",
                             now=datetime.now(timezone.utc))
