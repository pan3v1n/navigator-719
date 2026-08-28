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

import sys
import unittest
from pathlib import Path

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
    MUST_INVALIDATE = ("http_401", "http_403", "http_429")

    def test_each_invalidating_class_kills_percentiles(self):
        for bad in self.MUST_INVALIDATE:
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
