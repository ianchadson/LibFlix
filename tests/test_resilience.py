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
        with patch.object(
            app,
            "ol_get",
            return_value={"numFound": 1, "docs": [self.energy_game_record()]},
        ) as ol_get:
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
        queries = {call.args[1]["q"] for call in ol_get.call_args_list}
        self.assertIn("the energy game amantha imber", queries)
        self.assertIn(
            "the energy game amantha imber cover_i:* language:eng",
            queries,
        )

    def test_art_of_simple_living_sparse_work_stays_in_discovery_results(self):
        records = [
            self.energy_game_record(
                key="/works/OL21195178W",
                title="Zen: The Art of Simple Living",
                author_name=["Shunmyo Masuno"],
                language=["eng"],
                cover_i=123,
            ),
            self.energy_game_record(
                key="/works/OL20153193W",
                title="The Art of Simple Living",
                author_name=["Shunmyo Masuno"],
            ),
        ]
        with patch.object(
            app,
            "ol_get",
            return_value={"numFound": len(records), "docs": records},
        ):
            books, _, _ = app.fetch_discovery_books(
                "The Art of Simple Living",
                lang="en",
            )

        self.assertIn("/works/OL20153193W", [book["ol_key"] for book in books])

    def test_fallback_rejects_explicit_wrong_language(self):
        french = self.energy_game_record(
            key="/works/OL2W",
            title="The Energy Game French Edition",
            language=["fre"],
        )
        with patch.object(
            app,
            "ol_get",
            return_value={"numFound": 2, "docs": [french, self.energy_game_record()]},
        ):
            books, _, _ = app.fetch_discovery_books("energy game", lang="en")

        self.assertEqual([book["ol_key"] for book in books], ["/works/OL45347056W"])

    def test_chinese_mode_does_not_accept_sparse_english_title(self):
        with patch.object(
            app,
            "ol_get",
            return_value={"numFound": 1, "docs": [self.energy_game_record()]},
        ):
            books, total, total_pages = app.fetch_discovery_books(
                "the energy game amantha imber",
                lang="cn",
            )

        self.assertEqual(books, [])
        self.assertEqual((total, total_pages), (1, 1))

    def test_explicit_english_result_is_accepted_in_single_request(self):
        english = self.energy_game_record(language=["eng"], cover_i=123)
        with patch.object(
            app,
            "ol_get",
            return_value={"numFound": 1, "docs": [english]},
        ) as ol_get:
            books, _, _ = app.fetch_discovery_books("energy", lang="en")

        self.assertEqual(len(books), 1)
        self.assertEqual(ol_get.call_count, 2)

    def test_discovery_keeps_exact_sparse_results_then_fills_with_covers(self):
        sparse = [
            self.energy_game_record(
                key=f"/works/OL{i}W",
                title=f"The Age of AI Volume {i}",
                author_name=[f"Author {i}"],
                language=["eng"],
            )
            for i in range(1, 7)
        ]
        covered = [
            self.energy_game_record(
                key=f"/works/OL{i}W",
                title=f"The Age of AI Volume {i}",
                author_name=[f"Author {i}"],
                language=["eng"],
                cover_i=1000 + i,
            )
            for i in range(4, 40)
        ]

        def search_response(_path, params):
            if "cover_i:*" in params["q"]:
                return {"numFound": len(covered), "docs": covered}
            return {"numFound": len(sparse), "docs": sparse}

        with patch.object(app, "ol_get", side_effect=search_response):
            books, total, total_pages = app.fetch_discovery_books(
                "the age of ai",
                lang="en",
            )

        self.assertEqual(len(books), 30)
        self.assertEqual([book["ol_key"] for book in books[:5]], [
            "/works/OL1W",
            "/works/OL2W",
            "/works/OL3W",
            "/works/OL4W",
            "/works/OL5W",
        ])
        self.assertGreaterEqual(
            sum(bool(book["cover_url"]) for book in books),
            27,
        )
        self.assertEqual(len({book["ol_key"] for book in books}), len(books))
        self.assertEqual((total, total_pages), (len(covered), 1))


class BookApiFallbackTests(unittest.TestCase):
    def test_book_api_reports_cold_detail_as_refreshing(self):
        with (
            app.app.test_request_context(
                "/api/book?ol_key=/works/OL20153193W&book_lang=en"
            ),
            patch.object(app, "get_book_detail", return_value=(None, "miss")),
        ):
            response, status = app.api_book()
            payload = response.get_json()

        self.assertEqual(status, 202)
        self.assertFalse(payload["success"])
        self.assertTrue(payload["refreshing"])
        self.assertEqual(payload["cache"], "miss")

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
