"""`K12-1` #122: фрагмент темы обязан покрывать ВЕСЬ её объявленный охват.

⚠⚠ ДЕФЕКТ. Тема `registry_entry` объявлена шире своего имени — «порядок внесения, подача через
ГИСП, реестровая запись, ВЫПИСКА», — а фрагмент промпта предписывал ОДНУ форму: «строй как
последовательность шагов заявителя». Вопрос «как получить ВЫПИСКУ из реестра» получал верный
маршрут и верное окно (Правила, п. 44, раздел V «Предоставление сведений»), а ответ начинался
словами «Внести продукцию в реестр реально — разберём по шагам» и излагал порядок ВНЕСЕНИЯ.
Ретрив нашёл нужное, а промпт увёл в сторону.

⚠⚠ У ДЕФЕКТА ОКАЗАЛОСЬ ДВА ИСТОЧНИКА, И ВТОРОЙ ВАЖНЕЕ. Правка одного фрагмента темы дала лишь
самопоправку: ответ по-прежнему ОТКРЫВАЛСЯ фразой «Внести продукцию в реестр реально…», а затем
оговаривался «но вы спросили про выписку». Причина — ПРИМЕР в правиле стиля базового промпта:
там дословно стояло «(напр. «Внести продукцию в реестр реально — разберём по шагам…»)», и модель
копировала пример, а не следовала теме. Пример, стоящий в промпте, работает как шаблон ответа.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.prompts import PROCEDURAL_SYSTEM_PROMPT  # noqa: E402
from app.rag import topics  # noqa: E402


class TestFragmentCoversTheDeclaredScope(unittest.TestCase):
    def test_registry_entry_names_its_subcases(self):
        frag = topics.TOPIC_FRAGMENTS["registry_entry"] if hasattr(topics, "TOPIC_FRAGMENTS") \
            else _fragment("registry_entry")
        low = frag.lower()
        for word in ("внесен", "выписк", "изменени"):
            self.assertIn(word, low, f"фрагмент темы не называет подслучай «{word}»")
        self.assertIn("отвечай на заданный", low,
                      "фрагмент не требует отвечать на ЗАДАННЫЙ вопрос темы")

    def test_fragment_forbids_defaulting_to_entry(self):
        """Несущее утверждение: запрет подменять порядок получения сведений порядком внесения."""
        frag = _fragment("registry_entry").lower()
        self.assertIn("не подменяй", frag)
        # ⚠ Длина фрагмента — ЦЕНА, а не бесплатная точность: развёрнутая редакция (747 знаков
        # против ~300) перетянула внимание с правила 3б, и документная популяция просела —
        # кейс #50 стал утверждать несуществующий документ 2 прогона из 3. Держим короткой.
        self.assertLess(len(frag), 560, "фрагмент темы разросся — проверить документную популяцию")


class TestStyleExampleIsNotATemplate(unittest.TestCase):
    """⚠⚠ Пример в промпте работает как ШАБЛОН. Правило стиля обязано это оговаривать."""

    def test_opening_phrase_must_match_the_question(self):
        p = PROCEDURAL_SYSTEM_PROMPT
        self.assertIn("ПРО ТО, О ЧЁМ СПРОСИЛИ", p,
                      "правило стиля снова не связывает вводную фразу с вопросом")
        self.assertIn("образец ТОНА, а не текст ответа", p,
                      "пример снова подаётся как готовая фраза")

    def test_more_than_one_opening_is_offered(self):
        """Один пример = один шаблон. Их должно быть несколько, иначе модель копирует
        единственный — ровно это и происходило с «Внести продукцию в реестр реально…»."""
        p = PROCEDURAL_SYSTEM_PROMPT
        i = p.find("4. СТИЛЬ")
        head = p[i:i + 420]
        # ⚠ Считаем МАРКЕР примера, а не кавычки: `count("«")` засчитывал «воды» из соседней
        # фразы, то есть требовал на деле два примера при сообщении про три, а переформулировка
        # несвязанного оборота уронила бы тест на верном коде (ревью PR #133).
        self.assertGreaterEqual(head.count("— «"), 2,
                                "в правиле стиля меньше двух образцов вводной фразы")


def _fragment(topic: str) -> str:
    """Фрагмент темы там, где его держит модуль (имя контейнера менялось)."""
    for attr in ("TOPIC_FRAGMENTS", "_FRAGMENTS", "FRAGMENTS", "TOPIC_PROMPT", "_TOPIC_PROMPT"):
        d = getattr(topics, attr, None)
        if isinstance(d, dict) and topic in d:
            return d[topic]
    raise AssertionError("фрагменты тем не найдены — тест устарел вместе с модулем")


if __name__ == "__main__":
    unittest.main()
