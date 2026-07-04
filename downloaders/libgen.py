"""Libgen.li downloader.

Implements ``Downloader`` for the Library Genesis mirror at ``libgen.li``,
including the HTML search parser, the ads.php download resolver, and the
cover-image proxy used by the ``/cover/<md5>`` Flask route.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from downloaders.base import (
    Book,
    Downloader,
    SESSION,
    cache_get,
    cache_set,
    CACHE_TTL_LG,
)

MIRROR = "https://libgen.li"


@dataclass
class LibgenBook(Book):
    """Libgen-specific book fields — ``cover_dir`` is used by the proxy."""

    cover_dir: str = ""
    source: str = "libgen"

    def to_dict(self, idx: int = 0) -> dict:
        d = super().to_dict(idx)
        d["cover_dir"] = self.cover_dir
        return d


class LibgenDownloader(Downloader):
    name = "libgen"

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
        html = self._fetch_search(query, sort, order, page, limit)
        books = self._parse_results(html)
        total = self._total_results(html)
        return books, total

    def _fetch_search(self, query, sort, order, page, res):
        key = f"lg:{query}:{sort}:{order}:{page}:{res}"
        cached = cache_get(key, CACHE_TTL_LG)
        if cached:
            return cached
        params = {
            "req": query,
            "columns[]": ["t", "a", "s", "y", "p", "i", "l", "la", "qi"],
            "sort": sort,
            "sortmode": order,
            "page": page,
            "res": res,
            "gmode": 1,
            "topics[]": ["l", "f"],
            "curtab": "f",
        }
        r = SESSION.get(f"{MIRROR}/index.php", params=params, timeout=30)
        r.raise_for_status()
        cache_set(key, r.text)
        return r.text

    @staticmethod
    def _parse_results(html_text: str) -> List[Book]:
        soup = BeautifulSoup(html_text, "html.parser")
        table = soup.find("table", id="tablelibgen")
        if not table:
            return []
        books: List[Book] = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 9:
                continue
            tds = list(row.find_all("td"))
            md5 = ""
            mlink = tds[-1].find("a")
            if mlink and mlink.get("href"):
                m = re.search(r"md5=([a-f0-9]{32})", mlink["href"])
                if m:
                    md5 = m.group(1)
            link_a = tds[0].find("a")
            title = (
                link_a.get_text(strip=True) if link_a else tds[0].get_text(strip=True)
            )
            title = re.sub(r"\s+", " ", title).strip()
            year = tds[3].get_text(" ", strip=True)
            year = re.sub(r"[;|].*$", "", year).strip()
            year = re.sub(r"\s+P\s+\d+.*$", "", year).strip()
            size_link = tds[6].find("a")
            size = (
                size_link.get_text(strip=True)
                if size_link
                else tds[6].get_text(" ", strip=True)
            )
            first_html = str(tds[0])
            l_match = re.search(r"l (\d+)", first_html)
            cover_dir = ""
            if l_match:
                l_num = int(l_match.group(1))
                cover_dir = str(l_num // 1000 * 1000)
            books.append(
                LibgenBook(
                    book_id=md5,
                    title=title,
                    author=tds[1].get_text(" ", strip=True),
                    publisher=tds[2].get_text(" ", strip=True),
                    year=year,
                    language=tds[4].get_text(" ", strip=True),
                    pages=tds[5].get_text(" ", strip=True),
                    size=size,
                    ext=tds[7].get_text(" ", strip=True),
                    cover_dir=cover_dir,
                )
            )
        return books

    @staticmethod
    def _total_results(html_text: str) -> int:
        soup = BeautifulSoup(html_text, "html.parser")
        paginator = soup.find("div", class_="paginator")
        if paginator:
            m = re.search(r'Paginator\("(\w+)",\s*(\d+)', str(paginator))
            if m:
                return int(m.group(2))
        return 0

    # ------------------------------------------------------- resolve + fetch
    def resolve_download(self, book_id: str) -> str:
        try:
            r = SESSION.get(
                f"{MIRROR}/ads.php",
                params={"md5": book_id},
                timeout=30,
                allow_redirects=True,
            )
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if "get.php" in href or "download" in href.lower() or "/dl/" in href:
                    return urljoin(r.url, href)
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if href.startswith("http") and ("libgen" in href or "booksdl" in href):
                    return urljoin(r.url, href)
            txt = r.text
            m = re.search(
                r'https?://[^"\']+\.(?:epub|pdf|mobi|djvu|zip|rar)[^"\']*', txt, re.I
            )
            if m:
                return m.group(0)
            m = re.search(r'(?:href|HREF)=["\']([^"\']+)["\']', txt)
            if m:
                return urljoin(r.url, m.group(1))
        except Exception:
            return f"{MIRROR}/ads.php?md5={book_id}"
        return f"{MIRROR}/ads.php?md5={book_id}"

    # ---------------------------------------------------------------- cover
    def cover_url(self, book: Book) -> Optional[str]:
        """Return the libgen proxy URL if the book has a ``cover_dir``."""
        cover_dir = getattr(book, "cover_dir", "")
        if not cover_dir or not book.book_id:
            return None
        return f"/cover/{book.book_id}?dir={cover_dir}"