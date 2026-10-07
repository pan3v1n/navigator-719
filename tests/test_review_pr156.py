"""Ревью PR #156 (актуализация под ред. 29.09.2026, medium): 10 находок.

Каждый тест закрепляет исправленную находку и проверен мутацией: правка отменяется — тест
краснеет. Отклонённая находка 2 («конфликт видов — пропустить таблицу, а не весь поиск») закреплена
отдельно: оба варианта на корпусе дают 0 расхождений, выбор сделан направлением ошибки.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from app.rag import procedural, retriever, thresholds, topics  # noqa: E402

DEPLOY_SH = ROOT / "scripts" / "deploy" / "deploy.sh"


class TestFootnoteMarkRoutes(unittest.TestCase):
    """Находка 1: записи «<12(1)>» завели, а маршрут вопрос с таким маркером до них не пускал."""

    def test_parenthesised_marker_reaches_the_procedural_branch(self):
        for q in ("что означает <12(1)>", "что значит <40(2)> в требованиях"):
            self.assertTrue(procedural.is_procedural(q), q)
            self.assertEqual(topics.classify(q), "classification", q)

    def test_plain_marker_still_routes(self):
        self.assertTrue(procedural.is_procedural("что означает <44>"))

    def test_code_in_question_still_wins(self):
        """Код ОКПД2 в вопросе отменяет правило сноски — как и прежде."""
        self.assertFalse(procedural.is_procedural("требования к 28.22.14 и что значит <12(1)>"))

    def test_window_quota_sees_the_marker(self):
        pat = next(p for doc, ps in retriever._RULES_TOPIC if doc == "appendix_footnotes" for p in ps[:1])
        self.assertRegex("что означает <12(1)>", pat)

    def test_one_definition_for_three_places(self):
        """Три копии регулярки — ровно так урок и не переезжал между модулями."""
        mark = topics.FOOTNOTE_MARK
        self.assertIn(mark, procedural._FOOTNOTE_REF_RE.pattern)
        foot = next(ps for doc, ps in retriever._RULES_TOPIC if doc == "appendix_footnotes")
        self.assertIn(mark, foot[0].pattern)
        self.assertIn(mark, topics._PATTERNS["classification"][0].pattern)

    def test_places_reference_the_constant_not_a_copy(self):
        """⚠ Посимвольная копия проходит тест выше (мутация M2 не ловилась) и расходится молча при
        следующей правке. Проверка по AST: в выражении ОБЯЗАНО стоять имя константы; проза
        комментария в дерево не попадает (урок раунда 10 — текст исходника считать нельзя)."""
        import ast

        for mod, target in ((procedural, "_FOOTNOTE_REF_RE"), (retriever, "_RULES_TOPIC"),
                            (topics, "_PATTERNS")):
            tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
            node = next(n for n in ast.walk(tree)
                        if isinstance(n, (ast.Assign, ast.AnnAssign))
                        and any(getattr(t, "id", None) == target
                                for t in (n.targets if isinstance(n, ast.Assign) else [n.target])))
            names = {getattr(x, "id", None) or getattr(x, "attr", None) for x in ast.walk(node)}
            self.assertIn("FOOTNOTE_MARK", names, f"{mod.__name__}.{target} держит свою копию маркера")


class TestNoteStepCleanup(unittest.TestCase):
    """Находка 4: хвост цитаты акта и повторы кода в строке «Порог:»."""

    def test_dangling_quote(self):
        self.assertEqual(thresholds._drop_dangling_quote('не менее 3300 баллов"'), "не менее 3300 баллов")
        self.assertEqual(thresholds._drop_dangling_quote('для "Изделия"'), 'для "Изделия"')
        self.assertEqual(thresholds._drop_dangling_quote("не менее 5 баллов"), "не менее 5 баллов")

    def test_survey_vessels_threshold_is_clean(self):
        thr = thresholds.lookup_threshold(["30.11.33.191"], "Суда промерные", "XVIII") or ""
        self.assertIn("не менее 3300 баллов [прим. 17]", thr, thr[:200])
        self.assertNotIn('баллов"', thr)
        self.assertEqual(thr.count("30.11.33.190"), 1, "код группы повторён в оговорке")
        # ⚠ Оговорка о группе ОСТАЁТСЯ (часть находки 4 отклонена): примечание пишет «из
        # 30.11.33.190», а код 30.11.33.191 позиции — вывод по имени, а не слово первоисточника.
        self.assertIn("группы кодов", thr)


class TestListVerdictIsCodeBound(unittest.TestCase):
    """Находка 3: вердикт по видам перечня — только по строкам, чей код покрывает код позиции."""

    TABLE = {"note": "x", "section": None, "rows": [
        {"codes": ["11.11.11.110"], "name": "Альфа", "by_year": {"y": "не менее 1 баллов"}},
        {"codes": ["11.11.11.120"], "name": "Бета", "by_year": {"y": "не менее 1 баллов"}},
    ]}

    def test_foreign_code_gets_nothing(self):
        t = {**self.TABLE}
        self.assertIsNone(thresholds._list_items_verdict(t, "Альфа;\nБета", ["99.99.99.990"]))

    def test_own_code_gets_the_shared_row(self):
        t = {**self.TABLE}
        got = thresholds._list_items_verdict(t, "Альфа;\nБета", ["11.11.11.110", "11.11.11.120"])
        self.assertIsInstance(got, dict)


class TestMergedCellsOnlyWhenWholeRowIsEmpty(unittest.TestCase):
    """Находка 6: пустая ячейка ОДНОГО года — не объединённая, и числом соседа не заполняется."""

    LINES = [
        "17(3). Продукция судостроения подлежит отнесению при условии достижения баллов:",
        "Код по ОК 034-2014|Наименование|до 1 января 2029 г.,",
        "не менее баллов|с 1 января 2029 г.,",
        "не менее баллов|",
        "30.11.11.110|Изделие А|10|20|",
        "30.11.11.120|Изделие Б|||",
        "30.11.11.130|Изделие В|30||",
        "",
    ]

    def _rows(self):
        table, _k = thresholds._unit_header_table(self.LINES, 0, "17(3)", "XVIII")
        self.assertIsNotNone(table, "синтетическая таблица не разобрана — тест сам себя не проверяет")
        return {r["name"]: r["by_year"] for r in table["rows"]}

    def test_whole_row_empty_inherits(self):
        self.assertEqual(self._rows()["Изделие Б"]["до 1 января 2029 г."], "не менее 10 баллов")

    def test_partially_empty_row_is_not_invented(self):
        self.assertNotIn("Изделие В", self._rows())


class TestClassifierMirrorsRuntime(unittest.TestCase):
    """Находка 5: «list_mixed» — только там, где рантайм этот вердикт действительно спрашивал."""

    WINCHES = ("Швартовные лебедки;\nтраловые лебедки;\nгиневые лебедки;\n"
               "вспомогательные лебедки промысловой палубы")

    def _cls(self, codes):
        import classify_missing_thresholds as C

        rec = {"product_name": self.WINCHES, "okpd2_codes": codes, "section_roman": "XVIII",
               "requirement_type": "points", "min_threshold": ""}
        return C.classify(rec, C.note_rows())["class"]

    def test_positive_control_with_code(self):
        self.assertEqual(self._cls(["28.22.12.190"]), "list_mixed")

    def test_without_code_the_runtime_never_asked(self):
        self.assertNotEqual(self._cls([]), "list_mixed")

    def test_procurement_table_is_not_the_runtime_s_reason(self):
        """Конфликт видов в ЗАКУПОЧНОЙ таблице: общий порог рантайм по ней не ищет вовсе.
        ⚠ Синтетика — на корпусе ред. 29.09.2026 такого случая нет; без неё фильтр области
        ничем не проверен (мутация «фильтр снят» оставляла батарею зелёной)."""
        from unittest import mock

        import classify_missing_thresholds as C

        table = {"note": "ZZ", "section": None, "rows": [
            {"codes": ["11.11.11.110"], "name": "Альфа", "by_year": {"y": "не менее 1 баллов"}},
            {"codes": ["11.11.11.120"], "name": "Бета", "by_year": {"y": "не менее 2 баллов"}},
        ]}
        rec = {"product_name": "Альфа;\nБета", "okpd2_codes": ["11.11.11.110", "11.11.11.120"],
               "section_roman": "I", "requirement_type": "points", "min_threshold": ""}
        for scope, want_mixed in (("general", True), ("procurement", False)):
            with self.subTest(scope=scope), \
                    mock.patch.object(C, "_tables", return_value=[dict(table)]), \
                    mock.patch.object(C, "note_scope", side_effect=lambda n, s=scope: s if n == "ZZ" else "general"):
                got = C.classify(rec, [])["class"]
                self.assertEqual(got == "list_mixed", want_mixed, got)


class TestConflictStopsTheLookup(unittest.TestCase):
    """Находка 2 ОТКЛОНЕНА замером: «пропустить таблицу» вместо «прекратить поиск» на корпусе
    ред. 29.09.2026 даёт 0 расхождений по 1534 записям (общий, закупочный, без раздела, класс).
    При неразличимых вариантах выбирает направление ошибки: неверный порог дороже отсутствующего,
    а продолжение поиска после конфликта отдаёт позицию правилам «≥2 общих слова» и группе."""

    def test_mixed_list_has_no_threshold(self):
        self.assertIsNone(thresholds.lookup_threshold(["28.22.12.190"], TestClassifierMirrorsRuntime.WINCHES,
                                                      "XVIII"))


@unittest.skipUnless(shutil.which("bash"), "bash недоступен")
class TestReindexNamesTheCollection(unittest.TestCase):
    """Находка 10: загрузчик получает имя коллекции из профиля, а не из `.env` VM."""

    def _env(self, c: str) -> str:
        fn = subprocess.run(["sed", "-n", "/^reindex_env()/,/^}/p", str(DEPLOY_SH)],
                            capture_output=True, text=True, encoding="utf-8").stdout
        self.assertIn("reindex_env()", fn, "функции reindex_env нет в deploy.sh")
        r = subprocess.run([shutil.which("bash"), "-c", fn + f'\nreindex_env "{c}"'],
                           capture_output=True, text=True, encoding="utf-8")
        return r.stdout.strip()

    def test_mapping(self):
        self.assertEqual(self._env("pp719"), "QDRANT_COLLECTION=pp719")
        self.assertEqual(self._env("pp719_rules"), "QDRANT_RULES_COLLECTION=pp719_rules")

    def test_step_passes_it(self):
        code = DEPLOY_SH.read_text(encoding="utf-8")
        self.assertIn('exec -T -e "$(reindex_env "$c")" app python "$(reindex_loader "$c")"', code)


if __name__ == "__main__":
    unittest.main()
