import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image, ImageDraw, ImageFont
import book_conversion
from pdf_reading import clean_pages
import app


def scanned_pdf(path):
    image = Image.new('RGB', (1200, 1600), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=36)
    lines = ['A Walk Through the Garden', '',
             'The morning light fell across the garden path.',
             'We stopped beside the old apple tree and listened.', '',
             'A small bird moved between the branches.',
             'Its song carried clearly through the quiet air.']
    y = 100
    for line in lines:
        draw.text((80, y), line, font=font, fill='black')
        y += 44 if line else 30
    image.save(path, 'PDF', resolution=144)
    image.close()


class ReadingTests(unittest.TestCase):
    def test_literal_web_links_preserve_punctuation_and_exact_reading_text(self):
        from bs4 import BeautifulSoup
        from pdf_reading import inline_html
        text = ('Read (https://example.org/a_(b)?x=1&y=2), then '
                'https://example.org/end. <Safe> & unchanged.')
        soup = BeautifulSoup(inline_html(text), 'html.parser')
        self.assertEqual(soup.get_text(), text)
        self.assertEqual([a['href'] for a in soup.find_all('a')],
                         ['https://example.org/a_(b)?x=1&y=2', 'https://example.org/end'])

    def test_web_links_do_not_guess_broken_or_unsafe_addresses(self):
        from bs4 import BeautifulSoup
        from pdf_reading import inline_html
        for text in ['https://', 'https://[bad', 'https://example.org:99999/a',
                     'https://user:password@example.org/a', 'javascript:alert(1)',
                     'www.example.org', '<script>https://</script>']:
            with self.subTest(text=text):
                soup = BeautifulSoup('<span>' + inline_html(text) + '</span>', 'html.parser')
                self.assertEqual(soup.get_text(), text)
                self.assertFalse(soup.find('a'))
                self.assertFalse(soup.find('script'))

    def test_wrapped_words_paragraphs_headers_and_numbers(self):
        pages = [f'Garden Notes\nA differ-\nent story for page {i}.\n\nA second paragraph.\n{i}' for i in range(1, 4)]
        cleaned = clean_pages(pages)
        self.assertEqual(cleaned[0], ['A different story for page 1.', 'A second paragraph.'])
        self.assertEqual(len(cleaned), 3)

    def test_common_left_margin_does_not_split_every_line(self):
        self.assertEqual(clean_pages(['    The first line\n    continues here.\n\n      An indented paragraph.']),
                         [['The first line continues here.', 'An indented paragraph.']])

    def test_paragraph_can_continue_across_pdf_pages(self):
        self.assertEqual(clean_pages(['A walk through the gar-', 'den in morning light.']),
                         [['A walk through the garden in morning light.'], []])

    def test_repeated_short_pages_are_not_deleted(self):
        self.assertEqual(clean_pages(['A meaningful line.'] * 3), [['A meaningful line.']] * 3)

    @unittest.skipUnless(shutil.which('tesseract'), 'Tesseract integration dependency')
    def test_real_scan_ocr_makes_reflowable_epub_without_duplicate_page_image(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'scan.pdf'); scanned_pdf(source)
            result = book_conversion.convert_to_epub(str(source), 'pdf')
            try:
                self.assertTrue(result.path, result.warning)
                with zipfile.ZipFile(result.path) as archive:
                    text = archive.read('OEBPS/part0000.xhtml').decode()
                    self.assertIn('morning light', text)
                    self.assertIn('apple tree', text)
                    self.assertNotIn('<img', text)
                    self.assertNotIn('Page 1', text)
                    self.assertNotIn('ocrx_word', text)
            finally:
                book_conversion.cleanup(result)

    def test_missing_ocr_is_an_actionable_error(self):
        import pypdfium2 as pdfium
        from contextlib import closing
        from pdf_reading import ocr_page
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'scan.pdf'); scanned_pdf(source)
            with pdfium.PdfDocument(source) as pdf, closing(pdf[0]) as page:
                with patch('pdf_reading.shutil.which', return_value=None):
                    with self.assertRaisesRegex(RuntimeError, 'Tesseract OCR installed'):
                        ocr_page(page, directory, 0, 'eng')

    def test_unaligned_existing_ocr_preserves_scan_or_refreshes_with_available_engine(self):
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        from test_pdf_epub import pdf_bytes
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'scan.pdf'); scanned_pdf(source)
            reader = PdfReader(source)
            text_reader = PdfReader(io.BytesIO(pdf_bytes()))
            resources = reader.pages[0]['/Resources']
            resources[NameObject('/Font')] = text_reader.pages[0]['/Resources']['/Font']
            stream = DecodedStreamObject()
            stream.set_data(reader.pages[0].get_contents().get_data() + b'\nBT /F1 12 Tf 20 100 Td (Existing OCR words form a complete readable sentence without a duplicate image.) Tj ET')
            reader.pages[0][NameObject('/Contents')] = stream
            writer = PdfWriter(); writer.add_page(reader.pages[0])
            with Path(directory, 'layered.pdf').open('wb') as output: writer.write(output)
            writer.close()
            with patch('book_conversion.shutil.which', return_value=None), patch('pdf_reading.ocr_page', side_effect=AssertionError('OCR unavailable')):
                book_conversion.build_epub_from_pdf(str(Path(directory, 'layered.pdf')), str(Path(directory, 'out.epub')))
            with zipfile.ZipFile(Path(directory, 'out.epub')) as archive:
                text = archive.read('OEBPS/part0000.xhtml').decode()
                self.assertNotIn('Existing OCR words', text)
                self.assertIn('<img', text)
                self.assertIn('Original page 1', text)
            from pdf_reading import ReadingPage, ReadingBlock
            refreshed = ReadingPage([ReadingBlock('text', (0, 0, 100, 100), 'Refreshed readable content.')], (600, 800))
            with patch('book_conversion.shutil.which', return_value='tesseract'), patch('pdf_reading.ocr_page', return_value=refreshed) as engine:
                book_conversion.build_epub_from_pdf(str(Path(directory, 'layered.pdf')), str(Path(directory, 'fresh.epub')))
                engine.assert_called_once()
            with zipfile.ZipFile(Path(directory, 'fresh.epub')) as archive:
                self.assertIn(b'Refreshed readable content.', archive.read('OEBPS/part0000.xhtml'))
                self.assertFalse(any(name.startswith('OEBPS/Images/') for name in archive.namelist()))

    def test_aligned_existing_ocr_is_reused_without_duplicate_scan(self):
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
        from test_pdf_epub import pdf_bytes
        import pypdfium2 as pdfium
        from contextlib import closing
        sentence = b'Existing OCR words form a complete readable sentence.'
        writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes()))
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 6 Tf 20 100 Td (' + sentence + b') Tj ET')
        writer.pages[0][NameObject('/Contents')] = stream
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory, 'native.pdf')
            with original.open('wb') as output: writer.write(output)
            with pdfium.PdfDocument(original) as pdf, closing(pdf[0]) as page, closing(page.render(scale=2)) as bitmap:
                image = bitmap.to_pil()
                try: image.save(Path(directory, 'scan.pdf'), 'PDF', resolution=144)
                finally: image.close()
            reader = PdfReader(Path(directory, 'scan.pdf'))
            reader.pages[0]['/Resources'][NameObject('/Font')] = writer.pages[0]['/Resources']['/Font']
            layered = DecodedStreamObject()
            layered.set_data(reader.pages[0].get_contents().get_data() + b'\nBT /F1 6 Tf 3 Tr 20 100 Td (' + sentence + b') Tj ET')
            reader.pages[0][NameObject('/Contents')] = layered
            final = PdfWriter(); final.add_page(reader.pages[0])
            with Path(directory, 'layered.pdf').open('wb') as output: final.write(output)
            with patch('pdf_reading.ocr_page', side_effect=AssertionError('OCR repeated')):
                book_conversion.build_epub_from_pdf(str(Path(directory, 'layered.pdf')), str(Path(directory, 'out.epub')))
            with zipfile.ZipFile(Path(directory, 'out.epub')) as archive:
                text = archive.read('OEBPS/part0000.xhtml').decode()
                self.assertIn(sentence.decode(), text)
                self.assertNotIn('<img', text)
            final.close()
        writer.close()


class ConversionQueueTests(unittest.TestCase):
    def test_slot_serialises_threads_and_reports_waiting(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'KINDLE_SOURCE_CACHE_DIR', directory):
            waiting = threading.Event(); acquired = threading.Event()
            def second():
                with app._epub_conversion_slot(waiting): acquired.set()
            with app._epub_conversion_slot():
                thread = threading.Thread(target=second); thread.start()
                self.assertTrue(waiting.wait(2))
                self.assertFalse(acquired.is_set())
            thread.join(2)
            self.assertTrue(acquired.is_set())
            self.assertFalse(thread.is_alive())

    def test_slot_is_shared_between_processes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'KINDLE_SOURCE_CACHE_DIR', directory):
            path = str(Path(directory, '.conversion-locks/global.lock'))
            script = 'import fcntl,sys; f=open(sys.argv[1],"a+b"); fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)'
            with app._epub_conversion_slot():
                blocked = subprocess.run([sys.executable, '-c', script, path], capture_output=True)
                self.assertNotEqual(blocked.returncode, 0)
            released = subprocess.run([sys.executable, '-c', script, path], capture_output=True)
            self.assertEqual(released.returncode, 0, released.stderr)

    def test_waiting_queue_emits_progress_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'KINDLE_SOURCE_CACHE_DIR', directory):
            def fake(source, book_id, extension, waiting):
                waiting.set(); time.sleep(0.03)
                return book_conversion.ConversionResult('', '', warning='synthetic failure')
            with patch.object(app, '_convert_and_cache_epub', side_effect=fake), patch.object(app, 'RESOLVE_HEARTBEAT_SECONDS', 0.005):
                events = list(app._convert_epub_progress('source', 'a'*32, 'pdf'))
            self.assertIn('Waiting for conversion', [event['stage'] for event in events])
            with self.assertRaises(ValueError):
                with app._epub_conversion_slot(): raise ValueError('synthetic')
            with app._epub_conversion_slot(): pass
