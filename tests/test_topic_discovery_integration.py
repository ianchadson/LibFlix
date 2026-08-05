import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import date
from unittest.mock import patch

import app
from topic_discovery import (
    DiscoveryCandidate,
    DiscoveryResult,
    ProviderPage,
    parse_openlibrary_payload,
    plan_topic_query,
)


class TopicIntegrationCacheTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_database = app.API_SQLITE_CACHE
        self.original_ready = app.SQLITE_CACHE_READY
        self.original_topic_corpus = (
            app.TOPIC_LOCAL_CORPUS_DATABASE,
            app.TOPIC_LOCAL_CORPUS_BUILT_AT,
            app.TOPIC_LOCAL_CORPUS_RECORDS,
        )
        app.API_SQLITE_CACHE = os.path.join(self.tempdir.name, "cache.sqlite3")
        app.SQLITE_CACHE_READY = False
        app.TOPIC_LOCAL_CORPUS_DATABASE = ""
        app.TOPIC_LOCAL_CORPUS_BUILT_AT = 0.0
        app.TOPIC_LOCAL_CORPUS_RECORDS = ()
        app.CACHE.clear()
        app.initialize_disk_cache()

    def tearDown(self):
        app.API_SQLITE_CACHE = self.original_database
        app.SQLITE_CACHE_READY = self.original_ready
        (
            app.TOPIC_LOCAL_CORPUS_DATABASE,
            app.TOPIC_LOCAL_CORPUS_BUILT_AT,
            app.TOPIC_LOCAL_CORPUS_RECORDS,
        ) = self.original_topic_corpus
        app.CACHE.clear()
        self.tempdir.cleanup()

    @staticmethod
    def books(count=40):
        return [{
            "title": f"Focus Book {index}",
            "author": f"Author {index}",
            "ol_key": f"/works/OL{index}W",
            "cover_url": f"/olcover/{1000 + index}/M.webp",
            "reasons": ["Subject: Attention"],
            "sources": ["openlibrary"],
        } for index in range(1, count + 1)]

    def payload(self, count=40, **overrides):
        payload = {
            "intent": "topic",
            "topic_mode": True,
            "display_query": "focus",
            "all_books": self.books(count),
            "partial": False,
            "sources": ["openlibrary", "inventaire"],
            "source_unavailable": False,
            "filters": dict(app.TOPIC_FILTER_DEFAULTS),
        }
        payload.update(overrides)
        return payload

    def test_topic_api_contract_has_start_here_explore_and_non_summed_total(self):
        payload = self.payload(40)
        with patch.object(app, "fetch_topic_discovery_payload", return_value=payload):
            response = app.app.test_client().get("/api/discover?q=focus")

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["topic_mode"])
        self.assertEqual(data["intent"], "topic")
        self.assertEqual(len(data["start_here"]), 6)
        self.assertEqual(len(data["books"]), 30)
        self.assertEqual(data["total"], 34)
        self.assertEqual(data["sources"], ["openlibrary", "inventaire"])
        self.assertFalse(
            {book["ol_key"] for book in data["start_here"]}
            & {book["ol_key"] for book in data["books"]}
        )

    def test_explicit_identity_override_preserves_literal_search_path(self):
        expected = ([{
            "title": "Focus",
            "author": "Arthur Miller",
            "ol_key": "/works/OL1W",
            "cover_url": "",
        }], 1, 1)
        with patch.object(app, "fetch_discovery_books", return_value=expected) as literal:
            response = app.app.test_client().get(
                "/api/discover?q=focus&intent=identity"
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["topic_mode"])
        literal.assert_called_once()

    def test_cold_topic_document_never_waits_for_providers(self):
        with (
            patch.object(app, "cached_topic_discovery_payload", return_value=None),
            patch.object(app, "fetch_topic_discovery_payload") as provider,
        ):
            response = app.app.test_client().get("/discover?q=focus")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Books about focus", response.data)
        self.assertIn(b"Finding books", response.data)
        provider.assert_not_called()

    def test_stale_topic_document_keeps_results_and_schedules_refresh(self):
        stale = self.payload(12, stale=True)
        with patch.object(
            app,
            "cached_topic_discovery_payload",
            return_value=stale,
        ):
            response = app.app.test_client().get("/discover?q=focus")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Focus Book 1", response.data)
        self.assertIn(b"const INITIAL_TOPIC_REFRESH = true;", response.data)
        self.assertIn(b"schedulePartialRefresh(initialTopicState);", response.data)

    def test_topic_cards_keep_metadata_visible_when_a_cover_is_missing(self):
        payload = self.payload(1)
        payload["all_books"][0]["cover_url"] = ""
        with patch.object(
            app,
            "cached_topic_discovery_payload",
            return_value=payload,
        ):
            response = app.app.test_client().get("/discover?q=focus")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="book-card no-cover"', response.data)
        self.assertIn(
            b".topic-book-item .book-card.no-cover .info { opacity: 1;",
            response.data,
        )
        self.assertIn(b"bookCard.classList.add('no-cover');", response.data)

    def test_stale_complete_beats_fresh_partial_outage(self):
        filters = dict(app.TOPIC_FILTER_DEFAULTS)
        key = app.topic_discovery_cache_key("focus", "en", filters)
        stale = self.payload(12)
        app.disk_cache_set(key, stale)
        cache_key = app.disk_cache_key(key)
        with closing(sqlite3.connect(app.API_SQLITE_CACHE)) as connection:
            with connection:
                connection.execute(
                    "UPDATE api_cache SET created_at = ? WHERE cache_key = ?",
                    (time.time() - app.TOPIC_MERGED_FRESH_TTL - 10, cache_key),
                )
        app.CACHE.clear()

        with patch.object(
            app,
            "_topic_provider_pages",
            return_value=([], [], True, False),
        ):
            result = app.fetch_topic_discovery_payload("focus", "en", filters)

        self.assertEqual(len(result["all_books"]), 12)
        self.assertTrue(result["stale"])
        self.assertTrue(result["refresh_partial"])
        self.assertFalse(result["partial"])

    def test_partial_empty_outage_is_not_cached(self):
        filters = dict(app.TOPIC_FILTER_DEFAULTS)
        with patch.object(
            app,
            "_topic_provider_pages",
            return_value=([], [], True, False),
        ):
            payload = app.fetch_topic_discovery_payload("focus", "en", filters)

        self.assertTrue(payload["source_unavailable"])
        key = app.topic_discovery_cache_key("focus", "en", filters)
        self.assertIsNone(app.disk_cache_entry(key))

    def test_valid_complete_empty_is_cacheable(self):
        filters = dict(app.TOPIC_FILTER_DEFAULTS)
        page = ProviderPage("openlibrary", "unknown", 0, (), available=True)
        with patch.object(
            app,
            "_topic_provider_pages",
            return_value=([page], ["openlibrary"], False, True),
        ):
            payload = app.fetch_topic_discovery_payload(
                "books about obscure topic",
                "en",
                filters,
            )

        self.assertEqual(payload["all_books"], [])
        self.assertFalse(payload["source_unavailable"])
        key = app.topic_discovery_cache_key(
            "books about obscure topic", "en", filters
        )
        self.assertIsNotNone(app.disk_cache_entry(key))

    def test_healthy_supplemental_provider_empty_is_partial_not_outage(self):
        filters = dict(app.TOPIC_FILTER_DEFAULTS)
        inventaire = ProviderPage(
            "inventaire",
            "meditation",
            0,
            (),
            available=True,
        )
        with patch.object(
            app,
            "_topic_provider_pages",
            return_value=([inventaire], ["inventaire"], True, False),
        ):
            payload = app.fetch_topic_discovery_payload(
                "meditation",
                "en",
                filters,
            )

        self.assertTrue(payload["partial"])
        self.assertFalse(payload["source_unavailable"])
        self.assertEqual(payload["all_books"], [])

    def test_durable_openlibrary_corpus_rescues_a_provider_outage(self):
        app.disk_cache_set("test-openlibrary-search", {"docs": [{
            "key": "/works/OL77W",
            "title": "Deep Focus",
            "author_name": ["Test Author"],
            "language": ["eng"],
            "subject": ["Focus", "Attention"],
            "cover_i": 700,
            "readinglog_count": 400,
        }]})
        with patch.object(
            app,
            "_topic_provider_pages",
            return_value=([], [], True, False),
        ):
            payload = app.fetch_topic_discovery_payload(
                "focus",
                "en",
                dict(app.TOPIC_FILTER_DEFAULTS),
            )

        self.assertTrue(payload["partial"])
        self.assertTrue(payload["cache_fallback"])
        self.assertFalse(payload["source_unavailable"])
        self.assertEqual(payload["all_books"][0]["ol_key"], "/works/OL77W")
        self.assertEqual(payload["all_books"][0]["cover_url"], "/olcover/700/M.webp")

    def test_filtered_out_cache_does_not_shorten_live_provider_wait(self):
        app.disk_cache_set("test-openlibrary-search", {"docs": [{
            "key": f"/works/OL{index}W",
            "title": f"Focus Archive {index}",
            "author_name": [f"Author {index}"],
            "language": ["eng"],
            "subject": ["Focus"],
        } for index in range(1, 6)]})
        provider = patch.object(
            app,
            "_topic_provider_pages",
            return_value=([], [], True, False),
        )
        with provider as mocked_provider:
            app.fetch_topic_discovery_payload(
                "focus",
                "en",
                {
                    **app.TOPIC_FILTER_DEFAULTS,
                    "published": "recent",
                },
            )

        self.assertFalse(mocked_provider.call_args.kwargs["fallback_ready"])

    def test_filtered_out_cache_is_not_reported_as_visible_fallback(self):
        app.disk_cache_set("test-openlibrary-search", {"docs": [{
            "key": "/works/OL77W",
            "title": "Focus Without a Date",
            "author_name": ["Cached Author"],
            "language": ["eng"],
            "subject": ["Focus"],
        }]})
        plan = plan_topic_query("focus", "topic")
        live_page = parse_openlibrary_payload({"docs": [{
            "key": "/works/OL99W",
            "title": "Recent Focus",
            "author_name": ["Live Author"],
            "language": ["eng"],
            "subject": ["Focus"],
            "first_publish_year": date.today().year,
        }]}, plan.queries[0], 0)
        with patch.object(
            app,
            "_topic_provider_pages",
            return_value=([live_page], ["openlibrary"], True, False),
        ):
            payload = app.fetch_topic_discovery_payload(
                "focus",
                "en",
                {
                    **app.TOPIC_FILTER_DEFAULTS,
                    "published": "recent",
                },
            )

        self.assertFalse(payload["cache_fallback"])
        self.assertEqual(
            [book["title"] for book in payload["all_books"]],
            ["Recent Focus"],
        )

    def test_inventaire_only_partial_does_not_reweight_complete_live_ol_pages(self):
        app.disk_cache_set("test-openlibrary-search", {"docs": [{
            "key": "/works/OL88W",
            "title": "Cached Focus",
            "author_name": ["Cached Author"],
            "language": ["eng"],
            "subject": ["Focus"],
        }]})
        plan = plan_topic_query("focus", "topic")
        live_pages = [
            parse_openlibrary_payload({"docs": [{
                "key": "/works/OL99W",
                "title": "Live Focus",
                "author_name": ["Live Author"],
                "language": ["eng"],
                "subject": ["Focus", "Attention", "Deep Work"],
            }]}, query, rank)
            for rank, query in enumerate(plan.queries)
        ]
        with patch.object(
            app,
            "_topic_provider_pages",
            return_value=(live_pages, ["openlibrary"], True, True),
        ):
            payload = app.fetch_topic_discovery_payload(
                "focus",
                "en",
                dict(app.TOPIC_FILTER_DEFAULTS),
            )

        self.assertFalse(payload["cache_fallback"])
        self.assertEqual(
            [book["title"] for book in payload["all_books"]],
            ["Live Focus"],
        )

    def test_local_corpus_term_matching_never_uses_raw_substrings(self):
        self.assertFalse(app._topic_cached_text_contains("chair design", "ai"))
        self.assertTrue(app._topic_cached_text_contains("the age of ai", "ai"))

    def test_local_corpus_rejects_weak_matches_in_noisy_subject_lists(self):
        plan = plan_topic_query("productivity", "topic")
        filler = [f"Unrelated subject {index}" for index in range(30)]
        weak = {
            "title": "Contract Pricing",
            "subject": ["Software Productivity Consortium", *filler],
        }
        polluted = {
            "title": "Race and Ethnicity in Society",
            "subject": ["Productivity", *filler, *(
                f"Other domain {index}" for index in range(40)
            )],
            "readinglog_count": 500,
        }
        supported = {
            "title": "The Productivity Project",
            "subject": ["Productivity", *filler],
        }
        established = {
            "title": "Better Work",
            "subject": ["Productivity", *filler],
            "readinglog_count": 500,
        }

        self.assertFalse(app._topic_cached_record_is_coherent(weak, plan))
        self.assertFalse(app._topic_cached_record_is_coherent(polluted, plan))
        self.assertTrue(app._topic_cached_record_is_coherent(supported, plan))
        self.assertTrue(app._topic_cached_record_is_coherent(established, plan))

    def test_sparse_local_corpus_uses_bounded_title_evidence(self):
        productivity = plan_topic_query("productivity", "topic")
        sleep = plan_topic_query("sleep", "topic")
        focus = plan_topic_query("focus", "topic")
        parenting = plan_topic_query("parenting", "topic")

        # The corpus gate admits the primary term; topic-specific semantic
        # ranking rejects the petroleum sense after parsing.
        self.assertTrue(app._topic_cached_record_is_coherent(
            {"title": "Well Productivity Handbook"},
            productivity,
        ))
        self.assertFalse(app._topic_cached_record_is_coherent(
            {"title": "Insomnia"},
            sleep,
        ))
        self.assertTrue(app._topic_cached_record_is_coherent(
            {"title": "Deep Work"},
            focus,
        ))
        self.assertTrue(app._topic_cached_record_is_coherent(
            {"title": "Parenting the New Teen"},
            parenting,
        ))

    def test_sparse_local_corpus_restores_primary_and_safe_sales_titles(self):
        focus = plan_topic_query("focus", "topic")
        meditation = plan_topic_query("meditation", "topic")
        sales = plan_topic_query("sales", "topic")

        self.assertTrue(app._topic_cached_record_is_coherent(
            {"title": "The Power of Focus"},
            focus,
        ))
        self.assertTrue(app._topic_cached_record_is_coherent(
            {"title": "The Meditation Handbook"},
            meditation,
        ))
        self.assertTrue(app._topic_cached_record_is_coherent(
            {"title": "The Little Red Book of Selling"},
            sales,
        ))

    def test_local_corpus_drops_incidental_topic_in_large_subject_list(self):
        science = plan_topic_query("science", "topic")
        record = {
            "title": "Drive",
            "subject": ["Science", *(
                f"Unrelated domain {index}" for index in range(19)
            )],
        }

        self.assertFalse(app._topic_cached_record_is_coherent(record, science))

    def test_local_productivity_corpus_keeps_personal_work_signals(self):
        plan = plan_topic_query("productivity", "topic")
        zapp = {
            "title": "Zapp!",
            "subject": [
                "Productivity", "Employee empowerment", "Motivation",
                *(
                    f"Workplace subject {index}" for index in range(20)
                ),
            ],
        }
        first_things_first = {
            "title": "First Things First",
            "subject": [
                "Time management", "Conduct of life", "Goals",
                *(
                    f"Workplace subject {index}" for index in range(20)
                ),
            ],
        }
        industrial = {
            "title": "MOST Work Measurement Systems",
            "subject": [
                "Productivity", "Work measurement", "Industrial engineering",
                *(
                    f"Engineering subject {index}" for index in range(20)
                ),
            ],
        }

        self.assertTrue(app._topic_cached_record_is_coherent(zapp, plan))
        self.assertTrue(app._topic_cached_record_is_coherent(
            first_things_first,
            plan,
        ))
        self.assertFalse(app._topic_cached_record_is_coherent(industrial, plan))

    def test_pagination_is_stable_and_duplicate_free(self):
        payload = self.payload(70)
        first = app.paginate_topic_discovery_payload(payload, 1)
        second = app.paginate_topic_discovery_payload(payload, 2)
        first_keys = {
            book["ol_key"] for book in first["start_here"] + first["books"]
        }
        second_keys = {book["ol_key"] for book in second["books"]}
        self.assertFalse(first_keys & second_keys)
        self.assertEqual((first["total_pages"], second["total_pages"]), (3, 3))
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])

    def test_changed_snapshot_rejects_mixed_page_two(self):
        payload = self.payload(40, snapshot_id="current-snapshot")
        with patch.object(
            app,
            "cached_topic_discovery_payload",
            return_value=payload,
        ):
            response = app.app.test_client().get(
                "/api/discover?q=focus&intent=topic&page=2&snapshot=old-snapshot"
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "snapshot_changed")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_source_unavailable_api_is_no_store(self):
        payload = self.payload(
            0,
            partial=True,
            source_unavailable=True,
            sources=[],
        )
        with patch.object(app, "fetch_topic_discovery_payload", return_value=payload):
            response = app.app.test_client().get("/api/discover?q=focus")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "source_unavailable")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_usable_partial_api_is_also_no_store(self):
        payload = self.payload(12, partial=True, sources=["openlibrary"])
        with patch.object(app, "fetch_topic_discovery_payload", return_value=payload):
            response = app.app.test_client().get("/api/discover?q=focus")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["partial"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_page_two_requires_an_existing_stable_snapshot(self):
        with (
            patch.object(app, "cached_topic_discovery_payload", return_value=None),
            patch.object(app, "fetch_topic_discovery_payload") as provider,
        ):
            response = app.app.test_client().get(
                "/api/discover?q=focus&intent=topic&page=2"
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "snapshot_unavailable")
        provider.assert_not_called()

    def test_malformed_openlibrary_search_page_is_unavailable(self):
        plan = plan_topic_query("focus", "topic")
        with patch.object(app, "ol_get", return_value={"numFound": 10}):
            pages = app._topic_openlibrary_page(
                plan,
                0,
                "en",
                dict(app.TOPIC_FILTER_DEFAULTS),
            )

        self.assertFalse(pages[0].available)

    def test_failed_inventaire_entity_hydration_marks_page_partial(self):
        plan = plan_topic_query("focus", "topic")
        search = {"results": [{"uri": "wd:Q1", "label": "Deep Work"}]}
        with patch.object(app, "inventaire_get", side_effect=[search, None]):
            pages = app._topic_inventaire_pages(
                plan,
                len(plan.queries) - 1,
                "en",
                dict(app.TOPIC_FILTER_DEFAULTS),
            )

        self.assertTrue(any(not page.available for page in pages))

    def test_sources_only_name_providers_that_contributed_results(self):
        plan = plan_topic_query("focus", "topic")
        openlibrary = parse_openlibrary_payload({"docs": [{
            "key": "/works/OL1W",
            "title": "Focus",
            "author_name": ["Author"],
            "language": ["eng"],
            "subject": ["Focus"],
        }]}, "focus")
        inventaire = ProviderPage("inventaire", "focus", 0, (), available=True)
        with patch.object(
            app,
            "_topic_provider_pages",
            return_value=([openlibrary, inventaire], ["inventaire", "openlibrary"], False, True),
        ):
            payload = app.fetch_topic_discovery_payload(
                plan.raw_query,
                "en",
                dict(app.TOPIC_FILTER_DEFAULTS),
            )

        self.assertEqual(payload["sources"], ["openlibrary"])

    def test_supplemental_unknown_language_policy_uses_effective_filter(self):
        result = DiscoveryResult(
            candidate=DiscoveryCandidate(
                provider="inventaire",
                provider_id="wd:Q1",
                native_rank=1,
                query_rank=0,
                title="Mapped Work",
                authors=("Author",),
                work_key="/works/OL1W",
                languages=(),
            ),
            score=1,
            reasons=("Related: Focus",),
            sources=("inventaire",),
        )

        self.assertEqual(
            len(app._topic_books_from_results([result], "en", "current")),
            1,
        )
        self.assertEqual(
            app._topic_books_from_results([result], "cn", "current"),
            [],
        )
        explicitly_english = app.filter_topic_results(
            [result],
            language="en",
            current_language="cn",
        )
        self.assertEqual(explicitly_english, [])

    def test_mapped_inventaire_work_without_author_still_renders(self):
        result = DiscoveryResult(
            candidate=DiscoveryCandidate(
                provider="inventaire",
                provider_id="wd:Q2",
                native_rank=1,
                query_rank=0,
                title="Mindfulness in Plain English",
                authors=(),
                work_key="/works/OL2W",
                semantic_terms=("meditation",),
            ),
            score=1,
            reasons=("Related: Meditation",),
            sources=("inventaire",),
        )

        books = app._topic_books_from_results([result], "en", "current")

        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["author"], "")

    def test_topic_book_hint_is_stored_under_the_active_language(self):
        result = DiscoveryResult(
            candidate=DiscoveryCandidate(
                provider="openlibrary",
                provider_id="OL9W",
                native_rank=1,
                query_rank=0,
                title="专注力",
                authors=("作者",),
                work_key="/works/OL9W",
                languages=("chi",),
            ),
            score=1,
            reasons=("主题：注意力",),
            sources=("openlibrary",),
        )
        with app.BOOK_HINTS_LOCK:
            app.BOOK_HINTS.clear()

        app._topic_books_from_results([result], "cn", "current")

        with app.BOOK_HINTS_LOCK:
            self.assertIn(("cn", "/works/OL9W"), app.BOOK_HINTS)
            self.assertNotIn(("en", "/works/OL9W"), app.BOOK_HINTS)

    def test_partial_payload_exposes_circuit_retry_after(self):
        with (
            patch.object(
                app,
                "_topic_provider_pages",
                return_value=([], [], True, False),
            ),
            patch.object(app, "openlibrary_status", return_value={"retry_after": 61}),
            patch.object(app, "inventaire_status", return_value={"retry_after": 20}),
        ):
            payload = app.fetch_topic_discovery_payload(
                "focus",
                "en",
                dict(app.TOPIC_FILTER_DEFAULTS),
            )

        self.assertEqual(payload["retry_after"], 61)

    def test_partial_payload_waits_past_inflight_provider_timeout(self):
        with (
            patch.object(
                app,
                "_topic_provider_pages",
                return_value=([], [], True, False),
            ),
            patch.object(app, "openlibrary_status", return_value={"retry_after": 0}),
            patch.object(app, "inventaire_status", return_value={"retry_after": 0}),
        ):
            payload = app.fetch_topic_discovery_payload(
                "focus",
                "en",
                dict(app.TOPIC_FILTER_DEFAULTS),
            )

        self.assertGreaterEqual(
            payload["retry_after"],
            app.OL_CONNECT_TIMEOUT + app.OL_READ_TIMEOUT,
        )

    def test_invalid_filter_values_fall_back_to_bounded_defaults(self):
        filters = app.normalize_topic_filters({
            "type": "all);DROP TABLE",
            "language": "klingon",
            "published": "tomorrow",
            "sort": "random",
        })
        self.assertEqual(filters, app.TOPIC_FILTER_DEFAULTS)


if __name__ == "__main__":
    unittest.main()
