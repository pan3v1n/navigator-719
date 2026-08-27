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
    """⚠ Радиус закрепляется ЧИСЛОМ: правка снимает порог, а снятие опаснее добавления.

    Замер 27.08.2026 по всем позициям без собственного порога: потеряно 1, появилось 0,
    изменено 0. Если правка начнёт снимать пороги шире — тест покраснеет здесь, а не на бою.
    """

    def test_exactly_one_position_loses_its_note_threshold(self):
        lost = []
        for f in glob.glob(str(ROOT / "knowledge_base/pp719/structured/*.json")):
            for rec in json.load(open(f, encoding="utf-8")):
                if (rec.get("min_threshold") or "").strip():
                    continue
                name = (rec.get("product_name") or "").strip()
                if not name:
                    continue
                if lookup_threshold(rec.get("okpd2_codes") or [], name,
                                    rec.get("section_roman")) is None and "ыключател" in name:
                    lost.append(name)
        self.assertIn(NOT_NAMED, lost, "целевая позиция снова получает чужой порог")
        # Прочие выключатели без порога были такими и до правки — «модульные до 125 А» и т. п.
        self.assertLessEqual(len(lost), 4, f"порогов снято больше ожидаемого: {lost}")


if __name__ == "__main__":
    unittest.main()
