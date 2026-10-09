"""CRUD-операции над данными приложения — тонкая обёртка над сессией SQLAlchemy.

Списки/JSON сериализуются здесь (sources/unverified кладём в *_json), чтобы вызывающий код
(эндпоинты) не знал про формат хранения.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import AnswerUsage, Feedback, GuestMessage, Lead, Message, User, _utcnow


# --- users ---------------------------------------------------------------
def get_user_by_username(db: Session, username: str) -> User | None:
    return db.execute(select(User).where(User.username == username)).scalar_one_or_none()


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def create_user(db: Session, username: str, password_hash: str, role: str = "expert") -> User:
    u = User(username=username, password_hash=password_hash, role=role)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def update_profile(
    db: Session, user_id: int, *, full_name: str, region: str, telegram: str, consent: bool,
    position: str | None = None, email: str | None = None, phone: str | None = None,
) -> User | None:
    """Сохраняет профиль пользователя (ФИО/регион/Telegram) и согласие на ПДн. Момент согласия
    (consent_at) фиксируем ОДИН раз при первой отметке — след 152-ФЗ. Возвращает User или None.

    Контакты кабинета (должность, email, телефон) необязательны: None — поле не трогаем, пустая
    строка — очищаем. Организацию и ИНН эта функция не пишет вовсе — их ведёт admin
    (`set_user_org`), и форма кабинета не должна иметь пути их переписать."""
    u = db.get(User, user_id)
    if not u:
        return None
    u.full_name = (full_name or "").strip()
    u.region = (region or "").strip()
    u.telegram = (telegram or "").strip()
    for field, value in (("position", position), ("email", email), ("phone", phone)):
        if value is not None:
            setattr(u, field, value.strip() or None)
    if consent and not u.consent:
        u.consent = True
        u.consent_at = _utcnow()
    db.commit()
    db.refresh(u)
    return u


def set_user_org(db: Session, user_id: int, org: str, inn: str) -> User | None:
    """Организация и ИНН пользователя — пишет только admin. Пустая строка — очистить."""
    u = db.get(User, user_id)
    if not u:
        return None
    u.org = (org or "").strip() or None
    u.inn = (inn or "").strip() or None
    db.commit()
    db.refresh(u)
    return u


def list_users(db: Session) -> list[User]:
    return list(db.execute(select(User).order_by(User.id)).scalars())


# --- тарифы и расход (#160) -----------------------------------------------
def set_user_plan(db: Session, user_id: int, plan: str | None, started_at=None) -> User | None:
    """Назначить тариф (None — снять лимит) и дату подключения. Возвращает User или None."""
    u = db.get(User, user_id)
    if not u:
        return None
    u.plan = plan
    u.plan_started_at = started_at if plan else None
    db.commit()
    db.refresh(u)
    return u


def record_answer_usage(db: Session, user_id: int) -> None:
    db.add(AnswerUsage(user_id=user_id))
    db.commit()


def is_answered_repeat(db: Session, user_id: int, session_id: str, content: str, since) -> bool:
    """Тот же вопрос в той же беседе после `since` уже получил ответ (#160, ревью PR #161).

    Так выглядит переход фронта со стрима на фолбэк, когда стрим дошёл до конца на сервере, а
    клиент финала не получил: ответ уже списан, повтор списываться и блокироваться не должен.
    Признак берётся из беседы на сервере, а не из ключа от клиента: ключ можно подсунуть, а
    бесплатным здесь выходит только ответ, который уже получен."""
    first = db.execute(
        select(func.min(Message.id)).where(
            Message.user_id == user_id, Message.session_id == session_id, Message.role == "user",
            Message.content == content, Message.ts >= since)
    ).scalar()
    if first is None:
        return False
    return db.execute(
        select(Message.id).where(Message.user_id == user_id, Message.session_id == session_id,
                                 Message.role == "assistant", Message.id > first).limit(1)
    ).first() is not None


def count_answer_usage(db: Session, user_id: int, start, end) -> int:
    """Списанные ответы за [start, end) — время в наивном UTC, как хранит SQLite."""
    return db.execute(
        select(func.count(AnswerUsage.id)).where(
            AnswerUsage.user_id == user_id, AnswerUsage.ts >= start, AnswerUsage.ts < end)
    ).scalar_one()


# --- messages (лог диалога) ----------------------------------------------
def log_message(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    role: str,
    content: str,
    sources: list | None = None,
    low_relevance: bool = False,
    unverified: list | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> Message:
    m = Message(
        user_id=user_id,
        session_id=session_id,
        role=role,
        content=content,
        sources_json=json.dumps(sources, ensure_ascii=False) if sources is not None else None,
        low_relevance=low_relevance,
        unverified_json=json.dumps(unverified, ensure_ascii=False) if unverified else None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def get_message(db: Session, message_id: int) -> Message | None:
    """Реплика по id — для проверки владельца перед сохранением оценки (см. web.submit_feedback).
    Без этой проверки любой залогиненный мог оценить ЧУЖОЙ ответ, и оценка попадала в приёмку."""
    return db.get(Message, message_id)


def get_messages_for_user(db: Session, user_id: int) -> list[Message]:
    return list(
        db.execute(
            select(Message).where(Message.user_id == user_id).order_by(Message.ts)
        ).scalars()
    )


def get_all_messages(db: Session) -> list[Message]:
    return list(db.execute(select(Message).order_by(Message.ts)).scalars())


def get_user_sessions(db: Session, user_id: int) -> list[dict]:
    """Беседы пользователя для сайдбара: [{session_id, title, ts}], новые сверху.
    Заголовок = первое сообщение эксперта в беседе.

    R24: раньше функция поднимала В ПАМЯТЬ ВСЕ реплики пользователя — включая полные тексты
    ОТВЕТОВ, которые тут не нужны вовсе и составляют основной объём. И вызывается она на каждом
    открытии чата. Теперь: время последней активности — агрегатом в SQL (тексты не передаются),
    заголовки — только из реплик `user`."""
    from sqlalchemy import func

    last_ts = {
        sid: ts
        for sid, ts in db.execute(
            select(Message.session_id, func.max(Message.ts))
            .where(Message.user_id == user_id)
            .group_by(Message.session_id)
        )
    }
    # Идём от НОВЫХ к старым и перезаписываем — в итоге останется первый вопрос беседы
    # (та же семантика, что у прежнего прохода по возрастанию с «первым непустым»).
    titles: dict[str, str] = {}
    for sid, content in db.execute(
        select(Message.session_id, Message.content)
        .where(Message.user_id == user_id, Message.role == "user")
        .order_by(Message.ts.desc(), Message.id.desc())
    ):
        titles[sid] = content

    sessions = [
        {"session_id": sid, "title": titles.get(sid), "ts": ts}
        for sid, ts in last_ts.items()
    ]
    return sorted(sessions, key=lambda s: (s["ts"] is not None, s["ts"]), reverse=True)


def get_session_messages(db: Session, user_id: int, session_id: str) -> list[Message]:
    """Реплики конкретной беседы пользователя (для переоткрытия), по возрастанию ts."""
    return list(
        db.execute(
            select(Message)
            .where(Message.user_id == user_id, Message.session_id == session_id)
            .order_by(Message.ts)
        ).scalars()
    )


def delete_session(db: Session, user_id: int, session_id: str) -> tuple[int, int]:
    """Удаляет беседу пользователя: её реплики И привязанную к ним обратную связь.

    Оценки удаляем ВМЕСТЕ с репликами (R2). Раньше уходили только `Message`, а строки `Feedback`
    оставались висеть на несуществующем `message_id` — и продолжали учитываться в приёмочной
    метрике (гейт 1.0), при том что вопрос и ответ в скоркарте были пустые. То есть пользователь,
    удаляя свой чат, тихо искажал главную метрику проекта.

    Фильтр по `user_id` везде — чужое не тронуть. Всё в одной транзакции.
    Возвращает (удалено реплик, удалено записей обратной связи)."""
    from sqlalchemy import delete as _delete, or_

    msg_ids = list(
        db.execute(
            select(Message.id).where(
                Message.user_id == user_id, Message.session_id == session_id
            )
        ).scalars()
    )
    # Оценка привязана к беседе (session_id) ИЛИ к конкретной реплике (message_id) — чистим оба следа.
    fb_where = [Feedback.session_id == session_id]
    if msg_ids:
        fb_where.append(Feedback.message_id.in_(msg_ids))
    fb_res = db.execute(
        _delete(Feedback).where(Feedback.user_id == user_id, or_(*fb_where))
    )
    res = db.execute(
        _delete(Message).where(Message.user_id == user_id, Message.session_id == session_id)
    )
    db.commit()
    return res.rowcount or 0, fb_res.rowcount or 0


# --- feedback ------------------------------------------------------------
def save_feedback(
    db: Session, *, user_id: int, kind: str = "service", rating: int | None = None,
    matched: str | None = None, comment: str | None = None, correction: str | None = None,
    session_id: str | None = None, message_id: int | None = None,
) -> Feedback:
    f = Feedback(
        user_id=user_id, kind=kind, rating=rating, matched=matched, comment=comment,
        correction=correction, session_id=session_id, message_id=message_id,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def get_feedback_for_user(db: Session, user_id: int) -> list[Feedback]:
    return list(
        db.execute(
            select(Feedback).where(Feedback.user_id == user_id).order_by(Feedback.ts)
        ).scalars()
    )


def get_all_feedback(db: Session) -> list[Feedback]:
    return list(db.execute(select(Feedback).order_by(Feedback.ts)).scalars())


# --- заявки с лендинга (ПДн: видит только admin) -------------------------------
def create_lead(
    db: Session, *, tariff: str, name: str, org: str, inn: str, email: str, phone: str,
    promo: str | None = None,
) -> Lead:
    """Сохранить заявку. Момент согласия ставится здесь: эндпоинт зовёт функцию только после того,
    как согласие проверено, — отдельного поля «согласен» в строке нет, есть время согласия."""
    lead = Lead(tariff=tariff, name=name, org=org, inn=inn, email=email, phone=phone,
                promo=promo or None, consent_at=_utcnow())
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def list_leads(db: Session, limit: int | None = None) -> list[Lead]:
    """Заявки, свежие сверху; `limit` — сколько показать (админка не тянет все ПДн разом)."""
    stmt = select(Lead).order_by(Lead.created_at.desc(), Lead.id.desc())
    if limit:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars())


def count_leads(db: Session) -> int:
    return db.execute(select(func.count(Lead.id))).scalar_one()


# Срок хранения заявки — обещание политики ПДн (раздел 9: «не дольше 12 месяцев»). Исполняется
# кодом, а не памятью оператора (ревью PR #158): вызывается при каждой новой заявке и при
# открытии админки. Резервные копии ротируются за 14 дней — удалённое уходит и из них.
LEAD_RETENTION_DAYS = 365


def purge_old_leads(db: Session, days: int = LEAD_RETENTION_DAYS, now=None) -> int:
    """Удалить заявки старше срока хранения. Возвращает число удалённых."""
    edge = (now or _utcnow()) - timedelta(days=days)
    # SQLite хранит наивное время — сравниваем в той же форме, что пишет `_utcnow` через ORM.
    res = db.execute(delete(Lead).where(Lead.created_at < edge.replace(tzinfo=None)))
    db.commit()
    return res.rowcount or 0


def delete_lead(db: Session, lead_id: int) -> bool:
    """Удалить одну заявку — по отзыву согласия или требованию субъекта ПДн (ст. 21 152-ФЗ)."""
    res = db.execute(delete(Lead).where(Lead.id == lead_id))
    db.commit()
    return bool(res.rowcount)


# --- пробный режим без входа (обезличенно: ни учётки, ни IP) ---------------------
def log_guest_message(
    db: Session, *, guest_id: str, session_id: str, role: str, content: str,
    sources: list | None = None, low_relevance: bool = False, prompt_tokens: int | None = None,
    completion_tokens: int | None = None, charged: bool = False,
) -> GuestMessage:
    m = GuestMessage(
        guest_id=guest_id, session_id=session_id, role=role, content=content,
        sources_json=json.dumps(sources, ensure_ascii=False) if sources is not None else None,
        low_relevance=low_relevance, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens, charged=charged,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def count_guest_answers(db: Session, guest_id: str) -> int:
    """Сколько пробных вопросов гость уже потратил — засчитанные ответы за всё время."""
    return db.execute(select(func.count(GuestMessage.id)).where(
        GuestMessage.guest_id == guest_id, GuestMessage.charged.is_(True))).scalar_one()


def count_guest_answers_since(db: Session, since) -> int:
    """Засчитанные ответы ВСЕМ гостям с момента `since` (наивное UTC) — общий суточный потолок."""
    return db.execute(select(func.count(GuestMessage.id)).where(
        GuestMessage.charged.is_(True), GuestMessage.ts >= since)).scalar_one()


def get_guest_session_messages(db: Session, guest_id: str, session_id: str) -> list[GuestMessage]:
    """Реплики беседы гостя. Ключ — пара (гость, беседа): чужой session_id даёт пустую историю."""
    return list(db.execute(
        select(GuestMessage).where(GuestMessage.guest_id == guest_id,
                                   GuestMessage.session_id == session_id)
        .order_by(GuestMessage.id)).scalars())


def is_guest_answered_repeat(db: Session, guest_id: str, session_id: str, content: str,
                             since) -> bool:
    """Как `is_answered_repeat`: тот же вопрос в той же беседе уже получил ответ — это фолбэк
    фронта после стрима, дошедшего до конца на сервере. Признак — из базы, не от клиента."""
    first = db.execute(
        select(func.min(GuestMessage.id)).where(
            GuestMessage.guest_id == guest_id, GuestMessage.session_id == session_id,
            GuestMessage.role == "user", GuestMessage.content == content, GuestMessage.ts >= since)
    ).scalar()
    if first is None:
        return False
    return db.execute(
        select(GuestMessage.id).where(GuestMessage.guest_id == guest_id,
                                      GuestMessage.session_id == session_id,
                                      GuestMessage.role == "assistant", GuestMessage.id > first)
        .limit(1)).first() is not None


def list_guest_messages(db: Session, limit: int = 200) -> list[GuestMessage]:
    """Последние реплики гостей, свежие сверху — для вкладки «Пробный режим» в админке."""
    return list(db.execute(
        select(GuestMessage).order_by(GuestMessage.id.desc()).limit(limit)).scalars())


# Срок хранения реплик пробного режима — обещание политики («не дольше 6 месяцев»). Исполняется
# кодом при открытии админки, как у заявок.
GUEST_RETENTION_DAYS = 183


def purge_old_guest_messages(db: Session, days: int = GUEST_RETENTION_DAYS, now=None) -> int:
    edge = (now or _utcnow()) - timedelta(days=days)
    res = db.execute(delete(GuestMessage).where(GuestMessage.ts < edge.replace(tzinfo=None)))
    db.commit()
    return res.rowcount or 0
