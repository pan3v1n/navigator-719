"""Сторожа седьмого раунда ревью PR #137 (`K14` #35).

Пять находок, две HIGH:

1-2 (HIGH). СЛОВА-СПУТНИКИ ТН ВЭД БЫЛИ ОБЩЕЙ ТАМОЖЕННОЙ ЛЕКСИКОЙ. `услови`, `сертификат`,
   `обработк` встречаются в вопросах, к 719 отношения не имеющих («условия ввоза», «сертификат
   соответствия», «условия хранения»), а у процедурной ветки НЕТ гейта вне-сферы: вопрос про
   таможенную декларацию получал ответ, синтезированный из Правил СНГ.
   Попутно словарь жил в ТРЁХ копиях — сведён в одну (`topics.ST1_COMPANION`).

   ⚠⚠ ПЕРВАЯ РЕДАКЦИЯ ПРАВКИ (только фразы второго ключа) БЫЛА ЗАМЕРЕНА НА СВОЁМ СПИСКЕ,
   выглядела чистой — и уронила ШЕСТЬ тестов прежних раундов: две формулировки СТ-1 закреплены
   как контракт, одна из них дословно экспертная. Радиус правки меряется на радиусе ДЕФЕКТА.
   Замер трёх состояний (12 таможенных вопросов + 11 формулировок СТ-1 + путь `T9`):
   стволы 11/1 · только фразы 0/3 · стволы + закрытый список дисквалификаторов **0/1**.

3 (HIGH). СЛОВО О ДЕНЬГАХ СЪЕДАЛО КОД ЗА МАРКЕРОМ. Проверка «есть ли между словом и числом
   признак товарной позиции» смотрела в ПУСТУЮ строку: `m.end()` указывал на конец всего матча,
   то есть ровно на начало числа. Предохранитель не мог сработать никогда.

4. ПРИКЛЕИВШЕЕСЯ ЧИСЛО-СЧЁТЧИК уносило код: «ТН ВЭД 8403 10 позиций» → «8403 10» (чужая
   субпозиция вместо позиции 8403).

5. ГОЛОВА РАЗДЕЛА С ПУНКТАМИ НЕ ИНДЕКСИРОВАЛАСЬ. Фолбэк покрывал только раздел БЕЗ единого
   пункта, а у приложения 4 Соглашения нумерация начинается со второго уровня (2.1) — вводная
   и весь «1. Общие положения» не попадали никуда.

⚠ Каждое утверждение проверено МУТАЦИЕЙ: снятие своего предохранителя роняет свой тест.
⚠ Офлайн: Qdrant и ключ DeepSeek не нужны (класс `O3` #104).
"""
from __future__ import annotations

import io
import json
import pathlib
import re
import sys
import tokenize
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import procedural, topics                      # noqa: E402
from app.rag.okpd2_ref import extract_tnved_position        # noqa: E402
from app.tools.navigator import extract_okpd2               # noqa: E402
from tests._source_tools import strip_comments              # noqa: E402


def route(q: str) -> bool:
    """Путь до гейта, а не гейт напрямую: дефект жил на стыке (урок раундов 1-2)."""
    return procedural.is_procedural(q, has_code=bool(extract_okpd2(q)))


# Вопросы ТАМОЖЕННЫЕ: код ТН ВЭД есть, к 719 отношения нет.
# ⚠⚠ ЧИТАЕТСЯ ИЗ САМОГО НАБОРА, А НЕ КОПИРУЕТСЯ (находка сводного прохода десятого раунда).
# Здесь лежал замороженный кортеж из 12 строк с подписью «набор замера находок 1-2 целиком», и
# он разошёлся с файлом: раунд 9 добавил в набор два экспортных пробника (101, 102), а кортеж
# остался двенадцатистрочным — то есть у обоих пробников не было НИ ОДНОГО сторожа уровня
# МАРШРУТА (в `test_k14_round8_relevance_gate` проверяется только тема). Класс «две таблицы про
# одно» — ровно тот, из-за которого раунд 6 получил находку, а раунд 8 — HIGH-2.
_OFF_DOMAIN_SET = ROOT / "scripts" / "eval_offdomain_tnved.json"


def off_domain(t: unittest.TestCase) -> tuple[str, ...]:
    """Вопросы набора — ЛЕНИВО и с внятным провалом (находка 13 одиннадцатого раунда).

    ⚠⚠ Здесь стоял `OFF_DOMAIN = tuple(json.loads(...))` НА УРОВНЕ МОДУЛЯ. Битый или
    отсутствующий файл ронял ИМПОРТ, а с ним исчезали все сторожа файла разом — включая те,
    что этот же раунд и заводил, — и наружу выходила ошибка JSON, не называющая, ЧТО перестало
    охраняться. Сравните `st1_ref._data()`, который на порченом файле осознанно ДЕГРАДИРУЕТ.
    ⚠ Провал (а не пропуск) выбран намеренно: файл коммитится в репозиторий, его отсутствие —
    дефект выкатки. Разница с прежним поведением в том, что провал ИМЕНОВАННЫЙ и локальный."""
    try:
        cases = json.loads(_OFF_DOMAIN_SET.read_text(encoding="utf-8"))["cases"]
    except (OSError, ValueError, KeyError) as e:
        t.fail(f"не читается {_OFF_DOMAIN_SET.name} ({e.__class__.__name__}: {e}) — "
               "класс «таможня + ТН ВЭД» перестал охраняться целиком")
    return tuple(c["query"] for c in cases)
# Вопросы СТ-1: обязаны остаться на процедурной ветке. Без них тест прошёл бы и на
# ПОЛНОСТЬЮ УДАЛЁННОМ втором ключе — «ничего не маршрутизируется» тоже даёт ноль утечек.
ST1_ALIVE = (
    "какие условия достаточной переработки для кода ТН ВЭД 8403",
    "мой код ТН ВЭД 8501, какие условия достаточной переработки",
    "какие условия достаточной переработки для форвардеров ТН ВЭД 8704",
    "что такое критерий достаточной обработки/переработки",
    "что такое кумулятивный принцип",
    # ⚠ Закреплены прежними раундами как КОНТРАКТ — их и уронила первая редакция правки.
    "мой код ТН ВЭД 8544 49 910 0, какие условия",
    "какие условия по ТН ВЭД 8403 в редакции от 01.07.2026",
)


class TestOffDomainCustomsStaysOut(unittest.TestCase):
    """Находки 1-2: таможенный вопрос не уходит на процедурную ветку и не берёт тему СТ-1."""

    def test_no_off_domain_leak_into_procedural(self):
        leaked = [q for q in off_domain(self) if route(q)]
        self.assertEqual(leaked, [], f"таможенный вопрос ушёл процедурной веткой: {leaked}")

    def test_no_off_domain_leak_into_st1_topic(self):
        wrong = [q for q in off_domain(self) if topics.classify(q) == topics.ST1_ORIGIN]
        self.assertEqual(wrong, [], f"таможенному вопросу назначена тема СТ-1: {wrong}")

    def test_positive_control_st1_still_routes(self):
        lost = [q for q in ST1_ALIVE if not route(q)]
        self.assertEqual(lost, [], f"потерян вопрос СТ-1: {lost}")

    def test_positive_control_st1_keeps_topic(self):
        lost = [q for q in ST1_ALIVE if topics.classify(q) != topics.ST1_ORIGIN]
        self.assertEqual(lost, [], f"вопрос СТ-1 потерял свою тему: {lost}")

    def test_the_class_lives_in_a_set_and_the_sweep_reads_it(self):
        """⚠⚠ Предохранитель переживает сессию только в НАБОРЕ, а не в тесте.

        Свип показывал по этому классу «0» не потому, что чисто, а потому, что вопросов с
        ТН ВЭД в нём не было НИ ОДНОГО. Положительный контроль снят: на коде до правки этот
        набор даёт 9 утечек из 12, после правки — 0.

        ⚠ Файл ОТДЕЛЬНЫЙ, не `eval_golden_negative.json`: тот кормит ПОРОГОВУЮ метрику отказа,
        и двенадцать легко проходящих кейсов подняли бы её без единой правки кода."""
        cases = json.loads((ROOT / "scripts" / "eval_offdomain_tnved.json")
                           .read_text(encoding="utf-8"))["cases"]
        self.assertGreaterEqual(len(cases), 10, "класс «таможня/ТН ВЭД» усох")
        self.assertTrue(all(c["in_scope"] is False for c in cases))
        self.assertTrue(all("ВЭД" in c["query"] for c in cases),
                        "в наборе появился вопрос без второго ключа — он мерит не тот класс")
        sweep = (ROOT / "scripts" / "eval_routing.py").read_text(encoding="utf-8")
        self.assertIn("eval_offdomain_tnved.json", sweep,
                      "набор есть, но свип его не читает — класс снова не мерится")


class TestCompanionVocabularyHasOneDefinition(unittest.TestCase):
    """⚠⚠ Инвариант согласованности (урок раунда 6): копий словаря быть не должно.

    Правка одной из трёх копий молча расходилась с остальными. Проверяем ИСХОДНИКИ, потому
    что шаблоны компилируются на импорте — подменять константу в рантайме уже поздно."""

    FRAGMENT = r"перечн\w*\s+услови"

    @staticmethod
    def _strip_comments(src: str) -> str:
        """Исходник БЕЗ комментариев. ⚠ Определение ОДНО — в `tests/_source_tools`.

        Раньше стриппер жил здесь, а соседний сторож раунда 10 считал СЫРОЙ текст (находка 7
        одиннадцатого раунда). Копировать его туда было нельзя — «две копии про одно» это класс
        HIGH-2 восьмого раунда, — поэтому определение вынесено, а метод оставлен точкой входа
        для тестов ниже."""
        return strip_comments(src)

    @classmethod
    def _code_only(cls, path) -> str:
        return cls._strip_comments(path.read_text(encoding="utf-8"))

    def test_single_literal_definition(self):
        top = self._code_only(ROOT / "app" / "rag" / "topics.py")
        self.assertEqual(top.count(self.FRAGMENT), 1,
                         "словарь спутников определён в `topics` не один раз")

    def test_stripper_removes_comments_and_keeps_code(self):
        """Отрицательный контроль к самому сторожу — НА СИНТЕТИКЕ, а не на прозе файла.

        ⚠⚠ Прежняя редакция требовала, чтобы фрагмент был НАЗВАН в комментарии `topics.py`
        (`raw count > 1`), и была зелёной только потому, что тот же коммит этот комментарий и
        добавил: на родительском `f0c19d8` сырых вхождений ровно одно, то есть там контроль был
        бы КРАСНЫМ. Проверка, привязанная к формулировке комментария, краснеет на законной
        переформулировке — на верном коде (находка 13 десятого раунда).
        Контроль обязан проверять СТРИППЕР, а не содержимое чужого файла."""
        fixture = (
            'X = r"' + self.FRAGMENT + '"  # хвостовой: ' + self.FRAGMENT + "\n"
            "# целая строка: " + self.FRAGMENT + "\n"
            'Y = "решётка # внутри литерала: ' + self.FRAGMENT + '"\n'
        )
        stripped = self._strip_comments(fixture)
        self.assertEqual(stripped.count(self.FRAGMENT), 2,
                         "стриппер снял не только комментарии либо снял не все")
        self.assertNotIn("хвостовой", stripped, "хвостовой комментарий не снят")
        self.assertNotIn("целая строка", stripped, "строка-комментарий не снята")
        self.assertIn("решётка # внутри литерала", stripped,
                      "стриппер порезал строковый литерал по знаку решётки")

    def test_stripper_survives_non_newline_line_breaks(self):
        """Находка 15 одиннадцатого раунда: модель строк обязана совпадать с моделью `tokenize`.

        `splitlines()` режет по `\\x0c`, `\\x0b`, `\\x85`, ` `; `tokenize` — только по `\\n`.
        Один такой знак ВЫШЕ по файлу сдвигал номера строк, и срез уезжал на чужую строку:
        хвостовой комментарий оставался в счёте (дефект находки 13 возвращался), а соседняя
        строка калечилась до неразбираемой — молча, без исключения.
        ⚠ Проверяется КАЖДЫЙ знак-разделитель `splitlines`, а не только форм-фид: иначе сторож
        охранял бы один символ из шести."""
        import ast
        for ch, name in (("\x0c", "form feed"), ("\x0b", "vertical tab"),
                         ("\x85", "NEL"), (" ", "line separator"),
                         (" ", "paragraph separator"), ("\x1c", "file separator")):
            with self.subTest(sep=name):
                fixture = (
                    f"# разрыв: {ch} конец\n"
                    'X = r"' + self.FRAGMENT + '"  # хвостовой: ' + self.FRAGMENT + "\n"
                    "Z = 1\n"
                )
                self.assertEqual(len(fixture.splitlines()), 4,
                                 f"{name} перестал быть разделителем для splitlines — "
                                 "пример выродился")
                stripped = self._strip_comments(fixture)
                self.assertEqual(stripped.count(self.FRAGMENT), 1,
                                 f"{name} сдвинул номера строк: хвостовой комментарий уцелел")
                self.assertNotIn("хвостовой", stripped)
                ast.parse(stripped)  # срез не уехал на чужую строку

    def test_procedural_has_no_own_copy(self):
        proc = self._code_only(ROOT / "app" / "rag" / "procedural.py")
        self.assertEqual(proc.count(self.FRAGMENT), 0,
                         "`procedural` завёл собственную копию словаря спутников")
        self.assertIn("topics.ST1_COMPANION", proc,
                      "`procedural` не ссылается на общее определение")

    def test_shared_constant_actually_reaches_the_gate(self):
        """Ссылка обязана быть ЖИВОЙ: слово из константы работает в маршруте."""
        self.assertIn("кумулятивн", topics.ST1_COMPANION)
        self.assertTrue(route("что такое кумулятивный принцип"))


class TestTopicVocabularyIsWiderByConstruction(unittest.TestCase):
    """⚠⚠ Асимметрия темы и маршрута ВОССТАНОВЛЕНА ЯВНО — при сведении копий её потеряли молча.

    В `topics` стоял ствол `требован`, в `procedural` — нет, и унификация усреднила их вместо
    того, чтобы расхождение перечислить.

    ⚠⚠⚠ ПРЕЖНЕЕ ОБОСНОВАНИЕ ЗДЕСЬ БЫЛО ЛОЖНЫМ И ПЕРЕЖИЛО СВОЁ ОПРОВЕРЖЕНИЕ (находка 9 десятого
    раунда). Стояло: «`classify` спрашивают ТОЛЬКО на процедурной ветке, то есть уже ЗА гейтом, —
    широкое слово там утечкой стать не может». Восьмой раунд признал эту фразу ложной и удалил её
    из `topics.py`, но здесь, в двадцати строках от теста, который её опровергает, она осталась —
    и следующий раунд, грепнувший обоснование, нашёл бы сперва опровергнутую формулировку.
    Как есть: `procedural._has_routing_topic` ЗОВЁТ `classify` и сам является одним из двух
    дизъюнктов гейта, а `pipeline` зовёт `classify` и на товарной ветке. Словарь темы КОРМИТ
    маршрут, поэтому расширять его без замера нельзя."""

    EXPERT_PHRASING = "укажите ваш код ТН ВЭД, распишите требования"

    def test_topic_vocabulary_extends_the_shared_one(self):
        """Расширение, а не форк: общий словарь обязан быть ПРЕФИКСОМ тематического."""
        self.assertTrue(topics.ST1_TOPIC_COMPANION.startswith(topics.ST1_COMPANION),
                        "тематический словарь перестал быть расширением общего")
        self.assertNotEqual(topics.ST1_TOPIC_COMPANION, topics.ST1_COMPANION,
                            "асимметрия снова потеряна — `требован` выпал из темы")

    def test_expert_phrasing_keeps_its_topic(self):
        self.assertEqual(topics.classify(self.EXPERT_PHRASING), topics.ST1_ORIGIN)

    def test_wider_word_does_not_widen_the_route(self):
        """⚠ Отрицательный контроль: попади `требован` в маршрут — сломался бы путь `T9`."""
        self.assertFalse(route(self.EXPERT_PHRASING))
        for q in ("подпадает ли под требования продукция ТН ВЭД 8479 89 970 8",
                  "мой код ТН ВЭД 8471 30 000 0, какие требования к продукции"):
            with self.subTest(q=q):
                self.assertFalse(route(q), "путь T9 ушёл на процедурную ветку")

    def test_topic_dictionary_actually_reaches_the_gate(self):
        """⚠⚠ СТОРОЖ ВЫШЕ БЫЛ СЛЕПЫМ, И ЭТО НАХОДКА MED-1 ВОСЬМОГО РАУНДА.

        Он проходит потому, что его три строки не дают попадания в `_ANCHOR_RE` ВООБЩЕ —
        короткое замыкание на якоре, а не на теме. То есть он не проверяет механизм, который
        называет, и прежний комментарий у `ST1_TOPIC_COMPANION` («тема стоит ЗА гейтом»)
        оставался неопровергнутым.

        Здесь утверждается сам механизм: словарь темы КОРМИТ гейт — `_has_routing_topic` зовёт
        `classify` и является одним из двух дизъюнктов `is_procedural`. Пока это так, расширение
        `ST1_TOPIC_COMPANION` расширяет маршрут, и обосновывать его «тема шире, ей можно» нельзя.

        ⚠⚠⚠ ЭТОТ СТОРОЖ САМ БЫЛ СЛЕПЫМ В ПЕРВОЙ РЕДАКЦИИ (находка 4 десятого раунда): он искал
        `topics.classify` во ВСЁМ исходнике функции, а эта строка стоит и в её ДОКСТРИНГЕ. Замена
        `topic = topics.classify(q)` на `topic = None` — дословно тот сценарий, который назван в
        сообщении об ошибке, — оставляла тест зелёным.

        ⚠⚠⚠ ВТОРАЯ РЕДАКЦИЯ (счёт по ТЕЛУ без докстринга) ПРОЖИЛА ОДИН ЧАС. Её уронил
        КОММЕНТАРИЙ, объясняющий соседнюю правку: в теле появилось имя `topics.classify`, и
        мутационная половина сторожа покраснела НА ВЕРНОМ КОДЕ. Это ровно тот класс, который в
        этом же файле чинила находка 13 («предохранитель, бьющий по верному коду, дороже
        отсутствующего») и который куплен ещё 18.08 остановкой ВЕРНОЙ выкатки. Вывод шире одного
        сторожа: считать ТЕКСТ функции нельзя вообще — любой текст вокруг кода прозаический,
        а инвариант здесь про КОД.

        Сторож переписан на КОД-ОБЪЕКТ: `co_names` перечисляет имена, к которым функция реально
        обращается. Комментарии и докстринг туда не попадают ПО ПОСТРОЕНИЮ, а замена вызова на
        `topic = None` убирает оттуда и `topics`, и `classify` — то есть мутация, названная выше,
        по-прежнему ловится."""
        import inspect

        from app.rag import procedural as P

        # ⚠⚠⚠ ТРЕТЬЯ РЕДАКЦИЯ: СЧЁТ ИМЁН УБРАН СОВСЕМ (находка 6 одиннадцатого раунда).
        # `co_names` — кортеж ГОЛЫХ идентификаторов, точки в них не бывает НИКОГДА, поэтому
        # `assertNotIn("topics.classify", names)` истинно для любой функции, какая только может
        # существовать: «отрицательный контроль к способу счёта» не мог покраснеть ни при какой
        # мутации. Это ровно тот класс «предохранитель, который не может сработать», который
        # нашёл раунд 7 и который вернулся в правке, сделанной ПО находке о нём же.
        # ⚠⚠ И у счёта имён была вторая беда — ложное срабатывание на ЗАКОННОМ рефакторинге:
        # `from app.rag.topics import classify` убирает `topics` из `co_names`, `assertIn`
        # краснеет на работающем гейте; вызов внутри генератора обнуляет `co_names` целиком.
        # Проверяется МЕХАНИЗМ, а не написание: подменяем тему и смотрим, меняется ли маршрут.
        # Проза, рефакторинг импорта и переносы вызова на это влиять не могут по построению.
        anchored = "как получить сертификат СТ-1 по 719"
        self.assertTrue(_ANCHORED_TOPIC_IS_REACHABLE := bool(P._ANCHOR_RE.search(anchored)),
                        "пример потерял 719-якорь — тема до гейта не доедет, проверка выродится")
        with mock.patch.object(P.topics, "classify", return_value=P.topics.ST1_ORIGIN):
            self.assertTrue(P._has_routing_topic(anchored),
                            "гейт больше не спрашивает тему — перепроверьте обоснование асимметрии")
        with mock.patch.object(P.topics, "classify", return_value=None):
            self.assertFalse(P._has_routing_topic(anchored),
                             "гейт не реагирует на отсутствие темы — тема выпала из механизма")
        with mock.patch.object(P.topics, "classify", return_value=P.topics.DOCUMENTS):
            self.assertFalse(P._has_routing_topic(anchored),
                             "оговорка про `documents` исчезла — смешанный вопрос уедет "
                             "процедурной веткой и потеряет требования продукции (`K12`)")
        self.assertIn("_has_routing_topic", inspect.getsource(P.is_procedural),
                      "тема выпала из гейта — обоснование асимметрии надо пересматривать")


class TestAmountWordDoesNotSwallowCode(unittest.TestCase):
    """Находка 3: слово о деньгах перед маркером не отменяет код ЗА маркером."""

    def test_code_after_marker_survives_money_before(self):
        for q in ("сумма контракта 250000, ТН ВЭД 8403",
                  "по контракту ТН ВЭД 8403 какие условия достаточной переработки",
                  "стоимость партии 900000, код ТН ВЭД 8403"):
            with self.subTest(q=q):
                self.assertEqual(extract_tnved_position(q), "8403")

    def test_negative_control_money_alone_is_not_a_code(self):
        """Предохранитель обязан остаться живым: без маркера позиции сумма кодом не станет."""
        for q in ("какие условия по ТН ВЭД, сумма контракта 250000",
                  "условия по ТН ВЭД при обороте 1500000"):
            with self.subTest(q=q):
                self.assertIsNone(extract_tnved_position(q))


class TestTrailingCounterTrimmed(unittest.TestCase):
    """Находка 4: приклеившееся число-счётчик отсекается, код остаётся."""

    def test_counter_words_do_not_extend_the_code(self):
        for q in ("код ТН ВЭД 8403 10 позиций", "ТН ВЭД 8403 12 шт",
                  "условия ТН ВЭД 8403 500000 рублей", "ТН ВЭД 8403 2026 года",
                  "ТН ВЭД 8403 50 % цены", "ТН ВЭД 8403 3 единицы"):
            with self.subTest(q=q):
                self.assertEqual(extract_tnved_position(q), "8403")

    def test_positive_control_real_subposition_survives(self):
        """⚠ Без этого «резать всё после четырёх знаков» тоже прошло бы."""
        self.assertEqual(extract_tnved_position("условия по ТН ВЭД 8403 10"), "8403 10")
        self.assertEqual(extract_tnved_position("условия по ТН ВЭД 0710 40 000"), "0710 40 000")
        self.assertEqual(extract_tnved_position("мой код ТН ВЭД 8544 70 000 0"), "8544 70 000 0")

    def test_long_code_is_never_truncated_by_a_counter_word(self):
        """⚠⚠ РЕГРЕССИЯ В САМОЙ ПРАВКЕ РАУНДА 7, найденная проверкой краёв.

        Первая редакция резала в цикле «пока следом стоит слово-опровержение» и усекала
        НАСТОЯЩИЕ полные коды: «8544 70 000 0 позиции» → «8544 70 000». Это класс HIGH первого
        раунда — усечённый ключ даёт уверенный ответ про ДРУГУЮ позицию.
        Отсечение разрешено только у четырёхзначной основы: «8403 10» → «8403» это подъём к
        РОДИТЕЛЮ, а срез хвоста у десятизначного — переход к ЧУЖОЙ позиции."""
        for q, want in (("условия по ТН ВЭД 8544 70 000 0 позиции", "8544 70 000 0"),
                        ("ТН ВЭД 0710 40 000 единиц", "0710 40 000"),
                        ("ТН ВЭД 8704 21 310 0 какие условия", "8704 21 310 0")):
            with self.subTest(q=q):
                self.assertEqual(extract_tnved_position(q), want)

    def test_negative_control_dates_still_rejected(self):
        self.assertIsNone(extract_tnved_position("в 2026 году какие условия"))
        self.assertIsNone(extract_tnved_position("постановление 719 от 2020"))


class TestSectionHeadIsIndexed(unittest.TestCase):
    """Находка 5: голова раздела с пунктами попадает в корпус и ничего не удваивает."""

    @classmethod
    def setUpClass(cls):
        import load_rules_kb as loader
        src = ROOT / "knowledge_base" / "pp719" / "sng_origin_rules.txt"
        cls.recs = loader.parse_sng_origin(src)
        cls.heads = [r for r in cls.recs if "(вводная)" in r["source_anchor"]]

    def test_appendix4_head_is_present(self):
        needle = "определяет общие принципы создания и применения электронной системы"
        self.assertTrue(any(needle in r["text"] for r in self.recs),
                        "вводная приложения 4 не попала в корпус")

    def test_head_does_not_duplicate_points(self):
        dup = [h["source_anchor"] for h in self.heads for r in self.recs
               if r is not h and r["text"][:120] in h["text"]]
        self.assertEqual(dup, [], f"текст пункта продублирован во вводной: {dup}")

    def test_anchors_stay_unique(self):
        anchors = [r["source_anchor"] for r in self.recs]
        self.assertEqual(len(anchors), len(set(anchors)),
                         "вводная столкнулась якорем с существующей записью")

    def test_positive_control_points_are_not_lost(self):
        """⚠ Отдай мы голову ВМЕСТО пунктов — предыдущие тесты остались бы зелёными."""
        p4 = [r for r in self.recs if "Приложение 4" in r["source_anchor"]]
        self.assertGreaterEqual(len(p4), 9, "пункты приложения 4 потеряны")
        self.assertTrue(any(r.get("point") == "2.1" for r in p4))

    def test_head_is_not_empty(self):
        for h in self.heads:
            with self.subTest(a=h["source_anchor"]):
                self.assertGreater(len(h["text"].strip()), 60)


class TestDeployProfileMatchesParsers(unittest.TestCase):
    """⚠ Числа профиля выкатки считаются ЗАПУСКОМ разборщиков, а не арифметикой по дельте.

    Расхождение профиля с корпусом ловится здесь, а не предохранителем `deploy.sh` на бою."""

    BASE_RULES = 328   # `pp719_rules` до `K14` (выкачено в `v0.5.0-test22`)
    PROFILE = ROOT / "scripts" / "deploy" / "releases" / "v0.5.0-test23.env"

    def test_expect_equals_what_parsers_produce(self):
        import load_rules_kb as loader
        kb = ROOT / "knowledge_base" / "pp719"
        got = (len(loader.parse_sng_origin(kb / "sng_origin_rules.txt"))
               + len(loader.parse_prikaz14(kb / "prikaz14_tpp_full.txt")))
        m = re.search(r"\[pp719_rules\]=(\d+)", self.PROFILE.read_text(encoding="utf-8"))
        self.assertIsNotNone(m, "в профиле нет ожидаемого числа точек `pp719_rules`")
        self.assertEqual(int(m.group(1)), self.BASE_RULES + got,
                         "профиль выкатки разошёлся с тем, что даёт разбор корпуса")

    def test_table_row_count_matches_profile(self):
        rows = json.loads((ROOT / "knowledge_base" / "classifiers"
                           / "tnved_st1_conditions.json").read_text(encoding="utf-8"))["rows"]
        self.assertIn(f"{len(rows)} записей", self.PROFILE.read_text(encoding="utf-8"),
                      "число записей таблицы фактов в профиле устарело")


if __name__ == "__main__":
    unittest.main()
