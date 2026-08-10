"""Консистентный бэкап БД приложения (SQLite) с ротацией — для cron на VM и локально.

Данные теста (оценки/исправления/комментарии/логи диалогов) = результат. Тома переживают редеплой,
но `instance delete` их стирает. Скрипт делает sqlite `.backup` (консистентно даже под нагрузкой) в
`<dir>/navigator_app-YYYY-MM-DD_HHMM.db` и хранит последние `--keep` копий. Путь БД — из
`settings.APP_DB_URL` (на VM это том `/data`).

Запуск:
  .venv/Scripts/python scripts/backup_db.py                      # в ./backups, хранить 14
  .venv/Scripts/python scripts/backup_db.py --dir /data/backups --keep 30
На VM (в контейнере):
  docker compose exec -T app python scripts/backup_db.py --dir /data/backups --keep 30
Автоматизация (cron ежедневно + вынос off-VM) — docs/DEPLOY.md §9.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402


def _db_path() -> Path:
    """Путь к файлу БД из APP_DB_URL (sqlite:///./x.db → относит.; sqlite:////data/x.db → абсол.)."""
    m = re.match(r"sqlite:///(.*)", settings.APP_DB_URL)
    if not m:
        sys.exit(f"APP_DB_URL не sqlite-файл: {settings.APP_DB_URL}")
    p = m.group(1)
    return Path(p) if (p.startswith("/") or ":" in p[:3]) else (ROOT / p)


def verify_backup(path: Path) -> tuple[bool, str]:
    """Пригоден ли файл как бэкап: целостность SQLite + непустые ключевые таблицы.

    Проверяем ровно то, что делает копию бесполезной: битые страницы и «успешно снятая» пустая
    база (случается, если подсунуть не тот путь). Возвращает (годен, человекочитаемый отчёт)."""
    if not path.exists():
        return False, "файла нет"
    # sqlite3.connect ЛЕНИВ: на не-базе он отработает молча, а исключение прилетит на первом
    # запросе. Поэтому под try — весь блок, включая PRAGMA, иначе проверка сама падает.
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            return False, f"integrity_check: {integrity}"
        counts = {}
        for t in ("users", "messages", "feedback"):
            try:
                counts[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                return False, f"нет таблицы {t}"
    except sqlite3.Error as e:
        return False, f"не читается как база SQLite: {e}"
    finally:
        if conn is not None:
            conn.close()
    stats = ", ".join(f"{t} {n}" for t, n in counts.items())
    if counts.get("users", 0) == 0:
        return False, f"целостность ok, но пользователей 0 — похоже, скопирована не та база ({stats})"
    return True, f"целостность ok, {stats}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Бэкап БД приложения (SQLite) с ротацией и проверкой")
    ap.add_argument("--dir", default=str(ROOT / "backups"), help="куда складывать бэкапы")
    ap.add_argument("--keep", type=int, default=14, help="сколько последних копий хранить (0 = все)")
    ap.add_argument("--verify", metavar="FILE",
                    help="только проверить готовый файл бэкапа (для регулярного контроля off-VM копий)")
    args = ap.parse_args()

    if args.verify:
        ok, report = verify_backup(Path(args.verify))
        print(f"{'[OK]' if ok else '[ОШИБКА]'} {args.verify}: {report}")
        sys.exit(0 if ok else 1)

    src = _db_path()
    if not src.exists():
        sys.exit(f"БД не найдена: {src}")
    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    dst = out_dir / f"navigator_app-{stamp}.db"

    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)  # online-backup: консистентная копия без остановки сервиса
    finally:
        dst_conn.close()
        src_conn.close()

    # ПРОВЕРКА СРАЗУ ПОСЛЕ СНЯТИЯ (R13). Бэкап, о непригодности которого узнаёшь в момент
    # восстановления, — это отсутствие бэкапа. Данные теста (оценки, исправления, диалоги) —
    # главный актив проекта, второй попытки их собрать не будет.
    ok, report = verify_backup(dst)
    print(f"[OK] бэкап -> {dst}  ({dst.stat().st_size:,} байт)")
    print(f"     проверка: {report}")
    if not ok:
        dst.unlink(missing_ok=True)  # битую копию не оставляем: она маскирует отсутствие бэкапа
        sys.exit("[ОШИБКА] копия не прошла проверку и удалена — бэкап НЕ создан")

    if args.keep > 0:  # ротация: оставить последние --keep (имена сортируются по дате лексикографически)
        for old in sorted(out_dir.glob("navigator_app-*.db"))[:-args.keep]:
            old.unlink()
            print(f"[rotate] удалён старый: {old.name}")


if __name__ == "__main__":
    main()
