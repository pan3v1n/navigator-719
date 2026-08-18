"""Инструменты замеров EV1–EV4: то, что можно проверить офлайн, без Qdrant и DeepSeek.

Замерный код опаснее продуктового: если он врёт, врут все решения, принятые по его числам.
В этом проекте так уже было — диагностика сравнивала якоря, обрезанные до 28 символов, и
показывала «ретрив стабилен», хотя нестабилен был именно он. Поэтому чистые части метрик
(разбор утверждений, n-граммы, отбор кейсов, фильтр ПДн) закрыты тестами.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.core.prompts import NAVIGATOR_SYSTEM_PROMPT  # noqa: E402
from app.core.sensitive import detect  # noqa: E402


def _rule_3v() -> str:
    """Текст правила 3в целиком — по ГРАНИЦАМ, а не по магическим 1100 символам.

    Срез по длине уже подводил: правило подросло на 200 символов (`EV7`), и утверждение про
    «СРАВНИТЬ» уехало за окно — тест позеленел бы на выпавшем требовании.

    ⚠ И по БЛИЖАЙШЕЙ границе: правило 3г, добавленное 18.08, попало внутрь среза до «4. СТИЛЬ», и
    утверждение «правило 3в отдаёт формулировку контексту» стало выполняться текстом СОСЕДНЕГО
    правила — тест зеленел бы и после удаления самой проверяемой фразы (ревью PR #94)."""
    tail = NAVIGATOR_SYSTEM_PROMPT.split("3в.")[1]
    for boundary in ("\n3г.", "\n4. СТИЛЬ"):
        if boundary in tail:
            return tail.split(boundary)[0]
    return tail


def _sample_context() -> str:
    """Настоящий контекст с целевой позицией и нецелевым кандидатом — для проверок ПО РЕНДЕРУ."""
    from app.rag.pipeline import format_context
    from app.rag.retriever import Hit

    def hit(name, ops, thr=None):
        return Hit(score=1.0, section_roman="III", section_title="—", product_name=name,
                   okpd2_codes=["28.15.10"], min_threshold=thr, source_anchor="Раздел III",
                   requirement_blocks=[{"operations": ops}])
    return format_context([hit("Целевая", [{"text": "сборка", "points": 30}], "не менее 60 баллов"),
                           hit("Кандидат", [{"text": "литьё", "points": 55}], "не менее 90 баллов")])


class TestForeignNumbersRule(unittest.TestCase):
    """EV1: правило 3в — числа только из позиции, о которой идёт речь."""

    def test_rule_forbids_numbers_of_other_candidates(self):
        self.assertIn("3в.", NAVIGATOR_SYSTEM_PROMPT)
        rule = _rule_3v()
        self.assertIn("БЕЗ баллов", rule)
        # исключение обязано остаться: прямая просьба сравнить позиции — законный сценарий
        self.assertIn("СРАВНИТЬ", rule)

    def test_rule_matches_what_context_actually_contains(self):
        """EV7: требований кандидатов в контексте больше нет — правило обязано это ЗНАТЬ.

        Иначе промпт спорит с контекстом: он разрешал бы «назвать без баллов» то, чего в контексте
        нет вовсе, и оставлял бы открытым второй, обратный риск — заявить «требований у позиции
        нет». Оба запрета обязаны быть в правиле дословно, потому что цена второго выше первого:
        «требований не найдено» при живых требованиях — это дефект класса `D4`."""
        rule = _rule_3v()
        self.assertIn("НЕ ПОКАЗАНЫ", rule)      # правило ссылается на строку найденных данных
        self.assertIn("НЕ утверждать, что требований у них нет", rule)
        self.assertIn("попроси его код", rule)              # путь для разбора кандидата
        # ⚠ И та же строка обязана быть в РЕНДЕРЕ, а не в исходнике. Проверка через
        # `inspect.getsource` зеленела бы на закомментированной строке — тест «правило ссылается на
        # то, чего нет» не поймал бы ничего. Поэтому строим настоящий контекст.
        self.assertIn("Требования этой позиции НЕ ПОКАЗАНЫ", _sample_context())

    def test_rule_does_not_reinstate_the_R7_defect(self):
        """Первая версия 3в велела на позиции без баллов писать «баллы не приведены» и просить код.

        Оба указания спорили с кодом, который специально шёл другим путём: `format_context` при
        требованиях-перечне печатает «Порог: не предусмотрен — требования заданы ПЕРЕЧНЕМ
        обязательных операций» именно потому, что молчание заставляло модель писать «в контексте не
        указан», а это читалось как пробел в данных и было **жалобой №1** платного теста (R7).
        Просьба уточнить код спорила с правилом 1ж, которое её прямо запрещает при совпадении по
        коду. Позиций без баллов в корпусе — сотни, так что цена ошибки не краевая."""
        rule = _rule_3v()
        self.assertNotIn("попроси уточнить код", rule)
        self.assertNotIn("баллы для этой позиции в контексте не приведены", rule)
        # правило обязано отдавать этот случай КОНТЕКСТУ, а не решать за него
        self.assertIn("не предусмотрен", rule)

    def test_context_still_owns_the_no_points_wording(self):
        """R7 остаётся в силе: формулировку про отсутствие порога задаёт КОНТЕКСТ, а не промпт.

        ⚠ Проверяется рендером, а не исходником: `inspect.getsource` зеленеет и на комментарии."""
        from app.rag.pipeline import format_context
        from app.rag.retriever import Hit
        listed = Hit(score=1.0, section_roman="III", section_title="—",
                     product_name="Позиция с перечнем", okpd2_codes=["28.15.10"],
                     min_threshold=None, source_anchor="Раздел III",
                     requirement_blocks=[{"operations": [{"text": "сварка"}, {"text": "сборка"}]}])
        ctx = format_context([listed])
        self.assertIn("Порог: не предусмотрен", ctx)
        self.assertIn("ПЕРЕЧНЕМ", ctx)

    def test_rule_sits_after_code_priority_rule(self):
        """3в опирается на «позицию, на которую опираешься» из правил 3 и 3а — порядок важен."""
        self.assertLess(NAVIGATOR_SYSTEM_PROMPT.index("3а."), NAVIGATOR_SYSTEM_PROMPT.index("3в."))


class TestForeignNumbersOracle(unittest.TestCase):
    """⚠⚠ ОРАКУЛ МЕТРИКИ НЕ ВЫВОДИТСЯ ИЗ АРТЕФАКТА, КОТОРЫЙ ОНА ПРОВЕРЯЕТ.

    Прежняя версия `foreign_numbers` добывала эталон «чужих чисел» рендером
    `format_context([target, h])` — той самой функции, из которой `EV7` требования кандидатов и
    убрал. После правки разность стала пуста ПО ПОСТРОЕНИЮ: гейт «без чужих чисел» показывал 1.00
    при любом поведении модели, и на этом основании было отчитано «гейт пройден впервые».
    Тот же класс, что «эталон нельзя размечать регулярками» (`K12`): метрика проверяет себя.
    Эти тесты краснеют, если оракул снова начнут строить из рендера."""

    @staticmethod
    def _hit(name, code, ops, thr=None, match=False):
        from app.rag.retriever import Hit
        return Hit(score=0.9, product_name=name, section_roman="XXIV", section_title="—",
                   okpd2_codes=[code], min_threshold=thr, okpd2_match=match,
                   requirement_blocks=[{"operations": ops}], source_anchor=name)

    def _pair(self):
        target = self._hit("Насосы подачи жидкостей прочие", "28.13.14.110",
                           [{"text": "сборка", "points": 20}], thr="не менее 60 баллов")
        cand = self._hit("Насосы технологические типов ВВ1 для крупнотоннажных производств СПГ",
                         "28.13.14.110",
                         [{"text": "литьё", "points": 110}, {"text": "механообработка", "points": 450}],
                         thr="не менее 750 баллов")
        return target, cand

    def test_oracle_sees_numbers_the_context_no_longer_renders(self):
        """Числа кандидата видны оракулу, ХОТЯ в контексте их нет — иначе метрика слепа."""
        from app.rag.pipeline import claim_numbers, format_context
        from eval_determinism import foreign_numbers
        target, cand = self._pair()
        ctx = format_context([target, cand])
        self.assertNotIn("450", claim_numbers(ctx), "контекст всё ещё несёт числа кандидата")
        self.assertEqual(foreign_numbers("центробежные насосы", [target, cand]),
                         {"110", "450", "750"})

    def test_oracle_counts_expert_case_numbers(self):
        """Кейс эксперта про ЧУЖУЮ продукцию — оставшийся канал чужих чисел, и он виден.

        `_plan_answer` кладёт `format_cases` и в промпт, и в строку заземления, а правило 1а даёт
        кейсу высший приоритет. После `EV7` это единственный канал, который остался, — прежняя
        метрика на него не смотрела вовсе."""
        from eval_determinism import foreign_numbers
        target, cand = self._pair()
        case = {"product_name": "Пластикат поливинилхлоридный", "okpd2": "20.16.30.110",
                "expert_answer": "Порог — не менее 300 баллов, доля не более 50 процентов."}
        leaked = foreign_numbers("насосы", [target, cand], [case])
        self.assertIn("300", leaked)
        self.assertIn("50", leaked)
        # кейс про ТУ ЖЕ продукцию чужим не считается
        own_case = dict(case, okpd2="28.13.14.110",
                        expert_answer="Порог — не менее 999 баллов.")
        self.assertNotIn("999", foreign_numbers("насосы", [target, cand], [own_case]))

    def test_oracle_does_not_flag_legitimate_sources(self):
        """Ложные срабатывания дороже пропусков: точный код и одна расколотая ячейка — свои."""
        from app.rag import fragments
        from eval_determinism import foreign_numbers
        target, cand = self._pair()
        # назван ТОЧНЫЙ код обеих записей — они обе целевые, чужого нет
        self.assertEqual(foreign_numbers("насосы", [target, cand], None, "28.13.14.110"), set())
        # строки одной расколотой ячейки — один источник, а не чужая продукция
        a = self._hit("Светодиоды белого диапазона", "26.11.22.216",
                      [{"text": "сборка кристалла", "points": 30}])
        b = self._hit("Светодиоды (в части светодиодов белого диапазона)", "26.11.22.210",
                      [{"text": "технические условия", "points": 25}])
        with unittest.mock.patch.object(fragments, "group_of", return_value=1):
            self.assertEqual(foreign_numbers("светодиоды белого диапазона", [a, b]), set())

    def test_oracle_reads_thresholds_of_nodes_and_texts(self):
        """Числа записи живут не только в `points`: пороги узлов (D9) и величины внутри текстов."""
        from eval_determinism import record_numbers
        h = self._hit("Кандидат", "28.13.14.110", [], thr=None)
        h.requirement_blocks = [{
            "min_threshold": "не менее 100 баллов",
            "note": "при доле импорта не более 40 процентов",
            "operations": [{"text": "механообработка не менее 15 процентов", "points": 7}],
        }]
        self.assertEqual(record_numbers(h), {"100", "40", "15", "7"})


class TestAnswerHasNoInternalVocabulary(unittest.TestCase):
    """Правило 3г: внутренней кухни («контекст», «промпт») в ответе быть не должно.

    ⚠ Дефект был НЕ в модели, а в самом промпте: шаблон ответа дословно предписывал писать
    «**Порог:** <дословно, если есть; иначе „в контексте не указан“>». То есть инструкция сама
    выносила наружу слово, которого пользователь не знает, и ответ читался как пробел в данных —
    класс R7, жалоба №1 платного теста. Тест закрывает шаблон, а не поведение модели."""

    def test_output_template_does_not_dictate_internal_words(self):
        lines = [l for l in NAVIGATOR_SYSTEM_PROMPT.splitlines() if "**Порог:**" in l]
        self.assertTrue(lines, "шаблон ответа потерял строку «Порог»")
        for l in lines:
            self.assertNotIn("контекст", l.lower())
        self.assertIn("не приведён", " ".join(lines))

    def test_rule_forbids_internal_vocabulary(self):
        self.assertIn("3г.", NAVIGATOR_SYSTEM_PROMPT)
        rule = NAVIGATOR_SYSTEM_PROMPT.split("3г.")[1].split("4. СТИЛЬ")[0]
        self.assertIn("контекст", rule)          # правило называет запрещённое слово
        self.assertIn("не предусмотрены", rule)  # и даёт замену, а не только запрет

    def test_metric_watches_the_same_words(self):
        """Метрика полноты обязана мерить этот запрет — иначе он живёт только в промпте."""
        from eval_completeness import KITCHEN_RE
        self.assertTrue(KITCHEN_RE.search("в контексте баллы не указаны"))
        self.assertTrue(KITCHEN_RE.search("судя по промпту"))
        # ⚠ и не ловит слова из САМИХ требований приложения
        self.assertFalse(KITCHEN_RE.search("руководство по эксплуатации и инструкция по монтажу"))


class TestClarifyingClassifier(unittest.TestCase):
    """Калибровка метрики полноты (`EV7`): что считать УТОЧНЯЮЩИМ ответом.

    Классификатор решает, исключать ли кейс из полноты, — то есть напрямую двигает метрику. Первая
    версия считала уточняющим любой ответ с фразой «укажите код», и после `EV7` (контекст сам велит
    просить код у кандидата) из полноты уехали ответы, довёзшие ВСЕ баллы целевой позиции. Доля
    оставалась 1.00, но проверялась уже не вся выборка, а три четверти."""

    @staticmethod
    def _hits(match: bool = False):
        class H:
            okpd2_match = match
        return [H()]

    def test_substantive_answer_asking_for_code_is_not_clarifying(self):
        from eval_completeness import is_clarifying
        text = ("**Позиция:** «Спецмашиностроение», Бульдозеры гусеничные (ОКПД2 28.92.21) [1].\n"
                "**Требования с балльной оценкой:**\n- сварка рамы — 15 баллов [1].\n"
                "**Что уточнить:** если продукция — одна из соседних позиций, укажите код.")
        self.assertFalse(is_clarifying(text, self._hits()))

    def test_rule_1g_shaped_answer_is_clarifying(self):
        from eval_completeness import is_clarifying
        text = ("Точного совпадения по «слесарный инструмент» не нашёл. Ближайшие позиции:\n"
                "- Инструмент ручной — ОКПД2 25.73.30 [1]\n"
                "- Инструмент слесарно-монтажный — ОКПД2 25.73.30.290 [2]\n"
                "Если ваша продукция — одна из них, укажите код, и я приведу требования и баллы.")
        self.assertTrue(is_clarifying(text, self._hits()))
        self.assertNotIn("**Позиция:", text)  # форма правила 1г: опоры на позицию нет

    def test_code_match_is_never_clarifying(self):
        from eval_completeness import is_clarifying
        text = "Точного совпадения не нашёл. Ближайшие позиции: - что-то [1]. Укажите код."
        self.assertFalse(is_clarifying(text, self._hits(match=True)))

    def test_clarify_phrase_alone_does_not_flip_a_normal_answer(self):
        """«Уточните у заявителя…» — обычная часть разбора, а не признак неподтверждённой позиции."""
        from eval_completeness import is_clarifying
        text = ("**Позиция:** «Насосное оборудование», Насосы центробежные (ОКПД2 28.13.14) [1].\n"
                "Уточните наименование сервисного центра у заявителя.")
        self.assertFalse(is_clarifying(text, self._hits()))


class TestBorderlineSet(unittest.TestCase):
    """EV2: набор пограничных in-scope — вторая половина весов guard'а."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / "scripts" / "eval_golden_borderline.json")
                              .read_text(encoding="utf-8"))
        cls.cases = cls.data["cases"]

    def test_size_and_shape(self):
        self.assertGreaterEqual(len(self.cases), 20)
        for c in self.cases:
            self.assertTrue(c["in_scope"], c["id"])
            self.assertTrue(c["query"].strip(), c["id"])
            self.assertTrue(c["evidence"], f"{c['id']}: кейс без признака in-scope бесполезен")
            for e in c["evidence"]:
                self.assertIn(e, ("expert", "retrieval"), c["id"])

    def test_no_personal_data(self):
        """152-ФЗ: тексты взяты из боевой базы, поэтому проверка обязательна, а не «на всякий случай»."""
        for c in self.cases:
            self.assertEqual(detect(c["query"]), [], f"{c['id']}: ПДн в наборе репозитория")

    def test_no_okpd2_code_in_queries(self):
        """Ловушка отбора: при совпадении по коду guard подавлен, и такой кейс мерит не то."""
        import re
        for c in self.cases:
            self.assertIsNone(re.search(r"\b\d{2}\.\d{2}(?:\.\d+)*\b", c["query"]),
                              f"{c['id']}: код ОКПД2 в пограничном кейсе")

    def test_queries_are_unique(self):
        qs = [c["query"].lower() for c in self.cases]
        self.assertEqual(len(qs), len(set(qs)))

    def test_guard_script_knows_the_set(self):
        import eval_guard
        self.assertTrue(eval_guard.BORDER.exists())
        self.assertTrue(hasattr(eval_guard, "run_borderline"))
        self.assertTrue(hasattr(eval_guard, "run_threshold"))

    def test_default_mode_runs_both_halves_of_the_scales(self):
        """По негативам одним порог хочется поднимать бесконечно — цена видна только вместе с
        ложными флагами. Прежний `both` молча пропускал эту половину, а докстринг обещал «два
        режима», когда их стало пять."""
        import inspect

        import eval_guard
        src = inspect.getsource(eval_guard.main)
        for mode in ("run_negative", "run_borderline", "run_threshold", "run_confusable"):
            self.assertIn(mode, src)
        # каждая секция должна запускаться и при `both`
        self.assertIn('"borderline", "both", "guard"', src)
        self.assertIn('"threshold", "both", "guard"', src)
        self.assertIn("borderline", eval_guard.__doc__)
        self.assertNotIn("Два режима", eval_guard.__doc__)

    def test_threshold_curve_reports_plateau_not_argmin(self):
        """Равные суммы на сетке 0.005 — норма, и «первый минимум» всегда самый permissive конец:
        прочитав его как рекомендацию, порог понизят и добавят false-accept за нулевой выигрыш."""
        import inspect

        import eval_guard
        src = inspect.getsource(eval_guard.run_threshold)
        self.assertIn("plateau", src)
        self.assertIn("ПЛАТО", src)
        self.assertIn("НЕ равноценны", src)

    def test_determinism_window_matches_production(self):
        """Гейт «чужие числа» при окне 5 структурно не видит утечек с рангов 6–8, а прод берёт 8."""
        import inspect

        import eval_determinism

        from app.rag import pipeline
        src = inspect.getsource(eval_determinism.main)
        self.assertIn('"--limit", type=int, default=8', src)
        prod = inspect.signature(pipeline.answer).parameters["limit"].default
        self.assertEqual(prod, 8, "боевое окно изменилось — гейт замера обязан идти за ним")


class TestTextFaithfulnessCore(unittest.TestCase):
    """EV4: разбор утверждений и n-граммы — чистая часть, её и проверяем."""

    @classmethod
    def setUpClass(cls):
        import eval_text_faithfulness as m
        cls.m = m

    def test_table_rows_are_not_claims(self):
        """Перечень баллов печатает КОД дословно — ссылка [N] ему не нужна, искажать нечего."""
        text = "Вот требования [1].\n| Операция | Баллы |\n|---|---|\n| сварка кузова | 6 |"
        got = self.m.claims(text)
        self.assertEqual(got, ["Вот требования [1]."])

    def test_meta_statements_are_excluded(self):
        """Мета-утверждения предписаны промптом; штрафовать за них — оптимизировать в молчание."""
        for meta in ("**Порог:** в контексте для этой позиции не указан [1].",
                     "Уточните, к какой категории относится ваша машина [1].",
                     "**Важно:** показаны 60 из 676 операций [1]."):
            self.assertTrue(self.m.META_RE.search(meta), meta)

    def test_paraphrase_is_grounded_and_invention_is_not(self):
        ctx = "Выполнение операций сварки кузова и окраски кузова на территории Российской Федерации"
        grams = self.m._ngrams(ctx)
        near = self.m.grounded_share("сварки кузова на территории Российской Федерации", grams)
        far = self.m.grounded_share("лазерная резка титанового профиля", grams)
        self.assertGreater(near, far)
        self.assertEqual(far, 0.0)

    def test_novel_words_ignore_generic_connectives(self):
        ctx_words = set(self.m.tokenize("сварка кузова на территории Российской Федерации"))
        self.assertEqual(self.m.novel_words("это также требование [1]", ctx_words), [])
        novel = self.m.novel_words("гальваническое покрытие", ctx_words)
        self.assertTrue(any(w.startswith("гальваническ") for w in novel), novel)


class TestGsNaturalBuilder(unittest.TestCase):
    """EV3: сборка gs_natural из оценённых ответов волны — на синтетической базе."""

    @classmethod
    def setUpClass(cls):
        import build_gs_natural as m
        cls.m = m

    def _db(self, rows, feedback):
        path = Path(self.tmp) / "wave.db"
        db = sqlite3.connect(path)
        db.execute("create table messages (id integer primary key, session_id text, ts text, "
                   "role text, content text, sources_json text, low_relevance integer)")
        db.execute("create table feedback (id integer primary key, message_id integer, rating integer)")
        db.executemany("insert into messages values (?,?,?,?,?,?,?)", rows)
        db.executemany("insert into feedback (message_id, rating) values (?,?)", feedback)
        db.commit()
        db.close()
        return path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_takes_rated_pair_and_drops_the_rest(self):
        anchor = json.dumps([{"source_anchor": "Раздел IV, поз. 12", "okpd2": ["28.13.14"]}],
                            ensure_ascii=False)
        rows = [
            (1, "s1", "t1", "user", "делаем гидравлические насосы для нефтепереработки", None, 0),
            (2, "s1", "t2", "assistant", "ответ", anchor, 0),
            (3, "s2", "t3", "user", "да", None, 0),                       # продолжение диалога
            (4, "s2", "t4", "assistant", "ответ", anchor, 0),
            (5, "s3", "t5", "user", "какой порядок внесения в реестр", None, 0),   # процедурный
            (6, "s3", "t6", "assistant", "ответ", anchor, 0),
            (7, "s4", "t7", "user", "производим станки, СНИЛС 112-233-445 95", None, 0),  # ПДн
            (8, "s4", "t8", "assistant", "ответ", anchor, 0),
            (9, "s5", "t9", "user", "выпускаем промышленные чиллеры", None, 0),
            (10, "s5", "t10", "assistant", "ответ", "[]", 0),             # без якоря позиции
        ]
        fb = [(2, 5), (4, 5), (6, 5), (8, 5), (10, 5)]
        cases, stats = self.m.collect(self._db(rows, fb), 4, "3")
        self.assertEqual([c["query"] for c in cases],
                         ["делаем гидравлические насосы для нефтепереработки"])
        self.assertEqual(cases[0]["expect"]["anchor"], "Раздел IV, поз. 12")
        self.assertEqual(stats["отброшено по ПДн"], 1, "вопрос с ПДн обязан отсеяться")

    def test_low_rating_is_not_evidence(self):
        anchor = json.dumps([{"source_anchor": "Раздел IV, поз. 12"}], ensure_ascii=False)
        rows = [(1, "s1", "t1", "user", "делаем гидравлические насосы", None, 0),
                (2, "s1", "t2", "assistant", "ответ", anchor, 0)]
        cases, _ = self.m.collect(self._db(rows, [(2, 3)]), 4, "3")
        self.assertEqual(cases, [], "оценка 3★ не подтверждает, что позиция верна")

    def test_flagged_answer_is_not_a_gold_label(self):
        """Оценка ≥4★ на ответе с поднятым флагом означает «честно сказал, что совпадения нет».

        Правило 1г в этом случае ЗАПРЕЩАЕТ называть баллы и требует список кандидатов, поэтому
        `source_anchor` там — догадка ретрива, а не подтверждённая экспертом позиция. Взяв её
        эталоном, мы бы мерили главную цифру ретрива по собственной догадке."""
        anchor = json.dumps([{"source_anchor": "Раздел XXII, поз. 3"}], ensure_ascii=False)
        rows = [(1, "s1", "t1", "user", "пластичная высокотемпературная смазка", None, 0),
                (2, "s1", "t2", "assistant", "Точного совпадения не нашёл…", anchor, 1)]
        cases, stats = self.m.collect(self._db(rows, [(2, 5)]), 4, "3")
        self.assertEqual(cases, [], "кейс с поднятым флагом не может быть эталоном")
        self.assertEqual(stats["отброшено: guard поднимал флаг"], 1)


if __name__ == "__main__":
    unittest.main()
