"""Забор свежей копии боевой базы на этот ПК — `O10` #188, шаг 1 (вариант А `docs/DEPLOY.md` §9).

ЗАЧЕМ. Ночной cron на сервере пишет копии в том `/data/backups` — на ТОТ ЖЕ диск, что и сама
база. Последний вынос наружу делался руками 14.08.2026; при переезде рабочего места 21.10 копии и
выгрузки остались на прежнем ПК, и на 27.10 вне сервера не было НИ ОДНОЙ копии. Потеря диска
сервера = потеря всех данных с 15.10.

КАК. ОДНО ssh-подключение (канал до сервера режет по частоте подключений): на сервере берётся
самая свежая копия `navigator_app-*.db` из тома, её имя и sha256 уходят в stderr, сам файл — в
stdout. Здесь: сверка sha256 (обрыв канала на середине файла не должен выглядеть копией), затем
`backup_db.verify_backup` (целостность SQLite и непустые `users/messages/feedback`), только потом
переименование в итоговое имя. Сервер скрипт не меняет — только читает.

ТРИ ИСХОДА, А НЕ ДВА (урок 30.08: у всякой пробы три исхода). Код 0 — свежая копия забрана и
проверена; код 2 — копия забрана и цела, но СТАРАЯ (ночной cron на сервере не отработал), это
тревога о сервере, а не о заборе; код 1 — копии нет (канал, сумма, битая база, нет каталога).

⚠ 152-ФЗ. В копии ПДн пользователей. `--dest` обязан лежать ВНЕ репозитория (иначе отказ) и на
зашифрованном носителе (BitLocker / VeraCrypt — проверить это скрипт не может, это на владельце).

⚠⚠ ТУННЕЛЬ (02.11.2026). На рабочем ПК весь трафик идёт через туннель Happ (TUN `happ-xray`), и ssh к
бою через него перестал проходить: «Connection timed out during banner exchange», забор падал каждые
сутки. Мимо туннеля вход сразу. Поэтому ssh привязывается к адресу ФИЗИЧЕСКОГО адаптера (`ssh -b`):
  --bind auto      (по умолчанию; пустое значение — тоже auto) найти адрес самому: Windows, включённый
                   адаптер с HardwareInterface, не виртуальный, IPv4 не из 169.254/16, со шлюзом по
                   умолчанию; подходящих несколько — с меньшей суммой метрик интерфейса и маршрута. Нет
                   такого — ждать до --wait-adapter секунд (по умолчанию 180, опрос раз в 15 с: после
                   пробуждения ПК сеть поднимается не сразу, а попытка одна в сутки), затем понятная
                   ошибка и код 1 ДО подключения. Вне Windows `auto` = без привязки.
  --bind <адрес>   явно (адрес адаптера — в `ipconfig`). Заведомо негодный адрес (0.0.0.0, петля,
                   169.254/16, групповой, не IPv4) — отказ до подключения; адрес не физического
                   адаптера со шлюзом (туннель) или ничей — забор идёт, в журнале `[!]` предупреждение.
  --bind none      без привязки — только так, пустое значение привязку НЕ отключает.
То же — переменной окружения NAVIGATOR_SSH_BIND. Адрес в код не зашит: его раздаёт DHCP, он сменится.

Запуск:
  .venv\\Scripts\\python scripts\\pull_backup.py --dest E:\\navigator-backups
По расписанию — Планировщик заданий Windows, раз в сутки после 03:00 UTC (06:00 МСК), например:
  schtasks /Create /SC DAILY /ST 09:00 /TN navigator-pull-backup ^
    /TR "D:\\navigator-719\\.venv\\Scripts\\python.exe D:\\navigator-719-backup\\scripts\\pull_backup.py --dest E:\\navigator-backups"
⚠ Скрипт для расписания — из ОТДЕЛЬНОЙ detached-копии (`git worktree add --detach D:\\navigator-719-backup
origin/dev`), а не из основной: основная стоит на чужих рабочих ветках и посреди слияний, и задача
бралась бы из той версии, какая там окажется. В обёртке `.cmd` путь к скрипту — ИМЕННО в этой копии
(`D:\\navigator-719-backup\\scripts\\pull_backup.py`): сама копия без правки пути ничего не меняет.
Копию обновлять осознанно, после слияния правки забора:
  git -C D:/navigator-719 fetch origin && git -C D:/navigator-719-backup checkout --detach origin/dev
⚠ Обёртка `.cmd` с русскими комментариями — только в CRLF: в LF-файле cmd теряет позицию строки и
исполняет обрывки комментариев как команды. `chcp 65001` ей не нужен: вывод PowerShell скрипт сам
пишет в UTF-8, кодировку вывода Python задаёт `PYTHONIOENCODING=utf-8`.
⚠ Проверка «`--dest` вне репозитория» смотрит на копию, из которой запущен скрипт; держите `--dest`
вне ЛЮБОЙ копии репозитория.
⚠ 29.10.2026: ПК спал в 09:00, и «запускать пропущенное» (StartWhenAvailable) после пробуждения запуск
не догнал — копии за сутки не было. Задаче нужны ещё триггеры «вход» и «разблокировка», а обёртке
(`D:\\navigator-backups\\pull-backup-daily.cmd`) — одна попытка в сутки по файлу `last-attempt.txt`:
подключение к бою разрешено раз в сутки, какой бы триггер ни сработал первым.
⚠ 04.11.2026: отметку `last-attempt.txt` ставит САМ скрипт непосредственно перед ssh (`--mark-attempt ФАЙЛ
--mark-value "%DATE%"`), обёртка её только сверяет. Прежде обёртка ставила её до запуска, и запуск, прерванный
до подключения, сжигал попытку дня. Обёртка задаёт `PYTHONUNBUFFERED=1`: иначе журнал прерванного запуска
пуст — Python держал вывод в буфере. Задача Планировщика запускает обёртку СКРЫТО (`powershell -WindowStyle
Hidden -Command "& cmd.exe /c '<обёртка>'; exit $LASTEXITCODE"`): у задачи «только при входе пользователя»
иначе на рабочем столе открывается окно консоли, и его закрытие убивает забор. `conhost --headless` не
годится — теряет код выхода.
"""

from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backup_db import verify_backup  # noqa: E402

DEFAULT_HOST = "dan_tpp_srv@77.241.30.132"
DEFAULT_APP_DIR = "navigator-719"          # каталог проекта в домашнем каталоге на сервере
DEFAULT_DOCKER = "sudo -n docker"          # как у deploy.sh: `DOCKER="${DOCKER:-sudo -n docker}"`
NAME_RE = re.compile(r"^navigator_app-(\d{4}-\d{2}-\d{2})_(\d{4})\.db$")

OK, FAILED, STALE = 0, 1, 2

BIND_ENV = "NAVIGATOR_SSH_BIND"
NO_BIND = {"none", "off", "no"}           # пустое значение — auto, а не отключение
DEFAULT_WAIT_ADAPTER = 180                 # с: ждать адаптер после пробуждения (лимит задачи 30 мин, ssh сам до 10)
_BIND_HELP = ("Адрес адаптера Ethernet — в `ipconfig`; задать явно: --bind <адрес> или переменная "
              f"{BIND_ENV}; без привязки: --bind none")


class BindError(Exception):
    """Адрес привязки ssh определить нельзя. Подключение не пробуем: без привязки ssh ушёл бы в
    туннель и через 30 с упал бы таймаутом, который о настоящей причине ничего не говорит."""


def attempt_marker(path: Path, value: str):
    """Отметка «сегодня к бою подключались» — файл, который обёртка `.cmd` сверяет с `%DATE%`.

    ЗАЧЕМ ЗДЕСЬ, А НЕ В ОБЁРТКЕ (04.11.2026). Разрешение владельца — одно подключение к бою в сутки. Обёртка
    ставила отметку ДО запуска скрипта, и запуск, прерванный ещё до ssh (окно консоли закрыли через секунды
    после пробуждения ПК), сжёг попытку дня: копии за 04.11 не было, повторов до утра — тоже. Теперь отметка
    ставится непосредственно перед ssh: «отмечено» = «подключались», прерванный раньше запуск повторит
    следующий триггер.
    `value` — строка `%DATE%` от самой обёртки: формат даты у cmd зависит от языка системы, своя дата Python
    могла бы не совпасть ни разу — и тогда к бою подключались бы на каждом триггере. Кодировка — OEM:
    `set /p` читает файл в ней (так файл писал прежний `echo %DATE%`)."""
    encoding = "oem" if os.name == "nt" else "utf-8"

    def mark() -> None:
        path.write_text(f"{value}\r\n", encoding=encoding, errors="replace", newline="")
    return mark


# Все адаптеры одним вызовом PowerShell (модуль NetAdapter есть в Windows 8+). Отбор «физический»
# делается В PYTHON по полям hardware / virtual, а не ключом `-Physical`: так правило проверяется
# тестом без Windows. Вывод — JSON в UTF-8: имена адаптеров здесь кириллицей («Беспроводная сеть»).
# ⚠ UTF-8 пишется БАЙТАМИ прямо в stdout, а не через `[Console]::OutputEncoding = UTF8`: то
# присваивание переключает кодовую страницу ОБЩЕЙ консоли (ей пользуется и обёртка `.cmd` под
# Планировщиком) и обратно её не возвращает (ревью #289, 03.11.2026).
_PS_ADAPTERS = r"""
$r = @(foreach ($a in Get-NetAdapter) {
  $i = $a.ifIndex
  $routes = @(Get-NetRoute -InterfaceIndex $i -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
  [pscustomobject]@{
    alias = $a.Name; description = $a.InterfaceDescription; index = $i; status = [string]$a.Status
    hardware = [bool]$a.HardwareInterface; virtual = [bool]$a.Virtual
    ipv4 = @(Get-NetIPAddress -InterfaceIndex $i -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object { $_.IPAddress })
    gateways = @($routes | ForEach-Object { $_.NextHop })
    metric = (Get-NetIPInterface -InterfaceIndex $i -AddressFamily IPv4 -ErrorAction SilentlyContinue).InterfaceMetric
    route_metric = ($routes | Measure-Object -Property RouteMetric -Minimum).Minimum
  }
})
$bytes = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $r -Compress -Depth 3))
$stdout = [Console]::OpenStandardOutput()
$stdout.Write($bytes, 0, $bytes.Length)
$stdout.Flush()
"""


def list_adapters(run=subprocess.run) -> list[dict]:
    """Сетевые адаптеры этого ПК (Windows). Любой сбой — BindError с причиной."""
    encoded = base64.b64encode(_PS_ADAPTERS.encode("utf-16-le")).decode("ascii")
    try:
        res = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-EncodedCommand", encoded], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BindError(f"список адаптеров не получен — PowerShell не выполнился: {type(e).__name__}: {e}") from e
    out = (res.stdout or b"").decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    if res.returncode != 0 or not out:
        err = (res.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
        raise BindError(f"список адаптеров не получен — PowerShell вернул код {res.returncode}: "
                        f"{err or 'пустой вывод'}")
    try:
        data = json.loads(out)
    except ValueError:
        raise BindError(f"список адаптеров не получен — PowerShell прислал не JSON: {out[:200]!r}") from None
    if isinstance(data, dict):        # один адаптер PowerShell может отдать объектом, а не списком
        data = [data]
    if not isinstance(data, list):
        raise BindError(f"список адаптеров не получен — неожиданный ответ PowerShell: {out[:200]!r}")
    return [d for d in data if isinstance(d, dict)]


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _usable_ipv4(text) -> bool:
    try:
        ip = ipaddress.ip_address(str(text))
    except ValueError:
        return False
    return (ip.version == 4 and not ip.is_link_local and not ip.is_loopback
            and not ip.is_unspecified and not ip.is_multicast)


def _has_gateway(gateways) -> bool:
    """Шлюз по умолчанию — настоящий адрес. `0.0.0.0` (маршрут «на линии», так у TUN) — не шлюз."""
    for g in _as_list(gateways):
        try:
            if not ipaddress.ip_address(str(g)).is_unspecified:
                return True
        except ValueError:
            continue
    return False


def _metric(a: dict) -> int:
    """Чем меньше, тем раньше Windows выбирает этот выход (метрика интерфейса + метрика маршрута)."""
    m, rm = a.get("metric"), a.get("route_metric")
    m = m if isinstance(m, int) and not isinstance(m, bool) else 10 ** 6
    rm = rm if isinstance(rm, int) and not isinstance(rm, bool) else 0
    return m + rm


def _exit_ips(a: dict) -> list[str]:
    """Адреса, через которые этот адаптер выводит ПК в сеть, — или пусто, если он не годится.

    Годится адаптер: физический (`hardware` и не `virtual` — туннель Happ, Hyper-V, Bluetooth
    виртуальные), включён (`Up`), с IPv4 не из 169.254/16 (такой Windows выдаёт сама, когда сети
    нет) и со шлюзом по умолчанию."""
    if a.get("hardware") is not True or a.get("virtual") is not False or a.get("status") != "Up":
        return []
    if not _has_gateway(a.get("gateways")):
        return []
    return [str(ip) for ip in _as_list(a.get("ipv4")) if _usable_ipv4(ip)]


def pick_adapter(adapters: list[dict]) -> tuple[str, str]:
    """Адрес физического адаптера, через который ПК выходит в сеть, — и его имя для журнала.

    Правило отбора — `_exit_ips`. Подходящих несколько (кабель и Wi-Fi) — меньшая метрика, как
    выбирает сама Windows. Нет ни одного — BindError: подключаться не через что."""
    candidates = []
    for a in adapters:
        ips = _exit_ips(a)
        if not ips:
            continue
        index = a.get("index") if isinstance(a.get("index"), int) else 10 ** 6
        candidates.append((_metric(a), index, ips[0], a))
    if not candidates:
        seen = "; ".join(f"«{a.get('alias')}» — {a.get('status')}" for a in adapters
                         if a.get("hardware") is True and a.get("virtual") is False)
        raise BindError("нет включённого физического адаптера с IPv4 и шлюзом по умолчанию "
                        f"(физические адаптеры: {seen or 'ни одного'})")
    candidates.sort(key=lambda c: (c[0], c[1]))
    _, _, ip, a = candidates[0]
    note = f"«{a.get('alias')}» ({ip})"
    if len(candidates) > 1:
        note += f" — из {len(candidates)} подходящих, с меньшей метрикой"
    return ip, note


class Bind(NamedTuple):
    """Итог разбора `--bind`: адрес для `ssh -b` (None — без привязки), строка для журнала и
    предупреждение (None — его нет). Предупреждение не останавливает забор — оно попадает в журнал."""
    addr: str | None
    note: str
    warning: str | None = None


def bind_mode(bind: str | None) -> str:
    """`none` / `auto` / `explicit`. Пустое значение — `auto`: отключить привязку можно только
    явным `none` (ревью #289: пустая переменная давала auto, а пустой `--bind ""` — отключение)."""
    value = (bind or "").strip().lower()
    if value in NO_BIND:
        return "none"
    return "auto" if value in ("", "auto") else "explicit"


def _unusable_reason(ip) -> str | None:
    """Почему с этим адресом ssh до сервера заведомо не дойдёт, — или None, если адрес возможен."""
    if ip.version != 4:
        return "не IPv4-адрес, а сервер доступен по IPv4"
    if ip.is_unspecified:
        return "адрес «любой» не привязывает ssh ни к какому адаптеру"
    if ip.is_loopback:
        return "адрес петли — с него в сеть не выйти"
    if ip.is_link_local:
        return "адрес 169.254/16 Windows выдаёт сама, когда сети нет"
    if ip.is_multicast:
        return "групповой адрес"
    return None


def _wait_for_adapter(adapters, wait: float, interval: float, sleep, clock) -> tuple[str, str, float]:
    """`pick_adapter` с ожиданием: после пробуждения ПК (StartWhenAvailable, триггеры «вход» и
    «разблокировка») Ethernet поднимается не сразу, а попытка подключения к бою — одна в сутки.
    Опрос раз в `interval` с, не дольше `wait` с. → (адрес, имя адаптера, сколько ждали; 0 — нашёлся
    с первого опроса: сам опрос PowerShell идёт секунды, это не ожидание)."""
    start = clock()
    deadline = start + max(0.0, wait)
    retried = False
    while True:
        try:
            ip, note = pick_adapter(adapters())
            return ip, note, (clock() - start if retried else 0.0)
        except BindError as e:
            last = e
        remaining = deadline - clock()
        if remaining <= 0:
            break
        # Остаток короче интервала — досыпаем ровно до срока и опрашиваем ещё раз: иначе последний
        # опрос шёл на ~150-й секунде из 180, а при wait < interval ожидания не было вовсе (ревью 37bdab8).
        sleep(min(interval, remaining))
        retried = True
    waited = f" (ждал {clock() - start:.0f} с)" if wait > 0 else ""
    raise BindError(f"{last}{waited}. {_BIND_HELP}")


def resolve_bind(bind: str | None, adapters=None, windows: bool | None = None,
                 wait: float = 0, interval: float = 15, sleep=time.sleep, clock=time.monotonic) -> Bind:
    """`auto` / адрес / `none` → `Bind` (адрес для `ssh -b` или None, строка для журнала, предупреждение).

    BindError — привязку определить нельзя, подключаться не надо. `wait` — сколько секунд ждать
    подходящий адаптер в режиме `auto` (у запуска по расписанию — `DEFAULT_WAIT_ADAPTER`)."""
    adapters = adapters or list_adapters
    windows = (os.name == "nt") if windows is None else windows
    mode = bind_mode(bind)
    if mode == "none":
        return Bind(None, "ssh без привязки к адаптеру (--bind none)")
    if mode == "auto":
        if not windows:
            return Bind(None, "автопривязка к адаптеру — только на Windows; ssh без привязки")
        ip, note, waited = _wait_for_adapter(adapters, wait, interval, sleep, clock)
        after = f", адаптер ждал {waited:.0f} с" if waited else ""
        return Bind(ip, f"ssh мимо туннеля — привязка к адаптеру {note}{after}")
    value = bind.strip()
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise BindError(f"--bind {value!r} — не IP-адрес. {_BIND_HELP}") from None
    reason = _unusable_reason(ip)
    if reason:
        raise BindError(f"--bind {value} — {reason}. {_BIND_HELP}")
    note = f"ssh с привязкой к {ip} (адрес задан явно)"
    if not windows:
        return Bind(str(ip), note)
    try:
        listed = adapters()
    except BindError as e:
        return Bind(str(ip), note, f"проверить, чей это адрес, не удалось ({e}); если он не у "
                                   "физического адаптера со шлюзом, ssh уйдёт в туннель")
    owners = [a for a in listed if str(ip) in {str(x) for x in _as_list(a.get("ipv4"))}]
    if not owners:
        return Bind(str(ip), note, f"адрес {ip} не найден ни у одного адаптера этого ПК — ssh к нему "
                                   "не привяжется; сверьте `ipconfig` или уберите явный адрес (по умолчанию auto)")
    alias = owners[0].get("alias")
    if any(str(ip) in _exit_ips(a) for a in owners):
        return Bind(str(ip), f"{note}, адаптер «{alias}»")
    return Bind(str(ip), f"{note}, адаптер «{alias}»",
                f"адрес {ip} принадлежит «{alias}» — это не физический адаптер со шлюзом (туннель, "
                "виртуальный или без сети): ssh может уйти в туннель; по умолчанию — auto")


def failure_hint(stderr: str, addr: str | None, mode: str = "auto") -> str | None:
    """Подсказка к отказу ssh — чтобы в журнале было видно, что чинить, а не только «код 255».
    `mode` — как получен адрес (`bind_mode`): у явного адреса причина может быть в самом адресе."""
    s = stderr.lower()
    if addr and f"bind {addr}".lower() in s:
        if mode == "explicit":
            return (f"адрес {addr} не принадлежит этому ПК — сверьте `ipconfig` или уберите явный "
                    "адрес (по умолчанию auto)")
        return (f"адрес {addr} пропал у адаптера между выбором и подключением (сменился по DHCP?) — "
                "следующий запуск выберет адрес заново; сверить `ipconfig`")
    if "timed out" in s or "banner exchange" in s:
        if addr and mode == "explicit":
            return (f"адрес {addr} задан явно: если он не у физического адаптера со шлюзом (см. "
                    "`ipconfig`), ssh ушёл в туннель — уберите явный адрес (по умолчанию auto); "
                    "если у физического — дело в канале до сервера или в самом сервере")
        if addr:
            return "таймаут и мимо туннеля — дело в канале до сервера или в самом сервере"
        return ("ssh шёл без привязки к адаптеру: если включён туннель (Happ), ssh к бою он не "
                "пропускает — запустите с --bind auto (он и по умолчанию: уберите --bind none)")
    return None


def remote_command(app_dir: str, docker: str) -> str:
    """Команда на сервере: имя и sha256 свежей копии — в stderr, файл — в stdout.

    `< /dev/null` у `exec` обязателен: иначе `docker compose exec` съедает stdin ssh-сессии
    (так уже терялся остаток скрипта в heredoc 22.10). Строки метаданных начинаются с меток
    `NAME `/`SHA `, потому что в тот же stderr может писать и сам `docker compose`."""
    inner = ('f=$(ls -1t /data/backups/navigator_app-*.db 2>/dev/null | head -1); '
             '[ -n "$f" ] || { echo "NO_BACKUP" >&2; exit 3; }; '
             'echo "NAME $(basename "$f")" >&2; '
             'echo "SHA $(sha256sum "$f" | cut -d" " -f1)" >&2; '
             'cat "$f"')
    return f"cd ~/{app_dir} && {docker} compose exec -T app sh -c '{inner}' < /dev/null"


def parse_meta(stderr: str) -> tuple[str | None, str | None]:
    name = sha = None
    for line in stderr.splitlines():
        if line.startswith("NAME "):
            name = line[5:].strip()
        elif line.startswith("SHA "):
            sha = line[4:].strip().lower()
    return name, sha


def backup_time(name: str) -> datetime | None:
    """Время снятия копии по ИМЕНИ файла (UTC, так его пишет `backup_db.py`), а не по mtime:
    mtime здесь — время забора, оно свежее всегда."""
    m = NAME_RE.match(name)
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)


def rotate(dest: Path, keep_days: int, now: datetime) -> list[str]:
    """Удалить копии старше `keep_days` по дате в имени. Чужие файлы и копию с нераспознанным
    именем не трогает. Самую свежую не удаляет никогда — даже если она старше срока."""
    dated = sorted(((backup_time(p.name), p) for p in dest.glob("navigator_app-*.db")
                    if backup_time(p.name)), key=lambda x: x[0])
    removed = []
    for when, path in dated[:-1]:
        if now - when > timedelta(days=keep_days):
            path.unlink()
            removed.append(path.name)
    return removed


def pull(dest: Path, host: str = DEFAULT_HOST, app_dir: str = DEFAULT_APP_DIR,
         docker: str = DEFAULT_DOCKER, keep_days: int = 30, max_age_hours: int = 36,
         run=subprocess.run, now: datetime | None = None, say=print,
         bind: str | None = None, resolve=resolve_bind, mark_attempt=None) -> int:
    """Забрать, проверить, сохранить, ротировать. Возвращает код выхода (OK / FAILED / STALE).

    `bind` — `auto` / адрес / `none` (см. шапку); None — без привязки и без строки в журнале.
    `mark_attempt` — вызывается непосредственно перед ssh (`attempt_marker`); не записалась — к бою не
    подключаемся: без отметки следующий триггер подключился бы второй раз за сутки."""
    now = now or datetime.now(timezone.utc)
    dest = dest.resolve()
    if dest == ROOT or ROOT in dest.parents:
        say("[ОШИБКА] --dest внутри репозитория: в копии ПДн (CLAUDE.md, 152-ФЗ). Укажите папку вне него.")
        return FAILED
    addr = None
    mode = bind_mode(bind) if bind is not None else "none"
    if bind is not None:
        try:
            got = resolve(bind)
        except BindError as e:
            say(f"[ОШИБКА] привязка ssh к адаптеру: {e} — к серверу НЕ подключался")
            return FAILED
        addr, note = got[0], got[1]
        warning = got[2] if len(got) > 2 else None
        say(f"[i] {note}")
        if warning:
            say(f"[!] {warning}")
    dest.mkdir(parents=True, exist_ok=True)

    # ⚠⚠ ssh ПИШЕТ ПРЯМО В ФАЙЛ, А НЕ В КАНАЛ (27.10.2026, первый боевой забор). `ssh` из Git для
    # Windows — программа MSYS; когда её stdout — нативный канал Windows (`capture_output`), при
    # выходе процесса терялся хвост: пришло 7 077 888 байт из 7 098 368, код ssh при этом 0. На
    # сервере все три способа чтения отдавали файл целиком; в файл тот же ssh — целиком, сумма
    # сошлась. Поймала это сверка sha256 ниже — она здесь не формальность.
    part = dest / ".incoming.part"
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30"]
    if addr:
        cmd += ["-b", addr]
    cmd += [host, remote_command(app_dir, docker)]
    if mark_attempt is not None:
        try:
            mark_attempt()
        except OSError as e:
            say(f"[ОШИБКА] отметка попытки дня не записалась ({type(e).__name__}: {e}) — к серверу НЕ подключался")
            return FAILED
    try:
        with open(part, "wb") as fh:
            res = run(cmd, stdout=fh, stderr=subprocess.PIPE, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        part.unlink(missing_ok=True)
        say(f"[ОШИБКА] ssh не выполнился: {type(e).__name__}: {e}")
        return FAILED
    stderr = (res.stderr or b"").decode("utf-8", errors="replace")
    if res.returncode != 0:
        part.unlink(missing_ok=True)
        why = "на сервере нет ни одной копии в /data/backups" if "NO_BACKUP" in stderr else stderr.strip()[-300:]
        say(f"[ОШИБКА] забор не удался (код {res.returncode}): {why}")
        hint = failure_hint(stderr, addr, mode)
        if hint:
            say(f"[подсказка] {hint}")
        return FAILED

    name, sha = parse_meta(stderr)
    if not name or not NAME_RE.match(name) or not sha:
        part.unlink(missing_ok=True)
        say(f"[ОШИБКА] сервер не прислал имя и sha256 копии: {stderr.strip()[-300:]}")
        return FAILED
    size = part.stat().st_size
    if hashlib.sha256(part.read_bytes()).hexdigest() != sha:
        part.unlink(missing_ok=True)
        say(f"[ОШИБКА] sha256 не сошлась ({size:,} байт пришло): канал оборвался или файл менялся — копия НЕ сохранена")
        return FAILED

    final = dest / name
    if final.exists() and hashlib.sha256(final.read_bytes()).hexdigest() == sha:
        part.unlink(missing_ok=True)
        say(f"[OK] {name} уже есть в {dest} — та же копия, повторно не пишу")
    else:
        ok, report = verify_backup(part)
        if not ok:
            part.unlink(missing_ok=True)  # битую не оставляем: она маскирует отсутствие копии
            say(f"[ОШИБКА] {name}: {report} — копия НЕ сохранена")
            return FAILED
        part.replace(final)
        say(f"[OK] {name} → {dest} ({size:,} байт), {report}")

    for old in rotate(dest, keep_days, now):
        say(f"[rotate] удалена копия старше {keep_days} дн.: {old}")

    taken = backup_time(name)
    if now - taken > timedelta(hours=max_age_hours):
        say(f"[ТРЕВОГА] самая свежая копия на сервере снята {taken:%Y-%m-%d %H:%M} UTC — старше "
            f"{max_age_hours} ч: ночной бэкап на сервере не работает")
        return STALE
    return OK


class _Parser(argparse.ArgumentParser):
    """Ошибка аргументов — код 1 («копии нет»), а не штатный 2 argparse: код 2 здесь значит «ночной
    бэкап сервера не работает», и опечатка в задаче Планировщика читалась бы тревогой о сервере."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(FAILED, f"{self.prog}: ошибка: {message}\n")


def main() -> None:
    ap = _Parser(description="Забрать свежую копию боевой базы с сервера и проверить её")
    ap.add_argument("--dest", required=True, help="папка ВНЕ репозитория, на зашифрованном носителе")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--app-dir", default=DEFAULT_APP_DIR, help="каталог проекта в домашнем каталоге на сервере")
    ap.add_argument("--docker", default=DEFAULT_DOCKER, help='как звать docker на сервере (по умолчанию "sudo -n docker")')
    ap.add_argument("--keep-days", type=int, default=30)
    ap.add_argument("--max-age-hours", type=int, default=36, help="старше — тревога: ночной бэкап не работает")
    ap.add_argument("--bind", default=os.environ.get(BIND_ENV) or "auto",
                    help=f"адрес для `ssh -b` мимо туннеля: auto (по умолчанию; пустое значение — тоже auto) "
                         f"| <IP> | none; или переменная {BIND_ENV}")
    ap.add_argument("--wait-adapter", type=int, default=DEFAULT_WAIT_ADAPTER,
                    help=f"сколько секунд ждать физический адаптер со шлюзом в режиме auto "
                         f"(по умолчанию {DEFAULT_WAIT_ADAPTER}; 0 — не ждать)")
    ap.add_argument("--mark-attempt", type=Path, metavar="ФАЙЛ",
                    help="записать отметку попытки дня непосредственно перед ssh (обёртка Планировщика)")
    ap.add_argument("--mark-value", metavar="СТРОКА", help="что записать в отметку — `%%DATE%%` обёртки")
    args = ap.parse_args()
    if (args.mark_attempt is None) != (args.mark_value is None):
        ap.error("--mark-attempt и --mark-value задаются только вместе")
    mark = attempt_marker(args.mark_attempt, args.mark_value) if args.mark_attempt else None
    resolve = functools.partial(resolve_bind, wait=max(0, args.wait_adapter))
    sys.exit(pull(Path(args.dest), args.host, args.app_dir, args.docker, args.keep_days, args.max_age_hours,
                  bind=args.bind, resolve=resolve, mark_attempt=mark))


if __name__ == "__main__":
    from app.core.console import enable_utf8  # #107: значки вне cp1251 роняли бы печать
    enable_utf8()
    main()
