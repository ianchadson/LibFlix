"""Abstract downloader interface.

A ``Downloader`` knows how to:
- search a book source and return normalized ``Book`` results
- resolve a book's unique ID into a fetchable download URL
- stream the file bytes from that download URL
- locate cover-image URLs for books that ship with covers

Concrete implementations live alongside this file (e.g. ``libgen.py``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict, field
from typing import Iterable, Iterator, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter


# Shared HTTP session used by all downloaders.  Connection pooling keeps
# repeat downloads fast and avoids hammering the source with new sockets.
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=20))
SESSION.mount("http://", HTTPAdapter(pool_connections=10, pool_maxsize=20))
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

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