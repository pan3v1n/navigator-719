"""Находки ревью захода 3 (PR #103), 26.08.2026. Офлайн, без Qdrant и LLM.

Заход 3 — единственный пакет на бою, не проходивший ревью, и единственный с правками в РАЗБОРЕ
ПОРОГОВ, то есть в числах, которые уходят эксперту. Ревью подтвердило сам заход (перепроверка
парсера по корпусу: +28 порогов, 0 потеряно, 38 изменено — 35 из них уточнение ссылки на
подпункт) и нашло дефекты в краях.

⚠ Каждая находка проверена ВОСПРОИЗВЕДЕНИЕМ на текущем коде до правки: ревью показывает код,
каким он был на момент PR, а `thresholds.py` с тех пор менялся.
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

from app.rag import thresholds as th  # noqa: E402

КОНТАКТОРЫ = "Контакторы и пускатели электромагнитные низковольтные"
ВАКУУМНЫЕ = "Контакторы вакуумные низковольтные переменного тока"


class TestQualifierBlockLeadsByName(unittest.TestCase):
    """⚠⚠ ЧУЖОЙ ГРАФИК СТАНОВИЛСЯ ГЛАВНЫМ «Порог:» — самый дорогой класс дефекта проекта.

    `_qualified_list_rows` жёстко брал `blocks[0]` ведущим, и какой график поедет первым, не
    зависело от того, о какой продукции спросили: обе позиции кода 27.33 возвращали ПОБАЙТНО одну
    строку. «Контакторы и пускатели» получали график ВАКУУМНЫХ контакторов со ступенью 51 балл
    с 01.09.2025, к ним не относящейся, а собственные 41 балл уезжали в сноску «⚠ ИНОЙ порог».

    Чинить это в разборе нельзя ПО ПОСТРОЕНИЮ: на этапе парсинга неизвестно, кто спросит."""

    def test_each_position_leads_with_its_own_block(self):
        got_k = th.lookup_threshold(["27.33"], КОНТАКТОРЫ, "V") or ""
        got_v = th.lookup_threshold(["27.33"], ВАКУУМНЫЕ, "V") or ""
        self.assertTrue(got_k.startswith("для контакторов и пускателей"),
                        f"ведёт чужой график: {got_k[:90]}")
        self.assertTrue(got_v.startswith("для вакуумных контакторов"),
                        f"ведёт чужой график: {got_v[:90]}")

    def test_the_two_positions_no_longer_return_the_same_string(self):
        """Признак самого дефекта: раньше ответ не зависел от вопроса."""
        self.assertNotEqual(th.lookup_threshold(["27.33"], КОНТАКТОРЫ, "V"),
                            th.lookup_threshold(["27.33"], ВАКУУМНЫЕ, "V"))

    def test_the_other_schedule_survives_as_an_exception(self):
        """Второй график не выброшен — он уходит в «⚠ ИНОЙ порог» со СВОИМ условием."""
        got = th.lookup_threshold(["27.33"], КОНТАКТОРЫ, "V") or ""
        self.assertIn("⚠ ИНОЙ порог", got)
        self.assertIn("вакуумных контакторов", got)
        self.assertIn("51 балл", got, "ступень чужого графика потеряна вовсе")

    def test_without_the_name_the_defect_reproduces(self):
        """⚠ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ: без имени позиции выбирать не из чего, и порядок остаётся
        исходным — тот самый, что и был дефектом. Иначе тест зеленел бы и при откате правки."""
        orig = th._fmt_flat
        try:
            th._fmt_flat = lambda r, group=False, product_name="": orig(r, group=group)
            got = th.lookup_threshold(["27.33"], КОНТАКТОРЫ, "V") or ""
        finally:
            th._fmt_flat = orig
        self.assertTrue(got.startswith("для вакуумных контакторов"),
                        "дефект не воспроизводится — тест мерит не его")


class TestNumbersAreNotIdentity(unittest.TestCase):
    """⚠⚠ ДЕФЕКТ В ПЕРВОЙ РЕДАКЦИИ ЭТОЙ ЖЕ ПРАВКИ, найденный ЗАМЕРОМ РАДИУСА, а не рассуждением.

    У позиции «Выключатель … на токи ДО 6300 А» два подблока: «на токи до 4000 А» и «на токи
    ОТ 4000 А ДО 6300 А». Позиция накрывает ОБА диапазона, а совпадение токена «6300» вытаскивало
    вперёд второй — то есть УЖЕ, чем позиция. Число в квалификаторе означает границу диапазона,
    а не тождество продукции."""

    NAME = ("Выключатель автоматический (воздушный) низковольтный в литом корпусе "
            "(до 1000 В) на токи до 6300 А")

    def test_digits_are_dropped_from_the_match(self):
        self.assertNotIn("6300", th._stems("на токи до 6300 А"))
        self.assertTrue(th._stems("контакторы вакуумные"), "слова обязаны остаться")

    def test_word_identical_qualifiers_keep_the_source_order(self):
        """Ничья → порядок первоисточника. Выдуманный выбор хуже сохранённого."""
        got = th.lookup_threshold(["27.12"], self.NAME, "V") or ""
        self.assertTrue(got.startswith("для выключателей автоматических"), got[:80])
        self.assertIn("до 4000 А включительно", got.split("⚠ ИНОЙ порог")[0],
                      "ведущим стал более УЗКИЙ диапазон, чем сама позиция")
        self.assertIn("⚠ ИНОЙ порог", got, "второй график потерян")


class TestRadiusIsPinned(unittest.TestCase):
    """⚠ Правка разбора бьёт шире списка позиций, который смотрели глазами (урок захода 3)."""

    def test_exactly_one_position_changes_across_the_corpus(self):
        orig = th._fmt_flat
        changed, lost, gained = [], 0, 0
        for f in glob.glob(str(ROOT / "knowledge_base" / "pp719" / "structured" / "*.json")):
            for r in json.loads(Path(f).read_text(encoding="utf-8")):
                codes = r.get("okpd2_codes") or []
                name = r.get("product_name") or ""
                sec = r.get("section_roman")
                new = th.lookup_threshold(codes, name, sec)
                try:
                    th._fmt_flat = lambda x, group=False, product_name="": orig(x, group=group)
                    old = th.lookup_threshold(codes, name, sec)
                finally:
                    th._fmt_flat = orig
                if old != new:
                    changed.append(name)
                lost += int(old is not None and new is None)
                gained += int(old is None and new is not None)
        self.assertEqual(lost, 0, "правка ПОТЕРЯЛА пороги")
        self.assertEqual(gained, 0, "правка добавила пороги — она не должна их создавать")
        self.assertEqual(changed, [КОНТАКТОРЫ],
                         f"радиус изменился: {changed}")


if __name__ == "__main__":
    unittest.main()
