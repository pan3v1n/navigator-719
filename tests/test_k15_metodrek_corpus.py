"""`K15` #36 — Методрекомендации ТПП в корпусе: паспорт, версия, исключение устаревшего.

⚠⚠ ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ В ПЕРВУЮ ОЧЕРЕДЬ. Документ помечен ДЕЙСТВУЮЩИМ, но два его фрагмента
ведут заявителя за «заключением Минпромторга», а «Правила выдачи заключения о подтверждении
производства» утратили силу 29.06.2024 (ПП РФ N 894). Проверено по корпусу 25.08.2026: такого шага
в действующих Правилах нет вовсе. Решение владельца — индексировать, исключив устаревшее.

Значит, тесты обязаны стеречь ОБЕ стороны: устаревшее не доехало, полезное не потерялось. Одной
половины мало — правило, вырезающее слишком много, прошло бы проверку «устаревшего нет».
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from app.core import manifest as kb_manifest

ROOT = Path(__file__).resolve().parents[1]
CORPUS_TEXT = ROOT / "knowledge_base" / "pp719" / "metodrek_tpp_full.txt"


def _loader():
    """Загрузчик процедурного корпуса как модуль (у него нет пакета — он в scripts/)."""
    spec = importlib.util.spec_from_file_location("lrk", ROOT / "scripts" / "load_rules_kb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doc() -> dict:
    return next(d for d in kb_manifest.load_manifest()["documents"]
                if d["doc_type"] == "metodrek_tpp")


class TestPassport(unittest.TestCase):
    def test_document_is_a_clarification_not_a_norm(self):
        """`legal_force: 4` — разъяснение. Оно помогает применять норму, но не создаёт обязанностей.

        Это же значение `K13` печатает в контекст, и правило 1а промпта не даст разъяснению
        перебить Правила (сила 2) или постановление (сила 1)."""
        d = _doc()
        self.assertEqual(d["legal_force"], 4)
        self.assertEqual(d["authority"], "ТПП РФ")
        self.assertEqual(d["status"], kb_manifest.ACTIVE)
        self.assertEqual(kb_manifest.LEGAL_FORCE_NAMES[4], "разъяснение, не норма")

    def test_version_is_detected_as_edition(self):
        """У методрекомендаций нет пометок «(в ред. …)» — у них ВЕРСИЯ.

        Без ветки версии в `detect_edition` штамп был бы «редакция не определена в тексте», и
        `A6` (отставание корпуса от действующей редакции) на этом документе не работал бы никогда."""
        m = _loader()
        text = CORPUS_TEXT.read_text(encoding="utf-8")
        self.assertEqual(m.detect_edition(text), "Версия 1.18.3")
        self.assertEqual(_doc()["edition_expected"], "Версия 1.18.3")

    def test_radius_editions_of_older_documents_unchanged(self):
        """⚠ Ветка версии добавлена в ОБЩУЮ функцию — радиус обязан быть измерен, а не предположен.

        Приоритет у пометок «в ред.»: документ, который их несёт, штампуется ими, даже если слово
        «версия» встретится в его тексте."""
        m = _loader()
        cases = {
            "knowledge_base/pp719/chunks/01_postanovlenie.txt": "ред. от 22.07.2026 N 923",
            "knowledge_base/pp719/prikaz52_tpp_full.txt": "ред. от 29.10.2025 N 70",
        }
        for rel, expected in cases.items():
            got = m.detect_edition((ROOT / rel).read_text(encoding="utf-8"))
            self.assertEqual(got, expected, f"{rel}: штамп редакции изменился")


class TestObsoleteFragmentsExcluded(unittest.TestCase):
    """Обе стороны сразу: устаревшее вырезано И полезное осталось."""

    @classmethod
    def setUpClass(cls):
        m = _loader()
        cls.mod = m
        cls.records = [r for r in m.load_records()[0] if r["doc_type"] == "metodrek_tpp"]
        cls.all_text = "\n".join(r["text"] for r in cls.records)

    def test_obsolete_step_did_not_reach_the_index(self):
        for phrase in ("Получите заключение Минпромторга России",
                       "Заключение выдается на основании одного из двух",
                       "наличие заключения Министерства промышленности и торговли"):
            self.assertNotIn(phrase, self.all_text,
                             f"отменённый шаг доехал до индекса: {phrase!r}")

    def test_useful_content_survived(self):
        """⚠ Вторая половина. Правило, вырезающее слишком много, прошло бы проверку выше."""
        # алгоритм поиска по коду ОКПД — его в корпусе больше нигде нет
        self.assertIn("12.34.56.789", self.all_text)
        # раздел 4: документы к заявлению, со ссылкой на раздел 4 Приказа №52
        self.assertIn("N 52", self.all_text)
        # выбор документа по наличию продукции в приложении
        self.assertIn("СТ-1", self.all_text)
        self.assertIn("акт экспертизы", self.all_text.lower())

    def test_all_six_sections_present(self):
        got = {r["section_roman"] for r in self.records}
        self.assertEqual(got, {"1", "2", "3", "4", "5", "6"},
                         "раздел потерялся — проверь разбор заголовков")

    def test_records_fit_the_context_cap(self):
        """Запись длиннее `RULES_TEXT_CAP` доедет до модели УСЕЧЁННОЙ — режем с запасом."""
        from app.rag.pipeline import RULES_TEXT_CAP
        longest = max(len(r["text"]) for r in self.records)
        self.assertLessEqual(longest, RULES_TEXT_CAP,
                             f"самая длинная запись {longest} символов при капе {RULES_TEXT_CAP}")

    def test_anchor_names_the_document_and_section(self):
        """Якорь печатается пользователю как источник — он обязан быть узнаваем."""
        for r in self.records:
            self.assertTrue(r["source_anchor"].startswith("Методрекомендации ТПП РФ, раздел "),
                            r["source_anchor"])


class TestExclusionHasAPositiveControl(unittest.TestCase):
    """⚠⚠ Правило, переставшее совпадать, обязано ОСТАНОВИТЬ загрузку, а не промолчать.

    Иначе переиздание текста тихо вернёт отменённую норму в индекс: снаружи «нечего вырезать» и
    «чистка ослепла» неразличимы. Это тот же урок, что у `eval_cases_influence` — у инструмента,
    чьё «нет» является результатом, обязан быть положительный контроль."""

    def test_rule_that_matches_nothing_stops_the_load(self):
        m = _loader()
        doc = {"doc_type": "metodrek_tpp", "exclude_fragments": [
            {"from": "такой строки в тексте нет", "to": "и такой тоже",
             "reason": "проверка предохранителя", "since": "2024-06-29"}]}
        with self.assertRaises(SystemExit) as cm:
            m.cut_fragments("любой текст документа", doc)
        self.assertIn("не совпало", str(cm.exception))

    def test_negative_control_matching_rule_cuts_and_reports(self):
        """Обратная половина: совпавшее правило режет ровно свой отрезок и возвращает его длину."""
        m = _loader()
        doc = {"doc_type": "x", "exclude_fragments": [
            {"from": "НАЧАЛО", "to": "КОНЕЦ", "reason": "тест", "since": "2024-06-29"}]}
        text, cut = m.cut_fragments("до НАЧАЛО середина КОНЕЦ после", doc)
        self.assertEqual(text, "до  после")
        self.assertEqual(cut, len("НАЧАЛО середина КОНЕЦ"))

    def test_document_without_rules_is_untouched(self):
        m = _loader()
        text, cut = m.cut_fragments("текст без правил", {"doc_type": "y"})
        self.assertEqual(text, "текст без правил")
        self.assertEqual(cut, 0)

    def test_manifest_records_the_reason_and_the_date(self):
        """Решение «эти строки не индексируем» обязано быть читаемо тем, кто смотрит состав корпуса.

        Ровно то, ради чего заводился манифест: прежде исключение утративших силу Правил знал
        только тот, кто его принял."""
        for rule in _doc()["exclude_fragments"]:
            self.assertTrue(rule.get("reason"), "правило исключения без причины")
            self.assertEqual(rule.get("since"), "2024-06-29",
                             "дата отмены обязана стоять рядом с правилом")


class TestConverter(unittest.TestCase):
    """Разбор первоисточника обязан воспроизводиться: PDF в репозиторий не едет."""

    def test_converter_exists_and_is_guarded(self):
        src = (ROOT / "scripts" / "convert_metodrek.py").read_text(encoding="utf-8")
        self.assertIn("enable_utf8()", src, "скрипт упадёт на консоли Windows (#107)")
        # положительный контроль чистки — без него дата печати уехала бы в корпус молча
        self.assertIn("служебных элементов печати не найдено ни одного", src)

    def test_print_artifacts_are_not_in_the_corpus(self):
        """Дата печати и адрес агрегатора — не часть нормы; в векторе им делать нечего."""
        text = CORPUS_TEXT.read_text(encoding="utf-8")
        self.assertNotIn("sudact.ru", text)
        self.assertNotIn(", 10:00", text)


class TestWindowBudget(unittest.TestCase):
    """Пятый документ в корпусе не имеет права стоить норме места в окне."""

    def test_metodrek_competes_by_rank_not_by_quota(self):
        """⚠ Без этого правка тихо ухудшила бы КАЖДЫЙ процедурный ответ.

        `RULES_QUOTA_MIN` резервирует место каждому документу, у которого есть кандидаты. Окно —
        шесть мест. Пятый документ забрал бы одно у нормы на любом вопросе — эффект, который `K10`
        однажды уже чинила. У разъяснения (`legal_force: 4`) оснований на гарантию нет вовсе."""
        from app.rag.retriever import RULES_QUOTA_ON_DEMAND
        self.assertIn("metodrek_tpp", RULES_QUOTA_ON_DEMAND)

    def test_guaranteed_seats_still_fit_the_window(self):
        """Считаем ПО МАНИФЕСТУ, а не по константе: следующий документ пересчитает это сам."""
        from app.rag.pipeline import RULES_TOP_K
        from app.rag.retriever import RULES_QUOTA_MIN, RULES_QUOTA_ON_DEMAND
        live = [d for d in kb_manifest.load_manifest()["documents"]
                if d["collection"] == "pp719_rules" and d["status"] == kb_manifest.ACTIVE]
        guaranteed = sum(RULES_QUOTA_MIN for d in live
                         if d["doc_type"] not in RULES_QUOTA_ON_DEMAND)
        self.assertLess(guaranteed, RULES_TOP_K,
                        f"квота съедает {guaranteed} из {RULES_TOP_K} мест — окну нечем "
                        f"ранжировать; следующий документ надо заводить «по запросу»")


class TestNewDocumentGuard(unittest.TestCase):
    """`K11` строилась под ДОБАВЛЕНИЕ документов — и на первом же добавлении обнаружился пробел.

    ⚠⚠ `avgdl` считается по всему корпусу и ЗАПЕКАЕТСЯ в sparse-вектор каждой точки. Пока документ
    уже был в коллекции, частичная загрузка безопасна. Но НОВЫЙ документ меняет среднюю длину для
    ВСЕХ — и в другом масштабе BM25 оказываются не его точки, а точки СОСЕДЕЙ, загруженных раньше.
    Замер на этом самом документе: 95.84 -> 96.72.
    """

    class _Client:
        def __init__(self, count, exists=True, raises=False):
            self._count, self._exists, self._raises = count, exists, raises
            self.upserted = False

        def collection_exists(self, name):
            return self._exists

        def count(self, **kw):
            if self._raises:
                raise RuntimeError("сеть оборвалась")
            return type("R", (), {"count": self._count})()

        def upsert(self, **kw):
            self.upserted = True

        def delete(self, **kw):
            pass

    def _recs(self):
        return [{"doc_type": "new_doc", "point": "1.1", "text": "текст", "index_text": "текст",
                 "source_anchor": "Новый документ, п. 1.1"}]

    def test_premise_is_real_adding_a_document_shifts_avgdl(self):
        """⚠ Положительный контроль ПРЕДПОСЫЛКИ, а не только предохранителя.

        Если бы добавление документа avgdl не двигало, весь этот гард был бы лишним трением.
        Проверяем на настоящем корпусе, а не на выдуманных числах."""
        m = _loader()
        import io
        import sys as _sys
        buf, orig = io.StringIO(), _sys.stdout
        _sys.stdout = buf
        try:
            recs, _ = m.load_records()
        finally:
            _sys.stdout = orig
        without = [r for r in recs if r["doc_type"] != "metodrek_tpp"]
        self.assertNotEqual(round(m.corpus_avgdl(without), 2), round(m.corpus_avgdl(recs), 2),
                            "добавление документа не сдвинуло avgdl — предпосылка гарда неверна")

    def test_new_document_stops_partial_reindex(self):
        m = _loader()
        with self.assertRaises(SystemExit) as cm:
            m.reindex_document(self._Client(count=0), "new_doc", self._recs())
        msg = str(cm.exception)
        self.assertIn("ДОБАВЛЕНИЕ", msg)
        self.assertIn("ПОЛНАЯ загрузка", msg, "сообщение обязано называть выход, а не только беду")

    def test_explicit_override_lets_it_through(self):
        """Предохранитель обязан иметь ОСОЗНАННЫЙ обход: запрет без выхода обходят молча."""
        m = _loader()
        c = self._Client(count=0)
        m.reindex_document(c, "new_doc", self._recs(), allow_new=True)
        self.assertTrue(c.upserted)

    def test_existing_document_is_not_blocked(self):
        """⚠ Отрицательный контроль: обычная переиндексация не должна требовать флага.

        Без этой половины гард, блокирующий ВСЁ, прошёл бы проверку выше."""
        m = _loader()
        c = self._Client(count=42)
        m.reindex_document(c, "new_doc", self._recs())
        self.assertTrue(c.upserted)

    def test_count_failure_does_not_turn_into_a_block(self):
        """Обрыв связи не должен превращать переиндексацию в отказ.

        Иначе оператор, чинящий бой, научится ставить --allow-new-doc всегда — и предохранитель
        выключит себя сам."""
        m = _loader()
        c = self._Client(count=0, raises=True)
        m.reindex_document(c, "new_doc", self._recs())
        self.assertTrue(c.upserted)


if __name__ == "__main__":
    unittest.main()
