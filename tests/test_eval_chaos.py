"""Предохранители хаос-проверки уровня 4 (`EV22` #130, `scripts/eval_chaos.py`).

⚠⚠ ЗАЧЕМ. Этот скрипт НАМЕРЕННО ломает боевой сервис, которым пользуются живые люди, и делает
это через канал, который рвётся. Цена ошибки здесь не «неверное число в отчёте», а «бой остался
лежать». Поэтому решающая логика вынесена в чистую функцию `judge()` и закреплена тестами: на
самой боевой машине её проверять уже поздно.

ЧТО ЗАКРЕПЛЕНО:
1. Три исхода, а не два. Сценарий, где поломка НЕ ВОСПРОИЗВЕЛАСЬ, не имеет права называться
   пройденным: «ошибок нет» у несработавшего хаоса выглядит ровно как у здорового фолбэка.
2. Невосстановившийся сервис — провал, даже если фолбэк был образцовым.
3. Выдумка при отвалившейся зависимости — провал (гайд требует ЧЕСТНОГО фолбэка).
4. Отсутствие второго контура восстановления сообщается ГРОМКО, а не молчанием.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import eval_chaos as C  # noqa: E402
import eval_level4 as L4  # noqa: E402


def probe(stream=L4.OK, plain=L4.OK, text="ответ по существу", unverified=(), sources=6):
    """⚠ `sources` — несущий признак здоровья, а не украшение: при лежащем Qdrant сервис отвечает
    кодом 200 и пустым списком источников, и по коду два состояния неразличимы (замерено)."""
    return {"stream_class": stream, "plain_class": plain, "plain_status": 200,
            "text": text, "unverified": list(unverified), "sources": sources}


class TestJudgeHasThreeOutcomes(unittest.TestCase):
    """⚠⚠ Несработавший хаос — НЕ «пройдено». Это тот же класс, что «ноль срабатываний» у слепого
    инструмента: снаружи неотличимо от успеха."""

    def test_healthy_during_outage_is_not_reproduced(self):
        """⚠⚠ Сервис остался здоров — поломка НЕ ВОСПРОИЗВЕЛАСЬ. Первая редакция считала
        поломкой любое расхождение ТЕКСТА, а текст у LLM разный при каждом вызове: «поломка»
        подтверждалась всегда, и вердикт был ложно положительным."""
        v = C.judge(before=probe(), during=probe(text="совсем другой текст"), after=probe())
        self.assertEqual(v["verdict"], "НЕ ВОСПРОИЗВЕДЕНО")

    def test_broken_and_recovered_passes(self):
        v = C.judge(before=probe(),
                    during=probe(text="Поиск временно недоступен, повторите позже", sources=0),
                    after=probe(), recovery_seconds=6.0)
        self.assertEqual(v["verdict"], "ПРОЙДЕН")
        self.assertEqual(v["recovery_seconds"], 6.0)

    def test_service_not_recovered_is_failure(self):
        """⚠ Даже образцовый фолбэк не спасает: невернувшийся сервис — худший исход из всех."""
        v = C.judge(before=probe(),
                    during=probe(text="временно недоступен", sources=0),
                    after=probe(text="всё ещё недоступен", sources=0))
        self.assertEqual(v["verdict"], "ПРОВАЛЕН")
        self.assertIn("НЕ ВЕРНУЛСЯ", v["why"])

    def test_recovery_judged_by_sources_not_by_status(self):
        """⚠⚠ Ровно та ловушка, на которой сгорела первая редакция: деградированный ответ
        приходит с кодом 200, и проверка «класс == ok» объявляла возврат состоявшимся."""
        after_degraded = probe(plain=L4.OK, sources=0, text="похоже на временный сбой")
        self.assertFalse(C.healthy(after_degraded))
        v = C.judge(probe(), probe(text="сбой", sources=0), after_degraded)
        self.assertEqual(v["verdict"], "ПРОВАЛЕН")

    def test_empty_answer_is_failure(self):
        v = C.judge(before=probe(), during=probe(text="   ", sources=0), after=probe())
        self.assertEqual(v["verdict"], "ПРОВАЛЕН")

    def test_invention_during_outage_is_failure(self):
        """Гайд требует ЧЕСТНОГО фолбэка: выдуманные числа при отвалившейся базе — худшее,
        что может случиться, потому что выглядят как обычный ответ."""
        v = C.judge(before=probe(),
                    during=probe(text="Порог 50 баллов", unverified=["50"], sources=0),
                    after=probe())
        self.assertEqual(v["verdict"], "ПРОВАЛЕН")
        self.assertIn("выдумка", v["why"])

    def test_verdict_is_not_always_passing(self):
        """⚠ Отрицательный контроль к первому тесту: `judge`, всегда возвращающий ПРОЙДЕН,
        прошёл бы половину проверок выше."""
        verdicts = {
            C.judge(probe(), probe(), probe())["verdict"],
            C.judge(probe(), probe(text="внятно", sources=0), probe())["verdict"],
        }
        self.assertEqual(verdicts, {"НЕ ВОСПРОИЗВЕДЕНО", "ПРОЙДЕН"})


class TestBoilerplateIsNotAFallback(unittest.TestCase):
    """⚠⚠ Сырое тело HTTP-ошибки — НЕ «внятный фолбэк» (ревью PR #135). При не-200 `ask_plain`
    кладёт в `text` декодированное тело, поэтому ветка «текста нет вовсе» для ошибки С ТЕЛОМ не
    срабатывает никогда, и голая заглушка фреймворка проходила бы как внятное сообщение."""

    def test_framework_stub_is_failure(self):
        v = C.judge(before=probe(),
                    during=probe(plain=L4.HTTP_5XX, sources=0,
                                 text='{"detail":"Internal Server Error"}'),
                    after=probe())
        self.assertEqual(v["verdict"], "ПРОВАЛЕН")
        self.assertIn("заглушку", v["why"])

    def test_human_message_passes(self):
        """⚠ Обратная половина: короткое, но осмысленное сообщение — это ВНЯТНАЯ ошибка.
        Наблюдено на бою при недоступной LLM, и по гайду такой ответ засчитывается."""
        v = C.judge(before=probe(),
                    during=probe(plain=L4.HTTP_5XX, sources=0,
                                 text='{"detail":"Сервис временно недоступен, повторите запрос."}'),
                    after=probe(), recovery_seconds=5.0)
        self.assertEqual(v["verdict"], "ПРОЙДЕН")

    def test_boilerplate_detector_is_not_length_based(self):
        """⚠ Различаем по СМЫСЛУ, а не по длине: длинная заглушка остаётся заглушкой, короткое
        объяснение остаётся объяснением."""
        self.assertTrue(C._is_boilerplate("x" * 200 + " Internal Server Error"))
        self.assertFalse(C._is_boilerplate("Поиск недоступен"))


class TestRateLimitIsNotIllHealth(unittest.TestCase):
    """⚠⚠ Ревью раунда 5: опрос восстановления делает ДВА запроса на пробу, оба считаются одним
    счётчиком 20/мин НА ПОЛЬЗОВАТЕЛЯ, а весь заход идёт под ОДНИМ аккаунтом. При сломанной
    зависимости запросы падают мгновенно, и пауза 3 с давала 40 запросов в минуту — вдвое выше
    лимита. Через полминуты 429, `healthy()` навсегда ложна, и скрипт печатал «СЕРВИС НЕ
    ВЕРНУЛСЯ» ПРО ЗДОРОВЫЙ СЕРВИС — сразу после того, как сам сломал бой."""

    # ⚠⚠ РАСПОЗНАВАТЕЛЬ БЫЛ ЗАВЕДЁН РАУНДОМ 5 И ПОЗВАН В ОДНОМ МЕСТЕ ИЗ ТРЁХ (раунд 6, #136).
    # Ниже закреплены оставшиеся два: `during` в `judge` и `after` под истёкшим дедлайном.
    # Правка, заводящая распознаватель, обязана позвать его ВЕЗДЕ, где распознаваемое возможно.
    _BODY_429 = '{"detail":"Слишком много вопросов подряд. Повторите через 12 с."}'

    def test_429_during_is_not_a_pass(self):
        """⚠⚠ ЛОЖНО ПОЛОЖИТЕЛЬНЫЙ ВЕРДИКТ — ХУДШИЙ КЛАСС ДЕФЕКТА У ПРОВЕРКИ, КОТОРАЯ ЛОМАЕТ БОЙ.
        Тело 429 непустое и по-русски, поэтому мимо `_is_boilerplate` (он знает только английские
        формулы) выполнение доходило до `recovered = healthy(after)` и печатало «ПРОЙДЕН — фолбэк
        внятный». Инструмент НЕ НАБЛЮДАЛ ПОЛОМКУ ВОВСЕ: он упёрся в собственный лимит."""
        v = C.judge(before=probe(),
                    during=probe(plain=L4.HTTP_429, stream=L4.HTTP_429, sources=0,
                                 text=self._BODY_429),
                    after=probe(), recovery_seconds=5.0)
        self.assertEqual(v["verdict"], "НЕ ИЗМЕРЕНО")
        self.assertNotEqual(v["verdict"], "ПРОЙДЕН")

    def test_429_after_under_expired_deadline_is_not_a_failure(self):
        """⚠ Зеркальная половина: «сервис не вернулся» под троттлингом — тоже не вердикт.
        Прежде оговорка про лимит жила только в stdout, а в JSON её не было вовсе, и читающий
        отчёт видел «разбирать немедленно» про сервис, которого никто не наблюдал."""
        v = C.judge(before=probe(), during=probe(text="внятно", sources=0),
                    after=probe(plain=L4.HTTP_429, sources=0, text=self._BODY_429),
                    recovery_seconds=None, throttled=3)
        self.assertEqual(v["verdict"], "НЕ ИЗМЕРЕНО")
        self.assertEqual(v["throttled_polls"], 3)

    def test_throttle_count_reaches_the_report(self):
        """⚠ След лимита обязан быть в JSON — единственном артефакте, переживающем сессию."""
        v = C.judge(before=probe(), during=probe(text="внятно", sources=0), after=probe(),
                    recovery_seconds=5.0, throttled=2)
        self.assertEqual(v["verdict"], "ПРОЙДЕН")
        self.assertEqual(v["throttled_polls"], 2)

    def test_429_is_recognised_as_not_measured(self):
        self.assertTrue(C.rate_limited({"plain_class": L4.HTTP_429, "stream_class": L4.OK}))
        self.assertTrue(C.rate_limited({"plain_class": L4.OK, "stream_class": L4.HTTP_429}))

    def test_healthy_service_is_not_rate_limited(self):
        """⚠ Обратная половина: распознаватель, говорящий «да» всегда, тоже «ловит все 429»."""
        self.assertFalse(C.rate_limited(probe()))
        self.assertFalse(C.rate_limited(probe(plain=L4.HTTP_5XX, sources=0)))

    @staticmethod
    def _hits_in_window(poll_seconds: float, window: float = 60.0) -> int:
        """НЕЗАВИСИМЫЙ оракул: сколько запросов сценарий кладёт в минутное окно.

        ⚠⚠ СИМУЛЯЦИЯ, А НЕ ПОВТОР ФОРМУЛЫ КОДА (ревью PR #135, раунд 6, #136). Прежний тест брал
        `requests_per_probe = 2` и делил лимит на него — ту же модель «опрос единственный
        потребитель», из-за которой пол и был занижен. Оракул, повторяющий ошибку кода, ловит
        только опечатку в константе, но не ошибку МОДЕЛИ, и раунд 6 нашёл ровно это.

        Считаем ВСЕХ потребителей окна честным перебором: `before` и `during` тратят по пробе ДО
        опроса, а при сломанной зависимости пробы возвращаются за миллисекунды, то есть опрос
        идёт ровно с шагом `poll_seconds` начиная с t=0 (проба первая, пауза после — см.
        `run_scenario`)."""
        probes = 2                                    # before + during
        t = 0.0
        while t < window:                             # пробы опроса: t=0, poll, 2*poll, …
            probes += 1
            t += poll_seconds
        return probes * 2                             # `probe()` бьёт дважды: стрим + нестрим

    def test_poll_floor_counts_every_consumer_of_the_window(self):
        """⚠ Проверка не должна блокировать сама себя: все запросы сценария обязаны умещаться
        в лимит, а не только запросы опроса."""
        hits = self._hits_in_window(C.POLL_MIN_SECONDS)
        self.assertLessEqual(
            hits, L4.RATE_LIMIT_PER_MIN,
            f"при паузе {C.POLL_MIN_SECONDS} с сценарий кладёт {hits} запросов в минуту при "
            f"лимите {L4.RATE_LIMIT_PER_MIN} — проверка упрётся в собственный лимит и обвинит "
            "в этом сервис")

    def test_the_old_floor_would_now_fail(self):
        """⚠⚠ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ. Без него тест выше зелен и при возврате прежнего числа:
        7.0 с давало 4 + 9 проб × 2 = 22 хита при лимите 20 — впритык и мимо. Проверка обязана
        КРАСНЕТЬ на том значении, ради которого её переписали."""
        self.assertGreater(self._hits_in_window(7.0), L4.RATE_LIMIT_PER_MIN)


class TestWatchdogFailureIsLoud(unittest.TestCase):
    """⚠⚠ Второй контур восстановления либо есть, либо о его отсутствии сказано ПРЯМО. Молчаливо
    невзведённый сторож хуже отсутствующего: оператор считает, что подстрахован, и ломает бой."""

    def test_missing_setsid_is_reported(self):
        with mock.patch.object(subprocess, "Popen", side_effect=FileNotFoundError("setsid")):
            msg = C.arm_watchdog(["docker", "start", "qdrant"], 300)
        self.assertIn("НЕ ВЗВЕДЁН", msg)

    def test_command_survives_the_shell(self):
        """⚠⚠ Команда восстановления LLM САМА содержит одинарные кавычки
        (`sed -i '\|^127.0.0.1 api.deepseek.com$|d'`). Первая редакция «оборачивала в кавычки
        аргумент с пробелом» — внутренние кавычки закрыли бы внешние, сторож упал бы с
        синтаксической ошибкой, и НИКТО БЫ НЕ УЗНАЛ: вывод уходил в /dev/null.

        Проверяем разбором той же строки штатным лексером оболочки: argv обязан вернуться тем же."""
        import shlex
        sc = C.LLMDown(["sudo", "-n", "docker"], "/home/yc-user/navigator-719", "app", "qdrant")
        cmd = sc.watchdog()
        parsed = shlex.split(C.watchdog_command(cmd, 300))
        # ⚠ Первая редакция сравнивала `parsed[:2]` С САМИМ СОБОЙ в ветке else — тавтология,
        # зелёная при любом результате. Ровно тот класс, от которого предостерегает шапка файла.
        self.assertEqual(parsed[0], "sleep")
        self.assertTrue(parsed[1].startswith("300"), f"задержка потерялась: {parsed[1]!r}")
        # хвост после `sleep N;` обязан побайтно совпасть с исходным argv
        self.assertEqual(parsed[-len(cmd):], cmd)

    def test_watchdog_output_is_not_discarded(self):
        """⚠ Упавший сторож обязан оставить след: иначе его поломка неотличима от работы."""
        with mock.patch.object(subprocess, "Popen") as popen:
            C.arm_watchdog(["docker", "start", "qdrant"], 5)
        inner = popen.call_args.args[0][-1]
        self.assertIn(C.WATCHDOG_LOG, inner)
        self.assertNotIn("/dev/null", inner)

    def test_armed_watchdog_reports_seconds(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            msg = C.arm_watchdog(["docker", "start", "qdrant"], 300)
        self.assertIn("300", msg)
        self.assertTrue(popen.called)
        # ⚠ Отвязка обязательна: сторож должен пережить смерть родителя.
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))


class TestComposeFileIsExplicit(unittest.TestCase):
    """⚠⚠ `--compose-dir` обязан ПОПАДАТЬ в команду. Первая редакция его принимала и игнорировала:
    `docker compose stop` подхватил бы проект из текущего каталога — на бою это «какой попало»
    сервис. Аргумент, который принимается и не используется, хуже отсутствующего."""

    def test_dir_reaches_the_command(self):
        sc = C.QdrantDown(["docker"], "/home/yc-user/navigator-719", "app", "qdrant")
        cmd = " ".join(sc.watchdog())
        self.assertIn("/home/yc-user/navigator-719", cmd)
        self.assertIn("docker-compose.yml", cmd)

    def test_path_stays_posix_on_any_host(self):
        """⚠⚠ Цель скрипта всегда Linux, а собирается команда на машине разработчика (Windows).
        Через `Path` выходило `\home\yc-user\...\docker-compose.yml` — команда, проверенная
        локально, уехала бы на бой другой. Склейка строкой платформенно нейтральна."""
        sc = C.QdrantDown(["docker"], "/home/yc-user/navigator-719", "app", "qdrant")
        cmd = " ".join(sc.compose)
        self.assertIn("/home/yc-user/navigator-719/docker-compose.yml", cmd)
        self.assertNotIn("\\", cmd)

    def test_trailing_slash_does_not_double(self):
        sc = C.QdrantDown(["docker"], "/srv/app/", "app", "qdrant")
        self.assertIn("/srv/app/docker-compose.yml", " ".join(sc.compose))

    def test_llm_restore_does_not_use_sed_i(self):
        """⚠⚠ ИНЦИДЕНТ НА БОЮ 28.08.2026. `/etc/hosts` в контейнере — bind-mount отдельного файла,
        а `sed -i` правит не на месте: пишет временный рядом и ПЕРЕИМЕНОВЫВАЕТ поверх. Ядро это
        запрещает — `sed: cannot rename /etc/sedXXXX: Device or resource busy`. Поломка (`echo >>`)
        работала, снятие не работало НИКОГДА: сервис отдавал 5xx, пока не починили руками.
        Правильный способ — усечь и переписать ТОТ ЖЕ inode через `cat > файл`."""
        sc = C.LLMDown(["docker"], ".", "app", "qdrant")
        # ⚠ Первая редакция склеивала сюда `" ".join(sc.restore.__doc__)` — join по СТРОКЕ
        # вставляет пробел между КАЖДЫМ символом, поэтому «sed -i» не нашёлся бы там никогда,
        # даже если бы он там был. Проверяем реальные команды: восстановление И сторож.
        cmd = " ".join(sc.watchdog()) + " " + " ".join(sc.compose + [sc._UNDO])
        self.assertNotIn("sed -i", cmd)
        self.assertIn("cat /tmp/hosts.new > /etc/hosts", " ".join(sc.watchdog()))

    def test_watchdog_and_restore_are_the_same_idempotent_undo(self):
        """⚠ Сторож и штатное восстановление обязаны делать ОДНО И ТО ЖЕ идемпотентное действие
        — иначе один из них будет проверен, а другой нет. ⚠⚠ Но одинаковость и есть причина,
        по которой холостая проверка восстановления ОБЯЗАТЕЛЬНА: два контура на одном механизме
        — это один контур, и 28.08 они упали вместе."""
        sc = C.LLMDown(["docker"], ".", "app", "qdrant")
        self.assertEqual(sc.watchdog()[-1], sc._UNDO)

    def test_llm_restore_targets_exact_line(self):
        """⚠ Восстановление сносит ТОЧНУЮ строку, а не всё про этот хост: чужие записи в
        /etc/hosts не наши."""
        sc = C.LLMDown(["docker"], ".", "app", "qdrant")
        cmd = " ".join(sc.watchdog())
        self.assertIn(f"127.0.0.1 {C.DEEPSEEK_HOST}", cmd)


class TestShellReportsFailures(unittest.TestCase):
    def test_missing_binary_is_not_silent(self):
        code, out = C.sh(["заведомо-несуществующая-команда-719"])
        self.assertNotEqual(code, 0)
        self.assertTrue(out)


if __name__ == "__main__":
    unittest.main()


class TestThrottledBeforeDoesNotBreakProd(unittest.TestCase):
    """⚠⚠⚠ САМОЕ ДОРОГОЕ СВОЙСТВО ВСЕГО МОДУЛЯ: не ломать бой, когда измерить НЕЛЬЗЯ.

    Ревью PR #135, раунд 6 (#136). `main()` гоняет оба сценария подряд без паузы, и `before`
    второго приходит вплотную к опросу первого, когда в окне уже 16–20 хитов. Без распознавания
    429 инструмент объявлял «сервис нездоров ДО поломки» — ложный диагноз ПРО СЕРВИС, — и это
    ровно то отравление `before`, которое коммит раунда 5 объявил починенным, закрыв только
    опрос. Распознаватель был заведён и позван В ОДНОМ МЕСТЕ ИЗ ТРЁХ."""

    class _Spy(C.Chaos):
        name, human = "шпион", "шпион"

        def __init__(self):
            super().__init__(["docker"], "/tmp", "app", "qdrant")
            self.broke = False

        def break_it(self):
            self.broke = True
            return 0, "сломал"

        def restore(self):
            return 0, "починил"

        def watchdog(self):
            return ["true"]

    def _run(self, probe_result):
        sc = self._Spy()
        with mock.patch.object(C, "probe", return_value=probe_result):
            res = C.run_scenario(sc, "http://x", "jar", "вопрос", timeout=1.0, settle=0.0)
        return sc, res

    def test_429_before_does_not_touch_the_service(self):
        sc, res = self._run(probe(plain=L4.HTTP_429, stream=L4.HTTP_429, sources=0,
                                  text='{"detail":"Слишком много вопросов подряд."}'))
        self.assertEqual(res["verdict"], "НЕ ИЗМЕРЕНО")
        self.assertFalse(sc.broke, "инструмент СЛОМАЛ БОЙ, не сумев его измерить")

    def test_genuinely_sick_service_is_still_not_broken(self):
        """⚠ Соседняя половина: нездоровый сервис ломать тоже нельзя, но диагноз ДРУГОЙ."""
        sc, res = self._run(probe(sources=0, text="фолбэк"))
        self.assertEqual(res["verdict"], "НЕ ЗАПУЩЕН")
        self.assertFalse(sc.broke)

    def test_healthy_service_is_actually_broken(self):
        """⚠⚠ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ. Проверка «не сломал» зелена и у инструмента, который не
        ломает НИКОГДА, — то есть у полностью мёртвого. На здоровом сервисе поломка обязана
        произойти, иначе два предыдущих теста ничего не значат."""
        sc, _ = self._run(probe())
        self.assertTrue(sc.broke, "на здоровом сервисе поломка не сработала — сценарий мёртв")


class TestRecoveryTimeHasNoArtificialFloor(unittest.TestCase):
    """⚠⚠ ЧИСЛО, ЗАВЫШЕННОЕ САМИМ ИНСТРУМЕНТОМ, — НЕ ЗАМЕР (раунд 6, #136).

    `time.sleep(poll)` стоял В НАЧАЛЕ тела цикла, поэтому первая проба уходила не раньше
    `POLL_MIN_SECONDS`. Замеренное репетицией «Qdrant поднимает коллекции ~6 с» инструмент не мог
    показать В ПРИНЦИПЕ: любое восстановление быстрее паузы округлялось вверх до неё, и нигде это
    не оговаривалось. Пауза обязана стоять МЕЖДУ пробами, а не перед первой."""

    def test_instant_recovery_is_reported_as_instant(self):
        # Последовательность настоящего захода: здоров → сломан → снова здоров с первой пробы.
        sequence = [probe(),                                        # before
                    probe(text="Поиск недоступен", sources=0),      # during
                    probe()]                                        # after, сразу здоров
        sc = TestThrottledBeforeDoesNotBreakProd._Spy()
        with mock.patch.object(C, "probe", side_effect=sequence):
            res = C.run_scenario(sc, "http://x", "jar", "в", timeout=1.0, settle=0.0)
        self.assertEqual(res["verdict"], "ПРОЙДЕН")
        self.assertIsNotNone(res["recovery_seconds"])
        self.assertLess(
            res["recovery_seconds"], C.POLL_MIN_SECONDS,
            f"возврат {res['recovery_seconds']} с не меньше паузы опроса "
            f"{C.POLL_MIN_SECONDS} с — число задано инструментом, а не измерено")
