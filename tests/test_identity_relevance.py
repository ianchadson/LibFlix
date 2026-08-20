import json
import unittest
from unittest.mock import patch

import app
from downloaders.base import Book


class BookIdentityTests(unittest.TestCase):
    def test_open_library_identity_preserves_titles_authors_and_isbns(self):
        identity = app.collect_book_identity_metadata(
            {"title": "The Age of AI", "download_title": "The Age of AI"},
            work={"title": "The Age of A.I.", "other_titles": ["The Age of AI"]},
            search_record={
                "title": "The Age of AI",
                "alternative_title": ["Our Human Future"],
                "author_name": [
                    "Henry Kissinger",
                    "Eric Schmidt",
                    "Daniel Huttenlocher",
                ],
                "isbn": ["9780316273800"],
                "editions": {
                    "docs": [{
                        "title": "The Age of AI and Our Human Future",
                        "isbn_10": ["0316273805"],
                    }]
                },
            },
        )

        self.assertIn("The Age of A.I.", identity["title_aliases"])
        self.assertIn("The Age of AI and Our Human Future", identity["title_aliases"])
        self.assertEqual(identity["authors"], [
            "Henry Kissinger",
            "Eric Schmidt",
            "Daniel Huttenlocher",
        ])
        self.assertEqual(identity["isbns"], ["9780316273800", "0316273805"])

        queries = app.english_download_queries({
            "title": "The Age of AI",
            "download_title": "The Age of AI",
            **identity,
        })
        self.assertEqual(queries[0], "The Age of AI Henry Kissinger")
        self.assertIn("9780316273800", queries[:4])

    def test_download_search_merges_aliases_before_accepting_weak_result(self):
        weak_pdf = Book(
            book_id="a" * 32,
            title="The Age of A.I.",
            author="Henry Kissinger",
            language="English",
            ext="pdf",
            size="8 MB",
        )
        strong_epub = Book(
            book_id="b" * 32,
            title="The Age of AI",
            author="Henry Kissinger; Eric Schmidt; Daniel Huttenlocher",
            language="English",
            ext="epub",
            size="1.2 MB",
        )

        def search(query, **_kwargs):
            if query == "The Age of AI Henry Kissinger":
                return [strong_epub], 1
            return [weak_pdf], 1

        with (
            patch.object(app.DOWNLOADER, "search", side_effect=search) as downloader_search,
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set"),
            patch.object(app, "disk_cache_set"),
        ):
            response = app.app.test_client().get("/api/search", query_string={
                "q": "The Age of A.I.",
                "search_aliases": json.dumps(["The Age of AI Henry Kissinger"]),
                "target_title": "The Age of A.I.",
                "target_title_aliases": json.dumps(["The Age of AI"]),
                "target_author": "Henry Kissinger",
                "target_author_aliases": json.dumps([
                    "Eric Schmidt", "Daniel Huttenlocher",
                ]),
                "lang": "English",
                "dedup": "0",
            })

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(downloader_search.call_count, 2)
        self.assertEqual(payload["books"][0]["md5"], strong_epub.book_id)
        self.assertTrue(payload["books"][0]["best_match"])
        self.assertEqual(payload["searched_queries"], [
            "The Age of A.I.", "The Age of AI Henry Kissinger",
        ])

    def test_download_alias_search_is_strictly_bounded(self):
        with patch.object(app.DOWNLOADER, "search", return_value=([], 0)) as search:
            outcome = app.search_download_aliases(
                [f"Alias {index}" for index in range(10)],
                sort="y",
                order="DESC",
                page=1,
                limit=25,
            )

        self.assertEqual((outcome.books, outcome.total), ([], 0))
        self.assertTrue(outcome.complete)
        self.assertEqual(search.call_count, app.DOWNLOAD_ALIAS_SEARCH_LIMIT)

    def test_weak_first_batch_continues_until_later_high_confidence_epub(self):
        weak_pdf = Book(
            book_id="a" * 32,
            title="The Age of AI",
            author="Henry Kissinger",
            language="English",
            ext="pdf",
            size="8 MB",
        )
        strong_epub = Book(
            book_id="b" * 32,
            title="The Age of AI",
            author="Henry Kissinger",
            language="English",
            ext="epub",
            size="1 MB",
        )
        queries = [
            "The Age of AI Henry Kissinger",
            "The Age of AI",
            "The Age of AI and Our Human Future Henry Kissinger",
            "9780316273800",
            "Our Human Future",
            "0316273805",
        ]

        def search(query, **_kwargs):
            if query == queries[0]:
                return [weak_pdf], 1
            if query == queries[-1]:
                return [strong_epub], 1
            return [], 0

        identity = {
            "title": "The Age of AI",
            "author": "Henry Kissinger",
            "title_aliases": ["The Age of AI", "Our Human Future"],
            "authors": ["Henry Kissinger"],
            "download_queries": queries,
        }
        with (
            patch.object(app, "server_download_identity", return_value=identity),
            patch.object(app.DOWNLOADER, "search", side_effect=search) as downloader_search,
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set"),
            patch.object(app, "disk_cache_set"),
        ):
            response = app.app.test_client().get("/api/search", query_string={
                "q": queries[0],
                "ol_key": "/works/OL1W",
                "target_title": "The Age of AI",
                "target_author": "Henry Kissinger",
                "lang": "English",
                "dedup": "0",
            })

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(downloader_search.call_count, app.DOWNLOAD_ALIAS_SEARCH_LIMIT)
        self.assertEqual(payload["books"][0]["md5"], strong_epub.book_id)
        self.assertEqual(payload["searched_queries"], queries)
        self.assertEqual(payload["total_pages"], 1)

    def test_high_confidence_first_batch_avoids_remaining_alias_calls(self):
        epub = Book(
            book_id="a" * 32,
            title="The Age of AI",
            author="Henry Kissinger",
            language="English",
            ext="epub",
            size="1 MB",
        )
        identity = {
            "title": "The Age of AI",
            "author": "Henry Kissinger",
            "title_aliases": ["The Age of AI"],
            "authors": ["Henry Kissinger"],
            "download_queries": [
                "The Age of AI Henry Kissinger", "The Age of AI", "9780316273800",
            ],
        }
        with (
            patch.object(app, "server_download_identity", return_value=identity),
            patch.object(app.DOWNLOADER, "search", return_value=([epub], 1)) as search,
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set"),
            patch.object(app, "disk_cache_set"),
        ):
            response = app.app.test_client().get("/api/search", query_string={
                "q": "The Age of AI Henry Kissinger",
                "ol_key": "/works/OL1W",
                "target_title": "The Age of AI",
                "target_author": "Henry Kissinger",
                "lang": "English",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(search.call_count, app.DOWNLOAD_ALIAS_BATCH_SIZE)

    def test_partial_empty_alias_batch_uses_error_path_and_is_not_cached(self):
        identity = {
            "title": "The Age of AI",
            "author": "Henry Kissinger",
            "download_queries": ["The Age of AI", "9780316273800"],
        }

        def search(query, **_kwargs):
            if query == "The Age of AI":
                return [], 0
            raise app.requests.Timeout("ISBN lookup timed out")

        with (
            patch.object(app, "server_download_identity", return_value=identity),
            patch.object(app.DOWNLOADER, "search", side_effect=search),
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set") as memory_set,
            patch.object(app, "disk_cache_set") as disk_set,
        ):
            response = app.app.test_client().get("/api/search", query_string={
                "q": "The Age of AI",
                "ol_key": "/works/OL1W",
                "target_title": "The Age of AI",
                "target_author": "Henry Kissinger",
                "lang": "English",
            })

        self.assertEqual(response.status_code, 504)
        memory_set.assert_not_called()
        disk_set.assert_not_called()

    def test_partial_nonempty_alias_result_is_served_but_not_cached(self):
        weak_pdf = Book(
            book_id="a" * 32,
            title="The Age of AI",
            author="Henry Kissinger",
            language="English",
            ext="pdf",
            size="8 MB",
        )
        identity = {
            "title": "The Age of AI",
            "author": "Henry Kissinger",
            "download_queries": ["The Age of AI", "9780316273800"],
        }

        def search(query, **_kwargs):
            if query == "The Age of AI":
                return [weak_pdf], 1
            raise app.requests.Timeout("ISBN lookup timed out")

        with (
            patch.object(app, "server_download_identity", return_value=identity),
            patch.object(app.DOWNLOADER, "search", side_effect=search),
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set") as memory_set,
            patch.object(app, "disk_cache_set") as disk_set,
        ):
            response = app.app.test_client().get("/api/search", query_string={
                "q": "The Age of AI",
                "ol_key": "/works/OL1W",
                "target_title": "The Age of AI",
                "target_author": "Henry Kissinger",
                "lang": "English",
            })

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["books"][0]["md5"], weak_pdf.book_id)
        memory_set.assert_not_called()
        disk_set.assert_not_called()

    def test_identity_json_input_has_a_strict_byte_cap(self):
        oversized = json.dumps(["书" * 180 for _ in range(12)], ensure_ascii=False)

        self.assertEqual(app.parse_bounded_json_list(oversized), [])


class DiscoveryRelevanceTests(unittest.TestCase):
    def test_discovery_drops_unrelated_provider_filler(self):
        records = [
            {
                "key": "/works/OL1W",
                "title": "The Age of AI",
                "author_name": ["Henry Kissinger"],
            },
            {
                "key": "/works/OL2W",
                "title": "AI Superpowers",
                "author_name": ["Kai-Fu Lee"],
            },
            {
                "key": "/works/OL3W",
                "title": "The Age of Innocence",
                "author_name": ["Edith Wharton"],
            },
            {
                "key": "/works/OL4W",
                "title": "Hamlet",
                "author_name": ["William Shakespeare"],
            },
            {
                "key": "/works/OL5W",
                "title": "The Hobbit",
                "author_name": ["J. R. R. Tolkien"],
            },
        ]

        ranked = app.rank_discovery_records(records, "the age of ai")

        self.assertEqual(
            [record["key"] for record in ranked],
            ["/works/OL1W"],
        )

    def test_discovery_accepts_exact_isbn_and_safe_title_typo(self):
        record = {
            "key": "/works/OL1W",
            "title": "The Age of AI",
            "author_name": ["Henry Kissinger"],
            "isbn": ["978-0-316-27380-0"],
        }

        self.assertGreater(app.discovery_record_relevance(record, "9780316273800"), 0)
        self.assertGreater(app.discovery_record_relevance(record, "the age of al"), 0)
        self.assertGreater(
            app.discovery_record_relevance(record, "age artificial intelligence"),
            0,
        )
        edition_only = {
            "key": "/works/OL2W",
            "title": "Identifier edition",
            "author_name": ["Author"],
            "editions": {"docs": [{"isbn_10": ["0316273805"]}]},
        }
        self.assertGreater(
            app.discovery_record_relevance(edition_only, "0316273805"),
            0,
        )


class SimilarBookRelevanceTests(unittest.TestCase):
    @staticmethod
    def record(key, title, author, cover):
        return {
            "key": key,
            "title": title,
            "author_name": [author],
            "language": ["eng"],
            "cover_i": cover,
        }

    def test_subject_selection_prefers_specific_topics_over_catalog_qualifiers(self):
        subjects = [
            "Businesspeople, juvenile literature",
            "Business, juvenile literature",
            "Business, biography",
            "nyt:business-books=2016-05-08",
            "Biography",
            "Sporting goods industry",
            "Nike (Firm)",
        ]

        self.assertEqual(
            app.similar_subject_candidates(subjects),
            ["Sporting goods industry", "Nike (Firm)"],
        )
        self.assertEqual(
            app.similar_subject_candidates([
                "New York Times bestseller",
                "United states, navy, seals",
                "United states, air force",
                "Triathlon",
                "Endurance sports",
                "Motivation (psychology)",
                "Self-realization",
            ]),
            ["Endurance sports", "Motivation (psychology)"],
        )
        self.assertEqual(
            app.similar_subject_candidates([
                "TECHNOLOGY & ENGINEERING / General",
                "BIOGRAPHY & AUTOBIOGRAPHY / Business",
                "Inc Apple Computer",
                "Computer engineers",
            ]),
            ["Inc Apple Computer", "Computer engineers"],
        )

    def test_cached_detail_recomputes_recommendation_subjects(self):
        cached = {
            "title": "Can't Hurt Me",
            "author": "David Goggins",
            "subjects": [
                "New York Times bestseller",
                "United states, navy, seals",
                "Mental endurance",
            ],
            "similar_subjects": ["New York Times bestseller"],
            "cover_url": "",
            "complete": True,
        }

        with (
            patch.object(app, "cached_book_detail", return_value=(cached, "memory")),
            patch.object(app, "alternate_canonical_book_detail", return_value={}),
        ):
            detail, cache_state = app.get_book_detail("OL18108064W", "en")

        self.assertEqual(cache_state, "memory")
        self.assertEqual(
            detail["similar_subjects"],
            ["Mental endurance", "United states, navy, seals"],
        )

    def test_similar_books_require_shared_context_or_same_author(self):
        shared = self.record(
            "/works/OL2W", "The Innovators", "Walter Isaacson", 2
        )
        shared["subject"] = ["Computer engineers", "Technology executives"]
        same_author = self.record(
            "/works/OL3W", "Einstein", "Walter Isaacson", 3
        )
        tangential = self.record(
            "/works/OL4W", "Jurassic Park", "Michael Crichton", 4
        )

        def open_library(_path, params):
            query = params["q"]
            if query.startswith('subject:"Computer engineers"'):
                return {"docs": [shared, tangential]}
            if query.startswith('subject:"Technology executives"'):
                return {"docs": [shared]}
            if query.startswith('author:"Walter Isaacson"'):
                return {"docs": [same_author, shared]}
            return {"docs": []}

        with patch.object(app, "ol_get", side_effect=open_library) as ol_get:
            books = app.build_similar_books(
                "/works/OL1W",
                ["Computer engineers", "Technology executives"],
                "en",
                current_title="Steve Jobs",
                current_authors=["Walter Isaacson"],
            )

        self.assertEqual(ol_get.call_count, app.SIMILAR_MAX_ORIGIN_QUERIES)
        self.assertEqual(
            [book["ol_key"] for book in books],
            ["/works/OL2W", "/works/OL3W"],
        )

    def test_author_only_similar_books_return_same_author(self):
        same_author = self.record(
            "/works/OL2W", "Klara and the Sun", "Kazuo Ishiguro", 2
        )

        with patch.object(
            app,
            "ol_get",
            return_value={"docs": [same_author]},
        ) as ol_get:
            books = app.build_similar_books(
                "/works/OL1W",
                [],
                "en",
                current_title="Never Let Me Go",
                current_authors=["Kazuo Ishiguro"],
            )

        self.assertEqual(ol_get.call_count, 1)
        self.assertEqual(
            ol_get.call_args.args[1]["q"],
            'author:"Kazuo Ishiguro" language:eng',
        )
        self.assertEqual([book["ol_key"] for book in books], ["/works/OL2W"])

    def test_inventaire_author_fallback_keeps_only_mapped_same_author_works(self):
        page = app.parse_inventaire_payload({"results": [
            {
                "uri": "inv:" + "a" * 32,
                "label": "Mindfulness in Plain English",
                "authors": ["Henepola Gunaratana"],
                "claims": {
                    "wdt:P648": ["OL4305347W"],
                    "wdt:P407": ["wd:Q1860"],
                },
            },
            {
                "uri": "inv:" + "b" * 32,
                "label": "Eight Mindful Steps to Happiness",
                "authors": ["Henepola Gunaratana"],
                "image": {"url": "/img/entities/" + "c" * 40},
                "claims": {
                    "wdt:P648": ["OL4305349W"],
                    "wdt:P407": ["wd:Q1860"],
                },
            },
            {
                "uri": "inv:" + "d" * 32,
                "label": "Unrelated Work",
                "authors": ["Another Author"],
                "claims": {
                    "wdt:P648": ["OL9W"],
                    "wdt:P407": ["wd:Q1860"],
                },
            },
        ]}, "Henepola Gunaratana")

        with patch.object(app, "_topic_inventaire_pages", return_value=[page]):
            books, complete = app.fetch_inventaire_similar_books(
                "/works/OL4305347W",
                [],
                "en",
                "Mindfulness in Plain English",
                ["Henepola Gunaratana"],
            )

        self.assertTrue(complete)
        self.assertEqual([book["ol_key"] for book in books], ["/works/OL4305349W"])
        self.assertEqual(books[0]["reason"], "Same author")
        self.assertTrue(books[0]["cover_url"].startswith("/invcover/"))

    def test_similar_books_use_inventaire_when_open_library_is_unavailable(self):
        fallback = {
            "title": "Eight Mindful Steps to Happiness",
            "author": "Henepola Gunaratana",
            "ol_key": "/works/OL4305349W",
            "cover_url": "/invcover/" + "c" * 40 + "/M.webp",
        }
        with (
            patch.object(app, "ol_get", return_value=None),
            patch.object(
                app,
                "fetch_inventaire_similar_books",
                return_value=([fallback], True),
            ) as inventaire,
        ):
            books, complete = app.build_similar_books(
                "/works/OL4305347W",
                [],
                "en",
                current_title="Mindfulness in Plain English",
                current_authors=["Henepola Gunaratana"],
                with_status=True,
            )

        self.assertFalse(complete)
        self.assertEqual(books, [fallback])
        inventaire.assert_called_once()

    def test_api_similar_accepts_author_only_seed(self):
        with (
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "disk_cache_get_stale", return_value=None),
            patch.object(app, "schedule_similar_refresh", return_value=True) as refresh,
        ):
            response = app.app.test_client().get("/api/similar", query_string={
                "ol_key": "/works/OL1W",
                "book_lang": "en",
                "title": "Never Let Me Go",
                "author": "Kazuo Ishiguro",
            })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["refreshing"])
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[2], [])
        self.assertEqual(refresh.call_args.args[5], ["Kazuo Ishiguro"])

    def test_api_similar_serves_local_results_while_remote_refreshes(self):
        local_book = {
            "title": "A Related Book",
            "author": "Example Author",
            "ol_key": "/works/OL2W",
            "cover_url": "",
        }
        with (
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "cache_set"),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "disk_cache_get_stale", return_value=None),
            patch.object(app, "local_similar_books", return_value=[local_book]) as local,
            patch.object(app, "schedule_similar_refresh", return_value=True) as refresh,
        ):
            response = app.app.test_client().get("/api/similar", query_string={
                "ol_key": "/works/OL1W",
                "book_lang": "en",
                "title": "Current Book",
                "author": "Example Author",
                "subject": "Productivity",
            })

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["partial"])
        self.assertTrue(payload["refreshing"])
        self.assertEqual(payload["books"], [local_book])
        local.assert_called_once()
        refresh.assert_called_once()

    def test_similar_cache_key_uses_current_relevance_version(self):
        cache_key = app.similar_cache_key(
            "/works/OL1W",
            ["Artificial intelligence"],
            "en",
            "The Age of AI",
            ["Henry Kissinger"],
        )

        self.assertTrue(cache_key.startswith("similar:v8:"))

    def test_confirmed_single_subject_candidates_backfill_after_strict_tier(self):
        strict = self.record(
            "/works/OL2W", "Strict intersection", "Different Author", 2
        )
        strict["subject"] = [
            "Artificial intelligence",
            "Technology and society",
        ]
        confirmed = self.record(
            "/works/OL3W", "Machine Learning Foundations", "Another Author", 3
        )
        confirmed["subject"] = ["Artificial intelligence", "Computers"]
        unconfirmed = self.record(
            "/works/OL4W", "Unrelated Provider Result", "Another Author", 4
        )

        def open_library(_path, params):
            query = params["q"]
            if query.startswith('subject:"Artificial intelligence"'):
                return {"docs": [strict, confirmed, unconfirmed]}
            if query.startswith('subject:"Technology and society"'):
                return {"docs": [strict]}
            return {"docs": []}

        with patch.object(app, "ol_get", side_effect=open_library):
            books = app.build_similar_books(
                "/works/OL1W",
                ["Artificial intelligence", "Technology and society"],
                "en",
                current_title="The Age of AI",
                current_authors=["Henry Kissinger"],
            )

        self.assertEqual(
            [book["ol_key"] for book in books],
            ["/works/OL2W", "/works/OL3W"],
        )

    def test_single_subject_backfill_does_not_dilute_three_strict_matches(self):
        strict = [
            self.record(f"/works/OL{index}W", f"Strict {index}", "Author", index)
            for index in range(2, 5)
        ]
        for record in strict:
            record["subject"] = [
                "Artificial intelligence",
                "Technology and society",
            ]
        loose = self.record(
            "/works/OL5W", "Loose single-subject result", "Other Author", 5
        )
        loose["subject"] = ["Artificial intelligence"]

        def open_library(_path, params):
            if params["q"].startswith('subject:"Artificial intelligence"'):
                return {"docs": [*strict, loose]}
            if params["q"].startswith('subject:"Technology and society"'):
                return {"docs": strict}
            return {"docs": []}

        with patch.object(app, "ol_get", side_effect=open_library):
            books = app.build_similar_books(
                "/works/OL1W",
                ["Artificial intelligence", "Technology and society"],
                "en",
                current_title="The Age of AI",
                current_authors=["Henry Kissinger"],
            )

        self.assertEqual(
            [book["ol_key"] for book in books],
            [book["key"] for book in strict],
        )

    def test_unconfirmed_dual_query_result_is_not_treated_as_related(self):
        unrelated = self.record(
            "/works/OL2W", "Labyrinths", "Jorge Luis Borges", 2
        )
        unrelated["subject"] = ["Speculative fiction", "Labyrinths"]

        with patch.object(
            app,
            "ol_get",
            return_value={"docs": [unrelated]},
        ):
            books = app.build_similar_books(
                "/works/OL1W",
                ["Economics", "Capital"],
                "en",
                current_title="The People's Marx",
                current_authors=["Karl Marx"],
            )

        self.assertEqual(books, [])

    def test_broad_single_subject_does_not_dilute_existing_strict_results(self):
        strict = [
            self.record("/works/OL2W", "The Neuroscience of Sleep", "A", 2),
            self.record("/works/OL3W", "Sleep Medicine", "B", 3),
        ]
        for record in strict:
            record["subject"] = ["sleep", "health & fitness"]
        broad = self.record("/works/OL4W", "Goodnight Moon", "C", 4)
        broad["subject"] = ["sleep", "juvenile fiction"]

        def open_library(_path, params):
            if params["q"].startswith('subject:"sleep"'):
                return {"docs": [*strict, broad]}
            if params["q"].startswith('subject:"health & fitness"'):
                return {"docs": strict}
            return {"docs": []}

        with patch.object(app, "ol_get", side_effect=open_library):
            books = app.build_similar_books(
                "/works/OL1W",
                ["sleep", "health & fitness"],
                "en",
                current_title="Why We Sleep",
                current_authors=["Matthew Walker"],
            )

        self.assertEqual(
            [book["ol_key"] for book in books],
            ["/works/OL2W", "/works/OL3W"],
        )

    def test_single_generic_title_token_cannot_bypass_two_subjects(self):
        false_positive = self.record(
            "/works/OL2W", "The Age of Dinosaurs", "Someone Else", 2
        )

        def open_library(_path, params):
            if params["q"].startswith('subject:"Artificial intelligence"'):
                return {"docs": [false_positive]}
            return {"docs": []}

        with patch.object(app, "ol_get", side_effect=open_library):
            books = app.build_similar_books(
                "/works/OL1W",
                ["Artificial intelligence", "Technology"],
                "en",
                current_title="The Age of AI",
                current_authors=["Henry Kissinger"],
            )

        self.assertEqual(books, [])

    def test_complete_empty_similar_result_is_negatively_cached(self):
        with (
            patch.object(app, "build_similar_books", return_value=([], True)),
            patch.object(app, "cache_set") as memory_set,
            patch.object(app, "disk_cache_set") as disk_set,
        ):
            app._refresh_similar_books(
                "similar-key", "/works/OL1W", ["AI"], "en", "Book", ["Author"]
            )

        empty_key = app.similar_empty_cache_key("similar-key")
        self.assertEqual(memory_set.call_args.args[0], empty_key)
        self.assertEqual(disk_set.call_args.args[0], empty_key)
        self.assertTrue(disk_set.call_args.args[1]["negative"])

    def test_partial_empty_similar_result_gets_only_short_memory_cache(self):
        with (
            patch.object(app, "build_similar_books", return_value=([], False)),
            patch.object(app, "cache_set") as memory_set,
            patch.object(app, "disk_cache_set") as disk_set,
        ):
            app._refresh_similar_books(
                "similar-key", "/works/OL1W", ["AI"], "en", "Book", ["Author"]
            )

        self.assertEqual(
            memory_set.call_args.args[0],
            app.similar_partial_cache_key("similar-key"),
        )
        self.assertTrue(memory_set.call_args.args[1]["partial"])
        disk_set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
