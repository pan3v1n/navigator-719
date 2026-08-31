"""`K14` #35, шаг 4 — маршрут вопросов по ВТОРОМУ КЛЮЧУ (СТ-1 / ТН ВЭД). Офлайн, без Qdrant и LLM.

ЗАЧЕМ. Корпус построен на ОКПД2, а требования СТ-1 живут в разрезе ТН ВЭД. Развилка
`is_procedural` решает, встретит ли вопрос Соглашение СНГ и Приказ ТПП РФ №14 вообще: на товарной
ветке этих документов нет ни при каком качестве поиска.

ЧТО ЧИНИЛОСЬ (замер — docs/eval_runs/2026-08-31_k14_route_variants.md, четыре варианта + три
отрицательных контроля, победил `б+`: 16 из 19 при НУЛЕВОМ радиусе на 191 вопросе):

* **словаря второго ключа в гейте не было вовсе.** Ни «СТ-1», ни «ТН ВЭД» не встречались в
  `procedural.py` ни маркером, ни якорем — при том что `topics.py` знает оба. 19 из 19 вопросов
  пробника уходили ТОВАРНОЙ веткой;
* ⚠⚠ **пять вопросов с темой `st1_origin` в приёмочных наборах проходили по СЛУЧАЙНЫМ причинам**
  (ГИСП, Минпромторг, ТПП, «страна происхождения», «подпункт г») и создавали видимость покрытия:
  голое «как получить СТ-1» было товарным;
* **третий случай класса `#120`:** `procedural.py` требовал предлог («сертификат О происхождении»),
  а `topics.py` давно сделал его необязательным с комментарием «так пишут в половине реальных
  вопросов». Урок снова не переехал через модуль;
* **регулярка темы не знала формы САМОГО документа** — «достаточной ОБРАБОТКИ/переработки»
  (13 вхождений в корпусном тексте) против записанного «достаточной переработки».

⚠ Каждое утверждение ниже проверено МУТАЦИЕЙ: снятие своего предохранителя роняет свой тест.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.rag import procedural, topics  # noqa: E402

ST1_SET = ROOT / "scripts" / "eval_st1_route.json"
# Пометка в `note` кейса, объявляющая записанный ПРЕДЕЛ правки, а не ожидание.
RESIDUAL_MARK = "ОСТАЁТСЯ ТОВАРНЫМ"


def route(q: str) -> bool:
    """Маршрут как в рантайме (`pipeline.py`): гейт зовётся с признаком кода ОКПД2."""
    from app.tools.navigator import extract_okpd2

    return procedural.is_procedural(q, has_code=bool(extract_okpd2(q)))


class TestSecondKeyVocabulary(unittest.TestCase):
    """Токены второго ключа сами по себе выводят на процедурную ветку."""

    def test_st1_token_alone_is_enough(self):
        # ⚠ Именно ГОЛЫЕ формы: до правки все три были товарными, а «Т.е. нам, чтобы внести песок
        # надо через ГИСП подать заявку на СТ-1?» проходило — но по якорю ГИСП, не по СТ-1.
        for q in ("как получить СТ-1", "что такое СТ-1", "нужен ли мне СТ-1", "нужен ли ст1"):
            with self.subTest(q=q):
                self.assertTrue(route(q), f"вопрос про СТ-1 обязан идти процедурной веткой: {q}")

    def test_tnved_token_alone_is_enough(self):
        for q in ("мой код ТН ВЭД 8544 49 910 0, какие условия",
                  "как перевести код ОКПД2 в ТН ВЭД для СТ-1"):
            with self.subTest(q=q):
                self.assertTrue(route(q))

    def test_agreement_terms_are_anchors(self):
        for q in ("что такое кумулятивный принцип", "как считается адвалорная доля",
                  "какие операции не отвечают критерию достаточной переработки"):
            with self.subTest(q=q):
                self.assertTrue(route(q))


class TestPrepositionAsymmetry(unittest.TestCase):
    """Класс `#120`: предлог не должен решать маршрут."""

    def test_certificate_of_origin_both_forms(self):
        with_prep = "срок действия сертификата о происхождении"
        without = "срок действия сертификата происхождения"
        self.assertTrue(route(with_prep))
        # ⚠ ЭТО И ЕСТЬ ДЕФЕКТ: до правки вторая форма уходила товарной веткой, отличаясь одним
        # предлогом. Оба утверждения нужны вместе — иначе тест зеленел бы и на прежнем шаблоне.
        self.assertTrue(route(without), "форма без предлога — половина реальных вопросов")

    def test_source_wording_of_criterion_gets_topic(self):
        # Форма самого документа: «достаточной ОБРАБОТКИ/переработки», 13 вхождений в корпусе.
        self.assertEqual(topics.classify("что такое критерий достаточной обработки/переработки"),
                         "st1_origin")
        self.assertEqual(topics.classify("критерий достаточной переработки"), "st1_origin")


class TestDisqualifierYieldsNarrowly(unittest.TestCase):
    """Товарный дисквалификатор уступает ЯВНОМУ токену второго ключа — и только ему."""

    def test_requirements_by_tnved_is_not_a_product_question(self):
        # Дословное ожидание эксперта из ТЗ `K14`: «укажите ваш код ТН ВЭД, распишу требования».
        # ⚠ «укажите ваш код ТН ВЭД, распишите требования» отсюда УБРАНА ревью PR #137: она
        # пересекается с путём `T9` по слову «требования», а `T9` обязан идти товарной веткой.
        # Уступка осталась для форм с явным токеном СТ-1 — они с `T9` не пересекаются.
        for q in ("по какому коду смотреть требования СТ-1",
                  "у вас итоговый документ скорее всего СТ-1, какие требования по коду ТН ВЭД"):
            with self.subTest(q=q):
                self.assertTrue(route(q))

    def test_chamber_membership_still_disqualifies(self):
        # ⚠⚠ ОТРИЦАТЕЛЬНАЯ ПОЛОВИНА УСТУПКИ. Второй ключ снимает ТОВАРНЫЙ дисквалификатор, но не
        # вопрос про САМУ ПАЛАТУ: членство и взносы — вне 719 независимо от того, зачем они нужны.
        # Без этого утверждения уступка была бы шире, чем описана.
        self.assertFalse(route("как вступить в ТПП, чтобы потом получить СТ-1"))

    def test_product_questions_stay_product(self):
        for q in ("какие требования к бульдозерам 28.92.21.110",
                  "сколько баллов нужно для лифтов 28.22.16.111"):
            with self.subTest(q=q):
                self.assertFalse(route(q), "товарный вопрос по 719 обязан остаться товарным")


class TestSweepCoversSecondKey(unittest.TestCase):
    """⚠⚠ Положительный контроль на САМ ИНСТРУМЕНТ: свип обязан считать второй ключ.

    Пока набора не было, свип по ТН ВЭД не мерил ничего, и его «ноль ложных срабатываний»
    означало «не считаю» (класс `P3` #121). Тест сторожит, что набор подключён и не выпал.
    """

    def test_set_is_wired_into_sweep(self):
        import eval_routing

        names = [name for name, _, _ in eval_routing.SETS]
        self.assertIn("eval_st1_route.json", names)

    def test_sweep_actually_collects_st1_questions(self):
        import eval_routing

        rows = [r for r in eval_routing.collect() if r["set"] == "st1_route"]
        self.assertEqual(len(rows), 21)
        # Контроли обязаны попасть в ТОВАРНЫЙ класс — иначе их перехват читался бы как улучшение.
        controls = [r for r in rows if r["class"] == "товарный"]
        self.assertEqual(len(controls), 2)

    def test_notes_are_the_contract(self):
        """Кейс, помеченный как записанный предел, обязан быть товарным; остальные — процедурными.

        ⚠ Так предел живёт в ДАННЫХ, а не в памяти: молчаливая починка одного из трёх остатков
        уронит тест и потребует обновить пометку вместе с замером радиуса.
        """
        cases = json.loads(ST1_SET.read_text(encoding="utf-8"))["cases"]
        residual = [c for c in cases if RESIDUAL_MARK in c.get("note", "")]
        # ⚠ Четвёртым остаток стал по итогу ревью PR #137 (MED-4/MED-5): кейс 2 отдан
        # ЗАМЕРЕННОЙ ценой за то, чтобы таможенные вопросы не получали 719-ответ, а путь
        # `T9` работал. Число здесь — не порог качества, а перепись записанных пределов.
        self.assertEqual(len(residual), 4, "список записанных пределов разошёлся с данными")
        for c in cases:
            if c.get("kind") == "control_product":
                continue
            want = RESIDUAL_MARK not in c.get("note", "")
            with self.subTest(id=c["id"]):
                self.assertEqual(route(c["query"]), want, c["query"])


if __name__ == "__main__":
    unittest.main()
