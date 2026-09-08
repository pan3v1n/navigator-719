"""`P3-1` #131 — добор процедурной ветки при низкой релевантности товарной выдачи. Офлайн.

ЗАЧЕМ. `P3` #121 снизила пропуски гейта `is_procedural`, но остаток вынесла сознательно:
утвердительные формулировки без процедурного глагола и без темы регулярками не берутся
(«какие сведения о производителе указываются в заявке»). Структурный ответ был назван при
закрытии `P3` и здесь исполнен: сначала спрашиваем товарный корпус, и только если он ответить
не может — пробуем корпус норм.

⚠⚠⚠ ЧТО ЗДЕСЬ ЗАКРЕПЛЯЕТСЯ ИСПОЛНЯЕМО, ПОМИМО САМОЙ ПРАВКИ, — ОТРИЦАТЕЛЬНЫЙ РЕЗУЛЬТАТ.
Разрешать добор ПОРОГОМ сходства по корпусу норм замерено и НЕ ГОДИТСЯ: полосы у целевых
вопросов (0.8230-0.9057) и у вне-сферы (0.7348-0.8660) сходятся вплотную, при нуле утечек
порог ловит 4 пропуска из 15 с запасом **0.0021** — то есть запоминает один вопрос, а не
разделяет классы. Это третий раз, когда порог по этому корпусу не разделяет (03.09 — гейт
вне-сферы процедурной ветки, заведён и откачен тем же днём). Без исполняемой записи следующий
раунд заведёт его заново, поэтому `TestThresholdWasMeasuredAndRejected` утверждает ОТСУТСТВИЕ
порога в предикате.

Замер (свод 240 вопросов, `scripts/eval_routing.py --with-fallback`):
    пропуски 18 → 9   ложных на товарных 1 → 1   вне сферы 0   документные `K12` целы
    ось `K9` (`eval_golden_rules`): пропусков 10 → 4

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
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

from app.rag import procedural  # noqa: E402
from app.rag.retriever import Hit, rules_topic  # noqa: E402


# Вопросы, которые добор ОБЯЗАН пускать: у каждого своя тема в таблице корпуса норм и нет
# ни процедурного маркера, ни темы `topics` — то есть гейт их не берёт по построению.
TARGETS = (
    "сроки проведения экспертизы происхождения",          # polozhenie49_tpp — приехал с K16
    "какие сведения о производителе указываются в заявке",  # tpp_order_52
    "как подтвердить производство компонентов",             # tpp_order_52
)

# ⚠⚠ РОВНО ЭТИ ДВА ВОПРОСА ВНЕ СФЕРЫ ПОЛУЧАЮТ ТЕМУ КОРПУСА — замерено на своде, а не придумано.
# Таблица тем видит «перечень документов» и не видит предмета вопроса. Без дисквалификатора
# членства добор дал бы 2 утечки вне сферы при требовании РОВНО НОЛЬ.
CHAMBER_LEAKS = (
    "какие документы нужны для вступления в ТПП",
    "какой перечень документов нужен для членства в ТПП",
)


def _hit(match: bool = False) -> Hit:
    return Hit(score=0.5, section_roman="I", section_title="Раздел", product_name="Продукция",
               okpd2_codes=["28.92.21.110"], min_threshold=None, requirement_blocks=[],
               source_anchor="прил., п. 1", okpd2_match=match)


class TestFallbackPredicate(unittest.TestCase):
    """Предикат `rules_fallback_applies` — чистая функция, Qdrant не нужен."""

    def test_targets_are_admitted(self):
        for q in TARGETS:
            with self.subTest(q=q[:40]):
                self.assertFalse(procedural.is_procedural(q),
                                 "вопрос перестал быть пропуском гейта — набор устарел")
                self.assertTrue(procedural.rules_fallback_applies(q))

    def test_chamber_membership_is_refused(self):
        """⚠ Несущий предохранитель: снятие строки про членство даёт 2 утечки вне сферы."""
        for q in CHAMBER_LEAKS:
            with self.subTest(q=q[:40]):
                self.assertIsNotNone(rules_topic(q),
                                     "вопрос перестал получать тему корпуса — риск изменился, "
                                     "перемерить свип, а не править тест")
                self.assertFalse(procedural.rules_fallback_applies(q))

    def test_question_without_a_corpus_topic_is_refused(self):
        """Отрицательный контроль: нет темы корпуса — нет добора."""
        for q in ("посоветуй хороший рецепт борща",
                  "какая завтра погода в Курске",
                  "до какого числа подавать отчёт о произведённой продукции"):
            with self.subTest(q=q[:40]):
                self.assertIsNone(rules_topic(q))
                self.assertFalse(procedural.rules_fallback_applies(q))


class TestThresholdWasMeasuredAndRejected(unittest.TestCase):
    """Отрицательный результат закрепляется исполняемо — иначе его заведут заново.

    ⚠⚠ Порог сходства по корпусу норм в предикате быть НЕ ДОЛЖЕН. Замер 08.09.2026: при нуле
    утечек он берёт 4 пропуска из 15 с запасом 0.0021, тогда как таблица тем берёт 8 при том же
    нуле. Полосы см. в шапке модуля. То же заключение получено 03.09 с другой стороны
    (`docs/eval_runs/2026-09-03_rules_relevance_gate.md`).
    """

    def test_predicate_does_not_consult_a_similarity_threshold(self):
        names = procedural.rules_fallback_applies.__code__.co_names
        # ⚠ ПО `co_names`, А НЕ ПО ТЕКСТУ ФУНКЦИИ. Урок раунда 10: сторож, считавший имя по ТЕЛУ
        # функции, покраснел на верном коде, потому что имя встретилось в комментарии. В имена
        # код-объекта проза не попадает по построению.
        for forbidden in ("dense_top1", "RELEVANCE_SOFT", "SOFT", "cosine"):
            self.assertNotIn(forbidden, names,
                             f"в предикат вернулся порог сходства ({forbidden}) — он замерен "
                             "и отвергнут, см. шапку модуля")

    def test_admission_is_decided_by_the_corpus_topic_table(self):
        """Положительная половина: предикат обязан СПРАШИВАТЬ таблицу тем, а не игнорировать её."""
        self.assertIn("rules_topic", procedural.rules_fallback_applies.__code__.co_names)


class TestFallbackWiring(unittest.TestCase):
    """Провод в `_plan_answer`: добор срабатывает ровно при низкой релевантности. На заглушках."""

    def _run(self, query: str, *, okpd2_match: bool, dense: float, enabled: bool = True,
             cases: list | None = None, scope_out: bool = True):
        from app.core.config import settings
        from app.rag import pipeline

        called: list[str] = []
        saved = settings.PROCEDURAL_DEFLECT_ENABLED
        settings.PROCEDURAL_DEFLECT_ENABLED = enabled
        try:
            with unittest.mock.patch.object(pipeline, "embed_query", lambda *a, **k: [0.0] * 8), \
                 unittest.mock.patch.object(pipeline, "search",
                                            lambda *a, **k: [_hit(okpd2_match)]), \
                 unittest.mock.patch.object(pipeline, "search_cases",
                                            lambda *a, **k: list(cases or [])), \
                 unittest.mock.patch("app.rag.scope.out_of_scope_by_classifier",
                                     lambda *a, **k: scope_out), \
                 unittest.mock.patch.object(pipeline, "dense_top1", lambda *a, **k: dense), \
                 unittest.mock.patch.object(pipeline, "_answer_procedural",
                                            lambda *a, **k: called.append("проц") or "PROC"), \
                 unittest.mock.patch.object(pipeline.settings, "RERANK_ENABLED", False):
                pipeline._plan_answer(query)
        finally:
            settings.PROCEDURAL_DEFLECT_ENABLED = saved
        return called

    def test_weak_product_retrieval_falls_back(self):
        # dense ниже RELEVANCE_SOFT, совпадения по коду нет → флаг поднят, тема корпуса есть.
        self.assertEqual(self._run(TARGETS[0], okpd2_match=False, dense=0.70), ["проц"])

    def test_confident_product_retrieval_is_left_alone(self):
        """⚠⚠ НЕСУЩИЙ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ — ради него добор стоит ПОСЛЕ поиска, а не в гейте.

        Тот же признак триггером `is_procedural` замерен и отвергнут: он забирает три вопроса,
        называющих ПРОДУКЦИЮ (`golden#19`, и оба смешанных документных `K12` — `golden#48`,
        `#49`), которые обязаны остаться товарными. Гейт стоит ДО поиска и о силе товарной
        выдачи не знает. Если этот тест зеленеет при переносе признака в гейт — он слеп.

        ⚠ ТРИ ПУТИ УВЕРЕННОСТИ, А НЕ ОДИН, И ЭТО НАШЁЛ САМ ТЕСТ. Первая редакция считала «высокого
        косинуса достаточно» и покраснела на ВЕРНОМ коде: у этого вопроса флаг поднимает ВТОРОЙ
        сигнал — классификатор ОКПД2 «вне покрытия 719», — а он смотрит на `confident`, а не на
        косинус. Проверяем все три: совпадение по коду, подтверждённый кейс, высокий косинус при
        молчащем классификаторе.
        """
        self.assertEqual(self._run(TARGETS[0], okpd2_match=True, dense=0.70), [],
                         "совпадение по коду ОКПД2 — уверенность, добора быть не должно")
        self.assertEqual(self._run(TARGETS[0], okpd2_match=False, dense=0.70,
                                   cases=[{"query": "к", "answer": "о", "_score": 0.9}]), [],
                         "подтверждённый кейс — уверенность, добора быть не должно")
        self.assertEqual(self._run(TARGETS[0], okpd2_match=False, dense=0.99, scope_out=False), [],
                         "высокий косинус при молчащем классификаторе — флаг не поднят")

    def test_disabled_procedural_branch_disables_the_fallback(self):
        """Настройка выключает ветку целиком — добор обязан выключаться вместе с ней."""
        self.assertEqual(self._run(TARGETS[0], okpd2_match=False, dense=0.70, enabled=False), [])


class TestSweepSeesTheEffectiveRoute(unittest.TestCase):
    """Инструмент замера — сам объект проверки (предупреждение из постановки #131)."""

    def test_registered_gaps_are_in_the_population(self):
        """⚠⚠ Три вопроса релизной проверки жили ТОЛЬКО в каталоге релиза: свип по этому классу
        печатал не «чисто», а «не считаю»."""
        data = json.loads((ROOT / "scripts" / "eval_route_gaps.json").read_text(encoding="utf-8"))
        got = {c["query"] for c in data["cases"]}
        for q in ("до какого числа подавать отчёт о произведённой продукции",
                  "включает ли стоимость реализации НДС",
                  "сроки проведения экспертизы происхождения"):
            self.assertIn(q, got)
        sets = __import__("importlib").import_module("scripts.eval_routing") \
            if "scripts" in sys.modules else None
        del sets  # импорт скрипта как пакета не нужен — состав наборов проверяем ниже текстом
        src = (ROOT / "scripts" / "eval_routing.py").read_text(encoding="utf-8")
        self.assertIn("eval_route_gaps.json", src, "набор не подключён к своду")

    def test_predicate_admits_targets_and_leaks_nothing_out_of_scope(self):
        """⚠⚠ ХРАПОВИК НА ОБЕ СТОРОНЫ ЗАМЕРА, И ОН ОФЛАЙНОВЫЙ.

        Считает по всему своду, скольких пропусков гейта предикат касается. Это НЕОБХОДИМОЕ
        условие добора (достаточное — низкая релевантность товарной выдачи, её без Qdrant не
        посчитать), поэтому числа здесь — верхняя граница, а не эффективный маршрут.

        ⚠ Сегодняшний урок `EV18`: тест, которому понадобился Qdrant, зелен у меня и красен в CI.
        Предикат и `collect()` — чистые, поэтому сторож остаётся в батарее, а живая половина
        меряется свипом.
        """
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import eval_routing
        finally:
            sys.path.pop(0)
        rows = eval_routing.collect()  # без --with-fallback: Qdrant не нужен
        missed = [r for r in rows if not r["procedural"]]
        admitted = [r for r in missed if procedural.rules_fallback_applies(r["query"])]

        leaks = [r for r in admitted if r["class"] == "вне сферы"]
        self.assertEqual([(r["set"], r["id"]) for r in leaks], [],
                         "добор касается вопроса ВНЕ СФЕРЫ — требование #131 «ровно 0»")

        targets = [r for r in admitted if r["class"] == "процедурный"]
        # Замерено 08.09.2026: 11 пропусков гейта проходят предикат, из них 8 доезжают до добора
        # (у трёх товарная выдача уверенная). Число может только РАСТИ — иначе правка что-то
        # потеряла и это надо увидеть, а не узнать через релиз.
        self.assertGreaterEqual(len(targets), 11,
                                f"предикат стал пускать меньше целевых: {len(targets)}")

    def test_sweep_declares_which_route_it_counted(self):
        """⚠⚠ Свип считает ГЕЙТ; добор живёт в пайплайне. Без строки режима «пропусков N» читается
        как утверждение о продукте, хотя это утверждение об одной из двух половин маршрута."""
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import eval_routing
        finally:
            sys.path.pop(0)
        rows = [{"set": "s", "id": 1, "class": "товарный", "query": "q",
                 "procedural": False, "by_fallback": False, "why": "", "topic": None}]
        out = "\n".join(eval_routing.summarize(rows))
        self.assertIn("НЕ УЧТЁН", out)
        rows[0].update(procedural=True, by_fallback=True)
        self.assertIn("ЭФФЕКТИВНЫЙ маршрут", "\n".join(eval_routing.summarize(rows)))


if __name__ == "__main__":
    unittest.main()
