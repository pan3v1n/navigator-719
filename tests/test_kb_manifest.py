r"""Манифест корпуса и паспорт записи — `K8` (issue #39). Офлайн, без Qdrant.

ЗАЧЕМ ЭТОТ ФАЙЛ. До манифеста состав процедурного корпуса жил в трёх местах сразу: списком путей
в загрузчике, значениями `doc_type` в коде и решением «утратившие силу „Правила выдачи заключения“
не индексируем», которое не было записано НИГДЕ — его знал только тот, кто его принял. Проверить
такое решение нечем, а в отзывах июля уже были «ссылается на устаревшие редакции нормативных
документов».

Тесты ниже закрепляют ровно то, ради чего манифест заводился:
  * паспорт есть у КАЖДОЙ записи, а не у документа на бумаге;
  * документ со `status: утратил силу` не попадает в индекс и не может попасть в контекст;
  * заявленная редакция сходится с текстом — иначе отставание корпуса (A6) видно только через
    полгода в жалобе пользователя;
  * пути в манифесте и константы загрузчика не разъезжаются (тот же приём, которым
    `.env.example` держится в согласии с `Settings`).

Запуск:  .venv\Scripts\python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


loader = _load("load_rules_kb_manifest", "scripts/load_rules_kb.py")


class TestManifestItself(unittest.TestCase):
    def setUp(self):
        self.man = loader.load_manifest()

    def test_manifest_is_in_the_repository(self):
        """Файл вне git не переживает сессию — урок скриптов выкатки (`O2`)."""
        self.assertTrue(loader.MANIFEST_PATH.is_file(), "нет knowledge_base/manifest.yaml")

    def test_every_document_has_a_full_passport(self):
        for doc in self.man["documents"]:
            with self.subTest(doc=doc.get("doc_type")):
                for field in loader.PASSPORT_FIELDS:
                    self.assertIn(field, doc)

    def test_values_are_from_the_vocabulary(self):
        """Опечатка в `status` («утратила силу») тихо вернула бы документ в индекс."""
        vocab = self.man["vocabularies"]
        for doc in self.man["documents"]:
            for field, allowed in vocab.items():
                if field in doc:
                    with self.subTest(doc=doc["doc_type"], field=field):
                        self.assertIn(doc[field], allowed)

    def test_sources_exist_on_disk(self):
        """⚠ Источник может быть ШАБЛОНОМ (`pp719/structured/*.json`), поэтому проверяем через
        `resolve_sources`, а не `is_file()`: он же и падает, если шаблон не нашёл ни одного файла —
        то есть «корпус исчез» не проходит молча."""
        from app.core import manifest as kb

        for doc in self.man["documents"]:
            with self.subTest(doc=doc["doc_type"]):
                files = kb.resolve_sources(doc)
                self.assertTrue(all(f.is_file() for f in files))
                if doc.get("sources"):
                    self.assertTrue(files, "источники заданы, а файлов не нашлось")

    def test_every_indexed_document_has_a_parser(self):
        """Документ в манифесте без парсера — молчаливая потеря целого документа корпуса.

        ⚠ Только СВОЯ коллекция: у товарного корпуса и кейсов другие загрузчики и свой формат,
        `PARSERS` про них ничего не знает и знать не должен."""
        for doc in self.man["documents"]:
            if doc.get("collection") != loader.COLLECTION:
                continue
            if doc.get("status") == "действует" and doc.get("sources"):
                with self.subTest(doc=doc["doc_type"]):
                    self.assertIn(doc["doc_type"], loader.PARSERS)

    def test_retired_wording_is_the_same_in_all_three_places(self):
        """⚠ Строка «утратил силу» живёт В ТРЁХ местах: словарь манифеста, `loader.RETIRED` и
        `retriever.RETIRED_STATUS` (фильтр Qdrant). Расхождение НЕВИДИМО: первый контур —
        загрузчик, который такой документ и так не индексирует, — прикроет поломку второго, и
        `must_not` тихо станет пустышкой. Ровно тот случай, о котором предупреждают комментарии
        рядом: предохранитель, который ничего не сообщает, когда сломался."""
        from app.rag import retriever

        self.assertIn(loader.RETIRED, self.man["vocabularies"]["status"])
        self.assertEqual(retriever.RETIRED_STATUS, loader.RETIRED)

    def test_retired_document_is_recorded_with_its_end_date(self):
        """Ради этой записи K8 и заводилась: решение об исключении обязано быть проверяемым."""
        retired = [d for d in self.man["documents"] if d["status"] == loader.RETIRED]
        self.assertTrue(retired, "исключённый документ пропал из манифеста — решение снова нигде")
        for d in retired:
            with self.subTest(doc=d["doc_type"]):
                self.assertTrue(d.get("valid_to"), "утратил силу без даты — это не паспорт")


class TestPassportReachesEveryRecord(unittest.TestCase):
    """Критерий приёмки K8: паспорт заполнен у ВСЕХ пунктов, а не у документов на бумаге."""

    @classmethod
    def setUpClass(cls):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):  # загрузчик разговорчив
            cls.recs, _ = loader.load_records()

    def test_all_records_carry_the_passport(self):
        for r in self.recs:
            missing = [f for f in loader.PASSPORT_FIELDS if f not in r]
            with self.subTest(anchor=r.get("source_anchor")):
                self.assertEqual(missing, [])

    def test_no_retired_record_is_indexed(self):
        """Первый контур: утративший силу документ не доходит до Qdrant вовсе."""
        self.assertEqual([r["source_anchor"] for r in self.recs if r.get("status") == loader.RETIRED],
                         [])

    def test_legal_force_is_a_number_so_hierarchy_can_be_compared(self):
        """`K13` будет сравнивать силу источников; строка «2» сломала бы сравнение молча."""
        for r in self.recs:
            with self.subTest(doc=r["doc_type"]):
                self.assertIsInstance(r["legal_force"], int)

    def test_declared_edition_matches_the_text(self):
        """⚠ ГЕЙТ ОТСТАВАНИЯ КОРПУСА (A6). Загрузчик о расхождении только предупреждает — он не
        вправе решать за человека, что устарело: текст или манифест. Решает этот тест в CI."""
        by_doc = {d["doc_type"]: d for d in loader.load_manifest()["documents"]}
        seen: dict[str, str] = {}
        for r in self.recs:
            seen.setdefault(r["doc_type"], r["edition"])
        for doc_type, edition in seen.items():
            expected = by_doc[doc_type].get("edition_expected")
            if expected:
                with self.subTest(doc=doc_type):
                    self.assertEqual(edition, expected,
                                     "манифест разошёлся с текстом — обновить одно из двух")


class TestManifestAgreesWithTheInterface(unittest.TestCase):
    """Редакция, которую видит пользователь, и редакция в манифесте — об одном документе.

    `app/rag/edition.py` считает редакцию НЕЗАВИСИМО от загрузчика (дубль намеренный: `app/` не
    импортирует из `scripts/`). Пока обе реализации согласны, дубль безвреден; разойдись они —
    интерфейс показывал бы одну редакцию, а корпус нёс другую, и заметить это было бы нечем.
    Манифест даёт третью точку сверки и делает расхождение видимым в CI.
    """

    def test_declared_decree_edition_equals_the_one_shown_to_the_user(self):
        from app.rag import edition

        by_doc = {d["doc_type"]: d for d in loader.load_manifest()["documents"]}
        self.assertEqual(by_doc["decree_body"]["edition_expected"], edition.corpus_edition())


class TestAllThreeCollectionsDescribed(unittest.TestCase):
    """`#106`: манифест описывает ВСЕ корпуса сервиса, а не только процедурный.

    Пока в нём был один `pp719_rules`, «единственный источник правды о составе» звучал шире, чем
    исполнялся: товарный корпус собирался `STRUCT_DIR.glob`, кейсы — `CASES_DIR.glob` с условием
    `startswith("_")` внутри цикла. Ни редакции, ни юридической силы у них не было — то есть
    `A6` нечем было сравнивать по товарному корпусу, а `K13` нечем строить иерархию.
    """

    @classmethod
    def setUpClass(cls):
        from app.core import manifest as kb
        cls.kb = kb
        cls.man = kb.load_manifest()

    def test_scope_and_documents_agree(self):
        declared = set(self.man["scope"])
        actual = {d["collection"] for d in self.man["documents"]}
        self.assertEqual(declared, actual, "scope обещает не то, что описано документами")

    def test_every_collection_has_a_document(self):
        for coll in self.man["scope"]:
            with self.subTest(collection=coll):
                self.assertTrue(self.kb.documents(coll))

    def test_product_corpus_matches_the_loader_directory(self):
        """Тот же приём, что у путей процедурного корпуса: шаблон манифеста и каталог загрузчика
        обязаны давать ОДИН набор файлов, иначе состав снова живёт в двух местах."""
        load_kb = _load("load_kb_manifest", "scripts/load_kb.py")
        doc = self.kb.documents("pp719")[0]
        self.assertEqual(self.kb.resolve_sources(doc), sorted(load_kb.STRUCT_DIR.glob("*.json")))

    def test_cases_exclude_templates(self):
        """Решение «файлы с `_` — шаблоны» переехало из условия в цикле в манифест. Проверяем,
        что оно исполняется: иначе `_example.json` уедет в петлю обучения как настоящий кейс."""
        doc = self.kb.documents("verified_cases")[0]
        files = self.kb.resolve_sources(doc)
        self.assertTrue(files)
        self.assertEqual([f.name for f in files if f.name.startswith("_")], [])
        self.assertIn("_*.json", doc.get("exclude") or [])

    def test_cases_are_practice_not_a_norm(self):
        """⚠ Кейс уходит в контекст ВЫШЕ первоисточника (правило 1а промпта). `K13` обязана
        знать, что он уточняет ПРИМЕНЕНИЕ нормы, а не заменяет её."""
        doc = self.kb.documents("verified_cases")[0]
        self.assertEqual(doc["legal_force"], 5)
        norms = [d["legal_force"] for d in self.man["documents"] if d["collection"] != "verified_cases"]
        self.assertTrue(all(doc["legal_force"] > n for n in norms),
                        "практика обязана быть слабее любой нормы корпуса")

    def test_passport_reaches_product_and_case_records(self):
        """Критерий тот же, что у `K8`: паспорт у ЗАПИСЕЙ, а не у документа на бумаге."""
        import io
        from contextlib import redirect_stdout

        load_kb = _load("load_kb_manifest2", "scripts/load_kb.py")
        seed = _load("seed_cases_manifest", "scripts/seed_cases.py")
        with redirect_stdout(io.StringIO()):
            products = load_kb.load_records()
            cases = seed.load_cases()
        for name, recs in (("pp719", products), ("verified_cases", cases)):
            with self.subTest(collection=name):
                self.assertTrue(recs)
                missing = [f for f in self.kb.PASSPORT_FIELDS if any(f not in r for r in recs)]
                self.assertEqual(missing, [])

    def test_product_edition_equals_the_one_shown_to_the_user(self):
        """`A6`: отставание товарного корпуса от действующей редакции становится видно в CI."""
        from app.rag import edition

        doc = self.kb.documents("pp719")[0]
        self.assertEqual(doc["edition_expected"], edition.corpus_edition())


class TestManifestMatchesLoaderPaths(unittest.TestCase):
    """Константы путей остались ручками для тестов парсеров — но правдой о составе они быть
    перестали. Тест держит их в согласии с манифестом; тот же приём, что у `.env.example`."""

    def setUp(self):
        self.by_doc = {d["doc_type"]: d for d in loader.load_manifest()["documents"]}

    def _sources(self, doc_type: str) -> list[Path]:
        return [ROOT / "knowledge_base" / s for s in self.by_doc[doc_type]["sources"]]

    def test_rules_files(self):
        self.assertEqual(self._sources("rules_registry"),
                         [loader.CHUNKS_DIR / n for n in loader.RULES_FILES])

    def test_single_file_documents(self):
        for doc_type, const in (("decree_body", loader.BODY_PATH),
                                ("tpp_order_52", loader.ORDER52_PATH),
                                ("appendix_footnotes", loader.FOOTNOTES_PATH)):
            with self.subTest(doc=doc_type):
                self.assertEqual(self._sources(doc_type), [const])


class TestRetiredNeverReachesContext(unittest.TestCase):
    """Второй контур: даже если в коллекции лежит запись прежней сборки, в окно она не попадёт.

    ⚠ Проверяется ФОРМА фильтра, а не выдача Qdrant: отсечение делает сервер, и подменённый
    `_hybrid` его бы не исполнил — тест «работает» и не проверяет ничего. Урок `EV16`: тест,
    которому нужен живой сервис, ломает офлайновость батареи, а тест, который его подменяет,
    обязан проверять то, что действительно остаётся в его власти, — переданный фильтр.
    """

    class _Point:
        def __init__(self, payload, score=1.0, id=1):  # noqa: A002
            self.payload, self.score, self.id = payload, score, id

    def setUp(self):
        from app.rag import retriever as r
        self.r = r
        self._orig = (r._client, r._hybrid)
        r._client = lambda: type("C", (), {"collection_exists": lambda self, n: True})()
        self.filters = []

        # ⚠ ПЕРВЫЙ вызов обязан вернуть НЕПУСТУЮ выдачу. Первая редакция теста отдавала пусто,
        # `search_rules` выходил на `if not points: return []` — и «каждый запрос» проверялся
        # ровно на одном из трёх. Тест был зелёным и не проверял добор темы и перечня документов.
        def spy(query, limit, qfilter=None, collection=None, qvec=None):
            self.filters.append(qfilter)
            if len(self.filters) > 1:
                return []
            return [self._Point({"doc_type": "rules_registry", "point": "1",
                                 "text": "пункт", "source_anchor": "Правила, п. 1"})]

        r._hybrid = spy

    def tearDown(self):
        self.r._client, self.r._hybrid = self._orig

    def test_every_query_carries_the_alive_filter(self):
        self.r.search_rules("какие документы нужны для получения акта экспертизы",
                            qvec=[0.1] * 4, primary_docs=("decree_body",))
        self.assertGreater(len(self.filters), 1,
                           "добор темы и перечня не сработал — проверен один запрос из трёх")
        for i, f in enumerate(self.filters):
            with self.subTest(call=i):
                self.assertIsNotNone(f, "запрос ушёл БЕЗ фильтра — утративший силу пункт пройдёт")
                self.assertIn(self.r.RETIRED_STATUS,
                              [c.match.value for c in (f.must_not or [])])

    def test_filter_is_must_not_so_a_stale_index_still_answers(self):
        """⚠ Ключевое решение. `must status=действует` при коде без переиндексации выключил бы
        процедурную ветку ЦЕЛИКОМ: у старых точек поля `status` нет. `must_not` их пропускает."""
        f = self.r.alive_only()
        self.assertTrue(f.must_not)
        self.assertFalse(f.must, "условие «действует» через must — старый индекс исчезнет весь")

    def test_extra_conditions_survive_alongside_the_status(self):
        from qdrant_client import models
        f = self.r.alive_only(models.FieldCondition(key="doc_type",
                                                    match=models.MatchValue(value="tpp_order_52")))
        self.assertEqual([c.key for c in f.must], ["doc_type"])
        self.assertEqual([c.key for c in f.must_not], ["status"])


if __name__ == "__main__":
    unittest.main()
