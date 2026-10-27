"""K2-3 #174: закупочный порог прим. 5 (раздел II) — графики «ступени (для <вида>)» в абзаце смешанного
примечания.

Отчёт по бою 22.09, случай ④: эксперт дописал к ответу «порог указан в примечании 5». Корень — два:
область прим. 5 определялась по голове («общая»), а форма «с 1 января 2023 г. не менее 3200 баллов, …
(для автомобилей легковых …)» — вид ПОСЛЕ чисел — не разбиралась. У всех четырёх позиций раздела II
закупочного порога не было. Офлайн: Qdrant и ключ не нужны."""
from __future__ import annotations

import json
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

from app.rag import pipeline, thresholds as t
from app.rag.retriever import Hit

ROOT = Path(__file__).resolve().parents[1]
STRUCTURED = ROOT / "knowledge_base" / "pp719" / "structured"
_HIT_FIELDS = {f.name for f in fields(Hit)}


def _records() -> list[dict]:
    out = []
    for f in sorted(STRUCTURED.glob("*.json")):
        out += [r for r in json.loads(f.read_text(encoding="utf-8"))
                if r.get("record_type") != "section_methodology"]
    return out


RECORDS = _records()
SECTION_II = [r for r in RECORDS if r["section_roman"] == "II"]


def _ii(label: str) -> dict:
    """Позиция раздела II по её собственной метке в наименовании."""
    found = [r for r in SECTION_II if label in r["product_name"]]
    assert len(found) == 1, label
    return found[0]


def _proc(r: dict) -> str | None:
    return t.lookup_procurement_threshold(r["okpd2_codes"], r["product_name"], r["section_roman"])


CARS = "(далее - автомобили легковые)"
LCV = "(легкие коммерческие автомобили)"
TRUCKS = "Средства автотранспортные грузовые"
BUSES = "Автобусы и автомобили скорой"


class TestNote5Paragraph(unittest.TestCase):
    def test_seven_kinds_verbatim(self):
        """Семь графиков абзаца — с числами дословно (прим. 5, ред. N 1762 / N 400)."""
        (p,) = t._kind_schedules()
        self.assertEqual((p["note"], p["section"], p["scope"]), ("5", "II", "procurement"))
        got = [(k.split(",")[0].split(" (")[0], [v for _, v in s]) for k, s in p["kinds"]]
        n = lambda *xs: [f"не менее {x} баллов" for x in xs]  # noqa: E731
        self.assertEqual(got, [
            ("автомобилей легковых", n(3200, 3500, 3700)),
            ("автомобилей легковых", n(1100, 1500)),
            ("легких коммерческих автомобилей", n(1100, 1600, 2300)),
            ("автомобилей грузовых", n(3200, 3500, 3700)),
            ("автомобилей грузовых", n(2000, 2300)),
            ("автобусов", n(2900, 3200, 3400)),
            ("автобусов", n(1900, 2300)),
        ])
        # годы ступеней — свои у каждого вида (легковые 2028, грузовые 2029)
        self.assertEqual([lbl for lbl, _ in p["kinds"][0][1]][-1], "с 1 января 2028 г.")
        self.assertEqual([lbl for lbl, _ in p["kinds"][3][1]][-1], "с 1 января 2029 г.")

    def test_kind_text_survives_unclosed_parenthesis(self):
        """«(для автомобилей легковых, … (электромобили), с 1 января …» — скобка «(для» не закрыта."""
        kinds = [k for k, _ in t._kind_schedules()[0]["kinds"]]
        self.assertEqual(kinds[0], "автомобилей легковых, легких коммерческих автомобилей")
        self.assertTrue(kinds[1].endswith("электроэнергии (электромобили)"), kinds[1])
        self.assertTrue(kinds[6].endswith("(электробусы)"), kinds[6])
        self.assertNotIn("Условием", kinds[6])

    def test_only_note_5_has_this_form(self):
        """Замер 27.10.2026: форма есть только у прим. 5. Новое примечание этой формы — повод
        посмотреть его привязку, а не поправить число."""
        self.assertEqual([p["note"] for p in t._kind_schedules()], ["5"])


class TestPositions(unittest.TestCase):
    def test_each_position_gets_its_own_kinds(self):
        want = {
            CARS: ("для автомобилей легковых, легких коммерческих автомобилей: с 1 января 2023 г. — "
                   "не менее 3200 баллов; с 1 января 2027 г. — не менее 3500 баллов; с 1 января 2028 г. "
                   "— не менее 3700 баллов", "(электромобили): с 1 января 2023 г. — не менее 1100 баллов"),
            LCV: ("для автомобилей легковых, легких коммерческих автомобилей:",
                  "(легкие коммерческие электромобили): с 1 января 2023 г. — не менее 1100 баллов"),
            TRUCKS: ("для автомобилей грузовых: с 1 января 2023 г. — не менее 3200 баллов",
                     "электроэнергии: с 1 января 2023 г. — не менее 2000 баллов"),
            BUSES: ("для автобусов: с 1 января 2023 г. — не менее 2900 баллов",
                    "(электробусы): с 1 января 2023 г. — не менее 1900 баллов"),
        }
        for label, (lead, electric) in want.items():
            got = _proc(_ii(label))
            self.assertTrue(got.startswith("по видам продукции [прим. 5 к разд. II]:"), label)
            self.assertIn(lead, got, label)
            self.assertIn(electric, got, label)
            self.assertEqual(got.count("\n      — "), 2, label)   # ровно два вида на позицию

    def test_no_foreign_kind(self):
        """Электро-график ЛЕГКОВЫХ не уходит лёгким коммерческим («легк» ≠ «легков»), грузовой —
        легковым («автомобили скорой помощи» есть во всех четырёх наименованиях)."""
        self.assertNotIn("(электромобили)", _proc(_ii(LCV)))
        self.assertNotIn("грузовых", _proc(_ii(CARS)))
        self.assertNotIn("автобусов", _proc(_ii(TRUCKS)))
        self.assertNotIn("легковых", _proc(_ii(BUSES)))

    def test_general_threshold_untouched(self):
        """Закупочный порог — не порог подтверждения производства: общий у раздела II по-прежнему нет."""
        for r in SECTION_II:
            self.assertIsNone(t.lookup_threshold(r["okpd2_codes"], r["product_name"], "II"))

    def test_radius_only_section_ii(self):
        """Радиус по корпусу: ветка графиков по видам добавляет порог ровно четырём позициям раздела II."""
        with_kinds = [r for r in RECORDS if (_proc(r) or "").startswith("по видам продукции [прим. 5")]
        self.assertEqual(sorted(r["source_anchor"] for r in with_kinds),
                         sorted(r["source_anchor"] for r in SECTION_II))
        self.assertEqual(len(with_kinds), 4)

    def test_section_is_required(self):
        r = _ii(CARS)
        self.assertIsNone(t._kind_schedule(r["product_name"], None, "procurement"))
        self.assertIsNone(t._kind_schedule(r["product_name"], "III", "procurement"))
        self.assertIsNone(t._kind_schedule(r["product_name"], "II", "general"))


class TestMatching(unittest.TestCase):
    def test_stem_hit(self):
        self.assertTrue(t._stem_hit("автомоб", "автомобил"))      # Snowball режет по-разному
        self.assertFalse(t._stem_hit("легк", "легков"))           # лёгкие ≠ легковые
        self.assertTrue(t._stem_hit("грузов", "грузов"))

    def test_kind_applies_by_any_comma_part(self):
        self.assertTrue(t._kind_applies("автомобилей легковых, легких коммерческих автомобилей",
                                        "… (легкие коммерческие автомобили)"))
        self.assertFalse(t._kind_applies("автомобилей грузовых", "Автобусы и автомобили скорой помощи"))


class TestFormGuards(unittest.TestCase):
    """Абзац, где форма не та, не даёт ничего: ступень без вида, один вид, нет раздела."""

    def _parse(self, para: str) -> list[dict]:
        text = f"5. Вводная.\n{para}\n6. Следующее примечание.\n"
        t._kind_schedules.cache_clear()
        try:
            with mock.patch.object(t, "_chunk_text", lambda: text):
                return t._kind_schedules()
        finally:
            t._kind_schedules.cache_clear()

    def test_good_form(self):
        got = self._parse("Для целей осуществления закупок, указанными в разделе II настоящего приложения "
                          "(с 1 января 2023 г. не менее 10 баллов (для тракторов), с 1 января 2023 г. "
                          "не менее 20 баллов (для комбайнов). Условием …")
        self.assertEqual([k for k, _ in got[0]["kinds"]], ["тракторов", "комбайнов"])

    def test_trailing_step_without_kind(self):
        """Два вида есть, но хвост ступеней ни к какому виду не привязан — форма не та, абзац не берём."""
        self.assertEqual(self._parse(
            "Для целей осуществления закупок, указанными в разделе II настоящего приложения (с 1 января "
            "2023 г. не менее 10 баллов (для тракторов), с 1 января 2023 г. не менее 20 баллов (для "
            "комбайнов), с 1 января 2023 г. не менее 30 баллов, с 1 января 2024 г. не менее 40 баллов."), [])

    def test_single_kind_or_no_section(self):
        self.assertEqual(self._parse("Указанными в разделе II настоящего приложения: с 1 января 2023 г. "
                                     "не менее 10 баллов, с 1 января 2024 г. не менее 20 баллов (для тракторов)."), [])
        self.assertEqual(self._parse("Для целей осуществления закупок (с 1 января 2023 г. не менее 10 "
                                     "баллов (для тракторов), с 1 января 2023 г. не менее 20 баллов "
                                     "(для комбайнов)."), [])


class TestContext(unittest.TestCase):
    def test_procurement_line_in_context_and_panel(self):
        r = _ii(CARS)
        h = Hit(**{k: v for k, v in {**r, "score": 1.0, "okpd2_match": True}.items() if k in _HIT_FIELDS})
        ctx = pipeline.format_context([h], "какой порог баллов для закупок легковых автомобилей", None)
        self.assertIn("Порог ДЛЯ ЦЕЛЕЙ ЗАКУПОК (не для подтверждения происхождения): по видам продукции "
                      "[прим. 5 к разд. II]:", ctx)
        for n in ("3200", "3500", "3700"):
            self.assertIn(f"не менее {n} баллов", ctx)
        self.assertNotIn("    Порог: ", ctx)        # общего порога у позиции нет — и не появился
        self.assertIn("Порог для целей закупок (не для подтверждения происхождения): по видам продукции",
                      pipeline.source_text(h))


if __name__ == "__main__":
    unittest.main()
