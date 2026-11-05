"""Инварианты корпуса по ВСЕМ записям, независимые от правил сборки (`scripts/kb_invariants.py`): то, что видят модель
и эксперт, — без дублей и обрезанных копий требований, без пустых и обрывочных вводных, без вводной перед такой же.

Ревью PR #268, раунд 6: golden D24 и корпусный тест D25 брали оракулом тот же код, что проверяли, и закрепили дубли.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import kb_invariants as K  # noqa: E402

# Законные исключения — ПО ТЕКСТУ повторённой строки (не «запись + вид»: иначе новый дубль в записи-исключении
# прятался бы — ревью PR #268, раунд 7, LOW 4а), с обоснованием из первоисточника (ячейка HTML Контура 508574).
ALLOWED = {
    # «сварка, нанесение защитных покрытий несущей рамы машин самоходных (6 баллов);» стоит в перечне ячейки дважды
    "III-46": ["сварка нанесение защитных покрытий несущей рамы машин самоходных"],
    # требование о правах на документацию в ячейке дважды, варианты разделены «или»
    "VII-16": ["наличие у юридического лица налогового резидента стран членов евразийского экономического союза прав"],
    "VII-17": ["наличие у юридического лица налогового резидента стран членов евразийского экономического союза прав"],
    # то же — трижды, под вариантами по датам
    "VII-23": ["наличие у юридического лица налогового резидента стран членов евразийского экономического союза прав"],
    # требование о правах на КД и ТД в ячейке дважды — по подпунктам
    "XXII-12": ["наличие у юридического лица налогового резидента стран членов евразийского экономического союза прав"],
}


# Известные ДЕФЕКТЫ (не исключения): склейка HTML «требование + вводная перечня» в одной строке ячейки — класс
# отложенной задачи LOW 8 / LOW 5 раунда 7 (ярлыки разборщика, IV-31/32 без вводной). Есть на `dev` (`6a511e1`).
# Список только сокращается.
KNOWN_DEFECTS = {
    "XXIII-3": ["наличие у юридического лица налогового резидента государств членов евразийского экономического союза"],
}


def allowed(key: str, text: str) -> bool:
    return any(frag in K.norm(text) for frag in ALLOWED.get(key, []) + KNOWN_DEFECTS.get(key, []))


class TestCorpusInvariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = K.violations()

    def test_no_violations_beyond_allowed(self):
        bad = {k: [t for kind, t in items if not (kind == "а" and allowed(k, t))]
               for k, items in self.v.items()}
        bad = {k: t for k, t in bad.items() if t}
        self.assertEqual(bad, {})

    def test_allowed_list_is_live(self):
        """Исключение без нарушения — устаревшее: список только сокращается."""
        dead = [(k, f) for k, frags in list(ALLOWED.items()) + list(KNOWN_DEFECTS.items()) for f in frags
                if not any(f in K.norm(t) for _, t in self.v.get(k, []))]
        self.assertEqual(dead, [])

    def test_new_duplicate_in_allowed_record_is_caught(self):
        """LOW 4а: две одинаковые операции в записи-исключении VII-16 — нарушение, исключение его не прячет."""
        import copy
        r = copy.deepcopy(next(x for x in K.load() if K.key(x) == "VII-16"))
        r["requirement_blocks"].append({"component": "узел", "note": None, "operations": [
            {"text": "сборка и наладка изделия на производственной площадке заявителя", "points": 5},
            {"text": "сборка и наладка изделия на производственной площадке заявителя", "points": 5}]})
        self.assertTrue([t for k, t in K.record_violations(r) if k == "а" and not allowed("VII-16", t)])

    def test_requirement_inside_heading_and_rf_form(self):
        """LOW 4б, 4в: требование целиком внутри вводной (IV-32 на `f1d9ec1`); «РФ» = «Российской Федерации»."""
        req = "наличие у юридического лица исключительных прав на программное обеспечение, используемое в продукции"
        r = {"section_roman": "IV", "section_title": "Раздел", "product_name": "Изделие", "okpd2_codes": [],
             "min_threshold": None, "source_anchor": "x", "requirement_blocks": [
                 {"component": req, "operations": [], "note": None},
                 {"component": req + ", производство на территории РФ следующих операций:", "note": None,
                  "operations": [{"text": "сборка", "points": 5}]}]}
        self.assertTrue([t for k, t in K.record_violations(r) if "во вводной" in t])
        self.assertEqual(K.norm("на территории Российской Федерации"), K.norm("на территории РФ"))

    def test_invariant_catches_known_defects(self):
        """Положительный контроль на синтетике: дубль-обрезок, вводная-обрывок, вводная перед такой же."""
        def rec(blocks):
            return {"section_roman": "IX", "section_title": "Раздел", "product_name": "Изделие", "okpd2_codes": [],
                    "min_threshold": None, "requirement_blocks": blocks, "source_anchor": "x"}
        r = rec([{"component": "наличие у производителя структурных подразделений и (или) аккредитованных лабораторий,",
                  "operations": [], "note": None},
                 {"component": "наличие у производителя структурных подразделений и (или) аккредитованных лабораторий,"
                               " отвечающих за проверку качества", "operations": [], "note": None}])
        self.assertTrue([k for k, _ in K.record_violations(r) if k == "а"])
        r = rec([{"component": "составе:", "operations": [], "note": None},
                 {"component": "узел", "operations": [{"text": "сборка", "points": 5}], "note": None}])
        self.assertTrue([k for k, _ in K.record_violations(r) if k == "б"])
        r = rec([{"component": "осуществление на территории Российской Федерации следующих операций:", "operations": [],
                  "note": None},
                 {"component": "осуществление на территории Российской Федерации следующих операций:",
                  "operations": [{"text": "сборка", "points": 5}], "note": None}])
        self.assertTrue([t for k, t in K.record_violations(r) if k == "в" and not t.startswith("панель")])
        self.assertTrue([t for k, t in K.record_violations(r) if k == "в" and t.startswith("панель")])


if __name__ == "__main__":
    unittest.main()
