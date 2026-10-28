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

Запуск:
  .venv\\Scripts\\python scripts\\pull_backup.py --dest E:\\navigator-backups
По расписанию — Планировщик заданий Windows, раз в сутки после 03:00 UTC (06:00 МСК), например:
  schtasks /Create /SC DAILY /ST 09:00 /TN navigator-pull-backup ^
    /TR "D:\\navigator-719\\.venv\\Scripts\\python.exe D:\\navigator-719\\scripts\\pull_backup.py --dest E:\\navigator-backups"
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backup_db import verify_backup  # noqa: E402

DEFAULT_HOST = "dan_tpp_srv@77.241.30.132"
DEFAULT_APP_DIR = "navigator-719"          # каталог проекта в домашнем каталоге на сервере
DEFAULT_DOCKER = "sudo -n docker"          # как у deploy.sh: `DOCKER="${DOCKER:-sudo -n docker}"`
NAME_RE = re.compile(r"^navigator_app-(\d{4}-\d{2}-\d{2})_(\d{4})\.db$")

OK, FAILED, STALE = 0, 1, 2


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
         run=subprocess.run, now: datetime | None = None, say=print) -> int:
    """Забрать, проверить, сохранить, ротировать. Возвращает код выхода (OK / FAILED / STALE)."""
    now = now or datetime.now(timezone.utc)
    dest = dest.resolve()
    if dest == ROOT or ROOT in dest.parents:
        say("[ОШИБКА] --dest внутри репозитория: в копии ПДн (CLAUDE.md, 152-ФЗ). Укажите папку вне него.")
        return FAILED
    dest.mkdir(parents=True, exist_ok=True)

    # ⚠⚠ ssh ПИШЕТ ПРЯМО В ФАЙЛ, А НЕ В КАНАЛ (27.10.2026, первый боевой забор). `ssh` из Git для
    # Windows — программа MSYS; когда её stdout — нативный канал Windows (`capture_output`), при
    # выходе процесса терялся хвост: пришло 7 077 888 байт из 7 098 368, код ssh при этом 0. На
    # сервере все три способа чтения отдавали файл целиком; в файл тот же ssh — целиком, сумма
    # сошлась. Поймала это сверка sha256 ниже — она здесь не формальность.
    part = dest / ".incoming.part"
    try:
        with open(part, "wb") as fh:
            res = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", host,
                       remote_command(app_dir, docker)],
                      stdout=fh, stderr=subprocess.PIPE, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        part.unlink(missing_ok=True)
        say(f"[ОШИБКА] ssh не выполнился: {type(e).__name__}: {e}")
        return FAILED
    stderr = (res.stderr or b"").decode("utf-8", errors="replace")
    if res.returncode != 0:
        part.unlink(missing_ok=True)
        why = "на сервере нет ни одной копии в /data/backups" if "NO_BACKUP" in stderr else stderr.strip()[-300:]
        say(f"[ОШИБКА] забор не удался (код {res.returncode}): {why}")
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Забрать свежую копию боевой базы с сервера и проверить её")
    ap.add_argument("--dest", required=True, help="папка ВНЕ репозитория, на зашифрованном носителе")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--app-dir", default=DEFAULT_APP_DIR, help="каталог проекта в домашнем каталоге на сервере")
    ap.add_argument("--docker", default=DEFAULT_DOCKER, help='как звать docker на сервере (по умолчанию "sudo -n docker")')
    ap.add_argument("--keep-days", type=int, default=30)
    ap.add_argument("--max-age-hours", type=int, default=36, help="старше — тревога: ночной бэкап не работает")
    args = ap.parse_args()
    sys.exit(pull(Path(args.dest), args.host, args.app_dir, args.docker, args.keep_days, args.max_age_hours))


if __name__ == "__main__":
    from app.core.console import enable_utf8  # #107: значки вне cp1251 роняли бы печать
    enable_utf8()
    main()
