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
        with app.BOOK_HINTS_LOCK:
            self.original_book_hints = dict(app.BOOK_HINTS)
            app.BOOK_HINTS.clear()
        app.API_SQLITE_CACHE = os.path.join(self.tempdir.name, "cache.sqlite3")
        app.SQLITE_CACHE_READY = False
        app.CACHE.clear()
        app.initialize_disk_cache()

    def tearDown(self):
        app.API_SQLITE_CACHE = self.original_database
        app.SQLITE_CACHE_READY = self.original_ready
        app.CACHE.clear()
        with app.BOOK_HINTS_LOCK:
            app.BOOK_HINTS.clear()
            app.BOOK_HINTS.update(self.original_book_hints)
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


class CrossLanguageDetailTests(TemporaryCacheTest):
    def test_localized_fallback_reuses_canonical_description_and_cover(self):
        app.cache_set("book_detail:v4:en:OL16085155W", {
            "success": True,
            "title": "Steve Jobs",
            "description": "A complete cached English description.",
            "cover_url": "/olcover/12374726/M",
            "subjects": ["Biography"],
            "similar_subjects": ["Biography"],
            "complete": True,
        })

        detail = app.fallback_book_detail("OL16085155W", "cn")

        self.assertEqual(detail["description"], "A complete cached English description.")
        self.assertEqual(detail["cover_url"], "/olcover/12374726/M")
        self.assertEqual(detail["subjects"], ["Biography"])
        self.assertFalse(detail["complete"])


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

        self.assertEqual(len(books), 28)
        self.assertEqual([book["ol_key"] for book in books[:5]], [
            "/works/OL1W",
            "/works/OL2W",
            "/works/OL3W",
            "/works/OL4W",
            "/works/OL5W",
        ])
        self.assertEqual(sum(bool(book["cover_url"]) for book in books), 25)
        self.assertEqual(len({book["ol_key"] for book in books}), len(books))
        self.assertEqual((total, total_pages), (len(covered), 2))

    def test_discovery_pagination_does_not_skip_covered_results(self):
        sparse = [
            self.energy_game_record(
                key=f"/works/OL90{i}W",
                title=f"Exact sparse result {i}",
                author_name=[f"Sparse Author {i}"],
                language=["eng"],
            )
            for i in range(1, 6)
        ]
        covered = [
            self.energy_game_record(
                key=f"/works/OL{i}W",
                title=f"Covered result {i}",
                author_name=[f"Covered Author {i}"],
                language=["eng"],
                cover_i=1000 + i,
            )
            for i in range(1, 62)
        ]

        def search_response(_path, params):
            records = covered if "cover_i:*" in params["q"] else sparse
            limit = params["limit"]
            start = (params["page"] - 1) * limit
            return {
                "numFound": len(records),
                "docs": records[start:start + limit],
            }

        pages = []
        with patch.object(app, "ol_get", side_effect=search_response):
            for page in range(1, 4):
                books, total, total_pages = app.fetch_discovery_books(
                    "covered pagination",
                    page=page,
                    lang="en",
                )
                pages.extend(books)
                self.assertEqual((total, total_pages), (61, 3))

        covered_keys = {book["key"] for book in covered}
        returned_keys = {book["ol_key"] for book in pages}
        self.assertTrue(covered_keys <= returned_keys)

    def test_invalid_covered_records_do_not_suppress_sparse_fallback(self):
        invalid_covered = self.energy_game_record(
            key="/works/OL2W",
            title="Covered but missing author",
            author_name=[],
            cover_i=123,
        )

        def search_response(_path, params):
            records = [invalid_covered] if "cover_i:*" in params["q"] else [self.energy_game_record()]
            return {"numFound": len(records), "docs": records}

        with patch.object(app, "ol_get", side_effect=search_response):
            books, _, _ = app.fetch_discovery_books("energy game", lang="en")

        self.assertEqual([book["ol_key"] for book in books], ["/works/OL45347056W"])


class CoverSelectionTests(unittest.TestCase):
    def test_negative_cover_sentinel_is_not_a_cover(self):
        record = {
            "key": "/works/OL1W",
            "title": "Sentinel cover",
            "author_name": ["Author"],
            "language": ["eng"],
            "cover_i": -1,
        }

        self.assertIsNone(app.extract_book(record, "en", allow_missing_cover=False))

    def test_first_positive_cover_is_selected(self):
        self.assertEqual(app.edition_cover_id({"covers": [-1, 14832331]}), 14832331)

    def test_later_matching_edition_with_cover_is_preferred(self):
        record = {
            "key": "/works/OL24739863W",
            "title": "The Age of A.I.",
            "author_name": ["Henry Kissinger"],
            "language": ["eng"],
            "editions": {
                "docs": [
                    {"title": "Age of AI", "language": ["eng"]},
                    {"title": "Age of A. I.", "language": ["eng"], "covers": [14757696]},
                ]
            },
        }

        book = app.extract_book(record, "en", allow_missing_cover=False)

        self.assertEqual(book["cover_url"], "/olcover/14757696/M")


class CoverFailureCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app.COVER_FAILURES.clear()

    def tearDown(self):
        app.COVER_FAILURES.clear()
        self.tempdir.cleanup()

    def test_archive_not_found_image_is_rejected_and_negatively_cached(self):
        class NotFoundImage:
            content = b"not-a-real-cover" * 200
            headers = {"Content-Type": "image/png"}
            url = "https://archive.org/images/notfound.png"

            @staticmethod
            def raise_for_status():
                return None

        cache_path = os.path.join(self.tempdir.name, "cover.webp")
        with (
            patch.object(app, "cover_cache_path", return_value=cache_path),
            patch.object(app.SESSION, "get", return_value=NotFoundImage()) as get,
        ):
            first = app.ensure_cover_cached(
                "internetarchive",
                "missing-item",
                "M",
                "https://archive.org/services/img/missing-item",
            )
            second = app.ensure_cover_cached(
                "internetarchive",
                "missing-item",
                "M",
                "https://archive.org/services/img/missing-item",
            )

        self.assertEqual(first, (None, False))
        self.assertEqual(second, (None, False))
        get.assert_called_once()

    def test_valid_shared_cover_wins_over_this_workers_negative_marker(self):
        cache_path = os.path.join(self.tempdir.name, "cover.webp")
        app.COVER_FAILURES[cache_path] = time.time()

        with (
            patch.object(app, "cover_cache_path", return_value=cache_path),
            patch.object(app, "cover_cache_file_is_valid", return_value=True),
            patch.object(app.SESSION, "get") as get,
        ):
            result = app.ensure_cover_cached(
                "internetarchive",
                "now-present-item",
                "M",
                "https://archive.org/services/img/now-present-item",
            )

        self.assertEqual(result, (cache_path, True))
        self.assertNotIn(cache_path, app.COVER_FAILURES)
        get.assert_not_called()

    def test_waiting_cover_request_rechecks_failure_inside_path_lock(self):
        cache_path = os.path.join(self.tempdir.name, "cover.webp")

        class OtherWorkerFailed:
            def __enter__(self):
                app.COVER_FAILURES[cache_path] = time.time()

            def __exit__(self, *_args):
                return False

        with (
            patch.object(app, "cover_cache_path", return_value=cache_path),
            patch.object(app, "cover_cache_file_is_valid", return_value=False),
            patch.object(app, "cover_lock", return_value=OtherWorkerFailed()),
            patch.object(app.SESSION, "get") as get,
        ):
            result = app.ensure_cover_cached(
                "internetarchive",
                "still-missing-item",
                "M",
                "https://archive.org/services/img/still-missing-item",
            )

        self.assertEqual(result, (None, False))
        get.assert_not_called()

    def test_cover_failure_state_is_expired_and_bounded(self):
        now = time.time()
        app.COVER_FAILURES.update({
            "expired": now - app.COVER_NEGATIVE_TTL - 1,
            "oldest": now - 3,
            "middle": now - 2,
            "newest": now - 1,
        })

        with patch.object(app, "COVER_FAILURE_LIMIT", 2):
            self.assertTrue(app.recent_cover_failure("newest"))

        self.assertNotIn("expired", app.COVER_FAILURES)
        self.assertNotIn("oldest", app.COVER_FAILURES)
        self.assertEqual(set(app.COVER_FAILURES), {"middle", "newest"})

    def test_cover_origin_capacity_does_not_poison_valid_cover(self):
        cache_path = os.path.join(self.tempdir.name, "cover.webp")

        class BusyOriginSlots:
            @staticmethod
            def acquire(timeout):
                return False

            @staticmethod
            def release():
                raise AssertionError("An unacquired cover slot must not be released")

        with (
            patch.object(app, "cover_cache_path", return_value=cache_path),
            patch.object(app, "cover_cache_file_is_valid", return_value=False),
            patch.object(app, "COVER_ORIGIN_SEMAPHORE", BusyOriginSlots()),
            patch.object(app.SESSION, "get") as get,
        ):
            result = app.ensure_cover_cached(
                "openlibrary",
                "123",
                "M",
                "https://covers.openlibrary.org/b/id/123-M.jpg",
            )

        self.assertEqual(result, (None, False))
        self.assertNotIn(cache_path, app.COVER_FAILURES)
        get.assert_not_called()


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

    def test_book_api_does_not_cache_cold_detail_response(self):
        with patch.object(app, "get_book_detail", return_value=(None, "miss")):
            response = app.app.test_client().get(
                "/api/book?ol_key=/works/OL20153193W&book_lang=en"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

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

    def test_refreshing_fallback_response_is_not_cached(self):
        fallback = {
            "success": True,
            "title": "Local title",
            "description": "",
            "complete": False,
        }
        with patch.object(app, "get_book_detail", return_value=(fallback, "fallback")):
            response = app.app.test_client().get(
                "/api/book?ol_key=/works/OL1W&book_lang=en"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")


class BookDescriptionFallbackTests(unittest.TestCase):
    def test_description_list_uses_longest_readable_value(self):
        description = app.extract_desc({
            "description": [
                {"value": "Short note"},
                {"value": "A much longer description of this particular book and its subject."},
            ]
        })

        self.assertEqual(
            description,
            "A much longer description of this particular book and its subject.",
        )

    def test_edition_description_is_used_when_work_fetch_fails(self):
        edition_description = (
            "Artificial intelligence changes the economics of prediction "
            "and the decisions businesses make."
        )
        editions = {
            "entries": [{
                "languages": [{"key": "/languages/eng"}],
                "description": edition_description,
            }]
        }
        with (
            patch.object(app, "ol_get_work", return_value=None),
            patch.object(app, "ol_get", return_value=editions),
        ):
            description, complete = app.english_description_result(
                "/works/OL19747345W"
            )

        self.assertEqual(description, edition_description)
        self.assertTrue(complete)

    def test_failed_edition_lookup_is_marked_incomplete(self):
        with (
            patch.object(app, "ol_get_work", return_value=None),
            patch.object(app, "ol_get", return_value=None),
        ):
            description, complete = app.english_description_result(
                "/works/OL19747345W"
            )

        self.assertEqual(description, "")
        self.assertFalse(complete)

    def test_archive_metadata_is_used_after_open_library_descriptions(self):
        editions = {
            "entries": [{
                "languages": [{"key": "/languages/eng"}],
                "ocaid": "ageofaiourhumanf0000kiss",
            }]
        }
        archive_text = (
            "Artificial intelligence is transforming society and changing "
            "how people approach security, economics, and knowledge."
        )
        with (
            patch.object(app, "ol_get_work", return_value={"title": "The Age of AI"}),
            patch.object(app, "ol_get", return_value=editions),
            patch.object(app, "archive_description", return_value=(archive_text, True)) as archive,
        ):
            description, complete = app.english_description_result(
                "/works/OL24739863W"
            )

        self.assertEqual(description, archive_text)
        self.assertTrue(complete)
        archive.assert_called_once_with("ageofaiourhumanf0000kiss")

    def test_failed_archive_lookup_keeps_detail_incomplete(self):
        editions = {"entries": [{"ocaid": "temporary-provider-failure"}]}
        with (
            patch.object(app, "ol_get", return_value=editions),
            patch.object(app, "archive_description", return_value=("", False)),
        ):
            description, complete = app.english_description_result(
                "/works/OL1W",
                {"title": "Book"},
            )

        self.assertEqual(description, "")
        self.assertFalse(complete)

    def test_archive_failure_is_backed_off_without_becoming_complete(self):
        memory = {}

        def memory_get(key, _ttl):
            return memory.get(key)

        def memory_set(key, value):
            memory[key] = value

        with (
            patch.object(app, "cache_get", side_effect=memory_get),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set", side_effect=memory_set),
            patch.object(app, "disk_cache_set"),
            patch.object(app.SESSION, "get", side_effect=app.requests.Timeout) as get,
        ):
            first = app.archive_description("temporary-provider-failure")
            second = app.archive_description("temporary-provider-failure")

        self.assertEqual(first, ("", False))
        self.assertEqual(second, ("", False))
        get.assert_called_once()

    def test_successful_empty_edition_lookup_is_complete(self):
        with patch.object(app, "ol_get", return_value={"entries": []}):
            description, complete = app.english_description_result(
                "/works/OL1W",
                {"title": "A book without a description"},
            )

        self.assertEqual(description, "")
        self.assertTrue(complete)

    def test_fresh_incomplete_detail_schedules_another_refresh(self):
        cached = {
            "success": True,
            "title": "Cached title",
            "description": "",
            "complete": False,
        }
        with (
            patch.object(app, "cached_book_detail", return_value=(cached, "memory")),
            patch.object(app, "schedule_book_detail_refresh", return_value=True) as refresh,
        ):
            detail, cache_state = app.get_book_detail("OL19747345W", "en")

        self.assertIs(detail, cached)
        self.assertEqual(cache_state, "memory")
        refresh.assert_called_once_with("OL19747345W", "en")


if __name__ == "__main__":
    unittest.main()
