"""Предохранители инструмента уровня 4 (`EV22` #130, `scripts/eval_level4.py`).

⚠⚠ ЗАЧЕМ ТЕСТИРОВАТЬ ЗАМЕР. Инструмент, чьё «ноль отказов» является результатом, ломается тихо:
слепой и здоровый снаружи неразличимы. У этого конкретного замера цена ошибки высокая — числа
уровня 4 пойдут в базу сравнения и станут порогом на все следующие прогоны, а порогов до первого
прогона нет (так сказано в `EVAL_GUIDE`), то есть проверить их будет не с чем.

ЧТО ЗАКРЕПЛЕНО:
1. Константа лимита в скрипте СОВПАДАЕТ с настройкой приложения. Разойдись они — предполётный
   отказ пропустит прогон, который гарантированно упрётся в 429.
2. Предполётная проверка ОТКАЗЫВАЕТ, а не предупреждает.
3. Недействительные классы (401/403/429) ГАСЯТ перцентили, и гашение не «всегда включено» —
   на чистом наборе числа печатаются.
4. Положительный контроль самого инструмента проходит (мета-контроль: он не должен сгнить).
"""

from __future__ import annotations

import http.client
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import eval_level4 as L4  # noqa: E402
from app.core.config import settings  # noqa: E402


class TestRateLimitStaysInSync(unittest.TestCase):
    """⚠⚠ САМЫЙ ВАЖНЫЙ ТЕСТ ФАЙЛА. Предполётный отказ считает ёмкость как «аккаунты × лимит».
    Если настройку приложения поменяют, а константу в скрипте нет, отказ пропустит прогон,
    который упрётся в 429 — и прогон отчитается «чисто», ничего не измерив."""

    def test_constant_matches_settings(self):
        self.assertEqual(
            L4.RATE_LIMIT_PER_MIN, settings.RATE_LIMIT_CHAT_PER_MIN,
            "константа лимита в scripts/eval_level4.py разошлась с RATE_LIMIT_CHAT_PER_MIN — "
            "предполётная проверка стала считать ёмкость по устаревшему числу")


class TestPreflightRefuses(unittest.TestCase):
    """Отказ ДО замера, а не разбор после: иначе получится час работы и числа, которые нельзя
    использовать."""

    def test_refuses_when_accounts_insufficient(self):
        # 50 запросов при лимите 20/мин требуют трёх аккаунтов; передан один.
        with self.assertRaises(SystemExit) as cm:
            L4.preflight(n=50, concurrency=10, accounts=1)
        self.assertIn("429", str(cm.exception))

    def test_refuses_when_wave_exceeds_instant_capacity(self):
        with self.assertRaises(SystemExit):
            L4.preflight(n=10, concurrency=100, accounts=1)

    def test_allows_when_enough(self):
        """⚠ Обратная половина: отказ, срабатывающий всегда, тоже даёт «ноль ошибок»."""
        L4.preflight(n=50, concurrency=10, accounts=3)  # не бросает — этого и ждём


class TestInvalidatingClassesSuppressNumbers(unittest.TestCase):
    """⚠⚠ 429/403/401 — это «мы не измеряли сервис», а не «сервис справился». Ни один из них
    не 5xx, поэтому сводка «доля 5xx» назвала бы такой прогон чистым."""

    @staticmethod
    def _ok(n: int) -> list[dict]:
        return [{"class": L4.OK, "ttft": 1.0 + i, "total": 5.0 + i, "message_id": i}
                for i in range(n)]

    # ⚠⚠ КЛАССЫ ПЕРЕЧИСЛЕНЫ ЗДЕСЬ, А НЕ ВЗЯТЫ ИЗ `L4.INVALIDATING`. Первая редакция теста читала
    # список из самого модуля — и мутационный контроль показал, что при ОПУСТОШЁННОЙ константе
    # тест остаётся ЗЕЛЁНЫМ: цикл просто не выполняется ни разу. Оракул, выведенный из
    # проверяемого артефакта, проверяет сам себя.
    # ⚠⚠ `tool_error` ДОБАВЛЕН ОСОЗНАННО (ревью PR #135, раунд 2), и тест на состав это поймал —
    # ради чего он и закрепляет множество В ОБЕ СТОРОНЫ. Побег исключения из `ask` — это дефект
    # ИНСТРУМЕНТА, а не отказ сервиса: записанный как `conn_error`, он попадал в долю отказов и
    # гнал вердикт, то есть сломанный измеритель выглядел как «сервис уронил N % соединений».
    MUST_INVALIDATE = ("http_401", "http_403", "http_429", "tool_error")
    # ⚠⚠ ПОРОГИ У КЛАССОВ РАЗНЫЕ, И ЭТО ОСОЗНАННО (ревью PR #135, раунд 4). Один 401/403/429
    # отменяет замер сразу: они приходят БУРЕЙ (окно лимита общее, сессия общая), и один
    # наблюдённый означает, что задеты и соседи. Побег из `ask` — событие ОДНОГО запроса, поэтому
    # у него допуск в одно событие: иначе единственная опечатка обнуляла бы весь прогон,
    # оплаченный окном на боевой машине.
    INVALIDATE_AT_ONE = ("http_401", "http_403", "http_429")

    def test_each_invalidating_class_kills_percentiles(self):
        for bad in self.INVALIDATE_AT_ONE:
            with self.subTest(bad=bad):
                s = L4.summarize(self._ok(5) + [{"class": bad, "ttft": None, "total": 0.1}], {})
                self.assertFalse(s["valid"], f"{bad} не объявил замер недействительным")
                self.assertNotIn("ttft_p50", s, f"{bad}: перцентили всё равно посчитались")
                self.assertNotIn("total_p95", s)

    def test_invalidating_set_has_exactly_these(self):
        """⚠ Состав закреплён в обе стороны: и не сузился, и не разросся молча. Лишний класс
        здесь так же вреден — он объявил бы недействительным замер, который состоялся."""
        self.assertEqual(set(L4.INVALIDATING), set(self.MUST_INVALIDATE))

    def test_clean_run_does_report_numbers(self):
        """⚠ Отрицательный контроль к предыдущему: гашение не «всегда включено»."""
        s = L4.summarize(self._ok(5), {})
        self.assertTrue(s["valid"])
        self.assertIsNotNone(s["ttft_p50"])
        self.assertIsNotNone(s["total_p95"])
        self.assertEqual(s["failure_share"], 0.0)

    def test_engine_failure_inside_stream_counts_as_failure(self):
        """200 + {"type":"error"} — движок упал, HTTP-код при этом 200."""
        s = L4.summarize(self._ok(3) + [{"class": L4.SSE_ERROR, "ttft": None, "total": 2.0}], {})
        self.assertTrue(s["valid"], "sse_error не отменяет замер — сервис ответил, но плохо")
        self.assertEqual(s["failure_share"], 0.25)


class TestToolDefectIsNotAServiceFailure(unittest.TestCase):
    """⚠⚠ Сломанный ИЗМЕРИТЕЛЬ и упавший СЕРВИС — разные вещи, и путать их нельзя: первое
    означает «мы не измеряли», второе — «сервис не справился». Раньше побег исключения из `ask`
    записывался как `conn_error` и утекал в долю отказов."""

    @staticmethod
    def _ok(n):
        return [{"class": L4.OK, "ttft": 1.0, "total": 2.0, "message_id": i} for i in range(n)]

    def test_mass_tool_error_invalidates(self):
        """Массовый побег — измеритель сломан, числа не значат ничего."""
        s = L4.summarize(self._ok(5) + [{"class": L4.TOOL_ERROR, "ttft": None, "total": None}
                                        for _ in range(3)], {})
        self.assertFalse(s["valid"], "дефект инструмента не объявил замер недействительным")
        self.assertNotIn("ttft_p50", s, "перцентили посчитались при сломанном инструменте")

    def test_single_tool_error_does_not_discard_the_window(self):
        """⚠⚠ РАЗМЕРЫ БЕРУТСЯ ТЕ, ЧТО РЕАЛЬНО ГОНЯЕТ ДРАЙВЕР: 30 (одиночный) и 10 / 25 / 50
        (нагрузка). Первая редакция теста стояла на n=100 — популяции, которой драйвер НЕ
        ПРОИЗВОДИТ, — и была ЗЕЛЁНОЙ при неизменившемся поведении: порог «доля > 1 %» достижим
        только со ста запросов. Тест, написанный под удобное число, сертифицирует несуществующую
        починку."""
        for n in (10, 25, 30, 50):
            with self.subTest(n=n):
                s = L4.summarize(
                    self._ok(n - 1) + [{"class": L4.TOOL_ERROR, "ttft": None, "total": None}], {})
                self.assertTrue(s["valid"], f"n={n}: единичный дефект инструмента выбросил замер")
                self.assertIsNotNone(s["ttft_p50"], f"n={n}: числа не напечатаны")
                self.assertIn("⚠ дефект инструмента", s, f"n={n}: допуск применён МОЛЧА")

    def test_two_tool_errors_do_invalidate_small_run(self):
        """⚠ Обратная половина: допуск — в ОДНО событие, а не «сколько угодно»."""
        s = L4.summarize(self._ok(28) + [{"class": L4.TOOL_ERROR, "ttft": None, "total": None}
                                         for _ in range(2)], {})
        self.assertFalse(s["valid"], "два побега на 30 запросах не отменили замер")


class TestVerdictStopsOnFailure(unittest.TestCase):
    """⚠⚠ КОД ВОЗВРАТА — ЭТО И ЕСТЬ ПРЕДОХРАНИТЕЛЬ. Первая редакция возвращала 0 при 100 %
    провалов: сухой прогон 28.08.2026 против живого приложения с выключенным Qdrant дал два
    `sse_error`, сводка честно сказала «успешных 0», а код был 0. Предохранитель, который
    сообщает о провале и не останавливает, равен отсутствующему."""

    @staticmethod
    def _s(ok: int, failures: int, valid: bool = True) -> dict:
        n = ok + failures
        return {"valid": valid, "ok": ok, "n": n,
                "failure_share": round(failures / n, 4) if n else None,
                "⚠⚠ ЗАМЕР НЕДЕЙСТВИТЕЛЕН": "" if valid else "429"}

    def test_zero_successes_is_failure(self):
        self.assertEqual(L4.verdict(self._s(ok=0, failures=2), 0.01), 1)

    def test_invalid_run_is_failure(self):
        self.assertEqual(L4.verdict(self._s(ok=5, failures=1, valid=False), 1.0), 1)

    def test_failure_share_above_threshold_is_failure(self):
        self.assertEqual(L4.verdict(self._s(ok=90, failures=10), 0.01), 1)

    def test_clean_run_returns_zero(self):
        """⚠ Обратная половина: код, всегда возвращающий 1, тоже «ловит все провалы»."""
        self.assertEqual(L4.verdict(self._s(ok=100, failures=0), 0.01), 0)


class TestProtocolErrorsAreCounted(unittest.TestCase):
    """⚠⚠ `IncompleteRead` — ЭТО ОБОРВАННЫЙ НА СЕРЕДИНЕ ПОТОК, то есть ровно класс `truncated`,
    ради которого инструмент писался. И он НЕ является `OSError` (проверено: OSError=False,
    HTTPException=True). До правки исключение уходило из `ask()` наружу, убивало поток замера,
    ячейка результата оставалась пустой и ОТФИЛЬТРОВЫВАЛАСЬ — отказ исчезал из таблицы классов
    и одновременно уменьшал знаменатель доли отказов. Найдено ревью PR #135.

    ⚠ Проверяется ПОДМЕНОЙ соединения, а не фиктивным сервером: первая попытка сделать это
    сервером дала `RemoteDisconnected`, который наследует и `OSError` и потому обрабатывался
    ВСЕГДА. Контроль обязан бить в тот путь, который чинят, — иначе он зелен независимо от правки.
    """

    def _raising(self, exc):
        class _Conn:
            def request(self, *a, **k):
                raise exc
            def close(self):
                pass
        return lambda base, timeout: (_Conn(), "")

    def test_incomplete_read_becomes_truncated(self):
        exc = http.client.IncompleteRead(b"partial")
        with mock.patch.object(L4, "_conn", self._raising(exc)):
            r = L4.ask("http://127.0.0.1:1", "session=x", "q", 1.0)
        self.assertEqual(r["class"], L4.TRUNCATED)
        self.assertIn("IncompleteRead", r.get("detail", ""))

    def test_bad_status_line_becomes_truncated(self):
        with mock.patch.object(L4, "_conn", self._raising(http.client.BadStatusLine("мусор"))):
            r = L4.ask("http://127.0.0.1:1", "session=x", "q", 1.0)
        self.assertEqual(r["class"], L4.TRUNCATED)

    def test_remote_disconnected_stays_conn_error(self):
        """⚠ Обратная половина: `RemoteDisconnected` наследует И `OSError`, и его классификация
        правкой меняться НЕ должна — иначе «починка» тихо переписала бы уже работавший случай."""
        with mock.patch.object(L4, "_conn", self._raising(http.client.RemoteDisconnected("нет"))):
            r = L4.ask("http://127.0.0.1:1", "session=x", "q", 1.0)
        self.assertEqual(r["class"], L4.CONN_ERROR)

    def test_plain_probe_survives_protocol_error(self):
        with mock.patch.object(L4, "_conn", self._raising(http.client.IncompleteRead(b""))):
            r = L4.ask_plain("http://127.0.0.1:1", "session=x", "q", 1.0)
        self.assertEqual(r["class"], L4.CONN_ERROR)


class TestOutcomeClassification(unittest.TestCase):
    def test_status_map(self):
        for status, expect in ((401, L4.HTTP_401), (403, L4.HTTP_403), (429, L4.HTTP_429),
                               (422, L4.HTTP_422), (500, L4.HTTP_5XX), (502, L4.HTTP_5XX),
                               (404, L4.HTTP_4XX)):
            self.assertEqual(L4._class_for_status(status), expect, f"код {status}")

    def test_percentile_is_nearest_rank(self):
        v = [float(i) for i in range(1, 11)]  # 1..10
        self.assertEqual(L4.pct(v, 0.50), 5.0)
        self.assertEqual(L4.pct(v, 0.95), 10.0)
        self.assertIsNone(L4.pct([], 0.5))


class TestToolSelfControlPasses(unittest.TestCase):
    """⚠⚠ МЕТА-КОНТРОЛЬ. Положительный контроль инструмента обязан проходить — иначе числа
    замера не интерпретируются. Держим в батарее, чтобы он не сгнил незамеченным.

    Офлайн: поднимается локальный сервер на 127.0.0.1, сети наружу не требуется."""

    def test_selftest_returns_zero(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = L4.selftest()
        self.assertEqual(code, 0, f"положительный контроль инструмента провален:\n{buf.getvalue()}")


if __name__ == "__main__":
    unittest.main()
