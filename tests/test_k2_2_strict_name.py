"""`K2-2` #128: у строк `strict_name` наименование и есть КЛЮЧ.

⚠⚠ ЧТО ОКАЗАЛОСЬ НЕ ТАК В САМОЙ ПОСТАНОВКЕ. Issue описывала дефект как «ведущий „Порог:“ берётся
из ЧУЖОГО блока прим. 27» и предлагала сравнивать диапазоны. Сверка с первоисточником показала
другое: вводная называет ОДНУ позицию, а два подблока делят ЕЁ ЖЕ по фактическому току — «чужого»
блока там нет. Настоящий дефект крупнее: порог получала ВТОРАЯ позиция того же кода, которую
примечание не называет.

⚠⚠ ПОЧЕМУ НЕ ПО НОМИНАЛУ, ХОТЯ ИМЕННО ОН БРОСАЕТСЯ В ГЛАЗА. Первая редакция сравнивала числа, и
ДВА раунда ревью показали, что путь не сходится: «6 кВ И ВЫШЕ» читалось точным значением и роняло
порог у «110 кВ»; «110 - 500 кВ» теряло нижнюю границу; «массой свыше 0,25 кг» давало 25 кг;
предлог «в» и союз «а» становились вольтами и амперами. Каждая ошибка разбора — СНЯТЫЙ порог,
класс «Мочеприемников», который проект считает дороже исправляемого.

⚠ И главное: настоящий различитель двух позиций 27.12 — слово «(ВОЗДУШНЫЙ)», а не ампераж.
Номинал давал верный ответ по неверной причине. Радиус обоих правил измерен и СОВПАЛ (ровно одна
позиция), поэтому выбрано правило без разбора чисел.
"""

from __future__ import annotations

import glob
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag import thresholds  # noqa: E402
from app.rag.thresholds import (  # noqa: E402
    _name_overlap,
    _names_this_position,
    _norm,
    _strip_fn,
    lookup_threshold,
)


def _old_rule(product_name, names) -> bool:
    """Правило до `K2-2`: пересечение «≥2 значимых слова» ИЛИ дословное совпадение."""
    return bool(_name_overlap(product_name, names) >= 2
                or any(_norm(_strip_fn(nm)) == _norm(_strip_fn(product_name)) for nm in names))

NAMED = ("Выключатель автоматический (воздушный) низковольтный в литом корпусе (до 1000 В) "
         "на токи до 6300 А")
NOT_NAMED = ("Выключатель автоматический низковольтный в литом корпусе (до 1000 В) "
             "на токи до 2000 А")


class TestNameIsTheKey(unittest.TestCase):
    def test_position_the_note_does_not_name_gets_no_threshold(self):
        self.assertIsNone(lookup_threshold(["27.12"], NOT_NAMED, "V"),
                          "позиция получает порог примечания, которое её не называет")

    def test_the_named_position_keeps_both_of_its_branches(self):
        """⚠ Обратная половина: сузить до слепоты — тот же дефект с другой стороны."""
        got = lookup_threshold(["27.12"], NAMED, "V")
        self.assertIsNotNone(got, "названная примечанием позиция потеряла порог")
        self.assertIn("147 баллов", got, "ветка «до 4000 А» пропала")
        self.assertIn("167 баллов", got, "ветка «от 4000 до 6300 А» пропала")

    def test_group_rows_are_untouched(self):
        """Правило локальное: обычные строки (не `strict_name`) работают как прежде."""
        got = lookup_threshold(["27.33"], "Контакторы и пускатели электромагнитные низковольтные",
                               "V")
        self.assertIsNotNone(got, "правило задело строку без strict_name")
        self.assertIn("41 балл", got)


class TestOpenBoundNamesAClass(unittest.TestCase):
    """⚠⚠ ИМЯ С ОТКРЫТОЙ ГРАНИЦЕЙ ОПИСЫВАЕТ КЛАСС, А НЕ ИЗДЕЛИЕ (ревью PR #133, раунд 2).

    Четыре из девяти строк `strict_name` называют себя так: «напряжением 6 кВ И ВЫШЕ», «35 кВ и
    выше», «110 кВ и выше». Требовать от них дословности значило бы ронять порог у любой позиции
    внутри класса: 110 ≥ 6, порог написан ровно для неё. Первая редакция правки именно это и
    делала — поймано ревью.
    """

    def test_a_value_inside_the_class_is_covered(self):
        for codes, name, expect in (
            (["27.12.10.110"], "Выключатели силовые высоковольтные напряжением 110 кВ", "82 балла"),
            (["27.11.4"], "Измерительные трансформаторы напряжением 10 кВ", "не менее 72"),
        ):
            got = lookup_threshold(codes, name, "V")
            self.assertIsNotNone(got, f"позиция внутри класса «… и выше» потеряла порог: {name!r}")
            self.assertIn(expect, got)

    def test_the_corpus_position_itself_still_matches(self):
        got = lookup_threshold(["27.12.10.110"],
                               "Выключатели силовые высоковольтные напряжением 6 кВ и выше", "V")
        self.assertIsNotNone(got)

    def test_specific_name_still_requires_verbatim(self):
        """Имя БЕЗ открытой границы называет одно изделие — пересечения слов мало."""
        self.assertTrue(_names_this_position(NAMED, [NAMED]))
        self.assertFalse(_names_this_position(NOT_NAMED, [NAMED]),
                         "имя без открытой границы принимает чужую позицию по пересечению слов")


class TestRadiusIsPinned(unittest.TestCase):
    """⚠⚠ РАДИУС ЗАКРЕПЛЯЕТСЯ СРАВНЕНИЕМ, А НЕ НАБЛЮДЕНИЕМ ЗА ВЫКЛЮЧАТЕЛЯМИ.

    Первая редакция теста фильтровала записи по «ыключател» в имени и проверяла ГРАНИЦУ, а не
    РАЗНОСТЬ: расширение правила на трансформаторы или кабели оставило бы её зелёной, а профиль
    релиза ссылается на этот радиус как на гарантию. Прогоняем корпус ДВАЖДЫ — с правилом и с
    отключённым — и сверяем разность.
    """

    @staticmethod
    def _sweep():
        # ⚠⚠ БАЗА — ПРЕЖНЕЕ ПРАВИЛО, А НЕ «ВСЕГДА ДА». Заглушка `lambda: True` не выключает
        # правило, а делает его шире прежнего кода: тогда разность считает не радиус правки,
        # а разницу с несуществовавшим поведением. Прежнее правило — пересечение «≥2 значимых
        # слова» ИЛИ дословное совпадение (`thresholds.py` до `K2-2`).
        real = thresholds._names_this_position
        strict, off = {}, {}
        try:
            for f in glob.glob(str(ROOT / "knowledge_base/pp719/structured/*.json")):
                for rec in json.load(open(f, encoding="utf-8")):
                    if (rec.get("min_threshold") or "").strip():
                        continue
                    name = (rec.get("product_name") or "").strip()
                    if not name:
                        continue
                    codes, sec = rec.get("okpd2_codes") or [], rec.get("section_roman")
                    thresholds._names_this_position = real
                    strict[name] = thresholds.lookup_threshold(codes, name, sec)
                    thresholds._names_this_position = _old_rule
                    off[name] = thresholds.lookup_threshold(codes, name, sec)
        finally:
            thresholds._names_this_position = real
        return strict, off

    def test_exactly_one_position_changes(self):
        strict, off = self._sweep()
        changed = sorted(n for n in off if strict[n] != off[n])
        self.assertEqual(changed, [NOT_NAMED], f"правило задело не только целевую позицию: {changed}")

    def test_the_rule_only_ever_removes_here(self):
        """⚠ Контроль направления. Правило `continue` убирает кандидата, и в общем случае это
        МОЖЕТ повысить другого до единственного победителя (найдено ревью PR #133) — то есть
        привязать чужое число, а не просто снять порог. На текущем корпусе такого нет, и тест
        покраснеет, если появится."""
        strict, off = self._sweep()
        gained = [n for n in off if off[n] is None and strict[n] is not None]
        swapped = [n for n in off if off[n] and strict[n] and off[n] != strict[n]]
        self.assertEqual(gained, [], f"правило ДОБАВИЛО порог: {gained}")
        self.assertEqual(swapped, [], f"правило ПОДМЕНИЛО порог другим: {swapped}")

    def test_positive_control_the_sweep_reads_the_corpus(self):
        strict, off = self._sweep()
        self.assertGreater(len(off), 900, "корпус не прочитан — замер пуст")
        self.assertTrue(any(v for v in off.values()), "ни у одной позиции нет порога")


if __name__ == "__main__":
    unittest.main()
