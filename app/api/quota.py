"""Лимиты запросов по тарифам (#160): сколько осталось пользователю в текущем периоде.

Решения владельца (07.10.2026): период — месяц от даты подключения, по исчерпании запросы
блокируются. Что списывается — ОТВЕТ движка на вопрос (`Answer.metered`): заготовки meta
(приветствие, «спасибо», «что умеешь») бесплатны, сбой (503, оборванный стрим) не списывается
— ответа не было. Направление ошибки выбрано в пользу клиента: недосчитать дешевле, чем
заблокировать честного пользователя раньше срока.

⚠ Проверка (до движка) и списание (после ответа) разнесены: два параллельных запроса на
последней единице пройдут оба. Перерасход — единицы, его держит лимит частоты `R12`.
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


def exhausted_message(st: QuotaState) -> str:
    return (f"Лимит тарифа «{st.plan}» исчерпан: {st.used} из {st.limit} запросов за период "
            f"с {plans.msk_date(st.start):%d.%m.%Y}. Новый период начнётся "
            f"{plans.msk_date(st.end):%d.%m.%Y}. Чтобы расширить тариф, обратитесь в Курскую ТПП.")
