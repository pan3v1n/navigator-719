"""`K16` #48 — процедура экспертизы происхождения (Приказ ТПП РФ №49) и разъяснения ГИСП.

ЧТО ЗАКРЫВАЕТ ЗАДАЧА. Корпус знал ТРЕБОВАНИЯ к происхождению (Соглашение СНГ) и порядок ВЫДАЧИ
сертификата (Приказ №14), но не знал двух вещей между ними:
  * процедуру ЭКСПЕРТИЗЫ — какие документы подаёт заявитель под каждый критерий и что попадает
    в акт экспертизы (Положение, приложение к приказу ТПП РФ №49);
  * порядок действий В СЕРВИСЕ ГИСП — «заявку можно подать только через ГИСП», «включение по
    СТ-1 происходит автоматически за 1 рабочий день», «отчёт — до 1 апреля». Этого нет ни в
    одном нормативном акте, потому что это не норма, а разъяснение оператора портала.

⚠⚠⚠ КРИТЕРИЙ ПРИЁМКИ ISSUE #48 — «ответы по процедуре ГИСП снабжены источником и пометкой
"не норма"». Он обеспечивается ТРЕМЯ вещами, и каждая проверяется здесь: `legal_force: 5` в
паспорте, подпись якоря «(разъяснение портала, не норма)» и отсутствие выдуманной редакции.

⚠⚠ ПЕРВОИСТОЧНИК ПОЛОЖЕНИЯ ОТДАЁТ УСТАРЕВШИЙ ФАЙЛ. ТПП РФ публикует PDF редакции 2023 года, а
документ действует в ред. приказа №59 от 16.07.2026 — того же, которым изменён Приказ №14.
Разница измерена: похожесть тела 0.983, +371 знак, ОДИН абзац в п. 6.13. Он дописывается
конвертером дословно, и здесь проверяется, что он доехал до записи ровно один раз.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.core import manifest as kb_manifest  # noqa: E402

P49_TXT = ROOT / "knowledge_base" / "pp719" / "polozhenie49_tpp.txt"
FAQ_TXT = ROOT / "knowledge_base" / "pp719" / "gisp_faq.txt"
AMEND = "16.07.2026 N 59"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCorpusTextsArePresent(unittest.TestCase):
    """Тексты корпуса коммитятся; PDF-исходники — нет (`.gitignore`, `/*.pdf`)."""

    def test_both_texts_exist(self):
        for p in (P49_TXT, FAQ_TXT):
            with self.subTest(file=p.name):
                self.assertTrue(p.exists(), f"нет {p.name} — соберите конвертером")
                self.assertGreater(len(p.read_text(encoding="utf-8")), 3000)

    def test_each_text_names_its_converter(self):
        """⚠ Единственное, что связывает текст корпуса с первоисточником, — скрипт сборки.

        Если шапка перестанет его называть, происхождение файла станет непроверяемым."""
        for p, script in ((P49_TXT, "convert_polozhenie49.py"), (FAQ_TXT, "convert_gisp_faq.py")):
            with self.subTest(file=p.name):
                head = p.read_text(encoding="utf-8")[:900]
                self.assertIn(script, head)
                self.assertTrue((ROOT / "scripts" / script).exists())


class TestPolozhenie49(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.L = _load("lrk_k16", "scripts/load_rules_kb.py")
        cls.recs = cls.L.parse_polozhenie49(P49_TXT)

    def test_all_sections_and_points_parsed(self):
        pts = [r["point"] for r in self.recs]
        self.assertGreaterEqual(len(pts), 60, "пунктов меньше, чем в документе — разбор потерял")
        secs = {r["section_roman"] for r in self.recs}
        self.assertGreaterEqual(len(secs), 5, "разделы потеряны")

    def test_point_numbers_are_unique(self):
        """⚠⚠ Дубль номера — это коллизия `by_key`, из-за которой подпункт теряет вводную.

        Первая редакция разбора давала дубль п. 6.7: строка «6.7 настоящего Положения, …» —
        ССЫЛКА внутри п. 6.6, перенесённая вёрсткой на начало строки, — становилась пунктом.
        Признак ловил форму, которая встречается и в прозе: тот же класс, на котором пять раундов
        подряд ломался разбор ТН ВЭД."""
        pts = [r["point"] for r in self.recs]
        dupes = sorted({p for p in pts if pts.count(p) > 1})
        self.assertEqual(dupes, [], f"номер пункта продублирован: {dupes}")

    def test_a_reference_in_prose_is_not_a_point(self):
        """Отрицательный контроль к предыдущему — на СИНТЕТИКЕ, а не на сегодняшнем файле."""
        self.assertIsNone(self.L._P49_POINT_RE.match("6.7 настоящего Положения, уполномоченная"))
        self.assertIsNotNone(self.L._P49_POINT_RE.match("6.7. При проведении экспертизы"))
        self.assertIsNotNone(self.L._P49_POINT_RE.match("4.3.6 Продукция, полученная"))

    def test_the_2026_amendment_reached_exactly_one_point(self):
        """⚠⚠ Абзац приказа №59 — единственное, чем действующая редакция отличается от файла ТПП."""
        carriers = [r["point"] for r in self.recs if AMEND in r["text"]]
        self.assertEqual(carriers, ["6.13"],
                         "абзац приказа №59 потерян или размножен — перечитайте "
                         "scripts/convert_polozhenie49.py")
        self.assertIn("оттиска печати", next(r["text"] for r in self.recs if r["point"] == "6.13"))

    def test_anchor_names_the_document(self):
        a = self.recs[0]["source_anchor"]
        self.assertIn("№49", a)
        self.assertIn("п.", a)


class TestGispFaq(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.L = _load("lrk_k16_faq", "scripts/load_rules_kb.py")
        cls.recs = cls.L.parse_gisp_faq(FAQ_TXT)

    def test_every_question_has_an_answer(self):
        self.assertGreaterEqual(len(self.recs), 8)
        for r in self.recs:
            with self.subTest(point=r["point"]):
                self.assertIn("?", r["text"], "вопрос потерян — остался один ответ")
                tail = r["text"].split("?", 1)[1].strip()
                self.assertTrue(tail, "у вопроса пустой ответ")

    def test_anchor_marks_it_as_not_a_norm(self):
        """⚠⚠⚠ ДОСЛОВНЫЙ КРИТЕРИЙ ПРИЁМКИ issue #48 — пометка «не норма» у источника."""
        for r in self.recs:
            with self.subTest(point=r["point"]):
                self.assertIn("не норма", r["source_anchor"])
                self.assertIn("ГИСП", r["source_anchor"])

    def test_no_site_chrome_leaked_into_the_corpus(self):
        joined = "\n".join(r["text"] for r in self.recs)
        for junk in ("Войти", "Напишите нам", "Главная >", "техподдержка"):
            with self.subTest(junk=junk):
                self.assertNotIn(junk, joined, "обвязка сайта уехала в корпус")

    def test_the_phantom_document_is_absent(self):
        """⚠⚠⚠ «Заключение ТПП, подтверждающее производство» НЕ СУЩЕСТВУЕТ (`K15`).

        Пересказы этого FAQ на сторонних сайтах его называют; настоящая страница — нет. Попади
        такой текст в корпус, он ЗАЗЕМЛИЛ БЫ выдумку, и рантайм-гард пропустил бы её: гард ловит
        незаземлённое, а не неверное."""
        joined = "\n".join(r["text"] for r in self.recs)
        self.assertNotRegex(joined, r"заключени\w*\s+(?:ТПП|торгово)")

    def test_it_points_at_documents_the_corpus_already_has(self):
        """Ценность FAQ — в отсылке к нормам: он связывает портал с ПП 719 и Приказом №52."""
        joined = "\n".join(r["text"] for r in self.recs)
        self.assertIn("719", joined)
        self.assertIn("52", joined)


class TestPassportsAndEdition(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.man = kb_manifest.load_manifest()
        cls.by_type = {d["doc_type"]: d for d in cls.man["documents"]}

    def test_both_documents_are_in_the_manifest(self):
        for dt in ("polozhenie49_tpp", "gisp_faq"):
            self.assertIn(dt, self.by_type, f"{dt} не описан в манифесте")

    def test_gisp_is_practice_and_expertise_is_a_departmental_act(self):
        self.assertEqual(self.by_type["gisp_faq"]["legal_force"], 5)
        self.assertEqual(self.by_type["gisp_faq"]["topic"], "ГИСП")
        self.assertEqual(self.by_type["polozhenie49_tpp"]["legal_force"], 3)
        self.assertEqual(self.by_type["polozhenie49_tpp"]["topic"], "СТ-1")

    def test_expertise_expects_the_2026_edition(self):
        """⚠ Тот же приказ №59, что и у Приказа №14 — сверка ловит расхождение на загрузке."""
        self.assertEqual(self.by_type["polozhenie49_tpp"]["edition_expected"],
                         self.by_type["prikaz14_tpp"]["edition_expected"])

    def test_faq_gets_no_invented_edition(self):
        """⚠⚠⚠ У страницы портала СВОЕЙ редакции нет, и выдумывать её нельзя.

        `detect_edition` — эвристика по тексту, а текст цитирует «ПП РФ от 17.07.2015 № 719».
        Штамп выходил «ред. от 17.07.2015 N 719» — реквизиты ДРУГОГО документа, выданные за
        редакцию этого, и уезжал в payload каждого пункта."""
        self.assertIsNone(self.by_type["gisp_faq"]["edition_expected"])
        L = _load("lrk_k16_ed", "scripts/load_rules_kb.py")
        with redirect_stdout(io.StringIO()):
            recs, _ = L.load_records(self.man)
        faq = [r for r in recs if r["doc_type"] == "gisp_faq"]
        self.assertTrue(faq)
        for r in faq:
            self.assertEqual(r.get("edition"), "", "у FAQ появилась выдуманная редакция")

    def test_the_detector_would_still_invent_one(self):
        """Отрицательный контроль: правка ЗАКРЫВАЕТ СИМПТОМ, а не чинит детектор.

        ⚠ Строгий вариант `_AMEND_RE` (с требованием слов «в ред.») замерен и отвергнут: FAQ он
        чинит, но телу ПП №719 даёт «ред. от 10.09.2025 N 1392» вместо «22.07.2026 N 923». Класс
        записан долгом — и этот тест не даёт забыть, что долг ещё есть."""
        L = _load("lrk_k16_det", "scripts/load_rules_kb.py")
        raw = FAQ_TXT.read_text(encoding="utf-8")
        self.assertEqual(L.detect_edition(raw), "ред. от 17.07.2015 N 719",
                         "детектор перестал выдумывать редакцию — долг закрыт, "
                         "уберите обход в load_records и этот тест")


class TestTopicQuotaKnowsTheNewDocuments(unittest.TestCase):
    """Квота окна: у экспертизы ПРОИСХОЖДЕНИЯ свой документ, а не раздел Приказа №52.

    ⚠ Проверяется ТАБЛИЦА ТЕМ (офлайн, без Qdrant), а не порядок выдачи: порядок зависит от
    индекса, а таблица — это решение о том, чей предмет вопрос затрагивает."""

    @staticmethod
    def _scores(q: str) -> dict[str, int]:
        from app.rag import retriever as R
        return {dt: sum(1 for p in pats if p.search(q)) for dt, pats in R._RULES_TOPIC}

    def test_origin_expertise_beats_order52(self):
        """⚠⚠ Предметы РАЗНЫЕ: у №52 акт экспертизы подтверждает ПРОИЗВОДСТВО в РФ для реестра,
        у №49 экспертиза определяет СТРАНУ ПРОИСХОЖДЕНИЯ для СТ-1."""
        for q in ("какие документы нужны для экспертизы происхождения товара",
                  "что входит в акт экспертизы происхождения",
                  "как проводится экспертиза по определению страны происхождения",
                  "сроки проведения экспертизы происхождения"):
            with self.subTest(q=q):
                s = self._scores(q)
                self.assertGreater(s["polozhenie49_tpp"], s["tpp_order_52"],
                                   f"вопрос про экспертизу происхождения ушёл к №52: {s}")

    def test_order52_keeps_its_own_subject(self):
        """⚠⚠⚠ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ — цена правки. Сужение ствола «экспертиз» не должно отнимать
        у №52 то, что у него работало: ось `K9` держит на нём 9 из 9 по атрибуции."""
        for q in ("какие документы нужны для получения акта экспертизы",
                  "перечень документов для подтверждения производства",
                  "что такое акт экспертизы и когда он нужен",
                  "как формируется заявка на включение в реестр"):
            with self.subTest(q=q):
                s = self._scores(q)
                self.assertGreater(s["tpp_order_52"], s.get("polozhenie49_tpp", 0),
                                   f"у №52 отобрали его собственный предмет: {s}")

    def test_no_tie_between_the_two(self):
        """⚠⚠ Ничья в `_rules_topic` даёт None и СНИМАЕТ `primary` у ОБОИХ — то есть правка ради
        нового документа испортила бы старому то, что у него работало. Ничьих быть не должно."""
        for q in ("какие документы нужны для экспертизы происхождения товара",
                  "какие документы нужны для получения акта экспертизы"):
            with self.subTest(q=q):
                s = self._scores(q)
                self.assertNotEqual(s["polozhenie49_tpp"], s["tpp_order_52"],
                                    "ничья: главу окна отдали ранжированию")

    def test_gisp_is_not_forced_to_lead(self):
        """⚠⚠ FAQ НАРОЧНО НЕ ПОЛУЧАЕТ СВОЕЙ СТРОКИ В ТАБЛИЦЕ, и это решение, а не упущение.

        Он `legal_force: 5` — слабейший источник корпуса. Критерий приёмки issue #48 требует
        «источник и пометку не норма», а НЕ первого места; поставить практику во главу окна над
        нормой значило бы перевернуть иерархию, на которой стоит `K13`. Достаточно, что документ
        В ОКНЕ, — это проверяется живым прогоном, а здесь фиксируется само решение."""
        from app.rag import retriever as R
        self.assertNotIn("gisp_faq", [dt for dt, _ in R._RULES_TOPIC],
                         "у FAQ появилась строка квоты — практика начнёт возглавлять окно "
                         "над нормой; если это осознано, перепишите этот тест и объясните")


class TestCorpusGrewByBothDocuments(unittest.TestCase):

    def test_record_count(self):
        L = _load("lrk_k16_all", "scripts/load_rules_kb.py")
        with redirect_stdout(io.StringIO()):
            recs, _ = L.load_records()
        by = {}
        for r in recs:
            by[r["doc_type"]] = by.get(r["doc_type"], 0) + 1
        self.assertGreaterEqual(by.get("polozhenie49_tpp", 0), 60)
        self.assertGreaterEqual(by.get("gisp_faq", 0), 8)
        self.assertGreaterEqual(len(recs), 745,
                                "корпус меньше ожидаемого — документ выпал из манифеста")


if __name__ == "__main__":
    unittest.main()
