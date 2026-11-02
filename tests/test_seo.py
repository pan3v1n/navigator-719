"""Технический SEO-пакет (этап 1 плана docs/SEO_AUDIT_2026-11-02.md).

Закрепляет то, что ломается молча: поиск не сообщает об ошибке, он просто перестаёт показывать сайт.
Файлы подтверждения Яндекс и Google перепроверяют периодически: удалённый файл снимает права на панель.
Настоящий Caddy здесь не запускается (в CI нет Docker), конфиг прогонялся на нём отдельно; сторожа ниже
держат то, чего он не проверит сам.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "landing"
CANON = "https://xn--719--83dani8b8bqyy.xn--p1ai/"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def _site(name: str) -> str:
    """Тело блока сайта из Caddyfile: от строки объявления до закрывающей скобки в первой колонке."""
    m = re.search(rf"(?m)^{re.escape(name)} \{{\n(.*?)^\}}", _read("caddy/Caddyfile"), re.S)
    assert m, f"нет блока сайта {name}"
    return m.group(1)


ROOT_SITE, APP_SITE, WWW_SITE = "{$PUBLIC_DOMAIN}", "{$APP_SUBDOMAIN}.{$PUBLIC_DOMAIN}", "www.{$PUBLIC_DOMAIN}"


class TestVerificationFiles(unittest.TestCase):
    def test_yandex_file(self):
        src = _read("landing/yandex_ba25587944061317.html")
        self.assertIn("Verification: ba25587944061317", src)

    def test_google_file(self):
        self.assertEqual(_read("landing/googleec35be9683f2b40b.html").strip(),
                         "google-site-verification: googleec35be9683f2b40b.html")

    def test_not_closed_for_robots(self):
        # проверяющий робот обязан получить файл, иначе подтверждение слетит при перепроверке
        robots = _read("landing/robots.txt")
        self.assertNotIn("yandex_", robots)
        self.assertNotIn("google", robots)


class TestRobotsAndSitemap(unittest.TestCase):
    def setUp(self):
        self.robots = _read("landing/robots.txt")

    def test_root_is_open(self):
        rules = re.findall(r"(?m)^Disallow:\s*(\S*)\s*$", self.robots)
        self.assertNotIn("/", rules, "весь сайт закрыт от поиска")
        self.assertEqual(rules, ["/api/"])

    def test_points_to_sitemap(self):
        self.assertIn(f"Sitemap: {CANON}sitemap.xml", self.robots)

    def test_sitemap_urls_are_real_pages_of_this_host(self):
        tree = ET.fromstring(_read("landing/sitemap.xml").encode("utf-8"))
        locs = [e.text for e in tree.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        self.assertIn(CANON, locs)
        for loc in locs:
            self.assertTrue(loc.startswith(CANON), f"чужой хост в sitemap: {loc}")
            path = loc[len(CANON):]
            page = LANDING / path / "index.html" if not path or path.endswith("/") else LANDING / path
            self.assertTrue(page.exists(), f"в sitemap страница без файла: {loc}")


class TestHead(unittest.TestCase):
    def setUp(self):
        self.html = _read("landing/index.html")
        self.desc = re.search(r'<meta name="description" content="([^"]+)">', self.html).group(1)
        self.ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', self.html, re.S).group(1))

    def _og(self, prop: str) -> str:
        return re.search(rf'<meta property="og:{prop}" content="([^"]*)">', self.html).group(1)

    def test_canonical(self):
        self.assertIn(f'<link rel="canonical" href="{CANON}">', self.html)
        self.assertEqual(self._og("url"), CANON)

    def test_open_graph_repeats_the_page(self):
        self.assertEqual(self._og("title"), re.search(r"<title>(.*?)</title>", self.html).group(1))
        self.assertEqual(self._og("description"), self.desc)

    def test_og_image_exists_with_declared_size(self):
        url = self._og("image")
        self.assertTrue(url.startswith(CANON))
        data = (LANDING / url[len(CANON):]).read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((str(w), str(h)), (self._og("image:width"), self._og("image:height")))

    def test_structured_data(self):
        by_type = {n["@type"]: n for n in self.ld["@graph"]}
        self.assertEqual(by_type["WebSite"]["url"], CANON)
        # WebApplication намеренно нет: Google требует для него рейтинг, а отзывы — плейсхолдеры
        self.assertNotIn("WebApplication", by_type)

    def test_placeholder_reviews_are_not_marked_up(self):
        # отзывы на лендинге — плейсхолдеры: разметить их значит подать выдумку поиску как факт
        blob = json.dumps(self.ld)
        self.assertNotIn("aggregateRating", blob)
        self.assertNotIn("Review", blob)

    def test_not_found_page(self):
        src = _read("landing/404.html")
        self.assertIn('<meta name="robots" content="noindex">', src)
        # отдаётся по любому адресу, поэтому только абсолютные пути
        self.assertIn('href="/styles.css"', src)


class TestAssetsAndFormSurviveStaleCache(unittest.TestCase):
    """Ревью PR #274: новый HTML со старым app.js из кэша падал до подключения формы, и заявка уходила
    обычным GET — с именем, ИНН и телефоном в адресной строке."""

    def setUp(self):
        self.html = _read("landing/index.html")

    @staticmethod
    def _version(name: str) -> str:
        data = (LANDING / name).read_bytes().replace(b"\r\n", b"\n")   # Windows и CI дают разные концы строк
        return hashlib.sha256(data).hexdigest()[:10]

    def test_assets_carry_current_version(self):
        self.assertIn(f'href="styles.css?v={self._version("styles.css")}"', self.html,
                      "styles.css менялся: обновите ?v= в index.html, иначе браузер оставит старый файл")
        self.assertIn(f'src="app.js?v={self._version("app.js")}"', self.html,
                      "app.js менялся: обновите ?v= в index.html, иначе браузер оставит старый файл")

    def test_form_never_falls_back_to_get(self):
        self.assertRegex(self.html, r'<form [^>]*id="lead-form"[^>]* method="post"')

    def test_page_is_revalidated_and_404_is_not_cached(self):
        site = _site(ROOT_SITE)
        self.assertIn('header @page Cache-Control "no-cache"', site)
        self.assertIn('header Cache-Control "no-store"', site[site.index("handle_errors"):])


class TestAudiencePanelsInHtml(unittest.TestCase):
    """Вкладки ролей: тексты в HTML, а не в скрипте, иначе робот не видит экспертов и закупщиков."""

    def setUp(self):
        self.html = _read("landing/index.html")
        self.js = _read("landing/app.js")
        self.sec = re.search(r'<section class="sec" id="audience".*?</section>', self.html, re.S).group(0)

    def test_three_panels_only_first_shown(self):
        tags = re.findall(r'<div class="aud" id="aud-panel-(\d)" role="tabpanel" aria-labelledby="tab-(\d)"( hidden)?>', self.sec)
        self.assertEqual(tags, [("0", "0", ""), ("1", "1", " hidden"), ("2", "2", " hidden")])

    def test_tabs_point_to_their_panels(self):
        for i in range(3):
            self.assertRegex(self.sec, rf'id="tab-{i}"[^>]* aria-controls="aud-panel-{i}"')

    def test_texts_are_in_html(self):
        for text in ("Производителю", "Эксперту ТПП", "Закупщику",
                     "Найти норму для проверки заявленных технологических операций",
                     "Проверить, внесена ли продукция участника в реестр российской промышленной продукции"):
            self.assertIn(text, self.sec)
        self.assertNotIn("var TABS", self.js, "вторая копия текстов разойдётся с HTML")

    def test_counter_matches_panel(self):
        self.assertEqual(re.findall(r'<div class="counter">(\d\d / 03)</div>', self.sec),
                         ["01 / 03", "02 / 03", "03 / 03"])

    def test_role_tabs_do_not_capture_pricing_tabs(self):
        # `.seg [role="tab"]` брал и вкладки тарифов: клик по роли портил выбранный тариф, и наоборот
        code = "\n".join(ln for ln in self.js.splitlines() if not ln.lstrip().startswith("//"))
        self.assertNotIn(""".seg [role="tab"]""", code)
        self.assertIn("""aud.querySelectorAll('[role="tab"]')""", code)

    def test_focus_follows_hidden_button(self):
        # кнопка скрывается вместе с панелью: без переноса фокуса клавиатура теряет место
        for cls in (".aud-prev", ".aud-next"):
            self.assertIn(f"audPanels[cur].querySelector('{cls}').focus()", self.js)

    def test_panel_change_is_announced(self):
        self.assertRegex(self.sec, r'<div aria-live="polite">\s*<!-- Все три панели')

    def test_pricing_tabs_keep_keyboard_access(self):
        # неактивные вкладки вне порядка Tab: без стрелок до них не добраться с клавиатуры
        block = self.js[self.js.index("ptabs.forEach(function (t, k)"):self.js.index("data-goto")]
        self.assertIn("ArrowRight", block)
        self.assertIn("next.focus()", block)


class TestCaddy(unittest.TestCase):
    def test_www_redirects_to_root_keeping_path(self):
        self.assertEqual(WWW_SITE and _site(WWW_SITE).strip(), "redir https://{$PUBLIC_DOMAIN}{uri} permanent")

    def test_index_html_redirects(self):
        self.assertIn("redir /index.html / permanent", _site(ROOT_SITE))

    def test_landing_is_not_noindexed(self):
        self.assertNotIn("X-Robots-Tag", _site(ROOT_SITE))

    def test_notes_are_hidden_with_the_404_page(self):
        site = _site(ROOT_SITE)
        self.assertIn("error @notes 404", site, "respond мимо handle_errors отдал бы пустое тело")
        self.assertIn("rewrite * /404.html", site)
        self.assertTrue((LANDING / "fonts" / "README.md").exists(), "заметка убрана — правило лишнее")

    def _open_paths(self) -> list[str]:
        return re.search(r"@private not path (.+)", _site(APP_SITE)).group(1).split()

    def test_only_public_pages_of_the_app_stay_open(self):
        self.assertEqual(self._open_paths(), ["/help", "/terms", "/privacy", "/robots.txt", "/static/*"])
        self.assertIn('header @private X-Robots-Tag "noindex"', _site(APP_SITE))

    def test_open_pages_exist_in_the_app(self):
        web = _read("app/api/web.py")
        for path in ("/help", "/terms", "/privacy"):
            self.assertIn(f'@router.get("{path}"', web)

    def test_app_robots_lets_crawler_read_the_noindex(self):
        body = re.search(r"respond @robots `(.*?)`", _site(APP_SITE), re.S).group(1)
        self.assertTrue(body.startswith("User-agent: *\n"))
        # страницу под запретом робот не откроет и `noindex` не увидит, поэтому закрыт только API
        self.assertEqual(re.findall(r"(?m)^Disallow:\s*(\S*)\s*$", body), ["/api/"])

    def test_streaming_survived(self):
        self.assertIn("flush_interval -1", _site(APP_SITE))


@unittest.skipUnless(shutil.which("bash"), "bash недоступен")
class TestDeployChecksWww(unittest.TestCase):
    """По поведению: фрагмент deploy.sh с подставным curl, по образцу TestCaddyConfMatches."""

    def _run(self, reply: str) -> tuple[int, str]:
        src = _read("scripts/deploy/deploy.sh")
        frag = re.search(r'^  host="www\.\$PUBLIC_DOMAIN_ENV".*?compose logs caddy\)"\n', src, re.S | re.M).group(0)
        script = (f'set -u\nPUBLIC_DOMAIN_ENV=x.test; FAILED=0\nsleep() {{ :; }}; say() {{ echo "$*"; }}\n'
                  f'fail() {{ FAILED=1; }}\ncurl() {{ printf "{reply}"; }}\n{frag}'
                  f'echo "FAILED=$FAILED retries=$n"\n')
        r = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, encoding="utf-8")
        return r.returncode, r.stdout

    def test_redirect_passes_at_once(self):
        rc, out = self._run("301")
        self.assertEqual(rc, 0, out)
        self.assertIn("FAILED=0 retries=0", out)
        self.assertNotIn("⚠", out)

    def test_missing_certificate_warns_but_does_not_fail(self):
        rc, out = self._run("000")
        self.assertEqual(rc, 0, out)
        self.assertIn("⚠ www не отдаёт 301", out)
        self.assertIn("FAILED=0 retries=6", out, "имя второстепенное: сертификат не должен ронять выкатку")


if __name__ == "__main__":
    unittest.main()
