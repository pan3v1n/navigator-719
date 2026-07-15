"""Создать пользователей (admin + эксперты + региональные тестировщики) с bcrypt-паролями.
Пароли генерируются и ПЕЧАТАЮТСЯ в консоль — раздать людям (в код/БД plaintext не попадает).

Запуск:
  .venv\\Scripts\\python scripts\\seed_users.py --experts 4
  .venv\\Scripts\\python scripts\\seed_users.py --reset          # сбросить пароли существующим
  # региональные аккаунты (роль user, логины вида <prefix>.expert1) для платного теста:
  .venv\\Scripts\\python scripts\\seed_users.py --experts 0 --admin-password СЕКРЕТ \\
      --accounts mosobl:1,sakhalin:1,perm:1,voronezh:1

Идемпотентно: существующего юзера НЕ дублирует; без --reset его пароль не трогает.
ПДн (ФИО/телефоны) в скрипт НЕ передаём — их заполняет сам пользователь в профиле; здесь только
логины по префиксу региона (app/core/regions.py). Таблицу «логин → человек» ведём офлайн.
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


def _parse_accounts(raw: str, suffix: str, role: str) -> list[tuple[str, str]]:
    """'moscow:2,perm:1' → [('moscow.expert1','user'), ('moscow.expert2','user'), ('perm.expert1','user')].
    count по умолчанию 1 (можно писать просто 'moscow'). Неизвестный префикс — предупреждаем, но создаём."""
    from app.core.regions import known_prefix  # ROOT уже в sys.path

    out: list[tuple[str, str]] = []
    for chunk in (c.strip() for c in raw.split(",") if c.strip()):
        prefix, sep, cnt = chunk.partition(":")
        prefix = prefix.strip()
        if not prefix:
            continue
        try:
            count = int(cnt) if sep else 1
        except ValueError:
            count = 1
        if not known_prefix(prefix):
            print(f"⚠️  Неизвестный префикс региона: {prefix!r} — создаю, но проверь app/core/regions.py")
        for i in range(1, count + 1):
            out.append((f"{prefix}.{suffix}{i}", role))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Сид пользователей демо (admin + эксперты)")
    ap.add_argument("--experts", type=int, default=4, help="сколько экспертов создать (expert1..N)")
    ap.add_argument("--admin", type=str, default="admin", help="логин администратора")
    ap.add_argument("--admin-password", type=str, default=None,
                    help="фиксированный пароль администратора (иначе генерируется); ставится даже без --reset")
    ap.add_argument("--reset", action="store_true", help="сбросить пароли существующим юзерам")
    ap.add_argument("--accounts", type=str, default="",
                    help="региональные аккаунты 'prefix:count' через запятую (напр. 'mosobl:1,perm:2'); count=1 по умолч.")
    ap.add_argument("--role", type=str, default="user",
                    help="роль для --accounts (default user — региональные тестировщики; сегмент отделён от expert)")
    ap.add_argument("--suffix", type=str, default="expert",
                    help="суффикс логина региональных аккаунтов: <prefix>.<suffix><i> (default expert)")
    args = ap.parse_args()

    init_db()
    spec = (
        [(args.admin, "admin")]
        + [(f"expert{i}", "expert") for i in range(1, args.experts + 1)]
        + _parse_accounts(args.accounts, args.suffix, args.role)
    )
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
    print(f"  {'логин':<24}{'пароль':<18}роль")
    print("  " + "-" * 54)
    for username, pw, role in creds:
        print(f"  {username:<24}{pw:<18}{role}")
    print("=" * 56)
    print("Совет: пароли пересоздать — `--reset`. Для прода задать SESSION_SECRET в .env.\n")


if __name__ == "__main__":
    main()
