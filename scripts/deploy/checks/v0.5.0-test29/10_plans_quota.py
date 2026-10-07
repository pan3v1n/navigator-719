"""test29: лимиты тарифов (#160) — на боевом образе и боевой схеме базы.

ЗАЧЕМ. Релиз меняет схему базы (таблица `answer_usage`, колонки `users.plan`/`plan_started_at`) и
добавляет блокировку по тарифу. Схема создаётся на СТАРТЕ приложения (`init_db`), и старый процесс
её не создаст — проверяется боевая база. Блокировка проверяется КОДОМ ОБРАЗА на базе в памяти:
тарифных пользователей на бою пока нет, а заводить их в боевой базе ради проверки нельзя.

⚠ Проверка НИЧЕГО не пишет в боевую базу: схема только читается, сценарий 402 идёт в «:memory:».
Положительный контроль — прогоном на `test28` (бой до выкатки), результат в профиле `test29`.
"""
import json
import os
import sqlite3
import urllib.error
import urllib.request

from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

from app.core.config import settings  # noqa: E402

BASE = os.environ.get("CHECK_BASE", "http://127.0.0.1:8000")


def http(path: str) -> tuple[int, str]:
    try:
        r = urllib.request.urlopen(BASE + path, timeout=20)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        print(f"   OK: {good}" if ok else f"   ❌ {fail}")
        bad += not ok

    code, _ = http("/ping")
    check(code == 200, "/ping отвечает", f"/ping → {code}")

    # 1. схема боевой базы — создана стартом нового процесса (только чтение)
    db_path = settings.APP_DB_URL.split("sqlite:///", 1)[-1]
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as c:
            table = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_usage'").fetchone()
            cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
            planned = c.execute("SELECT COUNT(*) FROM users WHERE plan IS NOT NULL").fetchone()[0] \
                if "plan" in cols else None
    except sqlite3.Error as e:
        table, cols, planned = None, set(), None
        print(f"   ⚠ база не открылась: {e}")
    check(bool(table), "в боевой базе есть таблица answer_usage", "таблицы answer_usage нет — процесс старый?")
    check({"plan", "plan_started_at"} <= cols, "у users есть колонки plan и plan_started_at",
          f"миграция users не прошла: {sorted(cols)}")
    if planned is not None:
        print(f"   · пользователей с тарифом: {planned} (до назначения в админке — 0, лимит никого не держит)")

    # 2. блокировка — кодом образа, на базе в памяти. На старом коде (положительный контроль)
    # модуля тарифов нет — это провал проверки, а не падение скрипта.
    try:
        from datetime import timedelta

        from fastapi import HTTPException
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.api import chat as chat_mod
        from app.api.ratelimit import SlidingWindow
        from app.core import plans
        from app.db import queries as q
        from app.db.models import AnswerUsage, Base

        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(eng)
        S = sessionmaker(bind=eng, expire_on_commit=False)
        with S() as db:
            u = q.create_user(db, "check-test29", "x", role="user")
            anchor = plans.anchor_from_date(plans.msk_today())
            q.set_user_plan(db, u.id, "Старт", anchor)
            db.add_all([AnswerUsage(user_id=u.id, ts=anchor + timedelta(minutes=1)) for _ in range(100)])
            db.commit()
            u = q.get_user(db, u.id)
            db.expunge(u)
        called = []
        chat_mod.get_session, chat_mod._chat_limit = S, SlidingWindow(1000, window=60.0)
        chat_mod.answer = lambda *a, **k: called.append(1)
        try:
            chat_mod.chat(chat_mod.ChatRequest(message="проверка выкатки"), user=u)
            status, detail = 200, ""
        except HTTPException as e:
            status, detail = e.status_code, str(e.detail)
        check(status == 402 and "«Старт»" in detail and not called,
              "исчерпанный тариф → 402 до движка, причина названа",
              f"блокировки нет: {status} {detail[:100]!r}, движок звался {len(called)} раз")

    except Exception as e:  # noqa: BLE001
        check(False, "", f"сценарий блокировки не выполнился: {type(e).__name__}: {e}")

    # 3. политика ПДн называет тариф и учёт расхода
    code, page = http("/privacy")
    check(code == 200 and "тариф и дата подключения" in page and "учёт расхода запросов" in page,
          "политика: тариф и учёт расхода названы", f"политика старая или недоступна ({code})")

    # 4. /navigate для анонима — по-прежнему за входом (закрытие тарифным не открыло его другим)
    try:
        req = urllib.request.Request(BASE + "/navigate", data=json.dumps({"query": "станки"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        code = urllib.request.urlopen(req, timeout=20).status
    except urllib.error.HTTPError as e:
        code = e.code
    check(code == 401, "/navigate без входа → 401", f"/navigate без входа → {code}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
