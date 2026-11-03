"""`O10` #188, шаг 1: забор копии боевой базы на ПК. Офлайн — ssh подменён.

Проверяется то, что делает забор бесполезным, если сломается: копия с оборванным каналом,
битая база, база внутри репозитория (ПДн), молчание при неработающем ночном бэкапе сервера.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
    """Подменённый `subprocess.run`: что сервер прислал бы по одному подключению.

    Файл пишется в переданный `stdout` — как настоящий ssh: скрипт отдаёт ему файл, а не канал
    (в канал ssh из Git для Windows терял хвост — первый боевой забор, 27.10)."""
    calls = []

    def run(cmd, stdout=None, **kw):
        calls.append(cmd)
        assert stdout is not None and hasattr(stdout, "write"), "ssh обязан писать в файл, а не в канал"
        stdout.write(data)
        meta = f"{extra}NAME {name}\nSHA {sha or hashlib.sha256(data).hexdigest()}\n"
        return SimpleNamespace(returncode=code, stdout=None, stderr=meta.encode("utf-8"))
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
            return SimpleNamespace(returncode=3, stdout=None, stderr=b"NO_BACKUP\n")
        self.assertEqual(self._pull(run), pb.FAILED)
        self.assertTrue(any("нет ни одной копии" in s for s in self.log), self.log)
        self.assertFalse(list(self.dest.iterdir()), "после отказа остался временный файл")

    def test_truncated_stream_saves_nothing(self):
        """Хвост потерян в пути (так было на первом боевом заборе): сумма не сходится — не сохраняем."""
        data = _db_bytes()
        run = _ssh(data[:-20480] if len(data) > 20480 else data[:-1],
                   sha=hashlib.sha256(data).hexdigest())
        self.assertEqual(self._pull(run), pb.FAILED)
        self.assertFalse(list(self.dest.iterdir()), "оборванная копия или её .part остались")

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


# Адаптеры как на рабочем ПК 03.11.2026 (адреса заменены документационными, RFC 5737): туннель
# Happ, два Hyper-V, Bluetooth и Wi-Fi без сети — и один настоящий выход, Ethernet.
def _adapter(alias, ip, gw=(), status="Up", hardware=True, virtual=False, metric=25, route_metric=None, index=7):
    return {"alias": alias, "description": alias, "index": index, "status": status,
            "hardware": hardware, "virtual": virtual, "ipv4": list(ip) if isinstance(ip, (list, tuple)) else [ip],
            "gateways": list(gw), "metric": metric, "route_metric": route_metric}


ETHERNET = _adapter("Ethernet", "192.0.2.12", ["192.0.2.1"], metric=25, route_metric=0, index=7)
WORKSTATION = [
    _adapter("Сетевое подключение Bluetooth", "169.254.188.59", status="Disconnected",
             hardware=False, virtual=True, metric=65, index=23),
    _adapter("Беспроводная сеть", "169.254.49.181", status="Disconnected", metric=25, index=18),
    _adapter("vEthernet (WSL (Hyper-V firewall))", "198.51.100.1", hardware=False, virtual=True,
             metric=5000, index=51),
    _adapter("happ-xray", "198.51.100.77", ["0.0.0.0"], hardware=False, virtual=True,
             metric=None, route_metric=0, index=27),
    ETHERNET,
]
# Ловушки: у каждой МЕНЬШАЯ метрика, чем у Ethernet, — выбрать её значит обойти ровно одно правило
# отбора. Туннель с настоящим шлюзом (как у VPN), программный адаптер без отметки «виртуальный»,
# «железный», но помеченный виртуальным, Wi-Fi с адресом и шлюзом, но выключенный, адрес 169.254 со
# шлюзом, физический без шлюза, физический с одним маршрутом «на линии» (0.0.0.0 — так у TUN).
TRAPS = [
    _adapter("vpn", "203.0.113.2", ["203.0.113.1"], hardware=False, virtual=True, metric=1, index=2),
    _adapter("software", "203.0.113.3", ["203.0.113.1"], hardware=False, virtual=False, metric=1, index=3),
    _adapter("hw-virtual", "203.0.113.7", ["203.0.113.1"], hardware=True, virtual=True, metric=1, index=8),
    _adapter("wifi-off", "203.0.113.4", ["203.0.113.1"], status="Disconnected", metric=1, index=4),
    _adapter("apipa", "169.254.7.7", ["169.254.7.1"], metric=1, index=5),
    _adapter("no-gateway", "203.0.113.6", [], metric=1, index=6),
    _adapter("on-link", "203.0.113.8", ["0.0.0.0"], metric=1, index=9),
]


class TestBindAddress(unittest.TestCase):
    """02.11.2026: ssh к бою через туннель Happ не проходит — забор привязывается к физическому адаптеру."""

    def test_auto_picks_ethernet_not_tunnel(self):
        ip, note = pb.pick_adapter(WORKSTATION)
        self.assertEqual(ip, "192.0.2.12")
        self.assertIn("Ethernet", note)

    def test_each_rule_of_selection_holds(self):
        """Каждая ловушка лучше Ethernet по метрике — выбор ловушки = снятое правило отбора."""
        for trap in TRAPS:
            with self.subTest(trap=trap["alias"]):
                ip, _ = pb.pick_adapter([trap, ETHERNET])
                self.assertEqual(ip, "192.0.2.12", f"выбран «{trap['alias']}»")

    def test_no_physical_exit_is_an_error_not_a_guess(self):
        only_virtual = [a for a in WORKSTATION if a is not ETHERNET] + TRAPS
        with self.assertRaises(pb.BindError) as cm:
            pb.pick_adapter(only_virtual)
        self.assertIn("Беспроводная сеть", str(cm.exception), "ошибка обязана назвать, какие адаптеры видны")
        with self.assertRaises(pb.BindError):
            pb.pick_adapter([])

    def test_lower_metric_wins_and_choice_is_said(self):
        wifi = _adapter("Беспроводная сеть", "192.0.2.40", ["192.0.2.1"], metric=35, index=18)
        ip, note = pb.pick_adapter([wifi, ETHERNET])
        self.assertEqual(ip, "192.0.2.12")
        self.assertIn("из 2", note)
        wifi_first = dict(wifi, metric=5)
        self.assertEqual(pb.pick_adapter([ETHERNET, wifi_first])[0], "192.0.2.40")

    def test_resolve_explicit_none_and_garbage(self):
        never = mock.Mock(side_effect=AssertionError("адаптеры не должны опрашиваться"))
        self.assertEqual(pb.resolve_bind("192.0.2.50", adapters=never, windows=True)[0], "192.0.2.50")
        for off in ("none", "NONE", "off", ""):
            self.assertIsNone(pb.resolve_bind(off, adapters=never, windows=True)[0], off)
        with self.assertRaises(pb.BindError) as cm:
            pb.resolve_bind("192.0.2.", adapters=never, windows=True)
        self.assertIn("--bind", str(cm.exception))

    def test_auto_outside_windows_means_no_bind(self):
        never = mock.Mock(side_effect=AssertionError("вне Windows PowerShell не зовём"))
        addr, note = pb.resolve_bind("auto", adapters=never, windows=False)
        self.assertIsNone(addr)
        self.assertIn("только на Windows", note)

    def test_auto_on_windows_uses_adapters_and_error_says_how_to_fix(self):
        self.assertEqual(pb.resolve_bind("AUTO", adapters=lambda: WORKSTATION, windows=True)[0], "192.0.2.12")
        with self.assertRaises(pb.BindError) as cm:
            pb.resolve_bind("auto", adapters=lambda: [], windows=True)
        self.assertIn(pb.BIND_ENV, str(cm.exception))
        self.assertIn("--bind none", str(cm.exception))


def _powershell(stdout: bytes = b"", code: int = 0, stderr: bytes = b"", exc=None):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if exc:
            raise exc
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
    run.calls = calls
    return run


class TestListAdapters(unittest.TestCase):
    def test_json_with_bom_and_single_object(self):
        """PowerShell с UTF-8 ставит BOM, а один адаптер может отдать объектом, а не списком."""
        one = json.dumps(_adapter("Ethernet", "192.0.2.12", ["192.0.2.1"]), ensure_ascii=False)
        run = _powershell(("\ufeff" + one + "\r\n").encode("utf-8"))
        got = pb.list_adapters(run=run)
        self.assertEqual([a["alias"] for a in got], ["Ethernet"])
        cyr = json.dumps([_adapter("Беспроводная сеть", "192.0.2.40")], ensure_ascii=False)
        self.assertEqual(pb.list_adapters(run=_powershell(cyr.encode("utf-8")))[0]["alias"], "Беспроводная сеть")

    def test_script_goes_encoded_and_asks_the_right_fields(self):
        run = _powershell(b"[]")
        self.assertEqual(pb.list_adapters(run=run), [])
        cmd = run.calls[0]
        self.assertIn("-EncodedCommand", cmd)
        script = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
        for field in ("Get-NetAdapter", "HardwareInterface", "Virtual", "0.0.0.0/0", "UTF8"):
            self.assertIn(field, script)

    def test_failures_become_bind_errors(self):
        for run in (_powershell(code=1, stderr=b"Get-NetAdapter : not found"),
                    _powershell(b""),
                    _powershell(b"WARNING: not json"),
                    _powershell(b'"text"'),
                    _powershell(exc=FileNotFoundError("powershell.exe"))):
            with self.subTest(run=run), self.assertRaises(pb.BindError):
                pb.list_adapters(run=run)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "только Windows")
    def test_real_powershell_returns_adapters(self):
        """Настоящий PowerShell этого ПК: скрипт исполняется и отдаёт поля, по которым идёт отбор.
        Выбор адреса не проверяется: у машины без сети его законно нет."""
        got = pb.list_adapters()
        self.assertTrue(got, "Get-NetAdapter не вернул ни одного адаптера")
        for a in got:
            self.assertTrue({"alias", "status", "hardware", "virtual", "ipv4", "gateways"} <= set(a), a)


class TestPullWithBind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "backups"
        self.log = []

    def tearDown(self):
        self.tmp.cleanup()

    def _pull(self, run, **kw):
        return pb.pull(self.dest, run=run, now=NOW, say=self.log.append, **kw)

    def test_bound_address_goes_to_ssh_before_host(self):
        run = _ssh(_db_bytes())
        resolve = lambda b: ("192.0.2.12", "ssh мимо туннеля — привязка к адаптеру «Ethernet» (192.0.2.12)")
        self.assertEqual(self._pull(run, bind="auto", resolve=resolve), pb.OK, self.log)
        cmd = run.calls[0]
        self.assertIn("-b", cmd)
        i = cmd.index("-b")
        self.assertEqual(cmd[i + 1], "192.0.2.12")
        self.assertLess(i, cmd.index(pb.DEFAULT_HOST), "ключ ssh после адреса сервера ушёл бы командой на сервер")
        self.assertTrue(any("Ethernet" in s for s in self.log), "в журнале не видно, через что шли")

    def test_without_bind_ssh_is_unchanged(self):
        run = _ssh(_db_bytes())
        self.assertEqual(self._pull(run), pb.OK)
        self.assertNotIn("-b", run.calls[0])
        none = _ssh(_db_bytes())
        self.assertEqual(self._pull(none, bind="none", resolve=pb.resolve_bind), pb.OK)
        self.assertNotIn("-b", none.calls[0])

    def test_bind_error_means_no_connection(self):
        run = _ssh(_db_bytes())

        def resolve(b):
            raise pb.BindError("нет включённого физического адаптера")
        self.assertEqual(self._pull(run, bind="auto", resolve=resolve), pb.FAILED)
        self.assertEqual(run.calls, [], "без адреса привязки ssh ушёл бы в туннель")
        self.assertFalse(self.dest.exists() and list(self.dest.iterdir()), "после отказа что-то записано")
        self.assertTrue(any("нет включённого" in s for s in self.log), self.log)

    def test_hints_name_the_cause(self):
        bound = lambda b: ("192.0.2.12", "явно")
        run = _ssh(b"", code=255, extra="bind 192.0.2.12: Cannot assign requested address\n")
        self.assertEqual(self._pull(run, bind="192.0.2.12", resolve=bound), pb.FAILED)
        self.assertTrue(any("не принадлежит" in s for s in self.log), self.log)

        self.log.clear()
        run = _ssh(b"", code=255, extra="Connection timed out during banner exchange\n")
        self.assertEqual(self._pull(run), pb.FAILED)
        self.assertTrue(any("--bind auto" in s for s in self.log), self.log)

        self.log.clear()
        run = _ssh(b"", code=255, extra="Connection timed out during banner exchange\n")
        self.assertEqual(self._pull(run, bind="auto", resolve=bound), pb.FAILED)
        self.assertTrue(any("мимо туннеля" in s for s in self.log), self.log)
        self.assertFalse(any("--bind auto" in s for s in self.log), "совет «включи привязку» при включённой")


class TestCommandLine(unittest.TestCase):
    def _main(self, argv, env=None):
        got = {}

        def fake_pull(dest, *a, **kw):
            got.update(kw, dest=dest)
            return pb.OK
        env = {k: v for k, v in os.environ.items() if k != pb.BIND_ENV} | (env or {})
        with mock.patch.object(sys, "argv", ["pull_backup.py", *argv]), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(pb, "pull", fake_pull), \
                self.assertRaises(SystemExit) as cm:
            pb.main()
        return cm.exception.code, got

    def test_default_is_auto(self):
        code, got = self._main(["--dest", "X:/b"])
        self.assertEqual(code, pb.OK)
        self.assertEqual(got["bind"], "auto")

    def test_env_and_argument_override(self):
        self.assertEqual(self._main(["--dest", "X:/b"], {pb.BIND_ENV: "none"})[1]["bind"], "none")
        self.assertEqual(self._main(["--dest", "X:/b", "--bind", "192.0.2.5"],
                                    {pb.BIND_ENV: "none"})[1]["bind"], "192.0.2.5")

    def test_bad_arguments_are_failed_not_stale(self):
        """Код 2 = «ночной бэкап сервера не работает»; опечатка в задаче — не тревога о сервере."""
        with mock.patch.object(sys, "stderr", new=SimpleNamespace(write=lambda s: None, flush=lambda: None)):
            code, _ = self._main(["--no-such-flag"])
        self.assertEqual(code, pb.FAILED)


if __name__ == "__main__":
    unittest.main()
