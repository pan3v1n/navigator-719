r"""Скрипты не должны падать на кодировке консоли Windows. Офлайн, без сети.

ЧТО СЛУЧИЛОСЬ 24.08.2026. Новая строка в `load_rules_kb.load_records` печатала `⏭` — символ,
которого нет в **cp1251**, кодировке консоли Windows по умолчанию. Строка исполнялась при КАЖДОМ
вызове, и документированная команда переиндексации умирала `UnicodeEncodeError`'ом до первой
записи. Ревью нашло это на пятой минуте.

⚠⚠ ПОЧЕМУ Я ЭТОГО НЕ ВИДЕЛ, И ЭТО ГЛАВНЫЙ УРОК. Батарея у меня была зелёной — потому что я
запускал её с `PYTHONUTF8=1`, а `CLAUDE.md` документирует запуск БЕЗ него. Замер в среде,
настроенной под себя, — не замер. В документированной среде тех же 638 тестов давали
**2 падения и 5 ошибок**.

Второй усилитель тишины: CI на ubuntu по умолчанию UTF-8 и этот класс не видит НИКОГДА. То есть
оба контура проверки были слепы к нему одновременно.

Кириллица в cp1251 укладывается — падают только значки разметки: `⚠ ✅ → ⏭ ≥ ×`. Поэтому дефект
живёт в редко исполняемых ветках и срабатывает ровно тогда, когда скрипт пытался о чём-то
предупредить.

Запуск:  .venv\Scripts\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPTS = ROOT / "scripts"

# Скрипты, которые печатают «взрывные» символы и ЕЩЁ НЕ защищены. Это ЗАРЕГИСТРИРОВАННЫЙ ДОЛГ
# (issue #107), а не разрешение: список может только СОКРАЩАТЬСЯ. Тест ниже запрещает добавлять
# в него новые имена — то есть класс перестаёт расти с сегодняшнего дня.
#
# Почему не починены разом: правка механическая, но затрагивает 36 файлов, и делать её в конце
# длинной сессии — ровно тот способ внести дефект, от которого проект уже страдал. Чинятся по
# мере того, как файл всё равно открывается по своей задаче.
KNOWN_UNGUARDED = {
    "backfill_block_thresholds.py", "backfill_okpd2_prefixes.py", "backup_db.py",
    "build_classifiers.py", "build_gs_natural.py", "calibrate_cases.py",
    "classify_missing_thresholds.py", "diag_orphan_requirements.py", "diff_edition.py",
    "drop_excluded_positions.py", "eval_cases_influence.py", "eval_env.py",
    "eval_experiments.py", "eval_faithfulness_stress.py", "eval_text_faithfulness.py",
    "eval_topics.py", "md2docx.py", "parse_rtf.py",
    "rechunk_appendix.py", "reconcile_kb.py", "run_test_cases.py",
    "seed_users.py", "structure_kb.py", "sync_payloads.py",
    "watch_edition.py",
}


def unencodable_in_cp1251(path: Path) -> set[str]:
    """Символы файла, которые не переживут печать в консоль Windows.

    Комментарии пропускаем: они не печатаются. Строки кода — считаем целиком, включая литералы
    внутри f-строк и таблиц вывода."""
    bad: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        for ch in line:
            if ord(ch) < 128:
                continue
            try:
                ch.encode("cp1251")
            except UnicodeEncodeError:
                bad.add(ch)
    return bad


def risky_scripts() -> dict[str, set[str]]:
    return {p.name: bad for p in sorted(SCRIPTS.glob("*.py"))
            if (bad := unencodable_in_cp1251(p))}


class TestScriptsSurviveWindowsConsole(unittest.TestCase):
    def test_no_new_unguarded_script(self):
        """Список долга может только сокращаться: новый скрипт со значками обязан звать `enable_utf8`."""
        unguarded = {name for name, _ in risky_scripts().items()
                     if "enable_utf8()" not in (SCRIPTS / name).read_text(encoding="utf-8")}
        new = unguarded - KNOWN_UNGUARDED
        self.assertEqual(new, set(),
                         "скрипт печатает символы вне cp1251 и упадёт на Windows: "
                         "добавьте `enable_utf8()` (см. app/core/console.py)")

    def test_debt_list_has_no_stale_names(self):
        """Починенный скрипт обязан УЙТИ из списка — иначе долг перестаёт быть измеримым."""
        stale = set()
        for name in KNOWN_UNGUARDED:
            path = SCRIPTS / name
            if not path.exists() or "enable_utf8()" in path.read_text(encoding="utf-8"):
                stale.add(name)
        self.assertEqual(stale, set(), "скрипты уже защищены — уберите их из KNOWN_UNGUARDED")

    def test_scripts_the_battery_exercises_are_guarded(self):
        """⚠ Минимум, ниже которого опускаться нельзя: скрипты, которые батарея ИМПОРТИРУЕТ и
        ЗОВЁТ, обязаны быть защищены — иначе документированный прогон снова покраснеет там, где
        мой прогон с `PYTHONUTF8=1` останется зелёным."""
        for name in ("load_rules_kb.py", "build_expert_checklist.py", "verify_structured.py",
                     "load_kb.py"):  # load_kb — команда переиндексации, нужна ближайшей выкатке
            with self.subTest(script=name):
                src = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("enable_utf8()", src)


class TestEnableUtf8Itself(unittest.TestCase):
    def test_survives_a_substituted_stream(self):
        """Тесты подменяют stdout на `io.StringIO` — у него нет `reconfigure`, и помощник
        обязан это пережить молча, а не уронить импорт скрипта."""
        import io

        from app.core.console import enable_utf8

        orig = sys.stdout
        sys.stdout = io.StringIO()
        try:
            enable_utf8()  # не должно бросить
        finally:
            sys.stdout = orig

    def test_is_idempotent(self):
        from app.core.console import enable_utf8

        enable_utf8()
        enable_utf8()


if __name__ == "__main__":
    unittest.main()
