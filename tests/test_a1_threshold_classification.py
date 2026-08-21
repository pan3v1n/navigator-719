r"""Третья форма примечания и классификация непокрытых порогов — `A1` (#56), остаток `K2` (#47).

ЧТО ЗДЕСЬ ЗАКРЕПЛЕНО. 21.08.2026 классификация непокрытых позиций показала, что разбор знал две
формы примечания — строку, НАЧИНАЮЩУЮСЯ с кода, и таблицу с шапкой по годам — и не знал третью:
код назван во ВВОДНОЙ, а график идёт ниже строками-ступенями.

    18. Продукция, классифицируемая кодом … 28.22.16.111, может быть отнесена … баллов:
        до 31 декабря 2026 г. - не менее 120 баллов;
        с 1 января 2027 г. - не менее 135 баллов.

Из-за этого «Лифты пассажирские» и «Счетчики электрической энергии» отвечали «порог не
предусмотрен», хотя он написан для их кода дословно. Радиус правки: +8 позиций получили порог,
у 3 он сменился с группового на точный, потеряно 0 — каждая сверена с первоисточником.

⚠ ТРИ ЛОВУШКИ ЭТОЙ ПРАВКИ, каждая уже сработала на живых данных и потому проверяется тестом:

1. ДАТА ПОХОЖА НА КОД: во вводных стоит «(в ред. … от 15.06.2026 N 746)», и `15.06.2026`
   подходит под общий шаблон кода ОКПД2.
2. ГРАНИЦА ПРИМЕЧАНИЯ ШИРЕ ЗАГОЛОВКА: у прим. 72 есть подпункты «72.1.» и «72.2.», а прежний
   `_NOTE_HDR_RE` требует пробел после точки и на них не срабатывает — сборщик ступеней
   проезжал сквозь них и тащил в «Порог» текст про оптические волокна. Тот же дефект, что
   `EV13` вычищала у прим. 17.
3. ДВА ПРИМЕЧАНИЯ НА ОДИН КОД: прим. 71 и 72 текстуально неразличимы и оба называют
   `26.11.22.200` и `.210`; различаются только ступенью 2028 года (39 против 35). Угадывать
   нельзя — разница в 4 балла с подлинной ссылкой опаснее отсутствующего порога.

Запуск:  .venv\Scripts\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.thresholds import _intro_codes, lookup_threshold  # noqa: E402
from scripts.classify_missing_thresholds import (  # noqa: E402
    DEFECT_CLASSES,
    classify,
    collect,
)


class TestIntroCodes(unittest.TestCase):
    """Ловушка 1: дата редакции не должна становиться кодом ОКПД2."""

    def test_amendment_date_is_not_a_code(self):
        line = ("18. Продукция, классифицируемая кодом по ОК 034-2014 (КПЕС 2008) 28.22.16.111, "
                "может быть отнесена … : (в ред. Постановления Правительства РФ от 15.06.2026 N 746)")
        self.assertEqual(_intro_codes(line), ["28.22.16.111"])

    def test_bare_date_without_amendment_wrapper(self):
        """Даже без «(в ред. …)» четырёхзначный сегмент — год, а не код."""
        self.assertEqual(_intro_codes("9. … от 02.04.2021 N 719 …"), [])

    def test_iz_prefix_stripped(self):
        self.assertEqual(_intro_codes("71. … из 26.11.22.200, из 26.11.22.210, 26.11.22.216 и …"),
                         ["26.11.22.200", "26.11.22.210", "26.11.22.216"])


class TestProseScheduleNotes(unittest.TestCase):
    """Третья форма примечания доезжает до позиции."""

    def test_note_18_lifts(self):
        thr = lookup_threshold(["28.22.16.111"], "Лифты пассажирские", "VI")
        self.assertIsNotNone(thr, "«Лифты пассажирские» снова без порога (прим. 18)")
        self.assertIn("не менее 120 баллов", thr)
        self.assertIn("не менее 135 баллов", thr)
        self.assertIn("[прим. 18]", thr)

    def test_note_19_meters(self):
        thr = lookup_threshold(["26.51.63.130"], "Счетчики электрической энергии", "XXII")
        self.assertIsNotNone(thr, "«Счетчики электрической энергии» снова без порога (прим. 19)")
        self.assertIn("не менее 60 баллов", thr)
        self.assertIn("[прим. 19]", thr)


class TestNoteBoundary(unittest.TestCase):
    """Ловушка 2: подпункты «72.1.»/«72.2.» — уже другое примечание."""

    def test_subnote_text_does_not_leak_into_threshold(self):
        thr = lookup_threshold(["26.11.22.215"], "Светодиоды инфракрасного диапазона", "IV")
        self.assertIsNotNone(thr)
        self.assertNotIn("Волокна оптические", thr,
                         "в «Порог» снова уехал текст подпунктов 72.1/72.2")
        self.assertNotIn("72.2", thr)
        self.assertLess(len(thr), 300, f"строка порога разрослась до {len(thr)} символов")

    def test_zero_width_characters_stripped(self):
        thr = lookup_threshold(["26.11.22.215"], "Светодиоды инфракрасного диапазона", "IV")
        self.assertNotIn("​", thr)


class TestAmbiguityNotGuessed(unittest.TestCase):
    """Ловушка 3: спор двух примечаний за один код разрешает эксперт, а не код."""

    def test_led_white_takes_note_71(self):
        thr = lookup_threshold(["26.11.22.216"], "Светодиоды белого диапазона", "IV")
        self.assertIn("не менее 39 баллов", thr, "прим. 71: ступень 2028 года — 39")
        self.assertIn("[прим. 71]", thr)

    def test_led_uv_takes_note_72(self):
        thr = lookup_threshold(["26.11.22.211"], "Светодиоды ультрафиолетового диапазона", "IV")
        self.assertIn("не менее 35 баллов", thr, "прим. 72: ступень 2028 года — 35")
        self.assertIn("[прим. 72]", thr)

    def test_shared_codes_stay_unresolved(self):
        """`26.11.22.200`/`.210` названы В ОБОИХ примечаниях — не показываем ничего."""
        for code, name in (("26.11.22.200", "Светодиоды, включая светодиодные модули"),
                           ("26.11.22.210", "Светодиоды (в части светодиодов белого диапазона)")):
            with self.subTest(code=code):
                self.assertIsNone(lookup_threshold([code], name, "IV"),
                                  "угадали между прим. 71 и 72 — так делать нельзя")


class TestExactCodeBeatsGroup(unittest.TestCase):
    """Примечание, написанное ДЛЯ ЭТОГО кода, конкретнее написанного для его группы."""

    def test_memory_module_takes_specific_note(self):
        thr = lookup_threshold(["26.20.22.160"],
                               "Энергозависимые части системы компьютерной (оперативная память)",
                               "IX")
        self.assertIn("[прим. 26(3)]", thr, "порог группы прим. 26 перебил точное прим. 26(3)")
        self.assertIn("не менее 60 баллов", thr, "ступень 2028 года прим. 26(3) — 60")
        self.assertNotIn("не менее 55 баллов", thr, "это ступень СОСЕДНЕГО прим. 26(2)")

    def test_ssd_takes_note_26_2(self):
        # ⚠ Наименование — ДОСЛОВНО как в корпусе: прим. 26(2) адресует продукцию по имени,
        # и сокращение здесь не сработает (первая редакция теста упала именно на этом).
        thr = lookup_threshold(
            ["26.20.22.110"],
            "Устройства внешние запоминающие полупроводниковые, сохраняющие информацию при "
            "выключении питания (твердотельные накопители информации)", "IX")
        self.assertIn("[прим. 26(2)]", thr)
        self.assertIn("не менее 55 баллов", thr, "ступень 2028 года прим. 26(2) — 55")


class TestNoteNamedProductScope(unittest.TestCase):
    """Примечание, назвавшее продукцию, сужено ДО НЕЁ — код общим ключом больше не служит.

    Прим. 72.1 «Волокна оптические ОДНОМОДОВЫЕ телекоммуникационные», 72.2 «многомодовые и
    волокна специального назначения», 72.3 «Преформы (заготовки)» делят ОДИН код
    `27.31.12.110`. Ступени в чанке есть только у 72.1 — то есть по коду находилась ровно одна
    строка, и её порог уходил всем трём позициям. Правильное число не той позиции, да ещё с
    подлинной ссылкой: гард молчит, эксперт видит подтверждение.
    """

    def test_multimode_fiber_gets_no_threshold_from_singlemode_note(self):
        self.assertIsNone(
            lookup_threshold(["27.31.12.110"],
                             "Волокна оптические многомодовые и волокна специального назначения",
                             "IV"),
            "многомодовые волокна снова получили порог прим. 72.1 (одномодовые)")

    def test_preforms_get_no_threshold_from_fiber_note(self):
        self.assertIsNone(
            lookup_threshold(["27.31.12.110"],
                             "Преформы (заготовки) для вытяжки оптических волокон", "IV"),
            "преформы снова получили порог прим. 72.1")

    def test_silicon_ingots_take_note_59(self):
        """Прим. 59: имя И порог в одной фразе вводной, без графика по годам."""
        thr = lookup_threshold(["20.13.21.290"], "Кремниевые слитки", "V")
        self.assertIsNotNone(thr, "«Кремниевые слитки» снова без порога (прим. 59)")
        self.assertIn("не менее 20 баллов", thr)
        self.assertIn("[прим. 59]", thr)
        self.assertLess(len(thr), 120, "в «Порог» уехал хвост предложения, а не только величина")

    def test_charging_stations_take_notes_67_68(self):
        dc = lookup_threshold(["27.11.50.120"],
                              "Электрозарядные станции постоянного тока для электротранспорта", "V")
        ac = lookup_threshold(["27.90.11.900"],
                              "Электрозарядные станции переменного тока для электротранспорта", "V")
        self.assertIn("не менее 54 баллов", dc or "")
        self.assertIn("не менее 27 баллов", ac or "")


class TestSubNoteNumbering(unittest.TestCase):
    """Ссылка обязана вести на ТОТ пункт первоисточника, из которого взято число.

    Заголовок «26.1.» не подходил под `_NOTE_HDR_RE` (тот требует пробел сразу после точки),
    поэтому 27 строк-порогов подпункта 26.1 уходили в ответ со ссылкой «[прим. 26]». Эксперт
    сверяет ответ по ссылке — неверный номер обесценивает верное число.
    """

    def test_sub_note_cited_by_its_own_number(self):
        thr = lookup_threshold(["26.30.11.110"],
                               "Средства связи, выполняющие функцию систем коммутации", "IX")
        self.assertIsNotNone(thr)
        self.assertIn("[прим. 26.1]", thr, "ссылка снова ведёт на прим. 26 вместо 26.1")

    def test_sub_note_has_its_own_scope(self):
        """Область действия у подпункта СВОЯ, а не унаследованная от родителя: иначе закупочный
        подпункт молча стал бы общим — тот класс, что стоил 57 позиций."""
        from app.rag.thresholds import _note_scopes

        scopes = _note_scopes()
        for num in ("26.1", "26(2)", "26(3)", "72.1"):
            with self.subTest(note=num):
                self.assertIn(num, scopes, f"подпункт {num} не получил собственной области действия")


class TestClassifierPositiveControl(unittest.TestCase):
    """⚠ ДЕТЕКТОР, ОТВЕЧАЮЩИЙ «НОЛЬ ДЕФЕКТОВ», ОБЯЗАН УМЕТЬ ОТВЕТИТЬ «ЕСТЬ».

    Классификация даёт 0 дефектов привязки на текущем корпусе — то есть ровно тот результат,
    который снаружи неотличим от слепоты инструмента. Поэтому здесь он проверяется на
    синтетических данных, где ответ известен заранее.
    """

    REC = {"okpd2_codes": ["12.34.56.789"], "product_name": "Изделие испытательное",
           "section_roman": "I", "requirement_type": "points", "min_threshold": None,
           "requirement_blocks": [{"component": "узел", "operations": [{"text": "операция"}]}]}

    def test_unnamed_note_row_is_a_defect(self):
        """Строка примечания без наименования = порог задан КОДУ. Не привязали — наш дефект."""
        rows = [{"codes": ["12.34.56.789"], "names": [], "note": "99",
                 "quote": "с 1 января 2026 г. - не менее 50 баллов", "kind": "список"}]
        self.assertEqual(classify(self.REC, rows)["class"], "defect_unattached")

    def test_note_row_about_another_product_is_not_a_defect(self):
        """Тот же код, но строка про другую продукцию — привязка дала бы чужое число."""
        rows = [{"codes": ["12.34.56.789"], "names": ["Совершенно иная продукция"], "note": "99",
                 "quote": "не менее 50 баллов", "kind": "список"}]
        self.assertEqual(classify(self.REC, rows)["class"], "note_other_product")

    def test_matching_name_is_a_defect(self):
        rows = [{"codes": ["12.34.56.789"], "names": ["Изделие испытательное"], "note": "99",
                 "quote": "не менее 50 баллов", "kind": "список"}]
        self.assertEqual(classify(self.REC, rows)["class"], "defect_unattached")

    def test_component_threshold_of_another_section_is_not_ours(self):
        """«шасси … операций, установленных разделом II … не менее 1700 баллов» — порог ШАССИ."""
        rec = dict(self.REC, requirement_blocks=[{"component": "кузов", "operations": [
            {"text": "использование шасси колесного транспортного средства, произведенного на "
                     "территории Российской Федерации, с выполнением операций (условий), "
                     "установленных разделом II настоящего приложения, которые в совокупности "
                     "оцениваются с 1 января 2025 г. количеством баллов не менее 1700 баллов"}]}])
        self.assertEqual(classify(rec, [])["class"], "component_threshold")

    def test_no_evidence_is_no_source(self):
        self.assertEqual(classify(self.REC, [])["class"], "no_source")


class TestClassifierOnCorpus(unittest.TestCase):
    """Результат `A1`: полоса 16.6–31.6 % схлопнулась в одно число."""

    @classmethod
    def setUpClass(cls):
        cls.items = collect("points")

    def test_no_attachment_defects_left(self):
        defects = [i for i in self.items if i["class"] in DEFECT_CLASSES]
        self.assertEqual(defects, [], "появились непривязанные пороги — разобрать поимённо")

    def test_every_item_has_evidence_text(self):
        """Классификация без улики бесполезна: эксперт должен видеть, на чём основан вывод."""
        for i in self.items:
            with self.subTest(code=i["code"]):
                self.assertTrue(i["evidence"].strip())

    def test_classes_are_exhaustive(self):
        from scripts.classify_missing_thresholds import CLASSES
        self.assertEqual({i["class"] for i in self.items} - set(CLASSES), set())


if __name__ == "__main__":
    unittest.main()
