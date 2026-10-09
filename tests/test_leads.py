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
        r = self.client().post("/api/leads", json={**GOOD, leads_api.HONEYPOT: "http://spam"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.rows(), [], "заявка бота попала в базу")

    def test_autofilled_website_field_does_not_drop_a_real_lead(self):
        """Ревью PR #158: ловушка «website» заполнялась автозаполнением браузера, и настоящая заявка
        пропадала молча. Поле с таким именем больше ничего не значит."""
        r = self.client().post("/api/leads", json={**GOOD, "website": "https://zavod.ru"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.rows()), 1)
        self.assertNotIn(leads_api.HONEYPOT.lower(), ("website", "url", "site", "company", "homepage"))

    def test_rate_limit_per_address(self):
        c = self.client()
        for _ in range(leads_api.LEADS_PER_HOUR):
            self.assertEqual(c.post("/api/leads", json=GOOD).status_code, 200)
        r = c.post("/api/leads", json=GOOD)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(len(self.rows()), leads_api.LEADS_PER_HOUR)

    def test_rejected_attempts_do_not_burn_the_quota(self):
        """Ревью PR #158: квота списывалась ДО проверок — посетитель, поправлявший форму, и проверка
        выкатки (пустая форма) съедали лимит, и верная заявка получала 429."""
        c = self.client()
        for _ in range(leads_api.LEADS_PER_HOUR * 2):
            self.assertEqual(c.post("/api/leads", json={}).status_code, 422)
        self.assertEqual(c.post("/api/leads", json=GOOD).status_code, 200)

    def test_lengths_are_bounded_like_the_columns(self):
        r = self.client().post("/api/leads", json={**GOOD, "phone": "+7" + " " * 500 + "9123456789",
                                                   "name": "Я" * 201})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["errors"]["name"], "Слишком длинное имя")
        self.assertIn("phone", r.json()["errors"])

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


class TestLeadRetention(unittest.TestCase):
    """Политика обещает «не дольше 12 месяцев» — обещание исполняет код, а не память оператора."""

    def setUp(self):
        from datetime import datetime, timedelta

        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.now = datetime(2027, 10, 7)
        with self.Session() as db:
            for days in (400, 366, 364, 1):
                db.add(Lead(created_at=self.now - timedelta(days=days), tariff="Старт", name="x", org="y",
                            inn="4632000000", email="a@b.ru", phone="+7", consent_at=self.now))
            db.commit()

    def test_purge_removes_only_expired(self):
        with self.Session() as db:
            self.assertEqual(q.purge_old_leads(db, now=self.now), 2)
            self.assertEqual(q.count_leads(db), 2)

    def test_policy_and_code_name_the_same_term(self):
        privacy = (ROOT / "app" / "web" / "templates" / "privacy.html").read_text(encoding="utf-8")
        self.assertIn("не дольше 12 месяцев", privacy)
        self.assertEqual(q.LEAD_RETENTION_DAYS, 365, "срок в коде разошёлся с политикой")

    def test_purge_runs_on_new_lead_and_on_admin(self):
        import ast

        # админ-панель пересобрана 09.10.2026: заявки — свой раздел /admin/leads
        for rel, fn in (("app/api/leads.py", "submit_lead"), ("app/api/admin.py", "_leads_response")):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn)
            calls = {c.attr for c in ast.walk(node) if isinstance(c, ast.Attribute)}
            self.assertIn("purge_old_leads", calls, f"{fn} не удаляет просроченные заявки")

    def test_single_lead_can_be_deleted_only_by_admin(self):
        from fastapi.testclient import TestClient

        from main import app

        r = TestClient(app).post("/api/admin/leads/1/delete", follow_redirects=False)
        self.assertIn(r.status_code, (401, 403), "удаление заявки доступно без входа")
        with self.Session() as db:
            first = q.list_leads(db)[0]
            self.assertTrue(q.delete_lead(db, first.id))
            self.assertFalse(q.delete_lead(db, first.id))

class TestLeadsInAdminOnly(unittest.TestCase):
    """Заявки читает только раздел `/admin/leads` (за ролью admin); выгрузка скоркарда их не несёт."""

    def test_list_leads_is_called_only_from_the_admin_page(self):
        import ast

        callers = []
        for rel in ("app/api/web.py", "app/api/admin.py", "app/api/admin_stats.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            callers += [n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)
                        and any(isinstance(c, ast.Attribute) and c.attr == "list_leads" for c in ast.walk(n))]
        self.assertEqual(callers, ["_leads_response"])   # раздел «Заявки» и ре-рендер его ошибки


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

    def test_no_placeholder_prices(self):
        self.assertNotIn("0 000 ₽", self.text)

    def test_tab_title_is_short(self):
        # решение владельца 07.10: во вкладке браузера — только имя сервиса, без «ИИ-помощник
        # Курской ТПП» (текст на странице и описание для поисковиков не тронуты)
        self.assertEqual(re.findall(r"<title>([^<]*)</title>", self.html), ["Навигатор ПП 719"])

    def test_plan_volumes_from_design(self):
        # Объёмы тарифов — из макета, решение владельца 07.10. Обещание лендинга и лимит, который
        # исполняет сервис (#160), — две таблицы про одно: держим их равными.
        from app.core.plans import PLAN_LIMITS

        # «в месяц» — период лимита (#160: месяц от даты подключения), решение владельца 07.10
        vols = re.findall(r'class="plan-name">([^<]+)</div>.*?class="plan-desc">До (\d+) запросов в месяц ',
                          self.html, re.S)
        self.assertEqual(vols, [("Старт", "100"), ("Стандарт", "200"), ("Профи", "400")])
        self.assertEqual({name: int(n) for name, n in vols}, PLAN_LIMITS)

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
