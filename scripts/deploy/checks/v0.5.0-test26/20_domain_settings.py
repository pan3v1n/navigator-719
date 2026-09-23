"""`v0.5.0-test26` — домен и HTTPS: настройки, которые обязан был дописать оператор.

Скрипт выкатки останавливается, если в `.env` VM нет PUBLIC_DOMAIN, — ДО распаковки. Здесь, уже
внутри контейнера, проверяется то, что видит ПРИЛОЖЕНИЕ: тот же `.env` доехал в процесс через
`env_file`, значение в punycode (кириллицу Caddy не кодирует), и куки помечены Secure — иначе за
HTTPS они по-прежнему уходили бы по http при первом же заходе по голому адресу.

⚠ Значение домена ЗАХАРДКОЖЕНО — по той же причине, что и тег в соседней проверке: сверять с
переменной из профиля значило бы сверять профиль сам с собой. Домен один и куплен владельцем.
"""
from app.core.console import enable_utf8
from app.core.config import settings

enable_utf8()  # #107

DOMAIN = "xn--719--83dani8b8bqyy.xn--p1ai"   # 719-навигатор.рф
SUB = "xn--b1afk4ade"                        # сервис

print("   PUBLIC_DOMAIN:", settings.PUBLIC_DOMAIN, " APP_SUBDOMAIN:", settings.APP_SUBDOMAIN,
      " COOKIE_SECURE:", settings.COOKIE_SECURE)
assert settings.PUBLIC_DOMAIN == DOMAIN, f"PUBLIC_DOMAIN в контейнере «{settings.PUBLIC_DOMAIN}», ждали {DOMAIN}"
assert settings.APP_SUBDOMAIN == SUB, f"APP_SUBDOMAIN «{settings.APP_SUBDOMAIN}» — не punycode метки «сервис»"
assert settings.COOKIE_SECURE is True, "COOKIE_SECURE не включён — куки уйдут по http"
assert "xn--" in settings.PUBLIC_DOMAIN and not any(ord(c) > 127 for c in settings.PUBLIC_DOMAIN), \
    "домен не в punycode"
