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

    def test_similar_books_require_shared_context_or_same_author(self):
        shared = self.record(
            "/works/OL2W", "The Innovators", "Walter Isaacson", 2
        )
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
