import unittest
from unittest.mock import patch

import requests

from downloaders import base
from downloaders.libgen import LibgenDownloader


class LibgenPaginationTests(unittest.TestCase):
    def test_paginator_script_is_converted_to_result_count(self):
        html = (
            '<div class="paginator" id="paginator_example_top"></div>'
            '<script>new Paginator("paginator_example_top", 80, 25, 1, "/index.php?page=")</script>'
        )

        self.assertEqual(
            LibgenDownloader._total_results(
                html,
                page_size=25,
                current_page=1,
                result_count=25,
            ),
            2000,
        )
        self.assertEqual(
            LibgenDownloader._total_results(
                html,
                page_size=25,
                current_page=80,
                result_count=7,
            ),
            1982,
        )

    def test_single_page_uses_actual_result_count(self):
        self.assertEqual(
            LibgenDownloader._total_results(
                "<html></html>",
                page_size=25,
                current_page=1,
                result_count=7,
            ),
            7,
        )


class LibgenResponseValidationTests(unittest.TestCase):
    class Response:
        status_code = 200
        url = "https://libgen.li/index.php"

        def __init__(self, text):
            self.text = text

        @staticmethod
        def raise_for_status():
            return None

    def test_unexpected_search_page_is_not_cached_as_empty_success(self):
        response = self.Response("<html><body>Temporary gateway page</body></html>")
        with (
            patch("downloaders.libgen.SESSION.get", return_value=response),
            patch("downloaders.libgen.cache_get", return_value=None),
            patch("downloaders.libgen.cache_set") as cache_set,
        ):
            with self.assertRaises(requests.RequestException):
                LibgenDownloader()._fetch_search("book", "y", "DESC", 1, 25)

        cache_set.assert_not_called()

    def test_explicit_zero_result_page_is_valid(self):
        response = self.Response("<html><body><span>Files 0</span></body></html>")
        with (
            patch("downloaders.libgen.SESSION.get", return_value=response),
            patch("downloaders.libgen.cache_get", return_value=None),
            patch("downloaders.libgen.cache_set") as cache_set,
        ):
            html = LibgenDownloader()._fetch_search("missing", "y", "DESC", 1, 25)

        self.assertIn("Files 0", html)
        cache_set.assert_called_once()

    def test_parser_drops_rows_without_download_identifier(self):
        cells = "".join(f"<td>value {index}</td>" for index in range(9))
        html = f'<table id="tablelibgen"><tr><th>Header</th></tr><tr>{cells}</tr></table>'

        self.assertEqual(LibgenDownloader._parse_results(html), [])


class LibgenResolverTests(unittest.TestCase):
    def tearDown(self):
        base._CACHE.clear()

    def test_generic_page_link_is_not_mistaken_for_book_file(self):
        response = self.Response('<html><a href="/">Home</a></html>')
        with patch("downloaders.libgen.SESSION.get", return_value=response):
            resolved = LibgenDownloader().resolve_download("a" * 32)

        self.assertEqual(resolved, "")

    def test_invalidate_download_removes_expired_resolved_url(self):
        key = "lg-download:" + "a" * 32
        base.cache_set(key, "https://example.test/expired.epub")

        LibgenDownloader().invalidate_download("a" * 32)

        self.assertIsNone(base.cache_get(key))

    class Response:
        status_code = 200
        url = "https://libgen.li/ads.php"

        def __init__(self, text):
            self.text = text

        @staticmethod
        def raise_for_status():
            return None


if __name__ == "__main__":
    unittest.main()
