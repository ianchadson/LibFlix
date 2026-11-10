"""Conservative PDF text cleanup and selective, local Tesseract OCR."""
from collections import Counter
from dataclasses import dataclass, field, replace
import html
import os
import re
import shutil
import subprocess
import textwrap
from difflib import SequenceMatcher
from urllib.parse import urlsplit


@dataclass
class ReadingBlock:
    kind: str
    box: tuple
    text: str = ''
    confidence: float = 100
    source: str = ''
    role: str = ''


@dataclass
class ReadingPage:
    blocks: list = field(default_factory=list)
    size: tuple = (0, 0)
    fallback: bool = False
    reason: str = ''


def scan_pages_to_preserve(pages, scan_flags):
    """Avoid alternating printed pages and reflow in unreliable scanned books."""
    scans = [i for i, (page, scan) in enumerate(zip(pages, scan_flags)) if scan and page.blocks]
    if len(scans) < 3 or sum(pages[i].fallback for i in scans) < len(scans) * .5:
        return []
    return [i for i in scans if not pages[i].fallback]


def _box(tag):
    match = re.search(r'\bbbox (\d+) (\d+) (\d+) (\d+)', tag.get('title', ''))
    return tuple(map(int, match.groups())) if match else None


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _overlap(a, b):
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _header(block, size):
    return block.box[3] < size[1] * .14 and len(block.text) < 80


def unaccounted_ink(image, boxes):
    """Check the actual page pixels before throwing away any part of a scan.

    hOCR can miss artwork or mistake it for words. Unexplained dark marks cause
    an intact-page fallback. Bounded Pillow operations avoid a second ML model.
    """
    from PIL import ImageDraw
    with image.convert('L') as grey:
        ink = grey.point(lambda p: 255 if p < 150 else 0)
    try:
        draw = ImageDraw.Draw(ink)
        width, height = ink.size
        margin = max(2, int(min(width, height) * .012))
        for left, top, right, bottom in boxes:
            bounds = (max(0, left - 3), max(0, top - 3),
                      min(width - 1, right + 3), min(height - 1, bottom + 3))
            # OCR layers can contain wholly off-page characters. They must
            # neither crash Pillow nor explain visible pixels on the page.
            if bounds[0] <= bounds[2] and bounds[1] <= bounds[3]:
                draw.rectangle(bounds, fill=0)
        with ink.crop((margin, margin, width - margin, height - margin)) as middle:
            unexplained = middle.histogram()[255]
        return unexplained > max(180, width * height * .0005)
    finally:
        ink.close()


def recover_illustrations(image, blocks, words):
    """Locate substantial unexplained ink; keep its whole horizontal composition.

    Tesseract often reads diagram labels as words and fails to emit ocr_photo.
    A small bounded connected-component map finds the remaining strokes. Any
    text touching a recovered region stays in that crop, so labels and overlays
    cannot be reordered. The final ink check still rejects missed content.
    """
    from PIL import ImageDraw, Image
    pictures = [b.box for b in blocks if b.kind == 'image']
    with image.convert('L') as grey:
        mask = grey.point(lambda p: 255 if p < 150 else 0)
    try:
        draw = ImageDraw.Draw(mask)
        for left, top, right, bottom in words + pictures:
            draw.rectangle((max(0, left - 3), max(0, top - 3),
                            min(image.width - 1, right + 3), min(image.height - 1, bottom + 3)), fill=0)
        margin = max(2, int(min(image.size) * .012))
        draw.rectangle((0, 0, image.width, margin), fill=0)
        draw.rectangle((0, image.height - margin, image.width, image.height), fill=0)
        draw.rectangle((0, 0, margin, image.height), fill=0)
        draw.rectangle((image.width - margin, 0, image.width, image.height), fill=0)
        small = mask.resize((max(1, image.width // 8), max(1, image.height // 8)), Image.Resampling.BOX)
    finally:
        mask.close()
    try:
        width, height = small.size
        pixels = small.tobytes()
        remaining = {i for i, value in enumerate(pixels) if value >= 24}
        regions = []
        while remaining:
            seed = remaining.pop(); pending = [seed]; component = []
            while pending:
                index = pending.pop(); component.append(index)
                x, y = index % width, index // width
                for nx in range(max(0, x - 1), min(width, x + 2)):
                    for ny in range(max(0, y - 1), min(height, y + 2)):
                        neighbor = ny * width + nx
                        if neighbor in remaining:
                            remaining.remove(neighbor); pending.append(neighbor)
            xs = [i % width for i in component]; ys = [i // width for i in component]
            if len(component) >= 12 and max(xs) - min(xs) >= 3 and max(ys) - min(ys) >= 3:
                # Full-width bands retain the spatial relationship of separate
                # diagram parts and their labels, including text between parts.
                regions.append((0, max(0, (min(ys) - 1) * image.height / height),
                                image.width, min(image.height, (max(ys) + 2) * image.height / height)))
    finally:
        small.close()
    regions += pictures
    changed = True
    while changed:
        changed = False
        for i, region in enumerate(regions):
            for block in blocks:
                if _overlap(region, block.box) > 0:
                    expanded = _union(region, block.box)
                    if expanded != region:
                        regions[i] = region = expanded; changed = True
            for j in range(len(regions) - 1, i, -1):
                if _touches(region, regions[j], image.height * .015):
                    regions[i] = region = _union(region, regions.pop(j)); changed = True
    kept = [b for b in blocks if b.kind != 'image' and not any(_overlap(b.box, region) for region in regions)]
    for region in regions:
        # Keep OCR text/confidence attached to unchanged glyph crops so later
        # running-header cleanup can still identify them.
        original = next((b for b in blocks if b.kind == 'image' and b.box == region), None)
        kept.append(original or ReadingBlock('image', region))
    return kept


def read_hocr(data, image):
    """Adapt Tesseract's native text/image layout; never treat hOCR as HTML."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data, 'html.parser')
    page = ReadingPage(size=image.size)
    words = []
    for paragraph in soup.select('.ocr_par'):
        box = _box(paragraph)
        if not box or not _area(box):
            page.fallback, page.reason = True, 'Missing text geometry'
            continue
        lines, scores, doubtful = [], [], []
        for line in paragraph.select('.ocr_line, .ocr_caption, .ocr_header, .ocr_textfloat'):
            line_words = []
            for word in line.select('.ocrx_word'):
                value = word.get_text()
                word_box = _box(word)
                if word_box:
                    words.append(word_box)
                score = re.search(r'\bx_wconf (\d+)', word.get('title', ''))
                score_value = int(score[1]) if score else 0
                scores.extend([score_value] * max(1, len(value)))
                if len(value) >= 3 and score_value < 80:
                    doubtful.extend(value)
                line_words.append(value)
            value = ' '.join(line_words).strip()
            line_box = _box(line)
            # Keep footer geometry separate even when Tesseract appends the
            # page number to the last body paragraph (common in old scans).
            if (line_box and line_box[1] > image.height * .85 and
                    re.fullmatch(r'\s*\[?[\dilrzst:?. ]{1,6}\]?\s*', value, re.I)):
                page.blocks.append(ReadingBlock('text', line_box, value))
            else:
                lines.append(value)
        text = '\n'.join(lines).strip()
        if text:
            confidence = sum(scores) / max(1, len(scores))
            if confidence >= 70 and len(text) > 100 and len(doubtful) > max(3, len(text) * .02):
                # Partial paragraph crops can omit ink outside hOCR's uncertain
                # boxes. Keep the whole page, including every drawing and word.
                page.fallback, page.reason = True, 'Uncertain OCR paragraph geometry'
            # Ignore isolated low-confidence speckles at the outside edge. Their
            # word boxes still enter the ink check; body marks are never removed.
            marginal = box[2] < image.width * .07 or box[0] > image.width * .93
            if marginal and confidence < 60 and _area(box) < image.width * image.height * .001:
                continue
            # Decorative flourishes and uncertain short headings/captions
            # should retain their actual glyphs, not become OCR gibberish.
            if (confidence < 60 and len(text) < 12) or (70 <= confidence < 85 and len(text) < 100):
                page.blocks.append(ReadingBlock('image', box, text, confidence, role='ocr-glyph'))
            else:
                page.blocks.append(ReadingBlock('text', box, text, confidence))
    for photo in soup.select('.ocr_photo'):
        box = _box(photo)
        if box and _area(box):
            # Reject bad/out-of-page coordinates instead of silently clipping art.
            if not (0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height):
                page.fallback, page.reason = True, 'Invalid illustration geometry'
            else:
                page.blocks.append(ReadingBlock('image', box))
    # Tesseract may report an image and several nested fragments of that image.
    photos = [b for b in page.blocks if b.kind == 'image']
    page.blocks = [b for b in page.blocks if b.kind != 'image' or not any(
        other is not b and _area(other.box) > _area(b.box) and
        _overlap(other.box, b.box) >= _area(b.box) * .98 for other in photos)]
    if not unaccounted_ink(image, []):
        return ReadingPage(size=image.size)
    if not page.blocks:
        page.fallback, page.reason = True, 'No reliable text or illustration regions'
        return page
    if unaccounted_ink(image, words + [b.box for b in page.blocks if b.kind == 'image']):
        page.blocks = recover_illustrations(image, page.blocks, words)
    # Recovered header bands may combine several glyph crops. Keep their OCR
    # labels available to repeated-header cleanup without editing visible art.
    for block in page.blocks:
        if block.kind == 'image' and not block.text and block.box[3] < image.height * .14:
            labels = [word.get_text() for word in soup.select('.ocrx_word')
                      if _box(word) and _overlap(_box(word), block.box) >= _area(_box(word)) * .95]
            if labels:
                block.text = ' '.join(labels)
                block.role = 'ocr-glyph'
    pictures = [block.box for block in page.blocks if block.kind == 'image']
    text_blocks = [block for block in page.blocks if block.kind == 'text']
    from statistics import median
    word_height = median([b[3] - b[1] for b in words if _area(b)]) if words else 0
    for block in text_blocks:
        lines = block.text.splitlines()
        if (len(lines) >= 4 and sum(re.sub(r'^\W+', '', line)[:1].isupper() for line in lines) >= len(lines) * .65
                and median(map(len, lines)) < 50):
            block.role = 'verse'
        elif (image.height * .12 < block.box[1] < image.height * .75 and
              len(lines) == 1 and len(block.text) < 100 and
              len(block.text.split()) <= 8 and re.sub(r'^\W+', '', block.text)[:1].isupper() and not re.search(r'[,;:.!?]', block.text) and
              block.box[3] - block.box[1] > word_height * 1.35 and
              block.box[3] - block.box[1] < image.height * .08):
            block.role = 'heading'
        if block.confidence < 70 and len(block.text) > 12 and not _header(block, page.size):
            page.fallback, page.reason = True, 'Uncertain text recognition'
        if any(_overlap(block.box, photo) > _area(block.box) * .02 for photo in pictures):
            page.fallback, page.reason = True, 'Text overlaps artwork'
    # Parallel text columns/tables require a layout-preserving page. Text wrapped
    # beside an illustration is fine: the artwork stays at that point in the text.
    for i, a in enumerate(text_blocks):
        for b in text_blocks[i + 1:]:
            if _header(a, page.size) or _header(b, page.size):
                continue
            vertical = max(0, min(a.box[3], b.box[3]) - max(a.box[1], b.box[1]))
            separated = a.box[2] < b.box[0] or b.box[2] < a.box[0]
            if separated and vertical > min(a.box[3] - a.box[1], b.box[3] - b.box[1]) * .45:
                page.fallback, page.reason = True, 'Parallel columns or table'
    if unaccounted_ink(image, words + pictures):
        page.fallback, page.reason = True, 'Unclassified page content'
    # Geometric order, rather than hOCR's sometimes late image blocks, puts
    # artwork above/below/beside the relevant passage without fragile CSS floats.
    page.blocks.sort(key=lambda block: (block.box[1], block.box[0]))
    # Put an illustration at the start of its adjacent text, even when the
    # first text baseline is a few pixels above the picture's top edge.
    for picture in [b for b in page.blocks if b.kind == 'image']:
        adjacent = [b for b in page.blocks if b.kind == 'text' and
                    abs(b.box[1] - picture.box[1]) < image.height * .015]
        if adjacent:
            page.blocks.remove(picture)
            page.blocks.insert(min(page.blocks.index(b) for b in adjacent), picture)
    return page


def save_reading_image(image, directory, name):
    """Bounded JPEGs retain composed colour pages without PNG size explosions."""
    path = os.path.join(directory, name + '.jpg')
    with image.convert('RGB') as rgb:
        rgb.thumbnail((2000, 2400))
        rgb.save(path, 'JPEG', quality=88, optimize=True)
    return path


def render_page_image(page, directory, number):
    width, height = page.get_size()
    scale = min(200 / 72, (8_000_000 / max(1, width * height)) ** .5)
    bitmap = page.render(scale=scale)
    try:
        image = bitmap.to_pil()
        try:
            return save_reading_image(image, directory, f'page-{number:05d}')
        finally:
            image.close()
    finally:
        bitmap.close()


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _touches(a, b, gap=2):
    return (a[0] <= b[2] + gap and b[0] <= a[2] + gap and
            a[1] <= b[3] + gap and b[1] <= a[3] + gap)


def native_illustrated_page(page, directory, number):
    """Reflow native prose; composite graphical regions with their overlaid text.

    PDF text rectangles split at font changes, sometimes duplicating letters.
    Character runs retain the source order and identify paragraphs, headings
    and lists. Connected vector objects (tables, boxed exercises, diagrams)
    remain a single rendered region rather than flattening the entire page.
    """
    from contextlib import closing
    from statistics import median
    from pypdfium2 import raw
    width, height = page.get_size()
    reading = ReadingPage(size=(width, height))
    def intact(reason):
        saved = render_page_image(page, directory, number)
        return ReadingPage([ReadingBlock('image', (0, 0, width, height), source=saved)],
                           (width, height), True, reason)
    if page.get_rotation():
        return intact('Rotated native layout')
    def top_box(box):
        left, bottom, right, top = box
        return (left, height - top, right, height - bottom)
    regions = []
    # Top-level form bounds already include the placement matrix. Render the
    # whole form region; descending would mix parent and local coordinates.
    for obj in page.get_objects(max_depth=1):
        if obj.type in {raw.FPDF_PAGEOBJ_FORM, raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_SHADING}:
            left, top, right, bottom = top_box(obj.get_bounds())
            # Include stroked rules and the edge of clipping masks.
            regions.append((max(0, left - 1), max(0, top - 1),
                            min(width, right + 1), min(height, bottom + 1)))
    lines, chars, sizes, boxes, column_gaps, fixed, origins = [], [], [], [], [], [], []
    line_cells = {}
    import ctypes
    def finish_line():
        text = ''.join(chars).strip()
        if text and boxes:
            box = boxes[0]
            for other in boxes[1:]:
                box = _union(box, other)
            block = ReadingBlock('text', box, text, role='code' if sum(fixed) >= len(fixed) * .7 else ('columns' if column_gaps else ''))
            steps = [b - a for a, b in zip(origins, origins[1:]) if 0 < b - a < median(sizes) * 1.2]
            line_cells[id(block)] = median(steps) if steps else median(sizes) * .6
            lines.append((block, median(sizes) if sizes else 12))
        chars.clear(); sizes.clear(); boxes.clear(); column_gaps.clear(); fixed.clear(); origins.clear()
    with closing(page.get_textpage()) as textpage:
        if textpage.count_chars() > 1_000_000:
            return intact('Excessive native page text')
        for index in range(textpage.count_chars()):
            value = raw.FPDFText_GetUnicode(textpage, index)
            if value in {10, 13}:
                finish_line()
                continue
            if not value or value > 0x10ffff or 0xd800 <= value <= 0xdfff:
                if _area(textpage.get_charbox(index)):
                    return intact('Unmapped native glyph')
                continue
            char = chr(value)
            chars.append(char)
            origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
            raw.FPDFText_GetCharOrigin(textpage, index, ctypes.byref(origin_x), ctypes.byref(origin_y))
            origins.append(origin_x.value)
            if not char.isspace():
                box = top_box(textpage.get_charbox(index))
                font_size = raw.FPDFText_GetFontSize(textpage, index)
                if boxes and box[0] - boxes[-1][2] > max(font_size, sizes[-1]) * 3:
                    column_gaps.append(True)
                boxes.append(box)
                sizes.append(font_size)
                flags = ctypes.c_int()
                name = ctypes.create_string_buffer(256)
                raw.FPDFText_GetFontInfo(textpage, index, name, len(name), ctypes.byref(flags))
                # Some embedded TeX fonts omit the PDF fixed-pitch flag.
                font_name = name.value.decode('ascii', 'ignore').casefold()
                fixed.append(bool(flags.value & 1) or bool(re.search(
                    r'courier|mono|typewriter|(?:^|\+)(?:cmtt|sftt|lmtt)', font_name)))
        finish_line()
    # Underlined hyperlinks and running rules are text decoration, not pictures.
    # Treating each underline as artwork rasterises an otherwise ordinary line.
    def decoration(region):
        if region[3] - region[1] > 3.5:
            return False
        if region[3] < height * .1 or region[1] > height * .93:
            return True
        return any((len(line.text.split()) >= 3 or re.search(
                   r'(?:Figure|Table|Example)\s|\d+\.\d+[a-z]?[).]+$', line.text)) and
                   line.box[3] - 1.5 <= region[1] + 1 <= line.box[3] + 3 and
                   region[0] >= line.box[0] - 3 and region[2] <= line.box[2] + 3
                   for line, _ in lines)
    regions = [region for region in regions if not decoration(region)]
    # Disconnected equation glyphs/labels on the same row belong to one region.
    # Include all overlaid text, even when the glyphs are separated horizontally.
    changed = True
    while changed:
        changed = False
        for i, region in enumerate(regions):
            for j in range(len(regions) - 1, i, -1):
                other = regions[j]
                vertical_gap = max(region[1], other[1]) - min(region[3], other[3])
                if vertical_gap <= 2:
                    regions[i] = region = _union(region, regions.pop(j)); changed = True
            for line, _ in lines:
                if min(region[3], line.box[3]) > max(region[1], line.box[1]):
                    expanded = _union(region, line.box)
                    if expanded != region:
                        regions[i] = region = expanded; changed = True
    # Merge connected graphics and any text that crosses their boundary. A
    # caption wholly outside stays selectable; overlaid labels stay with art.
    changed = True
    while changed:
        changed = False
        for index, region in enumerate(regions):
            for line, _ in lines:
                if _overlap(region, line.box) > 0 and _overlap(region, line.box) < _area(line.box):
                    expanded = _union(region, line.box)
                    if expanded != region:
                        regions[index] = region = expanded
                        changed = True
            for other in range(len(regions) - 1, index, -1):
                if _touches(region, regions[other]):
                    regions[index] = region = _union(region, regions.pop(other))
                    changed = True
    outside = [(line, size) for line, size in lines if not any(
        _overlap(line.box, region) >= _area(line.box) * .98 for region in regions)]
    # Disjoint columns must not be interleaved into a single sentence.
    for index, (line, _) in enumerate(outside):
        if line.role == 'columns' and not (line.box[3] < height * .12 or line.box[1] > height * .9):
            return intact('Parallel native text columns')
        if not (0 <= line.box[0] < line.box[2] <= width and 0 <= line.box[1] < line.box[3] <= height):
            return intact('Invalid native text geometry')
        for other, _ in outside[index + 1:]:
            vertical = min(line.box[3], other.box[3]) - max(line.box[1], other.box[1])
            if vertical > min(line.box[3] - line.box[1], other.box[3] - other.box[1]) * .45:
                return intact('Parallel native text columns')
    outside.sort(key=lambda entry: (entry[0].box[1], entry[0].box[0]))
    body_size = median([size for line, size in outside for _ in range(min(200, len(line.text)))]) if outside else 12
    outside = [(line, size) for line, size in outside if not (
        ((line.box[3] < height * .07 and size < body_size * .9) or
         (line.box[3] < height * .12 and line.role == 'columns'))
        and re.search(r'\d', line.text))]
    previous_size = 0
    for line, size in outside:
        line.role = 'code' if line.role == 'code' else ('heading' if (line.box[1] > height * .07 and size > body_size * 1.16
                                  and len(line.text) < 180 and not re.search(r'[.!?:;]$', line.text)) else (
            'list' if re.match(r'^(?:[•●▪]\s*|\d+[.)]\s+)', line.text) else ''))
        previous = reading.blocks[-1] if reading.blocks else None
        gap = line.box[1] - previous.box[3] if previous else 0
        between = previous and any(region[1] >= previous.box[3] - 1 and region[3] <= line.box[1] + 1 for region in regions)
        if previous and previous.role == line.role == 'code' and gap < body_size * 2:
            # Reconstruct leading indentation from the printed monospaced grid.
            unit = max(1, line_cells[id(line)])
            if line.box[0] < previous.box[0]:
                indent = ' ' * max(0, round((previous.box[0] - line.box[0]) / unit))
                previous.text = '\n'.join(indent + value for value in previous.text.splitlines())
            indent = ' ' * max(0, round((line.box[0] - min(previous.box[0], line.box[0])) / unit))
            previous.text += ('\n\n' if gap > body_size * .85 else '\n') + indent + line.text
            previous.box = _union(previous.box, line.box)
        elif (previous and not line.role and previous.role in {'', 'list'} and not between and
                abs(size - previous_size) < body_size * .2 and gap < body_size * .85 and
                gap > -1 and (line.box[0] <= previous.box[0] + body_size * .8 or previous.role == 'list')):
            previous.text += '\n' + line.text
            previous.box = _union(previous.box, line.box)
        else:
            reading.blocks.append(line)
        previous_size = size
    # A page with many inline equations or styled text fragments cannot reflow
    # coherently as alternating tiny pictures and prose. Keep the complete
    # printed composition instead of producing mismatched sizes or ordering.
    inline_regions = [region for region in regions if
                      region[3] - region[1] < height * .045 and
                      region[2] - region[0] > width * .25]
    if len(inline_regions) >= 4:
        return intact('Dense inline mathematics or styled composition')
    if regions:
        scale = min(200 / 72, (8_000_000 / max(1, width * height)) ** .5)
        bitmap = page.render(scale=scale)
        try:
            image = bitmap.to_pil()
            try:
                for index, region in enumerate(regions):
                    if not _area(region):
                        continue
                    crop_box = tuple(value * scale for value in region)
                    with image.crop(crop_box) as crop:
                        saved = save_reading_image(crop, directory, f'art-{number:05d}-{index}')
                    reading.blocks.append(ReadingBlock('image', region, source=saved))
            finally:
                image.close()
        finally:
            bitmap.close()
    reading.blocks.sort(key=lambda block: (block.box[1], block.box[0]))
    return reading


def remove_running_headers(pages):
    candidates = Counter()
    def key(text):
        return re.sub(r'[^a-z\u4e00-\u9fff]+', '', text.casefold())
    def eligible(block):
        return block.kind == 'text' and not block.role or block.role == 'ocr-glyph'
    def edge_lines(block, page):
        lines = block.text.splitlines()
        if not lines:
            return []
        return ([lines[0]] if block.box[1] < page.size[1] * .12 else []) + (
            [lines[-1]] if block.box[3] > page.size[1] * .85 else [])
    for page in pages:
        if not page.fallback:
            candidates.update({key(line) for block in page.blocks if eligible(block)
                               for line in edge_lines(block, page) if len(line) < 80 and key(line)})
    threshold = max(2, len(pages) * .1)
    repeated = {value for value in candidates if sum(count for other, count in candidates.items()
                if SequenceMatcher(None, value, other).ratio() >= .75) >= threshold}
    def furniture(line):
        value = key(line)
        return (value in repeated or any(len(other) >= 5 and other in value for other in repeated)
                or re.fullmatch(r'\s*(?:\[?\d{1,5}\]?|[\[\(][a-z0-9? ]{1,5}[\]\)]|'
                                r'[a-z]?\s*\d{1,4}\s*[a-z]\s*\d{0,2})\s*', line, re.I))
    for page in pages:
        if page.fallback:
            continue
        has_header = any(furniture(line) for block in page.blocks if eligible(block)
                         for line in edge_lines(block, page))
        kept = []
        for block in page.blocks:
            if eligible(block):
                lines = block.text.splitlines()
                if lines and block.box[1] < page.size[1] * .12 and furniture(lines[0]):
                    lines.pop(0)
                if lines and block.box[3] > page.size[1] * .85 and furniture(lines[-1]):
                    lines.pop()
                if block.kind == 'text':
                    block.text = '\n'.join(lines)
                elif not lines:
                    continue
                if not block.text or (has_header and block.box[3] < page.size[1] * .12 and
                                       (block.confidence < 60 or block.role == 'ocr-glyph') and len(key(block.text)) < 3):
                    continue
            kept.append(block)
        page.blocks = kept


def clean_pages(pages, *, edge_cleanup=True):
    """Remove repeated edge furniture, repair wrapped words, retain paragraph gaps."""
    lines = [[line.rstrip() for line in text.replace('\u00ad', '').splitlines()] for text in pages]
    counts = Counter()
    for page in lines:
        nonempty = [line.strip() for line in page if line.strip()]
        edge = [nonempty[0], nonempty[-1]] if len(nonempty) >= 4 else []
        counts.update(set(edge))
    repeated = {line for line, count in counts.items()
                if len(lines) >= 3 and count >= max(3, (len(lines) + 1) // 2) and len(line) < 120}
    output = []
    for page in lines:
        nonempty = [i for i, line in enumerate(page) if line.strip()]
        edges = set(nonempty[:1] + nonempty[-1:])
        kept = [line for i, line in enumerate(page) if not (
            edge_cleanup and i in edges and (line.strip() in repeated or (len(lines) > 1 and re.fullmatch(r'(?:Page\s+)?\d{1,5}', line.strip(), re.I))))]
        text = textwrap.dedent('\n'.join(kept))
        # A visibly indented line often starts a new paragraph in printed prose.
        text = re.sub(r'(?m)^(?: {2,}|\t+)(?=\S)', '\n', text)
        text = re.sub(r'([a-zA-Z])[-\u2010]\n(?=[a-z])', r'\1', text)
        paragraphs = []
        for paragraph in re.split(r'\n\s*\n', text.strip()):
            if paragraph.strip():
                paragraphs.append(re.sub(r'\s*\n\s*', ' ', paragraph).strip())
        output.append(paragraphs)
    previous = []
    for current in output:
        if previous and current and current[0][0].islower() and not re.search(r'[.!?:;][\"\u201d\u2019]?$', previous[-1]):
            separator = '' if previous[-1].endswith('-') else ' '
            previous[-1] = previous[-1].removesuffix('-') + separator + current.pop(0)
        if current:
            previous = current
    return output


def join_page_continuations(pages):
    """Keep a paragraph/list item together when a printed page divides it."""
    previous = None
    for page in pages:
        if not page.blocks:
            continue
        first = page.blocks[0]
        if (previous and previous.kind == first.kind == 'text' and
                previous.role in {'', 'list'} and not first.role and
                first.text[:1].islower() and
                not re.search(r'[.!?:;][\"\u201d\u2019]?$', previous.text)):
            previous.text += '\n' + first.text
            page.blocks.pop(0)
        if page.blocks:
            previous = page.blocks[-1]


def flow_blocks(blocks, previous_text=''):
    """Keep movable art between sentences when a PDF page splits a paragraph."""
    ordered = list(blocks)
    for index in range(len(ordered)):
        block = ordered[index]
        if block.kind == 'image' and previous_text and not re.search(r'[.!?:;][\"\u201d\u2019]?$', previous_text):
            following = ordered[index + 1:]
            if following and following[0].kind == 'text' and following[0].text[:1].islower():
                for offset, candidate in enumerate(following, 1):
                    if candidate.kind != 'text' or candidate.role:
                        break
                    sentence = None
                    for boundary in re.finditer(r'[.!?][\"\u201d\u2019]?(?=\s|$)', candidate.text):
                        word = re.search(r'([A-Za-z]+)\.$', candidate.text[:boundary.end()])
                        if word and (len(word[1]) == 1 or word[1].casefold() in {
                                'fig', 'figs', 'mr', 'mrs', 'ms', 'dr', 'prof', 'no', 'nos',
                                'st', 'vs', 'etc', 'vol', 'pp', 'approx'}):
                            continue
                        sentence = boundary
                        break
                    if sentence and sentence.end() < len(candidate.text):
                        # A PDF block can contain the end of this sentence
                        # followed by several new sentences. Move art after
                        # that first sentence, not into the middle of it.
                        prefix = replace(candidate, text=candidate.text[:sentence.end()])
                        rest = replace(candidate, text=candidate.text[sentence.end():].lstrip())
                        position = index + offset
                        ordered[position:position + 1] = [prefix, rest]
                        ordered.insert(position, ordered.pop(index))
                        break
                    if re.search(r'[.!?:;][\"\u201d\u2019]?$', candidate.text):
                        ordered.insert(index + offset, ordered.pop(index))
                        break
        if ordered[index].kind == 'text':
            previous_text = ordered[index].text
        else:
            previous_text = ''
    return ordered


def scan_coverage(pdf_page):
    """Large page images identify scans, including scans with existing OCR layers."""
    from pypdfium2 import raw
    width, height = pdf_page.get_size()
    area = max(1, width * height)
    covered = 0
    for obj in pdf_page.get_objects(max_depth=8):
        if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
            left, bottom, right, top = obj.get_bounds()
            covered += max(0, min(width, right) - max(0, left)) * max(0, min(height, top) - max(0, bottom))
    return covered / area >= 0.65



def ocr_page(page, directory, number, language):
    """Return text and illustration blocks, or one intact page if uncertain."""
    engine = shutil.which('tesseract')
    if not engine:
        raise RuntimeError('This scanned PDF needs Tesseract OCR installed on the server.')
    # Render a bounded full page so rotation, masks and tiled scans are honoured.
    width, height = page.get_size()
    scale = min(300 / 72, (12_000_000 / max(1, width * height)) ** 0.5)
    path = os.path.join(directory, f'ocr-{number}.png')
    bitmap = page.render(scale=scale)
    try:
        image = bitmap.to_pil()
        try:
            image.save(path)
        finally:
            image.close()
    finally:
        bitmap.close()
    try:
        env = dict(os.environ, OMP_THREAD_LIMIT='1')
        result = subprocess.run([engine, path, 'stdout', '-l', language, '--dpi', '300',
                                 '-c', 'tessedit_create_hocr=1'],
                                capture_output=True, timeout=60, env=env, check=False)
        if result.returncode:
            raise RuntimeError('OCR failed; check that the configured Tesseract language data is installed.')
        from PIL import Image
        with Image.open(path) as image:
            reading = read_hocr(result.stdout, image)
            if reading.fallback or not reading.blocks:
                reading.fallback = True
                reading.reason = reading.reason or 'No reliable text or illustration regions'
                saved = save_reading_image(image, directory, f'page-{number:05d}')
                reading.blocks = [ReadingBlock('image', (0, 0, image.width, image.height), source=saved)]
            else:
                for index, block in enumerate(reading.blocks):
                    if block.kind == 'image':
                        with image.crop(block.box) as crop:
                            block.source = save_reading_image(crop, directory, f'art-{number:05d}-{index}')
            return reading
    finally:
        os.unlink(path)


def existing_scan_page(page, text, directory, number):
    """Reuse aligned existing OCR only when it accounts for the visible ink.

    A pre-existing text layer alone says nothing about illustrations. Preserve
    the complete composition when non-text ink or dubious geometry remains.
    """
    from contextlib import closing
    width, height = page.get_size()
    scale = min(160 / 72, (8_000_000 / max(1, width * height)) ** .5)
    bitmap = page.render(scale=scale)
    try:
        image = bitmap.to_pil()
        try:
            boxes = []
            with closing(page.get_textpage()) as textpage:
                for index in range(textpage.count_rects()):
                    left, bottom, right, top = textpage.get_rect(index)
                    boxes.append((left * scale, (height - top) * scale,
                                  right * scale, (height - bottom) * scale))
            uncertain = not boxes or any(_area(box) > image.width * image.height * .2 for box in boxes)
            if uncertain or unaccounted_ink(image, boxes):
                saved = save_reading_image(image, directory, f'page-{number:05d}')
                return ReadingPage([ReadingBlock('image', (0, 0, *image.size), source=saved)],
                                   image.size, True, 'Existing OCR does not account for page artwork')
            return ReadingPage([ReadingBlock('text', (0, 0, *image.size), text)], image.size)
        finally:
            image.close()
    finally:
        bitmap.close()


def inline_html(text):
    """Link literal web addresses without changing text or guessing broken URLs."""
    output = []
    end = 0
    for match in re.finditer(r'https?://[^\s<>"\x00-\x1f]+', text):
        url = match.group().rstrip('.,;:!?')
        while url.endswith(')') and url.count(')') > url.count('('):
            url = url[:-1]
        while url.endswith(']') and url.count(']') > url.count('['):
            url = url[:-1]
        try:
            parts = urlsplit(url)
            valid = bool(parts.hostname) and not parts.username and not parts.password
            # Accessing port also rejects malformed/out-of-range values.
            parts.port
        except ValueError:
            valid = False
        if not valid:
            continue
        output.append(html.escape(text[end:match.start()]))
        output.append('<a href="' + html.escape(url, quote=True) + '">'
                      + html.escape(url) + '</a>')
        end = match.start() + len(url)
    output.append(html.escape(text[end:]))
    return ''.join(output)


def paragraph_html(paragraphs):
    output = []
    for paragraph in paragraphs:
        tag = 'h2' if len(paragraph) < 80 and re.match(r'^(?:chapter|part)\s+\w+|^(?:prologue|epilogue)$', paragraph, re.I) else 'p'
        output.append(f'<{tag}>' + inline_html(paragraph) + f'</{tag}>')
    return ''.join(output)
