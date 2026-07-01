"""Создать пользователей демо (admin + эксперты) с bcrypt-паролями. Пароли генерируются и
ПЕЧАТАЮТСЯ в консоль — раздать экспертам (в код/БД plaintext не попадает).

Запуск:
  .venv\\Scripts\\python scripts\\seed_users.py --experts 4
  .venv\\Scripts\\python scripts\\seed_users.py --reset          # сбросить пароли существующим

Идемпотентно: существующего юзера НЕ дублирует; без --reset его пароль не трогает.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.auth import hash_password  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.engine import get_session, init_db  # noqa: E402


def _gen_password() -> str:
    return secrets.token_urlsafe(6)  # ~8 символов, читаемо для раздачи


def main() -> None:
    ap = argparse.ArgumentParser(description="Сид пользователей демо (admin + эксперты)")
    ap.add_argument("--experts", type=int, default=4, help="сколько экспертов создать (expert1..N)")
    ap.add_argument("--admin", type=str, default="admin", help="логин администратора")
    ap.add_argument("--admin-password", type=str, default=None,
                    help="фиксированный пароль администратора (иначе генерируется); ставится даже без --reset")
    ap.add_argument("--reset", action="store_true", help="сбросить пароли существующим юзерам")
    args = ap.parse_args()

    init_db()
    spec = [(args.admin, "admin")] + [(f"expert{i}", "expert") for i in range(1, args.experts + 1)]
    creds: list[tuple[str, str, str]] = []

    with get_session() as db:
        for username, role in spec:
            existing = q.get_user_by_username(db, username)
            forced = args.admin_password if (role == "admin" and args.admin_password) else None
            if existing and not args.reset and not forced:
                creds.append((username, "(без изменений)", role))
                continue
            pw = forced or _gen_password()
            if existing:
                existing.password_hash = hash_password(pw)
                db.commit()
            else:
                q.create_user(db, username, hash_password(pw), role=role)
            creds.append((username, pw, role))

    print("\n" + "=" * 56)
    print("ПОЛЬЗОВАТЕЛИ ДЕМО (раздать экспертам; пароли больше нигде не хранятся)")
    print("=" * 56)
    print(f"  {'логин':<14}{'пароль':<18}роль")
    print("  " + "-" * 44)
    for username, pw, role in creds:
        print(f"  {username:<14}{pw:<18}{role}")
    print("=" * 56)
    print("Совет: пароли пересоздать — `--reset`. Для прода задать SESSION_SECRET в .env.\n")


if __name__ == "__main__":
    main()
