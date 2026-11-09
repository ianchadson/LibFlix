"""Resource regression fixtures never use external books or networking."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject

import book_conversion as conversion
from test_pdf_epub import pdf_bytes


class PdfLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name, 'source.pdf')
        self.source.write_bytes(pdf_bytes(image=True))

    def oversized(self, nested=False, mask=False):
        reader = PdfReader(io.BytesIO(pdf_bytes(image=True)))
        image = reader.pages[0]['/Resources']['/XObject']['/Im0']
        huge = DictionaryObject({NameObject('/Subtype'): NameObject('/Image'),
                                 NameObject('/Width'): NumberObject(10000),
                                 NameObject('/Height'): NumberObject(10000)})
        if mask:
            image[NameObject('/SMask')] = huge
        else:
            image.update(huge)
        if nested:
            objects = reader.pages[0]['/Resources']['/XObject']
            form = DictionaryObject({NameObject('/Subtype'): NameObject('/Form'),
                                    NameObject('/Resources'): DictionaryObject({NameObject('/XObject'): objects})})
            reader.pages[0][NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/Form'): form})})
        return reader.pages[0]

    def test_preflight_rejects_before_any_image_decode(self):
        from pypdf._page import PageObject
        for options in ({}, {'nested': True}, {'mask': True}):
            with self.subTest(options=options), patch.object(PageObject, 'images', new_callable=lambda: property(lambda page: self.fail('Decoded before validation'))):
                with self.assertRaisesRegex(ValueError, 'image is too large'):
                    conversion._preflight_pdf_images(self.oversized(**options), [0])

    def test_compressed_oversized_pdf_rejected_before_decode(self):
        page = self.oversized()
        # Keep valid compressed pixels but forge oversized dimensions; validation
        # must fail before pypdf/Pillow tries to interpret those pixels.
        image = page['/Resources']['/XObject']['/Im0']
        page['/Resources']['/XObject'][NameObject('/Im0')] = image.flate_encode()
        writer = PdfWriter(); writer.add_page(page)
        with self.source.open('wb') as output:
            writer.write(output)
        writer.close()
        self.assertLess(self.source.stat().st_size, 25000)
        from pypdf._page import PageObject
        with patch.object(PageObject, 'images', property(lambda page: self.fail('decoded'))):
            with self.assertRaisesRegex(ValueError, 'image is too large'):
                conversion.build_epub_from_pdf(str(self.source), str(Path(self.directory.name, 'out.epub')))

    def test_total_pixels_across_pages_and_image_output_bytes(self):
        writer = PdfWriter(); reader = PdfReader(io.BytesIO(pdf_bytes(image=True)))
        writer.add_page(reader.pages[0]); writer.add_page(reader.pages[0])
        with self.source.open('wb') as output: writer.write(output)
        writer.close()
        for name, limit, message in [('PDF_MAX_TOTAL_IMAGE_PIXELS', 4, 'too many image pixels'),
                                     ('PDF_MAX_IMAGE_BYTES', 1, 'images are too large')]:
            with self.subTest(limit=name), patch.object(conversion, name, limit):
                result = conversion.convert_to_epub(str(self.source), 'pdf')
                self.assertFalse(result.path)
                self.assertIn(message, result.warning)

    def test_25mb_input_ceiling_without_starting_worker(self):
        with self.source.open('wb') as output: output.truncate(25 * 1024**2 + 1)
        with patch.object(conversion.subprocess, 'Popen', side_effect=AssertionError('worker started')):
            result = conversion.convert_to_epub(str(self.source), 'pdf')
        self.assertIn('maximum 25 MB', result.warning)

    def slow_worker(self, command, **kwargs):
        # Simulate a parser hung after creating scratch files.
        workspace = Path(command[4]).parent
        workspace.joinpath('partial').mkdir()
        workspace.joinpath('partial', 'image.png').write_bytes(b'partial image')
        self.workspace = workspace
        self.child = self.real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        return self.child

    def test_deadline_kills_worker_and_removes_partial_files(self):
        self.real_popen = subprocess.Popen
        with patch.object(conversion, 'PDF_WORKER_TIMEOUT_SECONDS', 0.1), patch.object(conversion.subprocess, 'Popen', side_effect=self.slow_worker), patch.object(conversion, '_mac_resident_bytes', return_value=0):
            result = conversion.convert_to_epub(str(self.source), 'pdf')
        self.assertFalse(result.path)
        self.assertIn('time limit', result.warning)
        self.assertIsNotNone(self.child.poll())
        self.assertFalse(self.workspace.exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS RSS watchdog')
    def test_memory_watchdog_kills_worker_and_cleans_up(self):
        self.real_popen = subprocess.Popen
        with patch.object(conversion.subprocess, 'Popen', side_effect=self.slow_worker), patch.object(conversion, '_mac_resident_bytes', return_value=conversion.PDF_WORKER_MEMORY_BYTES+1):
            result = conversion.convert_to_epub(str(self.source), 'pdf')
        self.assertIn('memory limit', result.warning)
        self.assertIsNotNone(self.child.poll())
        self.assertFalse(self.workspace.exists())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux hard address-space limit')
    def test_linux_worker_memory_limit_fails_cleanly(self):
        with patch.object(conversion, 'PDF_WORKER_MEMORY_BYTES', 16 * 1024**2):
            result = conversion.convert_to_epub(str(self.source), 'pdf')
        self.assertFalse(result.path)
        self.assertTrue(result.warning)
