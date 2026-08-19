"""Юнит-тесты целостности базы знаний (без Qdrant, сети и DeepSeek).

Покрывают правило чистки записей-заглушек исключённых позиций приложения
(`scripts/drop_excluded_positions.is_excluded_stub`) — обе формы заглушки:
с пустым наименованием и с наименованием, равным заголовку раздела (D4).

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.drop_excluded_positions import is_excluded_stub  # noqa: E402

SECTION = "Продукция радиоэлектроники <5>"


def rec(**kw) -> dict:
    """Запись structured/*.json с разумными умолчаниями."""
    base = {
        "section_roman": "IX",
        "section_title": SECTION,
        "product_name": "Планшетные компьютеры",
        "okpd2_codes": ["26.20.11.130"],
        "requirement_blocks": [],
        "min_threshold": None,
    }
    base.update(kw)
    return base


class TestDenseEmbeddingText(unittest.TestCase):
    """EV5 (#82): порог не должен попадать в текст DENSE-вектора.

    Он выглядел частью идентичности, но идентичностью не является: «до 31 декабря 2023 г. -
    не менее 90 баллов; с 1 января 2024 г. …» — общий бойлерплейт сотен позиций, и у 139 записей
    из 236 он ДЛИННЕЕ самого наименования. Из-за него запрос-пустышка «сколько баллов нужно для
    производства» — без единого товара — давал «Конвейеры скребковые» с косинусом 0.858, выше,
    чем целевая позиция получает на своём продукте. Вернуть порог в вектор — значит вернуть
    притяжение любого вопроса про баллы к коротким записям с порогом."""

    THRESHOLD = ("до 31 декабря 2023 г. - не менее 90 баллов; "
                 "с 1 января 2024 г. - не менее 120 баллов")

    def _text(self):
        from scripts.load_kb import build_embedding_text
        return build_embedding_text(rec(product_name="Комбайны проходческие",
                                        min_threshold=self.THRESHOLD))

    def test_threshold_is_absent(self):
        text = self._text()
        self.assertNotIn("баллов", text, "порог вернулся в вектор — EV5 воспроизведётся")
        self.assertNotIn("2024", text)

    def test_identity_is_intact(self):
        """Убрали только порог: имя, раздел и коды — по-прежнему в векторе."""
        text = self._text()
        self.assertIn("Комбайны проходческие", text)
        self.assertIn("IX", text)
        self.assertIn("26.20.11.130", text)

    def test_sparse_still_sees_full_record(self):
        """Асимметрия R9 не тронута: BM25 индексирует полный текст, включая порог."""
        from scripts.load_kb import build_text
        self.assertIn("баллов", build_text(rec(min_threshold=self.THRESHOLD)))


class TestExcludedStub(unittest.TestCase):
    def test_empty_name_is_stub(self):
        """Прежняя форма: у строки-кода нет ни имени, ни требований."""
        self.assertTrue(is_excluded_stub(rec(product_name="")))

    def test_name_equals_section_title_is_stub(self):
        """Форма D4: LLM подставила в product_name заголовок раздела."""
        self.assertTrue(is_excluded_stub(rec(product_name=SECTION)))

    def test_name_equals_section_title_without_footnote_is_stub(self):
        """Маркер сноски в заголовке не должен мешать сопоставлению."""
        self.assertTrue(is_excluded_stub(rec(product_name="Продукция радиоэлектроники")))

    def test_real_product_is_kept(self):
        self.assertFalse(is_excluded_stub(rec()))

    def test_section_named_record_with_requirements_is_kept(self):
        """Совпадение имени с разделом само по себе не приговор: есть требования — оставляем."""
        blocks = [{"component": "Сборка", "operations": [{"text": "сборка", "points": 10}]}]
        self.assertFalse(is_excluded_stub(rec(product_name=SECTION, requirement_blocks=blocks)))

    def test_section_named_record_with_threshold_is_kept(self):
        self.assertFalse(is_excluded_stub(rec(product_name=SECTION, min_threshold="не менее 50 баллов")))

    def test_methodology_record_is_never_touched(self):
        """Методичка раздела (section_methodology) не продукт и под правило не подпадает."""
        self.assertFalse(
            is_excluded_stub(rec(product_name="", record_type="section_methodology"))
        )

    def test_empty_section_title_does_not_match_empty_name_twice(self):
        """Пустой заголовок не должен превращать любой продукт в заглушку."""
        self.assertFalse(is_excluded_stub(rec(section_title="", product_name="Ярусники")))


class TestCorpusHasNoExcludedStubs(unittest.TestCase):
    """Регресс на боевых данных: в корпусе не должно остаться ни одной заглушки."""

    def test_structured_corpus_is_clean(self):
        import json

        struct = ROOT / "knowledge_base" / "pp719" / "structured"
        if not struct.exists():  # на CI без данных тест не имеет смысла
            self.skipTest("structured/ отсутствует")
        offenders: list[str] = []
        for jf in sorted(struct.glob("*.json")):
            for i, r in enumerate(json.loads(jf.read_text(encoding="utf-8"))):
                if is_excluded_stub(r):
                    offenders.append(f"{jf.name}[{i}] {r.get('okpd2_codes')}")
        self.assertEqual(offenders, [], f"остались записи-заглушки: {offenders}")


class TestAppendixFootnotes(unittest.TestCase):
    """D3: определения сносок приложения режутся отдельным чанком и попадают в корпус."""

    def test_footnotes_chunk_exists_and_is_separate(self):
        chunks = ROOT / "knowledge_base" / "pp719" / "chunks"
        if not chunks.exists():
            self.skipTest("chunks/ отсутствует")
        foot = chunks / "131_SNOSKI_prilozheniya.txt"
        self.assertTrue(foot.exists(), "нет чанка сносок — пересобрать rechunk_appendix.py --write")
        xxix = next(iter(chunks.glob("*XXIX*.txt")), None)
        self.assertIsNotNone(xxix, "нет чанка XXIX")
        body = xxix.read_text(encoding="utf-8")
        self.assertNotIn(
            "<1> Комплектующие изделия, произведенные на территории стран",
            body,
            "определения сносок снова прилипли к разделу XXIX",
        )

    def test_footnote_44_is_parsed_with_current_edition(self):
        """<44> ужесточена ред. N 923: требование засчитывается только при акте экспертизы ТПП."""
        from scripts.load_rules_kb import FOOTNOTES_PATH, parse_footnotes

        if not FOOTNOTES_PATH.exists():
            self.skipTest("чанк сносок отсутствует")
        recs = parse_footnotes(FOOTNOTES_PATH)
        by_point = {r["point"]: r for r in recs}
        self.assertIn("<44>", by_point)
        r = by_point["<44>"]
        self.assertEqual(r["doc_type"], "appendix_footnotes")
        self.assertEqual(r["source_anchor"], "Приложение к ПП №719, сноска <44>")
        self.assertIn("акт", r["text"].lower())

    def test_excluded_footnotes_are_dropped(self):
        """«<7> Сноска исключена» отвечать нечем — в корпус не идёт."""
        from scripts.load_rules_kb import FOOTNOTES_PATH, parse_footnotes

        if not FOOTNOTES_PATH.exists():
            self.skipTest("чанк сносок отсутствует")
        for r in parse_footnotes(FOOTNOTES_PATH):
            self.assertNotRegex(r["text"], r"^<\d+>\s*[Сс]носка исключена")

    def test_footnotes_do_not_reserve_a_slot_in_every_answer(self):
        """Сноски — документ «по запросу»: без темы они не занимают место в окне процедурного ответа.

        Иначе на КАЖДОМ процедурном вопросе одно из шести мест уходило бы определению сноски,
        вытесняя норму — ровно тот дефект, который чинила K10 для Приказа №52."""
        from app.rag.retriever import RULES_QUOTA_ON_DEMAND

        self.assertIn("appendix_footnotes", RULES_QUOTA_ON_DEMAND)


class TestFragmentedListResolvesToCorpus(unittest.TestCase):
    """Каждая позиция списка расколотых ячеек обязана НАХОДИТЬСЯ в корпусе по ключу.

    ⚠ Молчаливый отказ уже случался: одна из 15 позиций хранит квалификатор второй строкой
    («Светодиоды, включая … кристаллов\n(за исключением белого диапазона)»), а в корпусе то же
    наименование лежит одной строкой. Ключ по первой строке её не находил — позиция теряла пометку
    неполноты `R29` (ровно то, ради чего список заведён), а `group_of` не видел её сиблингов, то
    есть `EV6` на этой группе не работал. По списку это не видно никак: файл валиден, тесты зелены.
    Тот же класс, что «висячая ; в ключе наследования» — 47 молчаливых отказов R6."""

    def test_every_listed_position_is_found_in_corpus(self):
        import json

        from app.rag.fragments import _keys
        path = ROOT / "knowledge_base" / "pp719" / "fragmented_requirements.json"
        struct = ROOT / "knowledge_base" / "pp719" / "structured"
        if not path.exists() or not struct.exists():
            self.skipTest("файлы корпуса отсутствуют")
        corpus = set()
        for p in sorted(struct.glob("*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            recs = d if isinstance(d, list) else (d.get("positions") or d.get("records") or [])
            for r in recs:
                corpus |= set(_keys(r.get("product_name")))
        groups = json.loads(path.read_text(encoding="utf-8"))
        listed = [p.get("product_name") for g in groups for p in (g.get("positions") or [])]
        self.assertTrue(listed, "список расколотых ячеек пуст")
        missing = [n for n in listed if not (set(_keys(n)) & corpus)]
        self.assertEqual(missing, [], f"позиции списка не найдены в корпусе: {missing}")


class TestFragmentedListHasRoles(unittest.TestCase):
    """У каждой позиции списка расколотых ячеек проставлена РОЛЬ — иначе рантайм ослепнет.

    Роль (`qualifier` / `product`) проставляет генератор списка, эксперт видит её в JSON и правит,
    а рантайм только читает. Без поля `role` строка-квалификатор снова стала бы опорой ответа."""

    def test_every_position_has_a_role(self):
        import json
        path = ROOT / "knowledge_base" / "pp719" / "fragmented_requirements.json"
        if not path.exists():
            self.skipTest("файл списка отсутствует")
        groups = json.loads(path.read_text(encoding="utf-8"))
        positions = [p for g in groups for p in (g.get("positions") or [])]
        self.assertTrue(positions, "список расколотых ячеек пуст")
        missing = [p.get("product_name") for p in positions if p.get("role") not in ("qualifier", "product")]
        self.assertEqual(missing, [], f"позиции без роли: {missing}")
        # обе роли реально встречаются — иначе поле бессмысленно
        roles = {p["role"] for p in positions}
        self.assertEqual(roles, {"qualifier", "product"})


class TestEditionWatcher(unittest.TestCase):
    """`A6` #60: слежение за редакциями. Проверяем на ТОМ САМОМ дефекте, ради которого заведено.

    12.08.2026 корпус отстал на ДВЕ редакции (N 899 от 16.07 и N 923 от 22.07), причём тело
    постановления оказалось СИЛЬНЕЕ приложения, и заметили это вручную при сверке с
    КонсультантПлюс. Тест воспроизводит обе формы расхождения — отставание от первоисточника и
    рассинхрон частей корпуса между собой."""

    def _watcher(self):
        from scripts import watch_edition
        return watch_edition

    def test_lag_behind_the_source_is_found(self):
        """Форма дефекта 12.08: в первоисточнике есть акты, которых нет в корпусе."""
        w = self._watcher()
        corpus = w.acts("(в ред. Постановлений Правительства РФ от 27.06.2026 N 794)")
        source = ("(в ред. Постановлений Правительства РФ от 27.06.2026 N 794, "
                  "от 16.07.2026 N 899, от 22.07.2026 N 923)")
        res = w.check_source(source, corpus)
        self.assertEqual(res["нет в корпусе"], ["от 16.07.2026 N 899", "от 22.07.2026 N 923"])
        self.assertEqual(res["последний в первоисточнике"], "от 22.07.2026 N 923")

    def test_no_false_alarm_when_corpus_is_current(self):
        """Ноль ложных тревог важнее полноты: отчёт, который «всегда что-то нашёл», не читают.

        ⚠ Первая версия сравнивала `acts(text)` с `acts(text)` — тавтология, зелёная даже при
        `acts()`, возвращающем пустоту (ревью PR #94). Теперь корпус описан ОТДЕЛЬНОЙ строкой и
        заведомо шире первоисточника, а непустота множеств проверяется явно."""
        w = self._watcher()
        source = "(в ред. от 27.06.2026 N 794, от 22.07.2026 N 923)"
        corpus = w.acts("(в ред. от 27.06.2026 N 794, от 22.07.2026 N 923, от 05.08.2026 N 1001)")
        self.assertEqual(len(corpus), 3, "разбор корпуса сломан — тест перестал что-либо мерить")
        res = w.check_source(source, corpus)
        self.assertEqual(res["актов в первоисточнике"], 2)
        self.assertEqual(res["нет в корпусе"], [])
        self.assertEqual(res["есть только в корпусе"], ["от 05.08.2026 N 1001"])

    def test_parts_of_the_corpus_must_agree(self):
        """Вторая форма: одну часть корпуса актуализировали, другую забыли."""
        import tempfile
        from pathlib import Path
        w = self._watcher()
        with tempfile.TemporaryDirectory() as d:
            body = Path(d) / "body.txt"
            appx = Path(d) / "appendix.txt"
            body.write_text("(в ред. от 22.07.2026 N 923)", encoding="utf-8")
            appx.write_text("(в ред. от 27.06.2026 N 794)", encoding="utf-8")
            with mock.patch.dict(w.PARTS, {"тело": body, "приложение": appx}, clear=True):
                res = w.check_corpus()
        self.assertFalse(res["части согласованы"], "рассинхрон частей корпуса не замечен")

    def test_sections_lagging_the_body_are_detected(self):
        """ФОРМА ДЕФЕКТА 12.08: тело актуализировали, разделы приложения — нет.

        ⚠ Первая версия сравнивала тело с полным текстом приложения, а тот открывается той же
        шапкой со всеми актами: проверка не могла провалиться (ревью PR #94)."""
        import tempfile
        from pathlib import Path
        w = self._watcher()
        with tempfile.TemporaryDirectory() as d:
            body = Path(d) / "01_postanovlenie.txt"
            sec = Path(d) / "02_I_razdel.txt"
            body.write_text("(в ред. от 27.06.2026 N 794, от 22.07.2026 N 923)", encoding="utf-8")
            sec.write_text("(в ред. от 27.06.2026 N 794)", encoding="utf-8")
            with mock.patch.dict(w.PARTS, {"тело постановления": body}, clear=True), \
                 mock.patch.object(w, "section_files", lambda: [sec]):
                res = w.check_corpus()
        self.assertFalse(res["части согласованы"], res["части"])

    def test_unreadable_part_is_not_agreement(self):
        """Нечитаемая часть = сравнение НЕ состоялось, а не «согласовано»."""
        from pathlib import Path
        w = self._watcher()
        with mock.patch.dict(w.PARTS, {"тело": Path("D:/нет-такого-файла.txt")}, clear=True), \
             mock.patch.object(w, "section_files", lambda: []):
            res = w.check_corpus()
        self.assertTrue(res["нечитаемые части"])
        self.assertFalse(res["части согласованы"])

    def test_unparsable_source_is_a_refusal_not_an_all_clear(self):
        """Ноль разобранных актов — отказ проверки. И «№» разбирается наравне с «N».

        ⚠ Экспорты правовых систем пишут «№ 923», а первая регулярка принимала только латинскую
        «N»: источник разбирался в ноль актов, скрипт печатал «корпус не отстаёт» и выходил с
        кодом 0 — ложное «всё чисто» на той самой проверке, ради которой существует."""
        w = self._watcher()
        corpus = w.acts("(в ред. от 27.06.2026 N 794)")
        blind = w.check_source("здесь нет ни одного акта", corpus)
        self.assertFalse(blind["разбор удался"])
        self.assertEqual(blind["нет в корпусе"], [])   # пусто, но это НЕ значит «не отстаём»
        cyr = w.check_source("(в ред. от 27.06.2026 № 794, от 05.08.2026 № 1001)", corpus)
        self.assertTrue(cyr["разбор удался"])
        self.assertEqual(cyr["нет в корпусе"], ["от 05.08.2026 N 1001"])

    def test_real_corpus_parts_agree_and_link_is_known(self):
        """Живая проверка состояния: части корпуса согласованы, ссылка на редакцию известна.

        ⚠ Тест намеренно завязан на реальные файлы: он краснеет ровно тогда, когда корпус
        актуализировали наполовину или забыли добавить `documentId` новой редакции, — то есть
        ведёт себя как `TestKonturLinks`, только со стороны данных."""
        w = self._watcher()
        res = w.check_corpus()
        self.assertNotEqual(res["редакция корпуса"], "редакция не определена")
        self.assertTrue(res["части согласованы"], res["части"])
        self.assertTrue(res["ссылка на первоисточник известна"], res["редакция корпуса"])


class TestIncompleteThresholdNotice(unittest.TestCase):
    """D9: позиции, где в законе несколько порогов, а в записи помещался один.

    ⚠ **Сама D9 закрыта 14.08.2026**: порог получил место в схеме блока
    (`requirement_blocks[].min_threshold`), и `backfill_block_thresholds.py` вернул пороги узлов
    и вторых шкал из первоисточника — список стал пустым (это проверяет
    `TestBlockThreshold.test_corpus_has_no_positions_with_lost_thresholds`).

    Механизм пометки оставлен СЕТКОЙ: новая редакция приложения может снова принести позицию с
    несколькими порогами, и тогда дефект обязан стать видимым, а не молчаливым. Поэтому тесты
    ниже проверяют механизм на синтетическом артефакте, а не на живом корпусе: привязка к
    содержимому корпуса превращала их в «зелёные молча» ровно в тот день, когда список опустел."""

    def _with_artifact(self, positions):
        """Подсовывает модулю синтетический артефакт и возвращает контекст-менеджер."""
        import contextlib
        import tempfile
        import unittest.mock as mock

        from app.rag import fragments

        @contextlib.contextmanager
        def ctx():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "incomplete_thresholds.json"
                path.write_text(json.dumps({"positions": positions}, ensure_ascii=False),
                                encoding="utf-8")
                with mock.patch.object(fragments, "_GAPS_PATH", path):
                    fragments._threshold_gaps.cache_clear()
                    try:
                        yield fragments
                    finally:
                        fragments._threshold_gaps.cache_clear()
        return ctx()

    def test_notice_follows_the_artifact(self):
        """Позиция из артефакта помечается, соседняя — нет."""
        with self._with_artifact([
            {"section": "XXIV", "product_name": "Модульная криогенная автозаправочная станция"},
        ]) as fragments:
            self.assertTrue(fragments.has_incomplete_thresholds(
                "XXIV", "Модульная криогенная автозаправочная станция"))
            self.assertFalse(fragments.has_incomplete_thresholds("XXIV", "Автокраны"))

    def test_notice_is_section_scoped(self):
        """Одноимённые позиции живут в разных разделах — пометка не должна уезжать к чужой."""
        with self._with_artifact([
            {"section": "XXIV", "product_name": "Модульная криогенная автозаправочная станция"},
        ]) as fragments:
            self.assertTrue(fragments.has_incomplete_thresholds(
                "XXIV", "Модульная криогенная автозаправочная станция"))
            self.assertFalse(fragments.has_incomplete_thresholds(
                "III", "Модульная криогенная автозаправочная станция"))

    def test_notice_forbids_threshold_conclusion(self):
        """Текст пометки обязан запрещать вывод «порог набирается» — иначе она бесполезна."""
        from app.rag import fragments

        self.assertIn("НЕ полностью".lower(), fragments.THRESHOLD_NOTICE.lower())
        self.assertIn("порог набирается", fragments.THRESHOLD_NOTICE)

    def test_missing_artifact_does_not_break_answer(self):
        """Файла нет → пометка просто не ставится (как у R29), а не падение ответа."""
        import unittest.mock as mock

        from app.rag import fragments

        with mock.patch.object(fragments, "_GAPS_PATH", Path("нет-такого-файла.json")):
            fragments._threshold_gaps.cache_clear()
            try:
                self.assertFalse(fragments.has_incomplete_thresholds("XXIV", "Модульная криогенная"))
            finally:
                fragments._threshold_gaps.cache_clear()


class TestRulesIndexContext(unittest.TestCase):
    """P2: подпункт индексируется вместе с вводной родителя, иначе не находится по своему вопросу."""

    def _recs(self):
        from scripts.load_rules_kb import ORDER52_PATH, add_index_text, parse_order52

        if not ORDER52_PATH.exists():
            self.skipTest("нет текста Приказа №52")
        recs = [r for r in parse_order52(ORDER52_PATH)
                if not str(r.get("section_roman") or "").startswith("прил")]
        add_index_text(recs)
        return {r["point"]: r for r in recs}

    def test_subpoint_carries_parent_intro(self):
        r = self._recs()["4.2.1"]
        self.assertIn("прилагаются следующие документы", r["parent_intro"])
        self.assertIn("прилагаются следующие документы", r["index_text"])
        self.assertIn("Правоустанавливающие", r["index_text"])

    def test_parent_lookup_stays_inside_its_section(self):
        """В формах приложений нумерация начинается заново: без раздела в ключе п. 4.2 получил бы
        родителем кусок чужой формы («4. Заключение: при изготовлении компонентов…»)."""
        r = self._recs()["4.2"]
        self.assertNotIn("Заключение: при изготовлении компонентов", r.get("parent_intro") or "")
        self.assertNotIn("Заключение: при изготовлении компонентов", r["index_text"])

    def test_long_parent_is_not_glued(self):
        """Длинный пункт — самостоятельная норма, а не заголовок перечня: п. 4.1 (3844 знака)
        не должен приклеиваться к подпунктам, иначе он их утопит."""
        from scripts.load_rules_kb import PARENT_INTRO_CAP

        recs = self._recs()
        for point, r in recs.items():
            intro = r.get("parent_intro") or ""
            self.assertLessEqual(len(intro), PARENT_INTRO_CAP, point)

    def test_section_title_not_glued_into_index_text(self):
        """Замер 13.08: заголовок раздела, приклеенный ко всем 172 пунктам, уравнивает их и роняет
        атрибуцию@1 0.92 → 0.88. В индекс идёт только точечная вводная родителя."""
        r = self._recs()["6.7"]
        self.assertNotIn("Порядок принятия и рассмотрения документов", r["index_text"])


class TestPerRecordPointsCheck(unittest.TestCase):
    """D8: сверка баллов ПО ЗАПИСИ ловит то, к чему сверка по разделу слепа.

    Слой по разделу сравнивает множества раздела, поэтому перенос балла из одной позиции в
    соседнюю для него невидим: множество не меняется. Именно так на перегенерации XVIII
    проехали 13 из 15 потерянных чисел."""

    SECTION = "XXIII"  # маленький раздел (11 записей), на текущем корпусе расхождений не даёт

    def _verify(self):
        from scripts import verify_structured  # ленивый импорт: тянет structure_kb

        return verify_structured

    def test_bracketed_points_form_is_recognised(self):
        """«мотор-генератора 36 (баллов)» — форма самого приложения, а не опечатка."""
        vs = self._verify()
        self.assertEqual(vs._points_in("производство мотор-генератора 36 (баллов);"), {36.0})
        self.assertEqual(vs._points_in("силового генератора (20 баллов)"), {20.0})

    def test_component_text_counts_as_record_points(self):
        """Балл, «переехавший» в component, — на месте, а не потерян (второе слепое пятно)."""
        vs = self._verify()
        rec = {"requirement_blocks": [{"component": "сборка изделия - 45 баллов", "operations": []}]}
        self.assertIn(45.0, vs.record_points(rec))

    def test_points_field_counts_even_without_word(self):
        vs = self._verify()
        rec = {"requirement_blocks": [{"component": "сборка", "operations": [
            {"text": "сварка рамы", "points": 12}]}]}
        self.assertIn(12.0, vs.record_points(rec))

    def test_clean_section_has_no_discrepancies(self):
        """На неиспорченном корпусе ложных срабатываний быть не должно."""
        vs = self._verify()
        if not vs.struct_for(self.SECTION) or not vs.chunk_for(self.SECTION):
            self.skipTest("нет данных раздела")
        checked, bad = vs.check_records(self.SECTION)
        self.assertGreater(checked, 0)
        self.assertEqual(bad, 0, f"ложные срабатывания на чистом разделе {self.SECTION}")

    def test_points_moved_between_records_is_caught(self):
        """Главный критерий: балл убран из одной записи и приписан соседней — обе видны."""
        import json as _json
        import tempfile
        import unittest.mock as mock

        vs = self._verify()
        src = vs.struct_for(self.SECTION)
        if not src:
            self.skipTest("нет данных раздела")
        data = _json.loads(src.read_text(encoding="utf-8"))

        donor = acceptor = None
        for r in data:
            if r.get("record_type") == "section_methodology":
                continue
            ops = [o for b in (r.get("requirement_blocks") or [])
                   for o in (b.get("operations") or []) if o.get("points") is not None]
            if not ops:
                continue
            if donor is None:
                donor, donor_ops = r, ops
            elif acceptor is None:
                acceptor, acceptor_ops = r, ops
                break
        if donor is None or acceptor is None:
            self.skipTest("в разделе нет двух записей с баллами")

        moved = donor_ops[0]["points"]
        donor_ops[0]["points"] = None          # у одной позиции балл исчез
        donor_ops[0]["text"] = "операция без балла"
        acceptor_ops[0]["points"] = moved      # у соседней — появился чужой

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / src.name).write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(vs, "STRUCT", d):
                checked, bad = vs.check_records(self.SECTION)

        self.assertGreater(checked, 0)
        self.assertGreaterEqual(bad, 1, "перенос балла между записями остался незамеченным")


class TestEmbeddingCacheFilename(unittest.TestCase):
    """D7: имя файла кэша эмбеддингов безопасно для файловой системы.

    EMBEDDING_MODEL часто задают абсолютным путём к скачанной модели, и двоеточие диска
    Windows понимал как разделитель NTFS-потока: кэш на диске был нулевого размера,
    а данные — в потоке, который не переживает копирование папки и не виден бэкапу."""

    def _slug(self, model: str) -> str:
        from scripts import load_kb  # ленивый импорт: модуль тянет embeddings

        return load_kb._model_slug(model)

    def test_windows_path_model_gives_plain_filename(self):
        slug = self._slug("D:/navigator-719/models/multilingual-e5-large")
        self.assertNotIn(":", slug)
        self.assertNotIn("/", slug)
        self.assertNotIn("\\", slug)
        self.assertTrue(slug.startswith("multilingual-e5-large_"), slug)

    def test_backslash_path_model_gives_plain_filename(self):
        slug = self._slug(r"D:\navigator-719\models\multilingual-e5-large")
        self.assertNotIn("\\", slug)
        self.assertTrue(slug.startswith("multilingual-e5-large_"), slug)

    def test_hf_repo_model_keeps_readable_name(self):
        self.assertTrue(
            self._slug("intfloat/multilingual-e5-large").startswith("multilingual-e5-large_")
        )

    def test_same_folder_name_different_source_is_different_cache(self):
        """Локальная папка и репозиторий HF — разные модели; общий кэш дал бы чужие векторы."""
        self.assertNotEqual(
            self._slug("intfloat/multilingual-e5-large"),
            self._slug("D:/navigator-719/models/multilingual-e5-large"),
        )

    def test_cache_file_lands_in_cache_dir(self):
        from scripts import load_kb

        f = load_kb._cache_file()
        self.assertEqual(f.parent, load_kb.CACHE_DIR)
        self.assertEqual(f.suffix, ".npz")
        self.assertNotIn(":", f.name)

    def test_legacy_cache_is_migrated_without_recompute(self):
        """Перенос старого кэша обязателен: иначе первый прогон после D7 считает e5 заново."""
        import tempfile
        import unittest.mock as mock

        import numpy as np

        from scripts import load_kb

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            model = "intfloat/multilingual-e5-large"  # без двоеточия — тест кроссплатформенный
            keys = np.array(["a" * 40, "b" * 40], dtype=object)
            vecs = np.zeros((2, 4), dtype=np.float32)
            vecs[1, 0] = 1.0

            with mock.patch.object(load_kb, "CACHE_DIR", cache_dir), mock.patch.object(
                load_kb.settings, "EMBEDDING_MODEL", model
            ):
                legacy = load_kb._legacy_cache_file()
                np.savez(legacy, keys=keys, vecs=vecs)
                new = load_kb._cache_file()
                self.assertNotEqual(new, legacy)

                load_kb._migrate_legacy_cache()

                self.assertTrue(new.exists(), "новый файл кэша не создан")
                self.assertFalse(legacy.exists(), "старый файл кэша не убран")
                with np.load(new, allow_pickle=True) as data:
                    self.assertEqual(list(data["keys"]), list(keys))
                    self.assertTrue(np.array_equal(data["vecs"], vecs))

                load_kb._migrate_legacy_cache()  # идемпотентность: второй вызов ничего не портит
                self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
