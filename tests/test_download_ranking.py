import unittest

from app import book_score, rank_download_books
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


if __name__ == "__main__":
    unittest.main()
