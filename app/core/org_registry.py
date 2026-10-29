"""Организация по ИНН из ЕГРЮЛ — через DaData (решение владельца 29.10.2026).

Кабинет подставляет название организации по введённому ИНН, карточка пользователя в `/admin` показывает
запись реестра рядом с тем, что вписал человек: подтверждать организацию становится сверкой одного
экрана, а не ручным походом в ЕГРЮЛ («чтобы не было фейков», 09.10.2026).

ТОЛЬКО ИНН ОРГАНИЗАЦИИ (10 цифр). ИНН ИП и физлица (12 цифр) — персональные данные: отправить его во
внешний сервис значит передать ПДн третьему лицу (152-ФЗ). Решение владельца — такие номера не
отправлять вовсе; проверка стоит здесь, в единственной точке выхода наружу, а не в вызывающих.

ТРИ ИСХОДА, А НЕ ДВА. «Найдена», «не найдена» и «не проверено» (нет ключа, сеть, таймаут, исчерпан
лимит, ответ не разобран). Последнее о самой организации не говорит НИЧЕГО: склеить его с «не найдена»
значило бы сказать человеку «такой организации нет», когда упал канал. Поиск — подсказка: сохранение
профиля и подтверждение admin'ом от него не зависят.

КЭШ В ПАМЯТИ ПРОЦЕССА — найденное и ненайденное на сутки: у DaData суточный лимит запросов (по тарифу), а
карточку и кабинет открывают повторно. «Не проверено» не кэшируется: следующий запрос
спросит снова. Сервис однопроцессный, как и у лимитов частоты (`app/api/ratelimit.py`).

ЧТО БЕРЁМ ИЗ ОТВЕТА — только реквизиты юрлица: название, ИНН, КПП, ОГРН, статус, адрес. ФИО
руководителя в ответе есть, но это ПДн, и сервису оно не нужно — не читается и не хранится.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from loguru import logger

from app.core.config import settings
from app.core.inn import inn_valid

PARTY_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
TIMEOUT_S = 3.0              # поиск — подсказка у поля ввода: дольше ждать незачем
CACHE_TTL_S = 24 * 3600.0
_CACHE_MAX = 2048

FOUND, NOT_FOUND, UNAVAILABLE = "found", "not_found", "unavailable"

# Статусы ЕГРЮЛ в DaData (`state.status`). Неизвестный — без подписи, но и не «действующая».
STATUS_TEXT = {
    "ACTIVE": "действующая",
    "LIQUIDATING": "ликвидируется",
    "LIQUIDATED": "ликвидирована",
    "BANKRUPT": "признана банкротом",
    "REORGANIZING": "в процессе реорганизации",
}

# Тесты подменяют транспорт (`httpx.MockTransport`) — без сети и без ключа.
_transport: httpx.BaseTransport | None = None


@dataclass(frozen=True)
class Org:
    inn: str
    name: str           # краткое с ОПФ: ООО "РОМАШКА"
    full_name: str      # полное с ОПФ
    kpp: str
    ogrn: str
    status: str         # код DaData: ACTIVE, LIQUIDATED, …
    liquidated_on: str  # «12.03.2021» или «»
    address: str
    names: tuple[str, ...] = ()  # все формы названия — для сверки с вписанным

    @property
    def active(self) -> bool:
        return self.status == "ACTIVE"

    @property
    def status_text(self) -> str:
        text = STATUS_TEXT.get(self.status, "")
        return f"{text} {self.liquidated_on}" if text and self.liquidated_on else text


@dataclass(frozen=True)
class Lookup:
    outcome: str        # FOUND | NOT_FOUND | UNAVAILABLE
    org: Org | None = None


def enabled() -> bool:
    return bool(settings.DADATA_API_KEY)


def can_lookup(inn: str) -> bool:
    """Что можно отправить наружу: ИНН организации (10 цифр) с верными контрольными цифрами. ИНН ИП и
    физлица — нет (ПДн), опечатку — тоже: лимит DaData на неё тратить незачем."""
    return len(inn) == 10 and inn_valid(inn)


_cache: dict[str, tuple[float, Lookup]] = {}
_lock = threading.Lock()


def _cached(inn: str, now: float) -> Lookup | None:
    with _lock:
        hit = _cache.get(inn)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
        _cache.pop(inn, None)
        return None


def _remember(inn: str, res: Lookup, now: float) -> None:
    with _lock:
        if len(_cache) >= _CACHE_MAX:
            for k in [k for k, (ts, _) in _cache.items() if now - ts >= CACHE_TTL_S]:
                del _cache[k]
            while len(_cache) >= _CACHE_MAX:          # всё свежее — уходит самое старое
                del _cache[next(iter(_cache))]
        _cache[inn] = (now, res)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _date(ms) -> str:
    """`state.liquidation_date` — миллисекунды эпохи (UTC)."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d.%m.%Y")


def _org(inn: str, data: dict) -> Org:
    name = data.get("name") or {}
    state = data.get("state") or {}
    short = name.get("short_with_opf") or name.get("full_with_opf") or ""
    full = name.get("full_with_opf") or short
    forms = (name.get("short_with_opf"), name.get("full_with_opf"), name.get("short"), name.get("full"))
    status = state.get("status") or ""
    return Org(inn=inn, name=short, full_name=full, kpp=data.get("kpp") or "", ogrn=data.get("ogrn") or "",
               status=status, liquidated_on=_date(state.get("liquidation_date")) if status == "LIQUIDATED" else "",
               address=(data.get("address") or {}).get("value") or "",
               names=tuple(dict.fromkeys(f for f in forms if f)))


def _ask(inn: str) -> Lookup:
    try:
        with httpx.Client(timeout=TIMEOUT_S, transport=_transport) as client:
            r = client.post(PARTY_URL, json={"query": inn, "type": "LEGAL", "branch_type": "MAIN", "count": 1},
                            headers={"Authorization": f"Token {settings.DADATA_API_KEY}",
                                     "Accept": "application/json"})
    except httpx.HTTPError as e:
        logger.warning(f"org_registry: DaData не ответила ({type(e).__name__})")
        return Lookup(UNAVAILABLE)
    if r.status_code != 200:
        # 401 — неверный ключ, 403 — исчерпан суточный лимит, 429 — частота: о самой организации ни слова
        logger.warning(f"org_registry: DaData ответила {r.status_code}")
        return Lookup(UNAVAILABLE)
    try:
        suggestions = r.json()["suggestions"]
        if not isinstance(suggestions, list):
            raise TypeError("suggestions")
        for s in suggestions:
            data = s.get("data") or {}
            # Ответ — по запрошенному ИНН и с названием; чужая запись или пустая не считается находкой
            if data.get("inn") == inn:
                org = _org(inn, data)
                if org.name:
                    return Lookup(FOUND, org)
        if suggestions:
            raise ValueError("нет записи с запрошенным ИНН")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        logger.warning(f"org_registry: ответ DaData не разобран ({type(e).__name__})")
        return Lookup(UNAVAILABLE)
    return Lookup(NOT_FOUND)


def lookup(inn: str, now: float | None = None) -> Lookup | None:
    """Запись ЕГРЮЛ по ИНН организации. None — не спрашивали: поиск выключен (нет ключа) или номер
    нельзя отправлять (`can_lookup`)."""
    inn = (inn or "").strip()
    if not enabled() or not can_lookup(inn):
        return None
    now = time.monotonic() if now is None else now
    hit = _cached(inn, now)
    if hit is not None:
        return hit
    res = _ask(inn)
    if res.outcome != UNAVAILABLE:
        _remember(inn, res, now)
    return res


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    for ch in "«»\"'„“”‘’`":
        s = s.replace(ch, " ")
    return " ".join(s.split())


def name_matches(org: Org, name: str) -> bool:
    """Вписанное название совпадает с одной из форм реестра — без регистра, кавычек и лишних пробелов:
    «ООО «Ромашка»» = ООО "РОМАШКА", «Ромашка» = РОМАШКА (форма без ОПФ). Сокращения своими словами
    («ООО Ромашка-Курск») — уже не совпадение: сверяет admin."""
    n = _norm(name or "")
    return bool(n) and any(_norm(f) == n for f in org.names)
