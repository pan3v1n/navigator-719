"""`O10` #188, шаг 1: забор копии боевой базы на ПК. Офлайн — ssh подменён.

Проверяется то, что делает забор бесполезным, если сломается: копия с оборванным каналом,
битая база, база внутри репозитория (ПДн), молчание при неработающем ночном бэкапе сервера.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import pull_backup as pb  # noqa: E402

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
NAME = "navigator_app-2026-10-27_0300.db"


def _db_bytes(users: int = 2) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.db"
        conn = sqlite3.connect(str(p))
        conn.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY);"
                           "CREATE TABLE messages(id INTEGER PRIMARY KEY);"
                           "CREATE TABLE feedback(id INTEGER PRIMARY KEY);")
        for i in range(users):
            conn.execute("INSERT INTO users(id) VALUES (?)", (i + 1,))
        conn.commit()
        conn.close()
        return p.read_bytes()


def _ssh(data: bytes, name: str = NAME, sha: str | None = None, code: int = 0, extra: str = ""):
    """Подменённый `subprocess.run`: что сервер прислал бы по одному подключению."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        meta = f"{extra}NAME {name}\nSHA {sha or hashlib.sha256(data).hexdigest()}\n"
        return SimpleNamespace(returncode=code, stdout=data, stderr=meta.encode("utf-8"))
    run.calls = calls
    return run


class TestPullBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "backups"
        self.log = []

    def tearDown(self):
        self.tmp.cleanup()

    def _pull(self, run, **kw):
        return pb.pull(self.dest, run=run, now=NOW, say=self.log.append, **kw)

    def test_fresh_copy_is_saved_and_verified(self):
        run = _ssh(_db_bytes())
        self.assertEqual(self._pull(run), pb.OK, self.log)
        self.assertTrue((self.dest / NAME).exists())
        self.assertFalse(list(self.dest.glob("*.part")), "временный файл не убран")
        self.assertEqual(len(run.calls), 1, "подключение обязано быть ОДНО — канал режет по частоте")

    def test_hash_mismatch_saves_nothing(self):
        """Сумма не сошлась — копию не сохраняем, даже если сама база читается.

        ⚠ Данные здесь — ЦЕЛАЯ база с чужой суммой (файл менялся во время чтения). Оборванный
        файл для этой проверки не годится: его отсеяла бы и `verify_backup`, и тест зеленел бы
        при снятой сверке суммы."""
        run = _ssh(_db_bytes(), sha="0" * 64)
        self.assertEqual(self._pull(run), pb.FAILED)
        self.assertFalse(list(self.dest.glob("navigator_app-*")), "копия с чужой суммой сохранена")

    def test_broken_database_saves_nothing(self):
        run = _ssh(b"not a sqlite database" * 100)
        self.assertEqual(self._pull(run), pb.FAILED)
        self.assertFalse(list(self.dest.iterdir()), "битая копия или её .part остались")

    def test_empty_database_is_rejected(self):
        """Целостность ok, но пользователей 0 — скопирована не та база."""
        run = _ssh(_db_bytes(users=0))
        self.assertEqual(self._pull(run), pb.FAILED)
        self.assertFalse((self.dest / NAME).exists())

    def test_ssh_failure_is_a_failure(self):
        run = _ssh(b"", code=255, extra="ssh: connect to host: Connection timed out\n")
        self.assertEqual(self._pull(run), pb.FAILED)

    def test_no_backup_on_server_is_said(self):
        def run(cmd, **kw):
            return SimpleNamespace(returncode=3, stdout=b"", stderr=b"NO_BACKUP\n")
        self.assertEqual(self._pull(run), pb.FAILED)
        self.assertTrue(any("нет ни одной копии" in s for s in self.log), self.log)

    def test_stale_backup_is_a_third_outcome(self):
        """Копия цела, но старая: забор удался, а ночной бэкап сервера — нет. Это тревога, не «ок»."""
        old = "navigator_app-2026-10-24_0300.db"
        self.assertEqual(self._pull(_ssh(_db_bytes(), name=old)), pb.STALE)
        self.assertTrue((self.dest / old).exists(), "старую, но целую копию всё равно сохраняем")

    def test_destination_inside_repository_is_refused(self):
        inside = ROOT / "backups_test_should_not_exist"
        # если отказ сломан, папка с «копией» появится в рабочем дереве — убрать её в любом случае
        self.addCleanup(shutil.rmtree, inside, ignore_errors=True)
        run = _ssh(_db_bytes())
        code = pb.pull(inside, run=run, now=NOW, say=self.log.append)
        self.assertEqual(code, pb.FAILED)
        self.assertEqual(run.calls, [], "до отказа не должно быть подключения")
        self.assertFalse(inside.exists())

    def test_rotation_removes_only_old_dated_copies(self):
        self.dest.mkdir(parents=True)
        old = self.dest / "navigator_app-2026-09-01_0300.db"
        mid = self.dest / "navigator_app-2026-10-10_0300.db"
        foreign = self.dest / "notes.txt"
        for p in (old, mid, foreign):
            p.write_bytes(b"x")
        self.assertEqual(self._pull(_ssh(_db_bytes())), pb.OK, self.log)
        self.assertFalse(old.exists(), "копия старше 30 дней не удалена")
        self.assertTrue(mid.exists(), "удалена копия моложе срока")
        self.assertTrue(foreign.exists(), "удалён чужой файл")

    def test_rotation_never_removes_the_newest_copy(self):
        self.dest.mkdir(parents=True)
        only = self.dest / "navigator_app-2026-08-01_0300.db"
        only.write_bytes(b"x")
        self.assertEqual(pb.rotate(self.dest, 30, NOW), [])
        self.assertTrue(only.exists())

    def test_same_copy_is_not_written_twice(self):
        data = _db_bytes()
        self.assertEqual(self._pull(_ssh(data)), pb.OK)
        mtime = (self.dest / NAME).stat().st_mtime_ns
        self.assertEqual(self._pull(_ssh(data)), pb.OK)
        self.assertEqual((self.dest / NAME).stat().st_mtime_ns, mtime)

    def test_remote_command_does_not_eat_stdin_and_reads_only(self):
        cmd = pb.remote_command("navigator-719", "sudo -n docker")
        self.assertIn("< /dev/null", cmd)
        self.assertIn("/data/backups/navigator_app-", cmd)
        for verb in (" rm ", " mv ", "backup_db.py", " > /data"):
            self.assertNotIn(verb, cmd, "забор обязан только читать сервер")


if __name__ == "__main__":
    unittest.main()
