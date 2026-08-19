"""Регрессии по находкам повторного ревью PR #94 (дельта `6dffe81..e3564ce`).

⚠ ЗАЧЕМ ОТДЕЛЬНЫМ ФАЙЛОМ. Все пятнадцать находок этого ревью — дефекты, ВНЕСЁННЫЕ правками по
ПЕРВОМУ ревью того же PR. Класс подтвердился трижды за сессию («свои свежие правки — самый
вероятный источник дефекта»), и почти у каждой находки ревью отдельно отметило: теста не было.
Здесь закрыт ровно тот сценарий, который был воспроизведён, — чтобы правка не вернулась молча.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.prompts import NAVIGATOR_SYSTEM_PROMPT, PROCEDURAL_SYSTEM_PROMPT  # noqa: E402
from app.rag import fragments, okpd2_ref  # noqa: E402
from app.rag import pipeline as P  # noqa: E402
from app.rag.retriever import Hit  # noqa: E402


def H(name="Позиция", codes=None, anchor="a", match=False, blocks=None,
      mt=None, section="IX", payload=None) -> Hit:
    return Hit(0.5, section, "Радиоэлектроника", name, list(codes or ["26.30.50.110"]),
               mt, list(blocks or []), anchor, match, dict(payload or {}))


class TestDateIsNotAnOkpd2Code(unittest.TestCase):
    """Находки 2, 3, 11: дата в тексте становилась кодом ОКПД2 (или гасила мультитёрн).

    Разбор кода был размазан по СЕМИ копиям регулярки, фильтр дат стоял в одной. Теперь разбор
    один — `okpd2_ref` — и проверяется он на всех пяти формах, воспроизведённых ревью."""

    DATES = ["Экспертизу проходили 27.12.2023г, какие требования?",   # откат регулярки на 27.12
             "с 01.07.2026г. действует новая редакция",               # то же с 01.07
             "заключение действует с 01.07.26 по 31.12.26",           # двузначный год
             "работаем с 09.00 до 18.00",                             # часы работы
             "что изменилось с 22.07.2026?"]

    def test_no_date_form_yields_a_code(self):
        for q in self.DATES:
            with self.subTest(q=q):
                self.assertEqual(okpd2_ref.extract_codes(q), [], f"дата разобрана как код: {q}")
                self.assertFalse(okpd2_ref.has_okpd2_code(q))

    def test_real_codes_survive_next_to_a_date(self):
        """Радиус правки: настоящий код обязан выжить рядом с датой и запятой."""
        self.assertEqual(
            okpd2_ref.extract_codes("наш код 28.15.10, заключение получали 27.12.2023"),
            ["28.15.10"])
        self.assertEqual(okpd2_ref.extract_codes("коды 28.13.14,26.30.50"), ["28.13.14", "26.30.50"])
        self.assertEqual(okpd2_ref.extract_codes("требования по 28.13.14.110."), ["28.13.14.110"])

    def test_common_classes_are_codes(self):
        """⚠ Классификатор НЕПОЛОН на уровне классов — проверка идёт по префиксам, не по вхождению.

        `29.10`, `32.50`, `21.20`, `26.30`, `21.10` в файле отсутствуют, хотя это ходовые коды
        корпуса; точная проверка отбросила бы их как «дату» (день ≤ 31, месяц ≤ 12)."""
        for c in ["29.10", "32.50", "21.20", "26.30", "21.10", "27.12", "13.2", "27.2"]:
            with self.subTest(code=c):
                self.assertTrue(okpd2_ref.is_okpd2_code(c), f"настоящий код отброшен: {c}")

    def test_point_numbers_are_not_codes(self):
        self.assertEqual(okpd2_ref.extract_codes("смотри пункт 4.2.1 правил"), [])

    def test_anchor_code_ignores_dates(self):
        """Находка 2: `_anchor_code` брал ПОСЛЕДНИЙ токен и якорил диалог на дате."""
        history = [{"role": "user", "content": "Наш код 28.15.10, заключение получали 27.12.2023"}]
        self.assertEqual(P._anchor_code(history), "28.15.10")

    def test_date_in_followup_keeps_multiturn(self):
        """Находка 11: `_HAS_CODE_RE` видел в дате код и молча выключал мультитёрн."""
        self.assertTrue(P._needs_context("а какой порог на 01.07.2026?"))
        self.assertTrue(P._is_continuation("а какой порог на 01.07.2026?"))
        self.assertFalse(P._needs_context("наш код 28.13.14"))   # настоящий код по-прежнему гасит


class TestPointsClaimsDoNotContradict(unittest.TestCase):
    """Находка 1: блок печатал «баллы не начисляются» И «баллы НЕ ПРИВЕДЕНЫ» подряд.

    Ветка `elif no_points` ловила ровно тот случай, ради которого у первой ветки стоит `and mt`
    (8–11 % корпуса), — два взаимоисключающих утверждения об одной позиции, которые модель
    разрешала сама, от прогона к прогону по-разному."""

    def _ctx(self, mt):
        hit = H(name="Модули синтеза", codes=["27.90.11.000"], mt=mt, match=True,
                blocks=[{"operations": [{"text": "сборка"}]}],
                payload={"requirement_type": "operations"})
        return P.format_context([hit], "модули синтеза", "27.90.11.000")

    def test_no_threshold_says_it_once(self):
        ctx = self._ctx(None)
        self.assertIn("баллы за них не начисляются", ctx)
        self.assertNotIn("НЕ ПРИВЕДЕНЫ", ctx)

    def test_textual_threshold_still_denies_points(self):
        ctx = self._ctx("не менее 5 из следующих технологических операций")
        self.assertIn("Балльная оценка: не предусмотрена", ctx)
        self.assertNotIn("НЕ ПРИВЕДЕНЫ", ctx)

    def test_rule_2v_keys_on_the_block_label_not_the_operation_label(self):
        """Находка 7: переименование подписи операции сделало её ТРИГГЕРОМ правила 2в.

        Подпись «— баллы не приведены» стоит у каждой безбалльной операции, в том числе внутри
        блока, который прямо говорит «баллы не предусмотрены»."""
        self.assertIn("Балльная оценка:", NAVIGATOR_SYSTEM_PROMPT)
        i = NAVIGATOR_SYSTEM_PROMPT.index("2в.")
        rule = NAVIGATOR_SYSTEM_PROMPT[i:NAVIGATOR_SYSTEM_PROMPT.index("3. Укажи", i)]
        self.assertIn("Балльная оценка:", rule, "правило 2в не привязано к строке блока")
        self.assertIn("ОТДЕЛЬНОЙ операции", rule, "правило 2в не отделяет подпись операции")


class TestAddPositionsByCode(unittest.TestCase):
    """Находки 4, 5, 6, 12, 13: окно, пометки и бюджет добора. Тестов не было ни одного."""

    def test_marked_hit_is_not_evicted_by_the_next_code(self):
        """Находка 4: обрезка шла ПО ИНДЕКСУ и выбрасывала запись, помеченную секунду назад.

        `EV8` отказывала ровно на вопросе «сравни A и B», ради которого написана."""
        win = [H(name=f"кандидат {i}", codes=[f"99.9{i}"], anchor=f"a{i}") for i in range(7)]
        win.append(H(name="ЦЕЛЬ-B", codes=["26.30.50.110"], anchor="bbb"))
        fetched = H(name="ЦЕЛЬ-A", codes=["28.13.14.110"], anchor="aaa", match=True)
        with mock.patch.object(P, "search", return_value=[fetched]):
            out = P._add_positions_by_code(["28.13.14", "26.30.50.110"], list(win), 8)
        names = [h.product_name for h in out]
        self.assertIn("ЦЕЛЬ-B", names, "помеченная запись вытеснена собственным добором")
        self.assertIn("ЦЕЛЬ-A", names)
        targets = [h.product_name for h in P.target_hits(out, ["28.13.14", "26.30.50.110"])]
        self.assertEqual(sorted(targets), ["ЦЕЛЬ-A", "ЦЕЛЬ-B"])

    def test_window_never_grows_past_the_limit(self):
        win = [H(name=f"к{i}", codes=[f"99.9{i}"], anchor=f"a{i}") for i in range(8)]
        fetched = H(name="новая", codes=["28.13.14.110"], anchor="new", match=True)
        with mock.patch.object(P, "search", return_value=[fetched]):
            out = P._add_positions_by_code(["28.13.14"], list(win), 8)
        self.assertLessEqual(len(out), 8)

    def test_sibling_topup_does_not_claim_a_code_match(self):
        """Находка 5: добор сиблинга ставил `okpd2_match`, то есть рантайм утверждал совпадение
        по коду, которого пользователь не называл. Это поднимало `confident`, а тот коротким
        замыканием снимал расчёт релевантности, второй сигнал out-of-scope и подсказки."""
        win = [H(name="кандидат", codes=["99.99"], anchor="c0")]
        sib = H(name="Светодиоды красного диапазона", codes=["26.11.22.214"], anchor="sib", match=True)
        with mock.patch.object(P, "search", return_value=[sib]):
            out = P._add_positions_by_code(["26.11.22.214"], list(win), 8, mark=False)
        self.assertIn("Светодиоды красного диапазона", [h.product_name for h in out])
        self.assertFalse(any(h.okpd2_match for h in out), "добор объявил совпадение по коду")

    def test_accept_predicate_rejects_a_foreign_group(self):
        """Находка 6: группы расколотых ячеек ПЕРЕСЕКАЮТСЯ по кодам, и добор по коду мог принести
        строку чужой группы — вплоть до записи, прямо ИСКЛЮЧАЮЩЕЙ спрошенную продукцию."""
        win = [H(name="кандидат", codes=["99.99"], anchor="c0")]
        alien = H(name="чужая группа", codes=["26.11.22.200"], anchor="alien", match=True)
        with mock.patch.object(P, "search", return_value=[alien]):
            out = P._add_positions_by_code(["26.11.22.200"], list(win), 8,
                                           mark=False, accept=lambda h: False)
        self.assertEqual([h.product_name for h in out], ["кандидат"])


class TestGroupCodes(unittest.TestCase):
    """Находка 12: бюджет добора уходил на САМ квалификатор, сиблинги не добирались."""

    def test_qualifier_rows_are_not_offered_as_siblings(self):
        for name in ["Светодиоды (за исключением светодиодов белого диапазона)",
                     "Светодиоды (в части светодиодов белого диапазона)"]:
            with self.subTest(name=name):
                codes = fragments.group_codes(name)
                self.assertTrue(codes, "группа не нашлась — тест перестал что-либо мерить")
                for c in codes:
                    self.assertNotEqual(c, "26.11.22.200",
                                        "в сиблинги попал сам квалификатор группы")

    def test_group_codes_stay_inside_one_group(self):
        """Имя входит в ОДНУ группу (первую по `_group_index`) — коды берутся только из неё."""
        name = "Светодиоды (в части светодиодов белого диапазона)"
        group = fragments.group_of(name)
        self.assertIsNotNone(group)
        self.assertEqual(fragments.group_codes(name), ("26.11.22.216",))

    def test_result_is_hashable_and_cached(self):
        """Единственный читатель файла без кэша перечитывал и разбирал его на КАЖДЫЙ запрос."""
        name = "Светодиоды (в части светодиодов белого диапазона)"
        self.assertIsInstance(fragments.group_codes(name), tuple)
        self.assertIs(fragments.group_codes(name), fragments.group_codes(name))


class TestOperationCounterAgreesWithContext(unittest.TestCase):
    """Находка 12 (смежное): опора внутри расколотой группы выбиралась ДРУГИМ счётчиком.

    `_n_operations` считал только `requirement_blocks[].operations`, а контекст показывает
    требования через `_hit_operations` (тот добавляет блоки R6 — текст в `component`)."""

    def test_r6_block_counts_as_a_requirement(self):
        hit = H(blocks=[{"component": "наличие прав на конструкторскую документацию"}])
        self.assertEqual(P._n_operations(hit), len(P._hit_operations(hit)))
        self.assertEqual(P._n_operations(hit), 1, "требование R6 не посчитано — счётчики разошлись")


class TestAskForCodeGuard(unittest.TestCase):
    """Находка 9: подмена опоры на сиблинга включала просьбу назвать УЖЕ НАЗВАННЫЙ код."""

    def test_no_request_for_a_code_the_user_already_gave(self):
        qual = H(name="Светодиоды (в части светодиодов белого диапазона)",
                 codes=["26.11.22.210"], anchor="q1", match=True, section="IV")
        sib = H(name="Светодиоды белого диапазона", codes=["26.11.22.216"], anchor="s1",
                section="IV", blocks=[{"operations": [{"text": "сборка", "points": 5}]}])
        other = H(name="Прочая продукция", codes=["99.99"], anchor="o1")
        hits = [qual, sib, other]
        self.assertEqual([t.product_name for t in P.target_hits(hits, "26.11.22.210")],
                         ["Светодиоды белого диапазона"])
        ctx = P.format_context(hits, "светодиоды белого диапазона", "26.11.22.210")
        self.assertNotIn("попроси её", ctx, "у эксперта просят код, который он уже назвал")

    def test_request_stays_when_no_code_was_given(self):
        hits = [H(name="Кандидат A", codes=["26.30.50.110"], anchor="a1",
                  blocks=[{"operations": [{"text": "сборка"}]}]),
                H(name="Кандидат B", codes=["99.99"], anchor="b1")]
        ctx = P.format_context(hits, "какая-то продукция", None)
        self.assertIn("попроси её", ctx, "подсказка уточнить по коду пропала вовсе")


class TestProceduralContextHasNoInternalVocabulary(unittest.TestCase):
    """Находка 10: врезка «(в контексте пункта: …)» ехала в промпт на ПРОЦЕДУРНОЙ ветке.

    Запрета на слово там нет вовсе, а метрика полноты гоняет только товарные кейсы — то есть
    отчёт «внутренняя лексика 1.00» этот канал не видел."""

    def test_parent_intro_wording_is_clean(self):
        ctx = P.format_rules_context([{
            "source_anchor": "Приказ ТПП №52, п. 4.2.1",
            "text": "Правоустанавливающие документы заявителя.",
            "parent_intro": "4.2. К заявке прилагаются следующие документы",
        }])
        self.assertIn("4.2. К заявке прилагаются", ctx, "вводная родителя потерялась")
        self.assertNotIn("контекст", ctx.lower())

    def test_procedural_prompt_has_no_ban_to_rely_on(self):
        """Фиксируем посылку находки: на этой ветке запрета нет, поэтому чистить надо РЕНДЕР."""
        self.assertNotIn("контекст", PROCEDURAL_SYSTEM_PROMPT.lower())


class TestAnswerCodesConsumers(unittest.TestCase):
    """Находка 14: `Answer.codes` завели, а продуктовый потребитель остался на своём коде."""

    def test_navigation_carries_the_runtime_codes(self):
        from app.tools.navigator import Navigation
        self.assertIn("codes", Navigation.__dataclass_fields__)

    def test_foreign_numbers_accepts_a_list_of_codes(self):
        """⚠ Прежняя сигнатура `str | None` звала `code.strip()` — передача `ans.codes` роняла
        AttributeError, то есть половинчатая миграция была ловушкой для следующей правки."""
        from scripts.eval_determinism import _codes_of, _exact_code
        self.assertEqual(_codes_of(["28.13.14", " 26.30.50 "]), ["28.13.14", "26.30.50"])
        self.assertEqual(_codes_of("28.13.14"), ["28.13.14"])
        self.assertEqual(_codes_of(None), [])
        hit = H(codes=["26.30.50"])
        self.assertTrue(_exact_code(hit, ["28.13.14", "26.30.50"]))
        self.assertFalse(_exact_code(hit, ["28.13.14"]))


class TestExtraCodesLimit(unittest.TestCase):
    """Находка 13: лимит резал ВЫБОРКУ, а целевыми объявлялись все названные коды."""

    def test_limit_is_about_extra_codes_only(self):
        self.assertEqual(P.MAX_EXTRA_CODES, 3)
        self.assertGreaterEqual(P.MAX_SIBLING_CODES, 4,
                                "бюджет добора сиблингов не покрывает самую большую группу")


if __name__ == "__main__":
    unittest.main()


class TestProcurementThresholdIsNotTheGeneralOne(unittest.TestCase):
    """`K2` #47: примечания «для целей осуществления закупок» задают ДРУГОЙ порог.

    ⚠ Замер 19.08.2026: 57 позиций корпуса получали закупочный порог как СВОЙ ОБЩИЙ — со ссылкой
    на настоящее примечание («не менее 75 баллов [прим. 53]»). Заявитель, спрашивающий про
    подтверждение российского происхождения, получал порог программы госзакупок. Неверный порог с
    подлинной ссылкой опаснее отсутствующего: число дословно, поэтому faithfulness-гард молчит, —
    тот же класс, что утечка порога вверх по иерархии в `_code_applies`."""

    def test_scope_is_read_from_the_note_text(self):
        from app.rag.thresholds import note_scope
        for n in ["29", "31", "52", "53", "8", "9"]:
            with self.subTest(note=n):
                self.assertEqual(note_scope(n), "procurement")
        for n in ["7", "11", "17", "26", "61", "77"]:
            with self.subTest(note=n):
                self.assertEqual(note_scope(n), "general")

    def test_unknown_note_defaults_to_general(self):
        """Неизвестное примечание — прежнее поведение, а не молчаливое исчезновение порога."""
        from app.rag.thresholds import note_scope
        self.assertEqual(note_scope("999"), "general")
        self.assertEqual(note_scope(None), "general")

    def test_procurement_threshold_never_answers_as_the_general_one(self):
        from app.rag.thresholds import lookup_procurement_threshold, lookup_threshold
        cases = [(["13.20.13"], "Ткани льняные", "XVII"), (["13.10.50"], "Пряжа шерстяная", "XVII")]
        for codes, name, sec in cases:
            with self.subTest(name=name):
                self.assertIsNone(lookup_threshold(codes, name, sec),
                                  "закупочный порог всё ещё выдаётся как общий")
                self.assertIsNotNone(lookup_procurement_threshold(codes, name, sec),
                                     "закупочный порог потерян вовсе — это тоже не годится")

    def test_context_prints_the_condition_with_the_number(self):
        """Число без условия и есть неверный ответ — условие обязано ехать вместе с порогом."""
        hit = Hit(0.9, "XVII", "Лёгкая промышленность", "Ткани льняные", ["13.20.13"], None,
                  [{"operations": [{"text": "ткачество", "points": 30}]}], "Разд. XVII, поз. 5",
                  True, {})
        ctx = P.format_context([hit], "ткани льняные", "13.20.13")
        self.assertIn("ДЛЯ ЦЕЛЕЙ ЗАКУПОК", ctx)
        self.assertIn("не для подтверждения происхождения", ctx)
        self.assertNotIn("\n    Порог: не менее 50", ctx, "закупочное число стоит в строке «Порог»")
