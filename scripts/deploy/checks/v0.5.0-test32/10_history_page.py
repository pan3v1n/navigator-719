"""test32: страница «История запросов» (Б3, макет «Navigator 719 Service», 08.10.2026).

ЗАЧЕМ. Релиз добавляет ручку `/api/history` и страницу истории во фронте. Код ответа движка не
меняется. Проверяется, что ручка есть и закрыта входом, фронт знает страницу, а сборка истории
кодом ОБРАЗА группирует, ищет по ответам и цитирует только источники ответа.

⚠ Ничего не пишет в боевую базу и не зовёт DeepSeek: история собирается на синтетических репликах
в памяти, API — только без сессии (401, не 404). Положительный контроль — прогоном на `test31`.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

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

    code, _ = http("/api/history")
    check(code == 401, "/api/history без входа → 401", f"/api/history → {code} (ручки нет?)")

    code, js = http("/static/chat.js")
    check(code == 200 and "function showHistory" in js and "HISTORY_SIDE_N" in js,
          "chat.js: страница истории и короткий список в сайдбаре", f"chat.js старый ({code})")

    # сборка истории кодом образа — на синтетике в памяти
    try:
        from app.api.history import build_history

        class M:
            def __init__(self, sid, role, content, ts, src=None):
                self.session_id, self.role, self.content, self.ts = sid, role, content, ts
                self.sources_json = json.dumps(src or [], ensure_ascii=False)

        now = datetime.utcnow()
        src = [{"product_name": "Бульдозеры", "okpd2": ["28.92.21.110"], "kind": "product",
                "source_anchor": "Приложение к ПП №719, Раздел III, позиция 2"},
               {"product_name": "Чужая позиция", "okpd2": ["28.93.15"], "kind": "product",
                "source_anchor": "Приложение к ПП №719, Раздел VI, позиция 40"}]
        msgs = [M("a", "user", "требования к бульдозерам", now - timedelta(days=10)),
                M("a", "assistant", "нужна сварка рамы [1]", now - timedelta(days=10) + timedelta(seconds=5), src),
                M("b", "user", "кто выдаёт акт", now)]
        out = build_history(msgs, q="сварка", now=now)
        items = [i for g in out["groups"] for i in g["items"]]
        check(len(items) == 1 and items[0]["session_id"] == "a" and out["groups"][0]["label"] == "Ранее",
              "поиск находит беседу по тексту ОТВЕТА, группа по дате", f"поиск/группы неверны: {out}")
        check(items and items[0]["refs"] == ["Разд. III, поз. 2"],
              "в чипах — только процитированный источник", f"чипы: {items[0]['refs'] if items else None}")
    except Exception as e:  # noqa: BLE001 — на старом коде это провал проверки, а не падение скрипта
        check(False, "", f"история не собирается: {type(e).__name__}: {e}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
