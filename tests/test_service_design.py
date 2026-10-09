"""Сервис по макету «Navigator 719 Service» (08.10.2026): панель источника (Б1), действия под
ответом (Б2), расход тарифа (Б4), вход и профиль.

Что закрепляем:
* у источника ответа есть текст для панели — у позиции приложения (те же данные, что видит модель:
  порог, требования группы, операции с баллами) и у пункта документа (целиком, с вводной фразой);
* текст для ЧЕЛОВЕКА не несёт указаний модели (фрагменты контекста с императивами в панель не идут);
* `/api/quota` и карточка тарифа: у пользователя без тарифа — ничего, у тарифного — остаток и дата;
* переоткрытая беседа отдаёт id ответов — оценка и «Сообщить об ошибке» доступны и там;
* пример ответа на публичной странице входа совпадает с пунктом, на который ссылается;
* фронт: источники и ссылки [N] навешиваются во всех трёх путях вывода ответа.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api import chat as chat_mod  # noqa: E402
from app.api import quota  # noqa: E402
from app.core import plans  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.db.models import AnswerUsage, Base  # noqa: E402
from app.rag import pipeline  # noqa: E402
from app.rag.retriever import Hit  # noqa: E402

WEB = ROOT / "app" / "web"
KB = ROOT / "knowledge_base" / "pp719"


def _records() -> list[dict]:
    recs: list[dict] = []
    for f in sorted((KB / "structured").glob("*.json")):
        recs += json.loads(f.read_text(encoding="utf-8"))
    return recs


def _hit(r: dict) -> Hit:
    return Hit(0.0, r["section_roman"], r.get("section_title", ""), r["product_name"],
               r.get("okpd2_codes") or [], r.get("min_threshold"), r.get("requirement_blocks") or [],
               r.get("source_anchor"), False, r)


def _find(name: str) -> dict:
    for r in _records():
        if r["product_name"].startswith(name):
            return r
    raise AssertionError(f"в корпусе нет позиции «{name}»")


class TestSourceText(unittest.TestCase):
    """Б1: текст позиции для панели «Источник» — на настоящих записях корпуса."""

    def test_points_listed_with_their_scores(self):
        t = pipeline.source_text(_hit(_find("Бульдозеры")))
        self.assertIn("• использование российского металлопроката для производства несущей рамы — 4 балл.", t)
        self.assertIn("▸ несущая рама", t, "вводная блока потерялась — операции висят без своего узла")

    def test_threshold_from_notes_is_shown(self):
        """«Мочеприемники»: порог живёт в примечании 81 — панель обязана его показать, как контекст."""
        t = pipeline.source_text(_hit(_find("Мочеприемники")))
        self.assertTrue(t.startswith("Порог:"), t[:200])
        self.assertIn("135 баллов", t)
        self.assertIn("[прим. 81]", t)

    def test_group_requirements_are_attributed(self):
        """Своих требований нет — показываем требования группы и называем позицию, у которой они стоят."""
        t = pipeline.source_text(_hit(_find("Гидротермокостюмы")))
        self.assertIn("Требования группы — приведены у позиции «Жилеты спасательные»", t)

    def test_no_instructions_for_the_model_leak_into_the_panel(self):
        """Панель читает эксперт: императивы из контекста модели («НЕ выдавай», «обязательно укажи»)
        сюда попадать не должны — ни из атрибуции группы, ни из пометок о неполноте."""
        bad = ("НЕ выдавай", "обязательно укажи", "предупреди об этом эксперта", "ТРЕБОВАНИЯ ГРУППЫ (")
        for r in _records():
            t = pipeline.source_text(_hit(r))
            for b in bad:
                self.assertNotIn(b, t, f"{r['product_name'][:60]}: в панель уехало указание модели")

    def test_text_is_capped(self):
        long = max(_records(), key=lambda r: len(pipeline.source_text(_hit(r), cap=10**9)))
        t = pipeline.source_text(_hit(long))
        self.assertLessEqual(len(t), pipeline.SOURCE_TEXT_CAP + 60)
        self.assertIn("полный перечень — в первоисточнике", t)


class TestPanelMatchesModelContext(unittest.TestCase):
    """Два рендера одних данных — панель эксперту (`source_text`) и блок модели (`format_context`) —
    обязаны совпадать по тому, что меняет вердикт: порог, закупочный порог, позиция группы.
    По ВСЕМ записям корпуса: разъедутся — эксперт сверит ответ не с тем, что видела модель."""

    @staticmethod
    def _grab(text: str, prefix: str):
        for line in text.splitlines():
            if line.strip().startswith(prefix):
                return line.strip()[len(prefix):].strip().lower()
        return None

    def test_every_record(self):
        bad = []
        for r in _records():
            h = _hit(r)
            h.okpd2_match = True  # позиция — целевая: только у целевой контекст печатает требования
            ctx = pipeline.format_context([h], code=(r.get("okpd2_codes") or [None])[0])
            st = pipeline.source_text(h)
            mt = self._grab(ctx, "Порог:")
            if mt and mt.startswith("не предусмотрен"):
                mt = None  # «порога нет» контекст говорит модели; панели нечего добавить
            pairs = ((mt, self._grab(st, "Порог:")),
                     (self._grab(ctx, "Порог ДЛЯ ЦЕЛЕЙ ЗАКУПОК (не для подтверждения происхождения):"),
                      self._grab(st, "Порог для целей закупок (не для подтверждения происхождения):")))
            if any(a != b for a, b in pairs):
                bad.append(r["product_name"][:60])
            if "ТРЕБОВАНИЯ ГРУППЫ" in ctx:
                parent = ctx.split("приведены у позиции «", 1)[1].split("»", 1)[0]
                if f"приведены у позиции «{parent}»" not in st:
                    bad.append("группа: " + r["product_name"][:60])
        self.assertEqual(bad, [], f"панель разошлась с контекстом модели у {len(bad)} записей")


class TestSourceItems(unittest.TestCase):
    def test_product_sources_carry_text(self):
        src = chat_mod._sources_from_hits([_hit(_find("Бульдозеры"))])[0]
        self.assertEqual(src.kind, "product")
        self.assertIn("балл.", src.text or "")

    def test_rule_sources_carry_full_text_with_intro(self):
        long_text = "Пункт. " + "слово " * 400   # длиннее RULES_TEXT_CAP контекста модели
        rule = {"source_anchor": "Правила ведения реестра, п. 6", "doc_type": "rules_registry",
                "text": long_text, "parent_intro": "В заявке указываются:"}
        src = chat_mod._sources_from_rules([rule])[0]
        self.assertEqual(src.kind, "rule")
        self.assertTrue(src.text.startswith("В заявке указываются:\n"), "вводная родителя потерялась")
        self.assertGreater(len(src.text), pipeline.RULES_TEXT_CAP, "панель показывает пункт целиком")

    def test_old_answers_without_text_stay_valid(self):
        """Ответы, записанные до правки, лежат в `sources_json` без поля — схема обязана их принять."""
        old = {"section": "Раздел II", "product_name": "Бульдозеры", "okpd2": ["28.92.21"],
               "source_anchor": None, "okpd2_match": False, "url": None}
        s = chat_mod.SourceItem(**old)
        self.assertIsNone(s.text)
        self.assertEqual(s.kind, "product")


class TestReviewFixes(unittest.TestCase):
    """Находки ревью 08.10.2026 — каждая закреплена, чтобы не вернулась."""

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "static" / "chat.js").read_text(encoding="utf-8")

    def test_source_text_failure_does_not_break_the_answer(self):
        """Текст панели собирается ПОСЛЕ оплаченной генерации — его сбой не должен ронять ответ."""
        with mock.patch.object(chat_mod, "source_text", side_effect=RuntimeError("сбой")):
            src = chat_mod._sources_from_hits([_hit(_find("Бульдозеры"))])[0]
        self.assertIsNone(src.text)
        self.assertEqual(src.product_name[:10], "Бульдозеры")

    def test_reopened_answer_has_no_rating(self):
        """Повторная оценка старого ответа пишется новой строкой и задваивает метрику приёмки."""
        hist = self.js.split("function addAssistant")[1].split("\nfunction ")[0]
        self.assertIn("addFeedbackBar(wrap, messageId, sessionId, false)", hist)
        bar = self.js.split("function addFeedbackBar")[1].split("\nfunction ")[0]
        self.assertIn("if (withRating)", bar)

    def test_uncited_answer_lists_no_answer_sources(self):
        """Отказ или ответ без [N] — «Источников ответа» нет, окно поиска только за кнопкой."""
        body = self.js.split("function addSources")[1].split("\nfunction ")[0]
        self.assertIn("if (cited.size === 0) head.remove()", body)
        self.assertIn("(cited.has(i) ? chips : rest)", body)

    def test_refs_not_linked_inside_links_or_code(self):
        body = self.js.split("function linkRefs")[1].split("\nfunction ")[0]
        self.assertIn('closest("a, code, pre, button")', body)

    def test_disclaimer_names_the_expert_verdict(self):
        """Принцип 1 (CLAUDE.md): постоянная строка под полем ввода — вердикт за экспертом ТПП."""
        html = (WEB / "templates" / "chat.html").read_text(encoding="utf-8")
        tail = html[html.index('class="composer-frame"'):]
        self.assertIn("окончательное решение принимает уполномоченный", tail)


class _DB(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        p = mock.patch.object(chat_mod, "get_session", self.Session)
        p.start()
        self.addCleanup(p.stop)

    def _user(self, name="u1", plan=None):
        with self.Session() as db:
            u = q.create_user(db, name, "h", role="user")
            if plan:
                q.set_user_plan(db, u.id, plan, plans.anchor_from_date(plans.msk_today()))
            u = q.get_user(db, u.id)
            db.expunge(u)
            return u


class TestQuotaEndpoint(_DB):
    def test_no_plan_no_card(self):
        self.assertEqual(chat_mod.get_quota(user=self._user()), {"quota": None})

    def test_plan_user_gets_remaining_and_renew_date(self):
        u = self._user(plan="Старт")
        with self.Session() as db:
            ts = plans.anchor_from_date(plans.msk_today()) + timedelta(minutes=1)
            db.add_all([AnswerUsage(user_id=u.id, ts=ts) for _ in range(20)])
            db.commit()
        qv = chat_mod.get_quota(user=u)["quota"]
        self.assertEqual((qv["plan"], qv["limit"], qv["used"], qv["remaining"]), ("Старт", 100, 20, 80))
        with self.Session() as db:
            st = quota.quota_state(db, u)
        self.assertEqual(qv["renews"], f"{plans.msk_date(st.end):%d.%m.%Y}",
                         "дата в карточке — начало нового периода по Москве")


class TestConversationGivesAnswerIds(_DB):
    def test_assistant_messages_carry_id(self):
        u = self._user()
        with self.Session() as db:
            q.log_message(db, user_id=u.id, session_id="s1", role="user", content="вопрос")
            a = q.log_message(db, user_id=u.id, session_id="s1", role="assistant", content="ответ")
        msgs = chat_mod.get_conversation("s1", user=u)["messages"]
        self.assertNotIn("message_id", msgs[0], "у вопроса id ответа быть не должно")
        self.assertEqual(msgs[1]["message_id"], a.id)


class TestProfileErrorKeepsContext(_DB):
    def test_error_rerender_keeps_exit_and_plan(self):
        """Ревью: ре-рендер ошибки POST /profile терял «Вернуться к сервису» и карточку тарифа.
        С кабинетом по макету (09.10.2026) тариф — на своей вкладке: ре-рендер обязан сохранить
        выход и переключатель вкладок."""
        from app.api import web
        u = self._user("p1", plan="Старт")
        with self.Session() as db:
            q.update_profile(db, u.id, full_name="Иван Петров", region="Курская область",
                             telegram="@ivan", consent=True)
            u = q.get_user(db, u.id)
            db.expunge(u)
        with mock.patch.object(web, "get_session", self.Session), \
             mock.patch.object(web, "current_user", lambda request: u):
            resp = web.profile_submit(_Req(), consent="", full_name="", region="", telegram="",
                                      position="", email="", phone="", org="", inn="")
        body = resp.body.decode("utf-8")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Вернуться к сервису", body)
        self.assertIn('href="/profile?tab=plan"', body)


class _Req:  # Jinja-шаблону от запроса нужен только объект в контексте
    scope = {"type": "http"}
    session: dict = {}


class TestTemplates(unittest.TestCase):
    def _render(self, name, **kw):
        from app.api.web import _ctx, templates
        return templates.get_template(name).render(**_ctx(_Req(), **kw))

    def test_chat_page_without_plan_has_null_quota(self):
        from app.db.models import User
        html = self._render("chat.html", user=User(username="kursk.expert1", role="expert"),
                            kontur_719_url="#", input_hint="")
        self.assertIn("window.QUOTA = null", html)
        self.assertIn('id="src-panel"', html)
        self.assertIn('id="quota-card"', html)

    def test_chat_page_with_plan_embeds_quota(self):
        from app.db.models import User
        qv = {"plan": "Старт", "limit": 100, "used": 3, "remaining": 97, "renews": "08.11.2026"}
        html = self._render("chat.html", user=User(username="kursk.user1", role="user"),
                            kontur_719_url="#", input_hint="", quota=qv)
        self.assertIn('"remaining": 97', html)

    def test_landing_links_only_with_public_domain(self):
        from app.api import web
        with mock.patch.object(web.settings, "PUBLIC_DOMAIN", ""):
            self.assertNotIn("#form", self._render("login.html", error=None))
        with mock.patch.object(web.settings, "PUBLIC_DOMAIN", "xn--719--83dani8b8bqyy.xn--p1ai"):
            self.assertIn('href="https://xn--719--83dani8b8bqyy.xn--p1ai/#form"', self._render("login.html", error=None))

    def test_profile_keeps_explicit_consent_and_shows_plan(self):
        user = {"username": "u", "full_name": "Иван Петров", "region": "Курская область", "role": "user"}
        form = {"full_name": "Иван Петров", "region": "Курская область", "consent": False}
        qv = {"plan": "Старт", "limit": 100, "used": 25, "remaining": 75, "renews": "08.11.2026"}
        kw = dict(user=user, form=form, error=None, regions=["Курская область"], quota=qv, can_leave=True)
        html = self._render("profile.html", tab="profile", **kw)
        self.assertIn('name="consent"', html, "явное согласие на ПДн пропало")
        self.assertIn("required", html.split('name="consent"')[1][:80])
        self.assertIn("Вернуться к сервису", html)
        # кабинет по макету (09.10.2026): расход — на вкладке «Тариф»
        self.assertIn("Использовано 25 из 100 запросов", self._render("profile.html", tab="plan", **kw))

    def test_login_demo_answer_matches_the_cited_point(self):
        """Публичная страница: пример ответа со ссылкой «п. 7 Правил» обязан совпадать с п. 7.
        В макете стояло «…или уполномоченная ею территориальная палата» — в пункте этих слов нет."""
        html = self._render("login.html", error=None)
        self.assertIn("п. 7 Правил", html)
        self.assertNotIn("территориальная палата", html)
        src = (KB / "pp719_full.txt").read_text(encoding="utf-8")
        point7 = src[src.index("\n7. Заявки на включение сведений в реестр"):]
        point7 = point7[:point7.index("\n8. ")]
        self.assertIn("рассматриваются Торгово-промышленной палатой Российской Федерации в порядке, "
                      "определенном ею по согласованию с Министерством промышленности и торговли", point7)


class TestAssetVersion(unittest.TestCase):
    """Статика без `Cache-Control` кэшируется браузером эвристически: после выкатки новая разметка
    пришла бы со старыми `chat.js`/`style.css`. Ссылки несут версию — хеш содержимого."""

    def test_links_carry_version(self):
        base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
        chat = (WEB / "templates" / "chat.html").read_text(encoding="utf-8")
        self.assertIn('/static/style.css?v={{ asset_v }}', base)
        self.assertIn('/static/chat.js?v={{ asset_v }}', chat)

    def test_version_follows_content(self):
        from app.api import web
        web.asset_version.cache_clear()
        self.addCleanup(web.asset_version.cache_clear)
        v1 = web.asset_version()
        web.asset_version.cache_clear()
        with mock.patch.object(Path, "read_bytes", lambda self: b"other " + self.name.encode()):
            v2 = web.asset_version()
        self.assertNotEqual(v1, v2, "версия не зависит от содержимого — кэш браузера не сбросится")
        self.assertEqual(len(v1), 10)


class TestFrontContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "static" / "chat.js").read_text(encoding="utf-8")
        cls.css = (WEB / "static" / "style.css").read_text(encoding="utf-8")

    def test_sources_attached_in_every_render_path(self):
        """История беседы, стриминг и фолбэк — источники (а с ними чипы и [N]) нужны везде."""
        self.assertEqual(self.js.count("addSources("), 4)  # объявление + три вызова
        self.assertIn("linkRefs(wrap)", self.js.split("function addSources")[1].split("\nfunction ")[0])

    def test_ref_outside_sources_stays_text(self):
        """[N] без такого источника — не кнопка: ссылка в никуда хуже отсутствующей."""
        body = self.js.split("function linkRefs")[1].split("\nfunction ")[0]
        self.assertIn("n >= 1 && n <= sources.length", body)

    def test_quota_refreshed_after_every_answer(self):
        ask = self.js.split("async function ask(text)")[1].split("\nasync function ")[0]
        self.assertIn("refreshQuota()", ask.split("finally")[1])
        self.assertIn("renderQuota(window.QUOTA || null)", self.js)

    def test_fonts_are_local(self):
        """RF-first: шрифт с диска сервиса, а не с внешнего CDN."""
        self.assertIn('url("/static/fonts/inter-tight-cyrillic.woff2")', self.css)
        self.assertNotIn("fonts.googleapis", self.css)
        for f in ("inter-tight-cyrillic.woff2", "inter-tight-latin.woff2"):
            self.assertTrue((WEB / "static" / "fonts" / f).is_file(), f)


if __name__ == "__main__":
    unittest.main()
