"""Пометка версии во фронте: что ИМЕННО крутится на этой машине. Офлайн.

ЗАЧЕМ. `APP_VERSION` держится на `0.5.0` до приёмки эксперта ≥ 70 % — это гейт версии `1.0`, и он
намеренно не двигается. За август-сентябрь 2026 под одним и тем же «0.5.0» на бой уехало полтора
десятка сборок от `test10` до `test24`, и вопрос «что сейчас на сервере» по этому числу не решался.
Отвечает на него ТЕГ РЕЛИЗА, который пишет сама выкатка.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import release  # noqa: E402
from app.core.config import settings  # noqa: E402


class TestReleaseLabel(unittest.TestCase):
    def setUp(self):
        release.release_label.cache_clear()
        self.addCleanup(release.release_label.cache_clear)

    def test_env_override_wins(self):
        with unittest.mock.patch.object(settings, "RELEASE_TAG", "v0.5.0-test99"):
            self.assertEqual(release.release_label(), "v0.5.0-test99")

    def test_falls_back_to_the_file_written_by_the_deploy(self):
        with unittest.mock.patch.object(settings, "RELEASE_TAG", ""), \
             unittest.mock.patch.object(Path, "read_text",
                                        lambda *a, **k: "v0.5.0-test24\n"):
            self.assertEqual(release.release_label(), "v0.5.0-test24")

    def test_unknown_tag_is_said_out_loud_and_not_invented(self):
        """⚠⚠ ТРЕТИЙ ИСХОД. Пометка, назвавшая версию наугад, хуже отсутствующей: её читают как
        факт. Не зная тега, говорим ровно то, что знаем."""
        def _boom(*a, **k):
            raise OSError("нет файла")

        with unittest.mock.patch.object(settings, "RELEASE_TAG", ""), \
             unittest.mock.patch.object(Path, "read_text", _boom):
            label = release.release_label()
        self.assertIn(release.UNTAGGED, label)
        self.assertIn(settings.APP_VERSION, label)
        self.assertNotIn("test", label, "в метке появился тег, которого никто не сообщал")

    def test_unreadable_file_never_raises(self):
        """⚠⚠ НАХОДКА РЕВЬЮ 08.09.2026. `/ping` — это HEALTHCHECK контейнера (Dockerfile), и метка
        зовётся оттуда же, откуда рендерится каждая страница (`_ctx`). Первая редакция ловила
        `OSError` и `IndexError`, мимо `UnicodeDecodeError` (это `ValueError`): файл с не-UTF-8
        байтами — оборванная запись, правка в чужой кодировке — ронял бы КАЖДЫЙ рендер и
        healthcheck, то есть пометка версии переводила бы исправный сервис в «unhealthy».
        `lru_cache` исключения не кэширует, так что падало бы на каждом запросе.
        """
        for exc in (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
                    PermissionError("нет прав"),
                    IsADirectoryError("это каталог")):
            with self.subTest(exc=type(exc).__name__):
                release.release_label.cache_clear()

                def _raise(*a, _e=exc, **k):
                    raise _e

                with unittest.mock.patch.object(settings, "RELEASE_TAG", ""), \
                     unittest.mock.patch.object(Path, "read_text", _raise):
                    self.assertIn(release.UNTAGGED, release.release_label())

    def test_blank_file_is_treated_as_unknown(self):
        """Пустой `RELEASE` (обрыв записи) — это «не знаю», а не пустая метка."""
        with unittest.mock.patch.object(settings, "RELEASE_TAG", ""), \
             unittest.mock.patch.object(Path, "read_text", lambda *a, **k: "\n"):
            self.assertIn(release.UNTAGGED, release.release_label())


class TestLabelReachesTheUser(unittest.TestCase):
    """Метка бесполезна, если не доехала до страницы и до `/ping`."""

    def test_chat_page_prints_it(self):
        html = (ROOT / "app" / "web" / "templates" / "chat.html").read_text(encoding="utf-8")
        self.assertIn("release_label", html, "пометка версии пропала из шаблона чата")

    def test_every_page_gets_it_from_one_place(self):
        """⚠ Контекст один на все страницы (`_ctx`) — по той же причине, что и редакция корпуса:
        вторая копия строки разъедется с первой при первой же правке."""
        src = (ROOT / "app" / "api" / "web.py").read_text(encoding="utf-8")
        self.assertIn('"release_label": release_label()', src)

    def test_ping_reports_it(self):
        """Без этого метку нечем проверить с самой VM — только глазами в браузере."""
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('"release": release_label()', src)

    def test_every_page_shows_it_exactly_once(self):
        """⚠⚠ РЕНДЕРОМ, А НЕ ГРЕПОМ ПО ШАБЛОНУ. Требование владельца — «на всех страницах», и
        проверять его надо на том, что видит пользователь: блок `release_marker` живёт в
        `base.html`, а чат его ГАСИТ и печатает свою строку внутри дисклеймера под полем ввода.
        Греп по одному файлу этого не увидит — ни отсутствия на чате, ни задвоения на прочих.

        Считаем ровно один носитель на страницу: ноль — метки нет там, где обещана; два — две
        надписи об одном, и они разъедутся при первой правке.
        """
        from app.api.web import templates
        from app.core import release as rel

        rel.release_label.cache_clear()
        self.addCleanup(rel.release_label.cache_clear)
        user = {"login": "e", "username": "e", "role": "admin", "full_name": "Э", "id": 1,
                "region": "Курская область", "email": "", "phone": "", "org": "",
                "position": "", "consent": True}
        ctx = dict(request=None, app_title="Навигатор", org="Курская ТПП",
                   corpus_edition="ред. от 22.07.2026", release_label=rel.release_label())
        pages = (("login.html", {"error": None}),
                 ("profile.html", {"user": user, "error": None, "saved": False, "regions": []}),
                 ("terms.html", {}),
                 ("chat.html", {"user": user, "kontur_719_url": "#", "input_hint": ""}))
        for name, extra in pages:
            with self.subTest(page=name):
                html = templates.get_template(name).render(**ctx, **extra)
                carriers = html.count('"release-marker"') + html.count('"composer-version"')
                self.assertEqual(carriers, 1,
                                 f"{name}: носителей метки {carriers}, ожидался ровно один")
                self.assertIn(ctx["release_label"], html, f"{name}: сама метка не напечаталась")

    def test_chat_suppresses_the_shared_marker(self):
        """Отрицательный контроль к предыдущему: чат гасит общий блок ОСОЗНАННО, а не случайно.

        Без этого утверждения «ровно один носитель» на чате выполнялось бы и в том случае, если
        общий маркер пропал бы у ВСЕХ страниц, а чат просто печатал свою строку.
        """
        chat = (ROOT / "app" / "web" / "templates" / "chat.html").read_text(encoding="utf-8")
        base = (ROOT / "app" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn("{% block release_marker %}{% endblock %}", chat)
        self.assertIn("release-marker", base, "общий маркер пропал из base.html")


class TestDeployWritesTheLabel(unittest.TestCase):
    SH = (ROOT / "scripts" / "deploy" / "deploy.sh").read_text(encoding="utf-8")

    def test_deploy_writes_the_release_file(self):
        self.assertIn('printf \'%s\\n\' "$TAG" > RELEASE', self.SH)

    def test_the_label_is_written_before_the_image_is_built(self):
        """⚠⚠ НЕСУЩИЙ ПОРЯДОК. Образ собирается из рабочей копии (`COPY . .`). Файл, записанный
        ПОСЛЕ `compose build`, в контейнер не попадёт, и пометка молча показывала бы прошлый
        релиз — то есть врала бы ровно о том, ради чего заведена."""
        write_at = self.SH.index("> RELEASE")
        build_at = self.SH.index("--- сборка образа ---")
        self.assertLess(write_at, build_at,
                        "метка релиза пишется после сборки — в образ она не попадёт")

    def test_ping_check_compares_the_live_label_with_the_tag(self):
        """Проверка живого процесса ловит «код на диске обновлён, контейнер прежний»."""
        self.assertIn('*"\\"$TAG\\""*)', self.SH)
        self.assertIn("собрался не тот образ", self.SH)

    def test_missing_release_field_is_a_warning_not_a_failure(self):
        """⚠ Образ, собранный до появления метки, — не провал выкатки. Предохранитель, бьющий по
        верному коду, дороже отсутствующего (урок 18.08)."""
        tail = self.SH[self.SH.index("в /ping нет поля release"):][:200]
        self.assertNotIn("fail ", tail)

    def test_release_file_is_not_tracked(self):
        """⚠ Артефакт выкатки. Попади он в git — `git archive` понёс бы ЧУЖОЙ тег в пакет
        следующего релиза, и пометка показывала бы предыдущую версию."""
        self.assertIn("/RELEASE", (ROOT / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
