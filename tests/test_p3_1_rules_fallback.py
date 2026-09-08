"""`P3-1` #131 — добор процедурной ветки: ЗАМЕРЕН И ОТКАЧЕН. Отрицательный результат, исполняемо.

ЧТО ПРЕДЛАГАЛОСЬ. При закрытии `P3` #121 остаток гейта `is_procedural` был вынесен сознательно:
14 из 15 пропусков — утвердительные формулировки без процедурного глагола и без темы, регулярками
их не взять. Структурный ответ: если товарная выдача не дала уверенного совпадения, попробовать
корпус норм, прежде чем отвечать «подходящей позиции не нашёл».

ПОЧЕМУ НЕ ЖИВЁТ — ЗАМЕРОМ, А НЕ РАССУЖДЕНИЕМ (08.09.2026, свод 254 вопроса):

  * флаг низкой релевантности поднимается на **51 вопросе вне сферы из 53** — сам по себе он не
    отделяет ничего, и весь вопрос во втором сомножителе;
  * ПОРОГ сходства по корпусу норм: 4 целевых из 15 при запасе **0.0021** — запоминание одного
    вопроса, а не разделение классов. Третий отказ порога в проекте после 03.09;
  * ТАБЛИЦА ТЕМ корпуса без якоря: 8 целевых, но живой прогон дал утечки на **5 вопросах из 6**,
    включая «как получить загранпаспорт и какие сроки». `_RULES_TOPIC` собрана из широких стеблей
    и строилась РАНЖИРОВАТЬ документы для вопросов, уже дошедших до ветки;
  * ТАБЛИЦА ТЕМ + 719-якорь: утечек 0, но целевых **один**, и ни одного из трёх живых пробелов
    релизной проверки;
  * безъякорные кандидаты + порог 0.8362: 9 целевых из 10, запас 0.0197 — **не внедрено**:
    единственная популяция, на которой такой порог калибруется, построена в тот же день под эту
    же правку.

⚠⚠⚠ ЗАЧЕМ ЭТОТ ФАЙЛ. Без исполняемой записи следующий раунд заведёт добор заново — так уже было
с гейтом вне-сферы (03.09: заведён и откачен тем же днём, отрицательный результат закреплён
тестами). Здесь утверждается ОТСУТСТВИЕ добора в горячем пути и СОХРАННОСТЬ того, что от работы
осталось полезного: вынесенный флаг и два набора вопросов, которых свип раньше не видел.

Разбор — docs/eval_runs/2026-09-08_p3_1_rules_fallback.md
"""

from __future__ import annotations

import json
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag import pipeline, procedural  # noqa: E402
from app.rag.retriever import Hit  # noqa: E402


def _hit(match: bool = False) -> Hit:
    return Hit(score=0.5, section_roman="I", section_title="Раздел", product_name="Продукция",
               okpd2_codes=["28.92.21.110"], min_threshold=None, requirement_blocks=[],
               source_anchor="прил., п. 1", okpd2_match=match)


class TestFallbackIsNotInTheHotPath(unittest.TestCase):
    """Добор замерен и откачен — в горячем пути его быть не должно."""

    def test_predicate_is_gone(self):
        self.assertFalse(hasattr(procedural, "rules_fallback_applies"),
                         "предикат добора вернулся — он замерен и отвергнут, см. шапку модуля")

    def test_pipeline_does_not_consult_the_corpus_topic_table_for_routing(self):
        # ⚠ ПО `co_names` КОД-ОБЪЕКТА, А НЕ ПО ТЕКСТУ ФУНКЦИИ: урок раунда 10 — сторож, считавший
        # имя по телу функции, покраснел на верном коде, потому что имя встретилось в
        # комментарии. Здесь комментарий как раз ОБЪЯСНЯЕТ откат и называет `rules_topic`.
        self.assertNotIn("rules_topic", pipeline._plan_answer.__code__.co_names)

    def test_weak_product_retrieval_still_answers_from_the_product_branch(self):
        """Поведенческая половина: слабая товарная выдача НЕ уводит вопрос на процедурную ветку."""
        called: list[str] = []
        with unittest.mock.patch.object(pipeline, "embed_query", lambda *a, **k: [0.0] * 8), \
             unittest.mock.patch.object(pipeline, "search", lambda *a, **k: [_hit(False)]), \
             unittest.mock.patch.object(pipeline, "search_cases", lambda *a, **k: []), \
             unittest.mock.patch.object(pipeline, "dense_top1", lambda *a, **k: 0.70), \
             unittest.mock.patch.object(pipeline, "_answer_procedural",
                                        lambda *a, **k: called.append("проц")), \
             unittest.mock.patch.object(pipeline.settings, "RERANK_ENABLED", False):
            plan = pipeline._plan_answer("какие сведения о производителе указываются в заявке")
        self.assertEqual(called, [], "добор вернулся в горячий путь")
        self.assertTrue(getattr(plan, "low_relevance", False),
                        "флаг низкой релевантности перестал подниматься — замер опирался на него")


class TestWhatSurvivedIsKept(unittest.TestCase):
    """Полезное из работы: вынесенный флаг и два набора, которых инструмент раньше не видел."""

    def test_low_relevance_decision_is_callable(self):
        """⚠ Вынесено из `_plan_answer` без изменения поведения: пока решение жило двумя строками
        внутри, замерить его можно было только повторив в скрипте — оракулом, повторяющим модель
        кода. Он ловит опечатку, но не ошибку модели."""
        # ⚠ На заглушках: `dense_top1` ходит в Qdrant, а сторож обязан быть офлайновым — иначе он
        # зелен у меня и красен в CI (урок `EV18`, 08.09.2026, в тот же день).
        self.assertTrue(callable(pipeline.product_low_relevance))
        with unittest.mock.patch.object(pipeline, "dense_top1", lambda *a, **k: 0.70), \
             unittest.mock.patch("app.rag.scope.out_of_scope_by_classifier", lambda *a, **k: False):
            self.assertTrue(pipeline.product_low_relevance("q", None, [_hit(False)], [], None),
                            "слабая выдача перестала поднимать флаг")
            self.assertFalse(pipeline.product_low_relevance("q", None, [_hit(True)], [], None),
                             "совпадение по коду перестало быть сигналом уверенности")

    def test_registered_gaps_are_in_the_population(self):
        """Три вопроса релизной проверки жили ТОЛЬКО в каталоге релиза: свип по этому классу
        печатал не «чисто», а «не считаю»."""
        data = json.loads((ROOT / "scripts" / "eval_route_gaps.json").read_text(encoding="utf-8"))
        got = {c["query"] for c in data["cases"]}
        for q in ("до какого числа подавать отчёт о произведённой продукции",
                  "включает ли стоимость реализации НДС",
                  "сроки проведения экспертизы происхождения"):
            self.assertIn(q, got)
        src = (ROOT / "scripts" / "eval_routing.py").read_text(encoding="utf-8")
        self.assertIn("eval_route_gaps.json", src, "набор не подключён к своду")

    def test_offdomain_procedural_words_set_is_wired(self):
        """⚠⚠ ГЛАВНЫЙ ВЫЖИВШИЙ АРТЕФАКТ. Этот набор нашёл и находку HIGH о доборе, и ДВА дефекта
        самого гейта, старше её. Без него «вне сферы 0» означало «не считаю»."""
        data = json.loads((ROOT / "scripts" / "eval_offdomain_procedural_words.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(len(data["cases"]), 14)
        self.assertIn("как получить загранпаспорт и какие сроки",
                      {c["query"] for c in data["cases"]},
                      "именной контроль вне-сферы этого проекта выпал из набора")
        src = (ROOT / "scripts" / "eval_routing.py").read_text(encoding="utf-8")
        self.assertIn("eval_offdomain_procedural_words.json", src, "набор не подключён к своду")

    def test_the_rejection_is_written_where_the_code_was(self):
        """⚠ Отрицательный результат обязан жить В ТОМ МЕСТЕ, куда правку захотят вернуть."""
        src = (ROOT / "app" / "rag" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("ЗАМЕРЕН И ОТКАЧЕН", src)
        self.assertIn("0.0021", src, "цена порога не названа числом — вернут не глядя")


if __name__ == "__main__":
    unittest.main()
