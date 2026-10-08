"""test33: правки по замечаниям владельца на бою test32 — подменю «Справка» и «Текст постановления».

ЗАЧЕМ. «Справка» открывала подменю ПОД чатом (сайдбар обрезал его `overflow: hidden`), а «Текст
постановления» у владельца открылся в той же вкладке при `target="_blank"`. Обе правки — во фронте:
проверяется, что новые стили и скрипт отдаются именно с этого сервера. Код ответа движка не менялся.

⚠ Только чтение по HTTP. Положительный контроль — прогоном на `test32` (бой до выкатки).
"""
import os
import re
import urllib.error
import urllib.request

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

    code, css = http("/static/style.css")
    side = re.search(r"\n\.sidebar \{[^}]*\}", css or "")
    check(code == 200 and side is not None and "overflow: hidden" not in side.group(0)
          and "z-index" in side.group(0),
          "сайдбар не обрезает подменю и стоит слоем над чатом",
          f"сайдбар по-прежнему обрезает подменю ({code})")
    check(".nav-group .nav-menu, .app.collapsed .nav-menu { left: 0; right: 0; bottom: calc(100% + 4px)" in (css or ""),
          "на телефоне подменю открывается внутри шторки", "мобильное подменю уходит за край экрана")

    code, js = http("/static/chat.js")
    check(code == 200 and 'window.open(a.href, "_blank")' in js,
          "ссылки «в новой вкладке» открываются явно", f"chat.js старый ({code})")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
