"""Abstract downloader interface.

A ``Downloader`` knows how to:
- search a book source and return normalized ``Book`` results
- resolve a book's unique ID into a fetchable download URL
- stream the file bytes from that download URL
- locate cover-image URLs for books that ship with covers

Concrete implementations live alongside this file (e.g. ``libgen.py``).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, asdict, field
from typing import Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter


# Shared HTTP session used by all downloaders.  Connection pooling keeps
# repeat downloads fast and avoids hammering the source with new sockets.
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=20))
SESSION.mount("http://", HTTPAdapter(pool_connections=10, pool_maxsize=20))
SESSION.headers.update({
    "User-Agent": "LibFlix/1.0 "
    f"({os.environ.get('LIBFLIX_CONTACT', 'https://github.com/ianchadson/LibFlix')})"
})

# A small in-memory TTL cache shared across all downloaders; callers can use
# it via ``cache_get`` / ``cache_set``.
_CACHE: dict = {}
CACHE_TTL_LG = 900  # 15 minutes — used for search HTML / API responses


def cache_get(key: str, ttl: int = CACHE_TTL_LG):
    v = _CACHE.get(key)
    if v and time.time() - v["t"] < ttl:
        return v["d"]
    return None


def cache_set(key: str, data) -> None:
    _CACHE[key] = {"d": data, "t": time.time()}


def cache_delete(key: str) -> None:
    _CACHE.pop(key, None)


# ---------------------------------------------------------------------------
# Download-source identity
# ---------------------------------------------------------------------------
# LibGen keeps its raw 32-hex MD5 as the canonical id for backwards
# compatibility.  Additional sources namespace their native id with a short
# prefix so a single ``/download/<id>`` route can dispatch to the right
# provider without collisions:
#
#   libgen     -> 32 hex chars                     (e.g. 5f4bd0b00db4564b...)
#   realdebrid -> "rd" + 40-hex BitTorrent hash     (e.g. rd232cd67e...)
SOURCE_ID_PATTERN = re.compile(r"^(?:[a-f0-9]{32}|rd[a-f0-9]{40})$")
SOURCE_PREFIXES = {
    "rd": "realdebrid",
}


def is_download_id(value: str) -> bool:
    return bool(SOURCE_ID_PATTERN.fullmatch(str(value or "").casefold()))


def is_libgen_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{32}", str(value or "").casefold()))


def encode_source_id(source: str, native_id: str) -> str:
    """Prefix a provider-native id so it routes to the right downloader."""
    source = str(source or "").casefold()
    native_id = str(native_id or "").strip().casefold()
    if source == "libgen":
        return native_id
    for prefix, name in SOURCE_PREFIXES.items():
        if name == source:
            candidate = f"{prefix}{native_id}"
            return candidate if is_download_id(candidate) else ""
    return ""


def decode_source_id(value: str) -> Tuple[str, str]:
    """Return ``(source, native_id)`` for a namespaced download id."""
    value = str(value or "").strip().casefold()
    if re.fullmatch(r"[a-f0-9]{32}", value):
        return "libgen", value
    prefix = value[:2]
    source = SOURCE_PREFIXES.get(prefix)
    if source and is_download_id(value):
        return source, value[2:]
    return "", ""


# ---------------------------------------------------------------------------
# Safe upstream fetching shared by HTML-scraping sources
# ---------------------------------------------------------------------------
class SourceFetchError(Exception):
    """A provider response was missing, oversized, or off-source."""


def host_is_allowed(url: str, allowed_hosts: Iterable[str]) -> bool:
    """True when ``url`` is http(s) and on one of the allowed registrable hosts."""
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").casefold()
    if not host:
        return False
    for allowed in allowed_hosts:
        allowed = str(allowed or "").casefold().lstrip(".")
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def fetch_bounded(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    params: Optional[dict] = None,
    timeout: Tuple[float, float] = (4, 12),
    max_bytes: int = 2_000_000,
    headers: Optional[dict] = None,
    allow_redirects: bool = False,
) -> str:
    """Fetch one bounded HTML page from an allow-listed host.

    The provider never executes page scripts, so ad/pop-up surfaces cannot
    run.  Redirects are refused by default so a compromised source cannot
    bounce the server to an unrelated host.
    """
    if not host_is_allowed(url, allowed_hosts):
        raise SourceFetchError("Off-source request refused")
    try:
        response = SESSION.get(
            url,
            params=params,
            timeout=timeout,
            allow_redirects=allow_redirects,
            headers=headers,
            stream=True,
        )
    except requests.RequestException as error:
        raise SourceFetchError(str(error)) from error
    try:
        try:
            response.raise_for_status()
        except requests.RequestException as error:
            raise SourceFetchError(str(error)) from error
        if not host_is_allowed(response.url, allowed_hosts):
            raise SourceFetchError("Off-source redirect refused")
        content_type = (response.headers.get("Content-Type") or "").casefold()
        if "html" not in content_type and "json" not in content_type:
            raise SourceFetchError("Unexpected content type")
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise SourceFetchError("Response too large")
        chunks: List[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise SourceFetchError("Response too large")
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, "replace")
    finally:
        response.close()


def human_size(value) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


@dataclass
class Book:
    """A normalized book entry returned by any downloader."""

    book_id: str = ""
    title: str = ""
    author: str = ""
    publisher: str = ""
    year: str = ""
    language: str = ""
    pages: str = ""
    size: str = ""
    ext: str = ""
    cover_url: str = ""
    source: str = ""

    def to_dict(self, idx: int = 0) -> dict:
        d = asdict(self)
        # Frontend expects lowercase ``md5`` for back-compat with libgen; map
        # the generic ``book_id`` to ``md5`` so templates keep working.
        d["md5"] = self.book_id
        d["idx"] = idx
        return d


class Downloader:
    """Abstract downloader — subclass and implement the missing methods."""

    name: str = "base"

    # ---------------------------------------------------------------- search
    def search(
        self,
        query: str,
        *,
        sort: str = "y",
        order: str = "DESC",
        page: int = 1,
        limit: int = 25,
    ) -> Tuple[List[Book], int]:
        """Search the source and return ``(books, total_results)``."""
        raise NotImplementedError

    # ------------------------------------------------------- resolve + fetch
    def resolve_download(self, book_id: str) -> str:
        """Resolve ``book_id`` (typically an md5) to a fetchable URL."""
        raise NotImplementedError

    def invalidate_download(self, book_id: str) -> None:
        """Forget a cached file URL after an upstream transfer failure."""

    def resolved_filename(self, book_id: str) -> str:
        """Return the source's resolved filename when it is already known."""
        return ""

    def stream_file(self, url: str) -> Iterator[bytes]:
        """Stream bytes from ``url``.  Default impl uses ``SESSION`` + chunked reads."""
        r = SESSION.get(url, stream=True, timeout=120, allow_redirects=True)
        r.raise_for_status()
        yield from r.iter_content(chunk_size=65536)

    # ---------------------------------------------------------------- cover
    def cover_url(self, book: Book) -> Optional[str]:
        """Return a proxied/static cover URL for the book, or ``None``."""
        return None

    # --------------------------------------------------------------- helpers
    def filter_books(
        self,
        books: Iterable[Book],
        *,
        fmt: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> List[Book]:
        """Filter by format extension and language. ``None``/``"all"`` = no filter."""
        out = []
        for b in books:
            if fmt and fmt != "all" and b.ext.lower() != fmt.lower():
                continue
            if lang and lang != "all" and b.language.lower() != lang.lower():
                continue
            out.append(b)
        return out
