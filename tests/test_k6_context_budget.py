r"""Хвост кандидатов свёрнут в один блок — `K6` (#43).

ЗАМЕР, ОПРЕДЕЛИВШИЙ ПРАВКУ (21.08.2026, 52 запроса: 10 детерминизма + 42 in-scope golden):
контекст в среднем 6 890 символов, из них блоки НЕцелевых кандидатов — 3 454, то есть **ровно
половина**; медиана хвоста (3 515) БОЛЬШЕ медианы целевого блока (1 965). Целевая позиция, ради
которой пришёл пользователь, занимала меньше места, чем семь позиций, о которых не спрашивали.

Причина: после `EV7` у кандидата не осталось содержания — только имя, код, якорь и ЗАПРЕТ. Запрет
и был самой длинной частью блока, и он повторялся семь раз ДОСЛОВНО. Повтор ничего не усиливает:
правило, сказанное один раз над списком, действует на весь список.

После правки: среднее 5 034 (−27 %), медиана 5 583 → 3 633 (−35 %), хвост 3 454 → 1 609 (−53 %),
доля хвоста 50 % → 31 %. Целевые блоки не тронуты (3 436 → 3 425, шум).

⚠ ЧТО ЛЕГКО ПОТЕРЯТЬ ПРИ СВЁРТКЕ — и потому проверяется здесь:
  * номер `[N]` (по нему ответ ссылается на источник, метрика §2.2 «все ссылки существуют»);
  * пометку «совпадение по ГРУППЕ кода ОКПД2» — пользователь назвал код, подходящий кандидату;
    первая редакция свёртки её потеряла, поймано тестом в `test_rag`;
  * различение «требования у неё в базе ЕСТЬ» / «есть ОБЩИЕ требования её группы»;
  * сам запрет — он обязан остаться, но ровно один раз.

⚠ И ГЛАВНОЕ: формулировка «Требования … НЕ ПОКАЗАНЫ» процитирована в правиле 3в промпта. Данные и
правило обязаны меняться одним коммитом — на их расхождении проект уже горел. Инвариант закреплён
тестом `TestPromptQuotesContext`.

Запуск:  .venv\Scripts\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import prompts  # noqa: E402
from app.rag.pipeline import format_context  # noqa: E402
from tests.test_rag import make_hit  # noqa: E402

TAIL_HEAD = "ПРОЧИЕ КАНДИДАТЫ ОКНА. Требования этих позиций НЕ ПОКАЗАНЫ"
BAN = "НЕ приводи по ним баллов, порогов и процентов"


def _ctx(n_candidates: int = 6, code: str | None = None) -> str:
    target = make_hit(product_name="Целевая", okpd2_codes=["28.15.10"], okpd2_match=True,
                      min_threshold="не менее 300 баллов")
    others = [make_hit(product_name=f"Кандидат {k}", okpd2_codes=[f"29.10.{k}"],
                       requirement_blocks=[{"operations": [{"text": "литьё", "points": 7}]}])
              for k in range(1, n_candidates + 1)]
    return format_context([target, *others], None, code or "28.15.10")


class TestTailIsCollapsed(unittest.TestCase):
    def test_single_tail_block(self):
        ctx = _ctx(6)
        self.assertEqual(ctx.count(TAIL_HEAD), 1, "хвост кандидатов снова разложен на блоки")

    def test_ban_printed_once_regardless_of_candidate_count(self):
        """Запрет — самая длинная часть; семь его копий и съедали половину контекста."""
        for n in (1, 3, 7):
            with self.subTest(candidates=n):
                self.assertEqual(_ctx(n).count(BAN), 1)

    def test_every_candidate_keeps_its_number_and_identity(self):
        ctx = _ctx(4)
        tail = ctx.split(TAIL_HEAD, 1)[1]
        for k in range(1, 5):
            with self.subTest(candidate=k):
                self.assertIn(f"[{k + 1}] Кандидат {k}", tail)
                self.assertIn(f"29.10.{k}", tail)

    def test_target_block_comes_before_the_tail(self):
        """Приоритет целевого хита — часть правки: он в начале контекста при любом порядке выдачи."""
        ctx = _ctx(5)
        self.assertLess(ctx.index("Целевая"), ctx.index(TAIL_HEAD))
        self.assertIn("не менее 300 баллов", ctx.split(TAIL_HEAD, 1)[0])

    def test_candidate_numbers_have_no_requirement_numbers(self):
        """`EV7` не отменяется свёрткой: баллов кандидатов в контексте по-прежнему нет."""
        self.assertNotIn("7 балл", _ctx(3))

    def test_group_code_match_marker_survives(self):
        """Пользователь назвал код ГРУППЫ — кандидат обязан нести эту пометку."""
        a = make_hit(product_name="Первая", okpd2_codes=["26.11.22.210"], okpd2_match=True,
                     min_threshold="не менее 300 баллов")
        b = make_hit(product_name="Вторая", okpd2_codes=["26.11.22.210"], okpd2_match=True,
                     min_threshold="не менее 400 баллов")
        grp = format_context([a, b], None, "26.11.22")
        self.assertIn("совпадение по ГРУППЕ кода ОКПД2", grp)
        self.assertNotIn("не менее 400 баллов", grp, "требования кандидата вернулись в контекст")

    def test_requirements_presence_is_still_distinguished(self):
        """«ЕСТЬ в базе» и «есть требования ГРУППЫ» — разные подсказки, обе нужны модели."""
        target = make_hit(product_name="Целевая", okpd2_codes=["28.15.10"], okpd2_match=True)
        rich = make_hit(product_name="Богатый", okpd2_codes=["29.10.1"],
                        requirement_blocks=[{"operations": [{"text": "литьё", "points": 7}]}])
        empty = make_hit(product_name="Пустой", okpd2_codes=["29.10.2"], requirement_blocks=[])
        ctx = format_context([target, rich, empty], None, "28.15.10")
        self.assertIn("требования у неё в базе ЕСТЬ", ctx)
        line = [ln for ln in ctx.splitlines() if "Пустой" in ln][0]
        self.assertNotIn("в базе ЕСТЬ", line, "у пустого кандидата появилось несуществующее наличие")

    def test_ask_for_code_only_when_code_unknown(self):
        """Правило 1ж: у эксперта, назвавшего код, не переспрашивают его снова."""
        self.assertNotIn("попроси его код", _ctx(3, code="28.15.10"))
        target = make_hit(product_name="Целевая", okpd2_codes=["28.15.10"])
        other = make_hit(product_name="Кандидат", okpd2_codes=["29.10.1"])
        self.assertIn("попроси его код", format_context([target, other], None, None))


class TestPromptQuotesContext(unittest.TestCase):
    """Правило промпта цитирует строку контекста — цитата обязана совпадать дословно.

    Проект уже горел на том, что правило и данные менялись в разных коммитах и расходились
    (запрет слова не держался, пока слово жило в самом промпте). Здесь инвариант проверяется
    автоматически: изменишь заголовок хвоста — тест укажет на правило 3в.
    """

    def test_rule_quotes_the_actual_tail_heading(self):
        rule = prompts.NAVIGATOR_SYSTEM if hasattr(prompts, "NAVIGATOR_SYSTEM") else ""
        if not rule:
            rule = "\n".join(str(v) for v in vars(prompts).values() if isinstance(v, str))
        self.assertIn("ПРОЧИЕ КАНДИДАТЫ ОКНА", rule,
                      "правило 3в цитирует заголовок, которого в контексте больше нет")
        self.assertIn("Требования этих позиций НЕ ПОКАЗАНЫ", rule)
        self.assertIn(TAIL_HEAD, _ctx(2), "контекст перестал печатать цитируемую правилом строку")


if __name__ == "__main__":
    unittest.main()
