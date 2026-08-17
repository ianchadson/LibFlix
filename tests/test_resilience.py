import io
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import app
from topic_discovery import DiscoveryCandidate, ProviderPage


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
        with closing(sqlite3.connect(app.API_SQLITE_CACHE)) as connection:
            with connection:
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
        with closing(sqlite3.connect(app.API_SQLITE_CACHE)) as connection:
            with connection:
                connection.execute(
                    "UPDATE api_cache SET created_at = ? WHERE cache_key = ?",
                    (time.time() - app.API_DISK_CACHE_TTL - 1, cache_key),
                )

        with patch.object(app, "schedule_ol_refresh", return_value=True) as refresh:
            result = app.ol_get("/works/OL1W.json")

        self.assertEqual(result["title"], "Local copy")
        refresh.assert_called_once()
        self.assertNotIn(key, app.CACHE)


class ProviderBoundaryTests(TemporaryCacheTest):
    class FakeResponse:
        def __init__(self, chunks, content_length=""):
            self._chunks = chunks
            self.headers = {"Content-Length": content_length} if content_length else {}

        def iter_content(self, chunk_size=0):
            del chunk_size
            yield from self._chunks

    def test_bounded_json_rejects_declared_and_streamed_oversize_payloads(self):
        declared = self.FakeResponse([b"{}"], str(app.UPSTREAM_JSON_MAX_BYTES + 1))
        streamed = self.FakeResponse([
            b"{" + b" " * app.UPSTREAM_JSON_MAX_BYTES,
            b"}",
        ])

        with self.assertRaisesRegex(ValueError, "too large"):
            app.bounded_upstream_json(declared)
        with self.assertRaisesRegex(ValueError, "too large"):
            app.bounded_upstream_json(streamed)

    def test_bounded_json_rejects_non_object_schema(self):
        response = self.FakeResponse([b"[]"])

        with self.assertRaisesRegex(ValueError, "not an object"):
            app.bounded_upstream_json(response)

    def test_malformed_cached_search_payload_is_purged_before_use(self):
        params = {"subject": "focus"}
        key = f"ol:/search.json:{str(params)}"
        app.cache_set(key, {"unexpected": []})
        app.disk_cache_set(key, {"unexpected": []})
        replacement = {"docs": []}
        with patch.object(app, "_openlibrary_request", return_value=replacement) as origin:
            result = app.ol_get("/search.json", params, allow_stale=False)

        self.assertEqual(result, replacement)
        origin.assert_called_once()
        self.assertEqual(app.disk_cache_get(key), replacement)

    def test_refresh_queues_reject_saturation_without_submitting(self):
        with (
            patch.object(app, "OL_REFRESHING", {"existing"}),
            patch.object(app, "OL_REFRESH_PENDING_LIMIT", 1),
            patch.object(app.OL_REFRESH_EXECUTOR, "submit") as ol_submit,
        ):
            self.assertFalse(app.schedule_ol_refresh("new", "/search.json"))
        ol_submit.assert_not_called()

        with (
            patch.object(app, "INVENTAIRE_REFRESHING", {"existing"}),
            patch.object(app, "INVENTAIRE_REFRESH_PENDING_LIMIT", 1),
            patch.object(app.INVENTAIRE_REFRESH_EXECUTOR, "submit") as inv_submit,
        ):
            self.assertFalse(app.schedule_inventaire_refresh("new", "/search"))
        inv_submit.assert_not_called()

    def test_refresh_reservation_is_released_when_submit_fails(self):
        refreshing = set()
        with (
            patch.object(app, "INVENTAIRE_REFRESHING", refreshing),
            patch.object(app.INVENTAIRE_REFRESH_EXECUTOR, "submit", side_effect=RuntimeError),
        ):
            self.assertFalse(app.schedule_inventaire_refresh("new", "/search"))

        self.assertEqual(refreshing, set())


class CrossLanguageDetailTests(TemporaryCacheTest):
    def test_localized_fallback_reuses_canonical_description_and_cover(self):
        app.cache_set("book_detail:v4:en:OL16085155W", {
            "success": True,
            "title": "Steve Jobs",
            "description": "A complete cached English description.",
            "cover_url": "/olcover/12374726/M.webp",
            "subjects": ["Biography"],
            "similar_subjects": ["Biography"],
            "complete": True,
        })

        detail = app.fallback_book_detail("OL16085155W", "cn")

        self.assertEqual(detail["description"], "A complete cached English description.")
        self.assertEqual(detail["cover_url"], "/olcover/12374726/M.webp")
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


class DiscoveryFallbackTests(TemporaryCacheTest):
    def setUp(self):
        super().setUp()
        self.original_inventaire_identity = app.fetch_inventaire_identity_books
        self.inventaire_patcher = patch.object(
            app,
            "fetch_inventaire_identity_books",
            return_value=([], False),
        )
        self.inventaire_patcher.start()

    def tearDown(self):
        self.inventaire_patcher.stop()
        super().tearDown()

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
                title=f"Covered pagination sparse result {i}",
                author_name=[f"Sparse Author {i}"],
                language=["eng"],
            )
            for i in range(1, 6)
        ]
        covered = [
            self.energy_game_record(
                key=f"/works/OL{i}W",
                title=f"Covered pagination result {i}",
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

    def test_discovery_upstream_failure_is_not_cached_as_no_results(self):
        with (
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set") as memory_set,
            patch.object(app, "disk_cache_set") as disk_set,
            patch.object(app, "ol_get", return_value=None),
        ):
            books, total, total_pages = app.fetch_discovery_books(
                "temporarily unavailable",
                lang="en",
            )

        self.assertEqual(books, [])
        self.assertIsNone(total)
        self.assertEqual(total_pages, 1)
        memory_set.assert_not_called()
        disk_set.assert_not_called()

    def test_inventaire_identity_keeps_search_working_during_openlibrary_outage(self):
        cover_hash = "a" * 40
        fallback = [{
            "title": "Catching Fire",
            "author": "Suzanne Collins",
            "cover_url": app.inventaire_cover_url(cover_hash),
            "ol_key": "/works/OL5735360W",
            "description": "2009 novel by Suzanne Collins",
        }]
        with (
            patch.object(app, "ol_get", return_value=None),
            patch.object(
                app,
                "fetch_inventaire_identity_books",
                return_value=(fallback, True),
            ),
        ):
            books, total, total_pages = app.fetch_discovery_books(
                "catching fire suzanne collins",
                lang="en",
            )

        self.assertEqual(books, fallback)
        self.assertEqual((total, total_pages), (1, 1))

    def test_inventaire_identity_fallback_is_not_repeated_on_later_pages(self):
        with (
            patch.object(
                app,
                "ol_get",
                return_value={"numFound": 0, "docs": []},
            ),
            patch.object(app, "fetch_inventaire_identity_books") as inventaire,
        ):
            books, total, total_pages = app.fetch_discovery_books(
                "later fallback page",
                page=2,
                lang="en",
            )

        inventaire.assert_not_called()
        self.assertEqual(books, [])
        self.assertEqual((total, total_pages), (0, 1))

    def test_inventaire_identity_requires_a_mapped_relevant_work(self):
        relevant = DiscoveryCandidate(
            provider="inventaire",
            provider_id="wd:Q837140",
            native_rank=0,
            query_rank=0,
            title="Catching Fire",
            authors=("Suzanne Collins",),
            work_key="/works/OL5735360W",
            languages=("eng",),
            description="2009 novel by Suzanne Collins",
            cover_hash="b" * 40,
        )
        unrelated = DiscoveryCandidate(
            provider="inventaire",
            provider_id="wd:Q2",
            native_rank=1,
            query_rank=0,
            title="An Unrelated Book",
            authors=("Different Author",),
            work_key="/works/OL2W",
            languages=("eng",),
            cover_hash="c" * 40,
        )
        page = ProviderPage(
            provider="inventaire",
            query="catching fire suzanne collins",
            query_rank=0,
            candidates=(relevant, unrelated),
        )
        with patch.object(app, "_topic_inventaire_pages", return_value=[page]):
            books, available = self.original_inventaire_identity(
                "catching fire suzanne collins",
                "en",
            )

        self.assertTrue(available)
        self.assertEqual([book["ol_key"] for book in books], ["/works/OL5735360W"])
        self.assertEqual(books[0]["cover_url"], f"/invcover/{'b' * 40}/M.webp")

    def test_english_discovery_uses_fast_list_fields(self):
        with patch.object(
            app,
            "ol_get",
            return_value={"numFound": 0, "docs": []},
        ) as ol_get:
            app.fetch_discovery_books("fast fields", lang="en")

        self.assertEqual(ol_get.call_count, 2)
        self.assertTrue(all(
            call.args[1]["fields"] == app.OL_LIST_FIELDS
            for call in ol_get.call_args_list
        ))

    def test_discovery_api_reports_provider_outage_instead_of_no_books(self):
        with patch.object(
            app,
            "fetch_discovery_books",
            return_value=([], None, 1),
        ):
            response = app.app.test_client().get("/api/discover?q=outage")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "source_unavailable")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_discovery_page_shows_retry_for_provider_outage(self):
        with patch.object(
            app,
            "fetch_discovery_books",
            return_value=([], None, 1),
        ):
            response = app.app.test_client().get("/discover?q=outage")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"temporarily unavailable", response.data)
        self.assertIn(b"Try again", response.data)
        self.assertIn("private", response.headers["Cache-Control"])


class CoverSelectionTests(unittest.TestCase):
    def test_legacy_cached_cover_urls_are_upgraded_to_canonical_webp(self):
        books = app.canonicalize_book_covers([
            {"title": "Legacy", "cover_url": "/olcover/12345/M"},
            {"title": "Archive", "cover_url": "/iacover/example-id/S"},
        ])

        self.assertEqual(books[0]["cover_url"], "/olcover/12345/M.webp")
        self.assertEqual(books[1]["cover_url"], "/iacover/example-id/M.webp")

    def test_cover_localization_preserves_valid_archive_fallback(self):
        source = app.open_library_cover_url(12345, "S", "archive-id")

        self.assertEqual(
            app.localize_cover_url(source, "L"),
            "/olcover/12345/L.webp?ia=archive-id",
        )
        self.assertEqual(
            app.size_url(source, "M"),
            "/olcover/12345/M.webp?ia=archive-id",
        )

    def test_inventaire_cover_urls_accept_only_entity_hashes(self):
        cover_hash = "d" * 40

        self.assertEqual(
            app.inventaire_cover_url(cover_hash, "L"),
            f"/invcover/{cover_hash}/L.webp",
        )
        self.assertEqual(app.inventaire_cover_url("../unsafe", "L"), "")

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

        self.assertEqual(book["cover_url"], "/olcover/14757696/M.webp")


class CoverFailureCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app.COVER_FAILURES.clear()
        app.COVER_VALIDATED_FILES.clear()

    def tearDown(self):
        app.COVER_FAILURES.clear()
        app.COVER_VALIDATED_FILES.clear()
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

    def test_cover_failure_marker_is_shared_between_workers(self):
        cache_path = os.path.join(self.tempdir.name, "cover.webp")
        app.remember_cover_failure(cache_path)
        app.COVER_FAILURES.clear()

        self.assertTrue(app.recent_cover_failure(cache_path))

        marker = app.cover_failure_marker_path(cache_path)
        old = time.time() - app.COVER_NEGATIVE_TTL - 1
        os.utime(marker, (old, old))
        app.COVER_FAILURES.clear()
        self.assertFalse(app.recent_cover_failure(cache_path))
        self.assertFalse(os.path.exists(marker))


class CoverPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_cover_dir = app.COVER_CACHE_DIR
        app.COVER_CACHE_DIR = self.tempdir.name
        app.COVER_FAILURES.clear()
        app.COVER_VALIDATED_FILES.clear()

    def tearDown(self):
        app.COVER_CACHE_DIR = self.original_cover_dir
        app.COVER_FAILURES.clear()
        app.COVER_VALIDATED_FILES.clear()
        self.tempdir.cleanup()

    @staticmethod
    def source_image_bytes():
        output = io.BytesIO()
        app.Image.new("RGB", (800, 1200), "navy").save(output, format="JPEG")
        return output.getvalue()

    @staticmethod
    def image_response(content, url):
        class ImageResponse:
            headers = {"Content-Type": "image/jpeg"}

            @staticmethod
            def raise_for_status():
                return None

        response = ImageResponse()
        response.content = content
        response.url = url
        return response

    @unittest.skipIf(app.Image is None, "Pillow is required for optimized cover tests")
    def test_large_openlibrary_fetch_generates_all_variants_once(self):
        source_url = "https://covers.openlibrary.org/b/id/123-L.jpg"
        response = self.image_response(self.source_image_bytes(), source_url)
        with patch.object(app.SESSION, "get", return_value=response) as get:
            result = app.ensure_cover_cached("openlibrary", "123", "L", source_url)
            medium = app.ensure_cover_cached(
                "openlibrary",
                "123",
                "M",
                "https://covers.openlibrary.org/b/id/123-M.jpg",
            )

        self.assertEqual(result, (app.cover_cache_path("openlibrary", "123", "L"), False))
        self.assertEqual(medium, (app.cover_cache_path("openlibrary", "123", "M"), True))
        get.assert_called_once()
        for size in ("S", "M", "L"):
            self.assertTrue(app.cover_cache_file_is_valid(app.cover_cache_path("openlibrary", "123", size)))

    @unittest.skipIf(app.Image is None, "Pillow is required for optimized cover tests")
    def test_medium_openlibrary_fetch_does_not_create_low_quality_large_variant(self):
        source_url = "https://covers.openlibrary.org/b/id/456-M.jpg"
        response = self.image_response(self.source_image_bytes(), source_url)
        with patch.object(app.SESSION, "get", return_value=response):
            app.ensure_cover_cached("openlibrary", "456", "M", source_url)

        self.assertTrue(os.path.exists(app.cover_cache_path("openlibrary", "456", "S")))
        self.assertTrue(os.path.exists(app.cover_cache_path("openlibrary", "456", "M")))
        self.assertFalse(os.path.exists(app.cover_cache_path("openlibrary", "456", "L")))

    @unittest.skipIf(app.Image is None, "Pillow is required for optimized cover tests")
    def test_successful_validation_fingerprint_avoids_reopening_image(self):
        cache_path = app.cover_cache_path("openlibrary", "789", "M")
        app.write_optimized_cover(self.source_image_bytes(), cache_path, "M")
        app.COVER_VALIDATED_FILES.clear()
        image_open = app.Image.open

        with patch.object(app.Image, "open", wraps=image_open) as open_image:
            self.assertTrue(app.cover_cache_file_is_valid(cache_path))
            self.assertTrue(app.cover_cache_file_is_valid(cache_path))

        self.assertEqual(open_image.call_count, 1)

    def test_identity_lock_is_shared_by_all_sizes(self):
        medium = app.cover_cache_path("openlibrary", "same", "M")
        large = app.cover_cache_path("openlibrary", "same", "L")

        self.assertEqual(
            app.cover_identity_lock_path("openlibrary", "same", medium),
            app.cover_identity_lock_path("openlibrary", "same", large),
        )

    def test_extension_cover_routes_are_canonical_without_redirects(self):
        md5 = "a" * 32
        cover_hash = "b" * 40
        download_url = app.download_cover_url(md5, "123000")
        self.assertEqual(download_url, f"/cover/{md5}/S.webp?dir=123000")
        self.assertEqual(app.open_library_cover_url(123, "M"), "/olcover/123/M.webp")
        self.assertEqual(app.archive_cover_url("archive-id", "M"), "/iacover/archive-id/M.webp")
        self.assertEqual(app.inventaire_cover_url(cover_hash, "M"), f"/invcover/{cover_hash}/M.webp")

        with patch.object(app, "cached_cover_response", return_value=("cover", 200)) as cached:
            client = app.app.test_client()
            for url in (
                download_url,
                "/olcover/123/M.webp",
                "/iacover/archive-id/M.webp",
                f"/invcover/{cover_hash}/M.webp",
            ):
                response = client.get(url, follow_redirects=False)
                self.assertEqual(response.status_code, 200, url)
                self.assertNotIn("Location", response.headers, url)

            client.get("/olcover/123/M.webp?ia=archive-id")

        self.assertEqual(
            cached.call_args.kwargs["fallback"],
            (
                "internetarchive",
                "archive-id",
                "https://archive.org/services/img/archive-id",
                "",
            ),
        )

    def test_cached_cover_response_uses_validated_fallback(self):
        fallback_path = os.path.join(self.tempdir.name, "fallback.webp")
        with (
            patch.object(
                app,
                "ensure_cover_cached",
                side_effect=[(None, False), (fallback_path, True)],
            ) as ensure,
            patch.object(
                app,
                "send_file",
                return_value=app.Response(b"cover", mimetype="image/webp"),
            ),
        ):
            response = app.cached_cover_response(
                "openlibrary",
                "123",
                "M",
                "https://covers.openlibrary.org/b/id/123-M.jpg",
                fallback=(
                    "internetarchive",
                    "archive-id",
                    "https://archive.org/services/img/archive-id",
                    "",
                ),
            )

        self.assertEqual(ensure.call_count, 2)
        self.assertEqual(response.headers["X-LibFlix-Cover-Source"], "internetarchive")
        self.assertEqual(response.headers["X-LibFlix-Cover-Cache"], "HIT")

    def test_cover_mimetype_matches_unconverted_source_bytes(self):
        webp_path = os.path.join(self.tempdir.name, "inventaire.webp")
        with open(webp_path, "wb") as output:
            output.write(b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"x" * 32)

        with patch.object(app, "Image", None):
            self.assertEqual(app.cached_cover_mimetype(webp_path), "image/webp")

    def test_kindle_cover_reuses_archive_when_openlibrary_cover_fails(self):
        with (
            patch.object(app, "_cached_cover_variant", return_value=""),
            patch.object(
                app,
                "ensure_cover_cached",
                side_effect=[(None, False), ("archive-cover", False)],
            ) as ensure,
            patch.object(
                app,
                "_cover_file_as_jpeg",
                side_effect=lambda path: b"archive-jpeg" if path == "archive-cover" else b"",
            ),
        ):
            content = app._kindle_cover_bytes(
                "/olcover/123/L.webp?ia=archive-id",
            )

        self.assertEqual(content, b"archive-jpeg")
        self.assertEqual(
            [call.args[0] for call in ensure.call_args_list],
            ["openlibrary", "internetarchive"],
        )

    def test_warm_plan_covers_every_trending_book_and_possible_hero(self):
        shelves = [{
            "name": "Trending",
            "books": [
                {"cover_url": app.open_library_cover_url(1000 + index)}
                for index in range(20)
            ],
        }]

        jobs = app.cover_warm_jobs_for_shelves(shelves)

        self.assertEqual(jobs[:16], [(str(1000 + index), "L") for index in range(16)])
        self.assertEqual(jobs[16:], [(str(1000 + index), "M") for index in range(20)])

    def test_warm_marker_is_written_only_after_batch_finishes(self):
        marker = os.path.join(self.tempdir.name, ".warm-complete")
        marker_seen_during_warm = []

        def warm(*_args, **_kwargs):
            marker_seen_during_warm.append(os.path.exists(marker))
            return ("cover", False)

        with patch.object(app, "ensure_cover_cached", side_effect=warm):
            completed = app._run_cover_warm_batch([("123", "L")], marker)

        self.assertTrue(completed)
        self.assertEqual(marker_seen_during_warm, [False])
        self.assertTrue(os.path.exists(marker))

    def test_failed_warm_batch_does_not_write_completion_marker(self):
        marker = os.path.join(self.tempdir.name, ".warm-complete")
        with patch.object(app, "ensure_cover_cached", return_value=(None, False)):
            completed = app._run_cover_warm_batch([("missing", "L")], marker)

        self.assertFalse(completed)
        self.assertFalse(os.path.exists(marker))


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
    def test_description_repairs_joined_source_paragraphs(self):
        description = app.extract_desc({
            "description": "the interested lifeIn Rapt. attention.Gallagher explains."
        })

        self.assertEqual(
            description,
            "the interested life In Rapt. attention. Gallagher explains.",
        )

    def test_heavily_joined_work_description_falls_back_to_clean_edition(self):
        malformed = "lifeIn Rapt questionsCan we focus?driving onward.attention.Gallagher explains."
        edition_description = (
            "A clean edition summary explains how attention shapes the quality "
            "of an interested and fully lived life."
        )
        with (
            patch.object(app, "ol_get", return_value={
                "entries": [{"description": edition_description}],
            }),
            patch.object(app, "archive_description") as archive,
        ):
            description, complete = app.english_description_result(
                "/works/OL1932184W",
                {"description": malformed},
            )

        self.assertEqual(description, edition_description)
        self.assertTrue(complete)
        archive.assert_not_called()

    def test_malformed_legacy_detail_is_not_rendered_while_v6_refreshes(self):
        legacy = {
            "title": "Rapt",
            "author": "Winifred Gallagher",
            "description": "lifeIn Rapt questionsCan we focus?driving onward.attention.Gallagher explains.",
            "complete": True,
        }

        def memory_lookup(key, _ttl):
            return legacy if key == "book_detail:v5:en:OL1932184W" else None

        with (
            patch.object(app, "cache_get", side_effect=memory_lookup),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "disk_cache_get_stale", return_value=None),
        ):
            detail, state = app.cached_book_detail("OL1932184W", "en")

        self.assertEqual(state, "stale")
        self.assertEqual(detail["description"], "")
        self.assertFalse(detail["complete"])

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

    def test_search_description_is_used_before_edition_or_archive_requests(self):
        search_description = (
            "A clear description supplied by the Open Library search record "
            "when the canonical work record has no summary."
        )
        with patch.object(app, "ol_get") as upstream:
            description, complete = app.english_description_result(
                "/works/OL1932184W",
                {"title": "Rapt"},
                search_description,
            )

        self.assertEqual(description, search_description)
        self.assertTrue(complete)
        upstream.assert_not_called()

    def test_topic_hint_keeps_the_richest_description_for_fallback(self):
        rich_description = (
            "A substantive explanation of attention, concentration, and the "
            "practical conditions that make focused work possible."
        )
        with (
            patch.object(app, "BOOK_HINTS", {}),
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get_stale", return_value=None),
            patch.object(app, "alternate_canonical_book_detail", return_value={}),
        ):
            app.remember_book_hint({
                "ol_key": "/works/OL17713267W",
                "title": "Deep Work",
                "author": "Cal Newport",
                "description": rich_description,
            })
            app.remember_book_hint({
                "ol_key": "/works/OL17713267W",
                "title": "Deep Work",
                "author": "Cal Newport",
                "description": "Short note.",
            })

            detail = app.fallback_book_detail("OL17713267W", "en")

        self.assertEqual(detail["description"], rich_description)

    def test_topic_hint_supplies_missing_recommendation_subjects(self):
        cached = {
            "success": True,
            "title": "Mindfulness in Plain English",
            "author": "Henepola Gunaratana",
            "subjects": [],
            "similar_subjects": [],
            "complete": True,
        }
        with (
            patch.object(app, "BOOK_HINTS", {}),
            patch.object(app, "cached_book_detail", return_value=(cached, "memory")),
            patch.object(app, "alternate_canonical_book_detail", return_value={}),
        ):
            app.remember_book_hint({
                "ol_key": "/works/OL4305347W",
                "title": "Mindfulness in Plain English",
                "author": "Henepola Gunaratana",
                "subjects": ["meditation"],
            })

            detail, cache_state = app.get_book_detail("OL4305347W", "en")

        self.assertEqual(cache_state, "memory")
        self.assertEqual(detail["subjects"], ["meditation"])
        self.assertEqual(detail["similar_subjects"], ["meditation"])

    def test_incomplete_book_html_is_not_cached(self):
        detail = {
            "title": "Provisional title",
            "localized_title": "",
            "download_title": "Provisional title",
            "author": "Example Author",
            "cover_url": "",
            "description": "",
            "similar_subjects": [],
            "download_queries": ["Provisional title Example Author"],
            "complete": False,
        }
        with patch.object(app, "get_book_detail", return_value=(detail, "fallback")):
            response = app.app.test_client().get("/book/OL1W")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

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
