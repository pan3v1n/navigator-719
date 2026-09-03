"""MED-3 восьмого раунда ревью PR #137: заголовок раздела Приказа №14 занимает НЕСКОЛЬКО строк.

Шаблон `_P14_SECTION_RE` брал одну строку, и восемь заголовков обрывались на запятой:
    «Раздел 3. Перечень и порядок предоставления документов,»
    «необходимых для получения сертификата формы СТ-1»   <- строка пропадала
Хвост называет ФОРМУ сертификата, а до запятой все девять Положений Приказа звучат одинаково.
Девятый случай — многострочная скобка у «Особенности оформления сертификатов формы СТ-1».

⚠⚠⚠ ОТЧЁТ РАУНДА 8 ПРЕДПИСЫВАЛ ДРУГОЙ ФИКС, И ОН БЫЛ БЫ ХУЖЕ ДЕФЕКТА. Отчёт требовал завести
здесь `head_buf`, как в `parse_sng_origin`. Проверено замером на первоисточнике: весь
«потерянный» текст — ПРОДОЛЖЕНИЕ ЗАГОЛОВКА, а не проза раздела, поэтому `head_buf` завёл бы
десять записей вида «необходимых для получения сертификата формы СТ-1» (48 знаков) как
самостоятельные пункты — класс `D12` («записи-обрывки»). И поднял бы латентную MED-4: вводная
нумеруется `section_roman.k`, а `{part}.{sec}.1` занят настоящим пунктом на ДЕСЯТИ разделах из
одиннадцати. Тесты ниже держат ОБА этих запрета, а не только починку заголовка.

⚠ Тяжесть находки в отчёте ЗАВЫШЕНА, и это проверено: якорь формы не терял. `part_title` называет
Положение («…товара формы СТ-1»), поэтому неуникальных якорей 0 и записей без владельца 0.
Обрезка портила ЧИТАЕМОСТЬ якоря и payload, а не привязку. Закреплено тестом, чтобы правка
следующего раунда не «чинила» несуществующую проблему привязки.
"""

from __future__ import annotations

import collections
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import load_rules_kb as L  # noqa: E402

P14 = ROOT / "knowledge_base" / "pp719" / "prikaz14_tpp_full.txt"


def _recs(t: unittest.TestCase):
    if not P14.exists():
        t.skipTest("prikaz14_tpp_full.txt не сгенерирован")
    return L.parse_prikaz14(P14)


class TestMultilineHeadingIsJoined(unittest.TestCase):

    def test_no_section_title_ends_with_a_dangling_comma(self):
        """Замер до правки: 25 записей из 287 несли висячую запятую."""
        bad = [r["section_title"] for r in _recs(self)
               if (r.get("section_title") or "").rstrip().endswith(",")]
        self.assertEqual(bad, [], f"заголовок оборван: {bad[:3]}")

    def test_tail_names_the_certificate_form(self):
        """Хвост — единственное, что различает девять одинаковых «Перечень и порядок…».

        ⚠ Смотрим ИМЕННО часть РАЗДЕЛА (после « — »), а не весь `section_title`. Первая редакция
        теста проверяла целую строку и была СЛЕПОЙ: `part_title` («…товара формы СТ-1») называет
        форму сам, поэтому тест проходил и с полностью отключённой сборкой хвоста. Поймано
        мутацией, а не чтением."""
        sections = {(r["section_title"] or "").split(" — ", 1)[-1] for r in _recs(self)
                    if "Перечень и порядок" in (r.get("section_title") or "")}
        for form in ("СТ-1", "СТ-2", "СТ-3", "EAV", '"A"', "зерно", "общей формы"):
            self.assertTrue(any(form in s for s in sections),
                            f"форма {form} не названа в ЗАГОЛОВКЕ РАЗДЕЛА: {sorted(sections)[:2]}")

    def test_multiline_parenthesis_is_closed(self):
        """«Особенности оформления…» + «(на товары, вывозимые… / … / таможенными органами)»."""
        got = [t for t in {r["section_title"] for r in _recs(self)}
               if "Особенности оформления сертификатов формы СТ-1" in t]
        self.assertTrue(got, "раздел не найден")
        self.assertIn("таможенными органами)", got[0])
        for t in {r["section_title"] for r in _recs(self)}:
            self.assertEqual(t.count("("), t.count(")"), f"скобка не закрыта: {t[:70]}")


class TestHeadingNeverEatsANorm(unittest.TestCase):
    """⚠ Сборка заголовка обязана останавливаться на пункте — иначе она съест норму."""

    def test_point_stops_the_assembly(self):
        lines = ["Раздел 3. Перечень и порядок предоставления документов,",
                 "необходимых для получения сертификата формы СТ-1",
                 "3.1 Выдача сертификата формы СТ-1 осуществляется уполномоченной ТПП"]
        title, last = L._p14_section_title(lines, 0, "Перечень и порядок предоставления документов,")
        self.assertEqual(title,
                         "Перечень и порядок предоставления документов, "
                         "необходимых для получения сертификата формы СТ-1")
        self.assertEqual(last, 1, "сборка обязана остановиться ПЕРЕД пунктом")

    def test_point_right_after_a_comma_is_not_eaten(self):
        """⚠ РАЗЛИЧАЮЩИЙ случай: заголовок оборван запятой, а следующая строка — ПУНКТ.

        Предыдущий тест этого не ловит: там сборка и так останавливается, потому что к третьей
        строке заголовок уже дособран и запятой не кончается. Опасность возникает ровно тогда,
        когда «продолжение» и норма — одна и та же строка. Слепоту нашла мутация."""
        lines = ["Раздел 3. Что-то,", "3.1 Норма пункта, которую нельзя съесть заголовком"]
        title, last = L._p14_section_title(lines, 0, "Что-то,")
        self.assertEqual(title, "Что-то,", "заголовок съел НОРМУ")
        self.assertEqual(last, 0)

    def test_new_section_stops_the_assembly(self):
        lines = ["Раздел 3. Что-то,", "Раздел 4. Другое"]
        title, last = L._p14_section_title(lines, 0, "Что-то,")
        self.assertEqual(title, "Что-то,")
        self.assertEqual(last, 0)

    def test_complete_heading_absorbs_nothing(self):
        """Отрицательный контроль: законченный заголовок не тянет следующую строку."""
        lines = ["Раздел 1. Термины и понятия", "Далее идёт обычный текст раздела"]
        title, last = L._p14_section_title(lines, 0, "Термины и понятия")
        self.assertEqual(title, "Термины и понятия")
        self.assertEqual(last, 0)

    def test_assembly_is_bounded(self):
        """Предохранитель на длину: заголовок не бывает бесконечным."""
        lines = ["Раздел 1. Начало,"] + ["ещё," for _ in range(40)]
        title, _last = L._p14_section_title(lines, 0, "Начало,")
        self.assertLessEqual(len(title.split()), L._P14_TITLE_MAX_TAIL + 2)


class TestConsumedHeadingLinesAreSkipped(unittest.TestCase):
    """⚠⚠ Строки, ушедшие в заголовок, не должны второй раз попасть в ТЕЛО раздела.

    На сегодняшнем корпусе это ненаблюдаемо: все многострочные заголовки стоят у разделов С
    пунктами, а тело таких разделов собирается из пунктов. Поэтому мутация «не пропускать
    съеденные строки» не роняла ни один тест по числу записей — слепоту нашла мутация, и
    закрывается она СИНТЕТИЧЕСКИМ разделом БЕЗ пунктов, где тело идёт целиком."""

    CORPUS = "\n".join([
        "Приложение 1",
        "к приказу ТПП России",
        "от 01.03.2024 N 14",
        "",
        "Раздел 1. Термины и понятия,",
        "применяемые в настоящем Положении",
        "Партия товара - товары, поставляемые по одному или нескольким документам.",
        "Декларант - лицо, которое декларирует товары либо от имени которого декларируются.",
    ])

    def test_heading_tail_does_not_leak_into_the_section_body(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "synthetic.txt"
            p.write_text(self.CORPUS, encoding="utf-8")
            recs = L.parse_prikaz14(p)

        self.assertTrue(recs, "синтетический раздел без пунктов обязан дать запись")
        self.assertIn("применяемые в настоящем Положении", recs[0]["section_title"],
                      "хвост заголовка обязан быть В ЗАГОЛОВКЕ")
        for r in recs:
            self.assertNotIn("применяемые в настоящем Положении", r["text"],
                             "строка заголовка попала В ТЕЛО раздела — она прочитана дважды")


class TestReportedFixWasRejectedOnPurpose(unittest.TestCase):
    """⚠⚠ Запреты, купленные замером: сюда НЕ надо заводить `head_buf`."""

    def test_no_intro_records_are_created(self):
        """`head_buf` завёл бы обрывки заголовков как самостоятельные пункты (класс D12)."""
        intro = [r for r in _recs(self) if "(вводная)" in (r.get("source_anchor") or "")]
        self.assertEqual(intro, [], "у Приказа №14 вводных записей быть не должно")

    def test_section_record_numbering_does_not_collide_with_real_points(self):
        """MED-4 как ИСПОЛНЯЕМЫЙ предупредительный знак.

        `_section_records` нумерует запись раздела как `section_roman.k`, а у Приказа
        `section_roman` уже равен `{прил}.{раздел}` — то есть `3.5.1` совпал бы с настоящим
        пунктом 5.1 приложения 3. Сейчас коллизии нет, потому что вводных записей нет вовсе.
        Тест упадёт ровно в тот момент, когда кто-то заведёт их, не починив нумерацию."""
        recs = _recs(self)
        keys = collections.Counter((r["doc_type"], r["section_roman"], r["point"]) for r in recs)
        dup = [k for k, n in keys.items() if n > 1]
        self.assertEqual(dup, [], f"ключ by_key не уникален — parent_intro уедет не туда: {dup[:3]}")


class TestAttributionWasNotBrokenToBeginWith(unittest.TestCase):
    """⚠ Тяжесть находки отчёта проверена и оказалась завышенной — фиксируем факт."""

    def test_every_record_has_an_owner(self):
        self.assertEqual([r for r in _recs(self) if not (r.get("section_title") or "").strip()],
                         [])

    def test_anchors_are_unique(self):
        anchors = collections.Counter(r["source_anchor"] for r in _recs(self))
        self.assertEqual([a for a, n in anchors.items() if n > 1], [])


class TestSngParserIsUntouched(unittest.TestCase):
    """Радиус: правка Приказа не имеет права трогать Соглашение."""

    def test_sng_record_count_is_stable(self):
        sng = ROOT / "knowledge_base" / "pp719" / "sng_origin_rules.txt"
        if not sng.exists():
            self.skipTest("sng_origin_rules.txt не сгенерирован")
        self.assertEqual(len(L.parse_sng_origin(sng)), 66)


class TestRecordCountIsStable(unittest.TestCase):
    def test_prikaz14_still_gives_287(self):
        """Заголовок собирается из строк, которые и так не попадали в записи: 287 -> 287."""
        self.assertEqual(len(_recs(self)), 287)


if __name__ == "__main__":
    unittest.main()
