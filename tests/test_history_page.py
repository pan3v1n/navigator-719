"""Б3: страница «История запросов» (макет «Navigator 719 Service», `test32`).

Что закрепляем:
* группы — по календарным дням МСК: «Сегодня», «Вчера», «На этой неделе» (до 6 дней), «Ранее»;
* фильтр периода — как в макете: «Неделя» = сегодня + вчера + неделя;
* поиск — по вопросам И по ответам (слово только в ответе находит беседу), без учёта регистра;
* у строки — первый вопрос, начало первого ответа без разметки и номеров, источники, на которые
  ответ СОСЛАЛСЯ (не всё окно поиска), время;
* чужие беседы не видны;
* фронт: в сайдбаре последние беседы + «Посмотреть всё», страница уходит при любом переходе.
"""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api import history as hist  # noqa: E402
from app.core import plans  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import Base  # noqa: E402

WEB = ROOT / "app" / "web"
MSK = plans.MSK_OFFSET
# «Сейчас» для тестов: 08.10.2026 15:00 МСК (наивное UTC — так время лежит в БД)
NOW_UTC = datetime(2026, 10, 8, 15, 0) - MSK


def msk(y, m, d, hh=12, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm) - MSK


@dataclass
class M:
    session_id: str
    role: str
    content: str
    ts: datetime
    sources_json: str | None = None


PRODUCT_SRC = {"section": "Продукция отрасли специального машиностроения",
               "product_name": "Бульдозеры на гусеничных тракторах", "okpd2": ["28.92.21.110"],
               "source_anchor": "Приложение к ПП №719, Раздел III, позиция 2", "kind": "product"}
OTHER_SRC = {"section": "Раздел VI", "product_name": "Оборудование для мойки бутылок", "okpd2": ["28.93.15"],
             "source_anchor": "Приложение к ПП №719, Раздел VI, позиция 40", "kind": "product"}
RULE_SRC = {"section": "Правила ведения реестра", "product_name": "Правила ведения реестра, п. 7",
            "okpd2": [], "url": "https://example.test/", "kind": "rule"}


def conv(sid, day_ts, question, answer, sources=None):
    return [M(sid, "user", question, day_ts),
            M(sid, "assistant", answer, day_ts + timedelta(seconds=30),
              json.dumps(sources or [], ensure_ascii=False))]


def run(msgs, **kw):
    return hist.build_history(sorted(msgs, key=lambda m: m.ts), now=NOW_UTC, **kw)


class TestGroups(unittest.TestCase):
    def test_calendar_days_in_moscow(self):
        today = plans.msk_today(NOW_UTC)
        self.assertEqual(hist.group_index(today, today), 0)
        self.assertEqual(hist.group_index(today - timedelta(days=1), today), 1)
        self.assertEqual(hist.group_index(today - timedelta(days=6), today), 2)
        self.assertEqual(hist.group_index(today - timedelta(days=7), today), 3)

    def test_midnight_moscow_is_the_border(self):
        """00:30 МСК — это уже СЕГОДНЯ, хотя по UTC ещё вчера: группа считается по Москве."""
        msgs = conv("a", msk(2026, 10, 8, 0, 30), "ночной вопрос", "ответ")
        g = run(msgs)["groups"]
        self.assertEqual((g[0]["label"], g[0]["items"][0]["time"]), ("Сегодня", "00:30"))

    def test_groups_order_and_time_format(self):
        msgs = (conv("t", msk(2026, 10, 8, 14, 32), "сегодня", "о")
                + conv("y", msk(2026, 10, 7, 17, 48), "вчера", "о")
                + conv("w", msk(2026, 10, 3), "на неделе", "о")
                + conv("e", msk(2026, 9, 18), "давно", "о")
                + conv("old", msk(2025, 12, 1), "в прошлом году", "о"))
        g = run(msgs)["groups"]
        self.assertEqual([x["label"] for x in g], ["Сегодня", "Вчера", "На этой неделе", "Ранее"])
        self.assertEqual(g[0]["items"][0]["time"], "14:32")
        self.assertEqual(g[2]["items"][0]["time"], "03.10")
        self.assertEqual([i["time"] for i in g[3]["items"]], ["18.09", "01.12.25"])

    def test_period_filter_matches_the_mockup(self):
        msgs = (conv("t", msk(2026, 10, 8), "a", "о") + conv("y", msk(2026, 10, 7), "b", "о")
                + conv("w", msk(2026, 10, 4), "c", "о") + conv("e", msk(2026, 9, 1), "d", "о"))
        labels = lambda p: [x["label"] for x in run(msgs, period=p)["groups"]]  # noqa: E731
        self.assertEqual(labels("today"), ["Сегодня"])
        self.assertEqual(labels("week"), ["Сегодня", "Вчера", "На этой неделе"])
        self.assertEqual(labels("earlier"), ["Ранее"])
        self.assertEqual(len(labels("all")), 4)
        self.assertEqual(len(labels("неизвестный")), 4, "чужое значение периода — как «все»")

    def test_last_activity_decides_the_group(self):
        """Беседа, начатая неделю назад и продолженная сегодня, — в «Сегодня»."""
        msgs = conv("x", msk(2026, 9, 30), "начало", "ответ") + [M("x", "user", "продолжение", msk(2026, 10, 8, 9))]
        g = run(msgs)["groups"]
        self.assertEqual(g[0]["label"], "Сегодня")
        self.assertEqual(g[0]["items"][0]["q"], "начало", "строка — первый вопрос беседы")


class TestSearch(unittest.TestCase):
    MSGS = (conv("a", msk(2026, 10, 8), "Требования к бульдозерам", "Нужна сварка рамы [1]", [PRODUCT_SRC])
            + conv("b", msk(2026, 10, 8, 13), "Кто выдаёт акт экспертизы", "ТПП РФ [1]", [RULE_SRC]))

    def test_finds_by_answer_text(self):
        """Слово есть только в ОТВЕТЕ — беседа находится: ради этого поиск и ушёл на сервер."""
        items = [i for g in run(self.MSGS, q="СВАРКА")["groups"] for i in g["items"]]
        self.assertEqual([i["session_id"] for i in items], ["a"])

    def test_finds_by_question_and_reports_empty(self):
        self.assertEqual(run(self.MSGS, q="экспертизы")["groups"][0]["items"][0]["session_id"], "b")
        self.assertEqual(run(self.MSGS, q="мочеприемники"), {"groups": []})


class TestRowContent(unittest.TestCase):
    def test_refs_are_cited_sources_only_with_short_labels(self):
        ans = "Позиция — бульдозеры [1]; пункт Правил [3]. Повтор [1]."
        msgs = conv("a", msk(2026, 10, 8), "вопрос", ans, [PRODUCT_SRC, OTHER_SRC, RULE_SRC])
        item = run(msgs)["groups"][0]["items"][0]
        self.assertEqual(item["refs"], ["Разд. III, поз. 2", "Правила ведения реестра, п. 7"],
                         "в чипах — только процитированные источники, без повторов и без чужой позиции")

    def test_excerpt_drops_markup_tables_and_refs(self):
        ans = ("**Позиция:** «Специальное машиностроение», бульдозеры [1].\n\n"
               "| Операция | Баллы |\n|---|---|\n| сварка | 4 |\n- порог не приведён [1]")
        msgs = conv("a", msk(2026, 10, 8), "вопрос", ans, [PRODUCT_SRC])
        a = run(msgs)["groups"][0]["items"][0]["a"]
        self.assertEqual(a, "Позиция: «Специальное машиностроение», бульдозеры. порог не приведён")
        self.assertNotIn("|", a)

    def test_long_excerpt_is_cut(self):
        msgs = conv("a", msk(2026, 10, 8), "вопрос", "слово " * 200)
        a = run(msgs)["groups"][0]["items"][0]["a"]
        self.assertLessEqual(len(a), hist.EXCERPT_LEN)
        self.assertTrue(a.endswith("…"))

    def test_broken_sources_json_does_not_break_the_page(self):
        msgs = [M("a", "user", "вопрос", msk(2026, 10, 8)),
                M("a", "assistant", "ответ [1]", msk(2026, 10, 8, 12, 1), "{не json")]
        self.assertEqual(run(msgs)["groups"][0]["items"][0]["refs"], [])


class TestEndpointIsolation(unittest.TestCase):
    def test_only_own_conversations(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine)
        S = sessionmaker(bind=engine, expire_on_commit=False)
        with S() as db:
            a = q.create_user(db, "a", "h", role="user")
            b = q.create_user(db, "b", "h", role="user")
            q.log_message(db, user_id=a.id, session_id="sa", role="user", content="мой вопрос")
            q.log_message(db, user_id=b.id, session_id="sb", role="user", content="чужой вопрос")
            a = q.get_user(db, a.id)
            db.expunge(a)
        with mock.patch.object(hist, "get_session", S):
            out = hist.history(q="вопрос", user=a)
        sids = [i["session_id"] for g in out["groups"] for i in g["items"]]
        self.assertEqual(sids, ["sa"])


class TestFront(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "templates" / "chat.html").read_text(encoding="utf-8")
        cls.js = (WEB / "static" / "chat.js").read_text(encoding="utf-8")
        cls.css = (WEB / "static" / "style.css").read_text(encoding="utf-8")

    def test_sidebar_has_see_all_and_no_title_filter(self):
        self.assertIn('id="history-all"', self.html)
        self.assertIn("Посмотреть всё", self.html)
        self.assertNotIn('id="search-btn"', self.html, "старый фильтр сайдбара остался рядом со страницей")
        self.assertIn("const HISTORY_SIDE_N = 5", self.js)
        self.assertIn(".history-item.extra { display: none; }", self.css)

    def test_page_markup_matches_mockup(self):
        for node in ('id="history-page"', 'id="hp-query"', 'id="hp-list"', 'id="hp-empty"',
                     'data-period="today"', 'data-period="week"', 'data-period="earlier"'):
            self.assertIn(node, self.html)
        self.assertIn("Поиск по вопросам и ответам", self.html)

    def test_page_is_left_on_every_navigation(self):
        """Страница истории не должна висеть поверх открытой беседы или нового вопроса."""
        for fn in ("async function openConversation", "async function ask(text)", "function goHome()"):
            body = self.js.split(fn)[1][:400]
            # вызов кодом, а не упоминание в комментарии («// leaveHistory();» не засчитывается)
            self.assertRegex(body, r"\n\s+leaveHistory\(\);", fn)

    def test_active_old_conversation_stays_visible_in_sidebar(self):
        trim = self.js.split("function trimHistory")[1].split("\n}")[0]
        self.assertIn('!it.classList.contains("active")', trim)

    def test_stale_search_response_does_not_overwrite(self):
        body = self.js.split("async function loadHistory")[1].split("\n}")[0]
        self.assertIn("seq !== hpSeq", body)


if __name__ == "__main__":
    unittest.main()
