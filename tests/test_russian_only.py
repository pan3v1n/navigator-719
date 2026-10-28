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
        self.assertEqual(n, 4)
        self.assertNotRegex(text, "[一-鿿]")
        self.assertIn("вне сферы ПП №719, уточните код ОКПД2: 28.92.21.110", text,
                      "полноширинная запятая и двоеточие — обычные, с пробелом после")

    def test_russian_latin_and_typography_untouched(self):
        clean = "Порог — не менее 30 баллов [1]; модель WEP-3ax, ОКПД2 26.30.11; «кавычки» № 5 … 50 %."
        self.assertEqual(pipeline._clean_model_text(clean), (clean, 0))

    def test_japanese_kana_removed(self):
        self.assertEqual(pipeline._clean_model_text("ответ ひらがなカタカナ готов")[1], 8)

    def test_everything_the_metric_calls_cjk_is_cleaned(self):
        """Связка с замером: `eval_answers.CJK_RE` (метрика «без иероглифов») — то, что рантайм
        обязан вычищать целиком. Иначе метрика краснела бы на уже «вычищенном» ответе."""
        from eval_answers import CJK_RE
        sample = "".join(chr(c) for c in range(0x3040, 0x30FF, 7)) + "".join(chr(c) for c in range(0x4E00, 0x9FFF, 997))
        text, _ = pipeline._clean_model_text(sample)
        self.assertIsNone(CJK_RE.search(text))


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
