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

    def test_topics_supports_lightweight_partial_navigation(self):
        client = app.app.test_client()
        full = client.get("/topics")
        partial = client.get(
            "/topics",
            headers={"X-LibFlix-Navigation": "partial"},
        )

        self.assertEqual((full.status_code, partial.status_code), (200, 200))
        self.assertEqual(partial.headers["X-LibFlix-Partial"], "1")
        self.assertIn(b'id="mainContent"', partial.data)
        self.assertIn(b"Explore topics", partial.data)
        self.assertNotIn(b"const LOADER_DELAY", partial.data)
        self.assertLess(len(partial.data), len(full.data) * 0.75)

    def test_same_page_hash_navigation_bypasses_partial_page_fetch(self):
        navbar = (Path(app.APP_DIR) / "templates" / "_navbar.html").read_text()

        self.assertIn("if (rawHref.startsWith('#') && link.matches('.topic-jump'))", navbar)
        self.assertIn("document.getElementById(decodeURIComponent(window.location.hash.slice(1)))", navbar)
        self.assertIn("history.pushState({ libflix: true", navbar)
        self.assertIn("focusFragmentTarget(hashTarget)", navbar)
        self.assertIn("target.focus({ preventScroll: true })", navbar)
        self.assertIn("let renderedPageKey = window.location.pathname + window.location.search", navbar)
        self.assertIn("if (pageKey === renderedPageKey)", navbar)
        self.assertIn("top: Number(event.state?.scrollY) || 0", navbar)

    def test_cross_page_hash_navigation_focuses_the_imported_fragment(self):
        navbar = (Path(app.APP_DIR) / "templates" / "_navbar.html").read_text()
        replace_page = navbar.split("const replacePage = async", 1)[1].split(
            "let scrollSaveFrame", 1
        )[0]

        self.assertIn("if (renderedUrl.hash)", replace_page)
        self.assertIn("fragmentTarget = document.getElementById", replace_page)
        self.assertIn("focusFragmentTarget(fragmentTarget)", replace_page)
        self.assertIn("if (!push && !fragmentTarget", replace_page)

    def test_topic_errors_have_accessible_status_and_rate_limit_copy(self):
        template = (Path(app.APP_DIR) / "templates" / "discover.html").read_text()

        self.assertIn('id="emptyState" role="status" aria-live="polite"', template)
        self.assertIn("empty.setAttribute('role', 'status')", template)
        self.assertIn("Too many searches at once. Wait a moment", template)


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

    def test_topic_partial_refresh_reconciles_the_window_and_reopens_pagination(self):
        template = (Path(app.APP_DIR) / "templates" / "discover.html").read_text()
        refresh = template.split("function refreshPartialResults()", 1)[1].split(
            "function loadMore()", 1
        )[0]

        self.assertIn("startHereRow.replaceChildren();", refresh)
        self.assertIn("bookGrid.replaceChildren();", refresh)
        self.assertIn("pendingBooks = [];", refresh)
        self.assertIn("finished = false;", refresh)
        self.assertIn("scrollSentinel.style.display = '';", refresh)
        self.assertIn("responseNeedsRefresh(data)", refresh)
        self.assertIn("activePageRequest?.abort();", refresh)
        self.assertIn("partialRefreshCount = 4;", refresh)
        self.assertLess(
            refresh.index("bookGrid.replaceChildren();"),
            refresh.index("renderStartHere(starts, true);"),
        )

    def test_topic_page_requests_abort_on_navigation_and_partial_reconcile(self):
        template = (Path(app.APP_DIR) / "templates" / "discover.html").read_text()

        self.assertIn("let activePageRequest = null;", template)
        self.assertIn("if (pageDisposed || activePageRequest !== controller) return;", template)
        self.assertIn("activePageRequest?.abort();", template)
        self.assertIn("pageDisposed = true;", template)
        self.assertIn("const delays = [7000, 11000, 16000, 32000];", template)
        self.assertIn("Number(data?.page) === 1", template)

    def test_dynamic_topic_cards_keep_canonical_preview_work_keys(self):
        template = (Path(app.APP_DIR) / "templates" / "discover.html").read_text()

        self.assertIn("function canonicalWorkKey(value)", template)
        self.assertIn(
            "bookCard.dataset.olKey = canonicalWorkKey(olKey)",
            template,
        )


class SimilarRecommendationShellTests(unittest.TestCase):
    def test_book_page_supports_author_only_recommendations(self):
        template = (Path(app.APP_DIR) / "templates" / "book.html").read_text()

        self.assertIn(
            "similarSubjects.length || bookAuthor || bookAuthors.length",
            template,
        )
        self.assertIn("subjects.slice(0, 2).forEach", template)
        self.assertNotIn("similarVisible && similarSubjects.length", template)

    def test_refreshing_recommendations_keep_polling_without_false_empty(self):
        template = (Path(app.APP_DIR) / "templates" / "book.html").read_text()

        self.assertIn("similarRetryDeadline = Date.now() + 30000", template)
        self.assertIn("if (data.refreshing && (!data.books || !data.books.length))", template)
        self.assertIn("renderSimilarRetry('Recommendations are taking longer than expected.')", template)
        self.assertNotIn("similarRetryCount < 2", template)
        self.assertNotIn("No similar books are available.", template)

    def test_similar_requests_start_in_background_and_abort_on_cleanup(self):
        template = (Path(app.APP_DIR) / "templates" / "book.html").read_text()

        self.assertIn("requestSimilarIfReady(true), 500", template)
        self.assertIn("activeDetailRequest?.abort();", template)
        self.assertIn("activeSimilarRequest?.abort();", template)
        self.assertIn("activeSimilarRequest !== controller", template)
        self.assertIn("pageDisposed = true;", template)
        self.assertIn("cache: 'no-store'", template)

    def test_recommendation_retries_honor_server_delay_and_detail_failures(self):
        template = (Path(app.APP_DIR) / "templates" / "book.html").read_text()

        self.assertIn("retryAfterMilliseconds(response)", template)
        self.assertIn("scheduleSimilarRetry(error.retryAfterMs)", template)
        self.assertIn("similarRetryCount >= 8", template)
        self.assertIn("if (!hasSimilarSeed())", template)
        self.assertIn("loadBookDetails();", template)
        self.assertIn("Finding similar books…", template)


class DownloadShellTests(unittest.TestCase):
    def test_download_loading_state_only_previews_the_best_match(self):
        templates = Path(app.APP_DIR) / "templates"

        for template_name in ("book.html", "search.html"):
            with self.subTest(template=template_name):
                template = (templates / template_name).read_text()
                loader_start = template.index('id="downloadLoader"')
                loader_end = template.index('id="downloadError"', loader_start)
                loader = template[loader_start:loader_end]

                self.assertEqual(loader.count('class="edition-skeleton"'), 1)
                self.assertIn("Finding the best available edition", loader)


if __name__ == "__main__":
    unittest.main()
