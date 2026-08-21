import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

import app


ROOT = Path(__file__).resolve().parents[1]


class PwaIntegrationTests(unittest.TestCase):
    def test_versioned_shell_assets_are_public_and_do_not_set_language_cookie(self):
        shell_paths = (
            "/static/libflix.css?v=test",
            "/static/download-ui.js?v=test",
            "/static/libflix-pwa.js?v=test",
            "/static/manifest.webmanifest?v=test",
            "/static/libflix-offline.html?v=test",
            "/static/icons/libflix-icon-192.png?v=test",
            "/static/icons/libflix-icon-512.png?v=test",
            "/static/icons/libflix-icon-maskable-512.png?v=test",
        )
        with app.app.test_client() as client:
            for path in shell_paths:
                with self.subTest(path=path):
                    response = client.get(path)
                    try:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(
                            response.headers["Cache-Control"],
                            "public, max-age=31536000, immutable",
                        )
                        self.assertNotIn("Set-Cookie", response.headers)
                    finally:
                        response.close()

    def test_stale_chinese_requests_cannot_reverse_an_english_switch(self):
        with app.app.test_client() as client:
            switched = client.get("/language/en?next=/")
            self.assertEqual(switched.status_code, 302)
            self.assertIn("book_lang=en", switched.headers.get("Set-Cookie", ""))

            stale_api = client.get("/api/health?book_lang=cn")
            self.assertEqual(stale_api.status_code, 200)
            self.assertNotIn("Set-Cookie", stale_api.headers)

            with patch.object(app, "get_shelves", return_value=[]):
                stale_prefetch = client.get(
                    "/cn",
                    headers={"X-LibFlix-Navigation": "partial"},
                )
                home = client.get("/")
            self.assertEqual(stale_prefetch.status_code, 200)
            self.assertNotIn("Set-Cookie", stale_prefetch.headers)
            self.assertEqual(
                BeautifulSoup(home.data, "html.parser").html.get("lang"),
                "en",
            )

    def test_manifest_has_no_dead_search_shortcut(self):
        manifest = json.loads((ROOT / "static/manifest.webmanifest").read_text())
        self.assertNotIn("shortcuts", manifest)
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")

    def test_mobile_browse_can_be_displayed_and_settings_start_inert(self):
        css = (ROOT / "static/libflix.css").read_text()
        self.assertIn(
            "html.pwa-mobile-nav-ready .mobile-browse-sheet:not([hidden])",
            css,
        )
        with app.app.test_request_context("/"):
            html = app.render_template(
                "_navbar.html",
                mode="nonfiction",
                active_tab="home",
                **app.inject_book_context(),
            )
        panel = BeautifulSoup(html, "html.parser").select_one("#navSettingsPanel")
        self.assertIsNotNone(panel)
        self.assertEqual(panel.get("aria-hidden"), "true")
        self.assertTrue(panel.has_attr("inert"))

    def test_topics_are_cloned_into_mobile_browse_and_activate_browse(self):
        pwa = (ROOT / "static/libflix-pwa.js").read_text()
        with app.app.test_request_context("/topics"):
            html = app.render_template(
                "_navbar.html",
                mode="nonfiction",
                active_tab="topics",
                **app.inject_book_context(),
            )

        page = BeautifulSoup(html, "html.parser")
        topic_link = page.select_one('.cat-tabs a[href="/topics"]')
        self.assertIsNotNone(topic_link)
        self.assertIn("active", topic_link.get("class", []))
        self.assertIn("document.querySelectorAll('.cat-tabs a')", pwa)
        self.assertIn(r"/\/topics$/.test(path)", pwa)
        self.assertIn("link.classList.toggle('active', isCurrent)", pwa)
        self.assertIn("link.setAttribute('aria-current', 'page')", pwa)
        self.assertIn("currentMarker.setAttribute('aria-hidden', 'true')", pwa)

    def test_mobile_settings_has_one_navigation_owner(self):
        pwa = (ROOT / "static/libflix-pwa.js").read_text()
        navbar = (ROOT / "templates/_navbar.html").read_text()

        self.assertIn("settings.addEventListener('click'", pwa)
        self.assertIn("setSettingsOpen(shouldOpen)", pwa)
        self.assertNotIn(
            "document.getElementById('mobileNavSettings')?.addEventListener",
            navbar,
        )

    def test_worker_is_allowlisted_shell_only(self):
        worker = (ROOT / "static/libflix-sw.js").read_text()
        self.assertIn("SHELL_PATH_SET.has(url.pathname)", worker)
        self.assertIn("url.searchParams.size === 1", worker)
        self.assertIn("isPublicShellResponse", worker)
        self.assertNotIn("staleWhileRevalidateMetadata", worker)
        self.assertNotIn("NAVIGATION_CACHE", worker)

    def test_install_action_recognizes_desktop_user_agent_ipads(self):
        pwa = (ROOT / "static/libflix-pwa.js").read_text()

        self.assertIn("navigator.platform === 'MacIntel'", pwa)
        self.assertIn("navigator.maxTouchPoints > 1", pwa)


if __name__ == "__main__":
    unittest.main()
