"""Сводка админ-панели (`/admin`, этап 2 пересборки 09.10.2026): работает ли сервис и что требует
внимания — сегодня и с начала месяца (по Москве).

Отдельно от приёмочного скоркарда пилота (`admin_stats.build_admin_view`, раздел «Качество»):
там — оценки ответов за любой срез, здесь — эксплуатация. И читает сводка меньше: реплики только
за текущий месяц и без текстов (`queries.message_rows_since`), тексты — лишь у последних сбоев.

«Без ответа» — вопрос, за которым в той же беседе не последовал ответ: движок упал (503) или
поток оборвался и фолбэк не ответил. Вопросы моложе `FRESH` не считаются: на них ответ, возможно,
ещё пишется. Расход — оценка по токенам (`core.costs`), как в «Качестве»: не для бухгалтерии.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api import quota
from app.api.auth import needs_profile
from app.core import plans
from app.core.config import settings
from app.core.costs import cost_rub
from app.db import queries as q

FRESH = timedelta(minutes=2)   # моложе — ответ, возможно, ещё пишется
SOON = timedelta(days=7)       # «истекает скоро»
FAILURES_SHOWN = 10


def _month_start(now: datetime) -> datetime:
    """00:00 МСК первого числа текущего месяца — в наивном UTC, как время лежит в базе."""
    today = plans.msk_today(now)
    return plans.anchor_from_date(today.replace(day=1))


def _window(rows, since, until_fresh) -> dict:
    """Вопросы, ответы, без ответа, активные пользователи и ₽ — по репликам с момента `since`."""
    questions = answers = 0
    users, unanswered, rub = set(), [], 0.0
    pending = {}  # (user, session) → последний вопрос без ответа
    for r in rows:
        if r.ts < since:
            continue
        key = (r.user_id, r.session_id)
        if r.role == "user":
            questions += 1
            users.add(r.user_id)
            if key in pending:                 # прошлый вопрос беседы так и остался без ответа
                unanswered.append(pending[key])
            pending[key] = r
        else:
            answers += 1
            rub += cost_rub(r.prompt_tokens, r.completion_tokens)
            pending.pop(key, None)
    unanswered += [r for r in pending.values() if r.ts < until_fresh]
    return {"questions": questions, "answers": answers, "unanswered": unanswered,
            "users": len(users), "rub": rub}


def _guests(rows, since) -> dict:
    asked = sum(1 for r in rows if r.ts >= since and r.role == "user")
    rub = sum(cost_rub(r.prompt_tokens, r.completion_tokens) for r in rows
              if r.ts >= since and r.role == "assistant")
    charged = sum(1 for r in rows if r.ts >= since and r.charged)
    return {"questions": asked, "charged": charged, "rub": rub}


def summary_view(db, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    now_n = plans.naive_utc(now)
    today = plans.anchor_from_date(plans.msk_today(now))
    month = _month_start(now)
    rows = q.message_rows_since(db, month)
    grows = q.guest_rows_since(db, month)
    fresh = now_n - FRESH
    periods = {}
    for key, since in (("today", today), ("month", month)):
        w, g = _window(rows, since, fresh), _guests(grows, since)
        periods[key] = {**w, "unanswered_n": len(w["unanswered"]), "guest": g,
                        "rub_total": w["rub"] + g["rub"]}

    users = q.list_users(db)
    by_id = {u.id: u for u in users}
    expiring, expired, exhausted = [], [], []
    for u in users:
        if not u.plan or u.role == "admin":   # тариф admin'а не закрывается никогда (plan_expired)
            continue
        if plans.plan_expired(u, now):
            expired.append(u)
        elif u.plan_expires_at is not None and plans.naive_utc(u.plan_expires_at) - now_n <= SOON:
            expiring.append(u)
        st = quota.quota_state(db, u, now)
        if st is not None and st.exhausted and not plans.plan_expired(u, now):
            exhausted.append((u, st))
    expiring.sort(key=lambda u: u.plan_expires_at)

    fails = sorted(periods["month"]["unanswered"], key=lambda r: r.id, reverse=True)[:FAILURES_SHOWN]
    failures = []
    for r in fails:
        m = q.get_message(db, r.id)
        u = by_id.get(r.user_id)
        failures.append({"user": u.username if u else "—", "user_id": r.user_id, "session_id": r.session_id,
                         "ts": f"{plans.naive_utc(r.ts) + plans.MSK_OFFSET:%d.%m %H:%M}",
                         "text": (m.content if m else "")})

    return {
        "periods": periods,
        "month_label": f"{plans.msk_today(now):%m.%Y}",
        "failures": failures,
        "expiring": [{"id": u.id, "username": u.username, "plan": u.plan,
                      "until": f"{plans.msk_date(u.plan_expires_at):%d.%m.%Y}"} for u in expiring],
        "expired": [{"id": u.id, "username": u.username, "plan": u.plan,
                     "until": f"{plans.msk_date(u.plan_expires_at):%d.%m.%Y}"} for u in expired],
        "exhausted": [{"id": u.id, "username": u.username, "plan": u.plan,
                       "renews": f"{plans.msk_date(st.end):%d.%m.%Y}"} for u, st in exhausted],
        "accounts": {
            "total": len(users),
            "blocked": sum(1 for u in users if u.blocked_at is not None),
            "no_profile": sum(1 for u in users if needs_profile(u)),   # правило гейта чата, не копия
            # организацию вписал пользователь, admin ещё не подтвердил (вечер 09.10.2026)
            "org_unverified": sum(1 for u in users if q.org_unverified(u)),   # одно правило со списком
        },
        "leads": {"total": q.count_leads(db), "week": q.count_leads_since(db, today - timedelta(days=7)),
                  "new": q.count_leads(db, "new")},
        "trial": {"enabled": settings.GUEST_TRIAL_ENABLED, "cap": settings.GUEST_DAILY_TOTAL,
                  "today": q.count_guest_answers_since(db, today)},
    }
