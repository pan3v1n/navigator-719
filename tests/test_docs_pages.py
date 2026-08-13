"""Правовые и справочные страницы + дисклеймеры интерфейса (офлайн, без Qdrant и DeepSeek).

Почему это тестируется. Политику конфиденциальности читают ДО того, как дать согласие в профиле,
поэтому она обязана открываться без входа — спрятанная за авторизацией, она бессмысленна. А пометка
о происхождении ответа обязана уезжать вместе с ответом: копирование выносит его за пределы сервиса
ровно так же, как экспорт (правило R3).

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WEB = ROOT / "app" / "web"


def _client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


class TestPublicDocPages(unittest.TestCase):
    """Страницы открыты без входа: их читают до согласия и до получения доступа."""

    @classmethod
    def setUpClass(cls):
        cls.c = _client()

    def test_pages_open_without_auth(self):
        for url in ("/terms", "/privacy", "/help"):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("<h1", r.text, url)

    def test_terms_state_expert_decides(self):
        """Главное правило продукта должно быть в условиях, а не только в интерфейсе."""
        t = self.c.get("/terms").text
        self.assertIn("эксперт", t.lower())
        self.assertIn("предварительн", t.lower())

    def test_privacy_names_operator_law_and_llm(self):
        """Три вещи, без которых политика — фикция: кто оператор, на каком основании, кому уходит текст."""
        t = self.c.get("/privacy").text
        self.assertIn("152-ФЗ", t)
        self.assertIn("DeepSeek", t)
        self.assertIn("соглас", t.lower())

    def test_help_has_faq_entries(self):
        t = self.c.get("/help").text
        self.assertGreaterEqual(t.count("<details"), 8, "справочный центр почти пуст")

    def test_pages_cross_link_each_other(self):
        for url in ("/terms", "/privacy", "/help"):
            t = self.c.get(url).text
            for link in ("/terms", "/privacy", "/help"):
                self.assertIn(f'href="{link}"', t, f"{url} не ссылается на {link}")


class TestChatDisclaimers(unittest.TestCase):
    """Дисклеймеры чата: постоянный под вводом и короткий над вводом при начатом диалоге."""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "templates" / "chat.html").read_text(encoding="utf-8")
        cls.css = (WEB / "static" / "style.css").read_text(encoding="utf-8")

    def test_composer_disclaimer_links_to_terms_and_privacy(self):
        self.assertIn('href="/terms"', self.html)
        self.assertIn('href="/privacy"', self.html)
        self.assertIn('href="/help"', self.html)  # «Подробнее»

    def test_notice_above_input_exists_and_is_shown_only_in_started_chat(self):
        self.assertIn('id="composer-notice"', self.html)
        self.assertIn("может ошибаться", self.html)
        # на пустом экране роль дисклеймера играет hero — строка не должна дублировать его
        self.assertIn(".main:not(.empty) .composer-notice", self.css)

    def test_corpus_edition_still_visible(self):
        """E1: редакция корпуса не должна пропасть из интерфейса при переверстке дисклеймера."""
        self.assertIn("corpus_edition", self.html)

    def test_sidebar_help_menu_has_three_entries(self):
        for link in ("/help", "/terms", "/privacy"):
            self.assertIn(f'href="{link}"', self.html)
        self.assertIn('id="help-toggle"', self.html)
        # подменю обязано открываться не только по hover — иначе недоступно с тача и клавиатуры
        self.assertIn(".nav-group:focus-within .nav-menu", self.css)
        self.assertIn(".nav-group.open .nav-menu", self.css)


class TestAnswerTools(unittest.TestCase):
    """Кнопки «копировать» и «поделиться» под ответом."""

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "static" / "chat.js").read_text(encoding="utf-8")
        cls.css = (WEB / "static" / "style.css").read_text(encoding="utf-8")

    def test_tools_added_in_both_render_paths(self):
        """Ответ приходит двумя путями — стримингом и одним куском; кнопки нужны в обоих."""
        self.assertEqual(self.js.count("addAnswerTools("), 3)  # объявление + два вызова

    def test_copied_text_carries_origin_note(self):
        """R3: ответ, покинувший сервис, несёт пометку — иначе получатель примет черновик за вердикт."""
        self.assertIn("SHARE_NOTE", self.js)
        self.assertIn("уполномоченный эксперт ТПП", self.js)

    def test_copy_works_without_secure_context(self):
        """Пилот работает по http, где clipboard API недоступен — нужен запасной путь."""
        self.assertIn("isSecureContext", self.js)
        self.assertIn("execCommand", self.js)

    def test_tooltip_on_hover_and_focus(self):
        self.assertIn(".tool-btn::after", self.css)
        self.assertIn("content: attr(data-tip)", self.css)
        self.assertIn("dataset.tip", self.js)          # подпись задаётся из JS
        self.assertIn(".tool-btn:focus-visible::after", self.css)  # и доступна с клавиатуры


if __name__ == "__main__":
    unittest.main()
