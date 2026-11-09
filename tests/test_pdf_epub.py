"""PDF conversion and delivery regressions, using only synthetic local books."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject, DecodedStreamObject

import app
import book_conversion
from test_kindle_delivery import FakeSMTP


def pdf_bytes(*, text=True, image=False, encrypted=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    resources = DictionaryObject()
    content = b''
    if text:
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        resources[NameObject('/Font')] = DictionaryObject({NameObject('/F1'): writer._add_object(font)})
        content += b'BT /F1 12 Tf 20 100 Td (Hello & <world>.) Tj ET\n'
    if image:
        raster = DecodedStreamObject()
        raster.set_data(b'\xff\x00\x00' * 4)
        raster.update({NameObject('/Type'): NameObject('/XObject'),
                       NameObject('/Subtype'): NameObject('/Image'),
                       NameObject('/Width'): NumberObject(2),
                       NameObject('/Height'): NumberObject(2),
                       NameObject('/ColorSpace'): NameObject('/DeviceRGB'),
                       NameObject('/BitsPerComponent'): NumberObject(8)})
        resources[NameObject('/XObject')] = DictionaryObject({NameObject('/Im0'): writer._add_object(raster)})
        content += b'q 20 0 0 20 0 0 cm /Im0 Do Q\n'
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject('/Resources')] = resources
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.add_metadata({'/Title': 'Example & Book', '/Author': 'Example Author'})
    if encrypted:
        writer.encrypt('secret')
    with io.BytesIO() as output:
        writer.write(output)
        result = output.getvalue()
    writer.close()
    return result


class PdfConversionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'source.pdf'

    def convert(self, **kwargs):
        data = pdf_bytes(**kwargs)
        self.source.write_bytes(data)
        result = book_conversion.convert_to_epub(str(self.source), 'pdf')
        self.addCleanup(book_conversion.cleanup, result)
        self.assertEqual(self.source.read_bytes(), data)
        return result

    def test_text_and_metadata_make_well_formed_epub(self):
        result = self.convert()
        self.assertTrue(result.path, result.warning)
        with zipfile.ZipFile(result.path) as archive:
            self.assertEqual(archive.infolist()[0].filename, 'mimetype')
            self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)
            for name in archive.namelist():
                if name.endswith(('.opf', '.ncx', '.xhtml', '.xml')):
                    ElementTree.fromstring(archive.read(name))
            self.assertIn(b'Example &amp; Book', archive.read('OEBPS/content.opf'))
            self.assertIn(b'Hello &amp; &lt;world&gt;.', archive.read('OEBPS/part0000.xhtml'))

    def test_scanned_page_keeps_image_without_ocr(self):
        result = self.convert(text=False, image=True)
        self.assertTrue(result.path, result.warning)
        with zipfile.ZipFile(result.path) as archive:
            self.assertIn('OEBPS/Images/image00001.png', archive.namelist())
            self.assertIn(b'Images/image00001.png', archive.read('OEBPS/part0000.xhtml'))

    def test_illustrated_page_keeps_text_and_image(self):
        result = self.convert(text=True, image=True)
        self.assertTrue(result.path, result.warning)
        with zipfile.ZipFile(result.path) as archive:
            page = archive.read('OEBPS/part0000.xhtml')
            self.assertIn(b'Hello', page)
            self.assertIn(b'<img', page)

    def test_encrypted_and_empty_pdfs_fail_visibly(self):
        for options, message in [({'encrypted': True}, 'Password-protected'),
                                 ({'text': False}, 'no extractable')]:
            with self.subTest(options=options):
                result = self.convert(**options)
                self.assertFalse(result.path)
                self.assertIn(message, result.warning)

    def test_bounds_fail_closed(self):
        for limit, options in [('PDF_MAX_BYTES', {}), ('PDF_MAX_PAGES', {}),
                               ('PDF_MAX_TEXT', {}), ('PDF_MAX_IMAGE_PIXELS', {'image': True})]:
            with self.subTest(limit=limit), patch.object(book_conversion, limit, 0):
                result = self.convert(**options)
                self.assertFalse(result.path)
                self.assertTrue(result.warning)

    def test_invalid_pdf_does_not_leave_workspace(self):
        self.source.write_bytes(b'not a PDF')
        workspaces = []
        mkdtemp = tempfile.mkdtemp
        def track(*args, **kwargs):
            path = mkdtemp(*args, **kwargs)
            workspaces.append(path)
            return path
        with patch.object(book_conversion.tempfile, 'mkdtemp', side_effect=track):
            result = book_conversion.convert_to_epub(str(self.source), 'pdf')
        self.assertFalse(result.path)
        self.assertTrue(all(not os.path.exists(path) for path in workspaces))


class PdfDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = pdf_bytes(image=True)
        self.md5 = hashlib.md5(self.data).hexdigest()
        self.source = Path(self.directory.name) / 'book.pdf'
        self.source.write_bytes(self.data)
        patcher = patch.object(app, 'KINDLE_SOURCE_CACHE_DIR', str(Path(self.directory.name) / 'cache'))
        patcher.start()
        self.addCleanup(patcher.stop)
        cache = app._kindle_source_cache()
        temporary = cache.temporary_path(self.md5, 'pdf')
        Path(temporary).write_bytes(self.data)
        self.cached_pdf = cache.commit(temporary, self.md5, 'pdf')
        self.client = app.app.test_client()

    def prepare(self):
        response = self.client.get(f'/api/download/prepare/{self.md5}?ext=pdf&format=epub')
        return [json.loads(line) for line in response.data.splitlines()]

    def test_download_preparation_conversion_reuse_and_original_pdf(self):
        with patch.object(app.DOWNLOADER, 'resolve_download', side_effect=AssertionError('No upstream expected')):
            events = self.prepare()
            self.assertEqual(events[-1]['type'], 'complete', events)
            self.assertIn('Converting to EPUB', [event.get('stage') for event in events])
            with patch.object(app, 'convert_to_epub', side_effect=AssertionError('Must reuse EPUB')):
                self.assertEqual(self.prepare()[-1]['type'], 'complete')
                response = self.client.get(f'/download/{self.md5}?format=epub&source_ext=pdf&filename=Example.pdf')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, 'application/epub+zip')
                self.assertIn('Example.epub', response.headers['Content-Disposition'])
                self.assertTrue(zipfile.is_zipfile(io.BytesIO(response.data)))
                response.close()
            original = self.client.get(f'/download/{self.md5}?filename=Example.pdf')
            self.assertEqual(original.mimetype, 'application/pdf')
            self.assertEqual(original.data, self.data)
            original.close()
        self.assertEqual(Path(self.cached_pdf).read_bytes(), self.data)
        # Converted EPUB bytes never occupy the original source-hash namespace.
        self.assertEqual(app._kindle_source_cache().get(self.md5, 'epub'), '')

    def test_direct_epub_link_works_without_javascript_and_supports_ranges(self):
        response = self.client.get(f'/download/{self.md5}?format=epub&source_ext=pdf&filename=Example.epub')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(response.data)))
        response.close()
        response = self.client.get(f'/download/{self.md5}?format=epub', headers={'Range': 'bytes=0-3'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b'PK\x03\x04')
        response.close()

    def test_conversion_failure_reports_error_without_pdf_fallback(self):
        failure = book_conversion.ConversionResult('', '', warning='PDF contains no readable content')
        with patch.object(app, 'convert_to_epub', return_value=failure):
            self.assertEqual(self.prepare()[-1]['type'], 'error')
            response = self.client.get(f'/download/{self.md5}?format=epub')
            self.assertEqual(response.status_code, 422)
            self.assertIn('no readable content', response.json['error'])
        self.assertEqual(Path(self.cached_pdf).read_bytes(), self.data)

    def kindle_events(self):
        FakeSMTP.connections = 0
        FakeSMTP.messages = []
        FakeSMTP.fail_first = False
        payload = {'md5': self.md5, 'ext': 'pdf', 'title': 'Example',
                   'kindle_email': 'reader@kindle.com', 'smtp_host': 'smtp.example.com',
                   'smtp_port': 587, 'smtp_user': 'sender@example.com', 'smtp_pass': 'test-only'}
        with patch('smtplib.SMTP', FakeSMTP):
            return list(app._send_to_kindle_events(payload))

    def test_kindle_sends_epub_by_default_and_reuses_download_conversion(self):
        self.assertEqual(self.prepare()[-1]['type'], 'complete')
        with patch.object(app, 'convert_to_epub', side_effect=AssertionError('Must reuse EPUB')):
            events = self.kindle_events()
        self.assertEqual(events[-1]['type'], 'complete', events)
        attachment = next(FakeSMTP.messages[-1].iter_attachments())
        self.assertEqual(attachment.get_filename(), 'Example.epub')
        self.assertEqual(attachment.get_content_type(), 'application/epub+zip')
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(attachment.get_payload(decode=True))))

    def test_kindle_converts_pdf_and_never_sends_pdf_on_failure(self):
        events = self.kindle_events()
        self.assertEqual(events[-1]['type'], 'complete', events)
        self.assertIn('Converting book', [event.get('stage') for event in events])
        self.assertTrue(next(FakeSMTP.messages[-1].iter_attachments()).get_filename().endswith('.epub'))
        # Use a fresh derived namespace for the failure case.
        with patch.object(app, '_converted_epub_key', return_value='rd' + 'a' * 40), \
             patch.object(app, 'convert_to_epub', return_value=book_conversion.ConversionResult('', '', warning='invalid PDF')):
            events = self.kindle_events()
        self.assertEqual(events[-1]['type'], 'error', events)
        self.assertEqual(FakeSMTP.messages, [])

    def test_invalid_conversion_parameters_are_rejected(self):
        self.assertEqual(self.client.get(f'/api/download/prepare/{self.md5}?ext=pdf&format=txt').status_code, 400)
        self.assertEqual(self.client.get(f'/download/{self.md5}?format=epub&source_ext=txt').status_code, 400)

    def test_cold_source_is_downloaded_and_hash_validated_before_conversion(self):
        data = self.data
        class Response:
            status_code = 200
            headers = {'content-length': str(len(data)), 'content-type': 'application/pdf'}
            def raise_for_status(self):
                pass
            def iter_content(self, chunk_size=65536):
                yield data
            def close(self):
                pass
        with patch.object(app, 'KINDLE_SOURCE_CACHE_DIR', str(Path(self.directory.name) / 'cold-cache')), \
             patch.object(app.DOWNLOADER, 'resolve_download', return_value='https://files.example.test/book.pdf'), \
             patch.object(app.SESSION, 'get', return_value=Response()) as get:
            events = self.prepare()
            self.assertEqual(events[-1]['type'], 'complete', events)
            self.assertTrue(get.called)
            self.assertEqual(Path(app._kindle_source_cache().get(self.md5, 'pdf')).read_bytes(), self.data)

    def test_slow_conversion_emits_heartbeat(self):
        import time
        real_convert = app._convert_and_cache_epub
        def slow(*args):
            time.sleep(0.03)
            return real_convert(*args)
        with patch.object(app, 'RESOLVE_HEARTBEAT_SECONDS', 0.005), \
             patch.object(app, '_convert_and_cache_epub', side_effect=slow):
            events = self.prepare()
        self.assertEqual(events[-1]['type'], 'complete', events)
        self.assertTrue(any(event.get('detail') == 'Still processing this edition' for event in events))

    def test_disabled_conversion_cache_does_not_leak_temporary_files(self):
        results = []
        real_convert = app.convert_to_epub
        def track(*args):
            result = real_convert(*args)
            results.append(result)
            return result
        with patch.object(app, 'KINDLE_SOURCE_CACHE_TTL', 0), \
             patch.object(app, 'convert_to_epub', side_effect=track):
            conversion = app._convert_and_cache_epub(str(self.source), self.md5, 'pdf')
            self.assertTrue(conversion.temporary)
            book_conversion.cleanup(conversion)
        self.assertTrue(results)
        self.assertTrue(all(not os.path.exists(result.path) for result in results))
