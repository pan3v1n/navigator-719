"""Ревью PR #137, РАУНД 5: шесть находок, две HIGH и одна MEDIUM-HIGH. Офлайн.

⚠⚠ ПЯТЫЙ РАУНД ПОДРЯД С HIGH (7 → 5 → 7 → 5 → 6 находок, 2 → 2 → 3 → 2 → 2 HIGH). И ОБЕ HIGH
пятого раунда порождены моей же правкой четвёртого: сузив правило продолжения до «ровно одна
значимая ячейка», я убрал загрязнение — и заодно выбросил условия у ПЯТИ настоящих позиций.

1. HIGH — пять позиций (8535, 8536, 1504, 880400000, 902129000) остались с ПУСТЫМ условием, а
   853990800 — с обрывком собственного имени. Контроль «без условия» стоял с порогом 10 %, а
   5 из 245 — это 2 %, и он промолчал.
2. HIGH — `format_for_context` утверждал «включён в Перечень условий» с ПУСТЫМ условием, под
   промптом «используй ТОЛЬКО это значение». Хуже загрязнения: модель получает «общее правило не
   применяется» и ничего взамен.
3. MEDIUM-HIGH — круговая раздача квоты изменила состав окна на оси ДОКУМЕНТОВ (кластер жалоб №1).
   Замерено на настоящем корпусе, см. `TestFinding3DocumentsAxisMeasured`.
4. MED — `719` в товарном сигнале глушил уступку у самого класса, ради которого `K14` делалась.
5. LOW — `_range_relation` замыкался на `exact` до глубокой сверки границ: чем ПОДРОБНЕЕ код
   пользователя, тем вероятнее он ошибочно принимался.
6. LOW — `ред` матчился внутри обычных слов («опРЕДеляет», «пРЕДприятия») и глотал код.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import procedural, st1_ref  # noqa: E402
from app.rag.okpd2_ref import extract_tnved_position  # noqa: E402
from app.tools.navigator import extract_okpd2  # noqa: E402

TABLE = ROOT / "knowledge_base" / "classifiers" / "tnved_st1_conditions.json"


def rows() -> list[dict]:
    return json.loads(TABLE.read_text(encoding="utf-8"))["rows"]


def route(q: str) -> bool:
    return procedural.is_procedural(q, has_code=bool(extract_okpd2(q)))


class TestFinding1NoConditionIsLost(unittest.TestCase):
    """Ни одной записи без условия — и контроль конвертера обязан ловить ОДНУ такую."""

    def test_every_row_has_a_condition(self):
        empty = [r["code"] for r in rows()
                 if not r.get("excluded") and not (r.get("condition") or "").strip()]
        self.assertEqual(empty, [], "условие снова выброшено вместе с переносом имени")

    def test_named_victims_are_back(self):
        by_code = {r["code"]: r for r in rows()}
        for code in ("8535", "8536", "880400000", "902129000"):
            with self.subTest(code=code):
                self.assertTrue((by_code[code]["condition"] or "").strip(), code)
                self.assertTrue(by_code[code]["condition"].startswith("Изготовление"), code)

    def test_no_name_fragment_leaked_into_a_condition(self):
        bad = [r["code"] for r in rows() if re.match(r"^\d", r.get("condition") or "")]
        self.assertEqual(bad, [], "в графу условия снова уехал обрывок наименования")

    def test_control_catches_the_real_proportion(self):
        """⚠⚠ ДОЛЯ ВЗЯТА НАСТОЯЩАЯ: 5 пустых из 245 — это 2 %, и прежний порог 10 % молчал.

        Первая редакция теста брала ОДНУ строку из одной — там любой порог срабатывает, и тест
        проходил бы и со старым контролем. Сэмпл обязан воспроизводить ту пропорцию, на которой
        дефект и прятался.
        """
        import convert_st1_perechen as conv

        def row(code, cond):
            return {"code": code, "code_to": None, "partial": False, "name": "x",
                    "condition": cond, "excluded": False}

        sample = ([row(f"{8000 + i}", "Изготовление из материалов") for i in range(240)]
                  + [row(f"{9000 + i}", "") for i in range(5)])
        forms = {"exact4": 1, "exact6": 1, "exact10": 1, "from": 1, "range": 1,
                 "excluded": 1, "editions": 1, "continuation": 1}
        problems = conv.controls(sample, forms)
        self.assertTrue(any("без условия" in p for p in problems),
                        f"контроль молчит на 2 % пустых — ровно как прежний порог: {problems}")


class TestFinding2EmptyConditionNeverClaimsInclusion(unittest.TestCase):
    """Лукап не утверждает «включён», если условия нет."""

    def test_guard_on_empty_condition(self):
        import unittest.mock

        fake = {"available": True, "matched": True, "code": "9999", "narrower": [],
                "exact": [{"code": "9999", "name": "Тест", "condition": "   "}],
                "general_rule": "ОБЩЕЕ", "source": "ИСТОЧНИК"}
        with unittest.mock.patch.object(st1_ref, "conditions_for", lambda _c: fake):
            block = st1_ref.format_for_context("9999")
        self.assertIn("текст условия в таблице отсутствует", block)
        self.assertNotIn("ОБЩЕЕ", block, "нельзя подставлять общее правило: запись в Перечне ЕСТЬ")

    def test_normal_case_unchanged(self):
        block = st1_ref.format_for_context("8403")
        self.assertIn("включён в Перечень", block)
        self.assertIn("50%", block)


class TestFinding3DocumentsAxisMeasured(unittest.TestCase):
    """⚠⚠ Круговая раздача МЕНЯЕТ состав окна на оси документов — это замерено и записано.

    На настоящем корпусе «какие документы нужны для получения акта экспертизы»: Приказ №52
    6 мест → 5, одно уходит Методрекомендациям (второй документ темы `documents` по `K15`).
    Ни один критерий не просел: атрибуция@1 0.96, представленность 1.00, нужный пункт 5/5.

    ⚠ Тест держит НАМЕРЕНИЕ, а не сегодняшние числа: документ по теме обязан сохранять
    БОЛЬШИНСТВО мест — `K9` показал, что ответ строится вокруг первого источника, и уравнять
    всех было бы регрессией.
    """

    def test_topic_document_keeps_the_majority(self):
        import unittest.mock
        from types import SimpleNamespace

        from app.rag import retriever

        def pt(doc, n):
            return SimpleNamespace(id=f"{doc}-{n}", score=1.0, payload={
                "doc_type": doc, "point": str(n), "text": f"{doc} {n}",
                "section_roman": "I", "source_anchor": f"{doc} п.{n}"})

        wide = ([pt("tpp_order_52", i) for i in range(6)]
                + [pt("metodrek_tpp", i) for i in range(3)]
                + [pt("rules_registry", i) for i in range(3)])

        def fh(query, limit, qfilter=None, collection=None, qvec=None):
            for c in (getattr(qfilter, "must", None) or []):
                if getattr(c, "key", None) == "doc_type":
                    doc = getattr(getattr(c, "match", None), "value", None)
                    return [pt(doc, i) for i in range(3)]
            return list(wide[:limit])

        fc = unittest.mock.Mock()
        fc.collection_exists.return_value = True
        with unittest.mock.patch.object(retriever, "_client", lambda: fc), \
             unittest.mock.patch.object(retriever, "_hybrid", fh), \
             unittest.mock.patch.object(retriever, "rules_topic", lambda _q: "tpp_order_52"), \
             unittest.mock.patch.object(retriever, "asks_document_list", lambda _q: False):
            hits = retriever.search_rules("какие документы", limit=6,
                                          primary_docs=("tpp_order_52", "metodrek_tpp"))
        docs = [h.get("doc_type") for h in hits]
        self.assertEqual(docs.count("tpp_order_52"), 3,
                         f"документ по теме потерял гарантированные места: {docs}")
        self.assertIn("metodrek_tpp", docs, "второй документ темы (K15) не получил места")


class TestFinding4DecreeNumberIsNotAProductSignal(unittest.TestCase):
    """Номер постановления в вопросе про СТ-1 — не признак товарного вопроса."""

    def test_st1_questions_mentioning_719(self):
        for q in ("какие требования по СТ-1, если продукции нет в приложении 719",
                  "какие требования для получения СТ-1 по постановлению 719",
                  "моей продукции нет в приложении 719, можно ли получить СТ-1"):
            with self.subTest(q=q[:44]):
                self.assertTrue(route(q), q)

    def test_mixed_questions_still_product(self):
        """⚠ Положительная половина: узкий якорь держит их и без сигнала «719»."""
        for q in ("сколько баллов нужно для 28.22.16.111, код ТН ВЭД 8428 10",
                  "какие требования по 719 к продукции с кодом ТН ВЭД 8501 40 200 0",
                  "подпадает ли под требования продукция ТН ВЭД 8479 89 970 8"):
            with self.subTest(q=q[:44]):
                self.assertFalse(route(q), q)


class TestFinding5DeeperCodeIsNotMoreAccepted(unittest.TestCase):
    """Чем подробнее код, тем строже граница — а не наоборот."""

    def test_codes_past_the_upper_bound(self):
        for code in ("150610", "15061010", "1506101000"):
            with self.subTest(code=code):
                res = st1_ref.conditions_for(code)
                self.assertEqual(res["exact"], [], f"{code} принят за пределами диапазона")
                self.assertEqual(res["narrower"], [], code)

    def test_inside_the_range_still_matches(self):
        self.assertTrue(st1_ref.conditions_for("1505")["exact"], "диапазон перестал работать")


class TestFinding6WordBoundaryOnActKeywords(unittest.TestCase):
    """`ред` — сокращение, а не кусок слова «определяет»."""

    def test_ordinary_words_do_not_swallow_the_code(self):
        for q in ("ТН ВЭД, условия для предприятия 8403",
                  "какие условия достаточной переработки, ТН ВЭД определяет 8403"):
            with self.subTest(q=q[:44]):
                self.assertEqual(extract_tnved_position(q), "8403", q)

    def test_real_act_requisites_still_skip(self):
        self.assertIsNone(
            extract_tnved_position("условия по ТН ВЭД, постановление 719 от 2020"))
        self.assertEqual(
            extract_tnved_position("в ред. от 08.06.2023 условия по ТН ВЭД 8403"), "8403")


class TestNoStrayControlCharacters(unittest.TestCase):
    """⚠⚠⚠ СЕДЬМОЙ РАЗ ЗА СЕССИЮ: heredoc превращал `\\b` в РЕАЛЬНЫЙ символ backspace (0x08).

    Он попадал внутрь raw-строки регулярки, `grep` его не видит, шаблон молча переставал работать
    (`\\x08` — не граница слова, а управляющий символ). Дважды это делало предохранитель
    неработающим при зелёном тесте: `_GROUP_ROW` не матчил ни одной строки, `_TNVED_POSITION_SKIP_RE`
    пропускал реквизиты акта. Один дешёвый сторож закрывает весь класс.
    """

    def test_sources_are_free_of_control_characters(self):
        bad = []
        for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for ch in ("\x08", "\x0c", "\x1b", "\x00"):
                if ch in text:
                    bad.append(f"{path.relative_to(ROOT)}: {ch!r}")
        self.assertEqual(bad, [], "в исходниках управляющие символы — почти наверняка съеденный \\b")


if __name__ == "__main__":
    unittest.main()
