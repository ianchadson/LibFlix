import unittest
import tempfile
from unittest.mock import patch

import app
from app import (
    book_score,
    download_book_is_relevant,
    fastest_kindle_candidate,
    is_visible_kindle_format,
    rank_download_books,
    recommendation_reasons,
)
from downloaders.base import Book


class DownloadRankingTests(unittest.TestCase):
    def test_smallest_equally_accurate_epub_is_fastest_to_kindle(self):
        compact = Book(
            book_id="a" * 32,
            title="The Age of AI",
            author="Henry Kissinger",
            language="English",
            ext="epub",
            size="1.2 MB",
        )
        large = Book(
            book_id="b" * 32,
            title="The Age of AI",
            author="Henry Kissinger",
            publisher="Little, Brown",
            language="English",
            ext="epub",
            size="9 MB",
            pages="272",
        )

        fastest = fastest_kindle_candidate(
            [large, compact],
            "The Age of AI",
            "Henry Kissinger",
            "English",
        )
        ranked, _ = rank_download_books(
            [large, compact],
            "The Age of AI",
            "Henry Kissinger",
            "English",
        )

        self.assertIs(fastest, compact)
        self.assertIs(ranked[0], compact)

    def test_best_match_api_marks_and_leads_with_fastest_epub(self):
        compact = Book(
            book_id="a" * 32,
            title="The Age of AI",
            author="Henry Kissinger",
            language="English",
            ext="epub",
            size="1.2 MB",
        )
        large = Book(
            book_id="b" * 32,
            title="The Age of AI",
            author="Henry Kissinger",
            publisher="Little, Brown",
            language="English",
            ext="epub",
            size="9 MB",
            pages="272",
        )
        with (
            patch.object(app.DOWNLOADER, "search", return_value=([large, compact], 2)),
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set"),
            patch.object(app, "disk_cache_set"),
            app.app.test_client() as client,
        ):
            response = client.get(
                "/api/search?q=The+Age+of+AI&target_title=The+Age+of+AI"
                "&target_author=Henry+Kissinger&lang=English&dedup=0"
            )

        books = response.get_json()["books"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(books[0]["md5"], compact.book_id)
        self.assertTrue(books[0]["fastest_to_kindle"])
        self.assertTrue(books[0]["best_match"])
        self.assertIn("Fastest to Kindle", books[0]["recommendation_reasons"])

    def test_fastest_tier_does_not_choose_materially_worse_epub(self):
        accurate_pdf = Book(
            book_id="a" * 32,
            title="The Age of AI",
            author="Henry Kissinger",
            language="English",
            ext="pdf",
            size="4 MB",
        )
        wrong_epub = Book(
            book_id="b" * 32,
            title="The Age of Algorithms",
            author="Different Author",
            language="English",
            ext="epub",
            size="500 kB",
        )

        self.assertIsNone(fastest_kindle_candidate(
            [wrong_epub, accurate_pdf],
            "The Age of AI",
            "Henry Kissinger",
            "English",
        ))

    def test_clean_english_edition_beats_chinese_source_metadata(self):
        source_branded = Book(
            title="Shoe Dog",
            author="Phil Knight",
            publisher="万千书友聚集地",
            language="English",
            ext="epub",
            year="2016",
            size="481 kB",
            pages="46",
        )
        clean_edition = Book(
            title="Shoe Dog: a Memoir by the Creator of NIKE",
            author="Phil Knight",
            publisher="Simon & Schuster UK",
            language="English",
            ext="epub",
            year="2016",
            size="436 kB",
            pages="0",
        )

        ranked, _ = rank_download_books(
            [source_branded, clean_edition],
            target_title="Shoe Dog",
            target_author="Phil Knight",
            preferred_language="English",
        )

        self.assertIs(ranked[0], clean_edition)
        self.assertGreater(
            book_score(clean_edition, "Shoe Dog", "Phil Knight", "English"),
            book_score(source_branded, "Shoe Dog", "Phil Knight", "English"),
        )

    def test_chinese_metadata_is_not_penalized_in_chinese_mode(self):
        edition = Book(
            title="鞋狗",
            author="Phil Knight",
            publisher="中信出版社",
            language="Chinese",
            ext="epub",
            size="1 MB",
        )

        self.assertGreater(
            book_score(edition, "鞋狗", "Phil Knight", "Chinese"),
            0,
        )

    def test_recommendation_reasons_explain_kindle_choice(self):
        edition = Book(
            title="Catching Fire",
            author="Suzanne Collins",
            publisher="Scholastic",
            language="English",
            ext="epub",
            size="2 MB",
            pages="391",
        )

        reasons = recommendation_reasons(
            edition,
            target_title="Catching Fire",
            target_author="Suzanne Collins",
            preferred_language="English",
        )

        self.assertIn("Strong title match", reasons)
        self.assertIn("Author match", reasons)
        self.assertIn("Kindle-ready EPUB", reasons)
        self.assertLessEqual(len(reasons), 4)

    def test_mobi_and_azw_formats_are_hidden(self):
        self.assertFalse(is_visible_kindle_format("mobi"))
        self.assertFalse(is_visible_kindle_format("AZW"))
        self.assertFalse(is_visible_kindle_format("azw3"))
        self.assertTrue(is_visible_kindle_format("epub"))
        self.assertTrue(is_visible_kindle_format("pdf"))

    def test_download_search_excludes_unsupported_kindle_formats(self):
        books = [
            Book(book_id="a" * 32, title="Book", language="English", ext="epub"),
            Book(book_id="b" * 32, title="Book", language="English", ext="mobi"),
            Book(book_id="c" * 32, title="Book", language="English", ext="azw3"),
            Book(book_id="d" * 32, title="Book", language="English", ext="pdf"),
        ]
        with (
            patch.object(app.DOWNLOADER, "search", return_value=(books, len(books))),
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set"),
            patch.object(app, "disk_cache_set"),
            app.app.test_client() as client,
        ):
            response = client.get(
                "/api/search?q=unsupported-format-filter-test&target_title=Book&lang=all&dedup=0"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {book["ext"] for book in response.get_json()["books"]},
            {"epub", "pdf"},
        )

        with (
            patch.object(app.DOWNLOADER, "search", return_value=(books, len(books))),
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set"),
            patch.object(app, "disk_cache_set"),
            app.app.test_client() as client,
        ):
            legacy_response = client.get(
                "/api/search?q=legacy-mobi-filter-test&target_title=Book&format=mobi&lang=all&dedup=0"
            )

        self.assertEqual(legacy_response.status_code, 200)
        self.assertEqual(legacy_response.get_json()["format"], "all")
        self.assertEqual(
            {book["ext"] for book in legacy_response.get_json()["books"]},
            {"epub", "pdf"},
        )

    def test_unrelated_book_cannot_be_selected_for_specific_target(self):
        unrelated = Book(
            book_id="a" * 32,
            title="Artemis Fowl Book 1",
            author="Eoin Colfer",
            language="English",
            ext="epub",
        )
        correct = Book(
            book_id="b" * 32,
            title="The Art of Simple Living",
            author="Masuno, Shunmyo",
            language="English",
            ext="epub",
        )

        self.assertFalse(
            download_book_is_relevant(
                unrelated,
                "The Art of Simple Living",
                "Shunmyo Masuno",
            )
        )
        self.assertTrue(
            download_book_is_relevant(
                correct,
                "The Art of Simple Living",
                "Shunmyo Masuno",
            )
        )

        with (
            patch.object(app.DOWNLOADER, "search", return_value=([unrelated, correct], 2)),
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "disk_cache_get", return_value=None),
            patch.object(app, "cache_set"),
            patch.object(app, "disk_cache_set"),
            app.app.test_client() as client,
        ):
            response = client.get(
                "/api/search?q=The+Art+of+Simple+Living+Shunmyo+Masuno"
                "&target_title=The+Art+of+Simple+Living"
                "&target_author=Shunmyo+Masuno&lang=English&dedup=0"
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([book["title"] for book in payload["books"]], [correct.title])
        self.assertTrue(payload["books"][0]["best_match"])

    def test_generic_title_fragment_without_author_is_rejected(self):
        generic = Book(
            book_id="a" * 32,
            title="AI",
            author="",
            language="English",
            ext="epub",
        )

        self.assertFalse(
            download_book_is_relevant(
                generic,
                "The Age of AI",
                "Henry Kissinger",
            )
        )

    def test_kindle_compatibility_is_separate_from_direct_download_visibility(self):
        self.assertTrue(app.is_visible_kindle_format("fb2"))
        self.assertFalse(app.is_kindle_delivery_format("fb2"))
        self.assertTrue(app.is_kindle_delivery_format("epub"))
        self.assertTrue(app.is_kindle_delivery_format("pdf"))


class DirectDownloadTests(unittest.TestCase):
    class Downloader:
        def __init__(self):
            self.invalidations = 0

        @staticmethod
        def resolve_download(_book_id):
            return "https://files.example.test/book.epub"

        def invalidate_download(self, _book_id):
            self.invalidations += 1

    class Upstream:
        def __init__(self, content, content_type="application/epub+zip"):
            self.content = content
            self.status_code = 200
            self.headers = {
                "Content-Type": content_type,
                "Content-Length": str(len(content)),
                "Accept-Ranges": "bytes",
            }
            self.url = "https://files.example.test/book.epub"
            self.closed = False

        @staticmethod
        def raise_for_status():
            return None

        def iter_content(self, chunk_size=65536):
            del chunk_size
            yield self.content

        def close(self):
            self.closed = True

    def test_broken_cached_file_url_is_invalidated_and_retried(self):
        downloader = self.Downloader()
        html = self.Upstream(b"<html>expired link</html>", "text/html")
        valid = self.Upstream(b"EPUB-CONTENT")
        with (
            patch.object(app, "DOWNLOADER", downloader),
            patch.object(app.DL_SESSION, "get", side_effect=[html, valid]) as get,
        ):
            response = app.app.test_client().get("/download/" + "a" * 32)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"EPUB-CONTENT")
        self.assertEqual(downloader.invalidations, 1)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(html.closed)
        self.assertTrue(valid.closed)

    def test_broken_file_never_returns_empty_success(self):
        downloader = self.Downloader()
        failures = [
            self.Upstream(b"<html>failure one</html>", "text/html"),
            self.Upstream(b"<html>failure two</html>", "text/html"),
        ]
        with (
            patch.object(app, "DOWNLOADER", downloader),
            patch.object(app.DL_SESSION, "get", side_effect=failures),
        ):
            response = app.app.test_client().get("/download/" + "b" * 32)

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.get_json()["success"])
        self.assertEqual(downloader.invalidations, 2)

    def test_verified_source_cache_serves_direct_download_without_upstream(self):
        with tempfile.NamedTemporaryFile(suffix=".epub") as source:
            source.write(b"cached EPUB bytes")
            source.flush()

            class Cache:
                @staticmethod
                def get(_md5, extension):
                    return source.name if extension == "epub" else ""

            downloader = self.Downloader()
            with (
                patch.object(app, "_kindle_source_cache", return_value=Cache()),
                patch.object(app, "DOWNLOADER", downloader),
            ):
                response = app.app.test_client().get(
                    "/download/" + "a" * 32 + "?filename=Cached.epub"
                )

        body = response.data
        response.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b"cached EPUB bytes")
        self.assertEqual(downloader.invalidations, 0)

    def test_prepare_endpoint_reports_a_cached_source_as_complete(self):
        with tempfile.NamedTemporaryFile(suffix=".epub") as source:
            source.write(b"cached EPUB bytes")
            source.flush()

            class Cache:
                @staticmethod
                def get(_md5, extension):
                    return source.name if extension == "epub" else ""

            with patch.object(app, "_kindle_source_cache", return_value=Cache()):
                response = app.app.test_client().get(
                    "/api/download/prepare/" + "a" * 32 + "?ext=epub"
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"type":"complete"', response.data)
        self.assertIn(b'"source_cache_hit":true', response.data)


if __name__ == "__main__":
    unittest.main()
