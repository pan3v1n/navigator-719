"""Юнит-тесты целостности базы знаний (без Qdrant, сети и DeepSeek).

Покрывают правило чистки записей-заглушек исключённых позиций приложения
(`scripts/drop_excluded_positions.is_excluded_stub`) — обе формы заглушки:
с пустым наименованием и с наименованием, равным заголовку раздела (D4).

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.drop_excluded_positions import is_excluded_stub  # noqa: E402

SECTION = "Продукция радиоэлектроники <5>"


def rec(**kw) -> dict:
    """Запись structured/*.json с разумными умолчаниями."""
    base = {
        "section_roman": "IX",
        "section_title": SECTION,
        "product_name": "Планшетные компьютеры",
        "okpd2_codes": ["26.20.11.130"],
        "requirement_blocks": [],
        "min_threshold": None,
    }
    base.update(kw)
    return base


class TestExcludedStub(unittest.TestCase):
    def test_empty_name_is_stub(self):
        """Прежняя форма: у строки-кода нет ни имени, ни требований."""
        self.assertTrue(is_excluded_stub(rec(product_name="")))

    def test_name_equals_section_title_is_stub(self):
        """Форма D4: LLM подставила в product_name заголовок раздела."""
        self.assertTrue(is_excluded_stub(rec(product_name=SECTION)))

    def test_name_equals_section_title_without_footnote_is_stub(self):
        """Маркер сноски в заголовке не должен мешать сопоставлению."""
        self.assertTrue(is_excluded_stub(rec(product_name="Продукция радиоэлектроники")))

    def test_real_product_is_kept(self):
        self.assertFalse(is_excluded_stub(rec()))

    def test_section_named_record_with_requirements_is_kept(self):
        """Совпадение имени с разделом само по себе не приговор: есть требования — оставляем."""
        blocks = [{"component": "Сборка", "operations": [{"text": "сборка", "points": 10}]}]
        self.assertFalse(is_excluded_stub(rec(product_name=SECTION, requirement_blocks=blocks)))

    def test_section_named_record_with_threshold_is_kept(self):
        self.assertFalse(is_excluded_stub(rec(product_name=SECTION, min_threshold="не менее 50 баллов")))

    def test_methodology_record_is_never_touched(self):
        """Методичка раздела (section_methodology) не продукт и под правило не подпадает."""
        self.assertFalse(
            is_excluded_stub(rec(product_name="", record_type="section_methodology"))
        )

    def test_empty_section_title_does_not_match_empty_name_twice(self):
        """Пустой заголовок не должен превращать любой продукт в заглушку."""
        self.assertFalse(is_excluded_stub(rec(section_title="", product_name="Ярусники")))


class TestCorpusHasNoExcludedStubs(unittest.TestCase):
    """Регресс на боевых данных: в корпусе не должно остаться ни одной заглушки."""

    def test_structured_corpus_is_clean(self):
        import json

        struct = ROOT / "knowledge_base" / "pp719" / "structured"
        if not struct.exists():  # на CI без данных тест не имеет смысла
            self.skipTest("structured/ отсутствует")
        offenders: list[str] = []
        for jf in sorted(struct.glob("*.json")):
            for i, r in enumerate(json.loads(jf.read_text(encoding="utf-8"))):
                if is_excluded_stub(r):
                    offenders.append(f"{jf.name}[{i}] {r.get('okpd2_codes')}")
        self.assertEqual(offenders, [], f"остались записи-заглушки: {offenders}")


class TestAppendixFootnotes(unittest.TestCase):
    """D3: определения сносок приложения режутся отдельным чанком и попадают в корпус."""

    def test_footnotes_chunk_exists_and_is_separate(self):
        chunks = ROOT / "knowledge_base" / "pp719" / "chunks"
        if not chunks.exists():
            self.skipTest("chunks/ отсутствует")
        foot = chunks / "131_SNOSKI_prilozheniya.txt"
        self.assertTrue(foot.exists(), "нет чанка сносок — пересобрать rechunk_appendix.py --write")
        xxix = next(iter(chunks.glob("*XXIX*.txt")), None)
        self.assertIsNotNone(xxix, "нет чанка XXIX")
        body = xxix.read_text(encoding="utf-8")
        self.assertNotIn(
            "<1> Комплектующие изделия, произведенные на территории стран",
            body,
            "определения сносок снова прилипли к разделу XXIX",
        )

    def test_footnote_44_is_parsed_with_current_edition(self):
        """<44> ужесточена ред. N 923: требование засчитывается только при акте экспертизы ТПП."""
        from scripts.load_rules_kb import FOOTNOTES_PATH, parse_footnotes

        if not FOOTNOTES_PATH.exists():
            self.skipTest("чанк сносок отсутствует")
        recs = parse_footnotes(FOOTNOTES_PATH)
        by_point = {r["point"]: r for r in recs}
        self.assertIn("<44>", by_point)
        r = by_point["<44>"]
        self.assertEqual(r["doc_type"], "appendix_footnotes")
        self.assertEqual(r["source_anchor"], "Приложение к ПП №719, сноска <44>")
        self.assertIn("акт", r["text"].lower())

    def test_excluded_footnotes_are_dropped(self):
        """«<7> Сноска исключена» отвечать нечем — в корпус не идёт."""
        from scripts.load_rules_kb import FOOTNOTES_PATH, parse_footnotes

        if not FOOTNOTES_PATH.exists():
            self.skipTest("чанк сносок отсутствует")
        for r in parse_footnotes(FOOTNOTES_PATH):
            self.assertNotRegex(r["text"], r"^<\d+>\s*[Сс]носка исключена")

    def test_footnotes_do_not_reserve_a_slot_in_every_answer(self):
        """Сноски — документ «по запросу»: без темы они не занимают место в окне процедурного ответа.

        Иначе на КАЖДОМ процедурном вопросе одно из шести мест уходило бы определению сноски,
        вытесняя норму — ровно тот дефект, который чинила K10 для Приказа №52."""
        from app.rag.retriever import RULES_QUOTA_ON_DEMAND

        self.assertIn("appendix_footnotes", RULES_QUOTA_ON_DEMAND)


if __name__ == "__main__":
    unittest.main()
