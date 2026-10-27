"""EV8-1 #178: точный код со многими позициями — целевыми только те, о ком говорит вопрос.

Под одним кодом в приложении лежат разные изделия (у 32.50.13.190 — 37), и все совпавшие с кодом были целевыми с
полными требованиями. Сужение (`pipeline._narrow_by_name`) оставляет названные в вопросе позиции, их подвиды и
позиции с необъяснёнными словами вопроса; ничья, синоним и имя группировки — без изменений. Первая версия
(отклонена ревью 27.10) выбирала ОДНУ позицию по числу общих слов и теряла названную на оговорках, синонимах и
парах — эти случаи закреплены здесь. Полный набор проб — `scripts/eval_ev8_probes.py` (сторож ниже). Офлайн."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path

from app.rag import okpd2_ref, pipeline as P
from app.rag.retriever import Hit

ROOT = Path(__file__).resolve().parents[1]
RECORDS = [r for f in sorted((ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json"))
           for r in json.loads(f.read_text(encoding="utf-8")) if r.get("record_type") != "section_methodology"]


def _hit(r: dict) -> Hit:
    return Hit(score=1.0, section_roman=r["section_roman"], section_title="", product_name=r["product_name"],
               okpd2_codes=list(r["okpd2_codes"]), min_threshold=None, requirement_blocks=[],
               source_anchor=r["source_anchor"], okpd2_match=True, payload={})


def _group(code: str, first: tuple[str, ...] = ()) -> list[Hit]:
    """Позиции кода — окно до 8, названные `first` впереди."""
    k = okpd2_ref.okpd2_key(code)
    g = [r for r in RECORDS if any(okpd2_ref.okpd2_key(c) == k for c in r["okpd2_codes"])]
    g.sort(key=lambda r: not r["product_name"].startswith(first) if first else False)
    return [_hit(r) for r in g[:8]]


def _names(hits: list[Hit]) -> list[str]:
    return [h.product_name for h in hits]


class TestNarrowing(unittest.TestCase):
    def test_named_position_alone(self):
        hits = _group("28.30.86.110")
        got = _names(P.target_hits(hits, "28.30.86.110", "Раздатчики кормов для КРС 28.30.86.110 — сколько баллов?"))
        self.assertEqual(got, ["Раздатчики кормов"])

    def test_synonym_changes_nothing(self):
        """Ревью 27.10, HIGH-2: «кормораздатчики» ≠ «раздатчики кормов», «для крупного рогатого скота» — общее
        определение, а не имя «Автоматических чесалок». Никто не назван — целевые прежние."""
        hits = _group("28.30.86.110")
        for q in ("Кормораздатчики для крупного рогатого скота 28.30.86.110 — какие требования?",
                  "Автопоилки для крупного рогатого скота 28.30.86.110 требования"):
            self.assertEqual(len(P.target_hits(hits, "28.30.86.110", q)), len(P.target_hits(hits, "28.30.86.110")), q)

    def test_excluding_position_goes(self):
        """Ревью 27.10, HIGH-1: позиция, ИСКЛЮЧАЮЩАЯ продукт, целевой не остаётся."""
        hits = _group("32.50.13.190", ("Вакуумные одноразовые пробирки", "Контейнеры для биопроб"))
        got = _names(P.target_hits(hits, "32.50.13.190",
                                   "Вакуумные пробирки для взятия крови, одноразовые стерильные, 32.50.13.190"))
        self.assertIn("Вакуумные одноразовые пробирки", got)
        self.assertFalse(any(n.startswith("Контейнеры для биопроб") for n in got))

    def test_head_inside_own_exclusion(self):
        """Голова позиции целиком в вопросе, но вопрос — о том, что она исключает: «…; стенты (за исключением
        стентов для коронарных артерий)» на вопрос о коронарных стентах."""
        q = P._ev8_stems("стентов для коронарных артерий")
        name = ("Заплаты сердечно-сосудистые; стент-графты эндоваскулярные; кава-фильтры; "
                "стенты (за исключением стентов для коронарных артерий)")
        self.assertTrue(P._ev8_excludes_query(name, P._ev8_heads(name), q))

    def test_head_named_apart_from_exclusion_stays(self):
        """Голова названа ОТДЕЛЬНО от оговорки — позиция остаётся предметом вопроса."""
        name = "Клеточное оборудование для содержания птицы (кроме кур-несушек родительского стада)"
        q = P._ev8_stems("Клеточное оборудование для содержания птицы и для кур-несушек родительского стада")
        self.assertFalse(P._ev8_excludes_query(name, P._ev8_heads(name), q))

    def test_pair_keeps_both(self):
        """Ревью 27.10, MED-3: вопрос о двух изделиях одного кода."""
        hits = _group("28.30.86.110")
        got = _names(P.target_hits(hits, "28.30.86.110", "Обмотчики рулонов и Кормушки животноводческие 28.30.86.110"))
        self.assertEqual(sorted(got), ["Кормушки животноводческие", "Обмотчики рулонов"])

    def test_subtype_of_named_stays(self):
        hits = [_hit({"section_roman": "I", "product_name": n, "okpd2_codes": ["28.13.14.110"], "source_anchor": n})
                for n in ("Насосы", "Насосы центробежные погружные", "Компрессоры поршневые", "Вентиляторы")]
        self.assertEqual(_names(P.target_hits(hits, "28.13.14.110", "требования к насосам 28.13.14.110")),
                         ["Насосы", "Насосы центробежные погружные"])

    def test_group_name_changes_nothing(self):
        code = "28.30.86.110"
        official = okpd2_ref._okpd2_names().get(okpd2_ref.okpd2_key(code)) or okpd2_ref._okpd2_names()[code]
        hits = _group(code)
        self.assertEqual(len(P.target_hits(hits, code, f"{official} {code}")), len(P.target_hits(hits, code)))

    def test_no_query_or_few_positions(self):
        hits = _group("28.30.86.110")
        self.assertEqual(P.target_hits(hits, "28.30.86.110", None), P.target_hits(hits, "28.30.86.110"))
        two = hits[:2]
        self.assertEqual(P.target_hits(two, "28.30.86.110", "Раздатчики кормов 28.30.86.110"),
                         P.target_hits(two, "28.30.86.110"))

    def test_group_code_is_not_narrowed_here(self):
        """Сужение — только на ярусе ТОЧНОГО кода: групповой код и так даёт одну позицию по рангу."""
        hits = _group("28.30.86.110")
        self.assertEqual(P.target_hits(hits, "28.30", "Раздатчики кормов 28.30"), P.target_hits(hits, "28.30"))


class TestParts(unittest.TestCase):
    def test_heads_drop_exclusions_and_parens(self):
        heads = P._ev8_heads("Контейнеры для биопроб полимерные; бутыли (стеклянные) (за исключением вакуумных пробирок)")
        self.assertEqual(heads[0], {"контейнер", "биопроб", "полимерн"})
        self.assertNotIn("вакуумн", set().union(*heads))

    def test_comma_does_not_split_a_head(self):
        self.assertEqual(len(P._ev8_heads("Маммографы, рентгеновские аппараты передвижные")), 1)

    def test_fleeting_vowel(self):
        self.assertEqual(P._ev8_stems("пробирок"), P._ev8_stems("пробирки"))
        self.assertEqual(P._ev8_stems("станок"), P._ev8_stems("станка"))

    def test_stop_words(self):
        self.assertEqual(P._ev8_stems("Какие требования для по на 28.13.14 баллы"), set())

    def test_one_word_exclusion_does_not_count(self):
        items = P._ev8_excluded("Мебель (за исключением медицинских изделий, содержащих электронику)")
        self.assertNotIn({"медицинск"}, items)
        self.assertTrue(all(len(it) >= 2 for it in items))


class TestPlumbing(unittest.TestCase):
    def test_answer_and_plan_carry_target_query(self):
        self.assertIn("target_query", P.Answer.__dataclass_fields__)
        self.assertIn("target_query", P._Plan.__dataclass_fields__)
        from app.tools.navigator import Navigation
        self.assertIn("target_query", Navigation.__dataclass_fields__)

    def test_cut_position_of_same_code_is_not_called_group(self):
        """Ревью 27.10, LOW-5: отсечённая позиция того же кода — не «совпадение по ГРУППЕ»."""
        hits = _group("28.30.86.110")
        ctx = P.format_context(hits, "Раздатчики кормов для КРС 28.30.86.110", "28.30.86.110")
        self.assertIn("тот же код ОКПД2, но наименование не совпадает с вопросом", ctx)
        self.assertNotIn("совпадение по ГРУППЕ", ctx)


class TestProbeSet(unittest.TestCase):
    def test_probe_set_has_no_violations(self):
        """Сторож: полный набор проб (≈ 5 300 вопросов из данных) — 0 нарушений."""
        from scripts import eval_ev8_probes
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = eval_ev8_probes.run(show=5)
        self.assertEqual(code, 0, out.getvalue()[-2000:])


if __name__ == "__main__":
    unittest.main()
