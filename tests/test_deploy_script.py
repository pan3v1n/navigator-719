r"""Эталонный скрипт выкатки: предохранители не должны исчезнуть — `O2` (#100).

ЗАЧЕМ ЭТОТ ФАЙЛ. До 21.08.2026 скрипт выкатки жил вне git и переписывался копированием, поэтому
предохранители, найденные на живых выкатках, терялись: `D12` проверялась на `test11` и исчезла в
`test12`; отсутствие `flock` стоило получаса SSH; `backup_db.py --verify` без имени файла делал
шаг «бэкап до всего» фикцией во ВСЕХ прошлых выкатках. Тесты ниже закрепляют ровно то, что уже
ломалось, — каждый пункт куплен инцидентом, а не придуман.

ЧЕГО ЗДЕСЬ СОЗНАТЕЛЬНО НЕТ: проверки, что шаблоны маркеров СОВПАДАЮТ с текущим кодом. Профили
релизов историчны, код развивается, и такой тест краснел бы на верном коде — а предохранитель,
бьющий по верному коду, дороже отсутствующего (урок 18.08). Совпадение маркеров проверяет
`deploy.sh --dry-run` в момент подготовки релиза, когда рядом лежит именно тот код, что едет.

Запуск:  .venv\Scripts\python -m unittest discover -s tests
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = ROOT / "scripts" / "deploy"
DEPLOY_SH = DEPLOY_DIR / "deploy.sh"
COMMON_CHECKS = DEPLOY_DIR / "checks" / "common"
RELEASES = DEPLOY_DIR / "releases"


class TestDeployScriptExists(unittest.TestCase):
    def test_script_in_repository(self):
        """Скрипт обязан быть В РЕПОЗИТОРИИ: вне git правки предохранителей не переживают сессию."""
        self.assertTrue(DEPLOY_SH.is_file(), "нет scripts/deploy/deploy.sh")

    @unittest.skipUnless(shutil.which("bash"), "bash недоступен")
    def test_bash_syntax(self):
        r = subprocess.run([shutil.which("bash"), "-n", str(DEPLOY_SH)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"синтаксис deploy.sh сломан:\n{r.stderr}")


def code_only(src: str) -> str:
    """Скрипт без комментариев.

    ⚠ ЭТО НЕ ОПТИМИЗАЦИЯ, А ИСПРАВЛЕНИЕ ДЕФЕКТА В САМИХ ЭТИХ ТЕСТАХ. Первая редакция искала
    `backup_db.py --verify` по всему файлу — и нашла его в комментарии, который ОБЪЯСНЯЕТ, почему
    так звать нельзя. Ровно тот случай, что 18.08 остановил верную выкатку: `grep` по имени
    удалённой константы поймал её в комментарии об удалении. Предохранитель, бьющий по верному
    коду, дороже отсутствующего.
    """
    out = []
    for line in src.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#\s.*$", "", line))
    return "\n".join(out)


class TestSafeguardsSurvive(unittest.TestCase):
    """Каждый тест — про инцидент, который уже был."""

    def setUp(self):
        self.src = code_only(DEPLOY_SH.read_text(encoding="utf-8"))

    def test_flock_present(self):
        """14.08: обрыв ssh плодил второй nohup — четыре параллельные выкатки положили SSH."""
        self.assertIn("flock -n 9", self.src)

    def test_backup_called_with_dir_and_keep(self):
        """18.08: у `--verify` обязателен ИМЯ ФАЙЛА; без него argparse печатал usage и копия
        НЕ создавалась — шаг «бэкап до всего» был фикцией во всех прошлых выкатках."""
        self.assertRegex(self.src, r"backup_db\.py --dir \S+ --keep \d+")
        self.assertNotIn("backup_db.py --verify", self.src)

    def test_backup_failure_aborts(self):
        """Бэкап не снялся — выкатка не начинается вовсе, а не «продолжим, наверное, ок»."""
        i = self.src.index("backup_db.py --dir")
        block = self.src[i:self.src.index("\nfi", i)]
        self.assertIn("exit 1", block, "провал бэкапа не прерывает выкатку")

    def test_no_http_client_inside_qdrant_container(self):
        """20.08: в контейнере qdrant НЕТ ни curl, ни wget. Запрос recover не уходил, коллекция
        осталась прежней, правка не доехала — а рапорт был успешный. HTTP к Qdrant делается
        только из контейнера ПРИЛОЖЕНИЯ."""
        for line in self.src.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if "$QC" in line:
                self.assertNotIn("curl", line, f"curl в контейнере qdrant: {line.strip()}")
                self.assertNotIn("wget", line, f"wget в контейнере qdrant: {line.strip()}")
        self.assertIn("http://qdrant:6333", self.src)

    def test_failed_check_stops_deploy(self):
        """20.08: два AssertionError в лог, а скрипт дошёл до «выкатка завершена» с кодом 0.
        Оператор читает последнюю строку. Итог обязан зависеть от FAILED."""
        self.assertRegex(self.src, r'fail\(\)\s*\{\s*FAILED=1')
        tail = self.src[self.src.rindex('if [ "$FAILED" = "1" ]'):]
        self.assertIn("exit 1", tail, "провал проверок не ведёт к ненулевому коду возврата")

    def test_buildkit_disabled(self):
        """BuildKit идёт в реестр за метаданными базового образа и падает на TLS до auth.docker.io."""
        self.assertIn("DOCKER_BUILDKIT=0", self.src)

    def test_no_secrets_or_addresses(self):
        """Скрипт лежит в git: ни ключей, ни БОЕВЫХ адресов в нём быть не может.

        Петля `127.0.0.1` — исключение и не адрес машины: проверка доступности идёт с самой VM,
        снаружи её мерить нельзя (таймауты дают характеристику канала, а не сервиса)."""
        addresses = [a for a in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", self.src)
                     if a != "127.0.0.1"]
        self.assertEqual(addresses, [], f"боевые адреса в скрипте: {addresses}")
        self.assertNotIn("DEEPSEEK_API_KEY", self.src)
        self.assertNotIn("SESSION_SECRET", self.src)


class TestLineEndings(unittest.TestCase):
    """CRLF в скрипте выкатки — отказ на первой же строке, и он не выглядит как дефект скрипта.

    На Linux-VM shebang с `` даёт «bad interpreter: /usr/bin/env bash^M», а `source` профиля
    кладёт `` внутрь `TAG` — то есть в имя архива, который потом «не найден». Разработка идёт
    на Windows с `core.autocrlf=true`, поэтому нормализация должна быть ЗАДАНА, а не унаследована
    от настроек машины.
    """

    def test_gitattributes_pins_lf(self):
        ga = ROOT / ".gitattributes"
        self.assertTrue(ga.is_file(), "нет .gitattributes — перевод строк отдан на волю машины")
        text = ga.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^\*\.sh\s+text\s+eol=lf", "*.sh не закреплён на LF")
        self.assertRegex(text, r"(?m)^scripts/deploy/releases/\*\.env\s+text\s+eol=lf",
                         "профили релизов не закреплены на LF")


class TestCommonChecksAccumulate(unittest.TestCase):
    """`checks/common` — то, ради чего заведён каталог: однажды найденный дефект не возвращается."""

    def test_common_dir_not_empty(self):
        files = sorted(COMMON_CHECKS.glob("*.py"))
        self.assertTrue(files, "в checks/common нет ни одной проверки — выкатка пойдёт вслепую")

    def test_every_check_compiles(self):
        for f in sorted(COMMON_CHECKS.glob("*.py")):
            with self.subTest(check=f.name):
                ast.parse(f.read_text(encoding="utf-8"), filename=str(f))

    def test_every_check_actually_asserts(self):
        """Проверка, которая только печатает, — фикция: лог зелёный, дефект на бою.
        Ровно этот класс уже стоил проекту трёх инцидентов."""
        for f in sorted(COMMON_CHECKS.glob("*.py")):
            with self.subTest(check=f.name):
                tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
                has_assert = any(isinstance(n, (ast.Assert, ast.Raise)) for n in ast.walk(tree))
                self.assertTrue(has_assert, f"{f.name} ничего не утверждает — только печатает")

    def test_d12_check_did_not_disappear_again(self):
        """`D12` проверялась на test11 и ИСЧЕЗЛА в test12 вместе со скриптом релиза."""
        joined = "\n".join(f.read_text(encoding="utf-8") for f in COMMON_CHECKS.glob("*.py"))
        self.assertIn("14.12.30.131", joined, "проверка добора по иерархии (D12) снова потеряна")


class TestReleaseProfiles(unittest.TestCase):
    MARKER_RE = re.compile(r'"([^"|]+)\|([^"|]+)\|([^"]*)"')

    def _profiles(self):
        return sorted(RELEASES.glob("*.env"))

    def test_at_least_one_profile(self):
        self.assertTrue(self._profiles(), "нет ни одного профиля релиза — скрипт нечем запустить")

    def test_required_fields(self):
        for p in self._profiles():
            with self.subTest(profile=p.name):
                text = p.read_text(encoding="utf-8")
                self.assertRegex(text, r'(?m)^TAG="[^"]+"', "профиль без TAG")
                self.assertRegex(text, r"(?m)^EXPECT=\(", "профиль без счётчиков коллекций")

    def test_marker_files_exist(self):
        """Шаблон может устареть (это нормально), а вот файл, которого нет, — всегда опечатка:
        `grep` по несуществующему пути молча не найдёт ничего и остановит верную выкатку."""
        for p in self._profiles():
            text = p.read_text(encoding="utf-8")
            for path, _pattern, desc in self.MARKER_RE.findall(text):
                if "/" not in path:
                    continue
                with self.subTest(profile=p.name, marker=path):
                    self.assertTrue((ROOT / path).is_file(), f"{path} нет в репозитории ({desc})")

    def test_marker_patterns_compile(self):
        for p in self._profiles():
            text = p.read_text(encoding="utf-8")
            for path, pattern, _desc in self.MARKER_RE.findall(text):
                if "/" not in path:
                    continue
                with self.subTest(profile=p.name, pattern=pattern):
                    re.compile(pattern)

    @unittest.skipUnless(shutil.which("bash"), "bash недоступен")
    def test_dry_run_passes_for_every_profile(self):
        """Сухой прогон — единственное место, где профиль проверяется целиком и до боя.

        ⚠ `SKIP_CI_GATE=1` здесь ОБЯЗАТЕЛЕН, и это не ослабление проверки. Тест отвечает на
        вопрос «профиль читается, файлы на месте», а гейт CI — на другой: «батарея на этом
        коммите зелёная». Без отключения получается петля: батарея красная → сухой прогон
        падает → падает этот тест → батарея красная. Предохранитель, замкнутый сам на себя,
        чинить нечем. Поведение самого гейта проверяет `TestCiGate` — на подставном `gh`.
        """
        env = {**os.environ, "SKIP_CI_GATE": "1"}
        for p in self._profiles():
            with self.subTest(profile=p.name):
                r = subprocess.run(
                    [shutil.which("bash"), str(DEPLOY_SH), "--release", str(p), "--dry-run"],
                    capture_output=True, text=True, cwd=str(ROOT), env=env)
                self.assertEqual(r.returncode, 0,
                                 f"сухой прогон {p.name} упал:\n{r.stdout}\n{r.stderr}")


@unittest.skipUnless(shutil.which("bash"), "bash недоступен")
class TestCiGate(unittest.TestCase):
    r"""Гейт CI: красная батарея обязана ОСТАНАВЛИВАТЬ выкатку — инцидент 21.08.2026.

    ЧТО СЛУЧИЛОСЬ. Тест `EV16`, перестав быть офлайновым, уронил батарею — и она простояла
    красной ШЕСТЬ прогонов подряд, пока PR #103 уезжал в `main`, а `v0.5.0-test13` — на бой.
    CI отработал безупречно: он ловит ровно это и предупреждает об этом прямо в `tests.yml`.
    Не сработал ЧЕЛОВЕЧЕСКИЙ шаг — прочитать сигнал. Поэтому проверка переезжает туда, где её
    нельзя не заметить: в обязательный сухой прогон перед выкаткой.

    ⚠ ПОДСТАВНОЙ `gh`, А НЕ ЖИВОЙ GITHUB. Тест о ПОВЕДЕНИИ гейта, а не о цвете репозитория:
    зависеть от реального состояния CI значило бы завести тест, меняющий вердикт сам по себе, —
    то есть повторить ошибку `EV16` в новом месте. Батарея офлайновая, и здесь тоже.
    """

    def _run_with_fake_gh(self, gh_stdout: str):
        """Сухой прогон с подставным `gh` первым в PATH; `gh_stdout` — что он печатает."""
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "gh"
            fake.write_text("#!/bin/sh\necho '" + gh_stdout + "'\n", encoding="utf-8",
                            newline="\n")
            fake.chmod(0o755)
            env = {**os.environ, "PATH": td + os.pathsep + os.environ.get("PATH", "")}
            env.pop("SKIP_CI_GATE", None)
            profile = sorted(RELEASES.glob("*.env"))[0]
            return subprocess.run(
                [shutil.which("bash"), str(DEPLOY_SH), "--release", str(profile), "--dry-run"],
                capture_output=True, text=True, cwd=str(ROOT), env=env)

    def test_red_ci_stops_the_dry_run(self):
        """Главное. Отчитаться о провале и продолжить — то же самое, что не проверять вовсе."""
        r = self._run_with_fake_gh("success failure")
        self.assertNotEqual(r.returncode, 0, f"красный CI не остановил прогон:\n{r.stdout}")
        self.assertIn("КРАСНЫЙ", r.stdout)

    def test_green_ci_passes(self):
        r = self._run_with_fake_gh("success")
        self.assertEqual(r.returncode, 0, f"зелёный CI остановил прогон:\n{r.stdout}{r.stderr}")

    def test_no_runs_found_is_a_warning_not_a_failure(self):
        """⚠ «Проверить не смог» ≠ «красный»: нет сети — это не дефект выкатываемого кода."""
        r = self._run_with_fake_gh("")
        self.assertEqual(r.returncode, 0, f"отсутствие прогона принято за провал:\n{r.stdout}")
        self.assertIn("НЕ ПРОВЕРЕН", r.stdout)

    def test_gate_is_wired_into_dry_run(self):
        """Функция может существовать и не вызываться — проверяем именно ВЫЗОВ."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertIn("ci_gate()", code, "функция гейта исчезла из скрипта")
        self.assertIn('ci_gate "$SRC"', code, "гейт не вызывается в сухом прогоне")


if __name__ == "__main__":
    unittest.main()
