"""`v0.5.0-test25` — пометка версии на фронте: критерий приёмки запроса владельца.

ЧТО ПРОВЕРЯЕМ И ПОЧЕМУ ИМЕННО ЭТО.

`/ping` уже сверяется скриптом выкатки (`release` обязан совпасть с тегом профиля) — и это
проверка СБОРКИ: файл `RELEASE` лежит на диске всегда, а в живой процесс попадает только через
образ. Здесь проверяется другое: что метка доехала до СТРАНИЦЫ, то есть до того, ради чего
задача и ставилась. Две разные вещи, и одна не заменяет другую.

⚠⚠ РЕНДЕРОМ, А НЕ ГРЕПОМ ПО ШАБЛОНУ. Метка живёт в двух местах по устройству: общий блок
`release_marker` в `base.html` — на семи страницах, и строка внутри дисклеймера под полем ввода —
в чате, который общий блок ГАСИТ. Греп по одному файлу не увидит ни отсутствия на чате, ни
задвоения на прочих. Считаем НОСИТЕЛИ: ноль — метки нет там, где обещана; два — две надписи об
одном, и они разъедутся при первой правке.

⚠ Тег здесь ЗАХАРДКОЖЕН намеренно. Каталог проверок — свой на каждый релиз, и сверять метку с
переменной, приходящей из того же профиля, значило бы сверять профиль сам с собой. Захардкоженный
тег ловит и подмену профиля, и невыполненную запись файла `RELEASE`.
"""
from app.core.console import enable_utf8
from app.core.release import UNTAGGED, release_label

enable_utf8()  # #107: проверка печатает значки вне cp1251

TAG = "v0.5.0-test25"

label = release_label()
print("   метка релиза:", label)
assert label == TAG, f"метка «{label}» не совпала с тегом релиза {TAG}"
assert UNTAGGED not in label, "метка говорит «тег не задан» — файл RELEASE не доехал в образ"

from app.api.web import templates  # noqa: E402 — импорт после проверки метки, он тяжелее

USER = {"login": "e", "username": "e", "role": "admin", "full_name": "Э", "id": 1,
        "region": "Курская область", "email": "", "phone": "", "org": "", "position": "",
        "consent": True}
CTX = dict(request=None, app_title="Навигатор ПП РФ №719", org="Курская ТПП",
           corpus_edition="ред.", release_label=label)
PAGES = (("login.html", {"error": None}),
         ("profile.html", {"user": USER, "error": None, "saved": False, "regions": []}),
         ("terms.html", {}),
         ("privacy.html", {}),
         ("chat.html", {"user": USER, "kontur_719_url": "#", "input_hint": ""}))

for name, extra in PAGES:
    html = templates.get_template(name).render(**CTX, **extra)
    carriers = html.count('"release-marker"') + html.count('"composer-version"')
    assert carriers == 1, f"{name}: носителей метки {carriers}, ожидался ровно один"
    assert label in html, f"{name}: метка не напечаталась в теле страницы"
    print(f"   OK {name:<14} носителей 1, метка на месте")

print(f"   OK: пометка версии «{TAG}» на всех {len(PAGES)} проверенных страницах, ровно по одной")
