"""Сторожа ДЕСЯТОГО раунда ревью (мишень `f0c19d8..6d41582` — остаток раунда 8).

Раунд дал 17 находок, одна HIGH — и HIGH была РЕГРЕССИЕЙ самой проверяемой правки.

1 (HIGH). MED-6 УСЕКАЛА НАСТОЯЩИЕ ДЕВЯТИЗНАЧНЫЕ КОДЫ. Правило «одиночная цифра в хвосте законна
   только как десятый знак, то есть при ДЕВЯТИЗНАЧНОЙ основе» не знало, что девятизначный код
   пишут и группами 4+2+2+1: «0710 40 00 0». Основа там восьмизначная, поэтому хвост отсекался,
   и ключ становился восьмизначным — совпадение с Перечнем терялось. Замер по всем 59
   девятизначным кодам Перечня (⚠ числа называют свою величину): КЛЮЧ усекался у 59 из 59,
   ВЕРДИКТ «включён» терялся у 53 из 59 — шесть уцелели совпадением по ПРЕФИКСУ, а не потому,
   что разбор их не тронул. До правки раунда 8 — 0 из 59 по обеим величинам. Канонная запись
   4+2+3 не страдала, то есть правило молча предполагало ровно одну форму записи. Это класс HIGH
   первого раунда («усечённый ключ даёт уверенный ответ про ДРУГУЮ позицию»), который
   комментарий той же правки объявлял здесь невозможным.

2. РАЗБОР ВООБЩЕ НЕ ПРОВЕРЯЛ ДЛИНУ НА ОБЫЧНОМ ПУТИ: отсечение включалось только когда за кодом
   стоит слово-опровержение. «какие условия для кода ТН ВЭД 8403 10 1» отдавало семизначный ключ —
   ту самую длину, «которой у ТН ВЭД не бывает», ради которой MED-6 и писалась.

3. Двух- и трёхзначный счётчик по-прежнему приклеивается, и на «8544 70 12» пропадает оговорка
   `narrower` («условия есть у части вашей позиции»), которую `conditions_for` обязан выдавать.

Остальные четырнадцать — в отчёте [docs/eval_runs/2026-09-07_review_round10.md]. Сторожа здесь
держат те, что закрыты кодом: 5 (мутация схемы `§`), 6 (цена подъёма к родителю), 7 (девять живых
коллизий `by_key`), 10 (асимметрия темы), 11 (популяция набора вне-сферы), 15 (арифметика окна),
16 (перечень общих с ретривером фрагментов). Находки 4, 8, 9, 12, 13, 14, 17 — про ложные или
устаревшие ОБОСНОВАНИЯ; они закрыты правкой текста там, где текст лежит, и сторожа не требуют,
кроме 13 и 14, чьи сторожа переписаны в `test_k14_review_pr137_round7.py`.

⚠⚠ ПОЧЕМУ ПРИЗНАК ЗАМЕНЁН, А НЕ ПОДКРУЧЕН. Раунды 7, 8 и 9 отвечали на один вопрос —
«принадлежит ли последняя группа коду» — тремя разными признаками: длиной ОСНОВЫ, размером
ХВОСТА, наличием слова-счётчика. Каждый закрывал свой случай и открывал соседний. Признак взят
из первоисточника и проверяем: ключ недопустимой ДЛИНЫ не обозначает ничего (`_TNVED_KEY_LENGTHS`).

⚠ ЧТО ОСТАЁТСЯ ИЗВЕСТНЫМ ПРЕДЕЛОМ. «8403 10 1500 единиц» даёт допустимые десять знаков и
лексически неотличимо от настоящего кода — случай, для которого раунд 9 постановил выбирать
НАПРАВЛЕНИЕМ ошибки. Здесь он не решается и записан долгом.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import okpd2_ref, procedural, st1_ref  # noqa: E402
from app.rag.okpd2_ref import extract_tnved_position, normalize_tnved  # noqa: E402
from app.tools.navigator import extract_okpd2  # noqa: E402


def route(q: str) -> bool:
    """Маршрут вопроса — так же, как его считает рантайм."""
    return procedural.is_procedural(q, has_code=bool(extract_okpd2(q)))

TABLE = ROOT / "knowledge_base" / "classifiers" / "tnved_st1_conditions.json"
COUNTERS = ("позиций", "единиц", "шт")


def _digits(s: str | None) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def _rows(t: unittest.TestCase) -> list[dict]:
    if not TABLE.exists():
        t.skipTest("таблица условий Перечня не сгенерирована")
    return json.loads(TABLE.read_text(encoding="utf-8"))["rows"]


class TestNineDigitCodeSurvivesEveryGrouping(unittest.TestCase):
    """Находка 1: девятизначный код нельзя усекать НИ В ОДНОЙ форме записи."""

    def test_four_group_form_keeps_the_full_code(self):
        codes = sorted({_digits(r["code"]) for r in _rows(self)
                        if r.get("code") and len(_digits(r["code"])) == 9})
        self.assertGreaterEqual(len(codes), 10, "девятизначных строк в Перечне вдруг нет")
        lost = []
        for c in codes:
            grouped = f"{c[:4]} {c[4:6]} {c[6:8]} {c[8]}"        # 0710 40 00 0
            key = extract_tnved_position(f"по ТН ВЭД {grouped} шт какие условия")
            if _digits(key) != c:
                lost.append((grouped, key))
        self.assertEqual(lost, [], f"код усечён в записи 4+2+2+1: {lost[:3]}")

    def test_canonical_form_keeps_the_full_code(self):
        """Положительный контроль: канонная запись 4+2+3 не страдала и до правки.

        Без неё тест был бы зелёным и на правиле «никогда ничего не отсекать»."""
        codes = sorted({_digits(r["code"]) for r in _rows(self)
                        if r.get("code") and len(_digits(r["code"])) == 9})[:20]
        for c in codes:
            grouped = f"{c[:4]} {c[4:6]} {c[6:]}"
            with self.subTest(code=c):
                self.assertEqual(_digits(extract_tnved_position(
                    f"по ТН ВЭД {grouped} шт какие условия")), c)

    def test_the_lookup_still_matches(self):
        """Цена дефекта была не в ключе, а в ВЕРДИКТЕ: усечённый ключ терял строку Перечня."""
        rows = _rows(self)
        c = next(_digits(r["code"]) for r in rows
                 if r.get("code") and len(_digits(r["code"])) == 9
                 and st1_ref.conditions_for(_digits(r["code"]))["matched"])
        grouped = f"{c[:4]} {c[4:6]} {c[6:8]} {c[8]}"
        key = extract_tnved_position(f"по ТН ВЭД {grouped} шт какие условия")
        self.assertTrue(st1_ref.conditions_for(key)["matched"],
                        "совпадение с Перечнем потеряно — ответ уйдёт по общему правилу")


class TestKeyLengthsComeFromTheSource(unittest.TestCase):
    """Множество допустимых длин обязано покрывать ПЕРВОИСТОЧНИК, а не память автора."""

    def test_every_length_in_the_table_is_allowed(self):
        lens = {len(_digits(r["code"])) for r in _rows(self) if r.get("code")}
        lens |= {len(_digits(r["code_to"])) for r in _rows(self) if r.get("code_to")}
        missing = sorted(lens - okpd2_ref._TNVED_KEY_LENGTHS)
        self.assertEqual(missing, [],
                         f"в Перечне есть ключи длины {missing}, а разбор их усечёт")

    def test_full_subposition_length_is_allowed(self):
        """Десять знаков в таблице не встречаются, но это полная подсубпозиция ТН ВЭД."""
        self.assertIn(10, okpd2_ref._TNVED_KEY_LENGTHS)

    def test_impossible_lengths_are_not_allowed(self):
        """Отрицательный контроль: множество не должно вырождаться в «любая длина»."""
        for n in (1, 2, 3, 5, 7, 11, 12, 13):
            self.assertNotIn(n, okpd2_ref._TNVED_KEY_LENGTHS)


class TestNoImpossibleKeyEverReachesTheLookup(unittest.TestCase):
    """Находка 2 раунда 10: ключ невозможной длины не должен рождаться НА ПУТИ ОТСЕЧЕНИЯ.

    ⚠⚠⚠ ОБЛАСТЬ СУЖЕНА ПОСЛЕ НАХОДКИ 2 ОДИННАДЦАТОГО РАУНДА (HIGH). Прежний докстринг говорил
    «НИ НА КАКОМ пути», и по этой формуле гейт длины поставили ещё и на обычный путь — туда, где
    матч никем не опровергнут. Это стоило **147 потерь вердикта из 308** на классе «код Перечня
    с лишним знаком»: `st1_ref.conditions_for` матчит по ПРЕФИКСУ, поэтому ключ длиннее записи
    доезжает до СВОЕЙ строки и отвечает верно, а усечение до родителя даёт «НЕ включён».
    Инвариант, который держится на самом деле: **невозможная длина не должна появляться там, где
    мы САМИ отбросили часть матча**; то, что написал пользователь, отдаётся как есть."""

    def _sweep(self) -> list[tuple[str, str]]:
        bad = []
        for r in _rows(self):
            code = _digits(r.get("code"))
            if not code:
                continue
            c = code
            pretty = (c if len(c) <= 4 else f"{c[:4]} {c[4:]}" if len(c) <= 6
                      else f"{c[:4]} {c[4:6]} {c[6:]}" if len(c) <= 9
                      else f"{c[:4]} {c[4:6]} {c[6:9]} {c[9:]}")
            for n in ("1", "5", "12", "25", "100", "1500"):
                for w in COUNTERS:
                    q = f"какие условия достаточной переработки по ТН ВЭД {pretty} {n} {w}"
                    key = extract_tnved_position(q)
                    if key and len(_digits(key)) not in okpd2_ref._TNVED_KEY_LENGTHS:
                        bad.append((q, key))
        return bad

    def test_counter_never_produces_a_key_of_impossible_length(self):
        bad = self._sweep()
        self.assertEqual(bad, [], f"ключ невозможной длины: {bad[:3]}")

    def test_ordinary_path_returns_what_the_user_wrote(self):
        """Находка 2 одиннадцатого раунда: на НЕопровергнутом матче гейта длины быть не должно.

        Ключ отдаётся как написан, даже если длина «невозможная»: решает не форма, а лукап."""
        for q, want in (
            ("какие условия достаточной переработки для кода ТН ВЭД 8403 10 1", "8403 10 1"),
            ("по ТН ВЭД 8403 10 00 00 00 какие условия", "8403 10 00 00 00"),
        ):
            with self.subTest(q=q):
                self.assertEqual(extract_tnved_position(q), want)

    def test_such_a_key_still_resolves(self):
        """Положительный контроль к предыдущему: «невозможная» длина НЕ мешает лукапу.

        ⚠ Без него предыдущий тест защищал бы только строку, а утверждение проверяется здесь —
        именно оно опровергает посылку «ключ недопустимой длины не обозначает НИЧЕГО», из-за
        которой гейт и поставили на обычный путь."""
        if not st1_ref.is_available():
            self.skipTest("таблица условий Перечня не сгенерирована")
        for key in ("8403 10 1", "8403 10 00 00 00"):
            with self.subTest(key=key):
                self.assertNotIn(len(_digits(key)), okpd2_ref._TNVED_KEY_LENGTHS,
                                 "пример перестал быть примером: длина стала допустимой")
                self.assertTrue(st1_ref.conditions_for(key)["matched"],
                                "ключ с лишним знаком перестал попадать в свою строку — "
                                "посылка гейта длины изменилась, перечитайте находку 2")

    def test_mutation_the_gate_on_the_ordinary_path_costs_verdicts(self):
        """Отрицательный контроль: вернуть гейт на обычный путь — и вердикты обязаны посыпаться.

        Считаем ЦЕНУ, а не форму: сколько кодов Перечня с лишним знаком перестанут отвечать.
        Без этого «гейта здесь нет» — утверждение о тексте, а не о поведении."""
        if not st1_ref.is_available():
            self.skipTest("таблица условий Перечня не сгенерирована")
        codes = sorted({_digits(r["code"]) for r in _rows(self)
                        if r.get("code") and not r.get("code_to")
                        and len(_digits(r["code"])) >= 6})[:40]
        self.assertGreaterEqual(len(codes), 10, "кодов от 6 знаков в Перечне вдруг нет")
        would_lose = 0
        for c in codes:
            longer = c + "1"
            if not st1_ref.conditions_for(longer)["matched"]:
                continue
            if len(longer) not in okpd2_ref._TNVED_KEY_LENGTHS:
                would_lose += 1          # гейт длины отверг бы разрешимый ключ
        self.assertGreater(would_lose, 0,
                           "гейт на обычном пути больше ничего не ломает — контроль выродился, "
                           "проверьте, что _TNVED_KEY_LENGTHS и лукап не разъехались")

    def test_positive_control_real_codes_are_not_touched(self):
        """Без этого «отдавать None на всё подряд» тоже дало бы ноль невозможных длин."""
        for q, want in (("код ТН ВЭД 8403", "8403"),
                        ("ТН ВЭД 8544 70 000 0 позиции", "8544 70 000 0"),
                        ("0710 40 000 единиц по ТН ВЭД", "0710 40 000"),
                        ("ТН ВЭД 8403 12 шт", "8403")):
            with self.subTest(q=q):
                self.assertEqual(extract_tnved_position(q), want)


class TestSectionRecordNumberingIsPinnedExecutably(unittest.TestCase):
    """Находка 5: у правки MED-4 не было мутационного покрытия.

    Единственный сторож считал дубли ключа `(doc_type, section_roman, point)`, а на сегодняшнем
    корпусе их нет и при СТАРОЙ схеме нумерации — то есть возврат строки не заметил бы никто, а
    в силу она вступает только на следующей переиндексации (окно `K16` #48). Здесь утверждается
    сама схема: номер записи раздела не должен иметь форму настоящего пункта."""

    def _make(self):
        import load_rules_kb as L
        base = {"doc_type": "prikaz14_tpp", "section_roman": "3.5", "section_title": "Раздел 5"}
        return L, L._section_records(base, ["Термины и понятия: партия товара — …"], "прил. 3, разд. 5")

    def test_number_cannot_look_like_a_real_point(self):
        L, recs = self._make()
        self.assertTrue(recs, "запись раздела не создана — проверять нечего")
        for r in recs:
            with self.subTest(point=r["point"]):
                self.assertIsNone(L._K14_POINT_RE.match(f"{r['point']} текст"),
                                  "номер записи раздела неотличим от настоящего пункта")

    def test_mutation_the_old_scheme_would_collide(self):
        """Отрицательный контроль: прежняя схема ОБЯЗАНА проваливать проверку выше.

        Иначе тест зелен независимо от схемы и снова ничего не значит."""
        L, _ = self._make()
        old_style = "3.5.1"
        self.assertIsNotNone(L._K14_POINT_RE.match(f"{old_style} текст"),
                             "контроль выродился: старая форма номера больше не читается "
                             "как настоящий пункт, значит и новая ничего не доказывает")


class TestGapIsATextDistance(unittest.TestCase):
    """Находка 15: разрыв до маркера — расстояние в ТЕКСТЕ, а не длина усечённого ключа.

    `_trim_to_code` укорачивает кандидата, и правый край, посчитанный как
    `m.start() + len(token)`, оказывался ПРАВЕЕ настоящего конца матча — окно в 30 знаков
    сужалось ровно на столько, сколько отсекли. Кандидат внутри окна выпадал, блок условий СТ-1
    молча исчезал. Замер правки: 9600 запросов, 240 расхождений, ВСЕ 240 «было None → стал код»,
    потерь 0, подмен одного кода другим 0."""

    CASE = "8403 10 позиций отгружено в мае по ТН ВЭД"

    def test_code_inside_the_window_is_not_dropped(self):
        self.assertEqual(extract_tnved_position(self.CASE), "8403")

    def test_the_arithmetic_uses_match_end(self):
        """Мутация: вернуть длину ключа — и тот же вопрос обязан снова потерять код.

        Без неё тест зелен и на формуле, которой просто повезло на выбранной строке."""
        t = self.CASE
        cues = [(m.start(), m.end()) for m in okpd2_ref._TNVED_POSITION_CUE_RE.finditer(t)]
        m = next(okpd2_ref._TNVED_POSITION_RE.finditer(t))
        token = okpd2_ref._trim_to_code(t, m)
        gap_text = min(max(cs - m.end(), m.start() - ce, 0) for cs, ce in cues)
        gap_key = min(max(cs - m.start() - len(token), m.start() - ce, 0) for cs, ce in cues)
        self.assertLessEqual(gap_text, okpd2_ref._TNVED_POSITION_MAX_GAP,
                             "настоящий разрыв вышел за окно — пример перестал быть примером")
        self.assertGreater(gap_key, okpd2_ref._TNVED_POSITION_MAX_GAP,
                           "прежняя арифметика больше не ошибается — контроль выродился")

    def test_far_code_is_still_rejected(self):
        """Положительный контроль: окно не превратилось в «берём что угодно»."""
        far = "8403 10 позиций отгружено в мае, июне и июле прошлого года по ТН ВЭД"
        self.assertIsNone(extract_tnved_position(far))


class TestParentClimbCostIsRecorded(unittest.TestCase):
    """Находка 6 раунда 10 — цена подъёма к родителю. ⚠⚠⚠ ЦЕНА БОЛЬШЕ НЕ ПЛАТИТСЯ (раунд 11).

    Раунд 10 назвал цену и оставил поведение: подъём «CCCC DD счётчик» → «CCCC» у 73 из 79 кодов
    даёт «в Перечень НЕ включён» о позиции, которой пользователь не называл. Раунд 11 показал,
    что этот же класс расширяется окном разрыва (находка 1), и что разводить два вида подъёма
    ПРИЗНАКОМ ФОРМЫ нельзя — пять раундов пробовали пять признаков.
    Развела их не форма, а ФАКТ: `_climb_resolves` спрашивает Перечень, есть ли запись у
    родителя. Есть — поднимаемся (замер: 453 верных вердикта из 906 на классе подъёма); нет —
    отказываемся молча (замер: 38 ложных «НЕ включён» убрано, потерь 0)."""

    def test_climb_happens_only_to_a_parent_that_exists(self):
        """Ядро правки раунда 11: подъём разрешён к записи, отказ — к пустоте."""
        if not st1_ref.is_available():
            self.skipTest("таблица условий Перечня не сгенерирована")
        self.assertTrue(st1_ref.conditions_for("8403")["matched"],
                        "пример протух: 8403 обязан быть в Перечне")
        self.assertFalse(st1_ref.conditions_for("2101")["matched"],
                         "пример протух: 2101 обязан ОТСУТСТВОВАТЬ в Перечне")
        self.assertEqual(extract_tnved_position("ТН ВЭД 8403 12 шт"), "8403",
                         "родитель В Перечне — подъём обязан состояться")
        self.assertIsNone(extract_tnved_position("ТН ВЭД 2101 12 шт"),
                          "родителя в Перечне НЕТ — вместо уверенного «НЕ включён» обязан "
                          "быть отказ (находки 1 и 6 одиннадцатого раунда)")
        self.assertEqual(extract_tnved_position("ТН ВЭД 2101 12"), "2101 12",
                         "без слова-счётчика шестизначный код обязан уцелеть")

    def test_mutation_unconditional_climb_would_assert_a_false_negative(self):
        """Отрицательный контроль: снять `_climb_resolves` — и появится ложное «НЕ включён».

        ⚠ Проверяется ПОСЛЕДСТВИЕ, а не наличие вызова: сторож, считающий имя функции, зеленеет
        на верном коде и краснеет на комментарии (урок находок 4 и 13 десятого раунда)."""
        if not st1_ref.is_available():
            self.skipTest("таблица условий Перечня не сгенерирована")
        with mock.patch.object(okpd2_ref, "_climb_resolves", return_value=True):
            key = extract_tnved_position("ТН ВЭД 2101 12 шт")
        self.assertEqual(key, "2101", "без проверки подъём обязан вернуться")
        self.assertFalse(st1_ref.conditions_for(key)["matched"],
                         "и обязан дать именно тот вердикт, ради которого правка сделана")

    def test_the_cost_is_what_was_measured(self):
        rows = _rows(self)
        six = sorted({_digits(r["code"]) for r in rows
                      if r.get("code") and len(_digits(r["code"])) == 6})
        self.assertGreaterEqual(len(six), 10, "шестизначных кодов в Перечне вдруг нет")
        flips = [c for c in six if st1_ref.conditions_for(c)["matched"]
                 and not st1_ref.conditions_for(c[:4])["matched"]]
        self.assertEqual(len(flips), len(six),
                         "цена подъёма изменилась — перечитайте докстринг `_trim_to_code`")

    def test_no_counter_word_means_no_climb(self):
        """Отрицательный контроль: подъём не должен срабатывать без опровержения."""
        for c in ("2101 12", "8544 70 000", "0710 40 00 0"):
            with self.subTest(code=c):
                self.assertEqual(_digits(extract_tnved_position(f"по ТН ВЭД {c} какие условия")),
                                 _digits(c))


class TestParentIntroNeverTakesAnAmbiguousParent(unittest.TestCase):
    """Находка 7: девять ЖИВЫХ коллизий `by_key`, которых сторож MED-4 не видел по построению.

    `add_index_text` строил словарь компрехеншеном — при совпадении ключа побеждала ПОСЛЕДНЯЯ
    запись молча. У Приказа №52 приложения 4, 6 и 7 нумеруют пункты заново, поэтому
    «1. Заявитель» и «1. Основания для проведения экспертизы» — оба п. 1 приложения 4. Сторож
    MED-4 считает дубли в выдаче `parse_prikaz14` и до общего корпуса не достаёт."""

    @staticmethod
    def _load():
        import load_rules_kb as L
        recs, _ = L.load_records()
        return L, recs

    def test_collisions_are_detected_across_the_whole_corpus(self):
        import collections
        L, recs = self._load()
        keys = collections.Counter(
            (r.get("doc_type"), r.get("section_roman"), r.get("point")) for r in recs)
        dup = {k for k, n in keys.items() if n > 1}
        # Коллизии сегодня ЕСТЬ — это факт корпуса, а не брак. Утверждается не их отсутствие,
        # а то, что ни одна не отдала подпункту чужую вводную.
        self.assertTrue(dup, "коллизий не стало — проверьте, что сторож ещё что-то охраняет")
        harmed = []
        for r in recs:
            p = L._parent_point(r.get("point"))
            if p and (r.get("doc_type"), r.get("section_roman"), p) in dup and r.get("parent_intro"):
                harmed.append(r.get("source_anchor"))
        self.assertEqual(harmed, [], "подпункт получил вводную от НЕОДНОЗНАЧНОГО родителя")

    def test_ambiguous_parent_is_skipped_not_arbitrated(self):
        """Мутация: подсунуть двух родителей одному ключу и потребовать ПРОПУСКА.

        Прежний код взял бы последнего — правдоподобную и чужую преамбулу."""
        import load_rules_kb as L
        recs = [
            {"doc_type": "d", "section_roman": "s", "point": "1", "text": "ПЕРВАЯ вводная"},
            {"doc_type": "d", "section_roman": "s", "point": "1", "text": "ВТОРАЯ вводная"},
            {"doc_type": "d", "section_roman": "s", "point": "1.1", "text": "подпункт"},
        ]
        L.add_index_text(recs)
        child = recs[-1]
        self.assertIsNone(child.get("parent_intro"),
                          "неоднозначный родитель всё-таки прикрепился")
        self.assertNotIn("ВТОРАЯ", child["index_text"],
                         "в индекс уехала произвольная из двух преамбул")

    def test_unambiguous_parent_still_attaches(self):
        """Положительный контроль: правка не отключила механизм целиком."""
        import load_rules_kb as L
        recs = [
            {"doc_type": "d", "section_roman": "s", "point": "2", "text": "единственная вводная"},
            {"doc_type": "d", "section_roman": "s", "point": "2.1", "text": "подпункт"},
        ]
        L.add_index_text(recs)
        self.assertEqual(recs[-1].get("parent_intro"), "единственная вводная")
        self.assertIn("единственная вводная", recs[-1]["index_text"])


class TestAsymmetryIsNotInertForTopic(unittest.TestCase):
    """Находка 10: «инертна» было сказано о МАРШРУТЕ, а прочитано как о поведении.

    ⚠ Мутация обязана быть В ИСХОДНИКЕ: `ST1_TOPIC_COMPANION` интерполируется в скомпилированный
    регексп на загрузке модуля, поэтому переприсваивание атрибута ничего не меняет и даёт честный
    ноль. Первый замер раунда 10 на этом и ошибся."""

    LINE = 'ST1_TOPIC_COMPANION = ST1_COMPANION + r"|требован\\w*"'
    PROBES = (
        "укажите ваш код ТН ВЭД, распишите требования",
        "мой код ТН ВЭД 8471 30 000 0, какие требования к продукции",
        "подпадает ли под требования продукция ТН ВЭД 8479 89 970 8",
        "какие требования по ТН ВЭД 8403",
    )

    @classmethod
    def _variants(cls):
        import types
        src_path = ROOT / "app" / "rag" / "topics.py"
        src = src_path.read_text(encoding="utf-8")
        if src.count(cls.LINE) != 1:
            return None, None, src

        def build(text, name):
            mod = types.ModuleType(name)
            mod.__file__, mod.__package__ = str(src_path), "app.rag"
            exec(compile(text, str(src_path), "exec"), mod.__dict__)
            return mod

        return (build(src, "t_base"),
                build(src.replace(cls.LINE, "ST1_TOPIC_COMPANION = ST1_COMPANION"), "t_mut"),
                src)

    def test_the_line_is_where_the_guard_thinks_it_is(self):
        _, _, src = self._variants()
        self.assertEqual(src.count(self.LINE), 1,
                         "строка асимметрии переписана — сторож ниже ничего не мутирует")

    def test_removing_it_changes_the_topic(self):
        base, mut, _ = self._variants()
        self.assertIsNotNone(base, "мутация не собралась")
        self.assertNotEqual(base.ST1_TOPIC_COMPANION, mut.ST1_TOPIC_COMPANION,
                            "мутация не изменила словарь — замер был бы пустым")
        changed = [q for q in self.PROBES if base.classify(q) != mut.classify(q)]
        self.assertEqual(len(changed), len(self.PROBES),
                         "асимметрия перестала влиять на ТЕМУ — перечитайте комментарий "
                         "у `ST1_TOPIC_COMPANION`, он утверждает обратное")
        for q in self.PROBES:
            with self.subTest(q=q):
                self.assertEqual(base.classify(q), "st1_origin")

    def test_but_the_route_does_not_move(self):
        """То, что действительно было измерено: маршрут не двигается ни на одном вопросе."""
        base, mut, _ = self._variants()
        import app.rag.topics as real
        saved = real.classify
        try:
            for q in self.PROBES:
                real.classify = base.classify
                a = route(q)
                real.classify = mut.classify
                b = route(q)
                with self.subTest(q=q):
                    self.assertEqual(a, b, "маршрут сдвинулся — числа в комментарии устарели")
        finally:
            real.classify = saved


class TestSharedFragmentsAreEnumerated(unittest.TestCase):
    """Находка 16: список общих с ретривером фрагментов держит ТЕСТ, а не память комментария.

    Комментарий MED-2 называл таблицу `_DOC_HINTS`, которой в проекте нет вовсе, и ОДИН общий
    фрагмент вместо шести. Перечисление проверяется против обоих файлов."""

    SHARED = (
        r"достаточн\w*\s+(?:обработк|переработк)",
        r"стран\w*\s+происхожден",
        r"сертификат\w*\s+(?:о\s+)?происхожден",
        r"перечн\w*\s+услови",
        r"кумулятивн",
        r"адвалорн",
    )
    NOT_SHARED = (r"происхожден\w*\s+(?:товар|продукц|сырь)",)

    @staticmethod
    def _sources():
        return ((ROOT / "app" / "rag" / "topics.py").read_text(encoding="utf-8"),
                (ROOT / "app" / "rag" / "retriever.py").read_text(encoding="utf-8"))

    def test_named_table_exists(self):
        _, retr = self._sources()
        self.assertIn("_RULES_TOPIC", retr)
        self.assertNotIn("_DOC_HINTS", retr,
                         "таблица переименована — поправьте комментарий у ST1_COMPANION")

    def test_every_listed_fragment_is_really_in_both_files(self):
        top, retr = self._sources()
        for frag in self.SHARED:
            with self.subTest(frag=frag):
                self.assertIn(frag, top, "фрагмента нет в topics — перечисление устарело")
                self.assertIn(frag, retr, "фрагмента нет в retriever — перечисление устарело")

    def test_the_negative_half_is_still_negative(self):
        """Отрицательный контроль: перечисление не выродилось в «общее всё»."""
        top, retr = self._sources()
        for frag in self.NOT_SHARED:
            with self.subTest(frag=frag):
                self.assertIn(frag, top)
                self.assertNotIn(frag, retr,
                                 "фрагмент стал общим — список расхождений надо пересобрать")


class TestOffDomainSetPopulationIsPinned(unittest.TestCase):
    """Находка 11: популяция исторического замера названа ПОИМЁННО и цела.

    `expected_leaks_before: 9` снято на 2f67c19 и переизмерению не подлежит; исполняемо
    проверяется то, от чего оно зависит, — что те самые кейсы никуда не делись. Прежняя запись
    ссылалась на «первые 12», а пробники раунда 9 стоят в НАЧАЛЕ файла, поэтому `cases[:12]`
    отличается от замеренной популяции четырьмя членами из двенадцати."""

    SET = ROOT / "scripts" / "eval_offdomain_tnved.json"

    def _data(self):
        return json.loads(self.SET.read_text(encoding="utf-8"))

    def test_historical_population_is_intact(self):
        j = self._data()
        hist = [c["id"] for c in j["cases"] if c["id"] < 100]
        self.assertEqual(hist, list(range(1, 13)),
                         "историческая популяция изменилась — expected_leaks_before больше "
                         "не относится к этому набору")

    def test_slicing_by_position_would_be_wrong(self):
        """Отрицательный контроль: срез по позиции ОБЯЗАН расходиться с популяцией.

        Если однажды совпадёт — значит порядок файла изменился, и оговорка в `_meta` перестала
        описывать действительность."""
        j = self._data()
        by_pos = [c["id"] for c in j["cases"][:12]]
        self.assertNotEqual(by_pos, list(range(1, 13)),
                            "срез совпал с популяцией — обновите _meta, оговорка устарела")

    def test_note_does_not_promise_an_unread_check(self):
        j = self._data()
        note = j["_meta"]["expected_leaks_before_note"]
        self.assertNotIn("Проверяется тестом", note,
                         "заметка снова обещает проверку — назовите проверяющий тест поимённо")
        self.assertIn("101", note, "популяция не названа поимённо")


if __name__ == "__main__":
    unittest.main()
