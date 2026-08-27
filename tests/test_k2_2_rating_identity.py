"""`K2-2` #128: номинал в наименовании — часть ИДЕНТИЧНОСТИ изделия, а не описание.

⚠⚠ ЧТО ОКАЗАЛОСЬ НЕ ТАК В САМОЙ ПОСТАНОВКЕ ЗАДАЧИ. Issue описывала дефект как «ведущий „Порог:“
берётся из ЧУЖОГО блока прим. 27» и предлагала сравнивать диапазоны. Сверка с первоисточником
показала другое устройство: вводная прим. 27 называет ОДНУ позицию —

    классифицируемой кодом ... из 27.12 "Выключатель автоматический (воздушный) низковольтный
    в литом корпусе (до 1000 В) на токи до 6300 А":

— а два подблока («до 4000 А включительно» и «от 4000 А до 6300 А включительно») делят ЕЁ ЖЕ по
фактическому току изделия. То есть для названной позиции ОБА блока верны, и «чужого» блока там нет.

Настоящий дефект крупнее и другого рода: порог прим. 27 получала ВТОРАЯ позиция того же кода —
«Выключатель автоматический низковольтный в литом корпусе (до 1000 В) на токи до 2000 А», которую
примечание НЕ НАЗЫВАЕТ. Строка «2000 А» не встречается во ВСЕХ примечаниях ни разу (проверено
grep по корпусу). Правило `strict_name` пропускало её по пересечению «≥2 значимых слова», а два
наименования, различающиеся только квалификатором «(воздушный)» и номиналом, совпадают по девяти.

Это самый дорогой класс дефекта проекта — правильное число, привязанное не к той позиции.
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
from app.rag.thresholds import _rating_conflict, _ratings, lookup_threshold  # noqa: E402

NAMED_BY_NOTE = ("Выключатель автоматический (воздушный) низковольтный в литом корпусе "
                 "(до 1000 В) на токи до 6300 А")
NOT_NAMED = ("Выключатель автоматический низковольтный в литом корпусе (до 1000 В) "
             "на токи до 2000 А")


class TestRatingIsIdentity(unittest.TestCase):
    def test_position_the_note_does_not_name_gets_no_threshold(self):
        """Несущая половина: чужой порог больше не приписывается."""
        self.assertIsNone(lookup_threshold(["27.12"], NOT_NAMED, "V"),
                          "позиция получает порог примечания, которое её не называет")

    def test_the_named_position_keeps_both_of_its_branches(self):
        """⚠ Обратная половина: сузить до слепоты — тот же дефект с другой стороны.

        У названной позиции подблоки делят ЕЁ ЖЕ по току, и оба обязаны остаться."""
        got = lookup_threshold(["27.12"], NAMED_BY_NOTE, "V")
        self.assertIsNotNone(got, "названная примечанием позиция потеряла порог")
        self.assertIn("147 баллов", got, "ветка «до 4000 А» пропала")
        self.assertIn("167 баллов", got, "ветка «от 4000 до 6300 А» пропала")

    def test_names_without_a_rating_are_untouched(self):
        """Вето срабатывает, только когда единица названа У ОБОИХ наименований."""
        got = lookup_threshold(["27.33"], "Контакторы и пускатели электромагнитные низковольтные",
                               "V")
        self.assertIsNotNone(got, "контакторы задеты вето, хотя номинала в имени нет")
        self.assertIn("41 балл", got)

    def test_units_are_compared_separately(self):
        """⚠⚠ ПО ЕДИНИЦАМ, А НЕ ОБЩЕЙ КУЧЕЙ. «(до 1000 В)» стоит в ОБОИХ наименованиях, и при
        сравнении одним множеством совпадение по вольтам гасило расхождение по амперам — то есть
        вето снималось ровно там, ради чего заводилось (поймано при первой редакции правки)."""
        self.assertEqual(_ratings(NOT_NAMED), {"в": {1000}, "а": {2000}})
        self.assertEqual(_ratings(NAMED_BY_NOTE), {"в": {1000}, "а": {6300}})
        self.assertTrue(_rating_conflict(NOT_NAMED, [NAMED_BY_NOTE]))
        self.assertFalse(_rating_conflict(NAMED_BY_NOTE, [NAMED_BY_NOTE]))

    def test_no_rating_no_veto(self):
        for name in ("Суда рыболовные", "Контакторы и пускатели электромагнитные низковольтные",
                     "Мочеприемники"):
            self.assertFalse(_rating_conflict(name, [NAMED_BY_NOTE]), name)


class TestRadiusIsPinned(unittest.TestCase):
    """⚠⚠ РАДИУС ЗАКРЕПЛЯЕТСЯ СРАВНЕНИЕМ С ОТКЛЮЧЁННЫМ ВЕТО, А НЕ НАБЛЮДЕНИЕМ ЗА ВЫКЛЮЧАТЕЛЯМИ.

    Первая редакция теста фильтровала записи по `"ыключател" in name` и утверждала, что покраснеет,
    если правка начнёт снимать пороги шире. Не покраснела бы: расширение вето на трансформаторы,
    кабели или компрессоры (у всех в наименованиях есть «кВ», «кг», «мм») она не видела вовсе.
    Вдобавок она проверяла ГРАНИЦУ («выключателей без порога не больше четырёх»), а не РАЗНОСТЬ,
    то есть не могла отличить «вето сняло порог» от «его там никогда не было». Профиль
    `v0.5.0-test21.env` ссылается на этот радиус как на гарантию релиза — значит мерить надо
    честно: прогнать корпус ДВАЖДЫ, с вето и без него, и сверить симметрическую разность.

    ⚠ Вето умеет ТОЛЬКО СНИМАТЬ порог. Поэтому расширение радиуса — это всегда потерянные числа
    у эксперта, класс «Мочеприемников», который проект считает дороже исправляемого.
    """

    @staticmethod
    def _sweep():
        """(с вето, без вето) по ВСЕМ записям без собственного порога."""
        with_veto, without = {}, {}
        real = thresholds._rating_conflict
        try:
            for f in glob.glob(str(ROOT / "knowledge_base/pp719/structured/*.json")):
                for rec in json.load(open(f, encoding="utf-8")):
                    if (rec.get("min_threshold") or "").strip():
                        continue
                    name = (rec.get("product_name") or "").strip()
                    if not name:
                        continue
                    codes, sec = rec.get("okpd2_codes") or [], rec.get("section_roman")
                    thresholds._rating_conflict = real
                    with_veto[name] = thresholds.lookup_threshold(codes, name, sec)
                    thresholds._rating_conflict = lambda *a, **k: False
                    without[name] = thresholds.lookup_threshold(codes, name, sec)
        finally:
            thresholds._rating_conflict = real
        return with_veto, without

    def test_the_veto_changes_exactly_one_position(self):
        with_veto, without = self._sweep()
        changed = sorted(n for n in without if with_veto[n] != without[n])
        self.assertEqual(changed, [NOT_NAMED],
                         f"вето задело не только целевую позицию: {changed}")

    def test_the_veto_only_ever_removes(self):
        """⚠ Отрицательный контроль направления: вето не должно ничего ДОБАВЛЯТЬ.

        Если оно что-то добавило — значит сравнение сломано (так уже было: старый модуль,
        положенный вне репозитория, не нашёл корпус и показал «536 появившихся порогов»)."""
        with_veto, without = self._sweep()
        gained = [n for n in without if without[n] is None and with_veto[n] is not None]
        self.assertEqual(gained, [], f"вето ДОБАВИЛО порог — замер сломан: {gained}")

    def test_positive_control_the_sweep_actually_reads_the_corpus(self):
        """«Ноль расхождений» и «я ничего не прочитал» снаружи неразличимы."""
        with_veto, without = self._sweep()
        self.assertGreater(len(without), 900, "корпус не прочитан — замер пуст")
        self.assertTrue(any(v for v in without.values()), "ни у одной позиции нет порога")


if __name__ == "__main__":
    unittest.main()
