"""Правовые и справочные страницы + дисклеймеры интерфейса (офлайн, без Qdrant и DeepSeek).

Почему это тестируется. Политику конфиденциальности читают ДО того, как дать согласие в профиле,
поэтому она обязана открываться без входа — спрятанная за авторизацией, она бессмысленна. А пометка
о происхождении ответа обязана уезжать вместе с ответом: копирование выносит его за пределы сервиса
ровно так же, как экспорт (правило R3).

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WEB = ROOT / "app" / "web"


def _client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


class TestPublicDocPages(unittest.TestCase):
    """Страницы открыты без входа: их читают до согласия и до получения доступа."""

    @classmethod
    def setUpClass(cls):
        cls.c = _client()

    def test_pages_open_without_auth(self):
        for url in ("/terms", "/privacy", "/help"):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("<h1", r.text, url)

    def test_terms_state_expert_decides(self):
        """Главное правило продукта должно быть в условиях, а не только в интерфейсе."""
        t = self.c.get("/terms").text
        self.assertIn("эксперт", t.lower())
        self.assertIn("предварительн", t.lower())

    def test_privacy_names_operator_law_and_llm(self):
        """Три вещи, без которых политика — фикция: кто оператор, на каком основании, кому уходит текст."""
        t = self.c.get("/privacy").text
        self.assertIn("152-ФЗ", t)
        self.assertIn("DeepSeek", t)
        self.assertIn("соглас", t.lower())

    def test_help_has_faq_entries(self):
        t = self.c.get("/help").text
        self.assertGreaterEqual(t.count("<details"), 8, "справочный центр почти пуст")

    def test_pages_cross_link_each_other(self):
        for url in ("/terms", "/privacy", "/help"):
            t = self.c.get(url).text
            for link in ("/terms", "/privacy", "/help"):
                self.assertIn(f'href="{link}"', t, f"{url} не ссылается на {link}")


class TestChatDisclaimers(unittest.TestCase):
    """Дисклеймеры чата: постоянный под вводом и короткий над вводом при начатом диалоге."""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "templates" / "chat.html").read_text(encoding="utf-8")
        cls.css = (WEB / "static" / "style.css").read_text(encoding="utf-8")

    def test_composer_disclaimer_links_to_terms_and_privacy(self):
        self.assertIn('href="/terms"', self.html)
        self.assertIn('href="/privacy"', self.html)
        self.assertIn('href="/help"', self.html)  # «Подробнее»

    def test_notice_above_input_exists_and_is_shown_only_in_started_chat(self):
        self.assertIn('id="composer-notice"', self.html)
        self.assertIn("может ошибаться", self.html)
        # на пустом экране роль дисклеймера играет hero — строка не должна дублировать его
        self.assertIn(".main:not(.empty) .composer-notice", self.css)

    def test_corpus_edition_still_visible(self):
        """E1: редакция корпуса не должна пропасть из интерфейса при переверстке дисклеймера."""
        self.assertIn("corpus_edition", self.html)

    def test_sidebar_help_menu_has_three_entries(self):
        for link in ("/help", "/terms", "/privacy"):
            self.assertIn(f'href="{link}"', self.html)
        self.assertIn('id="help-toggle"', self.html)
        # подменю обязано открываться не только по hover — иначе недоступно с тача и клавиатуры
        self.assertIn(".nav-group:focus-within .nav-menu", self.css)
        self.assertIn(".nav-group.open .nav-menu", self.css)


class TestSourceLinkEdition(unittest.TestCase):
    """Ссылка на первоисточник в разметке чата = редакция корпуса.

    16.08.2026 на бою источники вели на текст с пометкой «Не действует»: адрес был захардкожен
    в chat.js, корпус за это время ушёл на две редакции вперёд. Разметку никто не проверял —
    ни один тест страницу чата не рендерил, поэтому дефект дожил до эксперта."""

    @classmethod
    def setUpClass(cls):
        from app.api.web import _ctx, templates
        from app.db.models import User
        from app.rag.edition import kontur_719_url

        class _Req:  # Jinja-шаблону от запроса нужен только объект в контексте
            scope = {"type": "http"}
            session: dict = {}

        cls.url = kontur_719_url()
        cls.html = templates.get_template("chat.html").render(
            **_ctx(_Req(), user=User(username="kursk.expert1", role="expert"),
                   kontur_719_url=cls.url))

    def test_page_carries_current_edition_link(self):
        import re
        doc_id = re.search(r"documentId=(\d+)", self.url).group(1)
        self.assertIn(doc_id, self.html, "адрес первоисточника не доехал до разметки")
        self.assertIn("window.KONTUR_719", self.html)

    def test_js_has_no_own_copy(self):
        js = (WEB / "static" / "chat.js").read_text(encoding="utf-8")
        self.assertNotRegex(js, r"documentId=\d",
                            "во фронте снова появился свой documentId — он разъедется с корпусом")


class TestAnswerTools(unittest.TestCase):
    """Кнопки «копировать» и «поделиться» под ответом."""

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "static" / "chat.js").read_text(encoding="utf-8")
        cls.css = (WEB / "static" / "style.css").read_text(encoding="utf-8")

    def test_tools_added_in_every_render_path(self):
        """Ответ приходит ТРЕМЯ путями — история беседы, стриминг и фолбэк; кнопки нужны везде.

        ⚠ Тест раньше требовал два вызова и был зелёным, хотя фолбэк-путь (`askFallback`, обычное
        дело на рваной сети) кнопок не добавлял и `dataset.raw` не выставлял: ответ нельзя было ни
        скопировать, ни отправить с пометкой о происхождении. Найдено при U6."""
        self.assertEqual(self.js.count("addAnswerTools("), 4)  # объявление + три вызова
        # исходный markdown обязан сохраняться в каждом пути — копируем его, а не текст из DOM
        self.assertEqual(self.js.count("dataset.raw = "), 3)

    def test_copied_text_carries_origin_note(self):
        """R3: ответ, покинувший сервис, несёт пометку — иначе получатель примет черновик за вердикт."""
        self.assertIn("SHARE_NOTE", self.js)
        self.assertIn("уполномоченный эксперт ТПП", self.js)

    def test_copy_works_without_secure_context(self):
        """Пилот работает по http, где clipboard API недоступен — нужен запасной путь."""
        self.assertIn("isSecureContext", self.js)
        self.assertIn("execCommand", self.js)

    def test_tooltip_on_hover_and_focus(self):
        self.assertIn(".tool-btn::after", self.css)
        self.assertIn("content: attr(data-tip)", self.css)
        self.assertIn("dataset.tip", self.js)          # подпись задаётся из JS
        self.assertIn(".tool-btn:focus-visible::after", self.css)  # и доступна с клавиатуры


class TestLongTableCollapse(unittest.TestCase):
    """U6: длинная таблица показывает 5 строк и кнопку «Показать полностью».

    Поведение проверяется НА САМОМ КОДЕ, а не пересказом ассертами по строкам файла: чистая часть
    логики вынесена без DOM и исполняется на node — он в проекте уже есть, CI зовёт его для
    `node --check`. Ассерты по тексту остаются там, где без DOM не обойтись (склейка с разметкой).
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "static" / "chat.js").read_text(encoding="utf-8")
        cls.css = (WEB / "static" / "style.css").read_text(encoding="utf-8")

    def _cut(self, name: str, must_contain: str) -> str:
        """Кусок chat.js между метками — то, что тест исполняет на node."""
        start, end = f"// U6-{name}-начало", f"// U6-{name}-конец"
        self.assertIn(start, self.js, f"метка {start} пропала")
        block = self.js.split(start)[1].split(end)[0]
        self.assertIn(must_contain, block, "метки разъехались с кодом")
        return block

    def _logic(self) -> str:
        return self._cut("логика", "function tblPlan")

    def _run_node(self, source: str):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "u6.js"
            script.write_text(source, encoding="utf-8")
            r = subprocess.run([shutil.which("node"), str(script)],
                               capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, (r.stderr or "")[-1200:])
        self.assertIn("OK", r.stdout)

    @unittest.skipUnless(shutil.which("node"), "node не найден — поведенческая часть пропущена")
    def test_thresholds_and_labels(self):
        checks = r"""
const assert = require("node:assert");
assert.strictEqual(tblPlan(3, false), null, "короткая таблица — без кнопки");
assert.strictEqual(tblPlan(7, false), null, "семь строк ещё не сворачиваем: спрятать две — только мешать");
assert.deepStrictEqual(tblPlan(8, false), {hideFrom: 5, label: "Показать полностью (ещё 3 строки)"});
assert.strictEqual(tblPlan(29, false).hideFrom, 5, "видно ровно пять строк тела");
assert.strictEqual(tblPlan(29, false).label, "Показать полностью (ещё 24 строки)");
assert.strictEqual(tblPlan(26, false).label, "Показать полностью (ещё 21 строку)");
assert.strictEqual(tblPlan(10, false).label, "Показать полностью (ещё 5 строк)");
assert.strictEqual(tblPlan(16, false).label, "Показать полностью (ещё 11 строк)", "11-14 — «строк»");
assert.strictEqual(tblPlan(29, true).label, "Скрыть");
assert.strictEqual(tblPlan(29, true).hideFrom, 29, "раскрытая таблица не прячет ни одной строки");
console.log("OK");
"""
        self._run_node(self._logic() + checks)

    @unittest.skipUnless(shutil.which("node"), "node не найден — поведенческая часть пропущена")
    def test_expand_survives_streaming_rerender(self):
        """Сценарий приёмки целиком, на настоящем коде: свернули → раскрыли → пришёл следующий
        фрагмент стриминга → таблица ОСТАЛАСЬ раскрытой → свернули обратно.

        Именно здесь сидел риск задачи: разметка ответа пересобирается на каждом токене, и
        состояние, хранись оно в DOM, схлопывало бы таблицу сразу после нажатия."""
        stub = r"""
const assert = require("node:assert");
// Крошечная заглушка DOM: ровно те методы, которые трогает collapseTables.
function fakeClassList() {
  const s = new Set();
  return { toggle: (n, on) => (on ? s.add(n) : s.delete(n)), has: (n) => s.has(n) };
}
function fakeBox(n) {
  const rows = Array.from({ length: n }, () => ({ classList: fakeClassList() }));
  const box = { rows, btn: null, where: null };
  box.querySelectorAll = () => rows;
  box.insertAdjacentElement = (where, el) => { box.where = where; box.btn = el; };
  return box;
}
const document = {
  createElement: () => ({
    setAttribute(k, v) { this[k] = v; },
    addEventListener(_evt, fn) { this.click = fn; },
  }),
};
const bubbleOf = (boxes) => ({ querySelectorAll: () => boxes });
const hiddenCount = (box) => box.rows.filter((r) => r.classList.has("row-hidden")).length;

const wrap = {};                       // элемент сообщения: на нём и живёт состояние
const long = fakeBox(29), short = fakeBox(4);
collapseTables(wrap, bubbleOf([long, short]));
assert.ok(long.btn, "у таблицы на 29 строк должна появиться кнопка");
assert.strictEqual(short.btn, null, "короткая таблица кнопки не получает");
assert.strictEqual(long.where, "afterend", "кнопка обязана стоять ВНЕ прокручиваемой обёртки");
assert.strictEqual(hiddenCount(long), 24, "видно должно остаться пять строк");
assert.strictEqual(long.btn.textContent, "Показать полностью (ещё 24 строки)");
assert.strictEqual(long.btn["aria-expanded"], "false");

long.btn.click();                      // раскрыли
assert.strictEqual(hiddenCount(long), 0);
assert.strictEqual(long.btn.textContent, "Скрыть");
assert.strictEqual(long.btn["aria-expanded"], "true");

// Следующий фрагмент стриминга: разметка пересобрана, таблица подросла на две строки.
const grown = fakeBox(31);
collapseTables(wrap, bubbleOf([grown, fakeBox(4)]));
assert.strictEqual(hiddenCount(grown), 0,
  "после пересборки таблица снова свернулась — нажатие пользователя потеряно");
assert.strictEqual(grown.btn.textContent, "Скрыть");

grown.btn.click();                     // свернули обратно
assert.strictEqual(hiddenCount(grown), 26);
assert.strictEqual(grown.btn.textContent, "Показать полностью (ещё 26 строк)");
console.log("OK");
"""
        self._run_node(self._logic() + self._cut("разметка", "function collapseTables") + stub)

    def test_state_lives_outside_markup(self):
        """Разметка пересобирается на КАЖДОМ фрагменте стриминга: держи состояние в DOM —
        и таблица схлопывалась бы на каждом токене, отменяя нажатие пользователя."""
        self.assertIn("wrap._tblOpen = new Set()", self.js)
        self.assertNotIn("classList.contains(\"tbl-open\")", self.js)  # состояния в разметке нет

    def test_single_render_path(self):
        """Одна точка отрисовки: новый путь вывода нельзя добавить, забыв про сворачивание."""
        self.assertEqual(self.js.count("bubble.innerHTML = renderMarkdown"), 1)
        self.assertEqual(self.js.count("renderAnswer("), 5)  # объявление + история + стрим + финал + фолбэк

    def test_button_is_accessible(self):
        self.assertIn('btn.setAttribute("aria-expanded"', self.js)
        self.assertIn(".tbl-more:focus-visible", self.css)

    def test_threshold_is_not_duplicated_in_css(self):
        """Число видимых строк живёт только в JS: продублируй его в `nth-child` — и порог
        разъедется со стилями при первой же правке."""
        self.assertIn(".bubble .row-hidden { display: none; }", self.css)
        self.assertNotIn("nth-child", self.css.split(".row-hidden")[1][:300])

    def test_button_sits_outside_scrolling_wrapper(self):
        """У `.tbl-wrap` свой горизонтальный скролл (T17) — кнопка внутри уезжала бы вбок."""
        self.assertIn('box.insertAdjacentElement("afterend", btn)', self.js)

    def test_copy_takes_full_table(self):
        """Критерий приёмки: свёрнутая таблица уходит эксперту ЦЕЛИКОМ, а не обрезанной.

        Основной путь копирует исходный markdown, запасной читает DOM — а `innerText` не видит
        строк, скрытых через display:none. Значит запасной обязан снять сокрытие на время чтения,
        иначе копия молча теряет хвост таблицы."""
        tools = self.js.split("function addAnswerTools")[1][:1600]
        self.assertIn("wrap.dataset.raw", tools)
        self.assertIn('querySelectorAll(".row-hidden")', tools)
        self.assertIn('classList.remove("row-hidden")', tools)
        self.assertIn('classList.add("row-hidden")', tools)


class TestOnboardingTour(unittest.TestCase):
    """Онбординг-тур: затемняет экран, оставляя подсвеченной одну область, и объясняет её."""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "templates" / "chat.html").read_text(encoding="utf-8")
        cls.js = (WEB / "static" / "chat.js").read_text(encoding="utf-8")
        cls.css = (WEB / "static" / "style.css").read_text(encoding="utf-8")

    def test_every_step_points_at_existing_node(self):
        """Мёртвая цель = подсветка пустоты. Проверяем на разметке, а не на глаз."""
        import re

        sels = re.findall(r'\{\s*sel:\s*"([^"]+)"', self.js)
        self.assertGreaterEqual(len(sels), 5, "шагов слишком мало")
        for sel in sels:
            node = f'id="{sel[1:]}"' if sel.startswith("#") else sel
            self.assertIn(node, self.html, f"цель шага {sel} отсутствует в разметке")

    def test_tour_markup_present_and_old_modal_removed(self):
        for node in ("tour-spot", "tour-card", "tour-title", "tour-dots", "tour-next", "tour-skip"):
            self.assertIn(f'id="{node}"', self.html)
        self.assertNotIn("onboarding-modal", self.html, "старый онбординг остался в разметке")
        self.assertNotIn("ob-step", self.html)
        self.assertNotIn(".ob-card", self.css, "мёртвые стили старого онбординга не убраны")

    def test_launcher_lives_in_help_menu(self):
        menu = self.html[self.html.index('class="nav-menu"'):self.html.index("</nav>")]
        self.assertIn('id="ob-open"', menu, "кнопку запуска не перенесли в «Справку»")

    def test_seen_flag_is_versioned(self):
        """Старый онбординг писал onboarding719Seen, и его видели все 17 палат в июле.

        Оставь мы прежнее имя ключа — обновлённый тур не показался бы ни одному из участников:
        сервис решил бы, что знакомство уже прошло. Ключ обязан быть версионированным."""
        import re

        self.assertIn("TOUR_SEEN_KEY", self.js)
        key = re.search(r'const TOUR_SEEN_KEY = "([^"]+)"', self.js)
        self.assertIsNotNone(key, "ключ флага не найден")
        self.assertNotEqual(key.group(1), "onboarding719Seen", "ключ не сменили — тур не покажется")
        self.assertRegex(key.group(1), r"_v\d+$", "в ключе нет версии — следующий тур снова не покажут")
        # обращения к localStorage идут только через константу
        self.assertNotIn('localStorage.setItem("onboarding719Seen"', self.js)
        self.assertNotIn('localStorage.getItem("onboarding719Seen"', self.js)

    def test_step_without_visible_target_is_skipped(self):
        """Сайдбар скрыт на узком экране, часть пунктов — только у админа: шаг обязан отпасть."""
        self.assertIn("getBoundingClientRect().width > 0", self.js)

    def test_spotlight_follows_layout_and_is_keyboard_operable(self):
        self.assertIn('window.addEventListener("resize"', self.js)
        self.assertIn('"scroll", follow', self.js)
        self.assertIn('e.key === "Escape"', self.js)
        self.assertIn('e.key === "ArrowRight"', self.js)

    def test_dimming_is_one_element_and_animated(self):
        """Затемнение — тень самой подсветки: иначе слои расходятся при переходе между шагами."""
        self.assertIn("box-shadow: 0 0 0 9999px", self.css)
        self.assertIn("transition: top", self.css)

    def test_reduced_motion_respected(self):
        self.assertIn("prefers-reduced-motion", self.css)

    def test_card_never_covers_the_highlight(self):
        """Поле ввода живёт внизу экрана: центрированная карточка накрывала то, что подсвечивает."""
        self.assertIn("function placeCard", self.js)
        self.assertIn("if (above >= h) top = r.top - gap - h", self.js)   # цель внизу → карточка выше
        self.assertIn("Math.min(top, vh - h - gap)", self.js)             # и не вылезает за экран
        self.assertIn("transition: top", self.css)


class TestHelpMenuLayout(unittest.TestCase):
    """Подменю «Справка» — последний пункт у нижнего края сайдбара."""

    @classmethod
    def setUpClass(cls):
        cls.css = (WEB / "static" / "style.css").read_text(encoding="utf-8")

    def test_menu_opens_upwards(self):
        """Вниз оно уходило за пределы экрана."""
        import re

        block = re.search(r"\.nav-menu \{[^}]*\}", self.css).group(0)
        self.assertIn("bottom: 0", block)
        self.assertNotIn("top: 0", block)

    def test_button_item_looks_like_the_links(self):
        """Кнопка запуска тура среди ссылок не должна выглядеть выделенной сама по себе."""
        import re

        block = re.search(r"\.nav-menu-item \{[^}]*\}", self.css).group(0)
        self.assertIn("background: none", block)
        self.assertIn("border: none", block)
        self.assertIn(".nav-menu-item:hover, .nav-menu-item:focus-visible", self.css)


if __name__ == "__main__":
    unittest.main()
