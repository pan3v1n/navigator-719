"""Корпус СТ-1: разбор Соглашения СНГ и Приказа ТПП РФ №14 (`K14` #35).

⚠⚠ ЧТО ЗДЕСЬ ЗАКРЕПЛЕНО И ПОЧЕМУ ИМЕННО ЭТО. Каждый пункт куплен реальным дефектом разбора,
найденным на исходнике, а не придуман:

1. **Пункт нумеруется БЕЗ точки.** Приказ №14 пишет «4.1 Сертификаты формы СТ-1 выдаются…»,
   а прежний шаблон точку требовал. Он не находил НИ ОДНОГО настоящего пункта Приказа — зато
   исправно ловил подписи граф БЛАНКОВ («1. Exporter», «2. Consignee»), где точка есть.
   Разбор давал 12 записей из 231 кандидата, и числа выглядели правдоподобно.
2. **Раздел без нумерации отдаётся целиком.** «Термины и понятия» в ОБОИХ документах идут
   списком через тире. Парсер по пунктам проходил мимо, молча теряя словарь, которым как раз
   и спрашивают: «критерий достаточной обработки/переработки», «кумулятивный принцип».
3. **Владелец пункта хранится.** У Приказа ДЕВЯТЬ верхнеуровневых приложений, и каждое — своё
   Положение (общий порядок, общая форма, СТ-1, СТ-2, СТ-3, EAV, «A», пушнина, зерно). Разделы
   внутри нумеруются с единицы: «Раздел 1» встречается девять раз. Без владельца якорь указывал
   бы не на тот документ — самый дорогой класс дефекта в этом проекте.
4. **Перечня условий в корпусе НЕТ.** Он живёт таблицей фактов и лукапом; два источника одного
   факта расходятся молча.
5. **Редакция международного договора — Протокол БЕЗ номера.** Прежний распознаватель выдавал за
   редакцию Соглашения ссылку на посторонний акт (`ред. от 18.06.2010 N 324`), найденную внутри
   текста. Ложный штамп опаснее отсутствующего: гейт отставания корпуса сравнивал бы его с
   манифестом и молчал.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import load_rules_kb as L  # noqa: E402
from app.core import manifest as M  # noqa: E402

KB = ROOT / "knowledge_base"
SNG = KB / "pp719" / "sng_origin_rules.txt"
P14 = KB / "pp719" / "prikaz14_tpp_full.txt"


def _skip_if_missing(t: unittest.TestCase, p: Path):
    if not p.exists():
        t.skipTest(f"{p.name} не сгенерирован (scripts/convert_st1_docs.py)")


class TestPointNumberingWithoutDot(unittest.TestCase):
    """⚠⚠ Дефект-инверсия: шаблон ловил ровно НЕ ТО, и числа выглядели правдоподобно."""

    def test_point_without_trailing_dot_is_recognised(self):
        self.assertTrue(L._K14_POINT_RE.match("4.1 Сертификаты формы СТ-1 выдаются"))
        self.assertTrue(L._K14_POINT_RE.match("3.2.1 Акт экспертизы, подтверждающий"))

    def test_point_with_trailing_dot_still_works(self):
        """⚠ Обратная половина: Соглашение СНГ точку СТАВИТ, и её нельзя сломать."""
        self.assertTrue(L._K14_POINT_RE.match("2.4. Критерий достаточной переработки"))

    def test_single_number_is_not_a_point(self):
        """⚠⚠ Именно так выглядят подписи граф бланка. Взяв их за пункты, разбор набивает индекс
        строками «1. Exporter (name, address, country)» — класс `D12`, записи-обрывки."""
        for stub in ("1. Exporter (name, address, country)", "2. Consignee", "7 Item number"):
            self.assertIsNone(L._K14_POINT_RE.match(stub), stub)


class TestSectionWithoutPointsIsKept(unittest.TestCase):
    """⚠ «Термины и понятия» нумерации не имеют — определения идут через тире."""

    def test_terms_section_of_the_agreement_reaches_the_index(self):
        _skip_if_missing(self, SNG)
        recs = L.parse_sng_origin(SNG)
        terms = [r for r in recs if r["section_roman"] == "1"]
        self.assertTrue(terms, "раздел «Термины и понятия» потерян целиком")
        joined = " ".join(r["text"] for r in terms)
        for word in ("критерий достаточной обработки", "кумулятивный принцип"):
            self.assertIn(word, joined)

    def test_numbered_sections_are_still_cut_by_points(self):
        """⚠ Обратная половина: разбор, всегда отдающий раздел целиком, потерял бы дробность."""
        _skip_if_missing(self, SNG)
        recs = L.parse_sng_origin(SNG)
        sec2 = [r for r in recs if r["section_roman"] == "2"]
        self.assertGreater(len(sec2), 3)
        self.assertTrue(any(r["point"].startswith("2.") for r in sec2))


class TestOwnershipOfPoints(unittest.TestCase):
    """⚠⚠ САМЫЙ ДОРОГОЙ КЛАСС: правильный текст, привязанный не к тому месту."""

    def setUp(self):
        _skip_if_missing(self, P14)
        self.recs = L.parse_prikaz14(P14)

    def test_points_are_globally_unique(self):
        seen = [r["point"] for r in self.recs]
        self.assertEqual(len(seen), len(set(seen)),
                         "номера пунктов повторяются — владелец не сохранён")

    def test_every_top_level_polozhenie_is_present(self):
        """Девять приложений приказа, каждое — своё Положение."""
        parts = {r["section_roman"].split(".")[0] for r in self.recs}
        self.assertGreaterEqual(len(parts), 8, f"найдено приложений: {sorted(parts)}")

    def test_st1_polozhenie_is_attributed_to_st1(self):
        """⚠ Ради этого Положения заход и делается — его якорь обязан называть СТ-1."""
        st1 = [r for r in self.recs if r["section_roman"].startswith("3.")]
        self.assertTrue(st1)
        self.assertTrue(all("прил. 3" in r["source_anchor"] for r in st1))
        self.assertTrue(any("СТ-1" in r["source_anchor"] for r in st1))

    def test_anchor_names_the_document_and_the_place(self):
        """⚠ Место — это ПУНКТ либо РАЗДЕЛ: у ненумерованных разделов («Термины и понятия»)
        пункта нет по построению, и требовать «п. » от них значило бы требовать несуществующего."""
        for r in self.recs:
            self.assertIn("Приказ ТПП РФ №14", r["source_anchor"])
        anchors = " ".join(r["source_anchor"] for r in self.recs)
        self.assertIn("п. ", anchors)
        self.assertIn("Раздел ", anchors)


class TestPerechenIsNotInTheCorpus(unittest.TestCase):
    """⚠⚠ Перечень условий обслуживается ТАБЛИЦЕЙ ФАКТОВ. Попади он ещё и в вектор — два
    источника одного факта разошлись бы молча, и в контекст попадал бы то обрывок таблицы
    (без соседних строк, без пометки «из», без диапазона), то результат лукапа."""

    def test_corpus_text_has_no_perechen(self):
        _skip_if_missing(self, SNG)
        text = SNG.read_text(encoding="utf-8")
        self.assertNotIn("ПЕРЕЧЕНЬ УСЛОВИЙ", text)

    def test_but_the_prose_that_refers_to_it_is_kept(self):
        """⚠ Обратная половина: ССЫЛКА на Перечень — норма, и она обязана остаться, иначе ответ
        не сможет объяснить, почему для одних товаров правило иное."""
        _skip_if_missing(self, SNG)
        text = SNG.read_text(encoding="utf-8")
        self.assertIn("включенных в Перечень", text)

    def test_blank_forms_do_not_reach_the_index(self):
        """⚠⚠ ПРОВЕРЯЕМ ЗАПИСИ, А НЕ ФАЙЛ. Значение имеет то, что доезжает до индекса: в тексте
        остаётся ещё блок подписей сторон Соглашения («За Правительство … (подпись)»), но он
        стоит ДО первого раздела и записью не становится. Тест на файл краснел бы на безобидном.

        ⚠ Названия граф при этом законно живут в п. 7.4 («Заполнение сертификата формы СТ-1
        должно отвечать следующим требованиям: графа 1 — …») — это норма, и она обязана остаться.
        Вырезан сам БЛАНК: разграфка и пустые поля под вписывание."""
        _skip_if_missing(self, SNG)
        recs = L.parse_sng_origin(SNG)
        bad = [r for r in recs if r["text"].count("|") >= 3 or "N ______" in r["text"]]
        self.assertFalse(bad, f"разграфка бланка доехала до индекса: "
                              f"{[r['point'] for r in bad][:3]}")

    def test_the_rules_for_filling_the_form_are_kept(self):
        """⚠ Обратная половина: вырезав вместе с бланком и правила его заполнения, мы потеряли бы
        самый практичный раздел — именно его спрашивают («как заполнять графы СТ-1»)."""
        _skip_if_missing(self, SNG)
        text = SNG.read_text(encoding="utf-8")
        self.assertIn("Заполнение сертификата формы СТ-1", text)
        self.assertIn("графа 1", text)


class TestAltaChromeIsStripped(unittest.TestCase):
    """⚠ Контакты агрегатора в корпусе — это ТЕЛЕФОН РЯДОМ С НОРМОЙ: faithfulness-гард считает
    заземлённым всё, что лежит в контексте, и выдуманным такой номер уже не назовёт (класс `EV5`).
    Плюс агрегатор не первоисточник, а сервис печатает источники пользователю."""

    def test_no_aggregator_contacts(self):
        _skip_if_missing(self, P14)
        text = P14.read_text(encoding="utf-8")
        for chrome in ("alta.ru", "995-95-55", "alta@"):
            self.assertNotIn(chrome, text)

    def test_the_normative_text_survived(self):
        """⚠ Обратная половина: чистка, вырезающая всё, тоже «уберёт обвязку»."""
        _skip_if_missing(self, P14)
        text = P14.read_text(encoding="utf-8")
        self.assertIn("Сертификаты формы СТ-1 выдаются", text)
        self.assertGreater(len(text), 150_000)


class TestProtocolEdition(unittest.TestCase):
    """⚠⚠ Международный договор изменяется ПРОТОКОЛОМ, у которого номера нет."""

    HEADER = ("СОГЛАШЕНИЕ от 20 ноября 2009 года\n"
              "(в ред. Протоколов от 18.10.2011, от 28.09.2012, от 03.11.2017, от 08.06.2023)\n")

    def test_latest_protocol_wins(self):
        """⚠ Первая редакция шаблона брала только ПЕРВУЮ дату перечня (18.10.2011): `finditer`
        продолжает после конца совпадения. Штамп получался на двенадцать лет старше и выглядел
        совершенно нормально."""
        self.assertEqual(L.detect_edition(self.HEADER), "ред. от 08.06.2023")

    def test_foreign_numbered_act_does_not_outrank_a_later_protocol(self):
        """⚠⚠ Ровно тот дефект: в тексте Соглашения нашлась ссылка `ред. от 18.06.2010 N 324` на
        ПОСТОРОННИЙ акт, и она выдавалась за редакцию документа."""
        text = self.HEADER + "\nсогласно Положению (ред. от 18.06.2010 N 324) …"
        self.assertEqual(L.detect_edition(text), "ред. от 08.06.2023")

    def test_numbered_act_still_wins_when_it_is_later(self):
        """⚠ Обратная половина: пронумерованные акты не должны проиграть протоколу вообще."""
        text = self.HEADER + "\n(в ред. Приказа от 16.07.2026 N 59)"
        self.assertEqual(L.detect_edition(text), "ред. от 16.07.2026 N 59")

    def test_bare_date_is_not_an_edition(self):
        """⚠ Голое «от ДД.ММ.ГГГГ» встречается сотнями (даты писем, договоров). Штамповать по
        нему хуже, чем не штамповать вовсе."""
        self.assertEqual(L.detect_edition("договор от 01.01.2030 подписан"),
                         "редакция не определена в тексте")


class TestEditionRadius(unittest.TestCase):
    """⚠⚠ ПРАВКА РАСПОЗНАВАТЕЛЯ БЬЁТ ПО ВСЕМ ДОКУМЕНТАМ, А НЕ ПО НОВОМУ. Радиус измеряется, а не
    предполагается: штамп каждого документа корпуса обязан совпасть с манифестом."""

    def test_every_document_matches_its_manifest_edition(self):
        for doc in M.documents("pp719_rules"):
            srcs = doc.get("sources") or []
            expected = doc.get("edition_expected")
            if not srcs or not expected:
                continue
            paths = [KB / s for s in srcs]
            if not all(p.exists() for p in paths):
                continue
            text = "\n".join(p.read_text(encoding="utf-8") for p in paths)
            with self.subTest(doc=doc["doc_type"]):
                self.assertEqual(L.detect_edition(text), expected,
                                 f"{doc['doc_type']}: штамп разошёлся с манифестом")


class TestManifestKnowsBothDocuments(unittest.TestCase):
    def test_passports_are_declared(self):
        by_type = {d["doc_type"]: d for d in M.documents("pp719_rules")}
        for dt, authority, topic in (("sng_origin_rules", "СНГ", "СТ-1"),
                                     ("prikaz14_tpp", "ТПП РФ", "СТ-1")):
            self.assertIn(dt, by_type)
            self.assertEqual(by_type[dt]["authority"], authority)
            self.assertEqual(by_type[dt]["topic"], topic)

    def test_second_key_is_declared_on_the_agreement(self):
        """⚠⚠ Ради этого поля заход и заводился: условия СТ-1 живут в разрезе ТН ВЭД, а весь
        остальной корпус построен на ОКПД2."""
        by_type = {d["doc_type"]: d for d in M.documents("pp719_rules")}
        self.assertEqual(by_type["sng_origin_rules"]["key_type"], "ТН ВЭД")

    def test_both_have_parsers(self):
        for dt in ("sng_origin_rules", "prikaz14_tpp"):
            self.assertIn(dt, L.PARSERS)


if __name__ == "__main__":
    unittest.main()
