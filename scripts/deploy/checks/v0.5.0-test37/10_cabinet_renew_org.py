"""test37: источники бесплатно, «Продлить», попап корпоративных, организация с подтверждением, форма профиля.

ЗАЧЕМ. Релиз добавляет колонки users.org_verified_at / org_verified_by / plan_kind и leads.message — они
создаются на СТАРТЕ приложения, проверяется боевая база. Кабинет («Продлить», попап, подтверждение
организации, форма профиля по «Редактировать») проверяется КОДОМ ОБРАЗА на базе в памяти: заводить учётки и
заявки в боевой базе ради проверки нельзя. Лендинг в образ не входит — его сверяют маркеры профиля и
проверка снаружи после выкатки.

⚠ Проверка НИЧЕГО не пишет в боевую базу: схема только читается; запрос к `/api/leads` нарочно без согласия
— его отклоняет любой код, новый вдобавок называет ошибку длины комментария. Положительный контроль —
прогоном на `test36` (бой до выкатки), результат в профиле `test37`.
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

    # 1. схема боевой базы (только чтение)
    db_path = settings.APP_DB_URL.split("sqlite:///", 1)[-1]
    users, leads = set(), set()
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as c:
            users = {r[1] for r in c.execute("PRAGMA table_info(users)")}
            leads = {r[1] for r in c.execute("PRAGMA table_info(leads)")}
    except sqlite3.Error as e:
        print(f"   ⚠ база не открылась: {e}")
    need = {"org_verified_at", "org_verified_by", "plan_kind"}
    check(need <= users, "у users есть подтверждение организации и вид тарифа",
          f"миграция users не прошла, нет: {sorted(need - users)}")
    check({"message", "kind"} <= leads, "у leads есть комментарий заявителя и вид заявки",
          f"миграция leads не прошла, нет: {sorted({'message', 'kind'} - leads)}")

    # 2. /api/leads знает комментарий. Без согласия заявку отклоняет любой код — в базу не пишется ничего.
    code, body = http("/api/leads", {"tariff": "Профи", "consent": False, "message": "x" * 1001})
    try:
        errors = json.loads(body).get("errors", {})
    except ValueError:
        errors = {}
    check(code == 422 and "message" in errors, "заявка с лендинга проверяет комментарий",
          f"комментарий не проверяется: {code} {sorted(errors)}")

    # 3. кодом образа на базе в памяти. На старом коде (положительный контроль) этих модулей и полей нет —
    # это провал проверки, а не падение.
    try:
        from datetime import date
        from unittest import mock
        from urllib.parse import quote

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from starlette.requests import Request

        from app.api import web
        from app.core import plans, pricing
        from app.core.inn import inn_valid
        from app.db import queries as q
        from app.db.models import Base

        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(eng)
        S = sessionmaker(bind=eng, expire_on_commit=False)
        with S() as db:
            u = q.create_user(db, "check-test37", "x", role="user")
            q.update_profile(db, u.id, full_name="Проверка Выкатки", region="Курская область", telegram="",
                             consent=True, email="check@example.invalid", phone="+7 900 000-00-00",
                             org="ООО «Проверка»", inn="7707083893")
            q.set_user_plan(db, u.id, "Стандарт", plans.anchor_from_date(plans.msk_today()),
                            plans.anchor_from_date(date(2030, 1, 31)))
            u = q.get_user(db, u.id)
            db.expunge(u)

        def page(tab="plan", qs=""):
            qs = "&".join(f"{k}={quote(v)}" for k, v in (p.split("=", 1) for p in qs.split("&") if p))
            r = Request({"type": "http", "method": "GET", "path": "/profile", "headers": [],
                         "query_string": qs.encode(), "session": {}, "app": None})
            return web.profile_page(r, tab=tab).body.decode("utf-8")

        req = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"",
                       "session": {}, "app": None})
        with mock.patch.object(web, "get_session", S), mock.patch.object(web, "current_user", return_value=u):
            plan_tab, profile = page(), page("profile")
            renew = page(qs="renew=1")
            popup = page(qs="confirm=1&tariff=Профи&kind=corporate&period=year&seats=5")
            r = web.plan_request(req, tariff="Стандарт", kind="individual", period="month", seats="1",
                                 trial_flag="", consent="1", renew="1", message="")
        check(plan_tab.count(f"<b>{pricing.SOURCES}</b><small>{pricing.SOURCES_NOTE}</small>") == 6
              and "Гарант" not in plan_tab, "витрина: доступ к источникам — «Бесплатно» на каждой карточке",
              "источники на витрине не бесплатные")
        check(">Продлить</a>" in plan_tab and "до 28.02.2030" in " ".join(renew.split()),
              "«Продлить»: панель с новым сроком (месяц от конца текущего)", "продления нет или срок неверный")
        check('class="pop-back"' in popup and 'name="message"' in popup,
              "корпоративная карточка — попап «Связаться с нами» с комментарием", "попапа нет")
        with S() as db:
            leads_mem = q.list_leads(db)
        check(r.status_code == 303 and len(leads_mem) == 1 and "продление до 28.02.2030" in (leads_mem[0].options or ""),
              "заявка на продление создана с новым сроком", f"заявка на продление не сложилась: {r.status_code}")
        check('<fieldset class="fields-lock" disabled>' in profile and "не подтверждены" in profile,
              "профиль: просмотр по умолчанию, организация «не подтверждена»", "форма профиля не заблокирована")
        with S() as db:
            q.verify_org(db, u.id, 1)
            verified = q.get_user(db, u.id).org_verified_at is not None
            q.update_profile(db, u.id, full_name="Проверка Выкатки", region="Курская область", telegram="",
                             consent=True, org="ООО «Другая»")
            dropped = q.get_user(db, u.id).org_verified_at is None
        check(verified and dropped and inn_valid("7707083893") and not inn_valid("7707083894"),
              "подтверждение организации: ставит admin, правка снимает; ИНН — контрольные цифры",
              f"подтверждение: поставлено {verified}, снято правкой {dropped}")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"сценарии кодом образа не выполнились: {type(e).__name__}: {e}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
