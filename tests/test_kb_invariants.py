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

# Законные исключения — поимённо, с обоснованием из первоисточника (ячейка HTML Контура 508574). Не порог.
ALLOWED = {
    # строка повторена в самой ячейке: «сварка, нанесение защитных покрытий несущей рамы машин самоходных (6 баллов);»
    # стоит в перечне дважды подряд
    "III-46": {"а"},
    # требование о правах на документацию повторено в ячейке под вариантами по датам («до/с 1 января 2019 г.») —
    # у VII-16, VII-17, VII-23 в ячейке 4–6 одинаковых строк «наличие у юридического лица … прав на техническую
    # документацию и конструкторскую документацию …»
    "VII-16": {"а"}, "VII-17": {"а"}, "VII-23": {"а"},
    # то же в ячейке XXII-12: четыре строки «наличие у юридического лица … <6> прав на конструкторскую, технологическую
    # документацию …» — по одной на подпункт
    "XXII-12": {"а"},
}


class TestCorpusInvariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = K.violations()

    def test_no_violations_beyond_allowed(self):
        bad = {k: [t for kind, t in items if kind not in ALLOWED.get(k, set())]
               for k, items in self.v.items()}
        bad = {k: t for k, t in bad.items() if t}
        self.assertEqual(bad, {})

    def test_allowed_list_is_live(self):
        """Исключение без нарушения — устаревшее: список только сокращается."""
        dead = [k for k in ALLOWED if k not in self.v]
        self.assertEqual(dead, [])

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
        self.assertTrue([k for k, _ in K.record_violations(r) if k == "в"])


if __name__ == "__main__":
    unittest.main()
