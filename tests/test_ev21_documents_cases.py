"""`EV21` #119 — документные кейсы в приёмочном наборе и оракул, который их проверяет.

ЗАЧЕМ. До 26.08.2026 в golden set было **ноль** вопросов про состав документов, при том что это
кластер жалоб №1 июльских отзывов (31 упоминание). Значит `K12`, `K15` и `EV18` стандартным
замером не проверялись вовсе — только адресными живыми прогонами, руками, ровно столько раз,
сколько я вспомнил их сделать. Оба дефекта `K15` так и нашлись: уже ПОСЛЕ выкатки.

⚠⚠ ЧЕГО ЗДЕСЬ ПРИНЦИПИАЛЬНО НЕТ. Ни одна проверка ниже не зовёт `documents_ref.unverified_documents`
— рантайм-гард продукта. Позови оракул предохранитель, и СНЯТИЕ предохранителя сделало бы метрику
зелёной: набор остался бы зелёным при удалённом гарде. Это ровно `O3` #104, и `TestGuardIsNotTheOracle`
проверяет независимость мутацией, а не обещанием.

Всё офлайн: без Qdrant и без DeepSeek.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag.documents_ref import CONFIRMING_DOCUMENTS  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_golden.json"


def _cases() -> list[dict]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


def _docs_cases() -> list[dict]:
    return [c for c in _cases() if c.get("kind") == "documents"]


def _case(case_id: int) -> dict:
    return next(c for c in _cases() if c["id"] == case_id)


# Эталонный «идеальный» ответ на документный вопрос: называет все три документа закрытого перечня
# и ссылается на оба источника. От него ниже отнимают по одной детали — и каждая проверка обязана
# поймать РОВНО свою (мутационный контроль: утверждение, ловящее всё сразу, не ловит ничего).
PERFECT = (
    "Для подтверждения производства готовят акт экспертизы уполномоченной ТПП [1] — он "
    "подтверждает соответствие требованиям приложения. Если продукции в приложении нет, путь "
    "другой: сертификат о происхождении товара формы СТ-1. Результатом становится реестровая "
    "запись, её подтверждает выписка из реестра. Состав приложений к заявке приведён в разделе 4 "
    "Приказа ТПП РФ №52 [2]; порядок описан в Методических рекомендациях ТПП [3]."
)


class TestDocumentsCasesExist(unittest.TestCase):
    """Набор покрывает кластер жалоб №1 — и покрывает ОБА маршрута, а не один."""

    def test_set_has_documents_cases(self):
        self.assertGreaterEqual(len(_docs_cases()), 3,
                                "EV21: кластер жалоб №1 снова остался без кейсов")

    def test_three_classes_from_the_issue_are_present(self):
        """Чистый документный · смешанный товарный+документный · поднимающий несуществующий."""
        cases = _docs_cases()
        self.assertTrue(any(c["expected_section"] is None and not c.get("expect_explanation")
                            for c in cases), "нет чистого документного вопроса")
        self.assertTrue(any(c["expected_section"] for c in cases),
                        "нет СМЕШАННОГО товарного+документного — ради него заводилась K12")
        self.assertTrue(any(c.get("expect_explanation") for c in cases),
                        "нет вопроса, поднимающего несуществующий документ")

    def test_both_routes_are_covered(self):
        """Один и тот же кластер идёт двумя РАЗНЫМИ ветками — обе обязаны быть в наборе.

        Чистый вопрос уходит процедурной веткой, смешанный — товарной. Набор, покрывающий одну,
        оставляет вторую ровно там же, где кластер был до `EV21`: без замера."""
        from app.rag import procedural, topics

        routes = {procedural.is_procedural(c["query"], has_code=False) for c in _docs_cases()}
        self.assertEqual(routes, {True, False},
                         "в наборе кейсы только одной ветки — вторая остаётся непокрытой")
        for c in _docs_cases():
            with self.subTest(id=c["id"]):
                self.assertTrue(c["in_scope"], "документный вопрос — в сфере сервиса, не отказ")

    def test_ids_are_unique(self):
        ids = [c["id"] for c in _cases()]
        self.assertEqual(len(ids), len(set(ids)))


class TestExpectationsComeFromTheClosedList(unittest.TestCase):
    """⚠ Эталон взят из перечня экспертов, а не из вывода сервиса (требование issue #119)."""

    def test_expected_documents_exist_in_the_reference(self):
        names = {d["name"] for d in CONFIRMING_DOCUMENTS}
        for c in _docs_cases():
            for want in c.get("expect_documents") or []:
                with self.subTest(id=c["id"], want=want):
                    self.assertIn(want, names,
                                  "ожидание не из закрытого перечня — эталон разошёлся с продуктом")

    def test_expected_sources_are_real_doc_types(self):
        from app.core.manifest import documents as manifest_documents

        known = {d["doc_type"] for d in manifest_documents("pp719_rules")}
        for c in _docs_cases():
            for src in c.get("expect_sources") or []:
                with self.subTest(id=c["id"], src=src):
                    self.assertIn(src, known, "источник не описан манифестом корпуса")

    def test_positive_control_passes_today(self):
        import eval_documents

        eval_documents.check_documents_reference()   # не должно бросать

    def test_source_recognizers_know_the_label_the_model_sees(self):
        """⚠ Контекст подписывает документ СОКРАЩЕНИЕМ манифеста («Методрекомендации ТПП РФ»),
        а не полным названием. Первая редакция паттерна знала только «методические рекомендации»
        и на живом прогоне объявила «источник не назван» там, где ответ его назвал."""
        import eval_documents

        eval_documents.check_source_recognizers()
        self.assertTrue(eval_documents._SOURCE_RECOGNIZERS["metodrek_tpp"]
                        .search("Методрекомендации ТПП РФ, раздел 4"))
        self.assertTrue(eval_documents._SOURCE_RECOGNIZERS["metodrek_tpp"]
                        .search("Методические рекомендации ТПП"))

    def test_source_control_stops_the_run_on_a_blind_pattern(self):
        """Мутация: паттерн разошёлся с ярлыком корпуса — замер обязан остановиться."""
        import eval_documents

        blind = dict(eval_documents._SOURCE_RECOGNIZERS,
                     metodrek_tpp=__import__("re").compile("не встречается нигде"))
        with unittest.mock.patch.object(eval_documents, "_SOURCE_RECOGNIZERS", blind):
            with self.assertRaises(SystemExit):
                eval_documents.check_source_recognizers()

    def test_positive_control_stops_the_run_when_the_list_changes(self):
        """Мутация: документ переименован в продукте — замер обязан ОСТАНОВИТЬСЯ, а не измерять.

        Без этого «ноль срабатываний» и «инструмент ослеп» снаружи неразличимы."""
        import eval_documents

        renamed = ({"name": "Акт экспертизы ТПП (переименован)", "when": "", "source": ""},
                   *CONFIRMING_DOCUMENTS[1:])
        with unittest.mock.patch.object(eval_documents, "CONFIRMING_DOCUMENTS", renamed):
            with self.assertRaises(SystemExit):
                eval_documents.check_documents_reference()


class TestClosedListOracle(unittest.TestCase):
    """Перечень доехал до ответа — и каждая пропажа ловится ОТДЕЛЬНО."""

    def setUp(self):
        import eval_documents

        self.ev = eval_documents
        self.case = _case(47)

    def test_perfect_answer_names_everything(self):
        row = self.ev.documents_row(self.case, PERFECT)
        self.assertTrue(row["docs_ok"], f"не опознаны: {set(row['want_docs']) - set(row['named_docs'])}")
        self.assertEqual(row["named_sources"], row["want_sources"])
        self.assertTrue(row["explanation_ok"])

    def test_missing_st1_is_caught(self):
        text = PERFECT.replace("сертификат о происхождении товара формы СТ-1", "другой документ")
        row = self.ev.documents_row(self.case, text)
        self.assertFalse(row["docs_ok"])

    def test_missing_act_is_caught(self):
        text = PERFECT.replace("акт экспертизы уполномоченной ТПП", "нужный документ")
        row = self.ev.documents_row(self.case, text)
        self.assertFalse(row["docs_ok"])

    def test_missing_registry_entry_is_caught(self):
        text = PERFECT.replace(
            "Результатом становится реестровая запись, её подтверждает выписка из реестра.",
            "Результат оформляется отдельно.")
        self.assertNotIn("реестр", text.lower(), "деградация текста не удалась — тест мерил бы не то")
        row = self.ev.documents_row(self.case, text)
        self.assertFalse(row["docs_ok"])

    def test_missing_source_is_caught(self):
        text = PERFECT.replace("Приказа ТПП РФ №52", "внутреннего порядка")
        row = self.ev.documents_row(self.case, text)
        self.assertNotIn("tpp_order_52", row["named_sources"])


class TestExplanationIsConditional(unittest.TestCase):
    """`K15` + правка `dbfc622`: разъяснение приезжает, ТОЛЬКО когда про документ спросили."""

    def setUp(self):
        import eval_documents

        self.ev = eval_documents

    def test_term_absent_when_not_asked_is_correct(self):
        row = self.ev.documents_row(_case(47), PERFECT)
        self.assertTrue(row["explanation_ok"])
        self.assertEqual(row["term_mentions"], 0)

    def test_term_leaking_into_an_unasked_answer_is_a_defect(self):
        """Ровно тот дефект, что нашёлся живым прогоном на бою: правка, задуманная чтобы термин
        ИСЧЕЗ из ответов, начала его туда приносить."""
        text = PERFECT + " Документа «заключение ТПП» при этом не существует."
        row = self.ev.documents_row(_case(47), text)
        self.assertFalse(row["explanation_ok"],
                         "термин уехал пользователю, который про него не спрашивал")

    def test_explanation_required_when_asked(self):
        text = ("Документа «заключение ТПП», подтверждающего производство, не существует. "
                "Производство подтверждает акт экспертизы уполномоченной ТПП, а для продукции вне "
                "приложения — сертификат СТ-1.")
        row = self.ev.documents_row(_case(49), text)
        self.assertTrue(row["explanation_ok"])
        self.assertEqual(row["term_affirmed"], [])

    def test_silence_when_asked_is_a_defect(self):
        """Спросили про несуществующий документ, а ответ обошёл вопрос молчанием."""
        text = ("Готовьте акт экспертизы уполномоченной ТПП и сертификат СТ-1 для продукции вне "
                "приложения.")
        row = self.ev.documents_row(_case(49), text)
        self.assertFalse(row["explanation_ok"])


class TestFakeDocumentDetection(unittest.TestCase):
    """⚠⚠ Отрицание — ВЕРНЫЙ ответ, а не дефект: на этом гард продукта уже обжигался (`ddd3e16`)."""

    def setUp(self):
        import eval_documents

        self.mentions = eval_documents.conclusion_mentions

    def test_affirmative_use_is_flagged(self):
        text = "Дополнительно потребуется заключение ТПП о подтверждении производства."
        _, affirmed = self.mentions(text)
        self.assertTrue(affirmed)

    def test_denial_is_not_flagged(self):
        text = "Документа «заключение ТПП», подтверждающего производство, не существует."
        total, affirmed = self.mentions(text)
        self.assertEqual(total, 1, "упоминание обязано считаться — на нём стоит условность K15")
        self.assertEqual(affirmed, [], "отрицание — цель K15, а не дефект")

    def test_expert_conclusion_is_a_real_document(self):
        """«Экспертное заключение» — действующий документ п. 36-40 Правил (итог выездной проверки).

        Слепой запрет слова был бы дефектом: у «заключения» в корпусе три законных смысла."""
        text = ("ТПП РФ формирует экспертное заключение по итогам выездной проверки — оно служит "
                "основанием исключить реестровую запись.")
        total, affirmed = self.mentions(text)
        self.assertEqual((total, affirmed), (0, []))

    def test_second_affirmation_in_the_same_answer_is_seen(self):
        text = ("Нужно заключение ТПП о происхождении сырья. Также торгово-промышленная палата "
                "выдаёт заключение по результатам осмотра.")
        _, affirmed = self.mentions(text)
        self.assertGreaterEqual(len(affirmed), 2)


class TestGuardIsNotTheOracle(unittest.TestCase):
    """⚠⚠ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ: снятие рантайм-гарда НЕ красит и НЕ зеленит замер.

    Тест с одной положительной половиной зелен и при удалённом предохранителе — урок `O3` #104.
    Здесь проверяется само условие проверки: оракул читает ТЕКСТ, а не вердикт продукта."""

    def test_oracle_survives_a_disabled_guard(self):
        import eval_documents
        from app.rag import documents_ref

        text = "Дополнительно потребуется заключение ТПП о подтверждении производства."
        with unittest.mock.patch.object(documents_ref, "unverified_documents",
                                        lambda _t: []):        # гард «снят»
            _, affirmed = eval_documents.conclusion_mentions(text)
        self.assertTrue(affirmed, "оракул замолчал вместе с гардом — значит он и был гардом")

    def test_oracle_does_not_import_the_guard(self):
        """⚠ Ищем ВЫЗОВ и ИМПОРТ, а не слово: упоминание в комментарии — законно.

        Проверка по голому имени уже подводила проект — предохранитель выкатки 20.08 остановил
        релиз, поймав имя удалённой константы в комментарии, ОБЪЯСНЯЮЩЕМ её удаление. Здесь было
        бы то же: соседний комментарий разбирает, почему гард сюда не зовут."""
        import re as _re

        src = (ROOT / "scripts" / "eval_documents.py").read_text(encoding="utf-8")
        self.assertIsNone(_re.search(r"^\s*from .*documents_ref import .*unverified_documents",
                                     src, _re.M), "гард импортирован в замер")
        self.assertIsNone(_re.search(r"unverified_documents\s*\(", src),
                          "вердикт замера начал зависеть от предохранителя продукта")


class TestSectionAttributionSkipsDocumentsCases(unittest.TestCase):
    """У документного вопроса раздела нет — атрибуцию считать НЕ на чем.

    Прежняя форма (`if c["in_scope"]`) искала бы «раздел None»: уверенно возвращала False и
    роняла метрику, ничего при этом не измеряя. Тот же класс, что `EV17` #109."""

    def test_attribution_is_none_without_expected_section(self):
        import eval_answers

        self.assertFalse(eval_answers.attributed(PERFECT, None, []))   # искать нечего
        pure = [c for c in _docs_cases() if c["expected_section"] is None]
        self.assertTrue(pure, "проверять нечего: чистых документных кейсов в наборе нет")

    def test_mixed_case_keeps_its_section(self):
        mixed = [c for c in _docs_cases() if c["expected_section"]]
        self.assertTrue(mixed, "смешанный кейс обязан сохранять ожидаемый раздел")


if __name__ == "__main__":
    unittest.main()
