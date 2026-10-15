"""test39: смена пароля пользователем в кабинете; закрытие доступа тестировщику без отметки подтверждения.

ЗАЧЕМ. Схема базы не меняется. Смена пароля проверяется КОДОМ ОБРАЗА на базе в памяти: менять пароли в
боевой базе ради проверки нельзя. Сценарий: верный текущий пароль → новый хеш, эпоха входа +1, этот
браузер остаётся в сессии; неверный текущий → отказ, пароль прежний. Отдельно — закрытие доступа
тестировщику, назначенному до test38 (колонки подтверждения пусты), не записывает подтверждающего.

⚠ Проверка НИЧЕГО не пишет в боевую базу. Положительный контроль — прогоном на `test38` (бой до выкатки),
результат в профиле `test39`.
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

    # кодом образа на базе в памяти. На старом коде (положительный контроль) маршрута смены пароля нет —
    # это провал проверки, а не падение.
    try:
        from unittest import mock

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from starlette.requests import Request

        from app.api import admin as adm
        from app.api import auth, web
        from app.core import plans
        from app.db import queries as q
        from app.db.models import Base, User

        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(eng)
        S = sessionmaker(bind=eng, expire_on_commit=False)
        old, new = "Stary-Parol-39", "Novy-Parol-39"
        with S() as db:
            boss = q.create_user(db, "check-admin39", "x", role="admin")
            u = q.create_user(db, "check-user39", auth.hash_password(old), role="expert")
            t = q.create_user(db, "check-tester39", "x", role="user")
            q.set_user_plan(db, t.id, plans.TESTER_PLAN, plans.anchor_from_date(plans.msk_today()))
            row = db.get(User, t.id)
            row.tester_verified_at = row.tester_verified_by = None      # как у назначенного до test38
            db.commit()

        def req():
            return Request({"type": "http", "method": "POST", "path": "/profile/password", "headers": [],
                            "query_string": b"", "session": {"user_id": u.id, "epoch": 0}, "app": None})

        with mock.patch.object(web, "get_session", S), mock.patch.object(auth, "get_session", S), \
                mock.patch.object(adm, "get_session", S):
            bad_req = req()
            refused = web.password_submit(bad_req, current_password="не-тот", new_password=new, repeat_password=new)
            ok_req = req()
            done = web.password_submit(ok_req, current_password=old, new_password=new, repeat_password=new)
            adm.admin_tester_revoke(t.id, admin=boss)
        with S() as db:
            got, tester = q.get_user(db, u.id), q.get_user(db, t.id)
        check(refused.status_code == 400, "неверный текущий пароль — отказ", f"неверный текущий: {refused.status_code}")
        check(done.status_code == 303 and auth.verify_password(new, got.password_hash)
              and not auth.verify_password(old, got.password_hash) and got.auth_epoch == 1,
              "смена пароля: новый хеш, эпоха входа +1 (другие входы гаснут)",
              f"смена пароля: {done.status_code}, эпоха {got.auth_epoch}")
        check(ok_req.session.get("epoch") == 1 and ok_req.session.get("user_id") == u.id,
              "этот браузер остаётся в сессии с новой эпохой", f"сессия после смены: {ok_req.session}")
        check(plans.plan_expired(tester) and tester.tester_verified_at is None and tester.tester_verified_by is None,
              "закрытие доступа тестировщику не записывает подтверждающего",
              f"закрытие: истёк {plans.plan_expired(tester)}, подтверждение {tester.tester_verified_by}")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"сценарии кодом образа не выполнились: {type(e).__name__}: {e}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
