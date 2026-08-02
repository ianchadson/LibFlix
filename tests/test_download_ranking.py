import unittest
from unittest.mock import patch

import app
from app import (
    book_score,
    download_book_is_relevant,
    is_visible_kindle_format,
    rank_download_books,
    recommendation_reasons,
)
from downloaders.base import Book


class DownloadRankingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
