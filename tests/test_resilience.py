import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import app


class TemporaryCacheTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_database = app.API_SQLITE_CACHE
        self.original_ready = app.SQLITE_CACHE_READY
        app.API_SQLITE_CACHE = os.path.join(self.tempdir.name, "cache.sqlite3")
        app.SQLITE_CACHE_READY = False
        app.CACHE.clear()
        app.initialize_disk_cache()

    def tearDown(self):
        app.API_SQLITE_CACHE = self.original_database
        app.SQLITE_CACHE_READY = self.original_ready
        app.CACHE.clear()
        self.tempdir.cleanup()


class DurableCacheTests(TemporaryCacheTest):
    def test_expired_entry_remains_available_as_stale(self):
        app.disk_cache_set("book", {"title": "Cached"})
        cache_key = app.disk_cache_key("book")
        with sqlite3.connect(app.API_SQLITE_CACHE) as connection:
            connection.execute(
                "UPDATE api_cache SET created_at = ? WHERE cache_key = ?",
                (time.time() - 7200, cache_key),
            )

        self.assertIsNone(app.disk_cache_get("book", ttl=3600))
        self.assertEqual(
            app.disk_cache_get_stale("book", ttl=86400),
            {"title": "Cached"},
        )

    def test_openlibrary_stale_response_returns_without_origin_wait(self):
        key = "ol:/works/OL1W.json:None"
        app.disk_cache_set(key, {"title": "Local copy"})
        cache_key = app.disk_cache_key(key)
        with sqlite3.connect(app.API_SQLITE_CACHE) as connection:
            connection.execute(
                "UPDATE api_cache SET created_at = ? WHERE cache_key = ?",
                (time.time() - app.API_DISK_CACHE_TTL - 1, cache_key),
            )

        with patch.object(app, "schedule_ol_refresh", return_value=True) as refresh:
            result = app.ol_get("/works/OL1W.json")

        self.assertEqual(result["title"], "Local copy")
        refresh.assert_called_once()


class HealthEndpointTests(TemporaryCacheTest):
    def test_health_reports_shared_cache_shelves_and_jobs(self):
        app.CACHE["shelves_en_nonfiction"] = {"data": [], "time": time.time()}
        app.kindle_job_create()

        response = app.app.test_client().get("/api/health")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["database"]["ready"])
        self.assertGreaterEqual(payload["database"]["cache_entries"], 0)
        self.assertEqual(payload["cache"]["loaded_shelf_sets"], 1)
        self.assertEqual(payload["kindle_jobs"]["queued"], 1)


class WebVitalsEndpointTests(unittest.TestCase):
    def test_web_vitals_rejects_oversized_payload(self):
        response = app.app.test_client().post(
            "/api/metrics/web-vitals",
            data="x" * 5000,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 413)

    def test_web_vitals_accepts_bounded_metrics(self):
        response = app.app.test_client().post(
            "/api/metrics/web-vitals",
            json={"path": "/book/OL1W", "lcp": 420, "cls": 0.01, "inp": 60},
        )

        self.assertEqual(response.status_code, 204)


class CategoryFallbackTests(unittest.TestCase):
    def test_empty_cached_category_never_hides_populated_shelf(self):
        shelf = {
            "name": "History",
            "topic": "history",
            "books": [{"ol_key": "/works/OL1W", "title": "History", "author": "A"}],
        }
        with (
            patch.object(app, "cache_get", return_value=([], 0, 1)),
            patch.object(app, "get_shelves", return_value=[shelf]),
        ):
            books, total, total_pages = app.fetch_category_page_books(
                "history",
                page=1,
                mode="nonfiction",
                lang="en",
            )

        self.assertEqual(books, shelf["books"])
        self.assertEqual(total, 1)
        self.assertGreaterEqual(total_pages, 1)


class DiscoveryFallbackTests(unittest.TestCase):
    def setUp(self):
        app.CACHE.clear()

    def tearDown(self):
        app.CACHE.clear()

    @staticmethod
    def energy_game_record(**overrides):
        record = {
            "key": "/works/OL45347056W",
            "title": "The Energy Game",
            "author_name": ["Amantha Imber"],
            "language": [],
            "cover_i": None,
        }
        record.update(overrides)
        return record

    def test_sparse_english_work_is_recovered_without_language_or_cover(self):
        responses = [
            {"numFound": 0, "docs": []},
            {"numFound": 1, "docs": [self.energy_game_record()]},
        ]
        with patch.object(app, "ol_get", side_effect=responses) as ol_get:
            books, total, total_pages = app.fetch_discovery_books(
                "the energy game amantha imber",
                page=1,
                lang="en",
            )

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["title"], "The Energy Game")
        self.assertEqual(books[0]["author"], "Amantha Imber")
        self.assertEqual(books[0]["ol_key"], "/works/OL45347056W")
        self.assertEqual(books[0]["cover_url"], "")
        self.assertEqual((total, total_pages), (1, 1))
        self.assertEqual(ol_get.call_count, 2)
        self.assertNotIn("language:", ol_get.call_args_list[1].args[1]["q"])

    def test_fallback_rejects_explicit_wrong_language(self):
        french = self.energy_game_record(
            key="/works/OL2W",
            title="The Energy Game French Edition",
            language=["fre"],
        )
        responses = [
            {"numFound": 0, "docs": []},
            {"numFound": 2, "docs": [french, self.energy_game_record()]},
        ]
        with patch.object(app, "ol_get", side_effect=responses):
            books, _, _ = app.fetch_discovery_books("energy game", lang="en")

        self.assertEqual([book["ol_key"] for book in books], ["/works/OL45347056W"])

    def test_chinese_mode_does_not_accept_sparse_english_title(self):
        responses = [
            {"numFound": 0, "docs": []},
            {"numFound": 1, "docs": [self.energy_game_record()]},
        ]
        with patch.object(app, "ol_get", side_effect=responses):
            books, total, total_pages = app.fetch_discovery_books(
                "the energy game amantha imber",
                lang="cn",
            )

        self.assertEqual(books, [])
        self.assertEqual((total, total_pages), (1, 1))

    def test_usable_strict_results_do_not_trigger_second_request(self):
        strict = self.energy_game_record(language=["eng"], cover_i=123)
        with patch.object(
            app,
            "ol_get",
            return_value={"numFound": 1, "docs": [strict]},
        ) as ol_get:
            books, _, _ = app.fetch_discovery_books("energy", lang="en")

        self.assertEqual(len(books), 1)
        ol_get.assert_called_once()


class BookApiFallbackTests(unittest.TestCase):
    def test_book_api_serves_local_fallback_while_refreshing(self):
        fallback = {
            "success": True,
            "title": "Local title",
            "author": "Local author",
            "description": "",
            "complete": False,
        }
        with (
            app.app.test_request_context(
                "/api/book?ol_key=/works/OL1W&book_lang=en"
            ),
            patch.object(app, "get_book_detail", return_value=(fallback, "fallback")),
        ):
            response = app.api_book()
            payload = response.get_json()

        self.assertTrue(payload["success"])
        self.assertTrue(payload["refreshing"])
        self.assertEqual(payload["title"], "Local title")


if __name__ == "__main__":
    unittest.main()
