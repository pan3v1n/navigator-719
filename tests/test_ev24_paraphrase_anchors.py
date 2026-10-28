r"""Кэш перефразов сверяется с корпусом по наименованию позиции — `EV24` (#244).

ЧТО БЫЛО. `scripts/eval_paraphrases.json` ключевался одним `source_anchor` («Раздел N, позиция M»).
Редакции перенумеровали разделы (XVIII изложен заново N 1254: 182 → 339 позиций), кэш не
переводился, и 10 из 156 якорей описывали ЧУЖУЮ позицию: перефразы «Суда-рыбозаводы» мерились против
«Кронблоков для буровых судов». `eval_paraphrase` считал промахом верный ответ и об этом молчал —
метрика перефразов при этом критерий приёмки правок индекса (`K3`, `K18`).

ЧТО СТАЛО. Рядом с якорем хранится наименование позиции, под которое писался перефраз;
`load_cache` сверяет весь кэш с текущим корпусом и при рассинхроне останавливает замер `sys.exit`
с перечнем расхождений. Генерация пишет наименование сама — что записано, то загрузчик и примет.

Тесты офлайн: корпус читается из `knowledge_base/` (`load_records`), Qdrant и ключ не нужны.

Запуск:  .venv\Scripts\python -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import eval_paraphrase as ep  # noqa: E402

FISH = "Приложение к ПП №719, Раздел XVIII, позиция 304"
CROWN = "Приложение к ПП №719, Раздел XVIII, позиция 150"
RECS = [
    {"source_anchor": FISH, "product_name": "Суда-рыбозаводы", "section_roman": "XVIII"},
    {"source_anchor": CROWN, "product_name": "Кронблоки для буровых судов", "section_roman": "XVIII"},
]


def _entry(name: str, *paraphrases: str) -> dict:
    return {"product_name": name, "paraphrases": list(paraphrases) or ["перефраз"]}


class _TmpCache(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "eval_paraphrases.json"

    def write(self, data) -> Path:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return self.path

    def exit_message(self, data) -> str:
        with self.assertRaises(SystemExit) as cm:
            ep.load_cache(RECS, self.write(data))
        return str(cm.exception.code)


class TestCommittedCacheMatchesCorpus(unittest.TestCase):
    """Критерий приёмки #244: ни одного якоря с несовпавшим наименованием — в САМОМ кэше."""

    def test_no_mismatches_against_current_corpus(self):
        from scripts.load_kb import load_records

        with contextlib.redirect_stdout(io.StringIO()):
            records = load_records()
        cache = json.loads(ep.CACHE.read_text(encoding="utf-8"))
        self.assertGreater(len(cache), 100, "кэш пуст — сверять нечего, тест ничего не доказал бы")
        self.assertEqual(ep.cache_mismatches(cache, records), [])


class TestLoaderStopsOnDrift(_TmpCache):
    def test_foreign_name_under_anchor_exits_with_both_names(self):
        # ровно случай из issue: перефразы рыбозаводов остались под якорем кронблоков
        msg = self.exit_message({CROWN: _entry("Суда-рыбозаводы", "Плавучий рыбзавод для промысла")})
        self.assertIn(CROWN, msg)
        self.assertIn("Суда-рыбозаводы", msg)
        self.assertIn("Кронблоки для буровых судов", msg)

    def test_anchor_absent_from_corpus_exits(self):
        gone = "Приложение к ПП №719, Раздел XVIII, позиция 999"
        msg = self.exit_message({gone: _entry("Суда-рыбозаводы")})
        self.assertIn(gone, msg)

    def test_entry_without_name_exits(self):
        # прежний формат: якорь → голый список перефразов, сверять не с чем
        msg = self.exit_message({FISH: ["Плавучий рыбзавод для промысла"]})
        self.assertIn(FISH, msg)

    def test_every_mismatch_is_listed_and_good_ones_are_not(self):
        msg = self.exit_message({
            FISH: _entry("Суда-рыбозаводы"),                       # годна
            CROWN: _entry("Суда научно-исследовательские"),         # чужая
            "Приложение к ПП №719, Раздел IV, позиция 1": _entry("x"),  # якоря нет
        })
        self.assertEqual(msg.count("✗"), 2)
        self.assertNotIn(f"{FISH}:", msg)

    def test_matching_cache_loads_whitespace_insensitive(self):
        data = {FISH: _entry("Суда-рыбозаводы", "Плавучий рыбзавод"),
                CROWN: _entry("Кронблоки  для\nбуровых судов ", "Верхний блок вышки")}
        self.assertEqual(ep.load_cache(RECS, self.write(data)), data)

    def test_missing_file_is_empty_cache(self):
        self.assertEqual(ep.load_cache(RECS, self.path), {})

    def test_main_stops_before_measuring(self):
        # проверка в функции бесполезна, если main её не зовёт: замер должен не начаться
        self.write({CROWN: _entry("Суда-рыбозаводы", "Плавучий рыбзавод для промысла")})
        started = AssertionError("замер пошёл на рассинхронизированном кэше")
        with mock.patch("app.rag.retriever._client"), \
                mock.patch.object(ep, "load_records", return_value=RECS), \
                mock.patch.object(ep, "CACHE", self.path), \
                mock.patch.object(ep, "gen_paraphrases", side_effect=started), \
                mock.patch.object(ep, "evaluate", side_effect=started), \
                mock.patch.object(sys, "argv", ["eval_paraphrase.py", "--no-progress"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                ep.main()
        self.assertIn("Кронблоки для буровых судов", str(cm.exception.code))


class TestGeneratedEntriesCarryName(_TmpCache):
    """Что пишет генерация, то принимает загрузчик: запись без имени уронила бы следующий прогон."""

    def test_new_entry_has_product_name_and_reloads(self):
        reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"paraphrases": ["Плавучий рыбзавод", "Судно с цехом переработки"]},
                               ensure_ascii=False)))])
        client = mock.MagicMock()
        client.chat.completions.create.return_value = reply
        with mock.patch("app.rag.pipeline._client", return_value=client), \
                mock.patch.object(ep, "CACHE", self.path), \
                contextlib.redirect_stdout(io.StringIO()):
            got = ep.gen_paraphrases([RECS[0]], 2, progress=False, cache={})
        self.assertEqual(got, {FISH: ["Плавучий рыбзавод", "Судно с цехом переработки"]})
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written[FISH]["product_name"], "Суда-рыбозаводы")
        self.assertEqual(ep.load_cache(RECS, self.path), written)


if __name__ == "__main__":
    unittest.main()
