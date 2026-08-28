"""Уровень 4 протокола замера: латентность, нагрузка, стоимость (`EV22` #130).

ЗАЧЕМ. Уровень 4 `EVAL_GUIDE` описан с первого дня и НЕ ИСПОЛНЯЛСЯ НИ РАЗУ за пять заходов —
в базе сравнения на его месте стоит строка «частично: стоимость считается по прогону; хаос
покрыт юнит-тестами уровня 0». Волна — это нагрузочное событие (17 региональных ТПП разом), а
p50, доля таймаутов и предел параллельности неизвестны. Ляжет сервис — это обнаружат участники
волны, а не мы, и приёмка `#13` испортится по причине, не имеющей отношения к качеству ответов.

⚠⚠ ГДЕ ЭТО ЗАПУСКАТЬ: ТОЛЬКО НА БОЕВОЙ МАШИНЕ, ПРОТИВ 127.0.0.1. Замер снаружи меряет канал, а
не сервис. 28.08.2026 проверено: один и тот же `/ping` в пределах секунд дал 0.164 с и таймаут
25 с (DPI на стороне разработчика, SSH в те же минуты не проходил 3 раза из 3). Тот же урок уже
записан в `CLAUDE.md`: «снаружи /ping уходил в таймаут, изнутри VM отвечал за 1.4 мс». Поэтому
скрипт держится СТАНДАРТНОЙ БИБЛИОТЕКИ — он должен запускаться и в контейнере приложения
(`docker compose exec app python scripts/eval_level4.py`), и на голом хосте, где venv проекта нет.

⚠⚠ ПОЧЕМУ «ДОЛЯ 5xx» — НЕДОСТАТОЧНЫЙ КРИТЕРИЙ. ЭТО ГЛАВНОЕ В ЭТОМ ФАЙЛЕ.
Постановка `#130` требует мерить «долю 5xx и таймаутов ≤1 %». На ЭТОМ сервисе так мерить нельзя:
запрос не доходит до движка ЧЕТЫРЬМЯ способами, и ни один из них не 5xx.

* **429** — `RATE_LIMIT_CHAT_PER_MIN = 20` НА ПОЛЬЗОВАТЕЛЯ (`app/api/chat.py`), причём проверка
  стоит ДО обращения к БД и движку. При 25 и 50 параллельных с одного аккаунта всё, начиная с
  21-го запроса, разворачивается на пороге.
* **403** — `require_user_profiled`: роль `user` без заполненного профиля в чат не пускается.
  Свежесозданный аккаунт под нагрузку — ровно такой.
* **401** — сессия не установилась (кука не доехала, `SESSION_SECRET` сменился рестартом).
* **200 + `{"type":"error"}`** — движок упал ВНУТРИ стрима. HTTP-код 200, потому что заголовки
  уже ушли; инструмент, глядящий на код ответа, засчитает это успехом.

Инструмент, считающий только 5xx, отчитался бы «0 % отказов, всё чисто», НЕ ИЗМЕРИВ НИЧЕГО.
Это ровно тот класс, который в проекте уже стоил месяца: «ноль» означал не «чисто», а «не считаю».
Поэтому здесь каждый исход классифицируется ПОИМЁННО, а классы 401/403/429 объявляют прогон
НЕДЕЙСТВИТЕЛЬНЫМ: перцентили при них не печатаются вовсе, чтобы их нельзя было процитировать.

ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ — `--mode selftest`, и он ОБЯЗАТЕЛЕН ПЕРЕД ЗАМЕРОМ. У инструмента, чьё
«ноль отказов» является результатом, обязан быть контроль: «ноль» и «слеп» снаружи неразличимы
(правило заведено `EV15`). Селф-тест поднимает локальный сервер, который умеет отвечать медленно,
падать 500, отдавать 429, рвать поток и молчать до таймаута, и требует, чтобы инструмент увидел
КАЖДЫЙ случай. Не увидел — скрипт падает и чисел не печатает.

⚠⚠ ЧЕГО ЭТОТ ИНСТРУМЕНТ НЕ МЕРИТ — ВАЖНО ДЛЯ ХАОС-ПУНКТА. Он ходит ТОЛЬКО в `/api/chat/stream`.
Когда движок падает внутри стрима, сервер шлёт `{"type":"error"}`, и пользователь видит НЕ это:
фронт делает фолбэк на НЕстримовый `/api/chat` (см. комментарий R26 в `app/api/chat.py`).
Значит на вопрос гайда «Qdrant недоступен → внятная ошибка, не пустой ответ» этот инструмент
отвечает лишь наполовину: он показывает, что стрим честно сигналит об ошибке, но не показывает,
что увидит человек. Вторую половину проверять запросом в `/api/chat` — руками или отдельным
режимом. Проверено сухим прогоном 28.08.2026 против живого приложения с выключенным Qdrant.

⚠ ЧТО ЭТОТ ЗАМЕР ДЕЛАЕТ С БОЕВОЙ БАЗОЙ. Каждый успешный запрос пишет пару реплик (вопрос+ответ)
в `messages`. Прогон из ~115 запросов оставит ~115 искусственных диалогов, видимых в админке и в
скоркарте волны. Поэтому: аккаунты ВЫДЕЛЕННЫЕ и с узнаваемым префиксом, бэкап до прогона, уборка
после. Скрипт пишет `message_id` каждого ответа в JSON — по ним и убирать, и считать стоимость.

ЧТО СЧИТАЕМ.
    single — N последовательных запросов: p50/p95 до первого токена и до полного ответа.
    load   — N запросов при C одновременных: те же величины + доля отказов ПО КЛАССАМ.
    ⚠ p95 на 30 замерах — это 29-е значение по порядку. Число честное, но грубое: его разброс
    больше, чем разница между «хорошо» и «терпимо». Поэтому n печатается рядом с каждым
    перцентилем, а порогов до первого прогона нет — он и есть baseline (так сказано в гайде).

ЗАПУСК:
    # 1. положительный контроль — сначала, иначе числа ничего не значат
    python scripts/eval_level4.py --mode selftest

    # 2. на бою, изнутри контейнера приложения
    docker compose exec app python scripts/eval_level4.py --mode single --n 30 \
        --accounts 'lt.expert1:PASS,lt.expert2:PASS' --json /data/level4_single.json
    docker compose exec app python scripts/eval_level4.py --mode load --n 50 --concurrency 10 \
        --accounts '...' --json /data/level4_load10.json
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import random
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # скрипт обязан работать и там, где пакета `app` рядом нет (голый хост VM)
    from app.core.console import enable_utf8
    enable_utf8()  # #107: печатаем значки вне cp1251
except Exception:  # noqa: BLE001
    pass

# ── Классы исхода ────────────────────────────────────────────────────────────────────────────
# ⚠ Классы перечислены ПОИМЁННО намеренно. Сводка «доля 5xx» скрыла бы три из них.
OK = "ok"                   # дошёл до движка, получен `done`
SSE_ERROR = "sse_error"     # 200, но движок упал внутри стрима → {"type":"error"}
TRUNCATED = "truncated"     # поток кончился без `done` и без `error` (обрыв)
TIMEOUT = "timeout"
CONN_ERROR = "conn_error"
HTTP_401, HTTP_403, HTTP_429, HTTP_422 = "http_401", "http_403", "http_429", "http_422"
HTTP_4XX, HTTP_5XX = "http_4xx", "http_5xx"
# ⚠⚠ ДЕФЕКТ САМОГО ИНСТРУМЕНТА — ОТДЕЛЬНЫЙ КЛАСС, А НЕ ОТКАЗ СЕРВИСА (ревью PR #135, раунд 2).
# Раньше любой побег из `ask` (опечатка после рефакторинга, кривой `--base-url`) записывался
# как `conn_error`, попадал в долю отказов и гнал вердикт: сломанный ИНСТРУМЕНТ выглядел как
# «сервис уронил N % соединений» — неотличимо от настоящего сбоя. У проекта уже есть верная
# корзина для «мы не измеряли сервис» — `INVALIDATING`, туда этот класс и идёт.
TOOL_ERROR = "tool_error"

# ⚠⚠ ИСХОДЫ, ПРИ КОТОРЫХ ЗАМЕР НЕДЕЙСТВИТЕЛЕН: запрос развернулся НА ПОРОГЕ, не дойдя до движка.
# Их наличие означает не «сервис справился», а «мы не измеряли сервис». Перцентили при них не
# печатаются — иначе число уйдёт в отчёт и станет цитатой.
INVALIDATING = frozenset({HTTP_401, HTTP_403, HTTP_429, TOOL_ERROR})
# Отказы, которые считаются в «долю отказов» по гайду (сервис ответил, но плохо).
FAILURES = frozenset({SSE_ERROR, TRUNCATED, TIMEOUT, CONN_ERROR, HTTP_5XX, HTTP_4XX, HTTP_422})

# ⚠ Копия `app/core/config.Settings.RATE_LIMIT_CHAT_PER_MIN`. Совпадение закреплено тестом
# `tests/test_eval_level4.py::TestRateLimitStaysInSync` — разойдись они, предполётный отказ
# считал бы ёмкость по устаревшему числу и пропускал прогон, обречённый упереться в 429.
# ⚠ Ссылка на тест — КОММЕНТАРИЕМ, не константой: мёртвое определение рядом с живым кодом уже
# было находкой ревью сегодня (`BREAKERS` в checks/common/70).
RATE_LIMIT_PER_MIN = 20


class LoginError(RuntimeError):
    pass


def _class_for_status(status: int) -> str:
    return {401: HTTP_401, 403: HTTP_403, 429: HTTP_429, 422: HTTP_422}.get(
        status, HTTP_5XX if status >= 500 else HTTP_4XX)


def _conn(base: str, timeout: float):
    u = urlsplit(base)
    host, port = u.hostname or "127.0.0.1", u.port
    if u.scheme == "https":
        c = http.client.HTTPSConnection(host, port or 443, timeout=timeout)
    else:
        c = http.client.HTTPConnection(host, port or 80, timeout=timeout)
    return c, (u.path or "").rstrip("/")


def pct(values: list[float], q: float) -> float | None:
    """Перцентиль методом ближайшего ранга. ⚠ Метод назван намеренно: на 30 замерах разные
    методы дают разные числа, и без указания метода число невоспроизводимо."""
    if not values:
        return None
    s = sorted(values)
    return round(s[max(0, math.ceil(q * len(s)) - 1)], 3)


# ── Горячий путь ─────────────────────────────────────────────────────────────────────────────
def login(base: str, username: str, password: str, timeout: float = 30.0) -> str:
    """Логин формой, как это делает браузер. Возвращает готовую строку Cookie.

    ⚠ Успешный вход — РЕДИРЕКТ 302 на «/». 200 означает возврат страницы логина с ошибкой, то
    есть неудачу: проверять надо код, а не отсутствие исключения."""
    c, prefix = _conn(base, timeout)
    body = urlencode({"username": username, "password": password}).encode("utf-8")
    try:
        c.request("POST", prefix + "/login", body=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
        })
        r = c.getresponse()
        r.read()
        raw = r.msg.get_all("Set-Cookie") or []
        status = r.status
    finally:
        c.close()
    if status == 429:
        raise LoginError(
            f"{username}: вход дал 429 — RATE_LIMIT_LOGIN_PER_MIN=10 НА IP. "
            "Логинить аккаунты пачками не быстрее десяти в минуту.")
    if status != 302:
        raise LoginError(f"{username}: вход дал {status}, ожидался 302 (успех — редирект на «/»)")
    jar = "; ".join(sc.split(";", 1)[0] for sc in raw)
    if "session=" not in jar:
        raise LoginError(f"{username}: сессионной куки в ответе нет — {jar[:120]!r}")
    return jar


def ask(base: str, cookie: str, question: str, timeout: float) -> dict:
    """Один вопрос ГОРЯЧИМ ПУТЁМ — тем же, которым идёт браузер: POST /api/chat/stream, SSE.

    ⚠ Мерить вызовом `pipeline.answer()` было бы дешевле и НЕПРАВИЛЬНО: мимо аутентификации,
    лимита, сериализации и стриминга, то есть мимо всего, что даёт пользователю секунды. В
    проекте это уже случалось — `eval_rules` дёргал `search_rules` минуя гейт, и «атрибуция 0.96»
    оказалась утверждением о поиске В корпусе, а не о попадании В него."""
    out = {"class": None, "status": None, "ttft": None, "total": None,
           "message_id": None, "chars": 0}
    t0 = time.monotonic()
    c = None
    try:
        c, prefix = _conn(base, timeout)
        body = json.dumps({"message": question, "session_id": None}).encode("utf-8")
        c.request("POST", prefix + "/api/chat/stream", body=body, headers={
            "Content-Type": "application/json", "Content-Length": str(len(body)),
            "Accept": "text/event-stream", "Cookie": cookie,
        })
        r = c.getresponse()
        out["status"] = r.status
        if r.status != 200:
            r.read()
            out["class"] = _class_for_status(r.status)
            out["total"] = round(time.monotonic() - t0, 3)
            return out
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "delta":
                if out["ttft"] is None:
                    out["ttft"] = round(time.monotonic() - t0, 3)
                out["chars"] += len(ev.get("text") or "")
            elif kind == "done":
                out["total"] = round(time.monotonic() - t0, 3)
                out["message_id"] = ev.get("message_id")
                out["class"] = OK
                return out
            elif kind == "error":
                out["total"] = round(time.monotonic() - t0, 3)
                out["class"] = SSE_ERROR
                return out
        # поток кончился, а `done` не пришёл — это НЕ успех, сколько бы дельт ни получили
        out["total"] = round(time.monotonic() - t0, 3)
        out["class"] = TRUNCATED
        return out
    except TimeoutError:
        out["class"], out["total"] = TIMEOUT, round(time.monotonic() - t0, 3)
        return out
    except OSError as e:  # socket.timeout наследует OSError; сюда же обрывы
        out["class"] = TIMEOUT if "timed out" in str(e).lower() else CONN_ERROR
        out["total"] = round(time.monotonic() - t0, 3)
        out["detail"] = str(e)[:120]
        return out
    except http.client.HTTPException as e:
        # ⚠ ПОСЛЕ `OSError` НАМЕРЕННО: `RemoteDisconnected` наследует ОБА класса, и он обрабатывался
        # как `conn_error` до этой правки. Правка нацелена на `IncompleteRead`/`BadStatusLine` —
        # менять заодно классификацию уже работавшего случая она не должна.
        # ⚠⚠ `IncompleteRead` НЕ является `OSError` (проверено: OSError=False,
        # HTTPException=True) — а это ровно оборванный на середине поток, тот самый класс
        # `TRUNCATED`, ради которого инструмент и писался. Без этой ветки исключение уходило из
        # `ask()` наружу, поток замера рвался, и ОТКАЗ ИСЧЕЗАЛ ИЗ СТАТИСТИКИ вместо того чтобы
        # в неё попасть. Найдено ревью PR #135.
        out["class"] = TRUNCATED
        out["total"] = round(time.monotonic() - t0, 3)
        out["detail"] = f"{type(e).__name__}: {str(e)[:100]}"
        return out
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def ask_plain(base: str, cookie: str, question: str, timeout: float) -> dict:
    """НЕстримовый `/api/chat` — то, что пользователь видит на самом деле, когда стрим падает.

    ⚠⚠ ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ. `ask()` ходит только в `/api/chat/stream`, и когда движок падает,
    стрим отдаёт `{"type":"error"}`. Но человеку это не показывается: фронт делает фолбэк на
    `/api/chat` (комментарий R26 в `app/api/chat.py`), и ответ человек получает ОТТУДА. Значит на
    вопрос гайда «Qdrant недоступен → внятная ошибка, не пустой ответ» стрим отвечает лишь
    наполовину — вторую половину знает только этот эндпоинт. Предел был записан в шапке 28.08 и
    закрывается здесь: хаос-проверка обязана смотреть туда, куда смотрит пользователь.

    Возвращает те же классы, плюс `text` — сам ответ, чтобы проверить, что он ВНЯТНЫЙ, а не пустой.
    """
    # ⚠⚠ `sources` ВОЗВРАЩАЕТСЯ НАМЕРЕННО. Это единственный СТРУКТУРНЫЙ признак, отличающий
    # здоровый ответ от деградированного: при отвалившемся Qdrant сервис отвечает честным
    # фолбэком с кодом 200 и пустым списком источников. Ни код ответа, ни текст этого не
    # показывают — см. `eval_chaos.judge`, где на этом сгорела первая редакция проверки.
    out = {"class": None, "status": None, "total": None, "text": "", "unverified": [],
           "sources": 0}
    t0 = time.monotonic()
    c = None
    try:
        c, prefix = _conn(base, timeout)
        body = json.dumps({"message": question, "session_id": None}).encode("utf-8")
        c.request("POST", prefix + "/api/chat", body=body, headers={
            "Content-Type": "application/json", "Content-Length": str(len(body)),
            "Cookie": cookie,
        })
        r = c.getresponse()
        out["status"] = r.status
        raw = r.read()
        out["total"] = round(time.monotonic() - t0, 3)
        if r.status != 200:
            out["class"] = _class_for_status(r.status)
            out["text"] = raw.decode("utf-8", "replace")[:400]
            return out
        payload = json.loads(raw.decode("utf-8", "replace"))
        out["text"] = payload.get("answer") or ""
        out["unverified"] = payload.get("unverified_numbers") or []
        out["sources"] = len(payload.get("sources") or [])
        out["class"] = OK
        return out
    except TimeoutError:
        out["class"], out["total"] = TIMEOUT, round(time.monotonic() - t0, 3)
        return out
    except (OSError, ValueError, http.client.HTTPException) as e:
        # ⚠ `HTTPException` здесь по той же причине, что в `ask()`: оборванный ответ не должен
        # уходить исключением наружу и исчезать из статистики.
        out["class"] = (TIMEOUT if "timed out" in str(e).lower()
                        else CONN_ERROR if isinstance(e, (OSError, http.client.HTTPException))
                        else SSE_ERROR)
        out["total"] = round(time.monotonic() - t0, 3)
        out["detail"] = str(e)[:120]
        return out
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


# ── Вопросы ──────────────────────────────────────────────────────────────────────────────────
def load_questions(n: int, seed: int) -> list[str]:
    """Вопросы из приёмочных наборов: товарные + процедурные, вперемешку.

    ⚠ Смесь важна: ветки стоят РАЗНЫХ денег и разного времени (у процедурной другой корпус и
    другой промпт). Замер на одних товарных дал бы латентность одной ветки и назвал её общей."""
    pool: list[str] = []
    for name, key in (("eval_golden.json", "query"), ("eval_golden_rules.json", "query")):
        path = ROOT / "scripts" / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for case in data.get("cases", []):
            q = (case.get(key) or "").strip()
            if q:
                pool.append(q)
    if not pool:
        raise SystemExit("ОСТАНОВ: приёмочных наборов рядом нет — мерить не на чем")
    rnd = random.Random(seed)
    rnd.shuffle(pool)
    return [pool[i % len(pool)] for i in range(n)]


# ── Сводка ───────────────────────────────────────────────────────────────────────────────────
def summarize(results: list[dict], meta: dict) -> dict:
    by_class: dict[str, int] = {}
    for r in results:
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
    ok = [r for r in results if r["class"] == OK]
    # ⚠⚠ ДЛЯ ДЕФЕКТА ИНСТРУМЕНТА — ПОРОГ, А НЕ АБСОЛЮТНЫЙ ЗАПРЕТ (ревью PR #135, раунд 3).
    # Один побег исключения в одном из 50 потоков обнулял ВЕСЬ прогон, за который заплачено окном
    # на боевой машине, — а этот же файл называет «ничего не сняли» худшим исходом захода.
    # ⚠ Отличие от 429: тот приходит бурей и заражает соседей (окно лимита общее), а побег
    # из `ask` — событие ОДНОГО запроса и о соседях ничего не говорит. Поэтому редкий добивается
    # пометки, а массовый — отмены: порог 1 %, как у доли отказов в гайде.
    tool_share = by_class.get(TOOL_ERROR, 0) / len(results) if results else 0.0
    invalid = {k: v for k, v in by_class.items()
               if k in INVALIDATING and (k != TOOL_ERROR or tool_share > 0.01)}
    failures = sum(v for k, v in by_class.items() if k in FAILURES)
    out = dict(meta)
    out["n"] = len(results)
    out["by_class"] = by_class
    out["ok"] = len(ok)
    out["failure_share"] = round(failures / len(results), 4) if results else None
    out["valid"] = not invalid
    if invalid:
        # ⚠⚠ Чисел здесь НЕ БУДЕТ. Запрос, развернувшийся на пороге, до движка не дошёл, и
        # перцентили по остатку описывают выжившую подвыборку, а не сервис.
        out["⚠⚠ ЗАМЕР НЕДЕЙСТВИТЕЛЕН"] = (
            f"запросы развернулись на пороге, не дойдя до движка: {invalid}. "
            "Это НЕ «сервис справился». 429 → мало аккаунтов (лимит "
            f"{RATE_LIMIT_PER_MIN}/мин на пользователя); 403 → роль `user` без профиля; "
            "401 → сессия не установилась; tool_error → сломан САМ ИНСТРУМЕНТ, и его дефект нельзя выдавать за отказ сервиса. Перцентили не печатаются намеренно.")
        return out
    out["ttft_p50"] = pct([r["ttft"] for r in ok if r["ttft"] is not None], 0.50)
    out["ttft_p95"] = pct([r["ttft"] for r in ok if r["ttft"] is not None], 0.95)
    out["total_p50"] = pct([r["total"] for r in ok if r["total"] is not None], 0.50)
    out["total_p95"] = pct([r["total"] for r in ok if r["total"] is not None], 0.95)
    out["percentile_method"] = "ближайший ранг (nearest-rank), n указан рядом"
    out["message_ids"] = [r["message_id"] for r in ok if r.get("message_id")]
    return out


def _print_summary(s: dict) -> None:
    print(f"\n{'=' * 78}\n{s.get('mode', '?')}: {s['n']} запросов, "
          f"успешных {s['ok']}, классы {s['by_class']}")
    if not s["valid"]:
        print(f"⚠⚠ {s['⚠⚠ ЗАМЕР НЕДЕЙСТВИТЕЛЕН']}")
        return
    n_ok = s["ok"]
    if not n_ok:
        # ⚠⚠ Ни один запрос не дошёл до ответа. Печатать «p50 None» рядом со словом «замер»
        # нельзя: пустое место в таблице читается как «мерили и получили», а не «не получили.
        print("   ⚠⚠ УСПЕШНЫХ ОТВЕТОВ НЕТ — измерять нечего. Числа не печатаются.")
        print(f"   доля отказов: {s['failure_share']} (классы выше)")
        return
    print(f"   до первого токена: p50 {s['ttft_p50']} с · p95 {s['ttft_p95']} с   (n={n_ok})")
    print(f"   до полного ответа: p50 {s['total_p50']} с · p95 {s['total_p95']} с   (n={n_ok})")
    print(f"   доля отказов: {s['failure_share']}")
    if n_ok < 20:
        print(f"   ⚠ n={n_ok}: p95 — это {math.ceil(0.95 * n_ok)}-е значение по порядку. Грубо.")


# ── Режимы ───────────────────────────────────────────────────────────────────────────────────
def run_single(base: str, jars: list[str], questions: list[str], timeout: float) -> list[dict]:
    """Последовательно. ⚠ Между запросами держим паузу, чтобы не упереться в лимит: на паузе
    личное время запроса не меняется, а 429 сделал бы весь замер недействительным."""
    results, min_gap = [], 60.0 / RATE_LIMIT_PER_MIN * 1.1 / max(1, len(jars))
    last = 0.0
    for i, q in enumerate(questions):
        wait = min_gap - (time.monotonic() - last)
        if wait > 0 and i:
            time.sleep(wait)
        last = time.monotonic()
        r = ask(base, jars[i % len(jars)], q, timeout)
        results.append(r)
        print(f"   [{i + 1}/{len(questions)}] {r['class']:10} "
              f"ttft={r['ttft']} total={r['total']}", flush=True)
    return results


def run_load(base: str, jars: list[str], questions: list[str], timeout: float,
             concurrency: int) -> list[dict]:
    """N запросов, не более `concurrency` одновременно. Аккаунты раздаются по кругу."""
    results: list[dict] = [None] * len(questions)  # type: ignore[list-item]
    lock, nxt = threading.Lock(), [0]

    def worker(wid: int) -> None:
        while True:
            with lock:
                i = nxt[0]
                if i >= len(questions):
                    return
                nxt[0] += 1
            try:
                results[i] = ask(base, jars[i % len(jars)], questions[i], timeout)
            except BaseException as e:  # noqa: BLE001
                # ⚠⚠ ЛЮБОЙ ПОБЕГ ИЗ `ask` ОБЯЗАН СТАТЬ ЗАПИСАННЫМ ОТКАЗОМ, А НЕ ИСЧЕЗНУТЬ.
                # Раньше исключение убивало поток, `results[i]` оставался `None` и отфильтровывался
                # ниже: реальный отказ пропадал ИЗ ТАБЛИЦЫ КЛАССОВ и одновременно УМЕНЬШАЛ
                # знаменатель — то есть занижал ровно ту долю отказов, по которой выносится
                # вердикт. Найдено ревью PR #135.
                results[i] = {"class": TOOL_ERROR, "status": None, "ttft": None,
                              "total": None, "message_id": None, "chars": 0,
                              "detail": f"побег из ask: {type(e).__name__}: {str(e)[:80]}"}

    threads = [threading.Thread(target=worker, args=(w,), daemon=True)
               for w in range(concurrency)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"   волна из {len(questions)} запросов при {concurrency} одновременных "
          f"заняла {round(time.monotonic() - t0, 1)} с")
    # ⚠ Пустая ячейка означает, что поток умер, не записав исход. Молча её отбросить — значит
    # уменьшить знаменатель; поэтому она становится видимым классом.
    # ⚠⚠ КЛАСС — `tool_error`, И ОН НЕ В `FAILURES` (ревью PR #135, раунд 3): прежняя строка
    # говорила «засчитаны отказами», и оператор искал бы их в доле отказов, которой они не
    # достигают. Дефект ИЗМЕРИТЕЛЯ — это «мы не измеряли», а не «сервис не справился».
    lost = sum(1 for r in results if r is None)
    if lost:
        print(f"   ⚠⚠ потоков умерло без записи исхода: {lost} — класс tool_error "
              "(НЕ в доле отказов: это дефект измерителя, а не сервиса)")
    return [r if r is not None else
            {"class": TOOL_ERROR, "status": None, "ttft": None, "total": None,
             "message_id": None, "chars": 0, "detail": "поток умер, исход не записан"}
            for r in results]


def preflight(n: int, concurrency: int, accounts: int) -> None:
    """⚠⚠ ОТКАЗ ДО ЗАМЕРА, А НЕ РАЗБОР ПОСЛЕ. Инструмент обязан не запускать прогон, про который
    заранее известно, что он упрётся в лимит: иначе получится час работы и числа, которые нельзя
    использовать. Ёмкость — `аккаунты × 20` запросов в минуту."""
    need = math.ceil(n / RATE_LIMIT_PER_MIN)
    if accounts < need:
        raise SystemExit(
            f"ОСТАНОВ: {n} запросов требуют ≥{need} аккаунтов "
            f"(лимит {RATE_LIMIT_PER_MIN}/мин НА ПОЛЬЗОВАТЕЛЯ), передано {accounts}.\n"
            "  Иначе часть запросов получит 429 и развернётся, НЕ ДОЙДЯ ДО ДВИЖКА, — а 429 не 5xx,\n"
            "  и наивная сводка назвала бы такой прогон чистым. Заведите аккаунты:\n"
            "  python scripts/seed_users.py --accounts 'lt:5' --suffix load --role expert\n"
            "  (роль expert — чтобы не упереться в гейт профиля, который для роли `user` даёт 403)")
    if concurrency > accounts * RATE_LIMIT_PER_MIN:
        raise SystemExit(f"ОСТАНОВ: {concurrency} одновременных при {accounts} аккаунтах — "
                         "мгновенная ёмкость меньше волны, 429 гарантирован")


# ── Положительный контроль ───────────────────────────────────────────────────────────────────
_SLOW_TTFT = 0.6


def _fake_server():
    """Сервер, который умеет ломаться назначенным способом. Случай берётся из ПУТИ."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # noqa: D102 - тишина в выводе теста
            pass

        def _sse(self, chunks, delay=0.0):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if delay:
                time.sleep(delay)
            for ch in chunks:
                payload = ("data: " + json.dumps(ch, ensure_ascii=False) + "\n\n").encode()
                self.wfile.write(hex(len(payload))[2:].encode() + b"\r\n" + payload + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

        def _json(self, payload: dict, code: int = 200):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            case = self.path.split("/")[2] if self.path.startswith("/case/") else "fast"
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            # ⚠ НЕстримовый эндпоинт отвечает JSON'ом, а не SSE. Без этой ветки контроль
            # `ask_plain` проверял бы разбор SSE json-парсером и «находил» несуществующий дефект.
            plain = self.path.endswith("/api/chat")
            try:
                if plain and case in ("fast", "slow", "oops", "half"):
                    self._json({"answer": "ответ по существу", "unverified_numbers": [],
                                "sources": [], "low_relevance": False, "session_id": "x"})
                elif plain:
                    code = {"boom": 500, "limit": 429, "denied": 403}.get(case, 500)
                    self._json({"detail": "нет"}, code)
                elif case == "fast":
                    self._sse([{"type": "delta", "text": "раз"}, {"type": "done", "message_id": 1}])
                elif case == "slow":
                    self._sse([{"type": "delta", "text": "раз"}, {"type": "done", "message_id": 2}],
                              delay=_SLOW_TTFT)
                elif case == "oops":
                    self._sse([{"type": "delta", "text": "раз"}, {"type": "error"}])
                elif case == "half":
                    # ⚠ Длина считается, а НЕ пишется руками. Первая редакция объявляла 34 при
                    # теле в 33 байта — клиент честно ждал недостающий байт и получал `timeout`
                    # вместо `truncated`. Контроль поймал ошибку в САМОМ КОНТРОЛЕ; ровно ради
                    # этого он и нужен, но константа-длина такой ловушкой быть не обязана.
                    payload = b'data: {"type": "delta", "t": 1}\n\n'
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                elif case == "hang":
                    time.sleep(30)
                else:
                    code = {"boom": 500, "limit": 429, "denied": 403}.get(case, 500)
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
            except OSError:
                pass  # клиент ушёл по таймауту — это и проверяем

    class Quiet(ThreadingHTTPServer):
        # ⚠ Случай `hang` ПО ЗАМЫСЛУ бросает клиента по таймауту, и сервер печатает на это
        # трассировку `ConnectionAborted`. Она не ошибка теста, а его ожидаемый побочный эффект;
        # в выводе контроля она заслоняет собственно результат.
        def handle_error(self, request, client_address):
            pass

    srv = Quiet(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def selftest() -> int:
    """⚠⚠ БЕЗ ЭТОГО ПРОГОНА ЧИСЛА НИЧЕГО НЕ ЗНАЧАТ. Проверяем, что инструмент ВИДИТ каждый вид
    поломки. «Ноль отказов» у слепого инструмента выглядит точно так же, как у здорового."""
    srv = _fake_server()
    port = srv.server_address[1]
    checks = [
        ("fast", OK, "обычный ответ"),
        ("slow", OK, "медленный первый токен"),
        ("boom", HTTP_5XX, "500 от сервера"),
        ("limit", HTTP_429, "429 — лимит запросов"),
        ("denied", HTTP_403, "403 — гейт профиля"),
        ("oops", SSE_ERROR, "200, но движок упал ВНУТРИ стрима"),
        ("half", TRUNCATED, "поток оборван без `done`"),
        ("hang", TIMEOUT, "молчание до таймаута"),
    ]
    bad = []
    print("положительный контроль инструмента:")
    for case, expect, human in checks:
        r = ask(f"http://127.0.0.1:{port}/case/{case}", "session=x", "вопрос", timeout=2.0)
        got = r["class"]
        mark = "✅" if got == expect else "❌"
        print(f"  {mark} {human:38} ждали {expect:10} получили {got}")
        if got != expect:
            bad.append((case, expect, got))
    # ⚠ НЕстримовый пробник — та половина, которую видит человек. Проверяется теми же случаями.
    for case, expect, human in (("fast", OK, "нестримовый /api/chat: ответ"),
                                ("boom", HTTP_5XX, "нестримовый /api/chat: 500"),
                                ("limit", HTTP_429, "нестримовый /api/chat: 429")):
        r = ask_plain(f"http://127.0.0.1:{port}/case/{case}", "session=x", "вопрос", timeout=2.0)
        mark = "✅" if r["class"] == expect else "❌"
        print(f"  {mark} {human:38} ждали {expect:10} получили {r['class']}")
        if r["class"] != expect:
            bad.append((f"plain:{case}", expect, r["class"]))
    r = ask_plain(f"http://127.0.0.1:{port}/case/fast", "session=x", "вопрос", timeout=2.0)
    if r["text"] != "ответ по существу":
        bad.append(("plain:текст ответа", "ответ по существу", r["text"][:40]))
        print(f"  ❌ {'нестримовый: текст ответа извлечён':38} получили {r['text'][:40]!r}")
    else:
        print(f"  ✅ {'нестримовый: текст ответа извлечён':38} {r['text']!r}")

    slow = ask(f"http://127.0.0.1:{port}/case/slow", "session=x", "вопрос", timeout=5.0)
    if not (slow["ttft"] and slow["ttft"] >= _SLOW_TTFT * 0.8):
        bad.append(("slow-ttft", f">={_SLOW_TTFT}", slow["ttft"]))
        print(f"  ❌ {'задержка первого токена измерена':38} ждали >={_SLOW_TTFT} "
              f"получили {slow['ttft']}")
    else:
        print(f"  ✅ {'задержка первого токена измерена':38} ttft={slow['ttft']} с")
    # Отрицательный контроль на сам предохранитель: недействительный класс обязан ГАСИТЬ числа.
    s = summarize([{"class": OK, "ttft": 1.0, "total": 2.0, "message_id": 1},
                   {"class": HTTP_429, "ttft": None, "total": 0.1}], {"mode": "проверка"})
    if s["valid"] or "ttft_p50" in s:
        bad.append(("гашение чисел при 429", "перцентилей нет", "перцентили напечатаны"))
        print("  ❌ предохранитель не сработал: при 429 перцентили всё равно посчитались")
    else:
        print(f"  ✅ {'429 в наборе гасит перцентили':38} valid={s['valid']}")
    srv.shutdown()
    srv.server_close()  # ⚠ без этого слушающий сокет остаётся открытым — тест в батарее шумит
    if bad:
        print(f"\n❌ КОНТРОЛЬ ПРОВАЛЕН ({len(bad)}): {bad}\n"
              "Числа замера при непройденном контроле НЕ ИНТЕРПРЕТИРУЮТСЯ.")
        return 1
    # ⚠ Без числа в тексте: счётчик проверок уже менялся дважды, а устаревшее число рядом с
    # «пройдено» читается как «проверок столько и есть». Тот же класс, что «0 ложных на 11 формах».
    print("\n✅ контроль пройден — инструмент видит каждый проверенный исход; можно мерить")
    return 0


# ── main ─────────────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Уровень 4: латентность, нагрузка (EV22 #130)")
    ap.add_argument("--mode", choices=("selftest", "single", "load"), required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000",
                    help="⚠ ТОЛЬКО локальный адрес НА БОЮ: снаружи меряется канал, а не сервис")
    ap.add_argument("--accounts", default="", help="'логин:пароль,логин:пароль' — выделенные")
    ap.add_argument("--n", type=int, default=30, help="сколько запросов")
    ap.add_argument("--concurrency", type=int, default=10, help="одновременных (режим load)")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=719, help="выборка вопросов воспроизводима")
    ap.add_argument("--json", default="", help="куда положить результат")
    ap.add_argument("--max-failure-share", type=float, default=0.01,
                    help="порог доли отказов для КОДА ВОЗВРАТА; по гайду ≤0.01")
    ap.add_argument("--warmup", type=int, default=1,
                    help="сколько первых запросов отбросить: e5 грузится ЛЕНИВО, и первый "
                         "запрос после рестарта несёт разовую загрузку модели")
    args = ap.parse_args()

    if args.mode == "selftest":
        return selftest()

    pairs = [p for p in (a.strip() for a in args.accounts.split(",")) if p]
    if not pairs:
        raise SystemExit("ОСТАНОВ: --accounts обязателен (горячий путь требует сессии)")
    preflight(args.n, args.concurrency if args.mode == "load" else 1, len(pairs))

    print(f"вход: {len(pairs)} аккаунт(ов) на {args.base_url}")
    jars = []
    for p in pairs:
        user, _, pw = p.partition(":")
        jars.append(login(args.base_url, user, pw, timeout=args.timeout))
        print(f"   ✅ {user}")

    # ⚠⚠ ПРОГРЕВ. `embeddings._model()` под `@lru_cache(maxsize=1)` — e5 грузится ЛЕНИВО, на
    # первом запросе, а не при старте. Значит первый запрос после рестарта несёт разовую
    # стоимость загрузки 2.1 ГБ и структурно отличается от остальных. На n=30 p95 — это 29-е
    # значение, то есть ОДИН выброс наверху и ЕСТЬ p95: невыброшенный прогрев испортил бы ровно
    # ту величину, ради которой всё меряется.
    # ⚠ Умолчание 1, а не 0, сознательно: на бою приложение обычно давно прогрето и потеря
    # одного замера из тридцати ничего не стоит, а холодный старт без прогрева портит p95.
    # Число прогревочных пишется в сводку и в `_command` — молча выбрасывать замеры нельзя.
    if args.warmup:
        print(f"прогрев: {args.warmup} запрос(ов), результаты отбрасываются")
        for q in load_questions(args.warmup, args.seed - 1):
            r = ask(args.base_url, jars[0], q, args.timeout)
            print(f"   прогрев: {r['class']} ttft={r['ttft']} total={r['total']}", flush=True)

    questions = load_questions(args.n, args.seed)
    cmd = (f"python scripts/eval_level4.py --mode {args.mode} --n {args.n}"
           + (f" --concurrency {args.concurrency}" if args.mode == "load" else "")
           + f" --seed {args.seed} --timeout {args.timeout} --warmup {args.warmup}"
           f" --base-url {args.base_url}")
    meta = {"mode": args.mode, "_command": cmd, "accounts": len(pairs), "warmup": args.warmup,
            "concurrency": args.concurrency if args.mode == "load" else 1,
            "base_url": args.base_url, "seed": args.seed, "timeout": args.timeout}

    if args.mode == "single":
        results = run_single(args.base_url, jars, questions, args.timeout)
    else:
        results = run_load(args.base_url, jars, questions, args.timeout, args.concurrency)

    s = summarize(results, meta)
    s["raw"] = results
    _print_summary(s)
    if args.json:
        Path(args.json).write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"   → {args.json}")
    return verdict(s, args.max_failure_share)


def verdict(s: dict, max_failure_share: float) -> int:
    """Код возврата. ⚠⚠ ЧИТАТЬ ЕГО, А НЕ ПОСЛЕДНЮЮ СТРОКУ — как у `deploy.sh`.

    ⚠⚠ ПЕРВАЯ РЕДАКЦИЯ ВОЗВРАЩАЛА 0 ПРИ 100 % ПРОВАЛОВ. Найдено сухим прогоном 28.08.2026 против
    живого приложения с выключенным Qdrant: оба запроса дали `sse_error`, сводка честно напечатала
    «успешных 0», а код возврата был 0 — то есть автоматический вызов (гейт, cron, скрипт выкатки)
    счёл бы прогон удавшимся. Предохранитель, который СООБЩАЕТ о провале и не останавливает, равен
    отсутствующему — это записанное правило проекта, и здесь оно нарушалось буквально."""
    if not s["valid"]:
        print("   ❌ код возврата 1: замер недействителен (запросы не дошли до движка)")
        return 1
    if not s["ok"]:
        print("   ❌ код возврата 1: успешных ответов ноль — мерить нечего")
        return 1
    share = s.get("failure_share") or 0.0
    if share > max_failure_share:
        print(f"   ❌ код возврата 1: доля отказов {share} выше порога {max_failure_share} "
              "(порог гайда — ≤0.01)")
        return 1
    print("   ✅ код возврата 0: замер состоялся")
    return 0


if __name__ == "__main__":
    sys.exit(main())
