"""Synthetic layout regressions; no downloaded books or personal fixtures."""
from contextlib import closing
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image, ImageDraw
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, ArrayObject, NumberObject, DictionaryObject
import pypdfium2 as pdfium

import app
import book_conversion
from pdf_reading import (ReadingBlock, ReadingPage, read_hocr, ocr_page,
                         remove_running_headers, save_reading_image)
from pdf_reading import (clean_pages, flow_blocks, unaccounted_ink, join_page_continuations,
                         scan_pages_to_preserve)
from test_pdf_epub import pdf_bytes
from test_pdf_reading import scanned_pdf


class LayoutTests(unittest.TestCase):
    def test_uncertain_scan_book_uses_consistent_layout_without_changing_native_pages(self):
        pages = [ReadingPage([ReadingBlock('image', (0, 0, 1, 1))], fallback=True),
                 ReadingPage([ReadingBlock('image', (0, 0, 1, 1))], fallback=True),
                 ReadingPage([ReadingBlock('text', (0, 0, 1, 1), 'Clear scan text.')]),
                 ReadingPage([ReadingBlock('text', (0, 0, 1, 1), 'Native text.')]),
                 ReadingPage()]
        self.assertEqual(scan_pages_to_preserve(pages, [True, True, True, False, True]), [2])

    def test_mostly_clear_scans_keep_reflow(self):
        pages = [ReadingPage([ReadingBlock('text', (0, 0, 1, 1), 'Clear text.')]) for _ in range(4)]
        pages[0].fallback = True
        self.assertEqual(scan_pages_to_preserve(pages, [True] * 4), [])

    def test_multiple_fragments_form_one_complete_paragraph(self):
        self.assertEqual(clean_pages(['She tried the little', 'golden key in',
                                      'the lock, and it fitted!', 'A new paragraph.'], edge_cleanup=False),
                         [['She tried the little golden key in the lock, and it fitted!'], [], [], ['A new paragraph.']])

    def test_list_item_continues_across_printed_pages_without_a_new_paragraph(self):
        item = ReadingBlock('text', (0, 0, 100, 100), '• Find a space that works', role='list')
        pages = [ReadingPage([item]), ReadingPage([
            ReadingBlock('text', (0, 0, 100, 100), 'best for you. Keep it quiet.'),
            ReadingBlock('text', (0, 100, 100, 200), '• Another item.', role='list')])]
        join_page_continuations(pages)
        self.assertEqual(item.text, '• Find a space that works\nbest for you. Keep it quiet.')
        self.assertEqual(len(pages[1].blocks), 1)
        self.assertEqual(item.role, 'list')

    def test_movable_art_does_not_interrupt_a_sentence_across_pages(self):
        art = ReadingBlock('image', (0, 0, 100, 100))
        text = [ReadingBlock('text', (0, 0, 0, 0), value) for value in
                ['upon a low curtain', 'and found a door.', 'Alice opened it.']]
        self.assertEqual(flow_blocks([art, *text], 'Presently she came'), [*text[:2], art, text[2]])
        self.assertEqual(flow_blocks([art, *text], 'A complete sentence.'), [art, *text])

    def test_art_moves_after_sentence_inside_a_multi_sentence_pdf_block(self):
        art = ReadingBlock('image', (0, 0, 100, 100))
        continuation = ReadingBlock('text', (0, 0, 0, 0), 'to be handy. Two blocks are placed on a board.')
        ordered = flow_blocks([art, continuation], 'Use any odd pieces that happen')
        self.assertEqual([b.kind for b in ordered], ['text', 'image', 'text'])
        self.assertEqual(ordered[0].text, 'to be handy.')
        self.assertEqual(ordered[2].text, 'Two blocks are placed on a board.')

    def test_art_does_not_split_a_figure_reference_at_its_abbreviation(self):
        art = ReadingBlock('image', (0, 0, 100, 100))
        continuation = ReadingBlock('text', (0, 0, 0, 0), 'as shown in Fig. 21. A new sentence.')
        ordered = flow_blocks([art, continuation], 'Build the arrangement')
        self.assertEqual(ordered[0].text, 'as shown in Fig. 21.')
        self.assertIs(ordered[1], art)

    def test_recovered_art_does_not_discard_glyph_header_metadata(self):
        page = self.layout([('ocr_header', (200, 40, 400, 65), 'A RUNNING HEADER', 75),
                            ('ocr_line', (80, 200, 500, 230), 'A clear sentence.', 95)], True)
        header = next(b for b in page.blocks if b.text == 'A RUNNING HEADER')
        self.assertEqual(header.role, 'ocr-glyph')
        self.assertEqual(header.kind, 'image')

    def layout(self, entries, extra_ink=False):
        image = Image.new('RGB', (600, 800), 'white')
        self.addCleanup(image.close)
        draw = ImageDraw.Draw(image)
        tags = []
        for kind, box, text, confidence in entries:
            bounds = ' '.join(map(str, box))
            draw.rectangle(box, fill='black')
            if kind == 'image':
                tags.append(f'<div class="ocr_photo" title="bbox {bounds}"></div>')
            else:
                tags.append(f'<p class="ocr_par" title="bbox {bounds}"><span class="{kind}">'
                            f'<span class="ocrx_word" title="bbox {bounds}; x_wconf {confidence}">'
                            f'{text}</span></span></p>')
        if extra_ink:
            draw.rectangle((300, 650, 500, 750), fill='black')
        return read_hocr(''.join(tags), image)

    def test_text_above_and_below_art_and_caption_are_ordered(self):
        page = self.layout([('ocr_line', (80, 200, 500, 230), 'Above the drawing.', 95),
                            ('image', (80, 270, 500, 450), '', 100),
                            ('ocr_caption', (80, 480, 500, 510), 'Below the drawing.', 95)])
        self.assertFalse(page.fallback, page.reason)
        self.assertEqual([b.kind for b in page.blocks], ['text', 'image', 'text'])
        self.assertEqual(page.blocks[-1].text, 'Below the drawing.')

    def test_art_beside_text_is_placed_before_adjacent_passage(self):
        page = self.layout([('ocr_line', (350, 299, 550, 430), 'Beside the drawing.', 95),
                            ('image', (60, 300, 330, 440), '', 100)])
        self.assertFalse(page.fallback, page.reason)
        self.assertEqual([b.kind for b in page.blocks], ['image', 'text'])

    def test_overlaid_text_unrecognised_content_and_columns_keep_whole_page(self):
        cases = [([('ocr_line', (100, 200, 400, 250), 'Overlaid words remain.', 95),
                   ('image', (80, 180, 500, 450), '', 100)], False),
                 ([('ocr_line', (60, 200, 280, 450), 'Left column words.', 95),
                   ('ocr_line', (320, 200, 560, 450), 'Right column words.', 95)], False)]
        for entries, extra in cases:
            with self.subTest(entries=entries):
                self.assertTrue(self.layout(entries, extra).fallback)

    def test_low_confidence_body_keeps_original(self):
        self.assertTrue(self.layout([('ocr_line', (80, 200, 500, 240), 'Uncertain body words.', 30)]).fallback)

    def test_high_average_ocr_does_not_hide_uncertain_body_words(self):
        with Image.new('RGB', (600, 800), 'white') as image:
            ImageDraw.Draw(image).rectangle((80, 200, 500, 240), fill='black')
            clear = 'Reliable paragraph words ' * 8
            hocr = ('<p class="ocr_par" title="bbox 80 200 500 240">'
                    '<span class="ocr_line"><span class="ocrx_word" '
                    f'title="bbox 80 200 450 240; x_wconf 99">{clear}</span>'
                    '<span class="ocrx_word" title="bbox 450 200 500 240; '
                    'x_wconf 30">uncertain</span></span></p>')
            page = read_hocr(hocr, image)
        self.assertTrue(page.fallback)
        self.assertEqual(page.reason, 'Uncertain OCR paragraph geometry')

    def test_quoted_verse_preserves_its_line_breaks(self):
        lines = ['“Thought starts this verse', 'By thought another line', 'mind',
                 'Hath another line', 'The next line', '.. If one endure',
                 'In purity of thought', 'As his own shadow.”']
        with Image.new('RGB', (600, 800), 'white') as image:
            draw = ImageDraw.Draw(image); tags = []
            for i, text in enumerate(lines):
                box = (100, 200 + i * 30, 400, 220 + i * 30)
                draw.rectangle(box, fill='black'); bounds = ' '.join(map(str, box))
                tags.append(f'<span class="ocr_line" title="bbox {bounds}"><span class="ocrx_word" '
                            f'title="bbox {bounds}; x_wconf 99">{text}</span></span>')
            page = read_hocr('<p class="ocr_par" title="bbox 100 200 400 430">' + ''.join(tags) + '</p>', image)
        self.assertFalse(page.fallback, page.reason)
        self.assertEqual(page.blocks[0].role, 'verse')
        self.assertEqual(page.blocks[0].text.splitlines(), lines)

    def test_lowercase_body_fragment_is_not_a_heading(self):
        page = self.layout([('ocr_line', (80, 200, 500, 245), 'golden key in', 99),
                            ('ocr_line', (80, 300, 500, 320), 'the lock, and it fitted.', 99)])
        self.assertEqual(page.blocks[0].role, '')

    def test_blank_scan_is_not_an_empty_full_page_picture(self):
        with Image.new('RGB', (600, 800), 'white') as image:
            page = read_hocr('<html></html>', image)
        self.assertFalse(page.blocks)
        self.assertFalse(page.fallback)

    def test_tall_single_body_line_is_not_promoted_to_heading(self):
        page = self.layout([('ocr_line', (80, 200, 500, 245),
                             'The hand plane, for smoothing the edges, etc.', 99),
                            ('ocr_line', (80, 300, 500, 320), 'A normal body line follows.', 99)])
        self.assertFalse(page.fallback, page.reason)
        self.assertEqual(page.blocks[0].role, '')
        page = self.layout([('ocr_line', (80, 200, 500, 245),
                             'Owing to the supply of narrow and faulty timber', 99),
                            ('ocr_line', (80, 300, 500, 320), 'the sentence continues here.', 99)])
        self.assertEqual(page.blocks[0].role, '')

    def test_nested_picture_boxes_do_not_duplicate_illustration(self):
        page = self.layout([('image', (80, 250, 500, 500), '', 100),
                            ('image', (100, 300, 200, 400), '', 100)])
        self.assertFalse(page.fallback)
        self.assertEqual(len(page.blocks), 1)

    def test_variable_headers_and_page_numbers_are_removed_without_body_loss(self):
        pages = [ReadingPage([ReadingBlock('text', (80, 70, 400, 100), header),
                              ReadingBlock('text', (500, 75, 520, 100), 'co', 25),
                              ReadingBlock('text', (80, 220, 500, 600), 'The actual body stays.'),
                              ReadingBlock('text', (280, 750, 320, 780), str(i))], (600, 800))
                 for i, header in enumerate(['RABBIT HOLE 1', 'RABBIT HOLE 2', 'RABRIT HOLE 3',
                                               'RABBIT HOLE 4'], 1)]
        remove_running_headers(pages)
        self.assertTrue(all([b.text for b in p.blocks] == ['The actual body stays.'] for p in pages))

    def test_composed_images_are_bounded_jpegs(self):
        with tempfile.TemporaryDirectory() as directory, Image.new('RGB', (3000, 4000), 'white') as image:
            saved = save_reading_image(image, directory, 'page')
            with Image.open(saved) as result:
                self.assertEqual(result.format, 'JPEG')
                self.assertLessEqual(result.width, 2000)
                self.assertLessEqual(result.height, 2400)
            self.assertLess(os.path.getsize(saved), 100_000)

    def test_ocr_off_uses_composed_page_without_calling_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'scan.pdf'); scanned_pdf(source)
            with patch.dict(os.environ, LIBFLIX_PDF_OCR_ENABLED='0'), patch('pdf_reading.ocr_page', side_effect=AssertionError('OCR disabled')):
                book_conversion.build_epub_from_pdf(str(source), str(Path(directory, 'out.epub')))
            with zipfile.ZipFile(Path(directory, 'out.epub')) as archive:
                content = archive.read('OEBPS/part0000.xhtml')
                self.assertEqual(content.count(b'<img'), 1)
                self.assertIn(b'Original page 1', content)

    def test_cache_separates_ocr_modes(self):
        with patch.dict(os.environ, LIBFLIX_PDF_OCR_ENABLED='0'):
            without = app._converted_epub_key('a' * 32, 'pdf')
        with patch.dict(os.environ, LIBFLIX_PDF_OCR_ENABLED='1'):
            self.assertNotEqual(without, app._converted_epub_key('a' * 32, 'pdf'))

    def test_native_vector_and_overlay_are_composed_instead_of_dropped(self):
        for drawing in [b'0 0 1 rg 20 50 100 100 re f', b'q 80 0 0 80 20 80 cm /Im0 Do Q']:
            with self.subTest(drawing=drawing), tempfile.TemporaryDirectory() as directory:
                writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes(image=True)))
                page = writer.pages[0]
                stream = DecodedStreamObject()
                stream.set_data(page.get_contents().get_data() + b'\n' + drawing)
                page[NameObject('/Contents')] = writer._add_object(stream)
                source = Path(directory, 'mixed.pdf')
                with source.open('wb') as output: writer.write(output)
                writer.close()
                book_conversion.build_epub_from_pdf(str(source), str(Path(directory, 'out.epub')))
                with zipfile.ZipFile(Path(directory, 'out.epub')) as archive:
                    self.assertNotIn(b'Hello &amp;', archive.read('OEBPS/part0000.xhtml'))
                    images = [n for n in archive.namelist() if n.startswith('OEBPS/Images/')]
                    self.assertEqual(len(images), 2)  # unrelated image plus composed overlay region
                    found = False
                    for name in images:
                      with Image.open(io.BytesIO(archive.read(name))) as composed:
                        colors = composed.convert('RGB').getcolors(composed.width * composed.height)
                        found |= (any(b > r + 80 for count, (r, g, b) in colors) if drawing.startswith(b'0')
                                  else any(r > b + 80 for count, (r, g, b) in colors))
                    self.assertTrue(found)

    def test_off_page_ocr_boxes_do_not_crash_or_hide_visible_ink(self):
        with Image.new('RGB', (100, 100), 'white') as image:
            ImageDraw.Draw(image).rectangle((30, 30, 70, 70), fill='black')
            self.assertTrue(unaccounted_ink(image, [(0, 200, 20, 240), (-200, 0, -100, 30)]))

    def test_unlabelled_drawing_is_retained_between_reflowable_text(self):
        page = self.layout([('ocr_line', (80, 200, 500, 230), 'A clear sentence.', 95)], True)
        self.assertFalse(page.fallback, page.reason)
        self.assertEqual([b.kind for b in page.blocks], ['text', 'image'])
        self.assertLessEqual(page.blocks[1].box[1], 650)
        self.assertGreaterEqual(page.blocks[1].box[3], 750)

    def test_header_in_same_ocr_paragraph_does_not_delete_body(self):
        pages = [ReadingPage([ReadingBlock('text', (50, 40, 500, 160),
                                           'A RUNNING HEADER\nThe complete body remains here.')], (600, 800))
                 for _ in range(4)]
        remove_running_headers(pages)
        self.assertTrue(all(p.blocks[0].text == 'The complete body remains here.' for p in pages))

    def test_printer_signature_at_page_foot_does_not_enter_a_continuing_sentence(self):
        page = ReadingPage([ReadingBlock('text', (50, 400, 500, 650), 'The wedges are'),
                            ReadingBlock('text', (200, 710, 450, 730), 'e 3 B2')], (600, 800))
        remove_running_headers([page])
        self.assertEqual([b.text for b in page.blocks], ['The wedges are'])

    def test_native_heading_lists_and_text_beside_diagram_survive(self):
        writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes()))
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 20 Tf 20 165 Td (Study Space) Tj ET\n'
                        b'BT /F1 10 Tf 20 140 Td (1. Choose a quiet place.) Tj ET\n'
                        b'BT /F1 10 Tf 30 125 Td (Continue the same item here.) Tj ET\n'
                        b'BT /F1 10 Tf 20 100 Td (2. Keep your desk clear.) Tj ET\n'
                        b'0 0 1 rg 20 30 160 40 re f\n'
                        b'0 0 0 rg BT /F1 10 Tf 30 50 Td (Diagram label) Tj ET\n')
        writer.pages[0][NameObject('/Contents')] = writer._add_object(stream)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'native.pdf')
            with source.open('wb') as output: writer.write(output)
            book_conversion.build_epub_from_pdf(str(source), str(Path(directory, 'out.epub')))
            with zipfile.ZipFile(Path(directory, 'out.epub')) as archive:
                text = archive.read('OEBPS/part0000.xhtml').decode()
                from bs4 import BeautifulSoup
                self.assertEqual(BeautifulSoup(text, 'html.parser').h2.get_text(), 'Study Space')
                self.assertIn('1. Choose a quiet place. Continue the same item here.', text)
                self.assertIn('2. Keep your desk clear.', text)
                self.assertNotIn('Diagram label', text)
                self.assertEqual(text.count('<img'), 1)
        writer.close()

    def test_native_parallel_columns_keep_their_original_order_visually(self):
        writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes()))
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 10 Tf 10 150 Td (Left text.) Tj ET\n'
                        b'BT /F1 10 Tf 120 150 Td (Right text.) Tj ET\n'
                        b'BT /F1 10 Tf 10 135 Td (Left next.) Tj ET\n'
                        b'BT /F1 10 Tf 120 135 Td (Right next.) Tj ET\n')
        writer.pages[0][NameObject('/Contents')] = writer._add_object(stream)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'columns.pdf')
            with source.open('wb') as output: writer.write(output)
            book_conversion.build_epub_from_pdf(str(source), str(Path(directory, 'out.epub')))
            with zipfile.ZipFile(Path(directory, 'out.epub')) as archive:
                self.assertIn(b'Original page 1', archive.read('OEBPS/part0000.xhtml'))
        writer.close()

    def test_transformed_form_artwork_stays_in_the_correct_position(self):
        writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes()))
        form = DecodedStreamObject(); form.set_data(b'0 0 1 rg 0 0 30 30 re f')
        form.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Form'),
                     NameObject('/BBox'): ArrayObject([NumberObject(n) for n in [0, 0, 30, 30]]),
                     NameObject('/Resources'): DictionaryObject()})
        page = writer.pages[0]
        page['/Resources'][NameObject('/XObject')] = DictionaryObject({NameObject('/Fm'): writer._add_object(form)})
        content = DecodedStreamObject()
        content.set_data(page.get_contents().get_data() + b'\nq 2 0 0 2 100 30 cm /Fm Do Q')
        page[NameObject('/Contents')] = writer._add_object(content)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'form.pdf')
            with source.open('wb') as output: writer.write(output)
            book_conversion.build_epub_from_pdf(str(source), str(Path(directory, 'out.epub')))
            with zipfile.ZipFile(Path(directory, 'out.epub')) as archive:
                self.assertNotIn(b'Original page 1', archive.read('OEBPS/part0000.xhtml'))
                colors = []
                for name in archive.namelist():
                    if name.startswith('OEBPS/Images/'):
                        with Image.open(io.BytesIO(archive.read(name))) as image:
                            colors.extend(image.convert('RGB').get_flattened_data())
                self.assertTrue(any(b > r + 80 for r, g, b in colors))
        writer.close()

    def test_native_code_keeps_indentation_and_separates_following_prose(self):
        writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes()))
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Courier')})
        writer.pages[0]['/Resources']['/Font'][NameObject('/Code')] = writer._add_object(font)
        stream = DecodedStreamObject()
        stream.set_data(b'BT /Code 10 Tf 20 150 Td (def square\\(x\\):) Tj ET\n'
                        b'BT /Code 10 Tf 44 138 Td (return x * x) Tj ET\n'
                        b'BT /F1 10 Tf 20 120 Td (The explanation follows.) Tj ET\n')
        writer.pages[0][NameObject('/Contents')] = writer._add_object(stream)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'code.pdf')
            with source.open('wb') as output: writer.write(output)
            book_conversion.build_epub_from_pdf(str(source), str(Path(directory, 'out.epub')))
            with zipfile.ZipFile(Path(directory, 'out.epub')) as archive:
                text = archive.read('OEBPS/part0000.xhtml').decode()
                self.assertIn('def square(x):\n    return x * x</code></pre>', text)
                self.assertIn('The explanation follows.', text)
        writer.close()

    def test_widely_spaced_running_header_does_not_force_page_image(self):
        writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes()))
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 15 180 Td (CHAPTER) Tj ET\n'
                        b'BT /F1 12 Tf 175 180 Td (22) Tj ET\n'
                        b'BT /F1 12 Tf 20 150 Td (The body remains readable.) Tj ET\n')
        writer.pages[0][NameObject('/Contents')] = writer._add_object(stream)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'header.pdf')
            with source.open('wb') as output: writer.write(output)
            book_conversion.build_epub_from_pdf(str(source), str(Path(directory, 'out.epub')))
            with zipfile.ZipFile(Path(directory, 'out.epub')) as archive:
                text = archive.read('OEBPS/part0000.xhtml').decode()
                self.assertNotIn('Original page 1', text)
                self.assertNotIn('CHAPTER', text)
                self.assertIn('The body remains readable.', text)
        writer.close()

    def test_uncertain_ocr_keeps_visible_page_and_removes_scratch_png(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'scan.pdf'); scanned_pdf(source)
            with pdfium.PdfDocument(source) as pdf, closing(pdf[0]) as page:
                with patch('pdf_reading.shutil.which', return_value='tesseract'), patch('pdf_reading.subprocess.run') as run:
                    run.return_value.returncode = 0
                    run.return_value.stdout = b'<html></html>'
                    reading = ocr_page(page, directory, 0, 'eng')
            self.assertTrue(reading.fallback)
            self.assertTrue(Path(reading.blocks[0].source).is_file())
            self.assertFalse(Path(directory, 'ocr-0.png').exists())


if __name__ == '__main__':
    unittest.main()
