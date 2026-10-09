"""Тарифы сервиса и их расчётный период (#160) — чистая арифметика, без БД.

Решения владельца (07.10.2026): объёмы — из макета лендинга (до 100 / 200 / 400 запросов), период —
месяц ОТ ДАТЫ ПОДКЛЮЧЕНИЯ, а не календарный. Дата подключения — день, который назначил admin;
период начинается в 00:00 по Москве, потому что так её читает и admin, и пользователь.

⚠ Таблица объёмов здесь и подписи тарифов на лендинге (`landing/index.html`) — про одно и то же;
их согласованность держит тест, а не память (`test_leads.TestLandingPage`).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

PLAN_LIMITS: dict[str, int] = {"Старт": 100, "Стандарт": 200, "Профи": 400}

# Внутренние тарифы (решение владельца 09.10.2026): назначает admin, на лендинге их нет, поэтому
# отдельно от PLAN_LIMITS — ту сверяет с лендингом тест. «Пробный режим» — права платного тарифа
# (лимит младшего, «Старт»); когда подключат оплату, он будет выдаваться на неделю за 1 ₽.
# None — без ограничения числа запросов.
TRIAL_PLAN = "Пробный режим"
TRIAL_DAYS = 7  # срок «Пробного режима» по умолчанию, если admin не задал свой
INTERNAL_PLANS: dict[str, int | None] = {
    TRIAL_PLAN: PLAN_LIMITS["Старт"],
    "Тестировщик": None,
    "Администратор": None,
}
ALL_PLANS: dict[str, int | None] = {**PLAN_LIMITS, **INTERNAL_PLANS}

# Москва живёт без перехода на летнее время с 2014 г. — смещение постоянное.
MSK_OFFSET = timedelta(hours=3)


def plan_limit(user) -> int | None:
    """Лимит запросов пользователя за период; None — без лимита (тариф не назначен, тариф без
    ограничения или admin)."""
    if getattr(user, "role", None) == "admin":
        return None
    return ALL_PLANS.get(getattr(user, "plan", None) or "")


def plan_expired(user, now: datetime | None = None) -> bool:
    """Срок тарифа истёк — платный функционал (вопросы) закрыт (решение владельца 09.10.2026).

    `plan_expires_at` — 00:00 МСК дня, С КОТОРОГО тариф уже не действует («Активен до 16.10.2026»
    = последний день 15.10, как «Лимит обновится …» в макете). Без даты — бессрочно. admin не
    блокируется никогда: иначе некому было бы продлить."""
    exp = getattr(user, "plan_expires_at", None)
    if getattr(user, "role", None) == "admin" or not getattr(user, "plan", None) \
            or not isinstance(exp, datetime):
        return False
    return naive_utc(now or datetime.now(timezone.utc)) >= naive_utc(exp)


def naive_utc(dt: datetime) -> datetime:
    """Наивное UTC — в той форме, в какой время лежит в SQLite."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def add_months(d: datetime, n: int) -> datetime:
    """Сдвиг на n месяцев с прижатием дня к концу месяца (31.01 + 1 мес = 28/29.02)."""
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


def period_bounds(anchor: datetime, now: datetime) -> tuple[datetime, datetime]:
    """Текущий период [начало, конец) в наивном UTC.

    Считается от ДАТЫ ПОДКЛЮЧЕНИЯ каждый раз заново (`anchor` + k месяцев), а не от прошлого
    периода: иначе прижатие дня копилось бы — 31.01 → 28.02 → 28.03 вместо 31.03."""
    a = naive_utc(anchor) + MSK_OFFSET
    n = naive_utc(now) + MSK_OFFSET
    k = (n.year - a.year) * 12 + (n.month - a.month)
    if add_months(a, k) > n:
        k -= 1
    k = max(k, 0)  # подключение позже «сейчас» — первый период ещё не начался, считаем от него
    return add_months(a, k) - MSK_OFFSET, add_months(a, k + 1) - MSK_OFFSET


def anchor_from_date(d: date) -> datetime:
    """Дата подключения → начало первого периода: 00:00 МСК этого дня, в наивном UTC."""
    return datetime(d.year, d.month, d.day) - MSK_OFFSET


def msk_date(dt: datetime) -> date:
    """Календарная дата по Москве — для показа периода людям."""
    return (naive_utc(dt) + MSK_OFFSET).date()


def msk_today(now: datetime | None = None) -> date:
    return msk_date(now or datetime.now(timezone.utc))
