"""«Только по-русски» (28.10.2026): правило 7 системных промптов + рантайм-страховка от иероглифов.

DeepSeek изредка вставляет в русский ответ китайские токены (наблюдали «咨询服务» на «юридические
услуги»). Промпт запрещает, код вычищает — как эмодзи: по символу, в обычном ответе и в потоке.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from app.rag import pipeline  # noqa: E402

LEAK = "Юридические услуги (咨询服务) вне сферы ПП №719，уточните код ОКПД2：28.92.21.110 — «порог» не менее 30 баллов."


class TestCleanModelText(unittest.TestCase):
    def test_cjk_removed_and_counted(self):
        text, n = pipeline._clean_model_text(LEAK)
        self.assertEqual(n, 4, "считаются только вычищенные иероглифы, а не переведённые знаки")
        self.assertNotRegex(text, "[一-鿿]")
        self.assertIn("вне сферы ПП №719,уточните код ОКПД2:28.92.21.110", text,
                      "полноширинная запятая и двоеточие — обычные (как NFKC, без добавленного пробела)")

    def test_empty_brackets_are_the_accepted_price(self):
        """Цена посимвольной чистки (экран и запись не расходятся): от «(咨询服务)» остаются «()»."""
        self.assertIn("услуги ()", pipeline._clean_model_text(LEAK)[0])

    def test_fullwidth_digits_and_latin_are_converted_not_dropped(self):
        """Ревью PR #265, MED: «не менее １００ баллов» без перевода теряло число целиком — а пропуск числа
        проверка заземления не ловит. Полноширинные ASCII переводятся (как NFKC) и в счётчик не входят."""
        fw = "".join(chr(ord(c) + 0xFEE0) for c in "100")
        self.assertEqual(pipeline._clean_model_text(f"не менее {fw} баллов"), ("не менее 100 баллов", 0))
        model = "".join(chr(ord(c) + 0xFEE0) for c in "WEP-3ax")
        self.assertEqual(pipeline._clean_model_text(model)[0], "WEP-3ax")
        self.assertEqual(pipeline._clean_model_text("30" + chr(0xFF05))[0], "30%")

    def test_russian_latin_and_typography_untouched(self):
        clean = "Порог — не менее 30 баллов [1]; модель WEP-3ax, ОКПД2 26.30.11; «кавычки» № 5 … 50 %."
        self.assertEqual(pipeline._clean_model_text(clean), (clean, 0))

    def test_japanese_kana_removed(self):
        self.assertEqual(pipeline._clean_model_text("ответ ひらがなカタカナ готов")[1], 8)

    def test_everything_the_metric_calls_cjk_is_cleaned(self):
        """Всё, что `eval_answers.CJK_RE` называет иероглифом, рантайм вычищает. Поэтому метрика смотрит ещё и
        на `Answer.cjk_removed` — по одному тексту она давала бы 1.00 по построению (ревью PR #265, MED)."""
        from eval_answers import CJK_RE, leaked_cjk
        sample = "".join(chr(c) for c in range(0x3040, 0x30FF, 7)) + "".join(chr(c) for c in range(0x4E00, 0x9FFF, 997))
        text, n = pipeline._clean_model_text(sample)
        self.assertIsNone(CJK_RE.search(text))
        self.assertGreater(n, 0)
        self.assertTrue(leaked_cjk(text, SimpleNamespace(cjk_removed=n)), "протечка видна метрике и после чистки")
        self.assertFalse(leaked_cjk("чистый ответ", SimpleNamespace(cjk_removed=0)))


class TestAnswerPaths(unittest.TestCase):
    def _run(self, model_text):
        plan = pipeline._Plan(messages=[{"role": "user", "content": "в"}], grounding="30 баллов", hits=[],
                              cases=[], low_relevance=False)
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        halves = (model_text[:20], model_text[20:])   # кусок потока рвётся посреди слова

        def create(**kw):
            if kw.get("stream"):
                chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=h),
                                                                   finish_reason=None)], usage=None)
                          for h in halves]
                return iter(chunks + [SimpleNamespace(choices=[], usage=usage)])
            msg = SimpleNamespace(content=model_text)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(pipeline, "_plan_answer", return_value=plan), \
                mock.patch.object(pipeline, "_client", return_value=client):
            sync = pipeline.answer("в").text
            events = list(pipeline.answer_stream("в"))
        done = [p for k, p in events if k == "done"][0].text
        deltas = "".join(p for k, p in events if k == "delta")
        return sync, done, deltas

    def test_answer_reports_how_much_was_cleaned(self):
        """`Answer.cjk_removed` — и в обычном ответе, и в потоке (метрика читает его, а не только текст)."""
        plan = pipeline._Plan(messages=[{"role": "user", "content": "в"}], grounding="", hits=[], cases=[],
                              low_relevance=False)
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)

        def create(**kw):
            if kw.get("stream"):
                return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=LEAK),
                                                                      finish_reason=None)], usage=None),
                             SimpleNamespace(choices=[], usage=usage)])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=LEAK),
                                                            finish_reason="stop")], usage=usage)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(pipeline, "_plan_answer", return_value=plan),                 mock.patch.object(pipeline, "_client", return_value=client):
            self.assertEqual(pipeline.answer("в").cjk_removed, 4)
            done = [p for k, p in pipeline.answer_stream("в") if k == "done"][0]
        self.assertEqual(done.cjk_removed, 4)

    def test_no_cjk_reaches_the_user(self):
        for text in self._run(LEAK):
            self.assertNotRegex(text, "[一-鿿，：]")
            self.assertIn("вне сферы", text)

    def test_procedural_path_is_cleaned_too(self):
        """Третье место генерации — процедурный ответ — чистится тем же: имя функции в его коде."""
        import types

        def names(code):
            out = set(code.co_names)
            for c in code.co_consts:
                if isinstance(c, types.CodeType):
                    out |= names(c)
            return out
        for fn in (pipeline._answer_procedural, pipeline.answer, pipeline.answer_stream):
            self.assertIn("_clean_model_text", names(fn.__code__), fn.__name__)


class TestPromptRule(unittest.TestCase):
    def test_both_system_prompts_say_russian_only(self):
        from app.core.prompts import NAVIGATOR_SYSTEM_PROMPT, PROCEDURAL_SYSTEM_PROMPT
        for p in (NAVIGATOR_SYSTEM_PROMPT, PROCEDURAL_SYSTEM_PROMPT):
            self.assertIn("ТОЛЬКО ПО-РУССКИ", p)
            self.assertIn("иероглиф", p)


if __name__ == "__main__":
    unittest.main()
