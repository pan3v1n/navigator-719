"""test40: логин — рабочий email; вход без учёта регистра.

ЗАЧЕМ. Схема базы не меняется. Сценарий идёт через НАСТОЯЩИЕ маршруты приложения (TestClient без
lifespan — init_db боевой базы не вызывается) на базе в памяти: создание учётки admin'ом по email с
заглавными, вход тем же email в другом регистре, подстановка email в анкету, отказ кириллическому
адресу, прежний вид логина по-прежнему принимается.

⚠ Проверка НИЧЕГО не пишет в боевую базу: get_session подменён в auth, web и admin. Положительный
контроль — прогоном на `test39` (бой до выкатки), результат в профиле `test40`.
"""
import os
import urllib.error
import urllib.request

from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

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

    try:
        from unittest import mock

        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.api import admin as adm
        from app.api import auth, web
        from app.core.regions import region_from_username
        from app.db import queries as q
        from app.db.models import Base
        from main import app

        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(eng)
        S = sessionmaker(bind=eng, expire_on_commit=False)
        pw = "Check-Admin-40"
        with S() as db:
            q.create_user(db, "check-admin40", auth.hash_password(pw), role="admin")
        with mock.patch.object(auth, "get_session", S), mock.patch.object(web, "get_session", S), \
                mock.patch.object(adm, "get_session", S):
            web._login_limit.reset()
            # https: на сервере COOKIE_SECURE=true, кука сессии Secure — по http клиент её не вернул бы, и все
            # /api/admin/* ответили бы 401 на верном коде (ревью test40, HIGH). Без `with` — lifespan не запускается.
            boss = TestClient(app, base_url="https://testserver")
            boss.post("/login", data={"username": "check-admin40", "password": pw}, follow_redirects=False)
            r = boss.post("/api/admin/users", data={"username": "Check.Ivanov@KCCI.ru", "role": "user"})
            password = r.text.split('id="new-password">')[1].split("<")[0] if 'id="new-password">' in r.text else ""
            bad_addr = boss.post("/api/admin/users", data={"username": "иванов@кцци.рф", "role": "user"})
            old_form = boss.post("/api/admin/users", data={"username": "kursk.check40", "role": "user"})
            mixed = TestClient(app, base_url="https://testserver").post("/login", data={"username": "CHECK.IVANOV@kcci.ru", "password": password},
                                         follow_redirects=False)
            web._login_limit.reset()
        with S() as db:
            u = q.get_user_by_username(db, "check.ivanov@kcci.ru")
        check(r.status_code == 200 and u is not None and u.email == "check.ivanov@kcci.ru" and not u.consent,
              "учётка по email: логин строчными, email подставлен в анкету, согласие не поставлено",
              f"создание по email: {r.status_code}, учётка {u.username if u else None}")
        check(bool(password) and mixed.status_code == 302, "вход тем же email в другом регистре",
              f"вход в другом регистре: {mixed.status_code}")
        check(bad_addr.status_code == 400 and old_form.status_code == 200,
              "кириллический адрес — отказ; прежний вид логина принимается",
              f"кириллица {bad_addr.status_code}, прежний вид {old_form.status_code}")
        check(region_from_username("perm.office@kcci.ru") == "" and region_from_username("perm.expert1") != "",
              "регион из логина-почты не угадывается, из прежнего — как раньше", "регион по логину-почте")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"сценарии кодом образа не выполнились: {type(e).__name__}: {e}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
