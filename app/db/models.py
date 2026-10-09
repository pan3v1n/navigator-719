"""SQLAlchemy-модели данных приложения 1.0: пользователи, реплики диалога, обратная связь.

Отдельная синхронная БД (SQLite, `settings.APP_DB_URL`) — НЕ путать с Qdrant (векторы 719)
и не с `DATABASE_URL` (async, зарезервирован под будущий Postgres). Здесь — операционные данные
мультиюзер-чата: кто, что спросил, что ответил движок, флаги качества, и фидбек. Логи видит admin.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="expert")  # user | expert | admin
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Профиль + согласие на обработку ПДн (152-ФЗ). Обязательны для роли `user` — жёсткий гейт до
    # чата (см. app/api/auth.needs_profile). Nullable/дефолтны, чтобы существующие admin/expert и
    # тесты (create_user без этих полей) не ломались; consent пишем один раз с consent_at.
    consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # момент согласия
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)            # ФИО
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    telegram: Mapped[str | None] = mapped_column(String(128), nullable=True)      # ник в Telegram
    # Кабинет по макету (09.10.2026): необязательные контакты — пользователь правит сам.
    position: Mapped[str | None] = mapped_column(String(128), nullable=True)      # должность
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)         # рабочий email
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Организация и ИНН — вписывает сам пользователь в кабинете (решение владельца 09.10.2026, вечер;
    # утром их вёл только admin). Чтобы не было фейков — статус подтверждения: ставит admin в карточке,
    # любая правка пользователя его снимает. В будущем — подтверждение через профиль работника на Госуслугах.
    org: Mapped[str | None] = mapped_column(Text, nullable=True)
    inn: Mapped[str | None] = mapped_column(String(12), nullable=True)
    org_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    org_verified_by: Mapped[int | None] = mapped_column(Integer, nullable=True)   # id admin'а
    # Тариф (#160): None — без лимита. Лимиты и период — `app/core/plans.py`; назначает admin.
    plan: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # дата подключения
    # Срок тарифа (09.10.2026): 00:00 МСК дня, с которого он уже не действует; None — бессрочно.
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Вид тарифа (09.10.2026): «individual» — личный, оплата с личной карты (когда появится); «corporate» —
    # корпоративный, продление только через связь с заказчиком (попап). Ставит admin; None — личный.
    plan_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Доступ (админка 09.10.2026): блокировка без удаления данных и «эпоха» входа — её поднимают
    # сброс пароля и блокировка, и все выданные сессии и куки «запомнить меня» гаснут сразу.
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auth_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")

    messages: Mapped[list["Message"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    feedback: Mapped[list["Feedback"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    usage: Mapped[list["AnswerUsage"]] = relationship(cascade="all, delete-orphan")


class Message(Base):
    """Одна реплика диалога. role='user' — вопрос эксперта; role='assistant' — ответ движка.
    Одна беседа = один `session_id` (группировка реплик в тред)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # заполняются только у ответов ассистента:
    sources_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    low_relevance: Mapped[bool] = mapped_column(Boolean, default=False)
    unverified_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # токены DeepSeek за этот ответ (для учёта затрат в админ-логах; эмбеддинг/Qdrant локальны)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped["User"] = relationship(back_populates="messages")


class Feedback(Base):
    """Обратная связь эксперта. `kind` различает канал:
    - 'answer'  — оценка ответа (звёзды 0..5) + опц. `correction` (исправление критич. ошибки), к `message_id`;
    - 'dialog'  — комментарий к беседе, к `session_id`;
    - 'service' — глобальный отзыв на сервис (оценка 1..5 + `matched` + `comment`), как в мини-1.0."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    kind: Mapped[str] = mapped_column(String(16), default="service")  # answer | dialog | service
    session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)  # → messages.id (логическая ссылка)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # answer: звёзды 0..5 / service: 1..5
    matched: Mapped[str | None] = mapped_column(String(16), nullable=True)  # service: да | частично | нет
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction: Mapped[str | None] = mapped_column(Text, nullable=True)  # исправление критич. ошибки (к ответу)

    user: Mapped["User"] = relationship(back_populates="feedback")


class AnswerUsage(Base):
    """Одна единица расхода тарифа (#160) — ответ движка пользователю.

    ⚠ Отдельно от `messages`, а не счётом по ним: беседы пользователь удаляет сам
    (`delete_session`), и расход обнулялся бы удалением истории. Здесь только кто и когда —
    ни вопроса, ни ответа."""

    __tablename__ = "answer_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class GuestMessage(Base):
    """Реплика пробного режима без входа (решение владельца 09.10.2026).

    ⚠ Обезличенно: ни учётки, ни IP-адреса. `guest_id` — случайный идентификатор из подписанной
    куки браузера; по нему считаются пробные вопросы и собирается мультитёрн беседы. Отдельно от
    `messages`, а не под техническим пользователем: иначе гостевые реплики попали бы в приёмочный
    скоркард, срезы по ролям и регионам и в расход тарифов. Видит только admin (`/admin`).
    `charged` — ответ засчитан в пробные вопросы (приветствия и повторы — нет)."""

    __tablename__ = "guest_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    guest_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    low_relevance: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charged: Mapped[bool] = mapped_column(Boolean, default=False)


class Lead(Base):
    """Заявка на подключение с лендинга 719-навигатор.рф (форма «Оставить заявку»).

    ⚠ ПДн (152-ФЗ): имя, email, телефон, ИНН (у ИП — персональный). Хранятся только поля формы и
    момент согласия; IP-адрес в базу НЕ пишется — он нужен лишь ограничителю частоты в памяти.
    Видит только admin (`/admin`, вкладка «Заявки»); в языковую модель не передаётся никогда."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    tariff: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(Text)
    org: Mapped[str] = mapped_column(Text)
    inn: Mapped[str] = mapped_column(String(12))
    email: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(32))
    promo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_at: Mapped[datetime] = mapped_column(DateTime)  # согласие обязательно — без него заявки нет
    # Работа с заявкой в админке (09.10.2026): статус, заметка администратора и учётка, созданная
    # из заявки. Статусы — `LEAD_STATUSES` в app/api/admin.py.
    status: Mapped[str] = mapped_column(String(16), default="new", nullable=False, server_default="new")
    # Опции с витрины тарифов (09.10.2026): вид, период, число пользователей, проба за 1 ₽, опция-галочка —
    # строкой `pricing.parse_options`; пусто — тариф без опций.
    options: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Комментарий заявителя (09.10.2026): попап «Связаться с нами» корпоративных тарифов и форма лендинга.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Вид заявки (ревью PR #186): «corporate» — с корпоративной карточки, «Для организаций» или продление
    # корпоративного тарифа; учётка из неё получает корпоративный тариф. Данными, а не поиском по `options`.
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # логическая ссылка на users.id
