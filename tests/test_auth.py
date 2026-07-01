"""Юнит-тесты auth-примитивов (bcrypt) — офлайн, без БД/сети.

Полный login/web-флоу (редиректы, гейты ролей, лог диалога, фидбек, админ-вью) проверяется
интеграционно через TestClient (см. историю сборки); здесь — чистая криптологика хэша пароля.
Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.auth import hash_password, verify_password  # noqa: E402


class TestAuthPasswords(unittest.TestCase):
    def test_hash_verify_roundtrip(self):
        h = hash_password("СекретныйПароль123")
        self.assertTrue(verify_password("СекретныйПароль123", h))

    def test_hash_is_salted(self):
        # соль внутри → два хэша одного пароля различаются, но оба верифицируются
        h1, h2 = hash_password("pw"), hash_password("pw")
        self.assertNotEqual(h1, h2)
        self.assertTrue(verify_password("pw", h1))
        self.assertTrue(verify_password("pw", h2))

    def test_verify_rejects_wrong(self):
        h = hash_password("right")
        self.assertFalse(verify_password("wrong", h))

    def test_verify_handles_bad_hash(self):
        # не-bcrypt строка не должна ронять verify (возвращаем False)
        self.assertFalse(verify_password("pw", "не-хэш"))


if __name__ == "__main__":
    unittest.main()
