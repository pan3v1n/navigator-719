"""`O9` #145: схема API не публикуется на бою (офлайн, без Qdrant и DeepSeek).

Дефолт FastAPI отдаёт `/docs`, `/redoc` и `/openapi.json` — карту эндпоинтов вместе с
`/api/admin/export`. Локально документация нужна, на бою — нет.

⚠ Прод проверяется ПЕРЕЗАГРУЗКОЙ `main` под `APP_ENV=production`, а не вызовом одной функции:
тест на `api_docs_kwargs` остался бы зелёным, если бы `main.app` её не звал (класс «сторож проверяет
компонент напрямую, а не путь до него»). Lifespan в тесте не запускается (клиент без `with`),
поэтому гард секрета прод-сборку не роняет.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class TestApiDocsHidden(unittest.TestCase):
    def _app_under(self, env: str):
        from app.core.config import settings

        import main

        prev = settings.APP_ENV
        settings.APP_ENV = env
        try:
            return importlib.reload(main).app
        finally:
            settings.APP_ENV = prev
            importlib.reload(main)  # остальные тесты получают приложение среды по умолчанию

    def _get(self, app, path: str) -> int:
        from fastapi.testclient import TestClient

        return TestClient(app).get(path).status_code

    def test_development_keeps_the_docs(self):
        app = self._app_under("development")
        for p in DOC_PATHS:
            self.assertEqual(self._get(app, p), 200, p)

    def test_production_hides_the_docs(self):
        app = self._app_under("production")
        for p in DOC_PATHS:
            self.assertEqual(self._get(app, p), 404, f"{p} открыт на бою")
        self.assertEqual(self._get(app, "/ping"), 200, "закрыли лишнее: /ping не отвечает")

    def test_anything_but_development_counts_as_production(self):
        """Условие то же, что у гарда секрета: опечатка в APP_ENV не открывает схему."""
        from main import api_docs_kwargs

        for env in ("production", "prod", "staging", ""):
            self.assertEqual(api_docs_kwargs(env),
                             {"docs_url": None, "redoc_url": None, "openapi_url": None}, env)


if __name__ == "__main__":
    unittest.main()
