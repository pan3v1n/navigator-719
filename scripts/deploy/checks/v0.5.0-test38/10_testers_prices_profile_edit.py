"""test38: цены из презентации, тариф «Тестировщик» с подтверждением, правка анкеты admin'ом.

ЗАЧЕМ. Релиз добавляет колонки users.tester_verified_at / tester_verified_by — они создаются на СТАРТЕ
приложения, проверяется боевая база. Сценарии админки (правка логина и анкеты, закрытие доступа
тестировщику) и цены проверяются КОДОМ ОБРАЗА на базе в памяти: заводить и править учётки в боевой базе
ради проверки нельзя. Лендинг в образ не входит — его сверяют маркеры профиля и проверка снаружи.

⚠ Проверка НИЧЕГО не пишет в боевую базу: схема только читается. Положительный контроль — прогоном на
`test37` (бой до выкатки), результат в профиле `test38`.
"""
import os
import sqlite3
import urllib.error
import urllib.request

from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

from app.core.config import settings  # noqa: E402

BASE = os.environ.get("CHECK_BASE", "http://127.0.0.1:8000")


def http(path: str) -> int:
    try:
        return urllib.request.urlopen(BASE + path, timeout=20).status
    except urllib.error.HTTPError as e:
        return e.code


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        print(f"   OK: {good}" if ok else f"   ❌ {fail}")
        bad += not ok

    code = http("/ping")
    check(code == 200, "/ping отвечает", f"/ping → {code}")

    # 1. схема боевой базы (только чтение)
    db_path = settings.APP_DB_URL.split("sqlite:///", 1)[-1]
    users = set()
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as c:
            users = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    except sqlite3.Error as e:
        print(f"   ⚠ база не открылась: {e}")
    need = {"tester_verified_at", "tester_verified_by"}
    check(need <= users, "у users есть подтверждение тестировщика",
          f"миграция users не прошла, нет: {sorted(need - users)}")

    # 2. кодом образа на базе в памяти. На старом коде (положительный контроль) этих функций и полей нет —
    # это провал проверки, а не падение.
    try:
        from unittest import mock

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from starlette.requests import Request

        from app.api import admin as adm
        from app.core import plans, pricing
        from app.db import queries as q
        from app.db.models import Base

        check(pricing.PRICES == {"Старт": 9900, "Стандарт": 14900, "Профи": 19900}
              and pricing.price_line("Стандарт", kind="corporate", period="year", seats=3) == "536 400 ₽/год",
              "цены из презентации; корпоративный «Стандарт» × 3 × год = 536 400 ₽",
              f"цены не те: {getattr(pricing, 'PRICES', None)}")

        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(eng)
        S = sessionmaker(bind=eng, expire_on_commit=False)
        with S() as db:
            boss = q.create_user(db, "check-admin38", "x", role="admin")
            u = q.create_user(db, "check-user38", "x", role="user")
            t = q.create_user(db, "check-tester38", "x", role="user")
            q.set_user_plan(db, t.id, plans.TESTER_PLAN, plans.anchor_from_date(plans.msk_today()),
                            verified_by=boss.id)
        req = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"",
                       "session": {}, "app": None})
        with mock.patch.object(adm, "get_session", S):
            r = adm.admin_edit_profile(req, u.id, username="check-renamed38", full_name="Проверка Выкатки",
                                       position="", email="check@example.invalid", phone="+7 900 000-00-00",
                                       region="Курская область", telegram="", admin=boss)
            taken = adm.admin_edit_profile(req, u.id, username="check-admin38", full_name="", position="",
                                           email="", phone="", region="", telegram="", admin=boss)
            adm.admin_tester_revoke(t.id, admin=boss)
        with S() as db:
            got, tester = q.get_user(db, u.id), q.get_user(db, t.id)
        check(r.status_code == 303 and got.username == "check-renamed38" and got.full_name == "Проверка Выкатки"
              and not got.consent, "admin правит логин и анкету; согласие на ПДн не ставится",
              f"правка анкеты: {r.status_code}, логин {got.username}, согласие {got.consent}")
        check(taken.status_code == 400 and got.username == "check-renamed38",
              "занятый логин — отказ, учётка не тронута", f"занятый логин: {taken.status_code}")
        check(tester.plan == plans.TESTER_PLAN and plans.plan_expired(tester),
              "«Закрыть доступ» тестировщику: тариф тот же, срок истёк (не безлимит)",
              f"отзыв тестировщика: тариф {tester.plan!r}, истёк {plans.plan_expired(tester)}")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"сценарии кодом образа не выполнились: {type(e).__name__}: {e}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
