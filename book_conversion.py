"""Convert PDF and legacy Kindle formats (MOBI / AZW3) to EPUB.

PDF editions and legacy MOBI/AZW3 editions become EPUBs before Kindle
preparation and delivery. Original-format website downloads remain available.

The existing pure-Python ``pypdf`` and ``mobi`` packages are used instead of
Calibre so scanned pages use the local Tesseract OCR engine. The original file is never modified:
extraction writes to
a private temporary directory, and only a validated EPUB (ZIP with a mimetype
entry) is returned.  Any failure yields an empty result with a warning; the
caller must not silently substitute the original file after a failed conversion.
"""

from __future__ import annotations

import html
import signal
import json
import subprocess
import sys
import time
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from contextlib import closing
from dataclasses import dataclass
from typing import Optional

from book_preparation import normalize_language

CONVERTIBLE_EXTENSIONS = frozenset({"pdf", "mobi", "azw3"})
CONVERTIBLE_TARGET = "epub"
PDF_MAX_BYTES = 25 * 1024 * 1024
PDF_MAX_PAGES = 2000
PDF_MAX_TEXT = 10_000_000
PDF_MAX_IMAGE_PIXELS = 50_000_000
PDF_MAX_TOTAL_IMAGE_PIXELS = 1_000_000_000
PDF_MAX_IMAGE_BYTES = 512 * 1024 * 1024
PDF_WORKER_MEMORY_BYTES = 1024**3
PDF_WORKER_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class ConversionResult:
    path: str
    extension: str
    temporary: bool = False
    warning: str = ""


def is_convertible(extension: str) -> bool:
    return re.sub(r"[^a-z0-9]", "", str(extension or "").casefold()) in CONVERTIBLE_EXTENSIONS


def converter_available() -> bool:
    """True when the ``mobi`` package is importable in this deployment."""
    try:
        import mobi  # type: ignore  # noqa: F401
    except ImportError:
        return False
    return True


def _valid_epub(path: str) -> bool:
    if not path or not os.path.isfile(path) or not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            if "mimetype" not in names:
                return False
            if archive.read("mimetype").strip() != b"application/epub+zip":
                return False
            return "META-INF/container.xml" in names
    except (OSError, zipfile.BadZipFile, KeyError):
        return False


def convert_to_epub(source_path: str, extension: str) -> ConversionResult:
    """Return an EPUB path for a PDF/MOBI/AZW3 file, or an empty failure."""
    extension = re.sub(r"[^a-z0-9]", "", str(extension or "").casefold())
    if not is_convertible(extension):
        return ConversionResult("", "")
    if not source_path or not os.path.isfile(source_path):
        return ConversionResult("", "", warning="Source file is missing")

    workdir = tempfile.mkdtemp(prefix="libflix-convert-")
    output_path = os.path.join(workdir, "converted.epub")
    extraction_root = ""
    converted = False
    try:
        if extension == "pdf":
            _run_pdf_worker(source_path, output_path)
            if not _valid_epub(output_path):
                raise ValueError("Converted EPUB failed validation")
            converted = True
            return ConversionResult(output_path, CONVERTIBLE_TARGET, temporary=True)
        try:
            import mobi  # type: ignore
        except ImportError:
            return ConversionResult(
                "", "", warning="mobi converter is not installed"
            )

        extracted = mobi.extract(source_path)
        candidates = (
            [item for item in extracted if isinstance(item, str)]
            if isinstance(extracted, (tuple, list))
            else [extracted] if isinstance(extracted, str) else []
        )
        extraction_root = next(
            (root for root in map(_extraction_root, candidates) if root), ""
        )
        candidate = next((item for item in candidates if _valid_epub(item)), "")
        if candidate:
            shutil.copyfile(candidate, output_path)
        else:
            # Older (KF7) MOBI files unpack to HTML + images, not an EPUB.
            html_path = next(
                (
                    item for item in candidates
                    if item.lower().endswith((".html", ".htm")) and os.path.isfile(item)
                ),
                "",
            )
            if not html_path:
                return ConversionResult("", "", warning="Converter produced no usable EPUB")
            build_epub_from_mobi7(html_path, output_path)
        if not _valid_epub(output_path):
            return ConversionResult("", "", warning="Converted EPUB failed validation")
        converted = True
        return ConversionResult(output_path, CONVERTIBLE_TARGET, temporary=True)
    except Exception as error:  # never let conversion break delivery
        return ConversionResult(
            "", "", warning=f"{type(error).__name__}: {error}"
        )
    finally:
        _remove_tree(extraction_root)
        if not converted:
            _remove_tree(workdir)


def _mac_resident_bytes(pid):
    import ctypes
    # Darwin sys/proc_info.h: proc_taskinfo is six uint64s, twelve int32s.
    info = ctypes.create_string_buffer(96)
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
    size = library.proc_pidinfo(pid, 4, 0, info, len(info))
    if size != len(info):
        # A worker may exit between communicate's timeout and this read.
        if ctypes.get_errno() == 3:  # ESRCH
            return 0
        raise RuntimeError("Cannot monitor PDF conversion memory")
    return ctypes.c_uint64.from_buffer(info, 8).value


def _worker_resident_bytes(group):
    """Total RSS of the conversion worker and its OCR subprocesses."""
    if sys.platform == "darwin":
        import ctypes
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        members = (ctypes.c_int * 128)()
        size = library.proc_listpgrppids(group, members, ctypes.sizeof(members))
        if size <= 0:
            return _mac_resident_bytes(group)
        return sum(_mac_resident_bytes(pid) for pid in members[:size] if pid)
    total = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as source:
                fields = source.read().rsplit(")", 1)[1].split()
            if int(fields[2]) == group:
                with open(f"/proc/{name}/statm") as source:
                    total += int(source.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (FileNotFoundError, ProcessLookupError):
            pass
    return total


def _run_pdf_worker(source_path: str, output_path: str) -> None:
    """Isolate parsing/decoding; the parent owns and removes the entire workspace."""
    if os.path.getsize(source_path) > PDF_MAX_BYTES:
        raise ValueError("PDF is too large to convert (maximum 25 MB)")
    limits = {name: value for name, value in globals().items() if name.startswith("PDF_")}
    command = [sys.executable, os.path.abspath(__file__), "--pdf-worker",
               os.path.abspath(source_path), os.path.abspath(output_path), json.dumps(limits)]
    deadline = time.monotonic() + PDF_WORKER_TIMEOUT_SECONDS
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True) as worker:
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("PDF conversion exceeded the 180 second time limit")
                try:
                    _, error = worker.communicate(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    if _worker_resident_bytes(worker.pid) > PDF_WORKER_MEMORY_BYTES:
                        raise MemoryError("PDF conversion exceeded the 1 GB memory limit")
            if worker.returncode:
                detail = error.decode("utf-8", "replace").strip()[-1000:]
                raise RuntimeError(detail or "PDF conversion worker stopped (memory/resource limit)")
        finally:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.communicate()


def _preflight_pdf_images(page, budget):
    """Inspect raw XObjects (including Forms/masks) before pypdf decodes images.

    Inline images and misleading metadata still need the isolated worker limit.
    Count shared objects again on each page because output repeats those images.
    """
    seen = set()
    def visit(reference, depth=0):
        if depth > 64:
            raise ValueError("PDF image resource nesting is too deep")
        obj = reference.get_object()
        identity = id(obj)
        if identity in seen:
            return
        seen.add(identity)
        if obj.get("/Subtype") == "/Image":
            width, height = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
            pixels = width * height
            if width <= 0 or height <= 0 or pixels > PDF_MAX_IMAGE_PIXELS:
                raise ValueError("PDF image is too large to convert (maximum 50 million pixels)")
            budget[0] += pixels
            if budget[0] > PDF_MAX_TOTAL_IMAGE_PIXELS:
                raise ValueError("PDF has too many image pixels to convert")
            for name in ("/SMask", "/Mask"):
                mask = obj.get(name)
                if mask is not None and hasattr(mask.get_object(), "get"):
                    visit(mask, depth + 1)
        resources = obj.get("/Resources")
        if resources:
            objects = resources.get_object().get("/XObject", {})
            for child in objects.get_object().values() if hasattr(objects, "get_object") else objects.values():
                visit(child, depth + 1)
    visit(page)


def _xml_text(value: str) -> str:
    # PDF metadata/text may contain control characters forbidden in XML 1.0.
    return "".join(
        char for char in str(value or "")
        if char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF
    )


def build_epub_from_pdf(source_path: str, output_path: str) -> None:
    """Reflow safe text/illustration layouts, preserve uncertain pages intact.

    PDFium composes masks, vector graphics, rotation and image layers before
    anything is exported. Tesseract layout separates scan artwork from prose.
    Reuse the existing XHTML/OPF/NCX packager; never modify the source PDF.
    """
    from pypdf import PdfReader
    from pdf_reading import (clean_pages, scan_coverage, ocr_page, paragraph_html, inline_html,
                             ReadingPage, ReadingBlock, render_page_image,
                             existing_scan_page, remove_running_headers,
                             native_illustrated_page, flow_blocks, join_page_continuations,
                             scan_pages_to_preserve)
    import pypdfium2 as pdfium

    if os.path.getsize(source_path) > PDF_MAX_BYTES:
        raise ValueError("PDF is too large to convert (maximum 25 MB)")
    with tempfile.TemporaryDirectory(prefix="libflix-pdf-", dir=os.path.dirname(os.path.abspath(output_path))) as directory:
        image_dir = os.path.join(directory, "Images")
        os.makedirs(image_dir)
        html_path = os.path.join(directory, "book.html")
        with open(source_path, "rb") as source, open(html_path, "w", encoding="utf-8") as document:
            reader = PdfReader(source)
            if reader.is_encrypted:
                raise ValueError("Password-protected PDFs cannot be converted")
            if not 0 < len(reader.pages) <= PDF_MAX_PAGES:
                raise ValueError("PDF conversion supports 1 to 2000 pages")
            metadata = reader.metadata or {}
            title = _xml_text(metadata.get("/Title") or "Book")
            author = _xml_text(metadata.get("/Author") or "")
            # PDFs commonly omit /Lang. Kindle rejects the syntactically valid
            # BCP 47 placeholder "und", so use the packager's existing English
            # default when no useful source language is available.
            language = normalize_language(reader.trailer['/Root'].get('/Lang')) or "en"
            text_count = image_bytes = 0
            pixel_budget = [0]
            pages, scan_flags = [], []
            with pdfium.PdfDocument(source_path) as rendered:
                for index, page in enumerate(reader.pages):
                    _preflight_pdf_images(page, pixel_budget)
                    text = _xml_text(page.extract_text(extraction_mode="layout") or "")
                    with closing(rendered[index]) as rendered_page:
                        scan = scan_coverage(rendered_page)
                        scan_flags.append(scan)
                        if scan and os.environ.get('LIBFLIX_PDF_OCR_ENABLED', '1') == '0':
                            path = render_page_image(rendered_page, image_dir, index)
                            reading = ReadingPage([ReadingBlock('image', (0, 0, 0, 0), source=path)],
                                                  fallback=True, reason='OCR disabled')
                        elif scan and len(re.sub(r"\s", "", text)) < 40:
                            ocr_language = os.environ.get("LIBFLIX_OCR_LANGUAGES", "eng")
                            if not re.fullmatch(r"[A-Za-z0-9_+]+", ocr_language):
                                raise ValueError("Invalid OCR language configuration")
                            reading = ocr_page(rendered_page, image_dir, index, ocr_language)
                        elif scan:
                            reading = existing_scan_page(rendered_page, text, image_dir, index)
                            if reading.fallback:
                                # Old hidden OCR can be misaligned or omit drawings.
                                # Reanalyse the composed page when the local engine
                                # is available; retain the intact page otherwise.
                                if shutil.which('tesseract'):
                                    language_config = os.environ.get('LIBFLIX_OCR_LANGUAGES', 'eng')
                                    if not re.fullmatch(r'[A-Za-z0-9_+]+', language_config):
                                        raise ValueError('Invalid OCR language configuration')
                                    previous_images = {b.source for b in reading.blocks if b.source}
                                    reading = ocr_page(rendered_page, image_dir, index, language_config)
                                    for unused in previous_images - {b.source for b in reading.blocks}:
                                        os.unlink(unused)
                        else:
                            reading = native_illustrated_page(rendered_page, image_dir, index)
                    text_count += sum(len(block.text) for block in reading.blocks)
                    if text_count > PDF_MAX_TEXT:
                        raise ValueError("PDF contains too much text to convert")
                    for block in reading.blocks:
                        block.text = _xml_text(block.text)
                        if block.source:
                            image_bytes += os.path.getsize(block.source)
                            if image_bytes > PDF_MAX_IMAGE_BYTES:
                                raise ValueError("PDF images are too large to convert")
                    pages.append(reading)
                for index in scan_pages_to_preserve(pages, scan_flags):
                    # Mutate the existing page so geometry consumers also see
                    # the final representation. Unused crops are removed below.
                    with closing(rendered[index]) as rendered_page:
                        path = render_page_image(rendered_page, image_dir, index)
                    reading = pages[index]
                    reading.blocks = [ReadingBlock('image', (0, 0, *reading.size), source=path)]
                    reading.fallback, reading.reason = True, 'Consistent layout for uncertain scanned book'
                image_bytes = sum(os.path.getsize(block.source) for reading in pages
                                  for block in reading.blocks if block.source)
                if image_bytes > PDF_MAX_IMAGE_BYTES:
                    raise ValueError("PDF images are too large to convert")
            remove_running_headers(pages)
            join_page_continuations(pages)
            used_images = {block.source for reading in pages for block in reading.blocks if block.source}
            for name in os.listdir(image_dir):
                path = os.path.join(image_dir, name)
                if path not in used_images:
                    os.unlink(path)
            # Preserve cross-page paragraph/word repair for ordinary native PDFs.
            # Images and complex pages interrupt a run so prose never jumps art.
            run = []
            def finish_run():
                if run:
                    for reading, paragraphs in zip(run, clean_pages([p.blocks[0].text for p in run])):
                        reading.blocks[0].text = '\n\n'.join(paragraphs)
                    run.clear()
            for reading in pages:
                if reading.size == (0, 0) and len(reading.blocks) == 1 and reading.blocks[0].kind == 'text':
                    run.append(reading)
                else:
                    finish_run()
            finish_run()
            has_content = False
            document.write('<html><body><div style="overflow-wrap:anywhere;word-wrap:break-word;">')
            text_run = []
            def write_text_run():
                if text_run:
                    for paragraphs in clean_pages(text_run, edge_cleanup=False):
                        document.write(paragraph_html(paragraphs))
                    text_run.clear()
            for number, reading in enumerate(pages, 1):
                for block in flow_blocks(reading.blocks, text_run[-1] if text_run else ''):
                    if block.kind == 'text':
                        if block.text.strip():
                            has_content = True
                            if block.role:
                                write_text_run()
                                value = ' '.join(clean_pages([block.text], edge_cleanup=False)[0])
                                if block.role == 'heading':
                                    document.write('<h2>' + inline_html(value) + '</h2>')
                                elif block.role == 'list':
                                    # Keep the printed marker (including ordered
                                    # numbering) and a hanging indent on reflow.
                                    document.write('<p style="margin-left:1.2em;text-indent:-1.2em;">'
                                                   + inline_html(value) + '</p>')
                                elif block.role == 'code':
                                    document.write('<pre style="white-space:pre-wrap;overflow-wrap:anywhere;">'
                                                   '<code>' + html.escape(block.text) + '</code></pre>')
                                elif block.role == 'verse':
                                    document.write('<p>' + '<br/>'.join(inline_html(line) for line in
                                                                      block.text.splitlines()) + '</p>')
                            else:
                                text_run.append(block.text)
                    elif block.source:
                        write_text_run()
                        name = os.path.basename(block.source)
                        label = f'Original page {number}' if reading.fallback else f'Illustration on page {number}'
                        has_content = True
                        document.write(f'<p style="text-align:center;page-break-inside:avoid;"><img '
                                       f'src="Images/{name}" alt="{label}" '
                                       'style="max-width:100%;height:auto;"/></p>')
            write_text_run()
            document.write("</div></body></html>")
            if not has_content:
                raise ValueError("PDF contains no extractable text or images")
        with open(os.path.join(directory, "content.opf"), "w", encoding="utf-8") as metadata_file:
            metadata_file.write(
                '<package><metadata><dc:title>' + html.escape(title)
                + '</dc:title><dc:creator>' + html.escape(author)
                + '</dc:creator><dc:language>' + html.escape(language)
                + '</dc:language></metadata></package>'
            )
        _add_pdf_navigation(html_path)
        build_epub_from_mobi7(html_path, output_path)


def _add_pdf_navigation(html_path: str) -> None:
    """Add section/page jumps to existing blocks without disturbing text flow."""
    from bs4 import BeautifulSoup

    with open(html_path, encoding="utf-8") as handle:
        soup = BeautifulSoup(handle.read(), "html.parser")
    points = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "img"]):
        if tag.name == "img":
            label = tag.get("alt", "")
            if not re.fullmatch(r"Original page \d+", label):
                continue
            target = tag.parent
        else:
            label = tag.get_text(" ", strip=True)
            target = tag
        if not label:
            continue
        anchor = f"pdf-reading-{len(points) + 1}"
        target["id"] = anchor
        points.append((label, anchor))
    if not points:
        return
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(str(soup))
    with open(os.path.join(os.path.dirname(html_path), "toc.ncx"), "w", encoding="utf-8") as handle:
        handle.write('<ncx><navMap>' + ''.join(
            '<navPoint><navLabel><text>' + html.escape(label)
            + '</text></navLabel><content src="book.html#' + anchor + '"/></navPoint>'
            for label, anchor in points) + '</navMap></ncx>')


XHTML_HEADER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
    '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
)
IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}
# Presentation attributes MOBI7 puts on block elements; XHTML rejects them.
_LEGACY_BLOCK_ATTRIBUTES = ("height", "width", "filepos", "recindex")


def _opf_values(opf_text: str, tag: str) -> list:
    return [
        html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
        for value in re.findall(rf"<dc:{tag}\b[^>]*>(.*?)</dc:{tag}>", opf_text, re.S)
        if value.strip()
    ]


def _xhtml_document(title: str, body: str, language: str = "en") -> str:
    return (
        XHTML_HEADER
        + '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="'
        + html.escape(language, quote=True) + '"><head>'
        + f"<title>{html.escape(title)}</title>"
        + '<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>'
        + f"</head><body>{body}</body></html>"
    )


def build_epub_from_mobi7(html_path: str, output_path: str) -> None:
    """Package an unpacked KF7 MOBI (book.html, Images/, content.opf, toc.ncx).

    The HTML is normalised to well-formed XHTML, split at ``mbp:pagebreak`` so
    each chapter is its own file, internal ``#filepos`` links are pointed at the
    file that now holds their target, and a fresh OPF/NCX is written. Raises on
    anything that would not produce a well-formed book.
    """
    from bs4 import BeautifulSoup, Tag
    from xml.etree import ElementTree

    source_dir = os.path.dirname(html_path)
    with open(html_path, "r", encoding="utf-8", errors="replace") as handle:
        soup = BeautifulSoup(handle.read(), "html.parser")
    opf_text = ""
    opf_path = os.path.join(source_dir, "content.opf")
    if os.path.isfile(opf_path):
        with open(opf_path, "r", encoding="utf-8", errors="replace") as handle:
            opf_text = handle.read()
    title = (_opf_values(opf_text, "title") or ["Book"])[0]
    creators = _opf_values(opf_text, "creator")
    language = normalize_language((_opf_values(opf_text, "language") or [""])[0]) or "en"
    publisher = (_opf_values(opf_text, "publisher") or [""])[0]
    identifier = "urn:uuid:" + str(uuid.uuid4())

    for tag in soup.find_all(["guide", "reference", "script", "style", "link"]):
        tag.decompose()
    body = soup.body or soup
    parts = [[]]
    for child in list(body.children):
        if isinstance(child, Tag) and child.name == "mbp:pagebreak":
            parts.append([])
            continue
        parts[-1].append(child)
    for part in parts:
        for node in part:
            if not isinstance(node, Tag):
                continue
            for tag in [node, *node.find_all(True)]:
                if ":" in (tag.name or ""):
                    tag.unwrap() if tag is not node else None
                    continue
                if tag.name != "img":
                    for attribute in _LEGACY_BLOCK_ATTRIBUTES:
                        tag.attrs.pop(attribute, None)
                for attribute in list(tag.attrs):
                    if ":" in attribute:
                        tag.attrs.pop(attribute, None)

    def has_content(part):
        return any(
            (isinstance(node, Tag) and (node.get_text(strip=True) or node.find("img") or node.name == "img"))
            or (not isinstance(node, Tag) and str(node).strip())
            for node in part
        )

    parts = [part for part in parts if has_content(part)]
    if not parts:
        raise ValueError("MOBI text was empty")
    names = [f"part{index:04d}.xhtml" for index in range(len(parts))]
    anchor_file = {}
    for name, part in zip(names, parts):
        for node in part:
            if isinstance(node, Tag):
                for tag in [node, *node.find_all(id=True)]:
                    if tag.get("id"):
                        anchor_file.setdefault(tag["id"], name)
    documents = []
    for name, part in zip(names, parts):
        for node in part:
            if not isinstance(node, Tag):
                continue
            for link in [node, *node.find_all("a", href=True)]:
                href = link.get("href") if link.name == "a" else None
                if href and href.startswith("#") and href[1:] in anchor_file:
                    target = anchor_file[href[1:]]
                    link["href"] = href if target == name else f"{target}{href}"
        document = _xhtml_document(title, "".join(str(node) for node in part), language)
        ElementTree.fromstring(document.encode("utf-8"))
        documents.append((name, document))

    images = []
    image_dir = os.path.join(source_dir, "Images")
    if os.path.isdir(image_dir):
        for filename in sorted(os.listdir(image_dir)):
            media_type = IMAGE_MEDIA_TYPES.get(os.path.splitext(filename)[1].lower())
            if media_type:
                images.append((filename, media_type))
    cover = next((name for name, _ in images if name.lower().startswith("cover")), "")

    nav_points = []
    ncx_path = os.path.join(source_dir, "toc.ncx")
    if os.path.isfile(ncx_path):
        with open(ncx_path, "r", encoding="utf-8", errors="replace") as handle:
            ncx_text = handle.read()
        for label, src in re.findall(
            r"<navLabel>\s*<text>(.*?)</text>\s*</navLabel>\s*<content\s+src=\"([^\"]*)\"",
            ncx_text,
            re.S,
        ):
            fragment = src.split("#", 1)[1] if "#" in src else ""
            target = anchor_file.get(fragment, names[0])
            nav_points.append((html.unescape(label).strip(), f"{target}#{fragment}" if fragment else target))
    if not nav_points:
        nav_points = [(title, names[0])]

    escape = html.escape
    manifest = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        *[
            f'<item id="text{index}" href="{name}" media-type="application/xhtml+xml"/>'
            for index, name in enumerate(names)
        ],
        *[
            f'<item id="img{index}" href="Images/{escape(name)}" media-type="{media_type}"/>'
            for index, (name, media_type) in enumerate(images)
        ],
    ]
    cover_id = next(
        (f"img{index}" for index, (name, _) in enumerate(images) if name == cover), ""
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">'
        f"<dc:title>{escape(title)}</dc:title>"
        + "".join(f"<dc:creator>{escape(creator)}</dc:creator>" for creator in creators[:4])
        + f"<dc:language>{escape(language)}</dc:language>"
        + (f"<dc:publisher>{escape(publisher)}</dc:publisher>" if publisher else "")
        + f'<dc:identifier id="bookid">{identifier}</dc:identifier>'
        + (f'<meta name="cover" content="{cover_id}"/>' if cover_id else "")
        + "</metadata><manifest>" + "".join(manifest) + '</manifest><spine toc="ncx">'
        + "".join(f'<itemref idref="text{index}"/>' for index in range(len(names)))
        + "</spine></package>"
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f'<head><meta name="dtb:uid" content="{identifier}"/></head>'
        f"<docTitle><text>{escape(title)}</text></docTitle><navMap>"
        + "".join(
            f'<navPoint id="nav{index}" playOrder="{index + 1}"><navLabel><text>{escape(label)}</text>'
            f'</navLabel><content src="{escape(src)}"/></navPoint>'
            for index, (label, src) in enumerate(nav_points)
        )
        + "</navMap></ncx>"
    )
    for document in (opf, ncx):
        ElementTree.fromstring(document.encode("utf-8"))
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    with zipfile.ZipFile(output_path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container, zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/toc.ncx", ncx, zipfile.ZIP_DEFLATED)
        for name, document in documents:
            archive.writestr(f"OEBPS/{name}", document, zipfile.ZIP_DEFLATED)
        for name, _media_type in images:
            archive.write(os.path.join(image_dir, name), f"OEBPS/Images/{name}", zipfile.ZIP_DEFLATED)


def _extraction_root(candidate: str) -> str:
    """Return the ``mobiex...`` directory that ``mobi`` unpacked into."""
    path = os.path.abspath(str(candidate or ""))
    while path and path != os.path.dirname(path):
        if os.path.basename(path).startswith(("mobiex", "libflix-convert-")):
            return path
        path = os.path.dirname(path)
    return ""


def _remove_tree(path: str) -> None:
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def cleanup(result: Optional["ConversionResult"]) -> None:
    """Remove the temporary conversion workspace once delivery is finished."""
    if not result or not result.temporary or not result.path:
        return
    workdir = os.path.dirname(result.path)
    if os.path.basename(workdir).startswith(("libflix-convert-", "mobiex")):
        shutil.rmtree(workdir, ignore_errors=True)


def cleanup_path(path: str) -> None:
    """Remove a conversion workspace from an EPUB path."""
    if not path:
        return
    workdir = os.path.dirname(str(path))
    if os.path.basename(workdir).startswith(("libflix-convert-", "mobiex")):
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--pdf-worker":
        raise SystemExit(2)
    try:
        limits = json.loads(sys.argv[4])
        for name in tuple(globals()):
            if name.startswith("PDF_") and name in limits:
                globals()[name] = int(limits[name])
        import resource
        if sys.platform != "darwin":
            resource.setrlimit(resource.RLIMIT_AS, (PDF_WORKER_MEMORY_BYTES, PDF_WORKER_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (PDF_WORKER_TIMEOUT_SECONDS, PDF_WORKER_TIMEOUT_SECONDS + 1))
        build_epub_from_pdf(sys.argv[2], sys.argv[3])
    except BaseException as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
