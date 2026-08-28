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
        self.assertEqual(parsed[:2], ["sleep", "300;"] if parsed[1] == "300;" else parsed[:2])
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
