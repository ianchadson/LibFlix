import os
import struct
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import book_conversion
from book_conversion import (
    CONVERTIBLE_EXTENSIONS,
    convert_to_epub,
    is_convertible,
)
from kindle_delivery import SourceFileError, validate_source_file


def _palmdb_mobi(payload: bytes = b"BOOK DATA") -> bytes:
    """Build a minimal PalmDB header declaring a BOOKMOBI container."""
    header = bytearray(78)
    name = b"Test Book"
    header[0:len(name)] = name
    header[32:34] = struct.pack(">H", 1)  # 1 record
    header[36:40] = struct.pack(">I", 0)  # modification number
    header[60:64] = b"BOOK"
    header[64:68] = b"MOBI"
    header[68:72] = struct.pack(">I", 1)
    record_offset = 78 + 8
    header[72:76] = struct.pack(">I", 0)  # unique id seed
    header[76:78] = struct.pack(">H", 0)
    record = struct.pack(">I", record_offset) + struct.pack(">B", 0) + b"\x00\x00\x00"
    return bytes(header) + record + payload


def _valid_epub(path: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", "<container/>")
        archive.writestr("OEBPS/content.opf", "<package/>")


class ConvertibleDetectionTests(unittest.TestCase):
    def test_legacy_formats_are_convertible(self):
        for extension in ("mobi", "azw3", "MOBI", ".azw3"):
            with self.subTest(extension=extension):
                self.assertTrue(is_convertible(extension))

    def test_primary_and_hidden_formats_are_not_convertible(self):
        for extension in ("epub", "pdf", "azw", "", None, "txt", "djvu"):
            with self.subTest(extension=extension):
                self.assertFalse(is_convertible(extension))

    def test_convertible_set_is_bounded(self):
        self.assertEqual(CONVERTIBLE_EXTENSIONS, frozenset({"mobi", "azw3"}))


class MobiMagicTests(unittest.TestCase):
    def _write(self, data: bytes) -> str:
        handle = tempfile.NamedTemporaryFile(suffix=".mobi", delete=False)
        handle.write(data)
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    @staticmethod
    def _digest(path: str) -> str:
        import hashlib

        with open(path, "rb") as handle:
            return hashlib.md5(handle.read()).hexdigest()

    def test_bookmobi_header_is_accepted(self):
        path = self._write(_palmdb_mobi())
        digest = self._digest(path)
        result = validate_source_file(path, digest, "mobi")
        self.assertEqual(result.digest, digest)

    def test_html_disguised_as_mobi_is_rejected(self):
        path = self._write(b"<!DOCTYPE html><html>ad</html>")
        with self.assertRaises(SourceFileError):
            validate_source_file(path, self._digest(path), "mobi")

    def test_wrong_file_type_is_rejected(self):
        path = self._write(b"%PDF-1.7 fake pdf")
        with self.assertRaises(SourceFileError):
            validate_source_file(path, self._digest(path), "mobi")


class ConvertToEpubTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tempdir, ignore_errors=True))
        self.mobi_path = os.path.join(self.tempdir, "book.mobi")
        with open(self.mobi_path, "wb") as handle:
            handle.write(_palmdb_mobi())

    def test_non_convertible_source_is_refused(self):
        result = convert_to_epub(self.mobi_path, "epub")
        self.assertEqual(result.path, "")
        self.assertFalse(result.temporary)

    def test_missing_source_fails_closed(self):
        result = convert_to_epub(os.path.join(self.tempdir, "missing.mobi"), "mobi")
        self.assertEqual(result.path, "")
        self.assertIn("missing", result.warning.lower())

    def test_converter_output_is_copied_and_validated(self):
        extraction_dir = os.path.join(self.tempdir, "mobiex-test")
        os.makedirs(extraction_dir)
        epub_path = os.path.join(extraction_dir, "book.epub")
        _valid_epub(epub_path)

        with patch("mobi.extract", return_value=(extraction_dir, epub_path)):
            result = convert_to_epub(self.mobi_path, "mobi")

        self.assertTrue(result.path)
        self.assertTrue(result.temporary)
        self.assertEqual(result.extension, "epub")
        self.assertTrue(book_conversion._valid_epub(result.path))
        # the mobi library extraction directory is removed after copying
        self.assertFalse(os.path.exists(extraction_dir))
        # the retained workspace lives under libflix-convert-
        self.assertTrue(os.path.basename(os.path.dirname(result.path)).startswith("libflix-convert-"))

        book_conversion.cleanup_path(result.path)
        self.assertFalse(os.path.exists(os.path.dirname(result.path)))

    def test_invalid_converter_output_fails_closed(self):
        extraction_dir = os.path.join(self.tempdir, "mobiex-bad")
        os.makedirs(extraction_dir)
        bogus = os.path.join(extraction_dir, "book.epub")
        with open(bogus, "wb") as handle:
            handle.write(b"not an epub")

        with patch("mobi.extract", return_value=(extraction_dir, bogus)):
            result = convert_to_epub(self.mobi_path, "mobi")

        self.assertEqual(result.path, "")
        self.assertIn("no usable EPUB", result.warning)

    def test_extraction_exception_fails_closed(self):
        with patch("mobi.extract", side_effect=RuntimeError("boom")):
            result = convert_to_epub(self.mobi_path, "azw3")
        self.assertEqual(result.path, "")
        self.assertIn("boom", result.warning)

    def test_failed_conversion_removes_its_workspace(self):
        workspaces = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            workspaces.append(path)
            return path

        with patch("book_conversion.tempfile.mkdtemp", side_effect=tracking_mkdtemp), \
                patch("mobi.extract", side_effect=RuntimeError("boom")):
            result = convert_to_epub(self.mobi_path, "mobi")

        self.assertEqual(result.path, "")
        self.assertEqual(len(workspaces), 1)
        self.assertFalse(os.path.exists(workspaces[0]))

    def test_delivery_fails_instead_of_sending_unconverted_mobi(self):
        import app as app_module

        source = open(app_module.__file__, encoding="utf-8").read()
        conversion_block = source.split("conversion = convert_to_epub(source_path, ext)", 1)[1].split(
            "progress = 68", 1
        )[0]
        self.assertIn("raise RuntimeError(", conversion_block)
        self.assertIn("Try an EPUB or PDF edition", conversion_block)


class AppFormatVisibilityTests(unittest.TestCase):
    def test_mobi_is_visible_and_convertible_but_azw_is_hidden(self):
        import app as app_module

        self.assertTrue(app_module.is_visible_kindle_format("mobi"))
        self.assertTrue(app_module.is_visible_kindle_format("azw3"))
        self.assertFalse(app_module.is_visible_kindle_format("azw"))
        self.assertTrue(app_module.is_convertible_kindle_format("mobi"))
        self.assertTrue(app_module.is_convertible_kindle_format("azw3"))
        self.assertFalse(app_module.is_convertible_kindle_format("epub"))

    def test_deliverable_covers_direct_and_convertible(self):
        import app as app_module

        self.assertTrue(app_module.is_deliverable_kindle_format("epub"))
        self.assertTrue(app_module.is_deliverable_kindle_format("pdf"))
        self.assertTrue(app_module.is_deliverable_kindle_format("mobi"))
        self.assertTrue(app_module.is_deliverable_kindle_format("azw3"))
        self.assertFalse(app_module.is_deliverable_kindle_format("azw"))
        self.assertFalse(app_module.is_deliverable_kindle_format("djvu"))

    def test_convertible_libgen_book_is_kindle_compatible(self):
        import app as app_module

        book = app_module.Book(
            book_id="a" * 32,
            title="Protocols: An Operating Manual for the Human Body",
            author="Andrew D. Huberman",
            ext="mobi",
            size="1 MB",
            source="libgen",
        )
        score = app_module.book_score(book, "Protocols Huberman", "Andrew Huberman", "English")
        self.assertGreater(score, 0)
        self.assertTrue(app_module.is_deliverable_kindle_format(book.ext))

    def test_non_libgen_convertible_row_is_not_kindle_compatible(self):
        import app as app_module

        response = app_module.app.test_client().get(
            "/api/search?q=protocols&lang=English&format=all&dedup=0&limit=25"
        )
        payload = response.get_json()
        response.close()
        for row in payload.get("books", []):
            if row.get("source") != "libgen":
                self.assertFalse(row.get("kindle_compatible"))
                self.assertFalse(row.get("kindle_conversion"))


if __name__ == "__main__":
    unittest.main()
