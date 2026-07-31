import os
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from book_preparation import clean_book_title, prepare_book_for_kindle


def local_name(tag):
    return str(tag).rsplit("}", 1)[-1]


def build_epub(path, *, existing_cover=False):
    manifest_cover = (
        '<item id="cover-image" href="cover.jpg" media-type="image/jpeg" '
        'properties="cover-image"/>'
        if existing_cover else ""
    )
    cover_meta = '<meta name="cover" content="cover-image"/>' if existing_cover else ""
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Old_Source_Title [EPUB]</dc:title>
    {cover_meta}
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    {manifest_cover}
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>"""
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/chapter.xhtml", "<html><body>Chapter</body></html>")
        if existing_cover:
            archive.writestr("OEBPS/cover.jpg", b"existing-cover")


class BookPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_epub_gets_canonical_metadata_and_missing_cover(self):
        source_path = os.path.join(self.tempdir.name, "source.epub")
        build_epub(source_path)

        prepared = prepare_book_for_kindle(
            source_path,
            "epub",
            {
                "canonical_title": "A Clean Title",
                "author": "Example Author",
                "language": "English",
                "publisher": "Example Press",
                "year": "2026",
                "description": "A useful description.",
                "identifier": "https://openlibrary.org/works/OL123W",
            },
            cover_loader=lambda: b"jpeg-cover",
        )
        self.addCleanup(lambda: os.path.exists(prepared.path) and os.unlink(prepared.path))

        self.assertTrue(prepared.temporary)
        self.assertEqual(prepared.filename, "A Clean Title.epub")
        self.assertTrue(prepared.cover_added)
        self.assertIn("title", prepared.updated_fields)
        self.assertIn("author", prepared.updated_fields)

        with zipfile.ZipFile(prepared.path) as archive:
            first = archive.infolist()[0]
            self.assertEqual(first.filename, "mimetype")
            self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
            package = ET.fromstring(archive.read("OEBPS/content.opf"))
            values = {
                local_name(element.tag): (element.text or "").strip()
                for element in package.iter()
                if local_name(element.tag) in {
                    "title", "creator", "language", "publisher",
                    "date", "description", "identifier",
                }
            }
            self.assertEqual(values["title"], "A Clean Title")
            self.assertEqual(values["creator"], "Example Author")
            self.assertEqual(values["language"], "en")
            self.assertEqual(values["publisher"], "Example Press")
            self.assertEqual(values["date"], "2026")
            self.assertEqual(values["description"], "A useful description.")
            self.assertEqual(values["identifier"], "https://openlibrary.org/works/OL123W")
            cover_item = next(
                element for element in package.iter()
                if local_name(element.tag) == "item"
                and "cover-image" in element.get("properties", "").split()
            )
            self.assertEqual(archive.read("OEBPS/" + cover_item.get("href")), b"jpeg-cover")

    def test_existing_epub_cover_is_not_fetched_or_replaced(self):
        source_path = os.path.join(self.tempdir.name, "covered.epub")
        build_epub(source_path, existing_cover=True)
        loader_calls = []

        prepared = prepare_book_for_kindle(
            source_path,
            "epub",
            {"canonical_title": "Old Source Title [EPUB]"},
            cover_loader=lambda: loader_calls.append(True) or b"replacement",
        )
        self.addCleanup(lambda: prepared.temporary and os.path.exists(prepared.path) and os.unlink(prepared.path))

        self.assertEqual(loader_calls, [])
        self.assertFalse(prepared.cover_added)
        if prepared.temporary:
            with zipfile.ZipFile(prepared.path) as archive:
                self.assertEqual(archive.read("OEBPS/cover.jpg"), b"existing-cover")

    def test_invalid_epub_falls_back_to_original_with_clean_filename(self):
        source_path = os.path.join(self.tempdir.name, "invalid.epub")
        with open(source_path, "wb") as output:
            output.write(b"not an epub")

        prepared = prepare_book_for_kindle(
            source_path,
            "epub",
            {"canonical_title": "The_Book [retail]"},
        )

        self.assertEqual(prepared.path, source_path)
        self.assertEqual(prepared.filename, "The Book.epub")
        self.assertFalse(prepared.temporary)
        self.assertIn("BadZipFile", prepared.warning)

    def test_pdf_gets_title_author_and_subject_metadata(self):
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            self.skipTest("pypdf is not installed")
        source_path = os.path.join(self.tempdir.name, "source.pdf")
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with open(source_path, "wb") as output:
            writer.write(output)

        prepared = prepare_book_for_kindle(
            source_path,
            "pdf",
            {
                "canonical_title": "Clean PDF",
                "author": "PDF Author",
                "description": "PDF description",
            },
        )
        self.addCleanup(lambda: prepared.temporary and os.path.exists(prepared.path) and os.unlink(prepared.path))

        self.assertTrue(prepared.temporary)
        metadata = PdfReader(prepared.path).metadata
        self.assertEqual(metadata.title, "Clean PDF")
        self.assertEqual(metadata.author, "PDF Author")
        self.assertEqual(metadata.subject, "PDF description")

    def test_title_cleanup_only_removes_known_source_noise(self):
        self.assertEqual(clean_book_title("The_Great_Book (retail).epub"), "The Great Book")
        self.assertEqual(clean_book_title("The Book (A Novel)"), "The Book (A Novel)")


if __name__ == "__main__":
    unittest.main()
