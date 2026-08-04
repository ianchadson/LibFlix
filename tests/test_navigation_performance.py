import unittest
from pathlib import Path
from unittest.mock import patch

import app


class PartialNavigationTests(unittest.TestCase):
    def test_partial_navigation_omits_persistent_shell_code(self):
        client = app.app.test_client()
        path = "/preview?title=Performance%20Test&author=LibFlix"

        full = client.get(path)
        partial = client.get(
            path,
            headers={"X-LibFlix-Navigation": "partial"},
        )

        self.assertEqual((full.status_code, partial.status_code), (200, 200))
        self.assertEqual(partial.headers["X-LibFlix-Partial"], "1")
        self.assertIn("X-LibFlix-Navigation", partial.headers.get("Vary", ""))
        self.assertIn(b'id="mainContent"', partial.data)
        self.assertIn(b'class="navbar', partial.data)
        self.assertNotIn(b"const LOADER_DELAY", partial.data)
        self.assertLess(len(partial.data), len(full.data) * 0.75)

    def test_partial_navigation_executes_scripts_without_unsafe_eval(self):
        navbar = (Path(app.APP_DIR) / "templates" / "_navbar.html").read_text()

        self.assertNotIn("Function(script.textContent)", navbar)
        self.assertIn("replacement.textContent = `(() => {", navbar)


class DiscoveryShellTests(unittest.TestCase):
    def test_cold_discovery_document_never_waits_for_provider(self):
        with (
            patch.object(app, "cached_discovery_books", return_value=None),
            patch.object(app, "fetch_discovery_books") as provider,
        ):
            response = app.app.test_client().get(
                "/discover?q=a%20completely%20cold%20query"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="bookGrid"', response.data)
        provider.assert_not_called()

    def test_warm_discovery_document_keeps_server_rendered_results(self):
        cached = ([{
            "title": "Cached result",
            "author": "Fast Author",
            "ol_key": "/works/OL1W",
            "cover_url": "",
        }], 1, 1)
        with patch.object(app, "cached_discovery_books", return_value=cached):
            response = app.app.test_client().get("/discover?q=cached")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Cached result", response.data)


if __name__ == "__main__":
    unittest.main()
