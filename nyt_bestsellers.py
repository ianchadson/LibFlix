"""Parse an attributable NYT number-one history signal from Wikipedia.

This is deliberately a ranking overlay, not a discovery provider. Open Library
still supplies every canonical book candidate. The public year pages are
fetched in the background and an exact title-and-author match can only break a
close relevance tie; it cannot admit an unrelated book.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from bs4 import BeautifulSoup


INDEX_VERSION = "nyt-number-one-wikipedia-v1"
MAX_PAGES = 3
MAX_TABLES_PER_PAGE = 4
MAX_ROWS_PER_TABLE = 64
MAX_INDEX_BOOKS = 256
MAX_LIST_NAMES = 4


def normalize_text(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[^\w\u3400-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _text(value: Any, limit: int = 300) -> str:
    if hasattr(value, "get_text"):
        value = value.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_span(value: Any) -> int:
    try:
        return min(max(int(value or 1), 1), MAX_ROWS_PER_TABLE)
    except (TypeError, ValueError):
        return 1


def _expanded_rows(table: Any) -> Iterable[list[Any | None]]:
    """Yield logical table rows with HTML rowspans expanded."""

    carried: dict[int, tuple[int, Any]] = {}
    for raw_row in table.select("tr")[: MAX_ROWS_PER_TABLE + 1]:
        row: list[Any | None] = [None] * 8
        for column, (remaining, cell) in list(carried.items()):
            row[column] = cell
            if remaining <= 1:
                carried.pop(column, None)
            else:
                carried[column] = (remaining - 1, cell)

        column = 0
        for cell in raw_row.find_all(["th", "td"], recursive=False):
            while column < len(row) and row[column] is not None:
                column += 1
            if column >= len(row):
                break
            row[column] = cell
            span = _safe_span(cell.get("rowspan"))
            if span > 1:
                carried[column] = (span - 1, cell)
            column += 1
        yield row


def _issue_date(value: Any, year: int) -> str:
    text = re.sub(r"\[[^\]]*\]", "", _text(value, 40)).strip()
    for pattern in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(f"{text} {year}", pattern).date().isoformat()
        except ValueError:
            continue
    return ""


def _author_names(cell: Any) -> list[str]:
    raw = _text(cell, 300)
    names: list[str] = []
    for link in cell.select("a[href]") if cell else ():
        name = _text(link, 160)
        href = str(link.get("href") or "")
        if name and not re.fullmatch(r"\[?\d+\]?", name) and "wiki" in href:
            names.append(name)
    names.extend(
        part.strip(" ,")
        for part in re.split(r"\s+(?:with|and|&)\s+|;|,\s*(?=[A-Z])", raw)
        if part.strip(" ,")
    )
    if raw:
        names.append(raw)
    return list(dict.fromkeys(name for name in names if normalize_text(name)))[:12]


def _list_name(table: Any) -> str:
    heading = table.find_previous(["h2", "h3"])
    label = re.sub(r"\[edit\]$", "", _text(heading, 80), flags=re.I).strip()
    normalized = normalize_text(label)
    if "nonfiction" in normalized:
        return "Hardcover Nonfiction #1"
    if "fiction" in normalized:
        return "Hardcover Fiction #1"
    return "NYT #1"


def _merge_entry(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    dates = [
        value
        for value in (existing.get("published_date"), incoming.get("published_date"))
        if value
    ]
    first_dates = [
        value
        for value in (
            existing.get("first_published_date"),
            incoming.get("first_published_date"),
        )
        if value
    ]
    return {
        "title": existing.get("title") or incoming.get("title") or "",
        "author": existing.get("author") or incoming.get("author") or "",
        "author_names": list(dict.fromkeys(
            (existing.get("author_names") or []) + (incoming.get("author_names") or [])
        ))[:12],
        "rank": 1,
        "weeks_at_number_one": min(
            int(existing.get("weeks_at_number_one") or 0)
            + int(incoming.get("weeks_at_number_one") or 0),
            104,
        ),
        "list_names": list(dict.fromkeys(
            (existing.get("list_names") or []) + (incoming.get("list_names") or [])
        ))[:MAX_LIST_NAMES],
        "published_date": max(dates) if dates else "",
        "first_published_date": min(first_dates) if first_dates else "",
    }


def parse_nyt_number_one_pages(
    pages: Mapping[int, str],
    *,
    source_urls: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Build a compact exact-match index from bounded Wikipedia year pages."""

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for year, html in list(sorted(pages.items(), reverse=True))[:MAX_PAGES]:
        if not isinstance(year, int) or not isinstance(html, str) or not html.strip():
            continue
        soup = BeautifulSoup(html, "html.parser")
        for table in soup.select("table.wikitable")[:MAX_TABLES_PER_PAGE]:
            rows = list(_expanded_rows(table))
            if not rows:
                continue
            header = [normalize_text(_text(cell, 80)) for cell in rows[0][:5]]
            if header[:3] != ["issue date", "title", "author s"]:
                continue
            list_name = _list_name(table)
            for row in rows[1:]:
                issue_date = _issue_date(row[0], year)
                title = _text(row[1], 300)
                author = _text(row[2], 300)
                author_names = _author_names(row[2]) if row[2] else []
                if not issue_date or not title or not author or not author_names:
                    continue
                entry = {
                    "title": title,
                    "author": author,
                    "author_names": author_names,
                    "rank": 1,
                    "weeks_at_number_one": 1,
                    "list_names": [list_name],
                    "published_date": issue_date,
                    "first_published_date": issue_date,
                }
                key = (normalize_text(title), normalize_text(author_names[0]))
                if not all(key):
                    continue
                merged[key] = _merge_entry(merged[key], entry) if key in merged else entry
                if len(merged) >= MAX_INDEX_BOOKS:
                    break
            if len(merged) >= MAX_INDEX_BOOKS:
                break
        if len(merged) >= MAX_INDEX_BOOKS:
            break

    books = sorted(
        merged.values(),
        key=lambda item: (
            str(item.get("published_date") or ""),
            int(item.get("weeks_at_number_one") or 0),
            normalize_text(item.get("title")),
        ),
        reverse=True,
    )
    if not books:
        return None
    revision_source = json.dumps(books, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return {
        "version": INDEX_VERSION,
        "published_date": max(str(book.get("published_date") or "") for book in books),
        "revision": hashlib.sha256(revision_source.encode("utf-8")).hexdigest()[:16],
        "source_urls": list(dict.fromkeys(str(url) for url in source_urls if url))[:MAX_PAGES],
        "books": books,
    }


def nyt_index_valid(index: Any) -> bool:
    if not (
        isinstance(index, dict)
        and index.get("version") == INDEX_VERSION
        and re.fullmatch(r"[0-9a-f]{16}", str(index.get("revision") or ""))
        and isinstance(index.get("books"), list)
        and 0 < len(index["books"]) <= MAX_INDEX_BOOKS
        and isinstance(index.get("source_urls", []), list)
        and len(index.get("source_urls", [])) <= MAX_PAGES
    ):
        return False
    return all(
        isinstance(book, dict)
        and bool(normalize_text(book.get("title")))
        and isinstance(book.get("author_names"), list)
        and 0 < len(book["author_names"]) <= 12
        and int(book.get("rank") or 0) == 1
        and isinstance(book.get("list_names", []), list)
        and len(book.get("list_names", [])) <= MAX_LIST_NAMES
        for book in index["books"]
    )


def match_nyt_bestseller(
    index: Any,
    *,
    isbns: Iterable[Any] = (),
    title: Any = "",
    authors: Sequence[Any] = (),
) -> dict[str, Any] | None:
    """Return one unambiguous exact title-and-author identity match."""

    del isbns  # Wikipedia's year tables do not include edition identifiers.
    if not nyt_index_valid(index):
        return None
    normalized_title = normalize_text(title)
    normalized_authors = {normalize_text(author) for author in authors if normalize_text(author)}
    if not normalized_title or not normalized_authors:
        return None
    matches = [
        book
        for book in index["books"]
        if normalize_text(book.get("title")) == normalized_title
        and normalized_authors.intersection(
            normalize_text(author) for author in book.get("author_names", [])
        )
    ]
    return dict(matches[0]) if len(matches) == 1 else None
