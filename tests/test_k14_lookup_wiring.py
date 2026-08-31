"""`K14` #35, шаг 6 — детерминированный лукап Перечня подключён к процедурному ответу.

⚠⚠ ЗАЧЕМ ЭТО ОТДЕЛЬНЫЙ КОНТУР, А НЕ ПОИСК. Перечень условий (приложение 1 Соглашения СНГ) в
векторный индекс НЕ ПОШЁЛ намеренно (шаг 3): это список ИСКЛЮЧЕНИЙ, где промах поиска даёт не
пустоту, а ДРУГОЕ правдоподобное условие, снаружи неотличимое от верного. Значит без лукапа
вопрос про условия по ТН ВЭД отвечать НЕЧЕМ — и до 31.08.2026 лукап не был позван НИ ИЗ ОДНОГО
места рантайма: модуль `app/rag/st1_ref.py` существовал с 30.08 и не использовался.

⚠⚠ ВТОРАЯ НАХОДКА ТОГО ЖЕ ДНЯ — ГРАНУЛЯРНОСТЬ КЛЮЧА. `okpd2_ref.extract_tnved` требует не меньше
ШЕСТИ цифр (писался под переходные ключи ТН ВЭД↔ОКПД2, уровень HS6), а Перечень ключуется на
ЧЕТЫРЁХЗНАЧНОЙ товарной позиции. Ни один вопрос приёмочного набора он не разбирал — то есть даже
подключённый лукап не получил бы кода.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.prompts import build_procedural_user_prompt  # noqa: E402
from app.rag import okpd2_ref, st1_ref  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_golden.json"
BLOCK_HEADER = "УСЛОВИЯ ДОСТАТОЧНОЙ ПЕРЕРАБОТКИ"


def st1_cases() -> list[dict]:
    return [c for c in json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
            if c.get("kind") == "st1"]


class TestPositionExtraction(unittest.TestCase):
    """Извлечение ЧЕТЫРЁХЗНАЧНОЙ товарной позиции — своя гранулярность второго ключа."""

    def test_matches_the_acceptance_set_exactly(self):
        for c in st1_cases():
            with self.subTest(id=c["id"]):
                self.assertEqual(okpd2_ref.extract_tnved_position(c["query"]), c["st1_code"],
                                 c["query"])

    def test_old_extractor_cannot_see_four_digit_positions(self):
        """⚠ Именно поэтому заведена вторая функция, а не поправлена первая.

        Утверждение фиксирует ПРИЧИНУ разделения: поправь кто-нибудь `extract_tnved` «заодно» —
        радиус ляжет на `translate.py`, у которого своя приёмка.
        """
        self.assertIsNone(okpd2_ref.extract_tnved("какие условия для кода ТН ВЭД 8403"))
        self.assertEqual(okpd2_ref.extract_tnved_position("какие условия для кода ТН ВЭД 8403"),
                         "8403")

    def test_comma_after_the_code_does_not_reject_it(self):
        # ⚠ Первая редакция хвоста `(?![\d.,])` роняла реальную формулировку набора.
        self.assertEqual(
            okpd2_ref.extract_tnved_position("мой код ТН ВЭД 8501, какие условия"), "8501")

    def test_marker_is_required(self):
        """Четыре цифры сами по себе — не код: иначе блок уезжал бы в чужие вопросы."""
        for q in ("что говорит постановление № 1392", "в 2026 году какие изменения",
                  "приказ ТПП РФ №14 от 01.03.2024", "какие требования к 28.92.21.110"):
            with self.subTest(q=q):
                self.assertIsNone(okpd2_ref.extract_tnved_position(q))


class TestLookupReachesThePrompt(unittest.TestCase):
    """Блок обязан доехать до сообщения модели — иначе лукап считает в пустоту."""

    def test_prompt_carries_the_block(self):
        user = build_procedural_user_prompt("q", "пункты", st1_conditions="БЛОК")
        self.assertIn(BLOCK_HEADER, user)
        self.assertIn("БЛОК", user)

    def test_block_precedes_the_rules(self):
        """Порядок не косметика: закрытый адресный факт читается ДО контекста пунктов."""
        user = build_procedural_user_prompt("q", "ПУНКТЫ_ТУТ", st1_conditions="БЛОК")
        self.assertLess(user.index("БЛОК"), user.index("ПУНКТЫ_ТУТ"))

    def test_absent_block_adds_nothing(self):
        user = build_procedural_user_prompt("q", "пункты")
        self.assertNotIn(BLOCK_HEADER, user)


class TestGateInPlanProcedural(unittest.TestCase):
    """Гейт блока: тема «путь СТ-1» И явный код. Офлайн — окно подменено заглушкой.

    ⚠ Заглушка нужна, чтобы утверждение о ГЕЙТЕ не зависело от живого Qdrant: тест, которому
    понадобится коллекция, перестанет быть офлайновым и упадёт в CI (класс `O3` #104).
    """

    # ⚠ Ключ текста — `text`: так его читает `format_rules_context`. С «content» заглушка
    # молча давала ПУСТОЙ пункт, и утверждение о содержимом окна ничего не проверяло.
    FAKE = [{"doc_type": "sng_origin_rules", "point": "1",
             "text": "текст пункта", "_score": 1.0}]

    def _user(self, q: str) -> str:
        import unittest.mock

        from app.rag import pipeline

        with unittest.mock.patch.object(pipeline, "search_rules", return_value=self.FAKE):
            planned = pipeline.plan_procedural(q, q)
        self.assertIsNotNone(planned, q)
        return planned[3]

    def test_block_appears_for_topic_plus_code(self):
        user = self._user("какие условия достаточной переработки для кода ТН ВЭД 8403")
        self.assertIn(BLOCK_HEADER, user)
        self.assertIn("8403", user)

    def test_no_code_no_block(self):
        # ⚠ `format_for_context(None)` вернул бы «указана группа, а не код» — упрёк за то, чего
        # пользователь не писал. Гейт обязан молчать, а не извиняться.
        user = self._user("как получить сертификат СТ-1")
        self.assertNotIn(BLOCK_HEADER, user)

    def test_other_topic_does_not_get_the_block(self):
        """⚠⚠ Вопрос ОБЯЗАН нести код, иначе тест не изолирует гейт ТЕМЫ.

        Первая редакция брала «сроки рассмотрения заявления о внесении в реестр» — без кода, и
        снятие гейта темы её не роняло: блок всё равно не строился, потому что срабатывал ВТОРОЙ
        гейт. Тест был зелёным и проверял не тот предохранитель. Поймано мутацией.
        """
        q = "сроки рассмотрения заявки по коду ТН ВЭД 8403"
        from app.rag import topics
        from app.rag.okpd2_ref import extract_tnved_position

        self.assertNotEqual(topics.classify(q), "st1_origin", "изолятор перестал изолировать")
        self.assertEqual(extract_tnved_position(q), "8403", "в вопросе обязан быть код")
        self.assertNotIn(BLOCK_HEADER, self._user(q))


class TestBlockIsGrounding(unittest.TestCase):
    """⚠⚠ Числа лукапа обязаны считаться ЗАЗЕМЛЁННЫМИ — иначе гард бьёт по верному ответу.

    Найдено ПЛАТНЫМ прогоном 31.08.2026, а не рассуждением: faithfulness кейсов СТ-1 упала до
    0.62, и «выдуманным» числом объявлялось «50» из условия Перечня. Пока фактами были только
    пункты, `grounding = ctx` совпадало с истиной; детерминированный блок принёс числа, которых
    в пунктах нет ПО ПОСТРОЕНИЮ — Перечень в вектор не индексируется.

    Правило: заземление — это ВСЁ фактическое, что отдано модели, а не только выдача поиска.
    """

    def _grounding(self, q: str) -> str:
        import unittest.mock

        from app.rag import pipeline

        fake = [{"doc_type": "sng_origin_rules", "point": "1",
                 "text": "текст пункта без чисел", "_score": 1.0}]
        with unittest.mock.patch.object(pipeline, "search_rules", return_value=fake):
            planned = pipeline.plan_procedural(q, q)
        self.assertIsNotNone(planned)
        return planned[4]

    def test_condition_numbers_are_grounded(self):
        from app.rag.pipeline import unverified_numbers

        q = "какие условия достаточной переработки для кода ТН ВЭД 8403"
        grounding = self._grounding(q)
        self.assertIn("50", grounding, "величина условия обязана быть в заземлении")
        # Ответ, дословно повторяющий условие, не может считаться выдумкой.
        answer = "Стоимость всех используемых материалов не должна превышать 50% цены продукции."
        self.assertEqual(unverified_numbers(answer, grounding, q), [])

    def test_grounding_still_carries_the_rules(self):
        """Положительный контроль: расширение не подменило пункты блоком."""
        g = self._grounding("какие условия достаточной переработки для кода ТН ВЭД 8403")
        self.assertIn("текст пункта без чисел", g)

    def test_without_a_code_grounding_is_just_the_rules(self):
        g = self._grounding("как получить сертификат СТ-1")
        self.assertIn("текст пункта без чисел", g)
        self.assertNotIn("Перечень условий", g)


class TestBlockContentIsAddressed(unittest.TestCase):
    """Условие обязано быть привязано к СВОЕЙ позиции, а не просто верным по тексту."""

    def test_position_in_the_perechen(self):
        b = st1_ref.format_for_context("8403")
        self.assertIn("8403", b)
        self.assertIn("включён в Перечень", b)

    def test_position_outside_the_perechen_gets_the_general_rule(self):
        # ⚠ Ключевой кейс оси: сосед 8403 в Перечне есть, а 8402 — нет.
        b = st1_ref.format_for_context("8402")
        self.assertIn("НЕ включён", b)
        self.assertIn("общее правило", b)

    def test_range_row_prints_both_ends(self):
        """«из 8702 - 8704»: печать одного `code` показывала бы условие ЧУЖОЙ позиции."""
        b = st1_ref.format_for_context("8704")
        # ⚠ Проверяем ДИАПАЗОН ЦЕЛИКОМ, а не оба конца порознь. Первая редакция искала «8702»
        # и «8704» по отдельности — и проходила при печати одного `code`, потому что в блоке
        # 8704 ТРИ строки: две диапазонные и одна точная, и второй конец приезжал из точной.
        self.assertIn("8702 - 8704", b)
        self.assertIn("ЧАСТИ товаров", b)


if __name__ == "__main__":
    unittest.main()
