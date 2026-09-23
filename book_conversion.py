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

import os
import re
import shutil
import tempfile
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
        candidate = next((item for item in candidates if _valid_epub(item)), "")
        if not candidate:
            return ConversionResult("", "", warning="Converter produced no usable EPUB")
        extraction_root = _extraction_root(candidate)

        shutil.copyfile(candidate, output_path)
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
