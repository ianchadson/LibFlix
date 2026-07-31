import unittest

from app import book_score, rank_download_books, recommendation_reasons
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


if __name__ == "__main__":
    unittest.main()
