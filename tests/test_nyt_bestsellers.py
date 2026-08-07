import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import app
from nyt_bestsellers import (
    match_nyt_bestseller,
    nyt_index_valid,
    parse_nyt_number_one_pages,
)
from topic_discovery import (
    apply_nyt_bestseller_signals,
    candidate_to_book,
    merge_topic_candidates,
    parse_openlibrary_payload,
    plan_topic_query,
)


def wikipedia_page(
    title="Focused Days",
    author="Author Two",
    *,
    coauthor="",
):
    author_html = f'<a href="/wiki/{author.replace(" ", "_")}">{author}</a>'
    if coauthor:
        author_html += f' with <a href="/wiki/{coauthor.replace(" ", "_")}">{coauthor}</a>'
    return f"""
    <html><body>
      <h2>Fiction</h2>
      <table class="wikitable">
        <tr><th>Issue date</th><th>Title</th><th>Author(s)</th><th>Publisher</th><th>Ref.</th></tr>
        <tr><th>January 4</th><td>Story Book</td><td>Novelist</td><td>Press</td><td>[1]</td></tr>
      </table>
      <h2>Nonfiction</h2>
      <table class="wikitable">
        <tr><th>Issue date</th><th>Title</th><th>Author(s)</th><th>Publisher</th><th>Ref.</th></tr>
        <tr><th>January 4</th><td rowspan="3">{title}</td><td rowspan="3">{author_html}</td><td rowspan="3">Press</td><td>[2]</td></tr>
        <tr><th>January 11</th><td>[3]</td></tr>
        <tr><th>January 18</th><td>[4]</td></tr>
      </table>
    </body></html>
    """


def parsed_index(title="Focused Days", author="Author Two", *, year=2026, coauthor=""):
    return parse_nyt_number_one_pages(
        {year: wikipedia_page(title, author, coauthor=coauthor)},
        source_urls=[
            app.NYT_WIKIPEDIA_PAGE.format(year=year),
        ],
    )


class WikipediaIndexTests(unittest.TestCase):
    def test_rowspans_become_number_one_weeks(self):
        index = parsed_index()

        self.assertTrue(nyt_index_valid(index))
        book = next(item for item in index["books"] if item["title"] == "Focused Days")
        self.assertEqual(book["weeks_at_number_one"], 3)
        self.assertEqual(book["first_published_date"], "2026-01-04")
        self.assertEqual(book["published_date"], "2026-01-18")
        self.assertEqual(book["list_names"], ["Hardcover Nonfiction #1"])
        self.assertEqual(index["published_date"], "2026-01-18")

    def test_exact_title_and_any_exact_author_match(self):
        index = parsed_index(
            "The Look",
            "Michelle Obama",
            coauthor="Meredith Koop",
        )

        primary = match_nyt_bestseller(
            index,
            title="The Look",
            authors=["Michelle Obama"],
        )
        coauthor = match_nyt_bestseller(
            index,
            title="The Look",
            authors=["Meredith Koop"],
        )

        self.assertEqual(primary["rank"], 1)
        self.assertEqual(coauthor["title"], "The Look")
        self.assertIsNone(match_nyt_bestseller(
            index,
            title="The Look",
            authors=["Different Author"],
        ))
        self.assertIsNone(match_nyt_bestseller(
            index,
            title="The Look: A Memoir",
            authors=["Michelle Obama"],
        ))

    def test_malformed_or_unrelated_html_is_rejected(self):
        self.assertIsNone(parse_nyt_number_one_pages({2026: "<html></html>"}))
        self.assertIsNone(parse_nyt_number_one_pages({2026: ""}))


class NYTRankingTests(unittest.TestCase):
    @staticmethod
    def page(records):
        return parse_openlibrary_payload({"docs": records}, "focus")

    @staticmethod
    def record(key, title, author, subjects, **extra):
        record = {
            "key": key,
            "title": title,
            "author_name": [author],
            "language": ["eng"],
            "subject": subjects,
        }
        record.update(extra)
        return record

    def test_number_one_signal_breaks_close_tie_without_becoming_provider(self):
        page = self.page([
            self.record("/works/OL1W", "Focused Life", "Author One", ["Focus"]),
            self.record("/works/OL2W", "Focused Days", "Author Two", ["Focus"]),
        ])

        results = merge_topic_candidates(
            apply_nyt_bestseller_signals([page], parsed_index()),
            plan_topic_query("focus"),
        )
        book = candidate_to_book(results[0])

        self.assertEqual(book["title"], "Focused Days")
        self.assertEqual(book["sources"], ["openlibrary"])
        self.assertEqual(book["ranking_sources"], ["nyt_wikipedia"])
        self.assertEqual(book["reasons"][0], "NYT #1 bestseller")
        self.assertEqual(book["nyt_number_one"]["weeks_at_number_one"], 3)

    def test_number_one_signal_cannot_admit_an_irrelevant_book(self):
        page = self.page([
            self.record("/works/OL1W", "Popular Cookbook", "Chef", ["Cooking"]),
            self.record("/works/OL2W", "Focused Life", "Author", ["Focus"]),
        ])
        index = parsed_index("Popular Cookbook", "Chef")

        results = merge_topic_candidates(
            apply_nyt_bestseller_signals([page], index),
            plan_topic_query("focus"),
        )

        self.assertEqual(
            [result.candidate.title for result in results],
            ["Focused Life"],
        )


class FakeHtmlResponse:
    def __init__(self, body, *, content_type="text/html; charset=UTF-8", status=200):
        self.body = body.encode("utf-8")
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(self.body)),
        }
        self.closed = False

    def raise_for_status(self):
        if self.status >= 400:
            raise app.requests.HTTPError(str(self.status))

    def iter_content(self, chunk_size=64 * 1024):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class NYTWebGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_database = app.API_SQLITE_CACHE
        self.original_ready = app.SQLITE_CACHE_READY
        self.original_refresh_lock = app.NYT_REFRESH_LOCK_FILE
        app.API_SQLITE_CACHE = os.path.join(self.tempdir.name, "cache.sqlite3")
        app.NYT_REFRESH_LOCK_FILE = os.path.join(
            self.tempdir.name,
            "nyt-bestsellers.lock",
        )
        app.SQLITE_CACHE_READY = False
        app.CACHE.clear()
        app.NYT_REFRESHING = False
        app.initialize_disk_cache()

    def tearDown(self):
        app.API_SQLITE_CACHE = self.original_database
        app.SQLITE_CACHE_READY = self.original_ready
        app.NYT_REFRESH_LOCK_FILE = self.original_refresh_lock
        app.CACHE.clear()
        app.NYT_REFRESHING = False
        self.tempdir.cleanup()

    def test_fetches_only_bounded_wikipedia_year_pages_without_key(self):
        response = FakeHtmlResponse(wikipedia_page())

        with patch.object(app.SESSION, "get", return_value=response) as request:
            index = app._nyt_request()

        self.assertTrue(app._nyt_index_usable(index))
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            url = call.args[0]
            self.assertTrue(url.startswith("https://en.wikipedia.org/wiki/"))
            self.assertNotIn("api-key", repr(call))
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["stream"])

    def test_non_html_and_oversized_pages_fail_closed(self):
        non_html = FakeHtmlResponse("{}", content_type="application/json")
        oversized = FakeHtmlResponse(wikipedia_page())
        oversized.headers["Content-Length"] = str(app.NYT_HTML_MAX_BYTES + 1)

        with patch.object(app.SESSION, "get", return_value=non_html):
            self.assertIsNone(app._nyt_request())
        with patch.object(app.SESSION, "get", return_value=oversized):
            self.assertIsNone(app._nyt_request())

    def test_stale_usable_index_survives_failed_refresh(self):
        now = time.time()
        index = parsed_index()
        index.update({
            "fetched_at": now - app.NYT_FRESH_TTL - 30,
            "expires_at": now + 3600,
        })
        app.disk_cache_set(app.NYT_CACHE_KEY, index)

        with (
            patch.object(app, "filesystem_lock", return_value=nullcontext()),
            patch.object(app, "_nyt_request", return_value=None),
        ):
            app.NYT_REFRESHING = True
            app._refresh_nyt_bestsellers()

        retained = app.nyt_bestseller_index(schedule=False)
        self.assertEqual(retained["revision"], index["revision"])
        self.assertFalse(app.NYT_REFRESHING)

    def test_editorial_revision_partitions_and_expires_topic_cache(self):
        filters = dict(app.TOPIC_FILTER_DEFAULTS)
        old_key = app.topic_discovery_cache_key(
            "focus",
            "en",
            filters,
            "old-revision",
        )
        new_key = app.topic_discovery_cache_key(
            "focus",
            "en",
            filters,
            "new-revision",
        )
        self.assertNotEqual(old_key, new_key)
        app.disk_cache_set(old_key, {
            "intent": "topic",
            "all_books": [],
            "partial": False,
            "nyt_expires_at": time.time() - 1,
        })

        cached = app.cached_topic_discovery_payload(
            "focus",
            "en",
            filters,
            editorial_revision="old-revision",
        )

        self.assertIsNone(cached)
        self.assertIsNone(app.disk_cache_entry(old_key))

    def test_topic_pipeline_reports_attributed_ranking_source_only(self):
        index = parsed_index()
        index.update({
            "fetched_at": time.time(),
            "expires_at": time.time() + 3600,
        })
        page = parse_openlibrary_payload({"docs": [{
            "key": "/works/OL2W",
            "title": "Focused Days",
            "author_name": ["Author Two"],
            "language": ["eng"],
            "subject": ["Focus"],
        }]}, "focus")
        with (
            patch.object(app, "nyt_bestseller_index", return_value=index),
            patch.object(app, "_topic_cached_openlibrary_pages", return_value=[]),
            patch.object(
                app,
                "_topic_provider_pages",
                return_value=([page], ["openlibrary"], False, True),
            ),
        ):
            payload = app.fetch_topic_discovery_payload(
                "focus",
                "en",
                dict(app.TOPIC_FILTER_DEFAULTS),
            )

        self.assertEqual(payload["sources"], ["openlibrary"])
        self.assertEqual(payload["ranking_sources"], ["nyt_wikipedia"])
        self.assertGreater(payload["nyt_expires_at"], time.time())
        self.assertEqual(payload["all_books"][0]["reasons"][0], "NYT #1 bestseller")

    def test_attribution_renders_for_initial_and_dynamic_results(self):
        payload = {
            "intent": "topic",
            "topic_mode": True,
            "display_query": "focus",
            "all_books": [{
                "title": "Focused Days",
                "author": "Author Two",
                "ol_key": "/works/OL2W",
                "cover_url": "",
                "reasons": ["NYT #1 bestseller", "Subject: Focus"],
                "sources": ["openlibrary"],
                "ranking_sources": ["nyt_wikipedia"],
            }],
            "partial": False,
            "sources": ["openlibrary"],
            "ranking_sources": ["nyt_wikipedia"],
            "source_unavailable": False,
            "filters": dict(app.TOPIC_FILTER_DEFAULTS),
        }
        with patch.object(app, "cached_topic_discovery_payload", return_value=payload):
            response = app.app.test_client().get("/discover?q=focus&intent=topic")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"NYT #1", response.data)
        self.assertIn(b"en.wikipedia.org/wiki/", response.data)
        self.assertIn(b"rankingSources.includes('nyt_wikipedia')", response.data)
        self.assertNotEqual(response.headers.get("Cache-Control"), "no-store")

    def test_topic_page_one_schedules_editorial_refresh_in_background(self):
        payload = {
            "intent": "topic",
            "topic_mode": True,
            "display_query": "focus",
            "all_books": [],
            "partial": False,
            "sources": ["openlibrary"],
            "ranking_sources": [],
            "source_unavailable": False,
            "filters": dict(app.TOPIC_FILTER_DEFAULTS),
            "snapshot_id": "empty",
        }
        with (
            patch.object(app, "schedule_nyt_bestseller_refresh") as schedule,
            patch.object(app, "fetch_topic_discovery_payload", return_value=payload),
        ):
            response = app.app.test_client().get(
                "/api/discover?q=focus&intent=topic"
            )

        self.assertEqual(response.status_code, 200)
        schedule.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
