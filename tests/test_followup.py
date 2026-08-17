"""U5: подсказка в поле ввода зависит от состояния беседы (issue #74).

Проверяем не «удачный текст», а три свойства, без которых подсказка вредна.

1. **Маршрут.** Подсказка, ведущая к вопросу, на который движок ответить не может, ХУЖЕ пустого
   поля: она обещает от имени системы. Обе подсказки обязаны уходить процедурным путём — и в
   чистом виде, и с приклеенным наименованием продукции (так их перепишет контекстуализатор
   мультитёрна). Без 719-якоря процедурный детектор считает такой вопрос товарным, и вместо
   перечня документов человек получает ближайшую позицию приложения — живой дефект `K12`.
2. **Ветка ответа решает, что обещать.** Позиция не найдена, сработал out-of-scope guard,
   процедурный корпус недоступен — подсказки нет или она зовёт назвать продукцию.
3. **Одна копия текста.** Стартовую подсказку рендерит сервер, следующий шаг приходит полем
   ответа; своей копии у фронта нет. Вторая копия константы уже уводила эксперта на
   недействующую редакцию первоисточника (`app/rag/edition.py`).

Офлайн: без Qdrant и DeepSeek.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api import chat as chat_mod  # noqa: E402
from app.api.ratelimit import SlidingWindow  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.rag import followup, procedural  # noqa: E402
from app.rag import pipeline as pipeline_mod  # noqa: E402
from app.rag.pipeline import Answer  # noqa: E402
from app.rag.retriever import Hit  # noqa: E402

WEB = ROOT / "app" / "web"

SUGGESTIONS = (followup.DOCS_QUESTION, followup.DEADLINE_QUESTION)


class TestSuggestionsAreAnswerable(unittest.TestCase):
    """Каждая подсказка обязана вести туда, где движок действительно отвечает."""

    def test_every_suggestion_goes_procedural_route(self):
        for question in SUGGESTIONS:
            self.assertTrue(
                procedural.is_procedural(question),
                f"подсказка «{question}» уйдёт на ТОВАРНЫЙ путь: вместо процедурного ответа "
                "человек получит ближайшую позицию приложения",
            )

    def test_suggestion_survives_contextualization(self):
        """Мультитёрн переписывает короткий вопрос в самостоятельный, подмешивая продукцию.

        Переписывание делает LLM, поэтому проверяем УСТОЙЧИВОСТЬ формулировки: с приклеенным
        наименованием маршрут обязан остаться процедурным. Это и есть страховка от `K12` —
        «какие документы нужны для этикетировщиков» без 719-якоря считается товарным вопросом."""
        for question in SUGGESTIONS:
            for tail in ("гидравлических насосов", "для криогенной АЗС", "по коду 28.13.14"):
                rewritten = f"{question} {tail}"
                self.assertTrue(procedural.is_procedural(rewritten), rewritten)

    def test_hint_is_shown_as_example_not_command(self):
        hint = followup.after_product(low_relevance=False)
        self.assertTrue(hint.startswith("Например:"), hint)
        self.assertIn(followup.DOCS_QUESTION, hint)


class TestHintByAnswerBranch(unittest.TestCase):
    """Что показывать дальше — решает ветка, которой отвечено, а не текст ответа."""

    def setUp(self):
        self._orig = {
            "embed": pipeline_mod.embed_query,
            "search": pipeline_mod.search,
            "cases": pipeline_mod.search_cases,
            "top1": pipeline_mod.dense_top1,
            "rerank": pipeline_mod.settings.RERANK_ENABLED,
        }
        pipeline_mod.embed_query = lambda text: [0.1] * 4
        pipeline_mod.settings.RERANK_ENABLED = False  # реранкер — сеть, здесь не про него

    def tearDown(self):
        pipeline_mod.embed_query = self._orig["embed"]
        pipeline_mod.search = self._orig["search"]
        pipeline_mod.search_cases = self._orig["cases"]
        pipeline_mod.dense_top1 = self._orig["top1"]
        pipeline_mod.settings.RERANK_ENABLED = self._orig["rerank"]

    @staticmethod
    def _hit(okpd2_match: bool) -> Hit:
        return Hit(
            score=0.9, section_roman="XIX", section_title="Насосы",
            product_name="Насосы гидравлические", okpd2_codes=["28.13.14"],
            min_threshold="не менее 300 баллов", requirement_blocks=[], source_anchor="Раздел XIX",
            okpd2_match=okpd2_match,
        )

    def test_meta_and_translate_invite_to_name_product(self):
        """Приветствие и перевод кода: продукция не названа — подсказка зовёт её назвать."""
        for query in ("привет", "что ты умеешь", "переведи ТН ВЭД 8471 30 000 0 в ОКПД2"):
            planned = pipeline_mod._plan_answer(query)
            self.assertIsInstance(planned, Answer, query)
            self.assertEqual(planned.input_hint, followup.START_HINT, query)

    def test_not_found_invites_to_name_product(self):
        pipeline_mod.search = lambda *a, **k: []
        pipeline_mod.search_cases = lambda *a, **k: []
        planned = pipeline_mod._plan_answer("производим нечто неведомое")
        self.assertIsInstance(planned, Answer)
        self.assertIn("не найдена", planned.text)
        self.assertEqual(planned.input_hint, followup.START_HINT)

    def test_confident_product_answer_suggests_documents(self):
        pipeline_mod.search = lambda *a, **k: [self._hit(okpd2_match=True)]
        pipeline_mod.search_cases = lambda *a, **k: []
        planned = pipeline_mod._plan_answer("требования к 28.13.14", okpd2="28.13.14")
        self.assertIsInstance(planned, pipeline_mod._Plan)
        self.assertFalse(planned.low_relevance)
        self.assertIn(followup.DOCS_QUESTION, planned.input_hint)

    def test_unsure_product_answer_does_not_promise_documents(self):
        """Guard поднял флаг — позиция не подтверждена, и подсказка ведёт к уточнению, а не
        к документам по позиции, которой, возможно, и нет."""
        pipeline_mod.search = lambda *a, **k: [self._hit(okpd2_match=False)]
        pipeline_mod.search_cases = lambda *a, **k: []
        pipeline_mod.dense_top1 = lambda *a, **k: 0.5  # ниже RELEVANCE_SOFT
        planned = pipeline_mod._plan_answer("оказываем юридические услуги")
        self.assertIsInstance(planned, pipeline_mod._Plan)
        self.assertTrue(planned.low_relevance)
        self.assertEqual(planned.input_hint, followup.START_HINT)
        self.assertNotIn(followup.DOCS_QUESTION, planned.input_hint)

    # --- процедурная ветка -------------------------------------------------------------
    @staticmethod
    def _rules(topic: str) -> list[dict]:
        return [{"doc_type": topic, "_topic": topic, "source_anchor": "Приказ ТПП РФ №52, п. 4.1",
                 "text": "Заявитель представляет копию устава и выписку из ЕГРЮЛ."}]

    def _procedural_hint(self, topic: str) -> str:
        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "Ответ по пунктам [1]."})()})()]
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()

        class _Client:
            chat = type("Ch", (), {"completions": type(
                "Co", (), {"create": staticmethod(lambda *a, **k: _Resp())})()})()

        orig_search, orig_client = pipeline_mod.search_rules, pipeline_mod._client
        pipeline_mod.search_rules = lambda *a, **k: self._rules(topic)
        pipeline_mod._client = lambda: _Client()
        try:
            return pipeline_mod._answer_procedural("какие документы нужны", "какие документы нужны").input_hint
        finally:
            pipeline_mod.search_rules, pipeline_mod._client = orig_search, orig_client

    def test_documents_answer_suggests_deadlines(self):
        """Спросили про состав документов — следующий уместный шаг про сроки, а не про то же самое."""
        hint = self._procedural_hint("tpp_order_52")
        self.assertIn(followup.DEADLINE_QUESTION, hint)
        self.assertNotIn(followup.DOCS_QUESTION, hint)

    def test_other_procedural_topics_suggest_documents(self):
        for topic in ("rules_registry", "decree_body", "appendix_footnotes", None):
            self.assertIn(followup.DOCS_QUESTION, followup.after_procedural(topic), str(topic))

    def test_deflection_promises_nothing(self):
        """Процедурная ветка сейчас не отвечает — предлагать по ней следующий вопрос нельзя."""
        orig_search = pipeline_mod.search_rules
        pipeline_mod.search_rules = lambda *a, **k: []
        try:
            ans = pipeline_mod._answer_procedural("порядок внесения в реестр", "порядок внесения в реестр")
        finally:
            pipeline_mod.search_rules = orig_search
        self.assertEqual(ans.text, procedural.DEFLECTION)
        self.assertEqual(ans.input_hint, "", "дефер не должен ничего обещать")

        orig_flag = pipeline_mod.settings.PROCEDURAL_ANSWER_FROM_RULES
        pipeline_mod.settings.PROCEDURAL_ANSWER_FROM_RULES = False
        try:
            ans = pipeline_mod._answer_procedural("порядок внесения в реестр", "порядок внесения в реестр")
        finally:
            pipeline_mod.settings.PROCEDURAL_ANSWER_FROM_RULES = orig_flag
        self.assertEqual(ans.input_hint, "", "дефер не должен ничего обещать")


class TestHintReachesFront(unittest.TestCase):
    """Подсказка бесполезна, если не доехала до поля ввода — а сломается это молча."""

    def setUp(self):
        # StaticPool обязателен: стрим отдаётся из threadpool, а обычный пул под sqlite-in-memory
        # заводит каждому потоку СВОЮ пустую базу — запись лога падала бы «no such table» внутрь
        # своего же except и тест оставался зелёным на неработающем логировании.
        self.engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._orig_session, self._orig_limit = chat_mod.get_session, chat_mod._chat_limit
        self._orig_answer, self._orig_stream = chat_mod.answer, chat_mod.answer_stream
        chat_mod.get_session = lambda: self.Session()
        chat_mod._chat_limit = SlidingWindow(limit=1000, window=60.0)
        with self.Session() as db:
            self.user = q.get_user(db, q.create_user(db, "expert1", "h", role="expert").id)
            db.expunge(self.user)
        # Настоящий Answer, а не самодельная заглушка: двойник с неполным набором полей разъезжается
        # с моделью молча (на этой правке так и вышло — два теста упали на отсутствии input_hint).
        self.ans = Answer(text="Ответ движка", hits=[], input_hint="Например: сроки рассмотрения заявления")

    def tearDown(self):
        chat_mod.get_session, chat_mod._chat_limit = self._orig_session, self._orig_limit
        chat_mod.answer, chat_mod.answer_stream = self._orig_answer, self._orig_stream

    def test_chat_response_carries_hint(self):
        chat_mod.answer = lambda *a, **k: self.ans
        resp = chat_mod.chat(chat_mod.ChatRequest(message="вопрос", session_id="s1"), user=self.user)
        self.assertEqual(resp.input_hint, self.ans.input_hint)

    def test_stream_done_event_carries_hint(self):
        chat_mod.answer_stream = lambda *a, **k: iter([("delta", "Ответ"), ("done", self.ans)])
        resp = chat_mod.chat_stream(chat_mod.ChatRequest(message="вопрос", session_id="s2"), user=self.user)

        async def collect():
            return [chunk async for chunk in resp.body_iterator]

        body = "".join(c.decode() if isinstance(c, bytes) else c for c in asyncio.run(collect()))
        self.assertIn('"input_hint"', body)
        self.assertIn(self.ans.input_hint, body)


class TestPlaceholderState(unittest.TestCase):
    """Разметка и фронт: стартовая подсказка — с сервера, дальше её меняет состояние беседы."""

    @classmethod
    def setUpClass(cls):
        from app.api.web import _ctx, templates
        from app.db.models import User

        class _Req:  # Jinja-шаблону от запроса нужен только объект в контексте
            scope = {"type": "http"}
            session: dict = {}

        cls.html = templates.get_template("chat.html").render(
            **_ctx(_Req(), user=User(username="kursk.expert1", role="expert"),
                   kontur_719_url="https://example.test/", input_hint=followup.START_HINT))
        cls.js = (WEB / "static" / "chat.js").read_text(encoding="utf-8")

    def test_empty_chat_keeps_start_hint_rendered_by_server(self):
        self.assertIn(f'placeholder="{followup.START_HINT}"', self.html)

    def test_front_has_no_own_copy_of_any_hint(self):
        """Своя копия текста разъезжается молча — так уже случилось с адресом первоисточника."""
        for text in (followup.START_HINT, *SUGGESTIONS):
            self.assertNotIn(text, self.js, "во фронте появилась своя копия подсказки")
        self.assertIn('input.getAttribute("placeholder")', self.js)  # стартовую читает из разметки

    def test_placeholder_follows_conversation_state(self):
        self.assertIn('main.classList.contains("empty") ? START_HINT : inputHint', self.js)
        # Шесть точек переключения состояния: новый вопрос, готовый ответ (стриминг и фолбэк),
        # «Новый диалог», открытие беседы, удаление текущей. Пропусти любую — подсказка отстанет
        # от экрана, и это не будет видно ни по одной ошибке.
        self.assertGreaterEqual(self.js.count("setHint("), 7)  # объявление + шесть вызовов
        self.assertIn("done.input_hint", self.js)   # стриминг
        self.assertIn("data.input_hint", self.js)   # фолбэк

    def test_hint_returns_after_a_failed_send(self):
        """⚠ Сбой не должен оставлять поле без инструкции — а именно так и было.

        `ask()` гасит подсказку до отправки, и ни один путь отказа её не возвращал: 422 (в вопросе
        ПДн), 503 движка, обрыв сети. Человеку говорят «измените запрос» ровно в тот момент, когда
        подсказка из поля исчезла до конца сессии. До U5 плейсхолдер был статикой в шаблоне и
        переживал любой сбой. Счётчик вызовов такое поймать не мог: он считал шесть точек
        переключения, среди которых ни одной аварийной."""
        self.assertIn("hintBeforeAsk = inputHint", self.js)
        # три пути отказа: 422 (вопрос вернули в поле), 503 и обрыв сети
        self.assertEqual(self.js.count("setHint(hintBeforeAsk)"), 3)

    def test_hint_cannot_be_set_without_repaint(self):
        """Раньше это была ПАРА «присвоить + перерисовать», и половина без второй оставляла
        подсказку прошлого ответа висеть над свежим пустым чатом — молча, без ошибки."""
        body = self.js.split("function setHint(text) {")[1].split("}")[0]
        self.assertIn("inputHint = text", body)
        self.assertIn("setPlaceholder()", body)
        # присваивать inputHint напрямую можно только в объявлении и внутри сеттера
        self.assertEqual(self.js.count("inputHint = "), 2)

    def test_started_chat_does_not_reuse_start_hint(self):
        """Ради этого задача и заведена: в начатом диалоге подсказка не зовёт назвать новую продукцию.

        Стартовая подсказка обязана упоминаться РОВНО дважды — объявление и ветка пустого чата.
        Третье упоминание означает, что её вернули туда, где диалог уже идёт."""
        self.assertEqual(self.js.count("START_HINT"), 2)


if __name__ == "__main__":
    unittest.main()
