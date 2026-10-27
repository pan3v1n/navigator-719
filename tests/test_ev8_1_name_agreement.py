"""EV8-1 #178: точный код, под которым лежат РАЗНЫЕ изделия, — целевые по согласию с наименованием в вопросе.

Целевыми были все позиции окна с этим кодом (до 8) с полными требованиями — класс утечки чисел чужой
продукции, ради которого делалась `EV7`. Теперь, если вопрос ЯВНО называет одно изделие, целевым остаётся
оно; прочие идут кандидатами. Вопрос из одного кода, вопрос, назвавший саму группировку кода, и ничья —
как прежде. Офлайн."""
from __future__ import annotations

import unittest

from app.rag import pipeline
from app.rag.retriever import Hit


def _h(name: str, code: str = "26.51.12.110", anchor: str | None = None) -> Hit:
    return Hit(score=1.0, section_roman="XVIII", section_title="т", product_name=name, okpd2_codes=[code],
               min_threshold=None, requirement_blocks=[], source_anchor=anchor or name, okpd2_match=True)


ECHO = [_h("Эхолот; рыбопоисковый эхолот; однолучевой гидрографический эхолот"),
        _h("Теодолиты и нивелиры электронные"),
        _h("Пингер; гидроакустический маяк-ответчик")]


class TestNarrowByName(unittest.TestCase):
    def test_named_product_becomes_the_only_target(self):
        got = pipeline.target_hits(ECHO, "26.51.12.110", "требования к рыбопоисковому эхолоту 26.51.12.110")
        self.assertEqual([h.product_name[:6] for h in got], ["Эхолот"])

    def test_code_only_question_keeps_all(self):
        got = pipeline.target_hits(ECHO, "26.51.12.110", "требования по коду 26.51.12.110")
        self.assertEqual(len(got), 3)

    def test_no_query_keeps_all(self):
        self.assertEqual(len(pipeline.target_hits(ECHO, "26.51.12.110")), 3)

    def test_weak_agreement_keeps_all(self):
        """Одно общее слово («электронные») ничего не решает — запас `EV8_MARGIN`."""
        got = pipeline.target_hits(ECHO, "26.51.12.110", "приборы электронные 26.51.12.110")
        self.assertEqual(len(got), 3)

    def test_grouping_name_question_keeps_all(self):
        """Вопрос, назвавший группировку кода по справочнику, — о группировке целиком."""
        from app.rag import okpd2_ref
        name = okpd2_ref.okpd2_name("28.93.17.112")
        hits = [_h("Машины для нарезания хлеба", "28.93.17.112"), _h("Машины кухонные овощерезательные", "28.93.17.112"),
                _h("Слайсеры", "28.93.17.112")]
        got = pipeline.target_hits(hits, "28.93.17.112", f"{name} 28.93.17.112")
        self.assertEqual(len(got), 3)

    def test_grouping_name_guard_beats_strong_agreement(self):
        """Имя группировки совпадает с одной позицией сильно (запас бы пропустил) — вопрос всё равно о
        группировке целиком."""
        from unittest import mock

        from app.rag import okpd2_ref
        names = {"26.51.12": "Эхолоты рыбопоисковые однолучевые гидрографические"}
        with mock.patch.object(okpd2_ref, "_okpd2_names", lambda: names):
            got = pipeline.target_hits(ECHO, "26.51.12.110",
                                       "Эхолоты рыбопоисковые однолучевые гидрографические 26.51.12.110")
        self.assertEqual(len(got), 3)

    def test_two_positions_are_not_narrowed(self):
        """Две записи одного кода (26.11.22.210 «в части белого …» / «за исключением белого …») — `EV8`."""
        two = ECHO[:2]
        self.assertEqual(len(pipeline.target_hits(two, "26.51.12.110", "эхолот рыбопоисковый 26.51.12.110")), 2)

    def test_dropped_positions_stay_candidates_in_context(self):
        ctx = pipeline.format_context(ECHO, "требования к рыбопоисковому эхолоту 26.51.12.110", "26.51.12.110")
        self.assertIn("Теодолиты", ctx)                       # кандидат остался, имя и код
        self.assertIn("СОВПАДЕНИЕ ПО КОДУ ОКПД2 (наиболее вероятная позиция)", ctx)
        self.assertEqual(ctx.count("СОВПАДЕНИЕ ПО КОДУ ОКПД2 (наиболее вероятная позиция)"), 1)


if __name__ == "__main__":
    unittest.main()
