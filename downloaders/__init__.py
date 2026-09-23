"""Download source abstractions for LibFlix.

Each downloader implements a common interface so the Flask routes can be
source-agnostic.  ``MultiDownloader`` fans a search out across the sources that
are enabled for this deployment and routes a namespaced download id back to the
provider that owns it.

Enabled sources are chosen by ``LIBFLIX_DOWNLOAD_SOURCES`` (comma-separated, or
``all``).  When the variable is unset the default is LibGen plus Real-Debrid
whenever a Real-Debrid token is configured.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from downloaders.base import Book, Downloader, decode_source_id
from downloaders.libgen import LibgenDownloader
from downloaders.realdebrid import RealDebridDownloader
from downloaders.realdebrid import is_enabled as realdebrid_enabled

SOURCE_CLASSES = {
    "libgen": LibgenDownloader,
    "realdebrid": RealDebridDownloader,
}
SOURCE_ORDER = ("libgen", "realdebrid")


def get_downloader(name: str = "libgen") -> Downloader:
    """Return the named download source (default: libgen)."""
    cls = SOURCE_CLASSES.get(str(name or "").casefold(), LibgenDownloader)
    return cls()


def configured_sources() -> List[str]:
    raw = os.environ.get("LIBFLIX_DOWNLOAD_SOURCES")
    if raw is None:
        names = ["libgen"]
        if realdebrid_enabled():
            names.append("realdebrid")
        return names
    requested = [part.strip().casefold() for part in raw.split(",") if part.strip()]
    if "all" in requested:
        requested = list(SOURCE_ORDER)
    names: List[str] = []
    for name in requested:
        if name in SOURCE_CLASSES and name not in names:
            names.append(name)
    return names or ["libgen"]


def _merge_key(book: Book) -> str:
    identifier = str(getattr(book, "book_id", "") or "").casefold()
    if identifier:
        return identifier
    return "|".join((
        str(book.title or "").casefold(),
        str(book.author or "").casefold(),
        str(book.ext or "").casefold(),
        str(book.size or "").casefold(),
    ))


class MultiDownloader(Downloader):
    """Search several sources and dispatch downloads by namespaced id."""

    name = "multi"

    def __init__(self, names: Optional[List[str]] = None):
        self.order = list(names if names is not None else configured_sources())
        self.downloaders: Dict[str, Downloader] = {
            name: get_downloader(name) for name in self.order
        }

    def _primary(self) -> Downloader:
        if self.order:
            return self.downloaders[self.order[0]]
        return get_downloader()

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
        if not self.order:
            return [], 0
        if len(self.order) == 1 or page > 1:
            return self._primary().search(
                query, sort=sort, order=order, page=page, limit=limit
            )

        results: List[Optional[Tuple[List[Book], int]]] = [None] * len(self.order)
        errors: List[BaseException] = []
        with ThreadPoolExecutor(max_workers=min(4, len(self.order))) as pool:
            futures = {
                pool.submit(
                    self.downloaders[name].search,
                    query,
                    sort=sort,
                    order=order,
                    page=page,
                    limit=limit,
                ): index
                for index, name in enumerate(self.order)
            }
            for future in as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except Exception as error:  # one bad source must not fail search
                    errors.append(error)

        successful = [result for result in results if result is not None]
        if not successful:
            if errors:
                raise errors[0]
            return [], 0

        merged: List[Book] = []
        seen = set()
        total = 0
        for books, source_total in successful:
            total += int(source_total or 0)
            for book in books:
                key = _merge_key(book)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(book)
        return merged, max(total, len(merged))

    # ------------------------------------------------------- resolve + fetch
    def resolve_download(self, book_id: str) -> str:
        source, _native = decode_source_id(book_id)
        downloader = self.downloaders.get(source)
        if downloader is None:
            return ""
        return downloader.resolve_download(book_id)

    def invalidate_download(self, book_id: str) -> None:
        source, _native = decode_source_id(book_id)
        downloader = self.downloaders.get(source)
        if downloader is not None:
            downloader.invalidate_download(book_id)

    def resolved_filename(self, book_id: str) -> str:
        source, _native = decode_source_id(book_id)
        downloader = self.downloaders.get(source)
        if downloader is not None:
            return downloader.resolved_filename(book_id)
        return ""

    def cover_url(self, book: Book) -> Optional[str]:
        source = str(getattr(book, "source", "") or "").casefold()
        downloader = self.downloaders.get(source)
        if downloader is not None:
            return downloader.cover_url(book)
        return None


# Active downloader used by the Flask app routes.
DOWNLOADER: Downloader = MultiDownloader()
