"""Проверка вопроса на данные, которые нельзя отдавать во внешнюю модель (офлайн).

Два риска у такой защиты, и оба здесь покрыты. Первый — пропустить настоящий документ. Второй,
более коварный, — заблокировать рабочий вопрос: техтекст полон длинных чисел, и грубая регулярка
превращает сервис в неработающий. Поэтому ложные срабатывания тестируются подробнее, чем истинные.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.sensitive import KIND_NAMES, detect, message  # noqa: E402


class TestDetectsRealData(unittest.TestCase):
    def test_snils_with_valid_checksum(self):
        self.assertEqual(detect("СНИЛС 112-233-445 95"), ["snils"])

    def test_snils_with_broken_checksum_is_ignored(self):
        """Одиннадцать цифр без верных контрольных разрядов — не документ, а просто число."""
        self.assertNotIn("snils", detect("номер 112-233-445 11"))

    def test_inn_of_person(self):
        self.assertEqual(detect("ИНН 500100732259"), ["inn_person"])

    def test_passport_only_next_to_the_word(self):
        self.assertIn("passport", detect("паспорт 4611 123456"))
        self.assertNotIn("passport", detect("партия 4611 123456 штук"))

    def test_card_by_luhn(self):
        self.assertIn("card", detect("оплата картой 4111 1111 1111 1111"))
        self.assertNotIn("card", detect("инвентарный 4111 1111 1111 1112"))

    def test_phones_in_common_forms(self):
        for t in ("+7 (4712) 55-44-33", "8 (4712) 554433", "+79101234567", "тел. 8-910-123-45-67"):
            self.assertIn("phone", detect(t), t)

    def test_email(self):
        self.assertIn("email", detect("пишите на ivanov@kursk-tpp.ru"))

    def test_restricted_markings(self):
        for t in ("для служебного пользования", "это коммерческая тайна", "совершенно секретно"):
            self.assertIn("restricted", detect(t), t)


class TestDoesNotBlockWork(unittest.TestCase):
    """Рабочие вопросы обязаны проходить: ложная блокировка дороже пропущенной фамилии."""

    CASES = [
        "код ТН ВЭД 8544 49 910 0, какие требования?",     # 8 + 10 цифр — не телефон
        "ТН ВЭД 8471 30 000 0 требования по 719",
        "ОКПД2 28.13.14.110 насосы центробежные",
        "позиция 30.11.40.000, порог 180 баллов с 1 января 2022 г.",
        "ГОСТ 32601-2013 на насосы",
        "приказ ТПП РФ №52, пункт 4.2.1",
        "ред. от 22.07.2026 N 923",
        "ИНН нашей организации 4629045050",               # 10 цифр — юрлицо, не ПДн
        "выпущено 8544 49 единиц продукции",
        "заявка от 2026-08-13 на 1381 позицию",
    ]

    def test_no_false_positives_on_domain_text(self):
        for t in self.CASES:
            self.assertEqual(detect(t), [], f"ложная блокировка: {t}")


class TestMessage(unittest.TestCase):
    def test_names_what_was_found_and_what_to_do(self):
        m = message(["snils"])
        self.assertIn("СНИЛС", m)
        self.assertIn("уберите", m.lower())

    def test_restricted_has_its_own_wording(self):
        """Для грифов формулировка другая: там речь не о персональных данных."""
        m = message(["restricted"])
        self.assertIn("ограниченного доступа", m)
        self.assertNotIn("персональные данные", m)

    def test_several_kinds_listed_readably(self):
        m = message(["snils", "phone"])
        self.assertIn("СНИЛС", m)
        self.assertIn("номер телефона", m)
        self.assertIn(" и ", m)


class TestClientAndServerRulesMatch(unittest.TestCase):
    """Правила продублированы в браузере — там проверка мгновенная и текст не покидает компьютер.

    Дубль легко разъезжается: серверный список поменяют, клиентский забудут, и пользователь получит
    отказ уже после отправки. Сверяем виды данных и наличие контрольных сумм."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app" / "web" / "static" / "chat.js").read_text(encoding="utf-8")
        # именно блок правил: слово kind встречается и в полях обратной связи
        start = cls.js.index("const SENSITIVE_RULES")
        cls.rules = cls.js[start:cls.js.index("];", start)]

    def test_same_kinds_on_both_sides(self):
        js_kinds = set(re.findall(r'kind:\s*"([a-z_]+)"', self.rules))
        self.assertEqual(js_kinds, set(KIND_NAMES), "наборы видов данных разошлись")

    def test_same_user_facing_names(self):
        js_names = set(re.findall(r'name:\s*"([^"]+)"', self.rules))
        self.assertEqual(js_names, set(KIND_NAMES.values()), "названия для пользователя разошлись")

    def test_client_verifies_checksums_too(self):
        for fn in ("snils", "inn12", "luhn"):
            self.assertIn(f'kind === "{fn}"', self.js, f"на клиенте нет проверки {fn}")

    def test_client_blocks_send_and_keeps_the_text(self):
        """Вопрос должен остаться в поле: человеку его редактировать, а не набирать заново."""
        self.assertIn("showInputBlock(sensitiveMessage(found))", self.js)
        self.assertIn("return;   // вопрос остаётся в поле", self.js)

    def test_server_rejection_handled_on_client(self):
        """Клиентскую проверку можно обойти — ответ 422 обязан быть обработан."""
        self.assertIn("handleRejected", self.js)
        self.assertIn("r.status === 422", self.js)


if __name__ == "__main__":
    unittest.main()
