"""Convert legacy Kindle formats (MOBI / AZW / AZW3) to EPUB.

LibFlix hides MOBI/AZW/AZW3 from download search because Amazon's Send to
Kindle no longer accepts them.  Some books are only available in those
formats, so this module unpacks them into a clean EPUB before the normal
preparation and delivery path runs.

The pure-Python ``mobi`` package is used instead of Calibre so no external
binary is required.  The original file is never modified: extraction writes to
a private temporary directory, and only a validated EPUB (ZIP with a mimetype
entry) is returned.  Any failure yields an empty result with a warning; the
caller must not send the unconverted file, because Send to Kindle rejects
MOBI/AZW3.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from typing import Optional

CONVERTIBLE_EXTENSIONS = frozenset({"mobi", "azw3"})
CONVERTIBLE_TARGET = "epub"


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
    """Return an EPUB path for a MOBI/AZW/AZW3 file, or an empty failure."""
    if not is_convertible(extension):
        return ConversionResult("", "")
    if not source_path or not os.path.isfile(source_path):
        return ConversionResult("", "", warning="Source file is missing")

    workdir = tempfile.mkdtemp(prefix="libflix-convert-")
    output_path = os.path.join(workdir, "converted.epub")
    extraction_root = ""
    converted = False
    try:
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


def _xhtml_document(title: str, body: str) -> str:
    return (
        XHTML_HEADER
        + '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
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
    language = (_opf_values(opf_text, "language") or ["en"])[0]
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
        document = _xhtml_document(title, "".join(str(node) for node in part))
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
