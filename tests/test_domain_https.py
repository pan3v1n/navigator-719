"""Домен + HTTPS (22.09.2026): caddy перед приложением, лендинг на корне, сервис на `app.`.

Что закрепляем и почему — каждый сторож проверен мутацией (правка отменена → тест красный):
* флаг `Secure` у ОБЕИХ кук берётся из COOKIE_SECURE — вторая кука («запомнить меня») ставится
  своим кодом в auth.py и молча осталась бы без флага, если бы менялся только SessionMiddleware;
* приложение больше не публикует порт наружу: снаружи слушает только caddy, иначе домен и HTTPS
  обходятся голым `http://IP:8000`;
* Caddyfile держит оба имени (метка поддомена — из окружения, у владельца «сервис») и не
  буферизует стриминг;
* лендинг ведёт на поддомен «сервис.», а не на голый адрес;
* скрипт выкатки проверяет приложение по ПЕТЛЕ (порт 8000), а не через прокси — через
  `http://127.0.0.1` без порта у caddy нет сайта, и «/ping не ответил» ударил бы по верному коду.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestCookieSecureFlag(unittest.TestCase):
    def _remember_cookie(self, secure: bool):
        from unittest.mock import patch
        from fastapi import Response
        from app.api import auth
        resp = Response()
        with patch.object(auth.settings, "COOKIE_SECURE", secure):
            auth.set_remember_cookie(resp, 7)
        return resp.headers["set-cookie"]

    def test_remember_cookie_follows_setting(self):
        self.assertIn("Secure", self._remember_cookie(True))
        self.assertNotIn("Secure", self._remember_cookie(False))
        # HttpOnly и SameSite не потерялись при добавлении флага
        self.assertIn("HttpOnly", self._remember_cookie(True))

    def test_session_middleware_reads_the_same_setting(self):
        src = _read("main.py")
        self.assertIn("https_only=settings.COOKIE_SECURE", src)
        self.assertNotIn("https_only=False", src)

    def test_setting_documented(self):
        from app.core.config import Settings
        self.assertIn("COOKIE_SECURE", Settings.model_fields)
        self.assertIn("PUBLIC_DOMAIN", Settings.model_fields)
        self.assertEqual(Settings.model_fields["APP_SUBDOMAIN"].default, "xn--b1afk4ade",
                         "punycode метки «сервис» — кириллицу Caddy не кодирует")
        self.assertFalse(Settings.model_fields["COOKIE_SECURE"].default,
                         "локальный дефолт — без флага: иначе вход по http не работает у разработчика")


class TestComposeTopology(unittest.TestCase):
    def setUp(self):
        self.src = _read("docker-compose.yml")

    def test_app_port_only_on_loopback(self):
        app_block = self.src[self.src.index("  app:"):self.src.index("volumes:\n  qdrant_storage")]
        published = re.findall(r'-\s*"([^"]+)"', app_block[app_block.index("ports:"):])
        self.assertEqual(published, ["127.0.0.1:8000:8000"], published)

    def test_caddy_listens_outside(self):
        self.assertIn("  caddy:", self.src)
        caddy_block = self.src[self.src.index("  caddy:"):self.src.index("  app:")]
        for port in ('"80:80"', '"443:443"'):
            self.assertIn(port, caddy_block)
        self.assertIn("./caddy/Caddyfile:/etc/caddy/Caddyfile", caddy_block)
        self.assertIn("./landing:/srv/landing", caddy_block)
        self.assertIn("caddy_data:/data", caddy_block, "без тома ключи ACME теряются при пересоздании")

    def test_domain_comes_from_env_with_local_fallback(self):
        self.assertIn("${PUBLIC_DOMAIN:-localhost}", self.src)
        self.assertIn("${APP_SUBDOMAIN:-xn--b1afk4ade}", self.src)


class TestCaddyfile(unittest.TestCase):
    def setUp(self):
        self.src = _read("caddy/Caddyfile")

    def test_two_sites(self):
        self.assertRegex(self.src, r"(?m)^\{\$PUBLIC_DOMAIN\} \{", "корень → лендинг")
        self.assertRegex(self.src, r"(?m)^\{\$APP_SUBDOMAIN\}\.\{\$PUBLIC_DOMAIN\} \{",
                         "поддомен из окружения → сервис")
        self.assertIn("root * /srv/landing", self.src)
        self.assertIn("reverse_proxy app:8000", self.src)

    def test_streaming_not_buffered(self):
        self.assertIn("flush_interval -1", self.src)

    def test_no_hardcoded_domain_or_ip(self):
        body = self.src.split("\n\n", 1)[1]
        self.assertNotIn("xn--", body, "домен — из окружения, не в конфиге")
        self.assertNotIn("сервис", body, "метка — из окружения, не в конфиге")
        self.assertEqual(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", self.src), [])


class TestLanding(unittest.TestCase):
    def setUp(self):
        self.src = _read("landing/index.html")

    def test_button_leads_to_app_subdomain(self):
        m = re.search(r'id="open-app" href="([^"]+)"', self.src)
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).startswith("https://сервис."), m.group(1))
        # и скрипт строит адрес от имени лендинга, чтобы страница не зависела от punycode/кириллицы
        self.assertIn("var SUB = 'сервис'", self.src)
        self.assertIn("'//' + SUB + '.' + h", self.src)

    def test_disclaimer_present(self):
        self.assertIn("эксперт ТПП", self.src)

    def test_legal_links_point_to_app(self):
        for path in ("/help", "/terms", "/privacy"):
            self.assertRegex(self.src, r'href="https://сервис\.[^"]+' + path + '"')


class TestDeployChecksGoThroughLoopbackPort(unittest.TestCase):
    def test_ping_and_pages_use_port_8000(self):
        src = _read("scripts/deploy/deploy.sh")
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        self.assertIn("http://127.0.0.1:8000/ping", code)
        self.assertIn('"http://127.0.0.1:8000$p"', code)
        self.assertNotIn("http://127.0.0.1/", code)
        self.assertIn("compose up -d app caddy", code)

    def test_uvicorn_trusts_proxy_headers(self):
        src = _read("Dockerfile")
        self.assertIn("--proxy-headers", src)
        self.assertIn("--forwarded-allow-ips", src)

    def test_app_image_excludes_proxy_files(self):
        src = _read(".dockerignore")
        self.assertIn("\ncaddy\n", src)
        self.assertIn("\nlanding\n", src)
