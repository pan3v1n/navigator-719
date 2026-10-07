"""test28: форма заявки лендинга и закрытая схема API — на боевом процессе приложения.

ЗАЧЕМ. Релиз добавляет ПУБЛИЧНЫЙ эндпоинт, собирающий ПДн (`POST /api/leads`), и закрывает схему
API (`O9` #145). Обе вещи легко «выкатить» так, что они не работают: старый образ без маршрута
отдаст 404 на форму, а приложение без `APP_ENV=production` оставит `/docs` открытым. Проверяется
ЖИВОЙ процесс по петле (`127.0.0.1:8000`), а не файлы пакета. Маршрут через caddy (корневой домен)
проверяет сам `deploy.sh` — POST пустой формы, ожидается 422.

⚠ Проверка НИЧЕГО не пишет в боевую базу: форма отправляется пустой, проверки её отклоняют.
Положительный контроль — прогоном на `test27` (бой до выкатки), результат в профиле `test28`.
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект — это ответ, который проверяется (`/admin` → `/login`), а не повод идти дальше."""

    def redirect_request(self, *a, **k):
        return None


def http(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        r = urllib.request.build_opener(_NoRedirect()).open(req, timeout=20)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        print(f"   OK: {good}" if ok else f"   ❌ {fail}")
        bad += not ok

    # 1. схема API закрыта (O9 #145) — только на бою, где APP_ENV не development
    code_ping, _ = http("GET", "/ping")
    check(code_ping == 200, "/ping отвечает", f"/ping → {code_ping}")
    if settings.APP_ENV != "development":
        codes = {p: http("GET", p)[0] for p in ("/docs", "/redoc", "/openapi.json")}
        check(all(c == 404 for c in codes.values()), "схема API закрыта: /docs, /redoc, /openapi.json → 404",
              f"схема API открыта: {codes}")
    else:
        print("   ⚠ APP_ENV=development — закрытие схемы API здесь не проверяется (это не бой)")

    # 2. приём заявки: пустая форма отклоняется проверками, ничего не пишется
    code, text = http("POST", "/api/leads", {})
    errors = {}
    try:
        errors = json.loads(text).get("errors") or {}
    except ValueError:
        pass
    check(code == 422 and "consent" in errors and "inn" in errors,
          "POST /api/leads: пустая форма → 422 с ошибками полей (в базу не пишется)",
          f"форма заявки не работает: {code} {text[:120]!r}")

    # 3. таблица заявок создана на старте (init_db)
    db_path = settings.APP_DB_URL.split("sqlite:///", 1)[-1]
    try:
        with sqlite3.connect(db_path) as c:
            has = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads'").fetchone()
    except sqlite3.Error as e:
        has = None
        print(f"   ⚠ база не открылась: {e}")
    check(bool(has), "в боевой базе есть таблица leads", "таблицы leads нет — приложение не перезапущено?")

    # 4. политика ПДн описывает заявки и не врёт про http
    code, page = http("GET", "/privacy")
    check(code == 200 and "Заявки с сайта 719-навигатор.рф" in page and "не защищено сертификатом" not in page,
          "политика: раздел о заявках есть, неправды про http нет",
          f"политика старая или недоступна ({code})")

    # 5. заявки (ПДн) не видны без входа
    code, _ = http("GET", "/admin")
    check(code in (302, 303, 307), "/admin без входа → редирект на вход", f"/admin без входа → {code}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
