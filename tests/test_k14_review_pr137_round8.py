# -*- coding: utf-8 -*-
"""Сторожа находок ВОСЬМОГО раунда ревью PR #137 — блок, не зависящий от развилки по маршруту.

Покрыто: HIGH-1 (сумма принималась за код и ПОБЕЖДАЛА настоящий), HIGH-5 («позиция» в списке
«это НЕ код» роняла голый четырёхзначный код), HIGH-6б (у `/navigate` не было верхнего предела
длины), MED-5 (срез по ASCII-пробелу не видел NBSP).

⚠⚠ ПОЧЕМУ ЗДЕСЬ ТАК МНОГО ПОЛОЖИТЕЛЬНЫХ КОНТРОЛЕЙ. Это место чинили ТРИЖДЫ, и каждая редакция
ломала предыдущую: раунд 6 объявил «деньги перед числом → не код» и уронил «по контракту ТН ВЭД
8403»; раунд 7 сузил правило и вернул победу суммы над настоящим кодом. Оба раза правка была
верна ЛОКАЛЬНО и неверна на радиусе. Поэтому каждый сторож ниже идёт в паре с контролем из того
раунда, чью правку он переписывает, — иначе четвёртая редакция сломает вторую так же молча.
"""

import unittest

from app.rag.okpd2_ref import extract_tnved_position


class TestMoneyNeverBeatsARealCode(unittest.TestCase):
    """HIGH-1: сумма доезжала в промпт дословно под «использовать ТОЛЬКО это значение»."""

    def test_sum_loses_to_a_clean_code(self):
        # ⚠ Ровно четыре строки из отчёта раунда 8. Прежде каждая возвращала СУММУ.
        for text, want in (
            ("по контракту ТН ВЭД 250000 за товарную позицию 8703", "8703"),
            ("оборот по ТН ВЭД 3500000 и код 8703", "8703"),
            ("стоимость по ТН ВЭД 350000, товарная позиция 8703", "8703"),
            ("сумма по ТН ВЭД 250000, код 8703", "8703"),
        ):
            with self.subTest(text=text):
                self.assertEqual(extract_tnved_position(text), want)

    def test_positive_control_round7_household_contract_still_gives_the_code(self):
        """Контроль раунда 7: «по контракту» — бытовая лексика, код терять нельзя.

        Ради этого случая раунд 7 и сужал безусловный отказ раунда 6. Ранг вместо фильтра обязан
        сохранить его: затенённый кандидат выигрывает у ПУСТОТЫ, проигрывая лишь чистому."""
        self.assertEqual(extract_tnved_position("по контракту ТН ВЭД 8403 какие условия"), "8403")

    def test_positive_control_money_word_already_owns_a_nearer_number(self):
        """Сумма названа своим числом — следующий код чист. Без этой оговорки правка уронила бы
        положительный контроль раунда 7 («сумма контракта 250000, ТН ВЭД 8403» → 8403).

        ⚠ Этот контроль НЕ различает оговорку «своё число уже есть»: здесь 250000 отсекается
        жёстким фильтром, конкурента у 8403 не остаётся, и он берётся при любом ранге. Мутацией
        оговорку ловит тест ниже — разделено намеренно, чтобы слепота не пряталась в наборе."""
        self.assertEqual(extract_tnved_position("сумма контракта 250000, ТН ВЭД 8403"), "8403")

    def test_money_word_with_its_own_number_does_not_shadow_a_later_code(self):
        """Различитель оговорки «у слова о деньгах своё число уже есть» — КОНКУРЕНЦИЯ кандидатов.

        «сумма по ТН ВЭД 250000, код 8703»: у 250000 между словом и числом маркер и НЕТ цифры —
        затенён; у 8703 между «сумма» и числом стоит 250000 — сумма названа, кандидат чист.
        Без оговорки оба затенены, и побеждает ближайший к маркеру, то есть СУММА. Это ровно
        HIGH-1: в промпт дословно уезжает «Код ТН ВЭД 250000», а настоящий 8703 выброшен."""
        self.assertEqual(extract_tnved_position("сумма по ТН ВЭД 250000, код 8703"), "8703")

    def test_negative_control_round6_bare_sum_is_still_not_a_code(self):
        """Контроль раунда 6: голая сумма кодом не становится — правка ранга это не ослабляет."""
        self.assertIsNone(extract_tnved_position("сумма контракта 250000"))

    def test_negative_control_year_is_still_not_a_code(self):
        self.assertIsNone(extract_tnved_position("какие условия по ТН ВЭД в 2026 году"))
        self.assertEqual(
            extract_tnved_position("в 2026 году какие условия для кода ТН ВЭД 8403"), "8403")


class TestCounterWordDoesNotDropABareCode(unittest.TestCase):
    """HIGH-5: `позиц|единиц` лежали в списке «это НЕ код» и роняли код целиком."""

    def test_bare_code_survives_a_quantity_counter(self):
        for text, want in (
            ("ТН ВЭД 1902 позиций", "1902"),          # 1902 — НАСТОЯЩАЯ товарная позиция
            ("ТН ВЭД 8429 единиц техники", "8429"),
            ("какие условия для ТН ВЭД 8403 позиция", "8403"),
            ("код ТН ВЭД 840310 позиций", "840310"),
        ):
            with self.subTest(text=text):
                self.assertEqual(extract_tnved_position(text), want)

    def test_positive_control_counter_still_trims_a_glued_number(self):
        """Счётчик обязан ОТСЕКАТЬ лишнюю группу — вторая работа списка, её ломать нельзя.

        ⚠ «8403 10» это ДРУГАЯ строка Перечня, чем «8403» (у 2106, 3806, 8521, 8544, 4012 есть
        и родительская, и подсубпозиционные) — то есть склейка молча СУЖАЛА бы ключ."""
        self.assertEqual(extract_tnved_position("ТН ВЭД 8403 12 шт"), "8403")
        self.assertEqual(extract_tnved_position("код ТН ВЭД 8403 10 позиций"), "8403")

    def test_negative_control_money_unit_still_rejects_a_lone_number(self):
        """Денежная и временная единица опровергают группу и в одиночку — иначе правка HIGH-5
        вернула бы сумму в коды с другой стороны."""
        self.assertIsNone(extract_tnved_position("ТН ВЭД 250000 рублей"))
        self.assertIsNone(extract_tnved_position("ТН ВЭД 2026 года"))

    def test_positive_control_round1_full_code_is_never_truncated(self):
        """Класс HIGH первого раунда: срез хвоста у длинного кода — переход к ЧУЖОЙ позиции."""
        self.assertEqual(extract_tnved_position("ТН ВЭД 8544 70 000 0 позиции"), "8544 70 000 0")
        self.assertEqual(extract_tnved_position("0710 40 000 единиц по ТН ВЭД"), "0710 40 000")


class TestNonBreakingSpaceIsAWhitespace(unittest.TestCase):
    """MED-5: срез искал `rfind(" ")` и при NBSP уходил в ветку «одна группа» → None.

    NBSP — норма для кодов, скопированных из Word, выгрузок ГИСП и деклараций. Это тот же класс
    «предохранитель, который не может сработать никогда», что нашёл сам раунд 7 одной функцией выше.
    """

    def test_nbsp_between_groups_does_not_lose_the_code(self):
        self.assertEqual(extract_tnved_position("ТН ВЭД 8403\xa012 шт"), "8403")

    def test_positive_control_ascii_space_behaves_the_same(self):
        self.assertEqual(extract_tnved_position("ТН ВЭД 8403 12 шт"), "8403")


class TestNavigateHasAnUpperLengthBound(unittest.TestCase):
    """HIGH-6б: поле объявляло только `min_length`, и `/navigate` принимал вход любой длины.

    ⚠ Предел нужен и после починки самой квадратичности: соседний путь чата ограничен 2000 знаков
    с самого начала, и расхождение держалось лишь потому, что `/navigate` никто не мерил длинным
    входом. Проверяем КОНТРАКТ СХЕМЫ, а не быстродействие — время прогона порогом служить не может.
    """

    def test_overlong_query_is_rejected(self):
        from pydantic import ValidationError

        from app.api.schemas import NavigateRequest

        with self.assertRaises(ValidationError):
            NavigateRequest(query="условия " * 4000)

    def test_positive_control_normal_query_is_accepted(self):
        from app.api.schemas import NavigateRequest

        self.assertEqual(NavigateRequest(query="какие условия для ТН ВЭД 8403").limit, 5)

    def test_bound_matches_the_chat_path(self):
        """Два пути в один движок обязаны иметь ОДИН предел — иначе он снова разъедется."""
        from app.api.chat import ChatRequest
        from app.api.schemas import NavigateRequest

        self.assertEqual(NavigateRequest.model_fields["query"].metadata[-1].max_length,
                         ChatRequest.model_fields["message"].metadata[-1].max_length)


if __name__ == "__main__":
    unittest.main()
