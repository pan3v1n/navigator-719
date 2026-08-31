"""Ревью PR #137, РАУНД 2: пять находок, две HIGH — и обе в правках раунда 1. Офлайн.

⚠⚠ ПОДТВЕРЖДЁННЫЙ ШАБЛОН ПРОЕКТА: правки по ревью сами становятся объектом ревью. Здесь это
случилось дословно — обе HIGH второго раунда сидят в коде, написанном по находкам первого:

1. HIGH — `_TNVED_POSITION_RE` знал только ДВУЗНАЧНЫЕ группы (`\\d{4}(?:\\s?\\d{2}){0,3}`), а
   реальный код пишется 4+2+3+1: «0710 40 000» усекалось до «0710 40». Цена не «неполный код», а
   НЕВЕРНЫЙ ОТВЕТ: усечённого в Перечне нет, и лукап уверенно печатал «НЕ включён — применяется
   общее правило» про позицию, которую пользователь назвал ПОЛНОСТЬЮ и которая в Перечне ЕСТЬ.
   Кодов длиннее шести знаков в таблице 61 из 244 — четверть. Не поймал ни один сторож: все
   приёмочные кейсы и релизная проверка используют ЧЕТЫРЁХЗНАЧНЫЕ коды, а `test_st1_ref` кормит
   полные коды прямо в `conditions_for`, минуя извлекатель.
2. HIGH — правка раунда 1 («брать ближайшее к маркеру») применена НАПОЛОВИНУ: она помогает лишь
   когда настоящий код в тексте есть. Без него ближайшим оказывается год или сумма, и блок
   уезжает в промпт под указанием «используй ТОЛЬКО это значение». Тест раунда 1 покрывал только
   ДАЛЁКИЙ год.
3. MED — `_MIXED_PRODUCT_RE` ловил ДАТУ (`01.07.2026`) своей копией правила про код ОКПД2, при том
   что `has_okpd2_code` написан ровно против этого и уже импортирован в модуле.
4. MED — `st1_ref._data()` проверял `.exists()`, но не разбираемость: битый файл ронял ВЕСЬ
   процедурный ответ с горячего пути.
5. LOW — заголовок приложения 7 разбит на три строки в верхнем регистре, и 29 записей из 287
   теряли владельца-Положение.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import procedural, st1_ref  # noqa: E402
from app.rag.okpd2_ref import extract_tnved_position  # noqa: E402

TABLE = ROOT / "knowledge_base" / "classifiers" / "tnved_st1_conditions.json"


class TestFinding1FullCodeIsNotTruncated(unittest.TestCase):
    """Код длиннее шести знаков доезжает целиком — иначе ответ про ДРУГУЮ позицию."""

    def test_standard_grouping_survives(self):
        for q, want in (
            ("какие условия по ТН ВЭД 0710 40 000", "0710 40 000"),
            ("условия по ТН ВЭД 8544 70 000 0", "8544 70 000 0"),
            ("условия по ТН ВЭД 071040000", "071040000"),
            ("мой код ТН ВЭД 8544 49 910 0, какие условия", "8544 49 910 0"),
        ):
            with self.subTest(q=q[:40]):
                self.assertEqual(extract_tnved_position(q), want)

    def test_truncation_changed_the_answer_not_just_the_code(self):
        """⚠ Суть находки: усечение давало НЕВЕРНЫЙ вердикт, а не менее точный."""
        full = st1_ref.format_for_context("0710 40 000")
        self.assertIn("включён в Перечень", full)
        self.assertNotIn("НЕ включён", full)
        # Усечённый код — та самая ложная уверенность, ради которой правка и делалась.
        self.assertIn("НЕ включён", st1_ref.format_for_context("0710 40"))

    def test_specific_row_is_not_hidden_behind_the_parent(self):
        """⚠⚠ ЧЕРЕЗ ИЗВЛЕКАТЕЛЬ, А НЕ МИМО НЕГО. Первая редакция звала `conditions_for` с ПОЛНЫМ
        кодом и потому пережила мутацию усечения — я повторил ровно ту ошибку, на которую ревью и
        указало в `test_st1_ref`: «кормит полные коды прямо в `conditions_for`, минуя извлекатель».
        Дефект живёт НА СТЫКЕ, значит и проверять надо стык."""
        code = extract_tnved_position("условия по ТН ВЭД 8544 70 000 0")
        res = st1_ref.conditions_for(code)
        self.assertIn("854470000", [r["code"] for r in res["exact"]],
                      f"специфичная строка потерялась, извлечено {code!r}")

    def test_the_table_really_has_long_codes(self):
        """Положительный контроль: без длинных кодов утверждения выше ничего не проверяют."""
        rows = json.loads(TABLE.read_text(encoding="utf-8"))["rows"]
        long_codes = [r for r in rows if len(str(r.get("code", ""))) > 6]
        self.assertGreater(len(long_codes), 50, "длинных кодов в таблице почти нет")


class TestFinding2YearWithoutACodeIsNotACode(unittest.TestCase):
    """Ближайшее к маркеру число — код, только если это вообще код."""

    def test_year_and_amount_alone_give_nothing(self):
        for q in ("какие условия достаточной переработки по ТН ВЭД в 2026 году",
                  "изменились ли условия ТН ВЭД с 2024 года",
                  "условия по ТН ВЭД, сумма контракта 500000 рублей"):
            with self.subTest(q=q[:44]):
                self.assertIsNone(extract_tnved_position(q), q)

    def test_headings_that_look_like_years_still_work(self):
        """⚠ Диапазоном лет отсекать нельзя: 1902 и 2009 — НАСТОЯЩИЕ позиции ТН ВЭД."""
        self.assertEqual(extract_tnved_position("какие условия по ТН ВЭД 1902"), "1902")
        self.assertEqual(extract_tnved_position("какие условия по ТН ВЭД 2009"), "2009")

    def test_real_code_still_wins_over_a_year(self):
        self.assertEqual(
            extract_tnved_position("в 2026 году какие условия для кода ТН ВЭД 8403"), "8403")


class TestFinding3DateIsNotAnOkpd2Code(unittest.TestCase):
    """Дата в вопросе не должна выключать уступку второго ключа."""

    def test_date_keeps_the_concession(self):
        # ⚠ Первая форма («требования по ТН ВЭД …») ПЕРЕСМОТРЕНА раундом 3: она неотделима от
        # пути `T9` и теперь намеренно ТОВАРНАЯ. Утверждение о ДАТЕ от этого не исчезает — оно
        # проверяется на формах, у которых уступка есть: «условия по ТН ВЭД» и явный СТ-1.
        for q in ("какие условия по ТН ВЭД 8403 в редакции от 01.07.2026",
                  "какие требования к сертификату СТ-1 после 01.09.2026"):
            with self.subTest(q=q[:44]):
                self.assertTrue(procedural.is_procedural(q), q)

    def test_real_okpd2_code_still_cancels_it(self):
        for q in ("сколько баллов нужно для 28.22.16.111, код ТН ВЭД 8428 10",
                  "какие требования к локализации насосов, код ТН ВЭД 8413"):
            with self.subTest(q=q[:44]):
                self.assertFalse(procedural.is_procedural(q), q)


class TestFinding4CorruptTableDegrades(unittest.TestCase):
    """Битая таблица выключает лукап, а не роняет процедурный ответ."""

    def test_broken_json_does_not_raise(self):
        st1_ref._data.cache_clear()
        st1_ref._rows.cache_clear()
        try:
            with unittest.mock.patch.object(pathlib.Path, "read_text",
                                            return_value='{"rows": [oops'):
                self.assertFalse(st1_ref.is_available())
                self.assertEqual(st1_ref.format_for_context("8403"), "")
        finally:
            st1_ref._data.cache_clear()
            st1_ref._rows.cache_clear()
        self.assertTrue(st1_ref.is_available(), "кэш не восстановился — тест отравил соседей")


class TestFinding5EveryRecordKnowsItsPolozhenie(unittest.TestCase):
    """Владелец пункта нужен затем, чтобы «какой у вас сертификат» решалось сравнением форм."""

    def test_no_record_lost_its_owner(self):
        import load_rules_kb

        recs = load_rules_kb.parse_prikaz14(
            ROOT / "knowledge_base" / "pp719" / "prikaz14_tpp_full.txt")
        self.assertEqual(len(recs), 287)
        orphans = [r for r in recs if (r.get("section_title") or "").startswith(" — ")]
        self.assertEqual(orphans, [], f"записей без владельца: {len(orphans)}")

    def test_form_a_is_named_in_the_anchor(self):
        import load_rules_kb

        recs = load_rules_kb.parse_prikaz14(
            ROOT / "knowledge_base" / "pp719" / "prikaz14_tpp_full.txt")
        seven = [r["source_anchor"] for r in recs if "прил. 7" in (r.get("source_anchor") or "")]
        self.assertTrue(seven)
        self.assertTrue(all('формы "A"' in a for a in seven),
                        "приложение 7 снова неотличимо от 4/5/6")


if __name__ == "__main__":
    unittest.main()
