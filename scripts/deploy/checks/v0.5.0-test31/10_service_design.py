"""test31: сервис в новом дизайне (макет «Navigator 719 Service», 08.10.2026).

ЗАЧЕМ. Релиз меняет весь фронт сервиса и добавляет две ручки поведения: текст источника в ответе
(Б1, панель «Источник») и `/api/quota` (Б4). Код ответа движка не меняется. Проверяется то, что
видит пользователь, — разметка, статика с версией, шрифт с диска, — и то, что фронт получит от API.

⚠ Ничего не пишет в боевую базу и не зовёт DeepSeek: текст источника собирается КОДОМ ОБРАЗА из
записи корпуса (`knowledge_base` лежит в образе целиком), API — только без сессии (401, не 404).
Положительный контроль — прогоном на `test30` (бой до выкатки), результат в профиле `test31`.
"""
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

BASE = os.environ.get("CHECK_BASE", "http://127.0.0.1:8000")


def http(path: str) -> tuple[int, str, str]:
    try:
        r = urllib.request.urlopen(BASE + path, timeout=20)
        return r.status, r.headers.get("content-type", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read().decode("utf-8", "replace")


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        print(f"   OK: {good}" if ok else f"   ❌ {fail}")
        bad += not ok

    code, _, _ = http("/ping")
    check(code == 200, "/ping отвечает", f"/ping → {code}")

    # 1. вход — экран макета, статика с версией по содержимому (иначе кэш браузера отдаст старое)
    code, _, page = http("/login")
    check(code == 200 and "Вход в Навигатор" in page, "вход — экран нового дизайна",
          f"вход старый или недоступен ({code})")
    m = re.search(r"/static/style\.css\?v=([0-9a-f]{10})", page)
    check(bool(m), "стили подключены с версией ?v=<хеш>", "у стилей нет версии — после выкатки браузер возьмёт старые")
    check("п. 7 Правил" in page and "территориальная палата" not in page,
          "пример на входе — дословно по п. 7 Правил", "пример на входе не совпадает с пунктом")

    # 2. стили и шрифт — с диска сервиса (RF-first), версия ведёт на тот же файл
    if m:
        code, _, css = http(f"/static/style.css?v={m.group(1)}")
        check(code == 200 and 'font-family: "Inter Tight"' in css and ".src-panel" in css,
              "стили нового дизайна отдаются", f"стили старые или недоступны ({code})")
    try:
        r = urllib.request.urlopen(BASE + "/static/fonts/inter-tight-cyrillic.woff2", timeout=20)
        size = len(r.read())
        check(r.status == 200 and size > 10_000, f"шрифт Inter Tight с диска ({size} байт)", "шрифт не отдаётся")
    except urllib.error.HTTPError as e:
        check(False, "", f"шрифт не отдаётся ({e.code})")

    # 3. фронт знает панель источника и расход тарифа
    code, _, js = http("/static/chat.js")
    check(code == 200 and "function openSource" in js and "function renderQuota" in js,
          "chat.js: панель источника и карточка тарифа", f"chat.js старый ({code})")

    # 4. /api/quota существует (без сессии — 401; на старом коде — 404)
    code, _, _ = http("/api/quota")
    check(code == 401, "/api/quota без входа → 401", f"/api/quota → {code} (ручки нет?)")

    # 5. источник ответа несёт текст — кодом образа на настоящей записи корпуса
    try:
        from app.api.chat import _sources_from_hits
        from app.rag.retriever import Hit

        root = Path(__file__).resolve()
        while not (root / "knowledge_base").is_dir() and root.parent != root:
            root = root.parent
        recs = json.loads((root / "knowledge_base/pp719/structured/III_specmashinostroenie.json")
                          .read_text(encoding="utf-8"))
        r = next(x for x in recs if x["product_name"].startswith("Бульдозеры на гусеничных"))
        hit = Hit(0.0, r["section_roman"], r.get("section_title", ""), r["product_name"],
                  r.get("okpd2_codes") or [], r.get("min_threshold"), r.get("requirement_blocks") or [],
                  r.get("source_anchor"), False, r)
        text = getattr(_sources_from_hits([hit])[0], "text", None) or ""
        check("балл." in text and "▸ несущая рама" in text, "у источника есть текст позиции для панели",
              "у источника нет текста — панель «Источник» будет пустой")
    except Exception as e:  # noqa: BLE001 — на старом коде это провал проверки, а не падение скрипта
        check(False, "", f"текст источника не собирается: {type(e).__name__}: {e}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
