"""`K15` #36 — справочник подтверждающих документов: закрытый перечень + рантайм-гард.

ЧТО ЗАКРЫВАЕТ. «Какие документы готовить» — самая частая тема отзывов (31 упоминание), и там же
самый заметный дефект: модель выдумывала «заключение ТПП». Перечень закрыт экспертами дважды.

⚠⚠ ЦИТАТЫ В ТЕСТАХ — НЕ ВЫДУМАНЫ. Положительный контроль взят из кейса эксперта
(`knowledge_base/cases/dokumenty_st1_akt_ekspertizy_terminologiya.json`, поле `correction_of`) —
это РЕАЛЬНЫЕ формулировки, которые сервис выдавал. Отрицательный контроль взят из КОРПУСА (Правила
ведения реестра, тело ПП №719) — это РЕАЛЬНЫЕ законные «заключения», по которым предохранитель
бить не имеет права. Оракул, выведенный из проверяемого артефакта, — известный класс ошибки этого
проекта; здесь обе половины пришли извне.
"""

from __future__ import annotations

import unittest

from app.core.prompts import NAVIGATOR_SYSTEM_PROMPT, PROCEDURAL_SYSTEM_PROMPT
from app.rag import documents_ref
from app.rag import pipeline as pipeline_mod

# Реальные выдумки сервиса — из `correction_of` кейса эксперта.
REAL_HALLUCINATIONS = (
    "сертификаты СТ-1 или заключения ТПП о происхождении сырья/материалов",
    "потребуется заключение ТПП о подтверждении производства",
)

# Реальные ЗАКОННЫЕ «заключения» — дословно из корпуса.
LEGITIMATE_FROM_CORPUS = (
    # Правила ведения реестра, п. 39 — инструмент КОНТРОЛЯ, не подтверждения
    "Торгово-промышленная палата Российской Федерации формирует экспертное заключение, "
    "которое в течение 5 рабочих дней со дня составления направляется в Министерство",
    # Правила ведения реестра, п. 40
    "Министерство промышленности и торговли Российской Федерации в течение 10 рабочих дней "
    "рассматривает экспертное заключение и принимает одно из следующих решений",
    # тело ПП №719 — здесь «заключение» вообще не документ, а действие
    "не истек 3-летний срок со дня заключения специального инвестиционного контракта",
    # тело ПП №719 — СПИК
    "реквизиты заключения о выполнении (полном, частичном) или невыполнении инвестором обязательств",
)


class TestClosedList(unittest.TestCase):
    def test_exactly_three_documents_with_all_fields(self):
        """Перечень закрыт экспертами дважды: СТ-1, Акт экспертизы, сведения о реестровой записи."""
        docs = documents_ref.CONFIRMING_DOCUMENTS
        self.assertEqual(len(docs), 3)
        for d in docs:
            for field in ("name", "when", "source"):
                self.assertTrue(d.get(field), f"{d.get('name')!r}: пустое поле {field}")

    def test_applicability_is_not_interchangeable(self):
        """Акт экспертизы — для продукции В приложении, СТ-1 — ВНЕ его. Подмена недопустима.

        Эксперт зафиксировал это отдельным предложением кейса: «Сертификат СТ-1 и Акт экспертизы —
        разные инструменты с разной сферой применения, и подменять один другим в ответе заявителю
        недопустимо»."""
        by_name = {d["name"]: d for d in documents_ref.CONFIRMING_DOCUMENTS}
        akt = next(v for k, v in by_name.items() if "Акт экспертизы" in k)
        st1 = next(v for k, v in by_name.items() if "СТ-1" in k)
        self.assertIn("ВКЛЮЧЕНА", akt["when"])
        self.assertIn("НЕТ в приложении", st1["when"])

    def test_context_block_says_the_list_is_closed(self):
        block = documents_ref.documents_context_block()
        self.assertIn("ЗАКРЫТЫЙ", block)
        for d in documents_ref.CONFIRMING_DOCUMENTS:
            self.assertIn(d["name"], block)

    def test_explanation_comes_only_when_the_user_raised_it(self):
        """⚠⚠ Разъяснение отвечает на НЕЗАДАННЫЙ вопрос — и потому условное.

        Первая редакция клала его в контекст всегда. Живой прогон на бою 25.08.2026: на вопрос
        «какие документы подготовить» ответ содержал строку «Документа «заключение ТПП» не
        существует» — пользователю, который про этот документ не спрашивал. Правка, задуманная
        чтобы термин ИСЧЕЗ из ответов, начала его туда ПРИНОСИТЬ (`EV12`: слово, живущее в
        контексте, модель воспроизводит).
        """
        plain = documents_ref.documents_context_block(
            "какие документы нужно подготовить для подтверждения производства")
        self.assertNotIn("не существует", plain,
                         "разъяснение приехало на вопрос, где про заключение не спрашивали")
        self.assertIn("ЗАКРЫТЫЙ", plain, "закрытый перечень обязан быть ВСЕГДА")
        for d in documents_ref.CONFIRMING_DOCUMENTS:
            self.assertIn(d["name"], plain)

    def test_explanation_arrives_when_asked(self):
        """Обратная половина: спросили — объясняем. Без неё правка «починила» бы дефект,
        просто выбросив разъяснение совсем."""
        for q in ("нужно ли мне заключение ТПП для внесения в реестр",
                  "что такое экспертное заключение ТПП"):
            block = documents_ref.documents_context_block(q)
            self.assertIn("не существует", block, q)
            self.assertIn("экспертное заключение", block, q)
            self.assertIn("ИСКЛЮЧЕНИЯ реестровой записи", block, q)

    def test_explanation_is_a_third_of_the_block(self):
        """Цена условности — измеренная, а не предполагаемая."""
        plain = len(documents_ref.documents_context_block("какие документы нужны"))
        full = len(documents_ref.documents_context_block("нужно ли заключение ТПП"))
        self.assertLess(plain, full)
        self.assertGreater(full - plain, 300, "разъяснение весит меньше, чем указано в решении")
    def test_table_is_markdown_and_lists_everything(self):
        t = documents_ref.documents_table()
        self.assertIn("|---|---|---|", t)
        self.assertEqual(t.count("\n| "), len(documents_ref.CONFIRMING_DOCUMENTS) + 1,
                         "строк данных должно быть ровно по числу документов плюс шапка")


class TestGuard(unittest.TestCase):
    """Предохранитель обязан ловить выдумку И НЕ бить по верному тексту."""

    def test_positive_control_catches_real_hallucinations(self):
        for text in REAL_HALLUCINATIONS:
            self.assertTrue(documents_ref.unverified_documents(text),
                            f"гард пропустил реальную выдумку: {text!r}")

    def test_negative_control_spares_legitimate_conclusions(self):
        """⚠ Предохранитель, бьющий по ВЕРНОМУ тексту, дороже отсутствующего.

        Урок куплен выкаткой 20.08: `grep` по имени удалённой константы поймал её в комментарии,
        объясняющем удаление, и остановил верную выкатку. Здесь тот же риск: «экспертное
        заключение» — действующий документ п. 36-40 Правил."""
        for text in LEGITIMATE_FROM_CORPUS:
            self.assertEqual(documents_ref.unverified_documents(text), [],
                             f"ложное срабатывание на законном тексте: {text[:70]!r}")

    def test_clean_answer_passes(self):
        clean = ("Для продукции из приложения к ПП №719 оформляется акт экспертизы уполномоченной "
                 "ТПП [1]; для продукции вне приложения — сертификат СТ-1 [2].")
        self.assertEqual(documents_ref.unverified_documents(clean), [])

    def test_same_phrase_reported_once(self):
        doubled = "заключение ТПП нужно получить. Затем заключение ТПП подаётся через ГИСП."
        self.assertEqual(len(documents_ref.unverified_documents(doubled)), 1)

    def test_negative_control_guard_is_not_vacuous(self):
        """Мутация: гард, который никогда не срабатывает, прошёл бы все проверки выше, кроме этой.

        «Ноль срабатываний» и «инструмент слеп» снаружи неразличимы — у предохранителя, чьё «нет»
        является результатом, обязан быть положительный контроль."""
        self.assertTrue(documents_ref.unverified_documents(
            "потребуется заключение ТПП о подтверждении производства"))
        self.assertFalse(documents_ref.unverified_documents(
            "ТПП РФ формирует экспертное заключение"))


# Дословные фрагменты ОТВЕТОВ С БОЯ, снятые 25.08.2026 сразу после выкатки v0.5.0-test15.
# Оракул взят ИЗВНЕ — это реальное поведение сервиса, а не сочинённый пример.
REAL_DENIALS_FROM_PRODUCTION = (
    "Перечень подтверждающих документов закрытый: производство подтверждается актом экспертизы "
    "ТПП (для продукции из приложения) или сертификатом СТ-1 (для продукции вне приложения). "
    "Документа «заключение ТПП» не существует.",
    "Перечень подтверждающих документов закрытый: производство подтверждает именно акт экспертизы "
    "ТПП (для продукции из приложения) или сертификат СТ-1 (для продукции вне приложения). "
    "Документа «заключение ТПП» не существует.",
)


class TestGuardSparesTheDenial(unittest.TestCase):
    """⚠⚠ ОТРИЦАНИЕ — ЭТО ЦЕЛЬ `K15`, А НЕ ЕЁ НАРУШЕНИЕ.

    Найдено живым прогоном на бою 25.08.2026, сразу после выкатки: гард сработал на ЛУЧШЕМ из
    возможных ответов — том, где перечень назван закрытым и прямо сказано, что «заключения ТПП»
    не существует. Справочник кладёт разъяснение в контекст, модель им пользуется — а гард за это
    наказывал и писал WARNING в админ-журнал, где эксперт видел «ответ называет несуществующий
    документ» на самых правильных ответах.

    ⚠ Почему не поймалось до выкатки: локально термин не появился НИ РАЗУ за четыре прогона
    (и кейсы были выключены), на бою — 2 раза из 5. Разброс генерации ловится повтором, а не
    одним прогоном.
    """

    def test_denial_is_not_a_defect(self):
        for text in REAL_DENIALS_FROM_PRODUCTION:
            self.assertEqual(documents_ref.unverified_documents(text), [],
                             f"гард ударил по ВЕРНОМУ ответу: {text[-80:]!r}")

    def test_negative_control_recommendation_still_caught(self):
        """Обратная половина: без отрицания рядом — по-прежнему дефект.

        Без неё правка «починила» бы гард, сделав его слепым: пропускать всё — тоже способ не
        давать ложных срабатываний."""
        for text in REAL_HALLUCINATIONS:
            self.assertTrue(documents_ref.unverified_documents(text),
                            f"гард ослеп на реальной выдумке: {text!r}")

    def test_denial_window_is_bounded(self):
        """Отрицание засчитывается только РЯДОМ, а не где-то в ответе.

        Иначе одно «не существует» в начале длинного ответа оправдывало бы рекомендацию
        несуществующего документа в конце."""
        far = ("Документа такого не существует. " + "текст " * 120
               + "Поэтому потребуется заключение ТПП о подтверждении производства.")
        self.assertTrue(documents_ref.unverified_documents(far),
                        "отрицание за 700 символов не должно оправдывать рекомендацию")

    def test_denial_forms_cover_real_wordings(self):
        for phrase in ("не существует", "нет такого документа", "не используй",
                       "такой документ отсутствует", "не применяется"):
            self.assertTrue(documents_ref._DENIAL.search(phrase), phrase)
        self.assertFalse(documents_ref._DENIAL.search("обычный текст ответа"))

    def test_denial_needs_a_subject_it_can_deny(self):
        """⚠⚠ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ, КОТОРОГО У ЭТОГО ТЕСТА НЕ БЫЛО (ревью захода 5.5).

        Половина «формы ловятся» зеленела бы и у регулярки, ловящей ВСЁ подряд. Голое «отсутству»
        именно такой и было: в домене 719 «при отсутствии продукции/сведений в реестре» — оборот
        частый, и он глушил гард на соседней выдумке."""
        for phrase in ("при отсутствии продукции в приложении",
                       "при отсутствии сведений в реестре"):
            self.assertFalse(documents_ref._DENIAL.search(phrase),
                             f"отрицание засчитано там, где отрицается не документ: {phrase!r}")


class TestReachesTheModel(unittest.TestCase):
    """Справочник, до контекста не доехавший, не работает — а сломается это молча."""

    def _procedural_prompt(self, query: str, rules: list[dict]) -> str:
        sent: list[str] = []

        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "Ответ [1]."})()})()]
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()

        def _create(*a, **k):
            sent.append(k["messages"][-1]["content"])
            return _Resp()

        class _Client:
            chat = type("Ch", (), {"completions": type(
                "Co", (), {"create": staticmethod(_create)})()})()

        orig_search, orig_client = pipeline_mod.search_rules, pipeline_mod._client
        pipeline_mod.search_rules = lambda *a, **k: rules
        pipeline_mod._client = lambda: _Client()
        try:
            pipeline_mod._answer_procedural(query, query)
        finally:
            pipeline_mod.search_rules, pipeline_mod._client = orig_search, orig_client
        return sent[0]

    RULES = [{"doc_type": "tpp_order_52", "source_anchor": "Приказ ТПП РФ №52, п. 4.2.1",
              "text": "Заявитель представляет документы."}]

    def test_document_question_gets_the_reference(self):
        prompt = self._procedural_prompt("какие документы нужны для внесения в реестр", self.RULES)
        self.assertIn("СПРАВОЧНИК ПОДТВЕРЖДАЮЩИХ ДОКУМЕНТОВ", prompt)
        self.assertIn("Сертификат о происхождении товара формы СТ-1", prompt)

    def test_negative_control_other_topics_do_not_pay_for_it(self):
        """Мутация: справочник обязан приходить ТОЛЬКО на документный вопрос.

        Без этой половины предыдущий тест остался бы зелёным и в случае, когда блок подмешивается
        в каждый процедурный ответ, — а это лишние ~700 символов контекста на вопросе о сроках."""
        prompt = self._procedural_prompt("сколько дней рассматривают заявление", self.RULES)
        self.assertNotIn("СПРАВОЧНИК ПОДТВЕРЖДАЮЩИХ ДОКУМЕНТОВ", prompt)

    def test_product_path_gets_reference_even_when_points_not_found(self):
        """⚠ Именно этот случай и рождал выдумку.

        Прежде блок документов строился ТОЛЬКО из найденных пунктов Приказа №52: не нашлись —
        модель отвечала про состав документов по памяти. Справочник детерминированный, от поиска
        не зависит и обязан приходить в обоих случаях."""
        from app.core.prompts import build_navigator_user_prompt
        block = documents_ref.documents_context_block()
        user = build_navigator_user_prompt("какие документы нужны для насосов", "КОНТЕКСТ",
                                           documents=block)
        self.assertIn("СПРАВОЧНИК ПОДТВЕРЖДАЮЩИХ ДОКУМЕНТОВ", user)
        self.assertIn("Акт экспертизы уполномоченной ТПП", user)

    def test_guard_is_wired_into_the_answer(self):
        """Гард доступен в модуле — этого мало; он обязан звучать на пути ответа."""
        self.assertIn("phantom_documents", pipeline_mod.Answer.__dataclass_fields__)
        sent: list[str] = []

        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {
                "content": "Вам потребуется заключение ТПП о подтверждении производства [1]."})()})()]
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()

        class _Client:
            chat = type("Ch", (), {"completions": type(
                "Co", (), {"create": staticmethod(lambda *a, **k: _Resp())})()})()

        orig_search, orig_client = pipeline_mod.search_rules, pipeline_mod._client
        pipeline_mod.search_rules = lambda *a, **k: self.RULES
        pipeline_mod._client = lambda: _Client()
        try:
            ans = pipeline_mod._answer_procedural("какие документы нужны", "какие документы нужны")
        finally:
            pipeline_mod.search_rules, pipeline_mod._client = orig_search, orig_client
        self.assertTrue(ans.phantom_documents,
                        "выдуманный документ в ответе обязан быть помечен, а не уехать молча")


class TestPromptRule(unittest.TestCase):
    RULE_MARK = "3б. СПРАВОЧНИК ДОКУМЕНТОВ — ПЕРЕЧЕНЬ ЗАКРЫТЫЙ"

    def _rule(self) -> str:
        i = PROCEDURAL_SYSTEM_PROMPT.index(self.RULE_MARK)
        return PROCEDURAL_SYSTEM_PROMPT[i:PROCEDURAL_SYSTEM_PROMPT.index("4. СТИЛЬ:", i)]

    def test_rule_closes_the_list(self):
        rule = self._rule()
        self.assertIn("перечень ИСЧЕРПЫВАЕТСЯ", rule)
        self.assertIn("НЕ придумывай других документов", rule)

    def test_rule_handles_the_user_naming_a_phantom(self):
        """Пользователь сам приносит несуществующее название — соглашаться нельзя."""
        rule = self._rule()
        self.assertIn("такого", rule)
        self.assertIn("Не соглашайся с", rule)

    def test_rule_keeps_applicability_apart(self):
        self.assertIn("путать их нельзя", self._rule())

    def test_procedural_prompt_does_not_seed_the_phantom_term(self):
        """⚠ `EV12`: запрет слова не держится, пока слово живёт в самом промпте (0.83 против 0.98).

        Поэтому процедурное правило закрывает перечень ПОЛОЖИТЕЛЬНО и термин не называет; сам
        термин приходит один раз, как факт с разъяснением, — в блоке справочника."""
        self.assertNotIn("заключение ТПП", PROCEDURAL_SYSTEM_PROMPT)
        self.assertNotIn("заключения ТПП", PROCEDURAL_SYSTEM_PROMPT)

    def test_known_debt_navigator_rule_still_names_the_term(self):
        """Долг, зафиксированный намеренно: правило 1е товарного промпта термин НАЗЫВАЕТ.

        Снимать его вслепую нельзя — это правка поведения, её надо мерить (EV12 мерила другое
        слово). Гард теперь такие случаи ловит, поэтому сначала замер, потом снятие. Тест
        существует, чтобы долг не забылся и чтобы его снятие было ОСОЗНАННЫМ: когда правило 1е
        перепишут, этот тест покраснеет и заставит обновить решение."""
        self.assertIn("заключение ТПП", NAVIGATOR_SYSTEM_PROMPT,
                      "правило 1е переписали — обнови решение по EV12 и этот тест")

    def test_negative_control_checks_fail_without_the_rule(self):
        i = PROCEDURAL_SYSTEM_PROMPT.index(self.RULE_MARK)
        j = PROCEDURAL_SYSTEM_PROMPT.index("4. СТИЛЬ:", i)
        mutated = PROCEDURAL_SYSTEM_PROMPT[:i] + PROCEDURAL_SYSTEM_PROMPT[j:]
        for phrase in ("перечень ИСЧЕРПЫВАЕТСЯ", "НЕ придумывай других документов",
                       "Не соглашайся с", "путать их нельзя"):
            self.assertNotIn(phrase, mutated,
                             f"{phrase!r} живёт вне правила 3б — проверка стережёт не то место")


if __name__ == "__main__":
    unittest.main()
