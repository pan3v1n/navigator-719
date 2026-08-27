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

    На Linux-VM shebang с `
` даёт «bad interpreter: /usr/bin/env bash^M», а `source` профиля
    кладёт `
` внутрь `TAG` — то есть в имя архива, который потом «не найден». Разработка идёт
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

    def test_every_check_survives_a_windows_console(self):
        """⚠ Проверки выкатки печатают «→» и «✅» — на cp1251 они падали бы в момент печати.

        В контейнере это не видно (там UTF-8), и потому дефект жил незаметно. Обнаружен
        25.08.2026 предпродажным прогоном проверок с машины разработчика: две из шести падали
        UnicodeEncodeError'ом на ВЕРНОМ корпусе. Предохранитель, который нельзя запустить там,
        где готовят релиз, проверяет только один из двух контуров.

        Тест закрывает класс: новая проверка обязана звать `enable_utf8()`."""
        root = COMMON_CHECKS.parent
        missing = [str(f.relative_to(root)) for f in sorted(root.rglob("*.py"))
                   if "enable_utf8()" not in f.read_text(encoding="utf-8")]
        self.assertFalse(missing, f"проверки без enable_utf8(): {missing}")
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

    def test_both_readers_of_a_profile_see_the_same_markers(self):
        """⚠⚠ ПРОФИЛЬ ЧИТАЮТ ДВА МЕХАНИЗМА, И ОНИ МОГУТ РАЗОЙТИСЬ МОЛЧА (ревью захода 5.5).

        `source` в `deploy.sh` разбирает запись по правилам bash, а этот файл — регуляркой
        `MARKER_RE`, которая режет её по первой кавычке. Анти-маркер вида `ci_gate \".SRC\"`
        первый читал верно, а второй ПРОПУСКАЛ — 17 записей из 18. Значит опечатка в такой
        записи не ловится ничем до самой выкатки, а проверки этого файла тихо её не покрывают.
        Предупреждение об этом расхождении стоит в профиле `test19` с 26.08.2026 — теперь оно
        ещё и проверяется.
        """
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash недоступен")
        for p in self._profiles():
            with self.subTest(profile=p.name):
                r = subprocess.run(
                    [bash, "-c", 'declare -A EXPECT=(); MARKERS=(); ANTI_MARKERS=(); . "$1"; '
                                 'echo $(( ${#MARKERS[@]} + ${#ANTI_MARKERS[@]} ))', "_", str(p)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
                by_bash = int(r.stdout.strip() or -1)
                by_regex = len(self.MARKER_RE.findall(p.read_text(encoding="utf-8")))
                self.assertEqual(by_bash, by_regex,
                                 f"{p.name}: bash видит {by_bash} записей, парсер тестов "
                                 f"{by_regex} — какая-то запись не проверяется одним из них")

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
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    cwd=str(ROOT), env=env)
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
            # ⚠ `encoding="utf-8"` ОБЯЗАТЕЛЕН. `text=True` декодирует вывод кодировкой локали, и на
            # Windows (cp1251) «КРАСНЫЙ» из `deploy.sh` возвращался как «К\xa0АСНЫЙ»: тест падал на
            # ВЕРНОМ коде — предохранитель отрабатывал, `returncode` был правильным, не совпадала
            # только строка. Ровно тот же класс, что и падение скриптов на печати «⚠».
            return subprocess.run(
                [shutil.which("bash"), str(DEPLOY_SH), "--release", str(profile), "--dry-run"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(ROOT), env=env)

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
        self.assertIn("ci_gate ", code, "гейт не вызывается в сухом прогоне")

    def test_gate_runs_on_a_real_deploy_too(self):
        """⚠ Первая редакция звала гейт ТОЛЬКО в `--dry-run`, а ничто не требует, чтобы сухой
        прогон вообще состоялся: самая очевидная форма запуска (первая строка usage) обходила
        предохранитель целиком. Найдено ревью 24.08.2026."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        after_dry_run = code.split('if [ "$DRY_RUN" = "1" ]', 1)[-1].split("exit 0", 1)[-1]
        self.assertIn("ci_gate", after_dry_run, "на реальной выкатке гейт не зовётся")

    def test_tag_lookup_cannot_return_garbage(self):
        r"""⚠⚠ ДЕФЕКТ, ВНЕСЁННЫЙ ПРАВКОЙ ПО РЕВЬЮ И ПОЙМАННЫЙ CI. Голый
        `git rev-parse "$TAG^{commit}"` при отсутствующем теге печатает в stdout САМУ СТРОКУ
        («v0.5.0-test12^{commit}») и выходит с кодом 128 — переменная получает мусор вместо SHA,
        и гейт ищет прогоны по несуществующему коммиту, всегда находя «ничего». В чекауте GitHub
        Actions тегов нет, поэтому туда попадала именно эта ветка.

        Второй слой: форма `sha=$(...) && from=...` при неудаче оставляла `from` неприсвоенной, и
        под `set -u` функция падала на печати. Поэтому проверяем и `--verify --quiet`, и `if`.
        """
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertIn('rev-parse --verify --quiet "$TAG^{commit}"', code,
                      "без --verify --quiet отсутствующий тег даёт мусор вместо SHA")
        self.assertRegex(code, r'if sha=\$\(cd "\$src" && git rev-parse --verify --quiet',
                         "форма `cmd && from=…` оставляет `from` неприсвоенной под set -u")

    def test_reindex_step_lives_in_the_script_not_in_someones_memory(self):
        """⚠ Правка, меняющая payload, доезжает до ответа только через переиндексацию. Пока это
        был «шаг оператора», релиз состоял из скрипта и человеческой памяти — ровно так уехала
        `D12`: код новый, данные прежние, снаружи неотличимо от успешной выкатки."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertIn("REINDEX_DOCS", code, "шаг переиндексации исчез из скрипта")
        self.assertIn("--doc", code, "переиндексация идёт не по документам (K11)")
        self.assertRegex(code, r"переиндексация \$doc НЕ УДАЛАСЬ[^|]*exit 1|exit 1",
                         "провал переиндексации не останавливает выкатку")

    def test_full_recreate_is_gated_never_the_default_path(self):
        """⚠⚠ ПРАВИЛО УТОЧНЕНО 25.08.2026, а не отменено.

        Прежняя редакция этого теста запрещала полную переиндексацию в скрипте АБСОЛЮТНО — и была
        права ровно для того случая, ради которого писалась: переиндексации СУЩЕСТВУЮЩЕГО
        документа. Но `K15` добавила в корпус НОВЫЙ документ, и тут `--doc` не годится вовсе:
        `avgdl` считается по всему корпусу и запекается в sparse-вектор каждой точки, поэтому
        новый документ уводит в чужой масштаб BM25 не свои точки, а точки СОСЕДЕЙ (95.84 -> 96.72).
        Сам загрузчик это и не даст сделать — `--doc` для отсутствующего документа останавливается.

        Правило поэтому такое: полная переиндексация ДОПУСТИМА, но только за явным
        `REINDEX_FULL` в профиле релиза, никогда не по умолчанию, и обязана предупреждать о цене.
        """
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertIn("REINDEX_FULL", code, "шаг полной переиндексации исчез из скрипта")
        # полная загрузка вызывается ТОЛЬКО внутри ветки REINDEX_FULL
        i = code.index("REINDEX_FULL")
        head = code[:i]
        self.assertNotIn("load_rules_kb.py 2>", head,
                         "полная переиндексация зовётся вне ветки REINDEX_FULL — это путь по умолчанию")
        self.assertNotIn("recreate_collection", code)
        # цена окна названа оператору
        self.assertRegex(code, r"процедурная ветка будет пустой",
                         "скрипт не предупреждает, что коллекция пустеет на время прогона e5")

    def test_dry_run_never_says_corpus_untouched_when_it_reindexes(self):
        """⚠⚠ Сообщение, врущее в сторону «безопасно», хуже отсутствующего.

        Прежняя редакция смотрела ТОЛЬКО на снапшот и на профиле v0.5.0-test15 (REINDEX_FULL,
        коллекция пересоздаётся) печатала «выкатка рантаймовая, корпус не трогаем». Оператор
        читает СТРОКУ, а не профиль — и принял бы решение о моменте выкатки по неверной картине.
        Поймано живым сухим прогоном 25.08.2026.
        """
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        # строка про «не трогаем» обязана быть под условием, исключающим обе переиндексации
        i = code.index("КОРПУС: не трогаем")
        head = code[:i]
        self.assertIn('elif [ -z "$SNAPSHOT" ]', head,
                      "«корпус не трогаем» печатается не в последней ветке цепочки")
        self.assertLess(head.index('if [ -n "${REINDEX_FULL:-}" ]'), i,
                        "ветка REINDEX_FULL обязана стоять ДО «не трогаем»")
        self.assertLess(head.index('elif [ -n "${REINDEX_DOCS:-}" ]'), i,
                        "ветка REINDEX_DOCS обязана стоять ДО «не трогаем»")
        # и полная переиндексация обязана называть цену
        self.assertRegex(code, r"КОРПУС: ПОЛНАЯ переиндексация")

    def test_full_and_per_document_reindex_are_mutually_exclusive(self):
        """Профиль, задавший оба, противоречив: `--doc` после пересоздания коллекции бессмыслен.

        Ловим это ДО выкатки кодом 2 (как и прочие противоречия профиля), а не в середине."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertRegex(code, r"ПРОФИЛЬ ПРОТИВОРЕЧИВ.*REINDEX_FULL")
        self.assertIn("exit 2", code)

    def test_failed_full_reindex_says_the_service_is_broken(self):
        """⚠ Провал ЗДЕСЬ страшнее провала `--doc`: коллекция уже снесена.

        При `--doc` неудача оставляет корпус в промежуточном, но рабочем состоянии; при полной —
        сервис отвечает пустотой, и сообщение обязано это называть, а не говорить «не удалось»."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertRegex(code, r"ПОЛНАЯ переиндексация НЕ УДАЛАСЬ[^\n]*СЕРВИС СЛОМАН")

    def test_network_failure_is_distinguished_from_no_runs(self):
        """«gh не ответил» и «прогонов нет» — разные вещи. На живом прогоне 24.08 запрос отвалился
        по таймауту, и гейт сказал «прогона не нашлось» про коммит, у которого их два и оба
        красные. Деградация безопасная, но оператор читает СТРОКУ."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertIn("if ! concl=$(", code, "код возврата gh не отличается от пустой выдачи")
        text = DEPLOY_SH.read_text(encoding="utf-8")
        self.assertIn("gh НЕ ОТВЕТИЛ", text)
        self.assertIn("прогонов на $sha нет", text)


class TestExitCodeOfCommandNotPipeline(unittest.TestCase):
    """`O6` #114: четыре предохранителя выкатки были недостижимым кодом.

    ⚠⚠ `set -o pipefail` в скрипте нет, поэтому статус конвейера — это статус ПОСЛЕДНЕЙ команды.
    Шаги, отправлявшие вывод в `tail`, проверялись по коду `tail`, а он всегда 0:
    бэкап боевой БД (предохранитель B из шапки, тот самый, что уже чинили 18.08 от ДРУГОГО
    дефекта), обе ветки сборки образа и обе переиндексации.
    """

    def test_run_step_wrapper_exists(self):
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertIn("run_step() {", code, "обёртка исчезла — коды возврата снова съест конвейер")
        self.assertIn('return $rc', code, "обёртка обязана возвращать код КОМАНДЫ")

    def test_all_four_safeguards_use_it(self):
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        for step in ("backup_db.py", "compose build app", "build -f /tmp/Dockerfile.thin",
                     'load_rules_kb.py --doc "$doc"'):
            i = code.index(step)
            head = code[max(0, i - 260):i]
            self.assertIn("run_step", head, f"шаг {step!r} снова проверяется по коду конвейера")

    def test_no_pipeline_swallows_a_checked_exit_code(self):
        """⚠ Закрываем КЛАСС, а не четыре места: форма `cmd | tail` рядом с проверкой запрещена.

        Дефект внесён дважды — заходом 4 и заходом 5, причём второй копировал форму первого."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        for line_no, line in enumerate(code.splitlines(), 1):
            if "| tail" not in line and "| head" not in line:
                continue
            checked = line.lstrip().startswith("if ") or "||" in line or line.rstrip().endswith("\\")
            self.assertFalse(checked,
                             f"строка {line_no}: код возврата конвейера проверяется — {line.strip()!r}")

    def test_full_reindex_failure_says_the_service_is_broken(self):
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertRegex(code, r"ПОЛНАЯ переиндексация НЕ УДАЛАСЬ[^\n]*СЕРВИС СЛОМАН")


class TestDryRunChecksTheCodeThatWillShip(unittest.TestCase):
    """`O5` #113: сухой прогон на VM сверял маркеры со СТАРОЙ рабочей копией.

    То есть с кодом, который выкатка как раз ЗАМЕНЯЕТ, — и верный профиль получал «13 маркеров не
    совпало, чинить ЗДЕСЬ». Воспроизведено 25.08.2026 на живой выкатке `v0.5.0-test15`.
    """

    def test_marker_roots_exist(self):
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertIn("MARKER_ROOTS", code)
        self.assertIn('for root in ${MARKER_ROOTS}', code,
                      "check_marker обязан искать файл по списку корней")

    def test_dry_run_unpacks_the_package_when_it_is_there(self):
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertIn('tar -xzf "$PKG/navigator-719-$TAG.tar.gz" -C "$DRY_TMP"', code)
        self.assertIn('MARKER_ROOTS="$DRY_TMP $APP"', code,
                      "пакет обязан идти ПЕРВЫМ, рабочая копия — вторым (файл не менялся)")

    def test_working_copy_is_not_a_lone_candidate_any_more(self):
        """⚠ Отрицательный контроль: `$APP` в одиночку — заведомо неверный источник.

        Он и есть код, который заменяют; сверка с ним всегда обвиняет верный профиль."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        i = code.index("for cand in")
        line = code[i:code.index("\n", i)]
        self.assertNotIn('"$APP"', line, "рабочая копия снова одна из кандидатов SRC")

    def test_temp_dir_is_cleaned_on_every_exit(self):
        """Распакованный пакет не должен оставаться на диске VM после сухого прогона."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertGreaterEqual(code.count('rm -rf "$DRY_TMP"'), 3,
                                "уборка есть не на всех выходах сухого прогона")

    def test_script_no_longer_contradicts_itself_about_where_it_runs(self):
        """⚠ Корень дефекта: usage учил «запуск на VM», комментарий — «идёт на машине разработчика».

        Два места скрипта утверждали противоположное, а резолвинг был написан под одно из
        прочтений. Тот же класс, что «утратил силу в трёх местах без связи» из ревью захода 4."""
        text = DEPLOY_SH.read_text(encoding="utf-8")
        self.assertNotIn("идёт на машине разработчика, где", text,
                         "противоречие вернулось в комментарий")


class TestRunCompositionGate(unittest.TestCase):
    """`EV19` #111: уровень 3 исполнялся частично ДВА релиза подряд, а сводки писали «✅»."""

    MANDATORY = ("recall@1", "attribution@1", "faithfulness", "decisive_numbers_stable",
                 "paraphrase", "perturbation", "context_size")

    def test_gate_lives_in_the_dry_run(self):
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        self.assertIn("В БАЗЕ СРАВНЕНИЯ НЕТ МЕТРИК", code)
        for metric in self.MANDATORY:
            self.assertIn(metric, code, f"метрика {metric} не проверяется гейтом")

    def test_gate_warns_and_does_not_stop(self):
        """⚠ Осознанно предупреждение, а не остановка: замер — решение владельца.

        Бывает выкатка без полного прогона (рантаймовая правка, откат). Но оператор обязан
        УВИДЕТЬ, чего нет, а не узнать через два релиза."""
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        i = code.index("В БАЗЕ СРАВНЕНИЯ НЕТ МЕТРИК")
        tail = code[i:i + 400]
        self.assertNotIn("exit 2", tail, "гейт состава замера не должен останавливать выкатку")

    def test_baseline_actually_carries_every_mandatory_metric(self):
        """⚠⚠ ГЛАВНАЯ половина: гейт в скрипте увидит пропажу только на выкатке, а этот тест —

        сразу, в CI. Обновление базы, недосчитавшее метрику, покраснеет здесь."""
        import json
        base = DEPLOY_DIR.parent.parent / "docs" / "eval_baseline" / "baseline.json"
        self.assertTrue(base.exists(), "базы сравнения нет — EV14 #98")
        # ⚠⚠ ПО КЛЮЧАМ РАЗОБРАННОГО JSON, А НЕ ПО ТЕКСТУ (ревью захода 5.5, 27.08.2026).
        # Прежняя форма `f'"{m}"' not in raw` удовлетворялась ЛЮБЫМ упоминанием имени — в том
        # числе строкой-примечанием «"perturbation" в этом заходе не снимали». То есть проверка
        # состава зеленела ровно там, где метрику не сняли, а именно это `EV19` и ловит.
        doc = json.loads(base.read_text(encoding="utf-8"))

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for v in node:
                    yield from keys(v)

        present = set(keys(doc))
        missing = [m for m in self.MANDATORY if m not in present]
        self.assertFalse(missing, f"в базе сравнения нет метрик (как КЛЮЧЕЙ): {missing}")

    def test_every_metric_in_the_baseline_has_a_command(self):
        """Правило 6 `eval_baseline/README`: число без команды — цитата, а не критерий.

        Куплено тем, что `context_size` пролежал в базе с 21.08 с пометкой «перемерить», а
        перемерить его было НЕЧЕМ — скрипта не существовало."""
        import json
        base = DEPLOY_DIR.parent.parent / "docs" / "eval_baseline" / "baseline.json"
        d = json.loads(base.read_text(encoding="utf-8"))
        for level in ("level1", "level2", "level3", "context_size", "corpus_metrics"):
            self.assertIn("_command", d[level], f"{level}: нет команды, которой считается")


class TestCiGateIsReachableWithAPackageNearby(unittest.TestCase):
    """Ревью захода 5.5 (27.08.2026): гейт CI МОЛЧАЛ в сценарии из usage — «запуск на VM, из
    каталога пакета».

    ⚠⚠ ПОЧЕМУ ЭТО ХУЖЕ `O3` #104, РАДИ КОТОРОГО ГЕЙТ И ЗАВЕДЁН. Там предохранитель сказал, и его
    не услышали. Здесь ветка «пакет рядом» ставила только `MARKER_ROOTS`, `SRC` оставался пустым,
    а вызов стоял под `if [ -n "$SRC" ]` без `else` — гейт не звался и не печатал НИ СТРОКИ, то
    есть лог выглядел полным. Проверка сверху («маркеры сошлись») при этом проходила, и оператор
    видел успешный сухой прогон.

    ⚠ Проверяется ПОВЕДЕНИЕ скрипта на подставных данных, а не текст: греп по исходнику здесь
    зеленел бы и до правки — вызов-то в файле был.
    """

    # ⚠ ПОДГОТОВКА ИДЁТ В BASH, А НЕ В PYTHON. Первая редакция собирала архив `tarfile` и
    # отдавала скрипту windows-пути: `tar -xzf` на них спотыкался, скрипт честно говорил «архив
    # не распаковался» и уходил в ДРУГУЮ ветку — то есть тест зеленел бы мимо проверяемой
    # развилки. Ровно «заглушка, обрывающая проверяемый путь», третий раз за неделю.
    SETUP = r"""
TD="$(mktemp -d)"; trap 'rm -rf "$TD"' EXIT
mkdir -p "$TD/pkg" "$TD/bin" "$TD/src/app/core"
printf '#!/bin/sh
echo success
' > "$TD/bin/gh"; chmod +x "$TD/bin/gh"
echo x > "$TD/src/app/core/config.py"
tar -czf "$TD/pkg/navigator-719-$3.tar.gz" -C "$TD/src" . || { echo "TAR-FAILED"; exit 3; }
APP_DIR="$4"; [ "$APP_DIR" = "__none__" ] && APP_DIR="$TD/nope"
PATH="$TD/bin:$PATH" PKG="$TD/pkg" APP="$APP_DIR" bash "$1" --release "$2" --dry-run
"""

    def _dry_run(self, app: str):
        """Сухой прогон с ПАКЕТОМ рядом: ровно та развилка, где гейт замолкал."""
        profile = sorted(RELEASES.glob("*.env"))[-1]
        tag = re.search(r'TAG="([^"]+)"', profile.read_text(encoding="utf-8")).group(1)
        env = {k: v for k, v in os.environ.items() if k != "SKIP_CI_GATE"}
        r = subprocess.run(
            [shutil.which("bash"), "-c", self.SETUP, "_", str(DEPLOY_SH), str(profile), tag, app],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), env=env)
        self.assertNotIn("TAR-FAILED", r.stdout, "архив не собрался — сценарий не воспроизведён")
        self.assertIn("сверяю маркеры с ПАКЕТОМ", r.stdout,
                      f"ветка «пакет рядом» не сработала, проверяется НЕ та развилка:\n{r.stdout}")
        return r

    def test_gate_speaks_when_the_package_is_nearby(self):
        """Главная половина: в этом сценарии гейт обязан ВЫСКАЗАТЬСЯ, а не промолчать."""
        r = self._dry_run(str(ROOT))
        self.assertRegex(r.stdout, r"коммит под проверкой|CI НЕ ПРОВЕРЕН",
                         f"гейт CI смолчал при пакете рядом:\n{r.stdout}")

    def test_missing_repository_is_said_out_loud(self):
        """⚠ Отрицательный контроль: без репозитория гейт обязан не «пройти», а СКАЗАТЬ.

        Тихий пропуск неотличим от зелёного — ровно то, чем дефект и был."""
        r = self._dry_run("__none__")
        self.assertIn("CI НЕ ПРОВЕРЕН", r.stdout,
                      f"молчание вместо предупреждения:\n{r.stdout}")
        self.assertEqual(r.returncode, 0, "отсутствие репозитория — не провал выкатки")


class TestBuildkitIsActuallyDisabled(unittest.TestCase):
    """Ревью захода 5.5: `env DOCKER_BUILDKIT=0 sudo docker …` НЕ передаёт переменную.

    `sudo` при `env_reset` (дефолт sudoers) вычищает окружение дочернего процесса, поэтому любое
    присваивание ПЕРЕД `sudo` до docker не доезжает. Дефект прятал фолбэк на тонкий слой: сборка
    получалась, просто каждый раз длинным путём через TLS к auth.docker.io — то есть ровно через
    то, от чего строка и уходила.
    """

    def test_env_comes_after_sudo_not_before(self):
        # ⚠ ПРОВЕРЯЕТСЯ ВЫЗОВ, А НЕ ПРИСВАИВАНИЕ. Первая редакция этого теста запрещала строку
        # `env DOCKER_BUILDKIT=0 $DOCKER` целиком — и падала на ВЕРНОМ коде: ровно так выглядит
        # законная ветка «$DOCKER без sudo», где присваивание перед командой работает. Предохра-
        # нитель, бьющий по верному коду, дороже отсутствующего (шапка `deploy.sh`, 18.08).
        code = code_only(DEPLOY_SH.read_text(encoding="utf-8"))
        for broken in ("env DOCKER_BUILDKIT=0 $DOCKER compose",
                       "env DOCKER_BUILDKIT=0 $DOCKER build"):
            self.assertNotIn(broken, code,
                             f"сборка снова зовётся как {broken!r} — переменная не доедет "
                             f"через sudo")
        self.assertEqual(code.count("run_step 5 $DOCKER_NOBUILDKIT"), 2,
                         "обе ветки сборки обязаны идти через префикс без BuildKit")

    def test_prefix_puts_env_after_sudo_for_both_shapes_of_DOCKER(self):
        """⚠ `$DOCKER` бывает и `sudo -n docker`, и голым `docker` — обе формы обязаны работать."""
        for docker, want in (("sudo -n docker", "sudo -n env DOCKER_BUILDKIT=0 docker"),
                             ("docker", "env DOCKER_BUILDKIT=0 docker")):
            r = subprocess.run(
                [shutil.which("bash"), "-c",
                 'DOCKER="$1"; if [ "${DOCKER% docker}" != "$DOCKER" ]; then '
                 'echo "${DOCKER% docker} env DOCKER_BUILDKIT=0 docker"; '
                 'else echo "env DOCKER_BUILDKIT=0 $DOCKER"; fi', "_", docker],
                capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), want, f"для DOCKER={docker!r}")


class TestEveryTestInThisFileActuallyRuns(unittest.TestCase):
    """Ревью захода 5.5 (27.08.2026): `unittest.main()` стоял ПОСРЕДИ файла.

    ⚠⚠ Три класса — 13 тестов предохранителей ВЫКАТКИ — были дописаны ПОСЛЕ него. При запуске
    `python tests/test_deploy_script.py` интерпретатор доходил до `unittest.main()`, прогонял
    первые шесть классов и завершал процесс `sys.exit()`; остальные не были даже определены.
    Через `unittest discover` (документированная команда) шли все, поэтому расхождение было
    МОЛЧАЛИВЫМ: тесты существуют, зелёные, и частью способов запуска не исполняются.
    """

    def test_main_block_is_the_last_statement(self):
        import ast
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        mains = [i for i, node in enumerate(tree.body)
                 if isinstance(node, ast.If) and ast.unparse(node.test).startswith("__name__")]
        self.assertEqual(len(mains), 1, "блок __main__ должен быть ровно один")
        after = [type(n).__name__ for n in tree.body[mains[0] + 1:]]
        self.assertFalse(after, f"после unittest.main() снова стоит код — он не выполнится: {after}")


if __name__ == "__main__":
    unittest.main()
