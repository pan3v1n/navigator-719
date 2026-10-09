"""test35: кабинет, пробный режим без входа, новая админка (/admin) — на боевом образе и боевой схеме.

ЗАЧЕМ. Релиз меняет схему базы (колонки `users`: контакты, организация, срок тарифа, блокировка,
эпоха входа; колонки `leads`: статус и связь с учёткой; таблица `guest_messages`) и открывает
`/chat` без входа — с потолками расхода. Схема создаётся на СТАРТЕ приложения (`init_db`), и старый
процесс её не создаст — проверяется боевая база. Блокировка, срок тарифа и потолки пробного режима
проверяются КОДОМ ОБРАЗА на базе в памяти: заводить учётки и гостей в боевой базе ради проверки нельзя.

⚠ Проверка НИЧЕГО не пишет в боевую базу и НЕ зовёт DeepSeek: схема только читается, сценарии идут
в «:memory:», вопрос гостя без куки отклоняется до движка. Положительный контроль — прогоном на
`test34` (бой до выкатки), результат в профиле `test35`.
"""
import os
import sqlite3
import urllib.error
import urllib.request

from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

from app.core.config import settings  # noqa: E402

BASE = os.environ.get("CHECK_BASE", "http://127.0.0.1:8000")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_open = urllib.request.build_opener(_NoRedirect).open


def http(path: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, str, dict]:
    """Ответ БЕЗ перехода по редиректу: «/admin/users → /login» и «/chat → /login» различаются
    именно кодом и адресом, а не страницей входа, на которую оба привели бы."""
    req = urllib.request.Request(BASE + path, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        r = _open(req, timeout=20)
        return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        print(f"   OK: {good}" if ok else f"   ❌ {fail}")
        bad += not ok

    code, _, _ = http("/ping")
    check(code == 200, "/ping отвечает", f"/ping → {code}")

    # 1. схема боевой базы — создана стартом нового процесса (только чтение)
    db_path = settings.APP_DB_URL.split("sqlite:///", 1)[-1]
    users_cols, leads_cols, guest_table, blocked = set(), set(), None, None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as c:
            users_cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
            leads_cols = {r[1] for r in c.execute("PRAGMA table_info(leads)")}
            guest_table = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_messages'").fetchone()
            if "blocked_at" in users_cols:
                blocked = c.execute("SELECT COUNT(*) FROM users WHERE blocked_at IS NOT NULL").fetchone()[0]
    except sqlite3.Error as e:
        print(f"   ⚠ база не открылась: {e}")
    need_users = {"position", "email", "phone", "org", "inn", "plan_expires_at", "blocked_at", "auth_epoch"}
    check(need_users <= users_cols, "у users есть колонки кабинета, срока тарифа, блокировки и эпохи входа",
          f"миграция users не прошла, нет: {sorted(need_users - users_cols)}")
    need_leads = {"status", "status_at", "note", "user_id"}
    check(need_leads <= leads_cols, "у leads есть статус, заметка и связь с учёткой",
          f"миграция leads не прошла, нет: {sorted(need_leads - leads_cols)}")
    check(bool(guest_table), "в боевой базе есть таблица guest_messages", "таблицы guest_messages нет — процесс старый?")
    if blocked is not None:
        print(f"   · заблокированных учёток: {blocked} (сразу после выкатки — 0)")

    # 2. страницы без входа: новая панель за входом, пробный режим открыт, вопрос без куки — до движка
    code, _, h = http("/admin/users")
    check(code == 302 and h.get("location", h.get("Location", "")).endswith("/login"),
          "/admin/users без входа → на вход (раздел панели существует)", f"/admin/users без входа → {code}")
    code, page, h = http("/chat")
    cookie = h.get("set-cookie", h.get("Set-Cookie", ""))
    # Нет настройки — старый образ (положительный контроль): это провал проверки, а не падение.
    trial_on = getattr(settings, "GUEST_TRIAL_ENABLED", None)
    check(trial_on is not None, "в образе есть настройки пробного режима", "в образе нет пробного режима — образ старый")
    if trial_on:
        check(code == 200 and 'id="guest-banner"' in page and "guest719=" in cookie,
              "/chat без входа — пробный режим, кука гостя ставится",
              f"/chat без входа → {code}, баннер пробы {'есть' if 'guest-banner' in page else 'нет'}")
    else:
        check(code == 302, "пробный режим выключен в .env — /chat без входа ведёт на вход",
              f"пробный режим выключен, а /chat без входа → {code}")
    code, body, _ = http("/api/guest/chat", data=b'{"message": "check"}',
                         headers={"Content-Type": "application/json"})
    want = 403 if trial_on else 404
    check(code == want, f"вопрос гостя без куки → {want} до движка", f"вопрос гостя без куки → {code} {body[:80]!r}")
    code, page, _ = http("/login")
    check(code == 200 and "подключенным тарифом" not in page and "подключённым тарифом" not in page,
          "на входе нет подзаголовка «для сотрудников… с подключённым тарифом»", f"страница входа старая ({code})")
    code, page, _ = http("/privacy")
    check(code == 200 and "Пробный режим без входа" in page,
          "политика: раздел о пробном режиме", f"политика старая или недоступна ({code})")

    # 3. кодом образа на базе в памяти: блокировка, эпоха входа, срок тарифа, потолки пробы.
    # На старом коде (положительный контроль) этих модулей и колонок нет — это провал проверки,
    # а не падение скрипта.
    try:
        from datetime import timedelta

        from fastapi import HTTPException
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from starlette.requests import Request

        from app.api import auth
        from app.api import chat as chat_mod
        from app.api import guest as guest_mod
        from app.api.ratelimit import SlidingWindow
        from app.core import plans
        from app.db import queries as q
        from app.db.models import Base

        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(eng)
        S = sessionmaker(bind=eng, expire_on_commit=False)
        auth.get_session = chat_mod.get_session = guest_mod.get_session = S

        def req(session: dict | None = None) -> Request:
            return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                            "client": ("203.0.113.35", 0), "session": session if session is not None else {}})

        # 3а. блокировка закрывает уже открытую сессию; разблокировка старую сессию не воскрешает
        with S() as db:
            u = q.create_user(db, "check-test35", "x", role="user")
        sess = {"user_id": u.id, "epoch": 0}
        seen_before = auth.current_user(req(dict(sess))) is not None
        with S() as db:
            q.set_blocked(db, u.id, True)
        seen_blocked = auth.current_user(req(dict(sess))) is not None
        with S() as db:
            q.set_blocked(db, u.id, False)
        seen_after = auth.current_user(req(dict(sess))) is not None
        check(seen_before and not seen_blocked and not seen_after,
              "блокировка закрывает открытую сессию, разблокировка её не воскрешает",
              f"сессия видна: до {seen_before}, при блокировке {seen_blocked}, после разблокировки {seen_after}")

        # 3б. истёкший срок тарифа → 402 до движка, причина названа
        with S() as db:
            t = q.create_user(db, "check-test35-plan", "x", role="user")
            q.set_user_plan(db, t.id, plans.TRIAL_PLAN, plans.anchor_from_date(plans.msk_today()) - timedelta(days=8),
                            expires_at=plans.anchor_from_date(plans.msk_today()))
            t = q.get_user(db, t.id)
            db.expunge(t)
        called = []
        chat_mod._chat_limit = SlidingWindow(1000, window=60.0)
        chat_mod.answer = lambda *a, **k: called.append(1)
        try:
            chat_mod.chat(chat_mod.ChatRequest(message="проверка выкатки"), user=t)
            status, detail = 200, ""
        except HTTPException as e:
            status, detail = e.status_code, str(e.detail)
        check(status == 402 and "истёк" in detail and not called,
              "истёкший срок тарифа → 402 до движка, причина названа",
              f"срок не держит: {status} {detail[:100]!r}, движок звался {len(called)} раз")

        # 3в. потолки пробного режима — бронью до движка: личный (402) и общий суточный (429)
        n, total = settings.GUEST_TRIAL_QUESTIONS, settings.GUEST_DAILY_TOTAL
        with S() as db:
            for i in range(n):
                q.log_guest_message(db, guest_id="spent", session_id="s", role="assistant",
                                    content=f"ответ {i}", charged=True)
        try:
            guest_mod._reserve(req(), "spent", "s2", "ещё вопрос")
            status = 200
        except HTTPException as e:
            status = e.status_code
        check(status == 402, f"гость, потративший {n} пробных вопроса, → 402", f"личный потолок не держит: {status}")
        with S() as db:
            for i in range(total):
                q.log_guest_message(db, guest_id=f"g{i}", session_id="s", role="assistant",
                                    content="ответ", charged=True)
        try:
            guest_mod._reserve(req(), "fresh", "f1", "первый вопрос")
            status = 200
        except HTTPException as e:
            status = e.status_code
        check(status == 429, f"общий суточный потолок ({total}) → 429 и новому гостю",
              f"общий потолок не держит: {status}")
        print(f"   · пробный режим: {n} вопроса на гостя, {settings.GUEST_IP_PER_DAY} с адреса за сутки, "
              f"{total} ответов всем гостям за сутки (МСК)")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"сценарии кодом образа не выполнились: {type(e).__name__}: {e}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
