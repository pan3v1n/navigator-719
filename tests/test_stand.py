"""Стенд dev.719-навигатор.рф: предохранители в файлах настройки. Офлайн, сервер не нужен.

Стенд живёт на сервере боя, поэтому здесь закреплено то, что защищает бой и данные: своя база, нет портов
наружу, потолок ресурсов, пароль и запрет индексации, откат блока caddy при любой ошибке.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy" / "stand.sh"
BASH = shutil.which("bash")
if BASH and "system32" in BASH.lower():  # заглушка WSL не видит файлов этой системы
    BASH = None

# Подготовка идёт в bash, а не в Python: пути Windows сюда не доезжают. Снаружи всё заменено заглушками, поэтому
# сценарии сбоя настоящие: docker отвечает кодом ошибки, а скрипт идёт по тем же веткам, что и на сервере.
REHEARSAL = r'''
set -u
script="$1"; shift
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/prod/caddy"
export PROD="$tmp/prod" STAND="$tmp/stand" DOCKER=docker STUB_LOG="$tmp/docker.log" PATH="$tmp/bin:$PATH"
: > "$STUB_LOG"
: > "$tmp/out"
cat > "$PROD/.env" <<'EOF'
PUBLIC_DOMAIN=example.test
APP_SUBDOMAIN=svc
SESSION_SECRET=prod-session-secret
DEEPSEEK_API_KEY=prod-llm-key
DEEPSEEK_MODEL=prod-model
RERANK_ENABLED=false
EMBEDDING_MODEL=/models/e5
DADATA_API_KEY=prod-dadata-key
EOF
printf 'site {\n}\n' > "$PROD/caddy/Caddyfile"
[ -n "${STUB_NO_IMPORT:-}" ] || printf 'import stand.d/*.caddy\n' >> "$PROD/caddy/Caddyfile"
cat > "$tmp/bin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"
for svc in caddy qdrant app; do
  case "$*" in ps*service=$svc*)
    [ "${STUB_NO_CONTAINER:-}" = "$svc" ] || echo "prod-$svc"
    exit 0 ;;
  esac
done
case "$*" in
  "inspect prod-qdrant"*) echo prod_default ;;
  "inspect prod-app"*) echo prod_models ;;
  *"caddy hash-password"*) echo '$2a$14$stubhash' ;;
  *"caddy validate"*) exit "${STUB_VALIDATE:-0}" ;;
  *"caddy reload"*)
    [ "$(grep -c 'caddy reload' "$STUB_LOG")" -eq 1 ] && exit "${STUB_RELOAD:-0}"
    exit 0 ;;
esac
EOF
cat > "$tmp/bin/curl" <<'EOF'
#!/bin/sh
case "$*" in
  *https://dev.*) printf '%s' "${STUB_DEV_CODE:-401}" ;;
  *) printf '%s' "${STUB_PROD_CODE:-200}" ;;
esac
EOF
printf '#!/bin/sh\necho "Mem: 7900 1900 500 10 1000 ${STUB_MEM:-4500}"\n' > "$tmp/bin/free"
printf '#!/bin/sh\necho Avail; echo " ${STUB_DISK:-49}G"\n' > "$tmp/bin/df"
printf '#!/bin/sh\necho "stubRandom$$"\n' > "$tmp/bin/openssl"
printf '#!/bin/sh\nexit 0\n' > "$tmp/bin/sleep"
chmod +x "$tmp"/bin/*
for _ in $(seq 1 "${STUB_RUNS:-1}"); do
  bash "$script" "$@" >> "$tmp/out" 2>&1
  code=$?
  [ -f "$tmp/first" ] || cp "$STAND/ACCESS.txt" "$tmp/first" 2>/dev/null
done
printf '===code\n%s\n' "$code"
printf '===modes\n'
stat -c '%a %n' "$PROD/caddy/stand.d" "$PROD/caddy/stand.d/dev.caddy" "$STAND" "$STAND/.env.stand" "$STAND/ACCESS.txt" 2>/dev/null \
  | sed "s|$tmp/||"
for f in out docker.log first prod/caddy/stand.d/dev.caddy stand/.env.stand stand/ACCESS.txt; do
  printf '===%s\n' "$f"
  if [ -f "$tmp/$f" ]; then cat "$tmp/$f"; else printf '<нет>\n'; fi
done
'''
SITE = "prod/caddy/stand.d/dev.caddy"
ENV_STAND = "stand/.env.stand"
NONE = "<нет>\n"


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


@unittest.skipUnless(BASH, "нужен bash, видящий файлы этой системы")
class TestRehearsal(unittest.TestCase):
    """`check` и `init` целиком: скрипт правит конфиг боевого caddy, и сверки текста тут мало."""

    def stand(self, *args, **stubs) -> dict[str, str]:
        r = subprocess.run([BASH, "-c", REHEARSAL, "_", SCRIPT.as_posix(), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env={**os.environ, **stubs}, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        parts = re.split(r"^===(\S+)\n", r.stdout, flags=re.M)
        return dict(zip(parts[1::2], parts[2::2]))

    def assert_untouched(self, f):
        self.assertEqual(f[SITE], NONE, "блока стенда быть не должно")
        self.assertEqual(f[ENV_STAND], NONE)
        self.assertNotIn("hash-password", f["docker.log"])

    def test_check_changes_nothing(self):
        f = self.stand("check")
        self.assertEqual(f["code"].strip(), "0", f["out"])
        self.assertIn("можно настраивать", f["out"])
        self.assertNotIn("exec", f["docker.log"])
        self.assert_untouched(f)

    def test_init_stops_before_writing_when_server_is_not_ready(self):
        for stubs in ({"STUB_MEM": "2000"}, {"STUB_DISK": "10"}, {"STUB_NO_IMPORT": "1"},
                      {"STUB_NO_CONTAINER": "caddy"}, {"STUB_NO_CONTAINER": "qdrant"}, {"STUB_NO_CONTAINER": "app"}):
            with self.subTest(stubs):
                f = self.stand("init", **stubs)
                self.assertEqual(f["code"].strip(), "1", f["out"])
                self.assert_untouched(f)

    def test_init_validates_before_reloading_and_checks_production(self):
        f = self.stand("init")
        self.assertEqual(f["code"].strip(), "0", f["out"])
        log = f["docker.log"]
        self.assertLess(log.index("caddy validate"), log.index("caddy reload"))
        self.assertEqual(log.count("caddy reload"), 1)
        self.assertIn("бой отвечает 200", f["out"])
        self.assertIn("стенд закрыт паролем", f["out"])
        self.assertIn("stand $2a$14$stubhash", f[SITE])
        self.assertIn("dev.{$PUBLIC_DOMAIN}", f[SITE], "домен подставляет caddy, а не оболочка")

    def test_stand_settings_take_engine_but_not_production_secrets(self):
        env = self.stand("init")[ENV_STAND]
        for line in ("DEEPSEEK_API_KEY=prod-llm-key", "DEEPSEEK_MODEL=prod-model", "RERANK_ENABLED=false",
                     "EMBEDDING_MODEL=/models/e5", "PUBLIC_DOMAIN=example.test", "COOKIE_SECURE=true"):
            self.assertIn(line + "\n", env)
        self.assertIn("SESSION_SECRET=stubRandom", env)
        self.assertNotIn("prod-session-secret", env)
        self.assertNotIn("DADATA", env)
        self.assertNotIn("DEEPSEEK_BASE_URL", env, "не заданное у боя остаётся значением по умолчанию")

    def test_nothing_secret_is_printed(self):
        f = self.stand("init")
        password = re.search(r"stand / (\S+)", f["stand/ACCESS.txt"]).group(1)
        self.assertNotIn(password, f["out"])
        self.assertNotIn("prod-llm-key", f["out"])

    def test_any_caddy_failure_leaves_production_as_it_was(self):
        for stubs, reloads in (({"STUB_VALIDATE": "1"}, 0), ({"STUB_RELOAD": "1"}, 2),
                               ({"STUB_PROD_CODE": "502"}, 2), ({"STUB_PROD_CODE": "000"}, 2)):
            with self.subTest(stubs):
                f = self.stand("init", **stubs)
                self.assertEqual(f["code"].strip(), "1", f["out"])
                self.assertEqual(f[SITE], NONE)
                self.assertEqual(f["docker.log"].count("caddy reload"), reloads, "откат обязан перечитать конфиг")
                self.assertIn("СТОП:", f["out"])

    def test_certificate_delay_is_a_warning_not_a_rollback(self):
        """Бой при этом здоров: откатывать блок из-за сертификата стенда незачем."""
        f = self.stand("init", STUB_DEV_CODE="000")
        self.assertEqual(f["code"].strip(), "0", f["out"])
        self.assertNotEqual(f[SITE], NONE)
        self.assertIn("стенд ответил 000", f["out"])

    def test_second_init_keeps_password_and_does_not_touch_caddy(self):
        f = self.stand("init", STUB_RUNS="2")
        self.assertEqual(f["code"].strip(), "0", f["out"])
        self.assertEqual(f["first"], f["stand/ACCESS.txt"])
        self.assertEqual(f["docker.log"].count("hash-password"), 1)
        self.assertEqual(f["docker.log"].count("caddy reload"), 1)
        self.assertIn("пароль не меняю", f["out"])

    @unittest.skipUnless(os.name == "posix", "права файлов проверяются только в Linux")
    def test_modes(self):
        modes = dict(reversed(line.split(" ", 1)) for line in self.stand("init")["modes"].splitlines())
        self.assertEqual(modes["prod/caddy/stand.d"], "755", "caddy в контейнере должен видеть блок")
        self.assertEqual(modes[SITE], "644")
        self.assertEqual(modes["stand"], "700")
        self.assertEqual(modes[ENV_STAND], "600")
        self.assertEqual(modes["stand/ACCESS.txt"], "600")


if __name__ == "__main__":
    unittest.main()
