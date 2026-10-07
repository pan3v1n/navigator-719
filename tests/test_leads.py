"""Заявки с лендинга (`POST /api/leads`) и сторожа самого лендинга — офлайн, без Qdrant и DeepSeek.

Зачем. Форма собирает ПДн (имя, ИНН, email, телефон). В макете она ничего не отправляла и
показывала «заявка отправлена» — на бою это обман пользователя. Здесь закреплено: без согласия
заявки нет, проверки сервера не доверяют браузеру, бот-ловушка не пишет в базу, частота ограничена.

Лендинг — статика, но в нём лежат ФАКТЫ о норме (редакция, ссылки на пункты). Макет ссылался на
«Правила выдачи заключения», утратившие силу 29.06.2024: сторожа ниже не дают такому вернуться.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import leads as leads_api  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base, Lead  # noqa: E402

LANDING = ROOT / "landing"
GOOD = {"tariff": "Стандарт", "name": "Иван Иванов", "org": "ООО «Предприятие»", "inn": "4632000000",
        "email": "ivan@example.ru", "phone": "+7 (912) 345-67-89", "promo": "", "consent": True}


class _Db(unittest.TestCase):
    def setUp(self):
        # StaticPool: один соединитель на всё — эндпоинт работает в threadpool, а «:memory:» иначе
        # у каждого соединения своя пустая база.
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        p = mock.patch.object(leads_api, "get_session", self.Session)
        p.start(); self.addCleanup(p.stop)
        leads_api._lead_limit.reset()

    def client(self):
        from fastapi.testclient import TestClient

        from main import app

        return TestClient(app)

    def rows(self):
        with self.Session() as db:
            return q.list_leads(db)


class TestLeadEndpoint(_Db):
    def test_valid_lead_is_stored_with_consent_time(self):
        r = self.client().post("/api/leads", json=GOOD)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True})
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].name, rows[0].inn, rows[0].tariff), ("Иван Иванов", "4632000000", "Стандарт"))
        self.assertIsNotNone(rows[0].consent_at)
        self.assertIsNone(rows[0].promo, "пустой промокод хранится как NULL")

    def test_no_consent_no_lead(self):
        r = self.client().post("/api/leads", json={**GOOD, "consent": False})
        self.assertEqual(r.status_code, 422)
        self.assertIn("consent", r.json()["errors"])
        self.assertEqual(self.rows(), [])

    def test_server_does_not_trust_the_browser(self):
        bad = {**GOOD, "inn": "123", "email": "не-почта", "phone": "+7 912", "name": " ", "tariff": "VIP"}
        r = self.client().post("/api/leads", json=bad)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(set(r.json()["errors"]), {"inn", "email", "phone", "name", "tariff"})
        self.assertEqual(self.rows(), [])

    def test_twelve_digit_inn_of_an_entrepreneur_is_accepted(self):
        r = self.client().post("/api/leads", json={**GOOD, "inn": "463200000012"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_honeypot_answers_ok_but_stores_nothing(self):
        r = self.client().post("/api/leads", json={**GOOD, "website": "http://spam"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.rows(), [], "заявка бота попала в базу")

    def test_rate_limit_per_address(self):
        c = self.client()
        for _ in range(leads_api.LEADS_PER_HOUR):
            self.assertEqual(c.post("/api/leads", json=GOOD).status_code, 200)
        r = c.post("/api/leads", json=GOOD)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(len(self.rows()), leads_api.LEADS_PER_HOUR)

    def test_no_personal_data_in_the_log(self):
        from loguru import logger

        seen: list[str] = []
        sink = logger.add(lambda m: seen.append(str(m)), level="INFO")
        try:
            self.client().post("/api/leads", json=GOOD)
        finally:
            logger.remove(sink)
        joined = "\n".join(seen)
        self.assertIn("сохранена заявка", joined, "лог молчит — тест не видит, что проверяет")
        for pd in (GOOD["email"], GOOD["inn"], "Иванов", "912"):
            self.assertNotIn(pd, joined, f"в журнал попали ПДн: {pd}")


class TestLeadsInAdminOnly(unittest.TestCase):
    """Заявки читает только страница `/admin` (за ролью admin); выгрузка скоркарда их не несёт."""

    def test_list_leads_is_called_only_from_the_admin_page(self):
        import ast

        src = (ROOT / "app" / "api" / "web.py").read_text(encoding="utf-8")
        callers = [n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)
                   and any(isinstance(c, ast.Attribute) and c.attr == "list_leads" for c in ast.walk(n))]
        self.assertEqual(callers, ["admin_page"])


class TestLandingGuards(unittest.TestCase):
    def setUp(self):
        self.html = (LANDING / "index.html").read_text(encoding="utf-8")
        self.js = (LANDING / "app.js").read_text(encoding="utf-8")
        # ⚠ Проверяется то, что ВИДИТ пользователь, а не комментарий, объясняющий исправление
        # (урок 18.08: сторож поймал имя удалённой константы в комментарии и остановил верный код).
        js_code = re.sub(r"/\*.*?\*/", "", self.js, flags=re.S)
        js_code = re.sub(r"(?m)^\s*//.*$", "", js_code)
        html_code = re.sub(r"<!--.*?-->", "", self.html, flags=re.S)
        self.text = html_code + js_code

    def test_edition_on_the_landing_is_the_corpus_edition(self):
        """Лендинг называет редакцию в трёх местах; после актуализации корпуса он обязан ехать с ней."""
        from app.rag import edition

        edition.corpus_edition.cache_clear()
        ed = edition.corpus_edition().replace(" N ", " № ")      # «ред. от 29.09.2026 № 1254»
        self.assertIn(ed, self.html, "в подвале демо-чата не та редакция, что у корпуса")
        date = re.search(r"\d{2}\.\d{2}\.\d{4}", ed).group(0)
        for m in re.findall(r"ред\. от (\d{2}\.\d{2}\.\d{4})", self.html):
            self.assertEqual(m, date, "на лендинге упомянута другая редакция")

    def test_no_dead_norms_or_phantom_documents(self):
        for bad in ("Правила выдачи заключения", "ПРАВИЛА ВЫДАЧИ ЗАКЛЮЧЕНИЯ", "заключения о подтверждении",
                    "получения заключения", "разд. II"):
            self.assertNotIn(bad, self.text, f"на лендинге «{bad}» — утратившая силу норма или неверный раздел")

    def test_no_placeholder_prices_or_quotas(self):
        self.assertNotIn("0 000 ₽", self.text)
        self.assertNotRegex(self.text, r"[Дд]о \d+ запросов", "лимитов запросов в сервисе нет")

    def test_nothing_loads_from_outside(self):
        """Шрифты и скрипты — только свои: внешние CDN из РФ грузятся ненадёжно."""
        for m in re.finditer(r'(?:src|href)="(https?:)?//([^"/]+)', self.html):
            host = m.group(2)
            self.assertTrue(host.startswith("сервис."), f"внешний ресурс: {host}")
        css = (LANDING / "styles.css").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"url\(\s*[\"']?https?:", "шрифт или картинка с внешнего адреса")
        for f in re.findall(r'url\("(fonts/[^"]+)"\)', css):
            self.assertTrue((LANDING / f).exists(), f"нет файла шрифта {f}")

    def test_form_posts_where_caddy_proxies(self):
        self.assertIn("fetch('/api/leads'", self.js)
        caddy = (ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        # Режем по СТРОКЕ объявления сайтов, а не по первому упоминанию: шапка-комментарий тоже
        # называет оба имени.
        root_site = re.split(r"(?m)^\{\$APP_SUBDOMAIN\}\.\{\$PUBLIC_DOMAIN\} \{", caddy, 1)[0]
        root_site = re.split(r"(?m)^\{\$PUBLIC_DOMAIN\} \{", root_site, 1)[1]
        i, j = root_site.find("handle /api/leads"), root_site.find("root * /srv/landing")
        self.assertGreater(i, 0, "корневой домен не проксирует /api/leads в приложение")
        self.assertLess(i, j, "проксирование заявок обязано стоять до раздачи статики")
        self.assertIn("reverse_proxy app:8000", root_site[i:j])

    def test_consent_links_to_the_privacy_policy(self):
        self.assertRegex(self.html, r'id="lnk-privacy-form" href="https://сервис\.[^"]+/privacy"')
        privacy = (ROOT / "app" / "web" / "templates" / "privacy.html").read_text(encoding="utf-8")
        self.assertIn("Заявки с сайта 719-навигатор.рф", privacy, "политика не описывает обработку заявок")
        self.assertNotIn("не защищено сертификатом", privacy, "в политике осталась неправда про http")


if __name__ == "__main__":
    unittest.main()
