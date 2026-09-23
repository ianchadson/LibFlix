import os
import unittest
from unittest.mock import patch

from downloaders import base
from downloaders.base import (
    Book,
    SourceFetchError,
    decode_source_id,
    encode_source_id,
    fetch_bounded,
    host_is_allowed,
    is_download_id,
    is_libgen_id,
)
from downloaders.realdebrid import RealDebridDownloader
import downloaders as downloaders_pkg
from downloaders import MultiDownloader, configured_sources


class SourceIdTests(unittest.TestCase):
    def test_libgen_id_is_raw_md5(self):
        value = "a" * 32
        self.assertTrue(is_download_id(value))
        self.assertTrue(is_libgen_id(value))
        self.assertEqual(decode_source_id(value), ("libgen", value))

    def test_realdebrid_id_round_trips(self):
        encoded = encode_source_id("realdebrid", "b" * 40)
        self.assertTrue(is_download_id(encoded))
        self.assertFalse(is_libgen_id(encoded))
        self.assertEqual(decode_source_id(encoded), ("realdebrid", "b" * 40))

    def test_malformed_ids_are_rejected(self):
        for value in ("", "xyz", "rd", "rdzz", "g" * 32, "rd" + "a" * 39, "pd123456"):
            with self.subTest(value=value):
                self.assertFalse(is_download_id(value))


class HostAllowlistTests(unittest.TestCase):
    def test_subdomains_are_allowed_but_other_hosts_are_not(self):
        self.assertTrue(host_is_allowed("https://www.example.com/x", ("example.com",)))
        self.assertTrue(host_is_allowed("https://cdn.example.com/x", ("example.com",)))
        self.assertFalse(host_is_allowed("https://evil.test/x", ("example.com",)))
        self.assertFalse(host_is_allowed("javascript:alert(1)", ("example.com",)))

    def test_off_source_fetch_is_refused_without_network(self):
        with patch("downloaders.base.SESSION.get") as get:
            with self.assertRaises(SourceFetchError):
                fetch_bounded("https://evil.test/page", allowed_hosts=("example.com",))
        get.assert_not_called()

    def test_off_source_redirect_is_refused(self):
        class Response:
            url = "https://evil.test/landing"
            encoding = "utf-8"
            headers = {"Content-Type": "text/html"}

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def iter_content(chunk_size=65536):
                return iter([b"<html>ad</html>"])

            @staticmethod
            def close():
                return None

        with patch("downloaders.base.SESSION.get", return_value=Response()):
            with self.assertRaises(SourceFetchError):
                fetch_bounded("https://www.example.com/x", allowed_hosts=("example.com",))


class RealDebridParsingTests(unittest.TestCase):
    def test_index_row_becomes_ebook(self):
        row = {
            "name": "Deep Work by Cal Newport [EPUB]",
            "info_hash": "AB" * 20,
            "size": "2500000",
            "seeders": "12",
            "category": "601",
        }
        book = RealDebridDownloader()._row_to_book(row)
        self.assertIsNotNone(book)
        self.assertEqual(book.book_id, "rd" + "ab" * 20)
        self.assertEqual(book.ext, "epub")
        self.assertEqual(book.size, "2.4 MB")
        self.assertIn("Cal Newport", book.author)

    def test_movie_rows_are_dropped(self):
        row = {
            "name": "Some Movie 2024 1080p WEB-DL x264",
            "info_hash": "CD" * 20,
            "size": "2000000000",
            "category": "201",
        }
        self.assertIsNone(RealDebridDownloader()._row_to_book(row))


class RealDebridResolveTests(unittest.TestCase):
    class Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def setUp(self):
        base._CACHE.clear()
        self.env = patch.dict(os.environ, {"LIBFLIX_REALDEBRID_KEY": "test-token"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        base._CACHE.clear()

    def _fake_request(self, calls):
        info_calls = {"count": 0}

        def request(method, path, **kwargs):
            calls.append((method, path))
            if path == "torrents":
                return self.Response(200, [])
            if path == "torrents/addMagnet":
                return self.Response(201, {"id": "T1"})
            if path == "torrents/info/T1":
                info_calls["count"] += 1
                if info_calls["count"] == 1:
                    return self.Response(200, {
                        "status": "waiting_files_selection",
                        "files": [{"id": 1, "path": "/book.epub"}],
                    })
                return self.Response(200, {
                    "status": "downloaded",
                    "files": [{"id": 1, "path": "/book.epub", "link": "https://real-debrid.com/d/XYZ"}],
                    "links": ["https://real-debrid.com/d/XYZ"],
                })
            if path == "torrents/selectFiles/T1":
                return self.Response(204)
            if path == "unrestrict/link":
                return self.Response(200, {
                    "download": "https://real-debrid.com/d/direct.epub",
                    "filename": "book.epub",
                    "filesize": 123,
                })
            if path == "torrents/delete/T1":
                return self.Response(204)
            return self.Response(404, {})

        return request

    def test_cached_torrent_resolves_to_direct_link(self):
        calls = []
        with patch.object(RealDebridDownloader, "_rd_request", side_effect=self._fake_request(calls)):
            url = RealDebridDownloader().resolve_download("rd" + "a" * 40)
        self.assertEqual(url, "https://real-debrid.com/d/direct.epub")
        self.assertIn(("POST", "torrents/addMagnet"), calls)
        self.assertIn(("POST", "torrents/selectFiles/T1"), calls)
        self.assertIn(("POST", "unrestrict/link"), calls)

    def test_uncached_torrent_is_deleted_and_fails_closed(self):
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path))
            if path == "torrents":
                return self.Response(200, [])
            if path == "torrents/addMagnet":
                return self.Response(201, {"id": "T2"})
            if path == "torrents/info/T2":
                return self.Response(200, {"status": "downloading", "files": [], "links": []})
            return self.Response(204)

        with (
            patch.object(RealDebridDownloader, "_rd_request", side_effect=request),
            patch("downloaders.realdebrid.RD_RESOLVE_WAIT", 0),
        ):
            url = RealDebridDownloader().resolve_download("rd" + "b" * 40)
        self.assertEqual(url, "")
        self.assertIn(("DELETE", "torrents/delete/T2"), calls)

    def test_resolve_without_token_is_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(RealDebridDownloader().resolve_download("rd" + "c" * 40), "")

    def test_selection_prefers_expected_extension(self):
        info_hash = "e" * 40
        base.cache_set(f"rd-ext:{info_hash}", "pdf")
        calls = {}
        info_count = {"n": 0}

        def request(method, path, **kwargs):
            if path == "torrents":
                return self.Response(200, [])
            if path == "torrents/addMagnet":
                return self.Response(201, {"id": "T3"})
            if path == "torrents/info/T3":
                info_count["n"] += 1
                if info_count["n"] == 1:
                    return self.Response(200, {
                        "status": "waiting_files_selection",
                        "files": [
                            {"id": 1, "path": "/book.epub"},
                            {"id": 2, "path": "/book.pdf"},
                        ],
                    })
                return self.Response(200, {
                    "status": "downloaded",
                    "files": [
                        {"id": 1, "path": "/book.epub", "link": "https://real-debrid.com/d/e"},
                        {"id": 2, "path": "/book.pdf", "link": "https://real-debrid.com/d/p"},
                    ],
                    "links": ["https://real-debrid.com/d/e", "https://real-debrid.com/d/p"],
                })
            if path == "torrents/selectFiles/T3":
                calls["files"] = kwargs.get("data", {}).get("files")
                return self.Response(204)
            if path == "unrestrict/link":
                calls["link"] = kwargs.get("data", {}).get("link")
                return self.Response(200, {
                    "download": "https://real-debrid.com/d/direct.pdf",
                    "filename": "book.pdf",
                    "filesize": 10,
                })
            if path == "torrents/delete/T3":
                return self.Response(204)
            return self.Response(404, {})

        with patch.object(RealDebridDownloader, "_rd_request", side_effect=request):
            url = RealDebridDownloader().resolve_download("rd" + info_hash)

        self.assertEqual(calls["files"], "2")
        self.assertEqual(calls["link"], "https://real-debrid.com/d/p")
        self.assertEqual(url, "https://real-debrid.com/d/direct.pdf")
        self.assertEqual(
            RealDebridDownloader().resolved_filename("rd" + info_hash),
            "book.pdf",
        )


class MultiDownloaderTests(unittest.TestCase):
    class FakeSource:
        def __init__(self, books=None, total=0, error=None, resolved=""):
            self._books = books or []
            self._total = total
            self._error = error
            self._resolved = resolved
            self.searches = []

        def search(self, query, *, sort="y", order="DESC", page=1, limit=25):
            self.searches.append((query, page))
            if self._error:
                raise self._error
            return self._books, self._total

        def resolve_download(self, book_id):
            return self._resolved

        def invalidate_download(self, book_id):
            return None

        def cover_url(self, book):
            return None

    def _book(self, book_id, title):
        return Book(book_id=book_id, title=title, ext="epub", source=book_id[:2])

    def test_search_merges_sources_and_isolates_failures(self):
        good = self.FakeSource([self._book("a" * 32, "One")], total=1)
        broken = self.FakeSource(error=RuntimeError("down"))
        md = MultiDownloader(["libgen", "realdebrid"])
        md.downloaders = {"libgen": good, "realdebrid": broken}

        books, total = md.search("q")

        self.assertEqual([book.title for book in books], ["One"])
        self.assertEqual(total, 1)

    def test_page_two_delegates_to_primary_only(self):
        primary = self.FakeSource([self._book("a" * 32, "One")], total=1)
        secondary = self.FakeSource([self._book("rd" + "b" * 40, "Two")], total=1)
        md = MultiDownloader(["libgen", "realdebrid"])
        md.downloaders = {"libgen": primary, "realdebrid": secondary}

        md.search("q", page=2)

        self.assertEqual(secondary.searches, [])
        self.assertEqual(primary.searches, [("q", 2)])

    def test_resolve_routes_by_id_prefix(self):
        md = MultiDownloader(["libgen", "realdebrid"])
        md.downloaders["realdebrid"] = self.FakeSource(resolved="https://real-debrid.com/d/x.epub")
        self.assertEqual(
            md.resolve_download("rd" + "d" * 40),
            "https://real-debrid.com/d/x.epub",
        )
        self.assertEqual(md.resolve_download("unknown-source-id"), "")
        self.assertEqual(md.resolve_download("not-an-id"), "")

    def test_configured_sources_env_parsing(self):
        with patch.dict(os.environ, {"LIBFLIX_DOWNLOAD_SOURCES": "libgen,realdebrid,bogus"}, clear=False):
            self.assertEqual(configured_sources(), ["libgen", "realdebrid"])
        with patch.dict(os.environ, {"LIBFLIX_DOWNLOAD_SOURCES": "all"}, clear=False):
            self.assertEqual(
                configured_sources(),
                list(downloaders_pkg.SOURCE_ORDER),
            )
        with patch.dict(os.environ, {"LIBFLIX_DOWNLOAD_SOURCES": ""}, clear=False):
            self.assertEqual(configured_sources(), ["libgen"])


class DownloadRouteTests(unittest.TestCase):
    def test_invalid_identifier_is_rejected(self):
        import app as app_module

        response = app_module.app.test_client().get("/download/not-an-id")
        self.assertEqual(response.status_code, 404)
        response.close()

    def test_namespaced_identifier_reaches_the_downloader(self):
        import app as app_module

        class FakeDownloader:
            def __init__(self):
                self.seen = []

            def resolve_download(self, book_id):
                self.seen.append(book_id)
                return ""

            def invalidate_download(self, book_id):
                return None

        fake = FakeDownloader()
        with patch.object(app_module, "DOWNLOADER", fake):
            response = app_module.app.test_client().get("/download/rd" + "a" * 40)
            self.assertEqual(response.status_code, 502)
            response.close()
        self.assertEqual(set(fake.seen), {"rd" + "a" * 40})


if __name__ == "__main__":
    unittest.main()


class RealDebridKeyFileTests(unittest.TestCase):
    def test_key_is_read_from_file_when_environment_is_unset(self):
        import tempfile
        from downloaders import realdebrid

        with tempfile.TemporaryDirectory() as directory:
            key_file = os.path.join(directory, "realdebrid-api-key")
            with open(key_file, "w", encoding="utf-8") as handle:
                handle.write("file-token\n")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("LIBFLIX_REALDEBRID_KEY", "LIBFLIX_RD_KEY")}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(realdebrid, "RD_KEY_FILE", key_file):
                self.assertTrue(realdebrid.is_enabled())
                self.assertEqual(realdebrid._api_headers(), {"Authorization": "Bearer file-token"})

    def test_missing_key_file_leaves_source_disabled(self):
        from downloaders import realdebrid

        env = {k: v for k, v in os.environ.items()
               if k not in ("LIBFLIX_REALDEBRID_KEY", "LIBFLIX_RD_KEY")}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(realdebrid, "RD_KEY_FILE", "/nonexistent/realdebrid-api-key"):
            self.assertFalse(realdebrid.is_enabled())

    def test_health_reports_download_sources_without_secrets(self):
        import app

        with patch.dict(os.environ, {"LIBFLIX_REALDEBRID_KEY": "secret-value"}):
            payload = app.app.test_client().get("/api/health").get_json()
        self.assertIn("sources", payload["downloads"])
        self.assertIsInstance(payload["downloads"]["mobi_conversion"], bool)
        self.assertNotIn("secret-value", str(payload))
