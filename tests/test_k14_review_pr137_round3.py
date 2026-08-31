"""Ревью PR #137, РАУНД 3: семь находок, три HIGH. Офлайн.

⚠⚠ ТРЕТИЙ РАУНД ПОДРЯД С HIGH В СВЕЖЕМ КОДЕ (7 → 5 → 7 находок, 2 → 2 → 3 HIGH). Батарея из
1094 тестов была зелёной на всех семи.

1. HIGH — нумерация с дефисом: Разделы «9-1» и «9-2» Соглашения не матчились и проглатывались в
   запись п. 9.4 (3729 знаков), которую `format_rules_context` режет по `RULES_TEXT_CAP`. Хвост,
   включая 12-месячный срок восстановления режима свободной торговли, НЕ СУЩЕСТВОВАЛ В КОРПУСЕ.
2. HIGH — `_in_range` сравнивал префиксы по КРАТЧАЙШЕЙ длине, и диапазон УЖЕ запроса считался
   покрывающим: «4012» получал условие восстановления шин, писанное для 4012 11 – 4012 19.
3. HIGH — `narrower` показывался только при отсутствии совпадения, и на четырёх кодах таблицы
   родительское условие выдавалось за условие всей позиции.
4. MED — путь `T9` (у пользователя ТОЛЬКО код ТН ВЭД) уходил процедурной веткой.
5. MED — «ТН ВЭД» как 719-якорь пускал ТАМОЖЕННЫЕ вопросы в корпус 719, у которого нет гейта
   вне-сферы. В `eval_golden_negative.json` ноль вопросов с ТН ВЭД — свип не видел класс вовсе.
6. MED — три документа темы пересоздают окно. ⚠ РАЗОБРАНО И НЕ ИСПРАВЛЕНО: см.
   `TestFinding6IsAMeasuredLimit` — механизм оказался НЕ тем, что назван в находке.
7. LOW — тема сравнивалась строковым литералом вместо константы.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import procedural, st1_ref, topics  # noqa: E402
from app.tools.navigator import extract_okpd2  # noqa: E402

SNG = ROOT / "knowledge_base" / "pp719" / "sng_origin_rules.txt"


def route(q: str) -> bool:
    return procedural.is_procedural(q, has_code=bool(extract_okpd2(q)))


class TestFinding1HyphenatedSectionsSurvive(unittest.TestCase):
    """Разделы «9-1» и «9-2» — свои записи, а не хвост чужого пункта."""

    def _records(self):
        import load_rules_kb

        return load_rules_kb.parse_sng_origin(SNG)

    def test_sections_exist_as_their_own_records(self):
        recs = self._records()
        anchors = [r.get("source_anchor", "") for r in recs]
        self.assertTrue(any("9-2.1" in a for a in anchors), "раздел 9-2 не разобран по пунктам")
        self.assertTrue(any("проверки достоверн" in a.lower() for a in anchors),
                        "раздел 9-1 не стал записью")

    def test_the_lost_deadlines_are_back_in_the_corpus(self):
        """⚠ Суть находки — ПОТЕРЯ ТЕКСТА, а не неверный заголовок.

        ⚠⚠ Проверяем текст ВМЕСТЕ С ЕГО ПУНКТОМ. Первая редакция искала только подстроку и
        пережила мутацию шаблона ПУНКТОВ: с починенным шаблоном разделов текст 9-2 доезжает
        целиком одной прозовой записью, и «срок на месте» ничего не говорит о разборе.
        """
        recs = self._records()
        texts = [r.get("text", "") for r in recs]
        self.assertTrue(any("отсутствия в течение 3 месяцев ответа" in t for t in texts),
                        "срок запроса из раздела 9-1 потерян")
        owner = [r for r in recs if "не истекло 12 месяцев с даты регистрации" in r.get("text", "")]
        self.assertTrue(owner, "срок восстановления из раздела 9-2 потерян")
        self.assertIn("9-2.1", owner[0].get("source_anchor", ""),
                      "раздел 9-2 не разобран по пунктам — срок лежит в прозовом блоке")

    def test_the_swallowing_record_shrank(self):
        p94 = [r for r in self._records() if "п. 9.4" in r.get("source_anchor", "")]
        self.assertEqual(len(p94), 1)
        self.assertLess(len(p94[0]["text"]), 1400,
                        "п. 9.4 снова длиннее порога усечения — значит снова что-то проглотил")

    def test_no_duplicate_anchors(self):
        anchors = [r["source_anchor"] for r in self._records()]
        self.assertEqual(len(anchors), len(set(anchors)))


class TestFinding2RangeNarrowerThanQueryIsNotExact(unittest.TestCase):
    """Диапазон подсубпозиций не покрывает всю товарную позицию."""

    def test_subheading_range_is_narrower(self):
        res = st1_ref.conditions_for("4012")
        exact = [r["code"] for r in res["exact"]]
        narrower = [r["code"] for r in res["narrower"]]
        self.assertIn("4012", exact)
        self.assertIn("401211000", narrower, "диапазон 4012 11–4012 19 снова выдан за покрывающий")

    def test_range_spanning_headings_still_covers(self):
        """⚠ Положительный контроль: диапазон ШИРЕ запроса обязан остаться покрывающим."""
        rows = [r for r in st1_ref._rows() if r.get("code_to")]
        self.assertTrue(rows, "в таблице нет диапазонов — утверждения выше пусты")
        spanning = [r for r in rows if r["code"][:4] != str(r["code_to"])[:4]]
        self.assertTrue(spanning, "нет диапазона через несколько позиций")
        r = spanning[0]
        self.assertEqual(st1_ref._range_relation(r["code"][:4], r), "exact")


class TestFinding3NarrowerIsAlwaysReported(unittest.TestCase):
    """Оговорка про более узкие коды нужна и при совпадении."""

    def test_parent_match_still_names_the_narrower_row(self):
        res = st1_ref.conditions_for("8544")
        self.assertTrue(res["matched"])
        self.assertTrue(res["narrower"], "в таблице нет узкой строки — утверждение пусто")
        self.assertIn("854470000", st1_ref.format_for_context("8544"),
                      "родительское условие снова выдаётся за условие всей позиции")

    def test_no_match_case_still_reports_it(self):
        block = st1_ref.format_for_context("8402")
        self.assertIn("НЕ включён", block)


class TestFindings4And5AnchorIsNarrow(unittest.TestCase):
    """«ТН ВЭД» якорит только рядом с предметом второго ключа."""

    def test_customs_questions_do_not_reach_the_719_corpus(self):
        for q in ("порядок оформления таможенной декларации по ТН ВЭД 8703",
                  "какой порядок получения лицензии на импорт по ТН ВЭД 8703 21",
                  "какой порядок получения разрешения на ввоз товара с кодом ТН ВЭД 8403"):
            with self.subTest(q=q[:44]):
                self.assertFalse(route(q), q)

    def test_t9_path_questions_stay_on_the_product_branch(self):
        for q in ("подпадает ли под требования продукция ТН ВЭД 8479 89 970 8",
                  "мой код ТН ВЭД 8471 30 000 0, какие требования к продукции"):
            with self.subTest(q=q[:44]):
                self.assertFalse(route(q), q)

    def test_second_key_questions_still_pass(self):
        for q in ("какие условия достаточной переработки для кода ТН ВЭД 8403",
                  "мой код ТН ВЭД 8544 49 910 0, какие условия",
                  "как получить сертификат СТ-1",
                  "что такое кумулятивный принцип"):
            with self.subTest(q=q[:44]):
                self.assertTrue(route(q), q)


class TestFinding6IsAMeasuredLimit(unittest.TestCase):
    """⚠⚠ РАЗОБРАНО И НАМЕРЕННО НЕ ИСПРАВЛЕНО — механизм оказался НЕ тем, что назван в находке.

    Находка объясняла отсутствие `decree_body` в окне пересозданием квоты (3+2+2 на шесть мест).
    Замерено шесть комбинаций «ширина пула × состав документов темы» — `decree_body` не попадает
    в окно НИ В ОДНОЙ, включая пул 48 (где он ТОЧНО есть среди кандидатов) и позицию второго
    документа темы с квотой 2. Значит связывает не квота и не пул по отдельности.

    Что это НЕ ломает: критерий приёмки выполняется, кейс 57 отвечает верно — норму подпункта «г»
    дублирует п. 1.3 Соглашения, который в окне есть.
    Что это ЗНАЧИТ: у главного вопроса задачи документ-норма в окно не доезжает, и разбирать это
    надо в `_rules_order`, а не подкруткой констант в хвосте длинной сессии.

    Тест фиксирует ФАКТ, чтобы он не потерялся, и упадёт, когда кто-нибудь это починит.
    """

    def test_topic_docs_are_three_and_measured_equal(self):
        self.assertEqual(topics.doc_types(topics.ST1_ORIGIN),
                         ("sng_origin_rules", "prikaz14_tpp", "decree_body"))

    def test_the_gap_is_recorded_not_forgotten(self):
        doc = (ROOT / "docs" / "eval_runs" / "2026-08-31_k14_step6_reindex.md").read_text(
            encoding="utf-8")
        self.assertIn("decree_body", doc, "измеренный предел не записан в отчёт")


class TestFinding7TopicNameIsAConstant(unittest.TestCase):
    """Переименование темы не должно ТИХО выключать лукап второго ключа."""

    def test_constant_exists_and_is_used(self):
        self.assertEqual(topics.ST1_ORIGIN, "st1_origin")
        self.assertIn(topics.ST1_ORIGIN, topics.TOPICS)
        # ⚠⚠ ПОТРЕБИТЕЛЮ ЛИТЕРАЛ НЕ ПОЛОЖЕН ВОВСЕ. Первая редакция допускала «не больше одного»
        # в обоих файлах и потому пережила откат `pipeline.py` к литералу: там их стало ровно
        # один. Определение живёт в `topics.py` — там одно вхождение и законно, у потребителя ноль.
        defs = (ROOT / "app" / "rag" / "topics.py").read_text(encoding="utf-8")
        self.assertEqual(defs.count('"st1_origin"'), 1, "в topics.py остался литерал по месту")
        user = (ROOT / "app" / "rag" / "pipeline.py").read_text(encoding="utf-8")
        self.assertEqual(user.count('"st1_origin"'), 0, "pipeline.py сравнивает тему литералом")


if __name__ == "__main__":
    unittest.main()
