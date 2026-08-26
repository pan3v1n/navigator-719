"""`K15-1` #120 — маршрут вопроса и гейт закрытого справочника. Офлайн, без Qdrant и LLM.

ЗАЧЕМ. `is_procedural` — развилка, после которой вопрос уже не встретит ни требований приложения,
ни корпуса Правил, смотря куда ушёл. Мерить её было нечем: `eval_rules` дёргает `search_rules`
НАПРЯМУЮ, минуя гейт, поэтому «атрибуция@1 0.96» три месяца была утверждением о качестве поиска
в корпусе, а не о попадании вопроса в этот корпус.

Что чинилось (замерено свипом `scripts/eval_routing.py`, радиус +3 / −0 / ложных 0):

* **асимметрия разрыва.** Три соседних шаблона задавали РАЗНЫЕ правила: у отглагольного
  существительного («внесение … в реестр») разрыв запрещён вовсе, у глагола («внести его в
  реестр») разрешён до четырёх слов. Из-за одного слова «нужно ли заключение ТПП для ВНЕСЕНИЯ
  ПРОДУКЦИИ В РЕЕСТР» уходило товарной веткой и получало 12.8 КБ требований к охранной
  сигнализации вместо норм;
* **разошедшиеся гейты блока и его содержимого.** Разъяснение про несуществующий документ
  зависело от ВОПРОСА, а сам справочник — от ТЕМЫ. На вопросе выше тема `registry_entry`, и
  справочника не было вовсе — ровно там, где риск выдумать документ максимален.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import documents_ref, procedural, topics  # noqa: E402

# Дословный вопрос кейса #50 приёмочного набора — он же критерий приёмки issue #120.
CASE_50 = "нужно ли заключение ТПП для внесения продукции в реестр"


def _reference_header() -> str:
    """Шапка блока справочника — из самой функции, чтобы утверждение не устарело молча."""
    return documents_ref.documents_context_block("").splitlines()[0]


class TestGapIsAllowedForEveryForm(unittest.TestCase):
    """Разрыв между словом внесения/включения и «в реестр» — один и тот же для ВСЕХ форм."""

    FORMS = ("внесение", "внесения", "внесении", "включение", "включения", "внести", "включить")
    GAPS = ("", "продукции ", "сведений о продукции ", "изменений в сведения ")

    def test_all_forms_with_all_gaps(self):
        for form in self.FORMS:
            for gap in self.GAPS:
                q = f"каков порядок {form} {gap}в реестр"
                with self.subTest(form=form, gap=gap.strip() or "<без разрыва>"):
                    self.assertTrue(procedural.is_procedural(q), q)

    def test_case_50_routes_procedurally(self):
        self.assertTrue(procedural.is_procedural(CASE_50),
                        "вопрос #120 снова уходит товарной веткой")

    def test_two_more_cases_of_the_procedural_golden_set(self):
        """Оба из набора `K9` — их промах и показал, что дефект не единичный."""
        for q in ("в каком случае откажут во внесении изменений в реестр",
                  "как формируется заявка на включение сведений в реестр"):
            with self.subTest(q=q[:40]):
                self.assertTrue(procedural.is_procedural(q))


class TestNarrowFormWouldBreakIt(unittest.TestCase):
    """⚠ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ: с прежним узким шаблоном тесты выше обязаны падать.

    Без него утверждение «маршрут починен» зелено и при откате правки — тест с одной
    положительной половиной не ловит снятие предохранителя (урок `O3` #104)."""

    def test_old_pattern_sends_case_50_to_the_product_branch(self):
        """⚠ Сигнал темы (`P3` #121) приходится глушить, и это не ослабление теста, а факт:
        после `P3` кейс #50 держат ДВА независимых сигнала — маркер и тема `registry_entry` с
        якорем «реестр». Оставь я тест как был, он зеленел бы за счёт темы и перестал бы измерять
        маркер вовсе — то самое насыщение, от которого страдали `is_clarifying` и `_target_hit`."""
        narrow = [p for p in procedural._STRONG_PATTERNS
                  if "внесени|включени|внести|включит" not in p]
        narrow += [r"внесени\w*\s+в\s+реестр", r"включени\w*\s+в\s+реестр",
                   r"внести\s+\w+(?:\s+\w+){0,4}\s+в\s+реестр"]
        with unittest.mock.patch.object(procedural, "_STRONG_RE",
                                        re.compile("|".join(narrow), re.I)), \
             unittest.mock.patch.object(procedural, "_has_routing_topic", lambda _q: False):
            self.assertFalse(procedural.is_procedural(CASE_50),
                             "узкий шаблон обязан воспроизводить дефект — иначе тест мерит не его")

    def test_case_50_is_now_held_by_two_independent_signals(self):
        """Оба сигнала по отдельности доводят кейс #50 до процедурной ветки — эшелонирование."""
        with unittest.mock.patch.object(procedural, "_has_routing_topic", lambda _q: False):
            self.assertTrue(procedural.is_procedural(CASE_50), "маркер перестал держать кейс")
        self.assertTrue(procedural._has_routing_topic(CASE_50), "тема перестала держать кейс")


class TestProductQuestionsStayOnTheProductBranch(unittest.TestCase):
    """Ложные срабатывания держит не узость шаблона, а товарно-балльный сигнал."""

    def test_localisation_question_is_not_procedural(self):
        # Пример из шапки самого модуля: «требования» уводят на товарный путь.
        for q in ("требования к локализации насосов для внесения в реестр",
                  "сколько баллов нужно набрать для внесения продукции в реестр",
                  "какие требования к локализации станков для включения в реестр"):
            with self.subTest(q=q[:40]):
                self.assertFalse(procedural.is_procedural(q), q)

    def test_sweep_pins_the_false_positive_count(self):
        """⚠⚠ ГЛАВНЫЙ ПРЕДОХРАНИТЕЛЬ ПРАВКИ: расширение маркера не должно тащить товарные вопросы.

        До `K15-1` у гейта не было НИ ОДНОГО такого теста — потому он и разошёлся с корпусом.
        Число закреплено на всех наборах репозитория (188 вопросов); единственное известное
        срабатывание — `topics#45`, оно старше этой правки."""
        import eval_routing

        rows = eval_routing.collect()
        false_pos = [r for r in rows if r["class"] == "товарный" and r["procedural"]]
        self.assertEqual([(r["set"], r["id"]) for r in false_pos], [("topics", 45)],
                         f"состав ложных срабатываний изменился: "
                         f"{[(r['set'], r['id'], r['query'][:40]) for r in false_pos]}")

    def test_sweep_covers_every_question_set(self):
        """Положительный контроль свипа: он молчал бы и на пустом наборе."""
        import eval_routing

        rows = eval_routing.collect()
        self.assertGreaterEqual(len(rows), 180)
        self.assertEqual(len({r["set"] for r in rows}), len(eval_routing.SETS))


class TestReferenceGateFollowsTheQuestion(unittest.TestCase):
    """Справочник приезжает, когда вопрос КАСАЕТСЯ документа, — а не только когда тема `documents`."""

    def test_predicate_recognises_the_four_measured_questions(self):
        for q in (CASE_50,
                  "сертификаты СТ-1 или заключения ТПП о происхождении сырья",
                  "какой порядок получения заключения о подтверждении производства",
                  "а что есть Заключение Минпромторга?"):
            with self.subTest(q=q[:40]):
                self.assertTrue(documents_ref.asks_about_conclusion(q))

    def test_predicate_is_quiet_on_a_plain_product_question(self):
        for q in ("производим станки с ЧПУ", "какие документы нужны для этикетировщиков"):
            with self.subTest(q=q[:40]):
                self.assertFalse(documents_ref.asks_about_conclusion(q))

    def test_case_50_is_not_a_documents_topic_and_still_gets_the_reference(self):
        """⚠ Суть дефекта: тема тут ДРУГАЯ, и раньше этого хватало, чтобы справочника не было."""
        self.assertNotEqual(topics.classify(CASE_50), topics.DOCUMENTS,
                            "кейс перестал быть примером расхождения гейтов — тест обесценился")
        self.assertTrue(documents_ref.asks_about_conclusion(CASE_50))

    def test_procedural_plan_carries_the_reference(self):
        """Через `plan_procedural` на заглушках: Qdrant не нужен, проверяется сборка промпта."""
        from app.rag import pipeline

        fake = [{"doc_type": "rules_registry", "text": "Пункт про реестр.",
                 "source_anchor": "Правила, п. 1", "_score": 1.0}]
        with unittest.mock.patch.object(pipeline, "search_rules", lambda *a, **k: fake):
            _topic, _rules, _ctx, user = pipeline.plan_procedural(CASE_50, CASE_50)
        # ⚠ Якорь берём из самой функции, а не константой: первая редакция теста сверяла
        # `TABLE_TITLE` — заголовок ТАБЛИЦЫ ОТВЕТА, а не блока контекста, и падала на верном коде.
        self.assertIn(_reference_header(), user, "закрытый справочник не доехал до промпта")
        self.assertIn(documents_ref.NONEXISTENT_EXPLANATION, user,
                      "разъяснение про несуществующий документ не доехало")

    def test_reference_absent_when_the_question_is_about_neither(self):
        """Отрицательный контроль: на процедурном вопросе без документов справочника быть не должно."""
        from app.rag import pipeline

        fake = [{"doc_type": "rules_registry", "text": "Пункт про сроки.",
                 "source_anchor": "Правила, п. 2", "_score": 1.0}]
        q = "какой срок рассмотрения заявления о внесении в реестр"
        self.assertNotEqual(topics.classify(q), topics.DOCUMENTS)
        with unittest.mock.patch.object(pipeline, "search_rules", lambda *a, **k: fake):
            _t, _r, _c, user = pipeline.plan_procedural(q, q)
        self.assertNotIn(_reference_header(), user,
                         "справочник приезжает туда, где о документах не спрашивали")


class TestTopicRoutesTheQuestion(unittest.TestCase):
    """`P3` #121 — намерение вопроса стало сигналом маршрута, но только с 719-якорем."""

    def test_topic_sends_a_registry_question_to_the_rules_branch(self):
        for q in ("как получить выписку из реестра",
                  "что делать если заявку направили на доработку",
                  "нужен ли специальный инвестиционный контракт для подтверждения производства"):
            with self.subTest(q=q[:40]):
                self.assertTrue(procedural.is_procedural(q), q)

    def test_without_the_topic_signal_they_are_missed(self):
        """⚠ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ: снятие сигнала темы обязано вернуть дефект."""
        q = "как получить выписку из реестра"
        with unittest.mock.patch.object(procedural, "_has_routing_topic", lambda _q: False):
            self.assertFalse(procedural.is_procedural(q),
                             "вопрос проходит и без темы — тест мерит не сигнал темы")

    def test_topic_alone_is_not_enough_without_a_719_anchor(self):
        """⚠⚠ Вне-719 админ-вопрос обязан остаться вне процедурной ветки.

        Шапка модуля называет «порядок получения загранпаспорта» примером ровно этого. Первая
        редакция правки его пропускала: `topics.classify` даёт ему `registry_entry` по словам
        «порядок получения», потому что калиброван он на вопросах, которые гейт УЖЕ пропустил, —
        вне-719 он не видел никогда."""
        q = "порядок получения загранпаспорта"
        self.assertIsNotNone(topics.classify(q),
                             "пример перестал быть примером: тема больше не срабатывает")
        self.assertFalse(procedural._has_routing_topic(q), "тема без якоря маршрутизирует")
        self.assertFalse(procedural.is_procedural(q))

    def test_documents_topic_never_routes_by_itself(self):
        """⚠⚠ СМЕШАННЫЙ ВОПРОС ОБЯЗАН ОСТАТЬСЯ ТОВАРНЫМ — ради него и заводилась `K12`.

        Тема `documents` единственная работает на ОБЕИХ ветках: «какие документы нужны для
        этикетировщиков» получает блок документов ДОПОЛНИТЕЛЬНО к требованиям позиции. Пропусти
        мы эту оговорку — вопрос уехал бы на процедурную ветку и потерял требования продукции."""
        q = "какие документы нужны для этикетировщиков"
        self.assertEqual(topics.classify(q), topics.DOCUMENTS)
        self.assertFalse(procedural._has_routing_topic(q))
        self.assertFalse(procedural.is_procedural(q), "смешанный вопрос уехал с товарной ветки")

    def test_tpp_is_a_719_anchor(self):
        """Список якорей называл ГИСП и Минпромторг — и пропускал ТПП, ради которой сервис и есть."""
        self.assertTrue(procedural._ANCHOR_RE.search("какой срок рассмотрения документов в ТПП"))
        self.assertTrue(procedural._ANCHOR_RE.search("чем подтвердить страну происхождения товара"))


class TestRoutingSweepPinsTheNumbers(unittest.TestCase):
    """Свип — предохранитель на пути правки маркеров, а не отчёт постфактум."""

    def setUp(self):
        import eval_routing

        self.ev = eval_routing
        self.rows = eval_routing.collect()

    def test_no_out_of_scope_question_takes_the_procedural_branch(self):
        """⚠⚠ Вторая регрессия, чуть не уехавшая: свип её сначала не считал вовсе."""
        oos = [r for r in self.rows if r["class"] == "вне сферы" and r["procedural"]]
        self.assertEqual(oos, [], f"вне-719 вопрос ушёл процедурной веткой: "
                                  f"{[r['query'][:40] for r in oos]}")

    def test_documents_cases_keep_their_expected_route(self):
        wrong = [(r["id"], r["procedural"]) for r in self.rows if r["class"] == "документный"
                 and r["procedural"] != self.ev.EXPECTED_DOCUMENTS_ROUTE.get(r["id"])]
        self.assertEqual(wrong, [], "документный кейс сменил ветку")

    def test_misses_do_not_grow(self):
        """Число закреплено: 11 на 26.08.2026. Может только УБЫВАТЬ (остаток — `P3` #121)."""
        miss = [r for r in self.rows if r["class"] == "процедурный" and not r["procedural"]]
        self.assertLessEqual(len(miss), 11,
                             f"пропусков стало больше: {[(r['set'], r['id']) for r in miss]}")


class TestConclusionIsADocumentNotAnAct(unittest.TestCase):
    """Ревью пакета 26.08.2026: «заключение» бывает ДЕЙСТВИЕМ, и широкий детектор это ловил.

    Справочник с разъяснением про несуществующий документ уезжал в товарные вопросы вроде «какие
    требования при ЗАКЛЮЧЕНИИ СПИК» — то есть `dbfc622` (термин приходит к тому, кто не спрашивал)
    переоткрывался на товарной ветке."""

    DOCUMENT = ("нужно ли заключение ТПП для внесения продукции в реестр",
                "сертификаты СТ-1 или заключения ТПП о происхождении сырья/материалов",
                "какой порядок получения заключения о подтверждении производства",
                "а что есть Заключение Минпромторга?")
    ACT = ("какие требования при заключении СПИК для 27.11.42",
           "производим станки, заключение договора с поставщиком",
           "заключение специального инвестиционного контракта — это что",
           "что нужно для заключения соглашения о защите капвложений")

    def test_document_forms_are_recognised(self):
        for q in self.DOCUMENT:
            with self.subTest(q=q[:44]):
                self.assertTrue(documents_ref.asks_about_conclusion(q))

    def test_act_forms_are_not(self):
        for q in self.ACT:
            with self.subTest(q=q[:44]):
                self.assertFalse(documents_ref.asks_about_conclusion(q),
                                 "справочник уедет в вопрос, где «заключение» — действие")

    def test_plain_product_question_gets_no_reference(self):
        """⚠ Следствие для `input_hint`: `documents_answered` считается по этому же блоку, и на
        товарном вопросе он не должен предлагать продолжение про сроки документов."""
        for q in ("производим этикетировочные машины", "требования к чиллерам"):
            with self.subTest(q=q[:40]):
                self.assertFalse(documents_ref.asks_about_conclusion(q))


class TestChamberMembershipIsOutOf719(unittest.TestCase):
    """Ревью пакета: новый якорь `ТПП` уводил вопросы о САМОЙ ПАЛАТЕ на процедурную ветку."""

    def test_membership_questions_do_not_take_the_procedural_branch(self):
        for q in ("какие документы нужны для вступления в ТПП",
                  "какой перечень документов нужен для членства в ТПП",
                  "сколько стоит членский взнос в торгово-промышленной палате"):
            with self.subTest(q=q[:44]):
                self.assertFalse(procedural.is_procedural(q),
                                 "вопрос о членстве в палате отвечается по Правилам реестра")

    def test_legitimate_719_questions_with_tpp_survive(self):
        for q in ("какой срок рассмотрения документов в ТПП",
                  "нужно ли заключение ТПП для внесения продукции в реестр",
                  "как получить акт экспертизы в ТПП"):
            with self.subTest(q=q[:44]):
                self.assertTrue(procedural.is_procedural(q))

    def test_entry_into_force_is_not_membership(self):
        """⚠ «Вступление В СИЛУ постановления» — законный 719-вопрос: запрет по одному слову
        «вступление» убил бы его, поэтому в правиле обязательно соседство с палатой."""
        self.assertIsNone(procedural._CHAMBER_MEMBERSHIP_RE.search(
            "когда вступление в силу новой редакции постановления 719"))

    def test_the_class_is_in_the_negative_set(self):
        """Предохранитель переживает сессию только в наборе: свип обязан видеть этот класс."""
        cases = json.loads((ROOT / "scripts" / "eval_golden_negative.json")
                           .read_text(encoding="utf-8"))["cases"]
        membership = [c for c in cases if c.get("category") == "членство-в-палате"]
        self.assertGreaterEqual(len(membership), 3, "класс исчез из негативного набора")


class TestMeasurementToolsAfterReview(unittest.TestCase):
    """Находки ревью в самих инструментах замера."""

    def test_context_size_does_not_generate_for_procedural_cases(self):
        """⚠⚠ `context_of` звал `_plan_answer` первым, а тот для процедурного вопроса ГЕНЕРИРУЕТ
        ответ DeepSeek. «Замер без вызова модели и без денег» платил за две генерации на прогон."""
        import eval_context_size as ecs
        from app.rag import pipeline

        calls = []
        fake = [{"doc_type": "rules_registry", "text": "п.", "source_anchor": "Правила, п. 1",
                 "_score": 1.0}]
        with unittest.mock.patch.object(pipeline, "search_rules", lambda *a, **k: fake), \
             unittest.mock.patch.object(pipeline, "_plan_answer",
                                        lambda *a, **k: calls.append(1)):
            got = ecs.context_of(CASE_50, "")
        self.assertIsNotNone(got, "процедурный кейс снова не измеряется")
        self.assertEqual(calls, [], "_plan_answer позван на процедурном вопросе — это платный путь")

    def test_source_recognizer_control_covers_the_set(self):
        """`KeyError` вылезал бы В СЕРЕДИНЕ прогона, уже потратив вызовы модели."""
        import eval_documents

        with self.assertRaises(SystemExit):
            eval_documents.check_source_recognizers({"rules_registry"})
        eval_documents.check_source_recognizers({"tpp_order_52"})   # известный — не бросает

    def test_sweep_explains_every_procedural_route(self):
        """Врущее объяснение хуже отсутствующего: инструмент затем и нужен, чтобы объяснять ПОЧЕМУ."""
        import eval_routing

        unexplained = [r for r in eval_routing.collect() if r["procedural"] and r["why"] == "?"]
        self.assertEqual(unexplained, [], f"маршрут без объяснения: "
                                          f"{[r['query'][:40] for r in unexplained]}")

    def test_sweep_attributes_the_topic_route_correctly(self):
        import eval_routing

        rows = {(r["set"], r["id"]): r for r in eval_routing.collect()}
        row = rows[("golden_rules", 12)]        # «как получить выписку из реестра»
        self.assertTrue(row["procedural"])
        self.assertTrue(row["why"].startswith("ТЕМА:"),
                        f"причина маршрута названа неверно: {row['why']}")


class TestAcceptanceCaseIsInTheSet(unittest.TestCase):
    """Критерий приёмки #120 обязан жить в наборе, а не в тексте issue."""

    def test_case_50_exists_with_the_right_expectations(self):
        cases = json.loads((ROOT / "scripts" / "eval_golden.json").read_text(encoding="utf-8"))["cases"]
        c = next((x for x in cases if x["id"] == 50), None)
        self.assertIsNotNone(c, "кейс #120 исчез из набора")
        self.assertEqual(c["query"], CASE_50)
        self.assertTrue(c["expect_explanation"])
        self.assertIn("Акт экспертизы уполномоченной ТПП", c["expect_documents"])


if __name__ == "__main__":
    unittest.main()
