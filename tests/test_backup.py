"""R13: бэкап БД приложения обязан проверяться сразу после снятия.

Данные теста (оценки экспертов, 26 исправлений, диалоги 17 региональных ТПП) — главный актив
проекта; второй попытки их собрать не будет. Бэкап, о непригодности которого узнаёшь в момент
восстановления, — это отсутствие бэкапа, поэтому проверка встроена в снятие, а не отложена.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from backup_db import verify_backup  # noqa: E402


def _make_db(path: Path, users: int = 1) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE users(id INTEGER PRIMARY KEY);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY);"
        "CREATE TABLE feedback(id INTEGER PRIMARY KEY);"
    )
    for i in range(users):
        conn.execute("INSERT INTO users(id) VALUES (?)", (i + 1,))
    conn.commit()
    conn.close()


class TestVerifyBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_good_backup_passes(self):
        p = self.dir / "ok.db"
        _make_db(p, users=3)
        ok, report = verify_backup(p)
        self.assertTrue(ok, report)
        self.assertIn("users 3", report)

    def test_missing_file_fails(self):
        ok, report = verify_backup(self.dir / "нет.db")
        self.assertFalse(ok)
        self.assertIn("файла нет", report)

    def test_not_a_database_fails_without_raising(self):
        # sqlite3.connect ленив: на не-базе исключение прилетает на ПЕРВОМ запросе,
        # и проверка не должна падать вместе с ним
        p = self.dir / "broken.db"
        p.write_text("это не база", encoding="utf-8")
        ok, report = verify_backup(p)
        self.assertFalse(ok)
        self.assertIn("не читается как база", report)

    def test_empty_but_valid_db_fails(self):
        # «успешно снятая» пустая база — типичный результат копирования не того пути;
        # целостность у неё ok, поэтому одной PRAGMA недостаточно
        p = self.dir / "empty.db"
        _make_db(p, users=0)
        ok, report = verify_backup(p)
        self.assertFalse(ok)
        self.assertIn("пользователей 0", report)

    def test_missing_table_fails(self):
        p = self.dir / "partial.db"
        conn = sqlite3.connect(str(p))
        conn.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY);")
        conn.execute("INSERT INTO users(id) VALUES (1)")
        conn.commit()
        conn.close()
        ok, report = verify_backup(p)
        self.assertFalse(ok)
        self.assertIn("нет таблицы", report)


if __name__ == "__main__":
    unittest.main()
