"""test36: витрина тарифов как у Нейроюриста (плейсхолдеры сумм) — на боевом образе и боевой схеме.

ЗАЧЕМ. Релиз добавляет колонку `leads.options` (опции с витрины), приём опций в `/api/leads`, витрину во
вкладке «Тариф» кабинета и заявку из кабинета (`/api/plan-request`). Колонка создаётся на СТАРТЕ
приложения — проверяется боевая база. Кабинет и заявка из него проверяются КОДОМ ОБРАЗА на базе в
памяти: заводить учётку и заявку в боевой базе ради проверки нельзя. Лендинг в образ не входит
(`.dockerignore`) — его сверяют маркеры профиля и проверка снаружи после выкатки.

⚠ Проверка НИЧЕГО не пишет в боевую базу: схема только читается; запрос к `/api/leads` нарочно без
согласия — его отклоняют и старый, и новый код, а новый вдобавок называет ошибку опций. Положительный
контроль — прогоном на `test35` (бой до выкатки), результат в профиле `test36`.
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


def http(path: str, data: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, method="POST" if data is not None else "GET",
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        r = urllib.request.urlopen(req, timeout=20)
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

    # 1. схема боевой базы — колонка опций заявки (только чтение)
    db_path = settings.APP_DB_URL.split("sqlite:///", 1)[-1]
    cols = set()
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(leads)")}
    except sqlite3.Error as e:
        print(f"   ⚠ база не открылась: {e}")
    check("options" in cols, "у leads есть колонка options", "миграция leads.options не прошла — процесс старый?")

    # 2. /api/leads знает опции витрины. Без согласия заявку отклоняет любой код — в базу не пишется ничего.
    code, body = http("/api/leads", {"tariff": "Профи", "trial": True, "consent": False})
    try:
        errors = json.loads(body).get("errors", {})
    except ValueError:
        errors = {}
    check(code == 422 and "options" in errors, "заявка с лендинга проверяет опции витрины (проба — только «Старт»)",
          f"опции витрины не проверяются: {code} {sorted(errors)}")

    # 3. кодом образа на базе в памяти: витрина в кабинете и заявка из кабинета, привязанная к учётке.
    # На старом коде (положительный контроль) модуля витрины нет — это провал проверки, а не падение.
    try:
        from unittest import mock

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from starlette.requests import Request

        from app.api import web
        from app.core import pricing
        from app.db import queries as q
        from app.db.models import Base

        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(eng)
        S = sessionmaker(bind=eng, expire_on_commit=False)
        with S() as db:
            u = q.create_user(db, "check-test36", "x", role="user")
            q.update_profile(db, u.id, full_name="Проверка Выкатки", region="Курская область", telegram="",
                             consent=True, email="check@example.invalid", phone="+7 900 000-00-00")
            u = q.get_user(db, u.id)
            db.expunge(u)
        req = Request({"type": "http", "method": "GET", "path": "/profile", "headers": [], "query_string": b"",
                       "session": {}, "app": None})
        with mock.patch.object(web, "get_session", S), mock.patch.object(web, "current_user", return_value=u):
            page = web.profile_page(req, tab="plan").body.decode("utf-8")
            r = web.plan_request(req, tariff="Профи", kind="corporate", period="year", seats="5",
                                 trial_flag="", consent="1")
        check('id="tariffs"' in page and page.count(f"{pricing.PRICE} ₽") >= 6 and "Корпоративные" in page,
              "вкладка «Тариф»: витрина с плейсхолдерами и вкладкой «Корпоративные»", "витрины в кабинете нет")
        with S() as db:
            leads = q.list_leads(db)
        check(r.status_code == 303 and len(leads) == 1 and leads[0].user_id == u.id
              and "корпоративный · год · 5 польз." in (leads[0].options or ""),
              "«Подключить» в кабинете — заявка, привязанная к учётке, с опциями",
              f"заявка из кабинета не сложилась: {r.status_code}, заявок {len(leads)}")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"сценарии кодом образа не выполнились: {type(e).__name__}: {e}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
