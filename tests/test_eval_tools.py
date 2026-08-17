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
        rule = NAVIGATOR_SYSTEM_PROMPT.split("3в.")[1][:900]
        self.assertIn("БЕЗ их баллов", rule)
        # исключение обязано остаться: прямая просьба сравнить позиции — законный сценарий
        self.assertIn("СРАВНИТЬ", rule)
        # и честный выход, когда чисел у целевой позиции нет
        self.assertIn("не приведены", rule)

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
                   "role text, content text, sources_json text)")
        db.execute("create table feedback (id integer primary key, message_id integer, rating integer)")
        db.executemany("insert into messages values (?,?,?,?,?,?)", rows)
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
            (1, "s1", "t1", "user", "делаем гидравлические насосы для нефтепереработки", None),
            (2, "s1", "t2", "assistant", "ответ", anchor),
            (3, "s2", "t3", "user", "да", None),                       # продолжение диалога
            (4, "s2", "t4", "assistant", "ответ", anchor),
            (5, "s3", "t5", "user", "какой порядок внесения в реестр", None),   # процедурный
            (6, "s3", "t6", "assistant", "ответ", anchor),
            (7, "s4", "t7", "user", "производим станки, СНИЛС 112-233-445 95", None),  # ПДн
            (8, "s4", "t8", "assistant", "ответ", anchor),
            (9, "s5", "t9", "user", "выпускаем промышленные чиллеры", None),
            (10, "s5", "t10", "assistant", "ответ", "[]"),             # без якоря позиции
        ]
        fb = [(2, 5), (4, 5), (6, 5), (8, 5), (10, 5)]
        cases, stats = self.m.collect(self._db(rows, fb), 4, "3")
        self.assertEqual([c["query"] for c in cases],
                         ["делаем гидравлические насосы для нефтепереработки"])
        self.assertEqual(cases[0]["expect"]["anchor"], "Раздел IV, поз. 12")
        self.assertEqual(stats["отброшено по ПДн"], 1, "вопрос с ПДн обязан отсеяться")

    def test_low_rating_is_not_evidence(self):
        anchor = json.dumps([{"source_anchor": "Раздел IV, поз. 12"}], ensure_ascii=False)
        rows = [(1, "s1", "t1", "user", "делаем гидравлические насосы", None),
                (2, "s1", "t2", "assistant", "ответ", anchor)]
        cases, _ = self.m.collect(self._db(rows, [(2, 3)]), 4, "3")
        self.assertEqual(cases, [], "оценка 3★ не подтверждает, что позиция верна")


if __name__ == "__main__":
    unittest.main()
