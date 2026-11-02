"""Стенд dev.719-навигатор.рф: предохранители в файлах настройки. Офлайн, сервер не нужен.

Стенд живёт на сервере боя, поэтому здесь закреплено то, что защищает бой и данные: своя база, нет портов
наружу, потолок ресурсов, пароль и запрет индексации, откат блока caddy при любой ошибке.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy" / "stand.sh"


class TestCompose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = yaml.safe_load((ROOT / "docker-compose.stand.yml").read_text(encoding="utf-8"))
        cls.app = cls.doc["services"]["app-stand"]

    def test_only_the_application_runs_here(self):
        self.assertEqual(list(self.doc["services"]), ["app-stand"], "caddy и Qdrant — боя, вторых быть не должно")

    def test_no_ports_outside(self):
        self.assertNotIn("ports", self.app)

    def test_database_is_its_own(self):
        self.assertIn("standdata:/data", self.app["volumes"])
        self.assertNotIn("external", self.doc["volumes"]["standdata"] or {}, "база боя сюда не монтируется: там ПДн")

    def test_model_volume_is_read_only(self):
        self.assertIn("prod_models:/models:ro", self.app["volumes"])

    def test_resources_are_capped(self):
        self.assertEqual(self.app["mem_limit"], "3g")
        self.assertEqual(self.app["cpus"], 2)

    def test_settings_are_separate_from_production(self):
        self.assertEqual(self.app["env_file"], ".env.stand")

    def test_production_mode_guards_the_secret(self):
        """В `production` приложение не стартует с секретом сессии из репозитория."""
        self.assertEqual(self.app["environment"]["APP_ENV"], "production")


class TestScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SCRIPT.read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("bash"), "bash недоступен")
    def test_bash_syntax(self):
        r = subprocess.run([shutil.which("bash"), "-n", str(SCRIPT)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_lf_endings_are_forced(self):
        self.assertIn("*.sh text eol=lf", (ROOT / ".gitattributes").read_text(encoding="utf-8"))

    def test_site_block_has_password_and_noindex(self):
        block = self.src[self.src.index("dev.{\\$PUBLIC_DOMAIN}"):self.src.index("\nEOF\n")]
        self.assertIn("basic_auth", block)
        self.assertIn('X-Robots-Tag "noindex, nofollow"', block)
        self.assertIn("reverse_proxy app-stand:8000", block)

    def test_any_caddy_failure_rolls_back(self):
        init = self.src[self.src.index("cmd_init()"):self.src.index("cmd_up()")]
        self.assertLess(init.index("caddy_cmd validate"), init.index("caddy_cmd reload"))
        self.assertIn('rm -f "$SITE"', init)
        self.assertIn("drop_site; die", init)
        self.assertIn('http_code "$svc_host" /ping', init, "после перечитывания сверяется бой")

    def test_passwords_are_not_printed(self):
        messages = re.findall(r'\b(?:say|die) "([^"]*)"', self.src)
        self.assertGreater(len(messages), 10, "сообщения не нашлись: проверка ничего не проверила бы")
        for text in messages:
            for secret in ("$pw", "$key", "$hash"):
                self.assertNotIn(secret, text)

    def test_access_file_is_private(self):
        self.assertIn("umask 077", self.src)
        self.assertIn('chmod 700 "$STAND"', self.src)

    def test_rerun_keeps_the_password(self):
        init = self.src[self.src.index("cmd_init()"):self.src.index("cmd_up()")]
        self.assertLess(init.index('if [ -f "$SITE" ]'), init.index("openssl rand -base64 18"))


class TestSecretsStayOut(unittest.TestCase):
    def test_gitignore_covers_stand_secrets(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("caddy/stand.d/", ignored)
        self.assertIn(".env.stand", ignored)

    def test_image_does_not_take_them(self):
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn(".env.stand", ignored)


if __name__ == "__main__":
    unittest.main()
