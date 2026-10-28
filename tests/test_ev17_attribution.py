"""`EV17` #109 — атрибуция раздела считается строго, лексика раздела считается отдельно.

⚠⚠ ЧТО БЫЛО. `attributed()` засчитывала атрибуцию, если в ответе есть ЛЮБОЕ слово названия
раздела длиннее 5 букв. Для XXV «Музыкальные инструменты и звуковое оборудование» это в том числе
«оборудование» — слово из каждого второго товарного ответа.

Поймано замером 25.08.2026: 41/42 = 0.98 в одном прогоне из трёх при 1.00 в двух других. Разбор
кейса 43 показал, что римская цифра не печатается НИ РАЗУ за четыре прямых прогона — строгий путь
не срабатывал никогда, и число держалось на совпадении случайного слова.

Тот же класс, что `named_requirements_source` (19.08): величина, зависящая от выбора модели между
двумя правильными формами ответа, — не метрика.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ea():
    # `eval_answers` импортирует соседа `eval_documents`: без `scripts/` в пути модуль в одиночку не
    # грузился (в полной батарее путь добавлял другой модуль — сторож работал только «в компании»).
    import sys
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("ea", ROOT / "scripts" / "eval_answers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Hit:
    def __init__(self, roman, title):
        self.section_roman = roman
        self.section_title = title


HITS = [_Hit("XXV", "Музыкальные инструменты и звуковое оборудование")]


class TestStrictAttribution(unittest.TestCase):
    def test_roman_numeral_counts(self):
        m = _ea()
        for text in ("Позиция относится к разделу XXV приложения.",
                     "Приложение к ПП №719, Раздел XXV, позиция 12.",
                     "…в разделе XXV…"):
            self.assertTrue(m.attributed(text, "XXV", HITS), text)

    def test_full_section_title_counts(self):
        """Название раздела целиком — тоже атрибуция: спутать его не с чем."""
        m = _ea()
        self.assertTrue(m.attributed(
            "Это раздел «Музыкальные инструменты и звуковое оборудование».", "XXV", HITS))

    def test_random_word_of_the_title_does_NOT_count(self):
        """⚠⚠ ЯДРО EV17. «оборудование» встречается в товарных ответах сплошь и рядом."""
        m = _ea()
        for text in ("Для этой продукции нужно специальное оборудование [1].",
                     "Перечислены инструменты и материалы.",
                     "Звуковое сопровождение не относится к делу."):
            self.assertFalse(m.attributed(text, "XXV", HITS),
                             f"случайное слово названия засчитано как атрибуция: {text!r}")

    def test_answer_without_the_section_is_not_attributed(self):
        m = _ea()
        self.assertFalse(m.attributed("Позиция 12, код ОКПД2 26.40.43.110 [1].", "XXV", HITS))


class TestCoherenceIsASeparateNumber(unittest.TestCase):
    """Лексика раздела осталась — но как НАБЛЮДАЕМАЯ величина, без порога."""

    def test_same_texts_are_coherent_but_not_attributed(self):
        """⚠ Отрицательный контроль к предыдущему классу: правка не выбросила старую величину,

        а развела её с атрибуцией. Если бы `topically_coherent` тоже вернула False, «починка»
        свелась бы к удалению сигнала."""
        m = _ea()
        text = "Для этой продукции нужно специальное оборудование [1]."
        self.assertFalse(m.attributed(text, "XXV", HITS))
        self.assertTrue(m.topically_coherent(text, "XXV", HITS))

    def test_unrelated_text_is_neither(self):
        m = _ea()
        text = "Сроки рассмотрения заявления — 10 рабочих дней."
        self.assertFalse(m.attributed(text, "XXV", HITS))
        self.assertFalse(m.topically_coherent(text, "XXV", HITS))

    def test_no_hits_no_crash(self):
        m = _ea()
        self.assertFalse(m.attributed("что угодно", "XXV", []))
        self.assertFalse(m.topically_coherent("что угодно", "XXV", []))


class TestBothReachTheReport(unittest.TestCase):
    """Величина, не попавшая в сводку, не существует — а именно так `EV17` и жила."""

    def test_report_prints_both(self):
        src = (ROOT / "scripts" / "eval_answers.py").read_text(encoding="utf-8")
        self.assertIn("АТРИБУЦИЯ раздела (раздел НАЗВАН)", src)
        self.assertIn("лексика раздела в ответе", src)
        self.assertIn("порога нет — EV17 #109", src,
                      "у наблюдаемой величины обязана стоять пометка «порога нет»")

    def test_coherence_is_computed_per_case(self):
        src = (ROOT / "scripts" / "eval_answers.py").read_text(encoding="utf-8")
        self.assertIn('"coherent": (topically_coherent(', src)


class TestTitleWithFootnoteOrAmendmentEV20(unittest.TestCase):
    """`EV20` #117: у трёх разделов к имени в данных приклеены маркер сноски или редакционная пометка.

    Ответ, назвавший раздел по имени («Продукция радиоэлектроники»), — атрибуция; хвост «<5>» или
    «(в ред. … N 2146)» повторять не обязан. До правки кейс 23 (IX) проваливался 3 из 3 именно так."""

    CASES = [
        ("IX", "Продукция радиоэлектроники <5>", "**Позиция:** «Продукция радиоэлектроники», интегральная схема"),
        ("XVIII", "Продукция судостроения <9>, <9(1)>", "Позиция раздела «Продукция судостроения», шлюпки"),
        ("XXIX", "Оборудование для разведки нефти и газа\xa0(в ред.\xa0Постановления Правительства РФ "
                 "от 25.12.2025 N 2146)", "«Оборудование для разведки нефти и газа», виброисточник"),
    ]

    def test_clean_name_counts(self):
        m = _ea()
        for roman, title, text in self.CASES:
            self.assertTrue(m.attributed(text, roman, [_Hit(roman, title)]), roman)

    def test_copied_tail_still_counts(self):
        """Ответ, повторивший хвост (как модель делала до правки контекста), — тоже атрибуция."""
        m = _ea()
        for roman, title, _text in self.CASES:
            self.assertTrue(m.attributed(f"Раздел «{title.replace(chr(160), ' ')}».", roman,
                                         [_Hit(roman, title)]), roman)

    def test_part_of_the_name_still_does_not_count(self):
        """Отрицательный контроль: нормализация не ослабила строгость — часть имени не атрибуция."""
        m = _ea()
        for roman, title, _text in self.CASES:
            self.assertFalse(m.attributed("Продукция и оборудование для нефти [1].", roman,
                                          [_Hit(roman, title)]), roman)

    def test_every_section_name_shown_to_the_model_is_accepted(self):
        """Контракт: то имя, которое контекст показывает модели (`retriever.section_name`), оракул
        принимает для КАЖДОГО из разделов корпуса. Иначе ответ «как велели» считался бы провалом."""
        import json

        from app.rag.retriever import _clean_section_title, section_name
        m = _ea()
        titles = {}
        for f in sorted((ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json")):
            for r in json.loads(f.read_text(encoding="utf-8")):
                if r.get("section_title"):
                    titles[r["section_roman"]] = _clean_section_title(r["section_title"])
        self.assertGreaterEqual(len(titles), 29)
        for roman, title in titles.items():
            text = f"**Позиция:** «{section_name(title)}», изделие (ОКПД2 01.01) [1]."
            self.assertTrue(m.attributed(text, roman, [_Hit(roman, title)]), f"{roman}: {title!r}")


if __name__ == "__main__":
    unittest.main()
