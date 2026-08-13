"""Юнит-тесты целостности базы знаний (без Qdrant, сети и DeepSeek).

Покрывают правило чистки записей-заглушек исключённых позиций приложения
(`scripts/drop_excluded_positions.is_excluded_stub`) — обе формы заглушки:
с пустым наименованием и с наименованием, равным заголовку раздела (D4).

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
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


class TestIncompleteThresholdNotice(unittest.TestCase):
    """D9 (страховка): позиции, где в законе несколько порогов, а в записи поместился один.

    Полное исправление — порог на блок + перепарс четырёх разделов. До него дефект делается
    ВИДИМЫМ: показать один порог как единственный опаснее, чем не показать ничего, — ответ
    выглядит полным, а недобор по узлу проходит незамеченным."""

    def test_artifact_lists_found_positions(self):
        from app.rag import fragments

        gaps = fragments._threshold_gaps()
        if not gaps:
            self.skipTest("артефакт incomplete_thresholds.json не сгенерирован")
        self.assertIn(("XXIV", "модульная криогенная автозаправочная станция"), gaps)
        self.assertIn(("XXV", "инструменты музыкальные струнные смычковые"), gaps)

    def test_notice_is_section_scoped(self):
        """Одноимённые позиции живут в разных разделах — пометка не должна уезжать к чужой."""
        from app.rag import fragments

        if not fragments._threshold_gaps():
            self.skipTest("артефакт не сгенерирован")
        self.assertTrue(fragments.has_incomplete_thresholds(
            "XXIV", "Модульная криогенная автозаправочная станция"))
        self.assertFalse(fragments.has_incomplete_thresholds(
            "III", "Модульная криогенная автозаправочная станция"))
        self.assertFalse(fragments.has_incomplete_thresholds("III", "Автокраны"))

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
