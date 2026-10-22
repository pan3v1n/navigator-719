"""Актуализация корпуса под редакцию ПП №719, действующую с 31.10.2026 (N 1297 + N 1257, `A7` #154).

N 1257 (от 29.09.2026) добавила в раздел XXIII позицию «Иглы швейные из черных металлов»
(25.93.18.110, 15 операций, не менее 115 баллов) и строку примечания с её порогом. Текст — экспорт
Контура 508574, сверенный с HTML той же редакции. Каждый тест проверен мутацией: правка отменяется —
названный тест краснеет.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from app.rag import edition, thresholds  # noqa: E402

KB = ROOT / "knowledge_base" / "pp719"
NEEDLES = "Иглы швейные из черных металлов"


def _xxiii() -> dict[str, dict]:
    recs = json.loads((KB / "structured" / "XXIII_metallurgiya.json").read_text(encoding="utf-8"))
    return {r["source_anchor"].rsplit(" ", 1)[1]: r for r in recs if not r.get("record_type")}


class TestCorpusEdition(unittest.TestCase):
    def setUp(self):
        edition.corpus_edition.cache_clear()
        edition.edition_key.cache_clear()

    def test_corpus_is_on_the_edition_in_force(self):
        """Подпись — по дате последнего акта, как у Контура и правовых баз; документ Контура — 508574."""
        self.assertEqual(edition.corpus_edition(), "ред. от 05.10.2026 N 1297")
        self.assertEqual(edition.edition_key(), "ред. от 05.10.2026 N 1297 + N 1257")
        self.assertTrue(edition.kontur_719_url().endswith("documentId=508574"))

    def test_n1257_is_in_both_header_lists(self):
        body = (KB / "chunks" / "01_postanovlenie.txt").read_text(encoding="utf-8")
        # Перечни актов постановления и приложения (третья строка «(в ред. Постановлений …)» —
        # поправка строки таблицы, N 894).
        lists = [ln for ln in body.split("\n") if ln.startswith("(в ред. Постановлений") and "N 1297" in ln]
        self.assertEqual(len(lists), 2)
        for ln in lists:
            self.assertRegex(ln, r"от 29\.09\.2026 N 1257,[\s\xa0]+от 05\.10\.2026 N 1297")

    def test_later_in_force_act_does_not_rename_an_edition_without_it(self):
        """Ключ меняет только акт, которого нет в тексте прошлой редакции: на тексте без N 1257 ключ
        равен подписи и ведёт на 508767."""
        text = (KB / "chunks" / "01_postanovlenie.txt").read_text(encoding="utf-8")
        old = re.sub(r"[\s\xa0]от 29\.09\.2026 N 1257,", "", text)
        self.assertNotEqual(old, text)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "01_postanovlenie.txt"
            path.write_text(old, encoding="utf-8")
            orig = edition._BODY
            try:
                edition._BODY = path
                edition.corpus_edition.cache_clear()
                edition.edition_key.cache_clear()
                self.assertEqual(edition.edition_key(), "ред. от 05.10.2026 N 1297")
                self.assertTrue(edition.kontur_719_url().endswith("documentId=508767"))
            finally:
                edition._BODY = orig
                edition.corpus_edition.cache_clear()
                edition.edition_key.cache_clear()


    def test_next_edition_key_does_not_inherit_n1257(self):
        """Ревью `A7`: N 1257 останется в тексте навсегда; акт следующей редакции (условно N 1400) даёт
        ключ по своей подписи, без «+ N 1257» — строку в таблицу добавляют по подписи."""
        text = (KB / "chunks" / "01_postanovlenie.txt").read_text(encoding="utf-8")
        newer = text.replace("от 05.10.2026 N 1297", "от 05.10.2026 N 1297, от 10.11.2026 N 1400")
        self.assertNotEqual(newer, text)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "01_postanovlenie.txt"
            path.write_text(newer, encoding="utf-8")
            orig = edition._BODY
            try:
                edition._BODY = path
                edition.corpus_edition.cache_clear()
                edition.edition_key.cache_clear()
                self.assertEqual(edition.edition_key(), "ред. от 10.11.2026 N 1400")
            finally:
                edition._BODY = orig
                edition.corpus_edition.cache_clear()
                edition.edition_key.cache_clear()


class TestNeedlesPosition(unittest.TestCase):
    def test_position_is_parsed_from_the_source_cells(self):
        r = _xxiii()["3"]
        self.assertEqual((r["product_name"], r["okpd2_codes"]), (NEEDLES, ["25.93.18.110"]))
        ops = [o for b in r["requirement_blocks"] for o in b["operations"]]
        points = [o["points"] for o in ops if o["points"] is not None]
        self.assertEqual((len(points), sum(points)), (15, 145))
        # В ячейке первоисточника перед «от» — неразрывный пробел; запись хранит его как есть.
        self.assertEqual([a.replace("\xa0", " ") for a in r["amendments"]],
                         ["(в ред. Постановления Правительства РФ от 29.09.2026 N 1257)"])

    def test_mandatory_requirement_is_a_requirement_not_a_note(self):
        """Модель положила «права на техническую документацию на срок не менее 5 лет» в `notes`, а
        `notes` в контекст модели и в панель «Источник» не попадает — требование было бы невидимо.
        У 618 записей корпуса оно — операция без баллов в блоке требований."""
        r = _xxiii()["3"]
        texts = [o["text"] for b in r["requirement_blocks"] for o in b["operations"] if o["points"] is None]
        self.assertTrue(any("прав на техническую документацию" in t and "не менее 5 лет" in t for t in texts))
        self.assertNotIn("техническую документацию", r.get("notes") or "")

    def test_threshold_is_the_own_115(self):
        r = _xxiii()["3"]
        self.assertEqual(r["min_threshold"], "не менее 115 баллов")

    def test_neighbours_renumbered_not_reparsed(self):
        """Позиция вставлена третьей: прежние 3–10 стали 4–11 без перепарса. Здесь — имена на местах;
        побайтное равенство записей (кроме якоря) сверено при сборке и ревью `A7`."""
        v = _xxiii()
        self.assertEqual(len(v), 11)
        self.assertEqual(v["4"]["product_name"], "Магниты редкоземельные постоянные")
        self.assertEqual(v["11"]["product_name"][:30], "Изделия столовые, кухонные и б")


class TestNote10OwnRule(unittest.TestCase):
    """N 1257 дописала под ступени прим. 10 (магниты) фразу про иглы без своего номера."""

    def test_magnets_keep_their_schedule(self):
        thr = thresholds.lookup_threshold(["25.99.29.110"], "Магниты редкоземельные постоянные", "XXIII")
        self.assertEqual(thr, "до 1 января 2022 г. - 70 баллов; с 1 января 2022 г. - 270 баллов; "
                              "с 1 июля 2023 г. - 900 баллов [прим. 10]")

    def test_the_needles_sentence_is_in_note_10(self):
        """Контроль: фраза действительно стоит сразу под ступенями магнитов — иначе тест выше
        проверял бы пустоту."""
        lines = thresholds._CHUNK.read_text(encoding="utf-8").split("\n")
        k = next(i for i, ln in enumerate(lines) if ln.startswith("с 1 июля 2023 г. - 900 баллов"))
        self.assertIn('25.93.18.110 - "Иглы швейные из черных металлов" - не менее 115 баллов', lines[k + 1])
        self.assertTrue(thresholds._OWN_RULE_RE.search(lines[k + 1]))

    def test_flat_row_steps_stop_at_an_own_rule_sentence(self):
        """Вторая ветка сбора ступеней — многострочная строка «код "имя":» (прим. 9): та же граница."""
        chunk = "\n".join([
            "9. Продукция … при условии достижения следующего количества баллов:",
            '28.11.11.111 "Изделие А":',
            "с 1 января 2026 г. - не менее 50 баллов;",
            "Продукция отрасли может быть отнесена к российской промышленной продукции при условии … "
            '22.22.22.222 - "Изделие Б" - не менее 115 баллов.',
            "",
        ])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "chunk.txt"
            path.write_text(chunk, encoding="utf-8")
            orig = thresholds._CHUNK
            try:
                thresholds._CHUNK = path
                thresholds._flat_thresholds.cache_clear()
                rows = [r for r in thresholds._flat_thresholds() if "Изделие А" in r["names"]]
            finally:
                thresholds._CHUNK = orig
                thresholds._flat_thresholds.cache_clear()
        self.assertEqual(len(rows), 1)
        self.assertIn("50 баллов", rows[0]["threshold"])
        self.assertNotIn("115", rows[0]["threshold"])

    def test_own_rule_sentence_forms(self):
        for s in ("Продукция … может быть отнесена к российской промышленной продукции",
                  "Изделия могут быть отнесены к российской промышленной продукции",
                  "Продукция относится к российской промышленной продукции"):
            self.assertTrue(thresholds._OWN_RULE_RE.search(s), s)
        for s in ("с 1 января 2022 г. - 270 баллов;", "до 1 января 2022 г. - не менее 70 баллов"):
            self.assertIsNone(thresholds._OWN_RULE_RE.search(s), s)


if __name__ == "__main__":
    unittest.main()
