"""Safe, format-aware preparation for Send to Kindle attachments."""

from __future__ import annotations

import html
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Mapping
from xml.etree import ElementTree as ET


DC_NS = "http://purl.org/dc/elements/1.1/"
OPF_NS = "http://www.idpf.org/2007/opf"
PDF_METADATA_REWRITE_MAX_BYTES = max(
    0,
    int(os.environ.get("KINDLE_PDF_METADATA_MAX_BYTES", str(20 * 1024 * 1024))),
)

ET.register_namespace("dc", DC_NS)
ET.register_namespace("", OPF_NS)


@dataclass(frozen=True)
class PreparedBook:
    path: str
    filename: str
    title: str
    author: str
    updated_fields: tuple[str, ...] = ()
    cover_added: bool = False
    temporary: bool = False
    warning: str = ""


def _plain_text(value, limit):
    text = html.unescape(str(value or ""))
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def clean_book_title(value):
    title = _plain_text(value, 220)
    title = re.sub(r"\.(?:epub|pdf|mobi|azw3?)$", "", title, flags=re.IGNORECASE)
    title = title.replace("_", " ")
    noisy_group = (
        r"(?:retail|ebook|e-book|epub|pdf|mobi|azw3?|calibre|converted|"
        r"ocr|scan|libgen(?:\.li)?|v\d+(?:\.\d+)*)"
    )
    previous = None
    while title and title != previous:
        previous = title
        title = re.sub(
            rf"\s*[\[(]\s*{noisy_group}(?:[^\])]*?)?[\])]\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )
        title = re.sub(
            rf"\s*[-|:]\s*{noisy_group}\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )
    title = re.sub(r"\s+", " ", title).strip(" ._-")
    return title[:180] or "Book"


def clean_book_author(value):
    author = _plain_text(value, 240)
    if not author:
        return ""
    parts = [part.strip() for part in re.split(r"\s*;\s*", author) if part.strip()]
    unique = []
    seen = set()
    for part in parts:
        key = part.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(part)
    return "; ".join(unique)[:180]


def normalize_language(value):
    language = _plain_text(value, 24).casefold()
    aliases = {
        "chinese": "zh",
        "chi": "zh",
        "zho": "zh",
        "cn": "zh",
        "zh-cn": "zh",
        "english": "en",
        "eng": "en",
    }
    language = aliases.get(language, language)
    return language if re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", language) else ""


def attachment_filename(title, extension):
    title = clean_book_title(title)
    base = re.sub(r'[\\/:*?"<>|]+', " ", title)
    base = re.sub(r"\s+", " ", base).strip(" .")[:120] or "Book"
    extension = re.sub(r"[^a-z0-9]", "", str(extension or "").lower()) or "epub"
    return f"{base}.{extension}"


def normalize_metadata(metadata: Mapping[str, object]):
    title = clean_book_title(metadata.get("canonical_title") or metadata.get("title"))
    return {
        "title": title,
        "author": clean_book_author(metadata.get("author")),
        "language": normalize_language(metadata.get("language")),
        "publisher": _plain_text(metadata.get("publisher"), 180),
        "date": _plain_text(metadata.get("year") or metadata.get("date"), 32),
        "description": _plain_text(metadata.get("description"), 4000),
        "identifier": _plain_text(metadata.get("identifier"), 300),
    }


def _titles_equivalent(existing, canonical):
    return clean_book_title(existing).casefold() == clean_book_title(canonical).casefold()


def _local_name(tag):
    return str(tag).rsplit("}", 1)[-1]


def _child(element, name):
    return next((child for child in list(element) if _local_name(child.tag) == name), None)


def _elements(element, name):
    return [child for child in element.iter() if _local_name(child.tag) == name]


def _safe_archive_path(value):
    path = PurePosixPath(str(value or ""))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Unsafe EPUB archive path")
    return str(path)


def _epub_package_path(archive):
    container = ET.fromstring(archive.read("META-INF/container.xml"))
    rootfile = next(
        (item for item in container.iter() if _local_name(item.tag) == "rootfile"),
        None,
    )
    if rootfile is None:
        raise ValueError("EPUB package file is missing")
    return _safe_archive_path(rootfile.get("full-path"))


def _opf_tag(package, name):
    if str(package.tag).startswith("{"):
        namespace = str(package.tag)[1:].split("}", 1)[0]
        return f"{{{namespace}}}{name}"
    return name


def _has_epub_cover(manifest, package):
    items = _elements(manifest, "item")
    item_by_id = {item.get("id", ""): item for item in items}
    metadata = _child(package, "metadata")
    for meta in _elements(metadata, "meta") if metadata is not None else []:
        if meta.get("name", "").casefold() == "cover":
            item = item_by_id.get(meta.get("content", ""))
            if item is not None and item.get("media-type", "").startswith("image/"):
                return True
    for item in items:
        properties = set(item.get("properties", "").split())
        if "cover-image" in properties:
            return True
        identity = f"{item.get('id', '')} {item.get('href', '')}".casefold()
        if item.get("media-type", "").startswith("image/") and "cover" in identity:
            return True
    guide = _child(package, "guide")
    return any(
        reference.get("type", "").casefold() == "cover"
        for reference in _elements(guide, "reference")
    ) if guide is not None else False


def _set_epub_metadata(package, values):
    metadata = _child(package, "metadata")
    if metadata is None:
        metadata = ET.SubElement(package, _opf_tag(package, "metadata"))
    changed = []

    titles = _elements(metadata, "title")
    if titles:
        if not _titles_equivalent(titles[0].text, values["title"]):
            titles[0].text = values["title"]
            changed.append("title")
    else:
        ET.SubElement(metadata, f"{{{DC_NS}}}title").text = values["title"]
        changed.append("title")

    optional_fields = (
        ("creator", "author"),
        ("language", "language"),
        ("publisher", "publisher"),
        ("date", "date"),
        ("description", "description"),
        ("identifier", "identifier"),
    )
    for element_name, value_name in optional_fields:
        value = values.get(value_name, "")
        if value and not any((item.text or "").strip() for item in _elements(metadata, element_name)):
            ET.SubElement(metadata, f"{{{DC_NS}}}{element_name}").text = value
            changed.append(value_name)
    return metadata, changed


def _unique_cover_member(existing_names, package_path):
    package_dir = PurePosixPath(package_path).parent
    for index in range(100):
        name = "libflix-cover.jpg" if not index else f"libflix-cover-{index}.jpg"
        member = str(package_dir / name) if str(package_dir) != "." else name
        if member not in existing_names:
            return member, name
    raise ValueError("Could not allocate EPUB cover path")


def _add_epub_cover(package, metadata, manifest, package_path, existing_names, cover_bytes):
    member, href = _unique_cover_member(existing_names, package_path)
    existing_ids = {item.get("id", "") for item in _elements(manifest, "item")}
    item_id = "libflix-cover-image"
    suffix = 1
    while item_id in existing_ids:
        item_id = f"libflix-cover-image-{suffix}"
        suffix += 1

    item = ET.SubElement(
        manifest,
        _opf_tag(package, "item"),
        {"id": item_id, "href": href, "media-type": "image/jpeg"},
    )
    if str(package.get("version", "")).startswith("3"):
        item.set("properties", "cover-image")
    ET.SubElement(
        metadata,
        _opf_tag(package, "meta"),
        {"name": "cover", "content": item_id},
    )
    return member, cover_bytes


def _rewrite_epub(source_path, output_path, values, cover_loader):
    with zipfile.ZipFile(source_path, "r") as source:
        package_path = _epub_package_path(source)
        package_info = source.getinfo(package_path)
        package = ET.fromstring(source.read(package_path))
        manifest = _child(package, "manifest")
        if manifest is None:
            raise ValueError("EPUB manifest is missing")
        metadata, changed = _set_epub_metadata(package, values)
        cover_member = ""
        cover_bytes = b""
        if not _has_epub_cover(manifest, package) and cover_loader:
            loaded_cover = cover_loader() or b""
            if loaded_cover:
                cover_member, cover_bytes = _add_epub_cover(
                    package,
                    metadata,
                    manifest,
                    package_path,
                    set(source.namelist()),
                    loaded_cover,
                )
                changed.append("cover")
        if not changed:
            return (), False

        package_xml = ET.tostring(package, encoding="utf-8", xml_declaration=True)
        infos = source.infolist()
        archive_comment = source.comment
        with zipfile.ZipFile(output_path, "w") as target:
            mimetype = next((item for item in infos if item.filename == "mimetype"), None)
            if mimetype is not None:
                mimetype.compress_type = zipfile.ZIP_STORED
                target.writestr(mimetype, source.read(mimetype.filename))
            for item in infos:
                if item.filename in {"mimetype", package_path, cover_member}:
                    continue
                with source.open(item, "r") as source_member:
                    with target.open(item, "w", force_zip64=True) as target_member:
                        shutil.copyfileobj(source_member, target_member, length=1024 * 1024)
            target.writestr(package_info, package_xml)
            if cover_member:
                target.writestr(cover_member, cover_bytes, compress_type=zipfile.ZIP_DEFLATED)
            target.comment = archive_comment
    return tuple(changed), bool(cover_member)


def _rewrite_pdf(source_path, output_path, values):
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(source_path)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF metadata cannot be updated")
    writer = PdfWriter()
    if hasattr(writer, "clone_document_from_reader"):
        writer.clone_document_from_reader(reader)
    else:
        writer.append_pages_from_reader(reader)

    existing = {
        str(key): str(value)
        for key, value in (reader.metadata or {}).items()
        if key and value is not None
    }
    updates = {}
    if existing.get("/Title", "").strip() != values["title"]:
        updates["/Title"] = values["title"]
    if values["author"] and not existing.get("/Author", "").strip():
        updates["/Author"] = values["author"]
    if values["description"] and not existing.get("/Subject", "").strip():
        updates["/Subject"] = values["description"]
    if not updates:
        return ()
    writer.add_metadata({**existing, **updates})
    with open(output_path, "wb") as output:
        writer.write(output)
    return tuple(key.removeprefix("/").casefold() for key in updates)


def prepare_book_for_kindle(
    source_path: str,
    extension: str,
    metadata: Mapping[str, object],
    cover_loader: Callable[[], bytes] | None = None,
    pdf_rewrite_max_bytes: int | None = None,
):
    values = normalize_metadata(metadata)
    extension = re.sub(r"[^a-z0-9]", "", str(extension or "").lower()) or "epub"
    filename = attachment_filename(values["title"], extension)
    temporary_path = ""
    try:
        if extension not in {"epub", "pdf"}:
            return PreparedBook(source_path, filename, values["title"], values["author"])
        if extension == "pdf":
            rewrite_limit = (
                PDF_METADATA_REWRITE_MAX_BYTES
                if pdf_rewrite_max_bytes is None
                else max(0, int(pdf_rewrite_max_bytes))
            )
            if rewrite_limit and os.path.getsize(source_path) > rewrite_limit:
                return PreparedBook(
                    source_path,
                    filename,
                    values["title"],
                    values["author"],
                    warning="Large PDF kept unchanged to avoid a slow full-file rewrite",
                )
        with tempfile.NamedTemporaryFile(suffix=f".{extension}", delete=False) as output:
            temporary_path = output.name
        if extension == "epub":
            updated_fields, cover_added = _rewrite_epub(
                source_path,
                temporary_path,
                values,
                cover_loader,
            )
        else:
            updated_fields = _rewrite_pdf(source_path, temporary_path, values)
            cover_added = False
        if not updated_fields:
            os.unlink(temporary_path)
            return PreparedBook(source_path, filename, values["title"], values["author"])
        return PreparedBook(
            temporary_path,
            filename,
            values["title"],
            values["author"],
            updated_fields,
            cover_added,
            True,
        )
    except Exception as error:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        return PreparedBook(
            source_path,
            filename,
            values["title"],
            values["author"],
            warning=f"{type(error).__name__}: {error}",
        )
