"""Conservative PDF text cleanup and selective, local Tesseract OCR."""
from collections import Counter
import html
import os
import re
import shutil
import subprocess
import textwrap


def clean_pages(pages):
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
            i in edges and (line.strip() in repeated or (len(lines) > 1 and re.fullmatch(r'(?:Page\s+)?\d{1,5}', line.strip(), re.I))))]
        text = textwrap.dedent('\n'.join(kept))
        # A visibly indented line often starts a new paragraph in printed prose.
        text = re.sub(r'(?m)^(?: {2,}|\t+)(?=\S)', '\n', text)
        text = re.sub(r'([a-zA-Z])[-\u2010]\n(?=[a-z])', r'\1', text)
        paragraphs = []
        for paragraph in re.split(r'\n\s*\n', text.strip()):
            if paragraph.strip():
                paragraphs.append(re.sub(r'\s*\n\s*', ' ', paragraph).strip())
        output.append(paragraphs)
    for index in range(1, len(output)):
        previous, current = output[index - 1], output[index]
        if previous and current and current[0][0].islower() and not re.search(r'[.!?:;][\"\u201d\u2019]?$', previous[-1]):
            separator = '' if previous[-1].endswith('-') else ' '
            previous[-1] = previous[-1].removesuffix('-') + separator + current.pop(0)
    return output


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
    """Return reflowable paragraphs; never embed the full scan beside its OCR text."""
    engine = shutil.which('tesseract')
    if not engine:
        raise RuntimeError('This scanned PDF needs Tesseract OCR installed on the server.')
    # Render a bounded full page so rotation, masks and tiled scans are honoured.
    width, height = page.get_size()
    scale = min(200 / 72, (12_000_000 / max(1, width * height)) ** 0.5)
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
        result = subprocess.run([engine, path, 'stdout', '-l', language, '--dpi', '200', 'hocr'],
                                capture_output=True, timeout=60, env=env, check=False)
        if result.returncode:
            raise RuntimeError('OCR failed; check that the configured Tesseract language data is installed.')
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(result.stdout, 'html.parser')
        paragraphs = []
        for paragraph in soup.select('.ocr_par'):
            lines = [' '.join(word.get_text() for word in line.select('.ocrx_word'))
                     for line in paragraph.select('.ocr_line')]
            text = '\n'.join(line for line in lines if line.strip())
            if text:
                paragraphs.append(text)
        return '\n\n'.join(paragraphs)
    finally:
        os.unlink(path)


def paragraph_html(paragraphs):
    output = []
    for paragraph in paragraphs:
        tag = 'h2' if len(paragraph) < 80 and re.match(r'^(?:chapter|part)\s+\w+|^(?:prologue|epilogue)$', paragraph, re.I) else 'p'
        output.append(f'<{tag}>' + html.escape(paragraph) + f'</{tag}>')
    return ''.join(output)
