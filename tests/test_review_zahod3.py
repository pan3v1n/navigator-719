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


class TestDecisivePoolIsNotTautological(unittest.TestCase):
    """⚠⚠ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ БЫЛ ФИКТИВНЫМ: «доступно» считалось тем же выражением, что «названо».

    `dec_pool` был ПОБАЙТНО `len(set().union(*dec_sets))` — то есть объединением того, что ответы
    СКАЗАЛИ. Строка «⚠ доступно решающих чисел: N» не могла показать «назвали 1 из 60» — ровно то,
    ради чего у соседней метрики заведён `alien_pool`, который считается по ОКНУ.

    ⚠ На этой метрике стоит порог базы ≥0.95 и запись «1.00 ×3»."""

    def _report(self, rows, runs=3):
        sys.path.insert(0, str(ROOT / "scripts"))
        import eval_determinism as ed

        return "\n".join(ed.report(rows, runs))

    def test_pool_comes_from_available_not_from_named(self):
        import inspect
        sys.path.insert(0, str(ROOT / "scripts"))
        import eval_determinism as ed

        src = inspect.getsource(ed.evaluate)
        self.assertIn("dec_avail |= avail", src, "пул снова считается не по доступному")
        self.assertNotIn('"dec_pool": len(set().union(*dec_sets))', src,
                         "пул снова тождественен объединению названного")

    def test_pool_can_exceed_what_was_named(self):
        """Главное свойство: «назвали 1 из 60» обязано быть выразимо."""
        row = {"q": "x", "dec_variants": 1, "dec_union": 1, "dec_common": 1, "dec_pool": 60,
               "sec_variants": 1, "flag_variants": 1, "alien_runs": 0, "alien_seen": [],
               "alien_pool": 0, "num_variants": 1, "sets": [], "secs": [], "flags": []}
        out = self._report([row])
        self.assertIn("доступно решающих чисел: 60", out)

    def test_nothing_measured_is_not_a_perfect_score(self):
        """⚠ Фолбэк рисовал 1.00 при пороге ≥0.95 там, где не измерено НИЧЕГО."""
        row = {"q": "x", "dec_variants": 1, "dec_union": 0, "dec_common": 0, "dec_pool": 0,
               "sec_variants": 1, "flag_variants": 1, "alien_runs": 0, "alien_seen": [],
               "alien_pool": 0, "num_variants": 1, "sets": [], "secs": [], "flags": []}
        out = self._report([row])
        self.assertIn("0/0 = 0.00", out, "«нет данных» снова показано как идеальный балл")
        self.assertIn("метрика ничего не проверяет", out, "предупреждение исчезло")


class TestExactNameBeatsWordOverlap(unittest.TestCase):
    """⚠⚠ ОДНОСЛОВНОЕ НАИМЕНОВАНИЕ НЕ МОГЛО ПРОЙТИ ТАЙ-БРЕЙК ПО ПОСТРОЕНИЮ.

    Ветка «таблицы, привязанные по коду» при нескольких кандидатах требовала пересечения ≥2
    значимых слов. `_name_overlap("Мочеприемники", ["Мочеприемники"])` = 1 — то есть дословное
    совпадение отвергалось, потому что слово одно.

    Цена конкретная: у позиции «Мочеприемники» (32.50.13.190, разд. VII) в прим. 81 есть строка,
    названная ДОСЛОВНО так же, с порогом 135 баллов, — а рантайм отдавал None. Эксперт порога не
    видел. Дефект прятался месяц, потому что классификатор его тоже не показывал (см. ниже)."""

    def test_single_word_name_now_attaches(self):
        got = th.lookup_threshold(["32.50.13.190", "32.50.50", "32.50.50.141"],
                                  "Мочеприемники", "VII")
        self.assertIsNotNone(got, "порог прим. 81 снова не доезжает до позиции")
        self.assertIn("135", got)

    def test_overlap_rule_alone_would_still_reject_it(self):
        """Отрицательный контроль: прежнее правило этот случай не берёт — значит чинили именно его."""
        self.assertLess(th._name_overlap("Мочеприемники", ["Мочеприемники"]), 2)

    def test_ambiguity_still_refuses_to_guess(self):
        """Принцип ветки не изменился: имя не различает — НЕ ГАДАЕМ."""
        self.assertIsNone(th.lookup_threshold(["32.50.13.190"], "Изделие без имени в примечаниях", "VII"))


class TestClassifierSeesLaterCoveringRow(unittest.TestCase):
    """⚠⚠ ГЕЙТ МОГ ПОКАЗЫВАТЬ НОЛЬ ПРИ ЖИВЫХ ДЕФЕКТАХ.

    `covering = covering or row` фиксировал ПЕРВУЮ покрывающую строку по порядку корпуса, и
    вердикт выносился по ней одной. Строка, называющая позицию дословно, но лежащая ниже, не
    рассматривалась — позиция уходила в «не наш дефект». `attachment_defects` при этом гейтится
    порогом 0: предохранитель, который не умеет сработать, равен отсутствующему."""

    # ⚠ Проверка ПОВЕДЕНЧЕСКАЯ, а не по тексту исходника. Первая редакция этого теста грепала
    # строку «covering = covering or row» — и падала, поймав её в КОММЕНТАРИИ, объясняющем
    # правку. Ровно тот класс ложного срабатывания, на котором проект уже горел на выкатке 20.08
    # («grep по имени удалённой константы нашёл его в комментарии об удалении»).
    ROWS = [
        {"codes": ["32.50.13.190"], "names": ["Вакуумные одноразовые пробирки"],
         "note": "28", "kind": "таблица", "quote": "порог чужой продукции"},
        {"codes": ["32.50.13.190"], "names": ["Мочеприемники"],
         "note": "81", "kind": "таблица", "quote": "2026 год — не менее 135 баллов"},
    ]

    def test_a_later_row_that_names_the_position_is_seen(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import classify_missing_thresholds as cm

        rec = {"okpd2_codes": ["32.50.13.190"], "product_name": "Мочеприемники",
               "min_threshold": None, "requirement_blocks": []}
        got = cm.classify(rec, self.ROWS)
        self.assertEqual(got["class"], "defect_unattached",
                         f"поздняя строка, называющая позицию, снова не видна: {got}")
        self.assertEqual(got["note"], "81", "выбрана не та строка")

    def test_when_no_row_speaks_about_us_the_verdict_is_unchanged(self):
        """Отрицательный контроль: правка не должна превращать чужие пороги в «наш дефект»."""
        sys.path.insert(0, str(ROOT / "scripts"))
        import classify_missing_thresholds as cm

        rec = {"okpd2_codes": ["32.50.13.190"], "product_name": "Наборы гинекологические",
               "min_threshold": None, "requirement_blocks": []}
        got = cm.classify(rec, self.ROWS)
        self.assertEqual(got["class"], "note_other_product", got)

    def test_gate_is_clean_and_that_zero_is_real(self):
        """Ноль дефектов теперь означает «нет дефектов», а не «не умею их видеть»."""
        sys.path.insert(0, str(ROOT / "scripts"))
        import classify_missing_thresholds as cm

        defects = [i for i in cm.collect("points") if i["class"] in cm.DEFECT_CLASSES]
        self.assertEqual(defects, [], f"неприсоединённые пороги: "
                                      f"{[(d.get('name'), d['class']) for d in defects][:5]}")


class TestEmptyCellsDoNotShiftYears(unittest.TestCase):
    """⚠⚠ ДЕФЕКТ, ОТ КОТОРОГО `_tables` УЖЕ ЧИНИЛИ, ВЕРНУЛСЯ В СОСЕДНЮЮ ФУНКЦИЮ.

    `_named_block_rows` собирал ячейки через `if c.strip()` и затем `zip(years, …)` — то есть
    ровно та форма, которую инвариант в `_tables` запрещает («ПУСТЫЕ ЯЧЕЙКИ НЕЛЬЗЯ ВЫБРАСЫВАТЬ»,
    ревью 20.08.2026): пропуск схлопывался и все значения строки уезжали ВЛЕВО, на чужой год.

    ⚠ Дефект ЛАТЕНТНЫЙ: на текущем корпусе таких пропусков в прим. 37/74 нет, радиус правки 0.
    Проверить его можно только синтетикой — иначе он ждал бы следующей редакции первоисточника.
    Форма строк взята дословно из `130_PRIMECHANIYA_prilozheniya.txt` (прим. 74)."""

    HEADER = " |до 31 декабря 2025 г.|с 1 января 2026 г.|"

    def _rows(self, data_line: str):
        lines = ['из 28.13.27 "Компрессорная установка смешанного хладагента":',
                 "", self.HEADER, data_line, ""]
        return th._named_block_rows(lines)

    def test_full_row_is_unchanged(self):
        got = self._rows("Компрессорная установка|не менее 160 баллов|не менее 210 баллов|")
        self.assertEqual(len(got), 1)
        self.assertIn("до 31 декабря 2025 г. — не менее 160 баллов", got[0]["threshold"])
        self.assertIn("с 1 января 2026 г. — не менее 210 баллов", got[0]["threshold"])

    def test_blank_cell_keeps_the_value_on_its_own_year(self):
        """Значение обязано остаться против СВОЕГО года, а не съехать на предыдущий."""
        got = self._rows("Компрессорная установка| |не менее 210 баллов|")
        self.assertEqual(len(got), 1)
        thr = got[0]["threshold"]
        self.assertIn("с 1 января 2026 г. — не менее 210 баллов", thr)
        self.assertNotIn("до 31 декабря 2025 г. — не менее 210 баллов", thr,
                         "значение съехало на чужой год — дефект вернулся")


class TestActTitleIsNotAProductName(unittest.TestCase):
    """⚠ Прим. 8(1) цитирует «Об утверждении Правил квалификации…», и цитата становилась ИМЕНЕМ
    строки-порога. Сегодня безвредно (у той строки нет кодов), но пост-проход снимает коды именно
    по признаку «вводная назвала продукт»: вводная, цитирующая акт И перечисляющая коды ОКПД2,
    молча потеряла бы привязку, и порог стал бы недостижим вовсе."""

    def test_act_titles_are_dropped(self):
        for t in ("Об утверждении Правил квалификации генерирующего объекта",
                  "О внесении изменений в постановление", "О порядке ведения реестра"):
            with self.subTest(t=t[:36]):
                self.assertEqual(th._quoted_names(f'из 27.11.50 "{t}" - 100 баллов'), [])

    def test_product_names_survive(self):
        got = th._quoted_names('из 27.31.12.110 "Волокна оптические одномодовые" - 50 баллов')
        self.assertEqual(got, ["Волокна оптические одномодовые"])

    def test_no_row_carries_an_act_title_as_a_name(self):
        bad = [(r.get("note"), n) for r in th._flat_thresholds()
               for n in (r.get("names") or []) if th._ACT_TITLE_RE.match(n)]
        self.assertEqual(bad, [], f"заголовок акта снова числится наименованием: {bad}")


class TestNoteNumberKeepsItsSubpoint(unittest.TestCase):
    """⚠⚠ ПРЕДОХРАНИТЕЛЬ, БЬЮЩИЙ ПО ВЕРНОМУ КОДУ, дороже отсутствующего — так говорит шапка
    самого `deploy.sh`, и здесь он был именно таким.

    Релизная проверка обрезала «прим. 8(1)» до «8», а это РАЗНЫЕ области:
    `note_scope('8')` = procurement, `note_scope('8(1)')` = general. Верный релиз, сославшийся на
    8(1), был бы ОСТАНОВЛЕН; закупочный подпункт N(M) при общем N, наоборот, проскочил бы."""

    def test_subpoint_is_captured_whole(self):
        import re
        src = (ROOT / "scripts" / "deploy" / "checks" / "common"
               / "20_k2_procurement_not_general.py").read_text(encoding="utf-8")
        m = re.search(r're\.findall\(r"([^"]+)", got\)', src)
        self.assertIsNotNone(m, "форма разбора номера примечания изменилась — перечитать проверку")
        rx = re.compile(m.group(1))
        self.assertEqual(rx.findall("[прим. 8(1)]"), ["8(1)"])
        self.assertEqual(rx.findall("[прим. 26(2)]"), ["26(2)"])
        self.assertEqual(rx.findall("[прим. 26.1]"), ["26.1"], "подпункт через точку потерян")

    def test_the_two_forms_really_differ_in_scope(self):
        """Отрицательный контроль: если бы области совпадали, обрезка была бы безобидной."""
        self.assertNotEqual(th.note_scope("8"), th.note_scope("8(1)"))


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
