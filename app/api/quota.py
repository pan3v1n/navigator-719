"""Лимиты запросов по тарифам (#160): сколько осталось пользователю в текущем периоде.

Решения владельца (07.10.2026): период — месяц от даты подключения, по исчерпании запросы
блокируются. Что списывается — ОТВЕТ движка на вопрос (`Answer.metered`): заготовки meta
(приветствие, «спасибо», «что умеешь») и деферы процедурной ветки (корпус недоступен / ветка
выключена) бесплатны, сбой (503, оборванный стрим) не списывается — ответа не было; повтор уже
отвеченного вопроса в той же беседе тоже (`chat._enforce_plan_quota`). Расход ведётся только у
пользователей с лимитом. Направление ошибки выбрано в пользу клиента: недосчитать дешевле, чем
заблокировать честного пользователя раньше срока.

⚠ Проверка (до движка) и списание (после ответа) разнесены: параллельные запросы на последней
единице пройдут все. Предел перерасхода — лимит частоты `R12` (`RATE_LIMIT_CHAT_PER_MIN`, 20 в
минуту): интерфейс так не шлёт (кнопка заблокирована до ответа), это сценарий скрипта.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core import plans
from app.db import queries as q


@dataclass(frozen=True)
class QuotaState:
    plan: str
    limit: int
    used: int
    start: datetime  # начало периода, наивное UTC (включительно)
    end: datetime    # начало следующего периода, наивное UTC

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


def quota_state(db: Session, user, now: datetime | None = None) -> QuotaState | None:
    """Состояние лимита; None — у пользователя лимита нет (тариф не назначен или admin)."""
    limit = plans.plan_limit(user)
    if limit is None:
        return None
    # Даты подключения нет только у строки, заведённой мимо админки, — считаем от создания учётки.
    anchor = user.plan_started_at or user.created_at
    start, end = plans.period_bounds(anchor, now or datetime.now(timezone.utc))
    return QuotaState(user.plan, limit, q.count_answer_usage(db, user.id, start, end), start, end)


def quota_view(db: Session, user) -> dict | None:
    """Расход для интерфейса (Б4, дизайн 08.10.2026): карточка в сайдбаре и строка под полем ввода.

    `renews` — день, с которого начинается новый период (по Москве): макет пишет «осталось N до
    этой даты», и конец текущего периода ровно на её 00:00 МСК. None — у пользователя нет лимита,
    карточки нет."""
    st = quota_state(db, user)
    if st is None:
        return None
    # Тариф, который кончится раньше нового периода (неделя «Пробного режима»), не «обновит лимит»:
    # интерфейс тогда называет дату окончания тарифа, а не начало периода, которого не будет.
    exp = getattr(user, "plan_expires_at", None)
    renews_first = not isinstance(exp, datetime) or st.end < plans.naive_utc(exp)
    return {"plan": st.plan, "limit": st.limit, "used": min(st.used, st.limit),
            "remaining": st.remaining, "renews": f"{plans.msk_date(st.end):%d.%m.%Y}",
            "renews_first": renews_first, **validity(user)}


def validity(user) -> dict:
    """Срок тарифа для интерфейса (09.10.2026): «до 16.10.2026» или бессрочно, и истёк ли он."""
    exp = getattr(user, "plan_expires_at", None)
    return {"expires": f"{plans.msk_date(exp):%d.%m.%Y}" if exp else None,
            "expired": plans.plan_expired(user)}


def plan_view(db: Session, user) -> dict | None:
    """Карточка тарифа в кабинете: название, срок и — если у тарифа есть лимит — расход.
    None — тариф не назначен. В отличие от `quota_view`, есть и у тарифов без лимита
    («Тестировщик», «Администратор»): срок у них тоже бывает."""
    if not getattr(user, "plan", None):
        return None
    return {"plan": user.plan, "quota": quota_view(db, user), **validity(user)}


def expired_message(user) -> str:
    return (f"Срок действия тарифа «{user.plan}» истёк {plans.msk_date(user.plan_expires_at):%d.%m.%Y}. "
            f"Чтобы продлить, обратитесь в Курскую ТПП.")


def exhausted_message(st: QuotaState) -> str:
    return (f"Лимит тарифа «{st.plan}» исчерпан: {st.used} из {st.limit} запросов за период "
            f"с {plans.msk_date(st.start):%d.%m.%Y}. Новый период начнётся "
            f"{plans.msk_date(st.end):%d.%m.%Y}. Чтобы расширить тариф, обратитесь в Курскую ТПП.")
