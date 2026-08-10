"""R3: маркировка выгрузок пометкой «ИИ — черновик, вердикт за экспертом ТПП».

Почему это отдельный набор тестов. Принцип №1 проекта — ответ ИИ не является заключением. В
интерфейсе пометка висит постоянной строкой под полем ввода, но ЭКСПОРТ уносит ответы за пределы
сервиса (заявителю, в переписку, в приложение к заявке) — и уходил без единой пометки ни в одном
из трёх форматов. Регресс здесь дорогой и незаметный глазом, поэтому проверяем все ручки и все
форматы, включая подпись каждой реплики ассистента (она переживает копирование отдельного ответа
из файла — самый вероятный способ, которым ответ уходит дальше).
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api import chat as chat_mod  # noqa: E402
from app.core.prompts import EXPERT_DISCLAIMER  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base  # noqa: E402


class _Msg:
    """Минимальная реплика — столько, сколько читают сериализаторы выгрузки."""

    def __init__(self, role: str, content: str):
        self.role, self.content = role, content
        self.ts = datetime(2026, 8, 10, 12, 0)
        self.sources_json = None


ANSWER = "Позиция: Насосы гидравлические, порог не менее 90 баллов."
DIALOG = [_Msg("user", "Требования к гидравлическим насосам?"), _Msg("assistant", ANSWER)]
PICKED = [({"session_id": "s1", "title": "Насосы", "ts": datetime(2026, 8, 10, 12, 0)}, DIALOG)]


class TestExportMarkedAsAI(unittest.TestCase):
    """Текстовые форматы: md и txt, обе ручки (все беседы / одна беседа)."""

    def _all_serializers(self) -> dict[str, str]:
        return {
            "все беседы, md": chat_mod._convs_to_markdown("expert1", PICKED),
            "все беседы, txt": chat_mod._convs_to_text("expert1", PICKED),
            "одна беседа, md": chat_mod._conv_to_markdown("Насосы", DIALOG),
            "одна беседа, txt": chat_mod._conv_to_text("Насосы", DIALOG),
        }

    def test_disclaimer_in_every_format(self):
        for name, doc in self._all_serializers().items():
            self.assertIn(EXPERT_DISCLAIMER, doc, f"нет пометки: {name}")

    def test_assistant_reply_labelled_as_ai(self):
        # подпись реплики переживает копирование отдельного ответа из файла
        for name, doc in self._all_serializers().items():
            self.assertIn(chat_mod._AI_LABEL, doc, f"ответ не помечен как ИИ: {name}")
            self.assertIn("ИИ", chat_mod._AI_LABEL)

    def test_user_reply_not_labelled_as_ai(self):
        """Подпись «ИИ» стоит у ответа, а не у вопроса — иначе маркировка теряет смысл."""
        for name, doc in self._all_serializers().items():
            lines = doc.split("\n")
            i = next(k for k, ln in enumerate(lines) if DIALOG[0].content in ln)
            prev = next(ln for ln in reversed(lines[:i]) if ln.strip())  # ближайшая подпись выше
            self.assertIn(chat_mod._HUMAN_LABEL, prev, f"{name}: вопрос не подписан экспертом")
            self.assertNotIn(chat_mod._AI_LABEL, prev, f"{name}: вопрос помечен как ИИ")

    def test_content_and_source_preserved(self):
        for name, doc in self._all_serializers().items():
            self.assertIn(ANSWER, doc, f"потеряно содержимое: {name}")
            self.assertIn("Навигатор", doc, f"нет указания на источник: {name}")


class TestJsonExportMarked(unittest.TestCase):
    """JSON-ручки: пометка должна быть полем документа, а не только в тексте реплик."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._orig = chat_mod.get_session
        chat_mod.get_session = lambda: self.Session()
        with self.Session() as db:
            self.uid = q.create_user(db, "expert1", "h", role="expert").id
            q.log_message(db, user_id=self.uid, session_id="s1", role="user", content="вопрос")
            q.log_message(db, user_id=self.uid, session_id="s1", role="assistant", content=ANSWER)
            self.user = q.get_user(db, self.uid)
            db.expunge(self.user)

    def tearDown(self):
        chat_mod.get_session = self._orig

    def _payload(self, response) -> dict:
        return json.loads(response.body.decode("utf-8"))

    def test_all_conversations_json_carries_disclaimer(self):
        data = self._payload(chat_mod.export_conversations(fmt="json", user=self.user))
        self.assertEqual(data["disclaimer"], EXPERT_DISCLAIMER)
        self.assertIn("exported_at", data)
        self.assertEqual(data["exported_conversations"], 1)

    def test_single_conversation_json_carries_disclaimer(self):
        data = self._payload(chat_mod.export_conversation("s1", fmt="json", user=self.user))
        self.assertEqual(data["disclaimer"], EXPERT_DISCLAIMER)
        self.assertEqual(len(data["conversation"]["messages"]), 2)

    def test_md_and_txt_downloads_carry_disclaimer(self):
        for fmt in ("md", "txt"):
            for resp in (chat_mod.export_conversations(fmt=fmt, user=self.user),
                         chat_mod.export_conversation("s1", fmt=fmt, user=self.user)):
                self.assertIn(EXPERT_DISCLAIMER, resp.body.decode("utf-8"), fmt)


if __name__ == "__main__":
    unittest.main()
