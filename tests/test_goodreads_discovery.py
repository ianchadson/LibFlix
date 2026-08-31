import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from goodreads_discovery import parse_most_read_books


MOST_READ_HTML = """
<table>
  <tr itemscope itemtype="http://schema.org/Book">
    <td><a class="bookTitle" href="/book/show/101-the-first-book"><span itemprop="name">The First Book</span></a></td>
    <td><a class="authorName"><span itemprop="name">A. Writer</span></a></td>
    <td><span class="minirating">4.31 avg rating — 12,345 ratings</span></td>
    <td><span class="greyText statistic">4,321 people read it</span></td>
  </tr>
  <tr itemscope itemtype="http://schema.org/Book">
    <td><a class="bookTitle" href="/book/show/202-second-book">Second Book</a></td>
    <td><a class="authorName">B. Author</a></td>
    <td><span class="minirating">3.95 avg rating — 876 ratings</span></td>
    <td><span class="greyText statistic">512 people read it</span></td>
  </tr>
</table>
"""


class GoodreadsDiscoveryParserTests(unittest.TestCase):
    def test_most_read_parser_preserves_rank_and_aggregate(self):
        books = parse_most_read_books(MOST_READ_HTML)

        self.assertEqual([book["title"] for book in books], ["The First Book", "Second Book"])
        self.assertEqual(books[0]["url"], "/book/show/101")
        self.assertEqual(books[0]["average"], 4.31)
        self.assertEqual(books[0]["ratings_count"], 12345)
        self.assertEqual(books[0]["activity_count"], 4321)

class GoodreadsDiscoveryCacheTests(unittest.TestCase):
    def test_identity_mapping_rejects_derivatives_and_keeps_goodreads_rating(self):
        records = [{
            "key": "/works/OL9W",
            "title": "The First Book Summary",
            "author_name": ["A. Writer"],
            "cover_i": 9,
            "language": ["eng"],
        }, {
            "key": "/works/OL1W",
            "title": "The First Book",
            "author_name": ["A. Writer"],
            "cover_i": 1,
            "language": ["eng"],
        }]
        source = [{
            "rank": 1,
            "title": "The First Book",
            "author": "A. Writer",
            "url": "/book/show/101",
            "average": 4.31,
            "ratings_count": 12345,
            "activity_count": 4321,
        }]
        with patch.object(app, "ol_get", return_value={"docs": records}):
            mapped = app._map_goodreads_books_to_openlibrary(source)

        book = mapped[("the first book", "a. writer")]
        self.assertEqual(book["ol_key"], "/works/OL1W")
        self.assertEqual(book["rating_source"], "Goodreads")
        self.assertEqual(book["rating_average"], 4.31)

    def test_cold_home_read_only_schedules_and_never_builds_inline(self):
        with (
            patch.object(app, "_goodreads_discovery_cached_entry", return_value=None),
            patch.object(app, "schedule_goodreads_discovery_refresh", return_value=True) as schedule,
            patch.object(app, "build_goodreads_discovery") as build,
        ):
            shelves = app.goodreads_discovery_shelves("fiction", "en")

        self.assertEqual(shelves, [])
        schedule.assert_called_once_with("fiction")
        build.assert_not_called()

    def test_stale_rails_render_immediately_while_refresh_is_queued(self):
        payload = {
            "version": app.GOODREADS_DISCOVERY_CACHE_VERSION,
            "mode": "nonfiction",
            "fetched_at": int(time.time()) - app.GOODREADS_DISCOVERY_FRESH_TTL - 1,
            "shelves": [{"name": "Popular on Goodreads", "books": [{"title": "Book"}]}],
        }
        with (
            patch.object(app, "_goodreads_discovery_cached_entry", return_value={
                "age": app.GOODREADS_DISCOVERY_FRESH_TTL + 1,
                "data": payload,
                "source": "memory",
            }),
            patch.object(app, "schedule_goodreads_discovery_refresh", return_value=True) as schedule,
        ):
            shelves = app.goodreads_discovery_shelves("nonfiction", "en")

        self.assertEqual(shelves[0]["name"], "Popular on Goodreads")
        schedule.assert_called_once_with("nonfiction")

    def test_chinese_mode_does_not_schedule_goodreads(self):
        with patch.object(app, "schedule_goodreads_discovery_refresh") as schedule:
            shelves = app.goodreads_discovery_shelves("fiction", "cn")

        self.assertEqual(shelves, [])
        schedule.assert_not_called()


class ReviewSummaryContractTests(unittest.TestCase):
    def test_summary_endpoint_is_cache_only(self):
        summary = {
            "/works/OL1W": {
                "average": 4.6,
                "ratings_count": 500,
                "source": "Goodreads",
                "url": "https://www.goodreads.com/book/show/1",
            },
        }
        with (
            patch.object(app, "cached_reception_summaries", return_value=summary) as cached,
            patch.object(app, "schedule_book_reception_refresh") as refresh,
            patch.object(app, "fetch_goodreads_reception") as goodreads,
        ):
            response = app.app.test_client().get(
                "/api/reception-summaries?ol_key=/works/OL1W&book_lang=en"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ratings"], summary)
        cached.assert_called_once_with(["/works/OL1W"], "en")
        refresh.assert_not_called()
        goodreads.assert_not_called()

    def test_goodreads_summary_wins_over_open_library(self):
        payload = {
            "success": True,
            "rating": {
                "source": "Open Library",
                "average": 4.1,
                "ratings_count": 20,
                "url": "https://openlibrary.org/works/OL1W",
            },
            "other_ratings": [{
                "source": "Goodreads",
                "average": 4.5,
                "ratings_count": 1000,
                "url": "https://www.goodreads.com/book/show/1",
            }],
        }

        summary = app.reception_summary_from_payload(payload)

        self.assertEqual(summary["source"], "Goodreads")
        self.assertEqual(summary["average"], 4.5)
        self.assertGreater(summary["confidence_score"], 0)

    def test_source_ratings_are_never_averaged_together(self):
        payload = {
            "success": True,
            "rating": {
                "source": "Goodreads",
                "average": 4.8,
                "ratings_count": 40,
                "url": "https://www.goodreads.com/book/show/1",
            },
            "other_ratings": [{
                "source": "Open Library",
                "average": 3.6,
                "ratings_count": 120,
                "url": "https://openlibrary.org/works/OL1W",
            }],
        }

        ratings = app.source_ratings_from_payload(payload)

        self.assertEqual([rating["source"] for rating in ratings], [
            "Goodreads", "Open Library",
        ])
        self.assertEqual([rating["average"] for rating in ratings], [4.8, 3.6])
        self.assertNotIn("average", app.reception_payload_with_source_ratings(payload))

    def test_invalid_goodreads_summary_falls_back_to_open_library(self):
        payload = {
            "success": True,
            "rating": {
                "source": "Goodreads",
                "average": 0,
                "ratings_count": 0,
            },
            "other_ratings": [{
                "source": "Open Library",
                "average": 4.2,
                "ratings_count": 35,
                "url": "https://openlibrary.org/works/OL1W",
            }],
        }

        summary = app.reception_summary_from_payload(payload)

        self.assertEqual(summary["source"], "Open Library")
        self.assertEqual(summary["average"], 4.2)


class ReviewSurfaceTemplateTests(unittest.TestCase):
    def test_shared_cards_and_dynamic_surfaces_carry_rating_data(self):
        shared = (Path(app.APP_DIR) / "templates" / "_book_card.html").read_text()
        navbar = (Path(app.APP_DIR) / "templates" / "_navbar.html").read_text()
        homepage = (Path(app.APP_DIR) / "templates" / "index.html").read_text()
        category = (Path(app.APP_DIR) / "templates" / "category.html").read_text()
        discovery = (Path(app.APP_DIR) / "templates" / "discover.html").read_text()
        book_page = (Path(app.APP_DIR) / "templates" / "book.html").read_text()

        self.assertIn('data-rating-average=', shared)
        self.assertIn("/api/reception-summaries?", navbar)
        self.assertIn("requestIdleCallback", navbar)
        self.assertIn("window.LibFlixRatings", navbar)
        self.assertNotIn("data-review-jump", navbar)
        self.assertNotIn("Open reviews", navbar)
        self.assertIn("goodreads_shelves", homepage)
        self.assertIn("data-goodreads-shelf", homepage)
        self.assertIn("shelf-source-link", homepage)
        for template in (category, discovery, book_page):
            self.assertIn("data-rating-average", template)
            self.assertIn("data-ratings-count", template)

    def test_similar_books_expose_intents_and_hover_only_reasons(self):
        navbar = (Path(app.APP_DIR) / "templates" / "_navbar.html").read_text()
        book_page = (Path(app.APP_DIR) / "templates" / "book.html").read_text()

        self.assertIn('id="similarIntents"', book_page)
        self.assertIn("recommendation_groups", book_page)
        self.assertIn("data-recommendation-reason", book_page)
        self.assertIn("card.dataset.recommendationReason", navbar)


if __name__ == "__main__":
    unittest.main()
