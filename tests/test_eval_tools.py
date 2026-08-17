"""Инструменты замеров EV1–EV4: то, что можно проверить офлайн, без Qdrant и DeepSeek.

Замерный код опаснее продуктового: если он врёт, врут все решения, принятые по его числам.
В этом проекте так уже было — диагностика сравнивала якоря, обрезанные до 28 символов, и
показывала «ретрив стабилен», хотя нестабилен был именно он. Поэтому чистые части метрик
(разбор утверждений, n-граммы, отбор кейсов, фильтр ПДн) закрыты тестами.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.core.prompts import NAVIGATOR_SYSTEM_PROMPT  # noqa: E402
from app.core.sensitive import detect  # noqa: E402


class TestForeignNumbersRule(unittest.TestCase):
    """EV1: правило 3в — числа только из позиции, о которой идёт речь."""

    def test_rule_forbids_numbers_of_other_candidates(self):
        self.assertIn("3в.", NAVIGATOR_SYSTEM_PROMPT)
        rule = NAVIGATOR_SYSTEM_PROMPT.split("3в.")[1][:1100]
        self.assertIn("БЕЗ их баллов", rule)
        # исключение обязано остаться: прямая просьба сравнить позиции — законный сценарий
        self.assertIn("СРАВНИТЬ", rule)

    def test_rule_does_not_reinstate_the_R7_defect(self):
        """Первая версия 3в велела на позиции без баллов писать «баллы не приведены» и просить код.

        Оба указания спорили с кодом, который специально шёл другим путём: `format_context` при
        требованиях-перечне печатает «Порог: не предусмотрен — требования заданы ПЕРЕЧНЕМ
        обязательных операций» именно потому, что молчание заставляло модель писать «в контексте не
        указан», а это читалось как пробел в данных и было **жалобой №1** платного теста (R7).
        Просьба уточнить код спорила с правилом 1ж, которое её прямо запрещает при совпадении по
        коду. Позиций без баллов в корпусе — сотни, так что цена ошибки не краевая."""
        rule = NAVIGATOR_SYSTEM_PROMPT.split("3в.")[1][:1100]
        self.assertNotIn("попроси уточнить код", rule)
        self.assertNotIn("баллы для этой позиции в контексте не приведены", rule)
        # правило обязано отдавать этот случай КОНТЕКСТУ, а не решать за него
        self.assertIn("не предусмотрен", rule)

    def test_context_still_owns_the_no_points_wording(self):
        """R7 остаётся в силе: формулировку про отсутствие порога задаёт контекст, а не промпт."""
        import inspect

        from app.rag import pipeline
        src = inspect.getsource(pipeline.format_context)
        self.assertIn("Порог: не предусмотрен", src)
        self.assertIn("ПЕРЕЧНЕМ", src)

    def test_rule_sits_after_code_priority_rule(self):
        """3в опирается на «позицию, на которую опираешься» из правил 3 и 3а — порядок важен."""
        self.assertLess(NAVIGATOR_SYSTEM_PROMPT.index("3а."), NAVIGATOR_SYSTEM_PROMPT.index("3в."))


class TestBorderlineSet(unittest.TestCase):
    """EV2: набор пограничных in-scope — вторая половина весов guard'а."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / "scripts" / "eval_golden_borderline.json")
                              .read_text(encoding="utf-8"))
        cls.cases = cls.data["cases"]

    def test_size_and_shape(self):
        self.assertGreaterEqual(len(self.cases), 20)
        for c in self.cases:
            self.assertTrue(c["in_scope"], c["id"])
            self.assertTrue(c["query"].strip(), c["id"])
            self.assertTrue(c["evidence"], f"{c['id']}: кейс без признака in-scope бесполезен")
            for e in c["evidence"]:
                self.assertIn(e, ("expert", "retrieval"), c["id"])

    def test_no_personal_data(self):
        """152-ФЗ: тексты взяты из боевой базы, поэтому проверка обязательна, а не «на всякий случай»."""
        for c in self.cases:
            self.assertEqual(detect(c["query"]), [], f"{c['id']}: ПДн в наборе репозитория")

    def test_no_okpd2_code_in_queries(self):
        """Ловушка отбора: при совпадении по коду guard подавлен, и такой кейс мерит не то."""
        import re
        for c in self.cases:
            self.assertIsNone(re.search(r"\b\d{2}\.\d{2}(?:\.\d+)*\b", c["query"]),
                              f"{c['id']}: код ОКПД2 в пограничном кейсе")

    def test_queries_are_unique(self):
        qs = [c["query"].lower() for c in self.cases]
        self.assertEqual(len(qs), len(set(qs)))

    def test_guard_script_knows_the_set(self):
        import eval_guard
        self.assertTrue(eval_guard.BORDER.exists())
        self.assertTrue(hasattr(eval_guard, "run_borderline"))
        self.assertTrue(hasattr(eval_guard, "run_threshold"))

    def test_default_mode_runs_both_halves_of_the_scales(self):
        """По негативам одним порог хочется поднимать бесконечно — цена видна только вместе с
        ложными флагами. Прежний `both` молча пропускал эту половину, а докстринг обещал «два
        режима», когда их стало пять."""
        import inspect

        import eval_guard
        src = inspect.getsource(eval_guard.main)
        for mode in ("run_negative", "run_borderline", "run_threshold", "run_confusable"):
            self.assertIn(mode, src)
        # каждая секция должна запускаться и при `both`
        self.assertIn('"borderline", "both", "guard"', src)
        self.assertIn('"threshold", "both", "guard"', src)
        self.assertIn("borderline", eval_guard.__doc__)
        self.assertNotIn("Два режима", eval_guard.__doc__)

    def test_threshold_curve_reports_plateau_not_argmin(self):
        """Равные суммы на сетке 0.005 — норма, и «первый минимум» всегда самый permissive конец:
        прочитав его как рекомендацию, порог понизят и добавят false-accept за нулевой выигрыш."""
        import inspect

        import eval_guard
        src = inspect.getsource(eval_guard.run_threshold)
        self.assertIn("plateau", src)
        self.assertIn("ПЛАТО", src)
        self.assertIn("НЕ равноценны", src)

    def test_determinism_window_matches_production(self):
        """Гейт «чужие числа» при окне 5 структурно не видит утечек с рангов 6–8, а прод берёт 8."""
        import inspect

        import eval_determinism

        from app.rag import pipeline
        src = inspect.getsource(eval_determinism.main)
        self.assertIn('"--limit", type=int, default=8', src)
        prod = inspect.signature(pipeline.answer).parameters["limit"].default
        self.assertEqual(prod, 8, "боевое окно изменилось — гейт замера обязан идти за ним")


class TestTextFaithfulnessCore(unittest.TestCase):
    """EV4: разбор утверждений и n-граммы — чистая часть, её и проверяем."""

    @classmethod
    def setUpClass(cls):
        import eval_text_faithfulness as m
        cls.m = m

    def test_table_rows_are_not_claims(self):
        """Перечень баллов печатает КОД дословно — ссылка [N] ему не нужна, искажать нечего."""
        text = "Вот требования [1].\n| Операция | Баллы |\n|---|---|\n| сварка кузова | 6 |"
        got = self.m.claims(text)
        self.assertEqual(got, ["Вот требования [1]."])

    def test_meta_statements_are_excluded(self):
        """Мета-утверждения предписаны промптом; штрафовать за них — оптимизировать в молчание."""
        for meta in ("**Порог:** в контексте для этой позиции не указан [1].",
                     "Уточните, к какой категории относится ваша машина [1].",
                     "**Важно:** показаны 60 из 676 операций [1]."):
            self.assertTrue(self.m.META_RE.search(meta), meta)

    def test_paraphrase_is_grounded_and_invention_is_not(self):
        ctx = "Выполнение операций сварки кузова и окраски кузова на территории Российской Федерации"
        grams = self.m._ngrams(ctx)
        near = self.m.grounded_share("сварки кузова на территории Российской Федерации", grams)
        far = self.m.grounded_share("лазерная резка титанового профиля", grams)
        self.assertGreater(near, far)
        self.assertEqual(far, 0.0)

    def test_novel_words_ignore_generic_connectives(self):
        ctx_words = set(self.m.tokenize("сварка кузова на территории Российской Федерации"))
        self.assertEqual(self.m.novel_words("это также требование [1]", ctx_words), [])
        novel = self.m.novel_words("гальваническое покрытие", ctx_words)
        self.assertTrue(any(w.startswith("гальваническ") for w in novel), novel)


class TestGsNaturalBuilder(unittest.TestCase):
    """EV3: сборка gs_natural из оценённых ответов волны — на синтетической базе."""

    @classmethod
    def setUpClass(cls):
        import build_gs_natural as m
        cls.m = m

    def _db(self, rows, feedback):
        path = Path(self.tmp) / "wave.db"
        db = sqlite3.connect(path)
        db.execute("create table messages (id integer primary key, session_id text, ts text, "
                   "role text, content text, sources_json text, low_relevance integer)")
        db.execute("create table feedback (id integer primary key, message_id integer, rating integer)")
        db.executemany("insert into messages values (?,?,?,?,?,?,?)", rows)
        db.executemany("insert into feedback (message_id, rating) values (?,?)", feedback)
        db.commit()
        db.close()
        return path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_takes_rated_pair_and_drops_the_rest(self):
        anchor = json.dumps([{"source_anchor": "Раздел IV, поз. 12", "okpd2": ["28.13.14"]}],
                            ensure_ascii=False)
        rows = [
            (1, "s1", "t1", "user", "делаем гидравлические насосы для нефтепереработки", None, 0),
            (2, "s1", "t2", "assistant", "ответ", anchor, 0),
            (3, "s2", "t3", "user", "да", None, 0),                       # продолжение диалога
            (4, "s2", "t4", "assistant", "ответ", anchor, 0),
            (5, "s3", "t5", "user", "какой порядок внесения в реестр", None, 0),   # процедурный
            (6, "s3", "t6", "assistant", "ответ", anchor, 0),
            (7, "s4", "t7", "user", "производим станки, СНИЛС 112-233-445 95", None, 0),  # ПДн
            (8, "s4", "t8", "assistant", "ответ", anchor, 0),
            (9, "s5", "t9", "user", "выпускаем промышленные чиллеры", None, 0),
            (10, "s5", "t10", "assistant", "ответ", "[]", 0),             # без якоря позиции
        ]
        fb = [(2, 5), (4, 5), (6, 5), (8, 5), (10, 5)]
        cases, stats = self.m.collect(self._db(rows, fb), 4, "3")
        self.assertEqual([c["query"] for c in cases],
                         ["делаем гидравлические насосы для нефтепереработки"])
        self.assertEqual(cases[0]["expect"]["anchor"], "Раздел IV, поз. 12")
        self.assertEqual(stats["отброшено по ПДн"], 1, "вопрос с ПДн обязан отсеяться")

    def test_low_rating_is_not_evidence(self):
        anchor = json.dumps([{"source_anchor": "Раздел IV, поз. 12"}], ensure_ascii=False)
        rows = [(1, "s1", "t1", "user", "делаем гидравлические насосы", None, 0),
                (2, "s1", "t2", "assistant", "ответ", anchor, 0)]
        cases, _ = self.m.collect(self._db(rows, [(2, 3)]), 4, "3")
        self.assertEqual(cases, [], "оценка 3★ не подтверждает, что позиция верна")

    def test_flagged_answer_is_not_a_gold_label(self):
        """Оценка ≥4★ на ответе с поднятым флагом означает «честно сказал, что совпадения нет».

        Правило 1г в этом случае ЗАПРЕЩАЕТ называть баллы и требует список кандидатов, поэтому
        `source_anchor` там — догадка ретрива, а не подтверждённая экспертом позиция. Взяв её
        эталоном, мы бы мерили главную цифру ретрива по собственной догадке."""
        anchor = json.dumps([{"source_anchor": "Раздел XXII, поз. 3"}], ensure_ascii=False)
        rows = [(1, "s1", "t1", "user", "пластичная высокотемпературная смазка", None, 0),
                (2, "s1", "t2", "assistant", "Точного совпадения не нашёл…", anchor, 1)]
        cases, stats = self.m.collect(self._db(rows, [(2, 5)]), 4, "3")
        self.assertEqual(cases, [], "кейс с поднятым флагом не может быть эталоном")
        self.assertEqual(stats["отброшено: guard поднимал флаг"], 1)


if __name__ == "__main__":
    unittest.main()
