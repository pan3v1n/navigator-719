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


def main() -> None:
    ap = argparse.ArgumentParser(description="Бэкап БД приложения (SQLite) с ротацией")
    ap.add_argument("--dir", default=str(ROOT / "backups"), help="куда складывать бэкапы")
    ap.add_argument("--keep", type=int, default=14, help="сколько последних копий хранить (0 = все)")
    args = ap.parse_args()

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
    print(f"[OK] бэкап -> {dst}  ({dst.stat().st_size:,} байт)")

    if args.keep > 0:  # ротация: оставить последние --keep (имена сортируются по дате лексикографически)
        for old in sorted(out_dir.glob("navigator_app-*.db"))[:-args.keep]:
            old.unlink()
            print(f"[rotate] удалён старый: {old.name}")


if __name__ == "__main__":
    main()
