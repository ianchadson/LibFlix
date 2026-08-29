"""Bounded parsers for Goodreads public discovery pages.

The networking and cache policy live in ``app.py``. Keeping these parsers
side-effect free makes the public-page integration straightforward to test and
safe to disable when Goodreads changes its markup.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup


def _number(value: Any, *, integer: bool = False) -> float | int:
    cleaned = re.sub(r"[^0-9.]", "", str(value or ""))
    if not cleaned:
        return 0 if integer else 0.0
    try:
        number = float(cleaned)
    except ValueError:
        return 0 if integer else 0.0
    return max(0, int(number)) if integer else max(0.0, number)


def _goodreads_path(value: Any) -> str:
    match = re.match(r"^/book/show/(\d+)(?:[-/?#].*)?$", str(value or "").strip())
    return f"/book/show/{match.group(1)}" if match else ""


def parse_most_read_books(html: str, limit: int = 36) -> list[dict[str, Any]]:
    """Parse Goodreads' weekly/monthly Most Read table in displayed order."""

    soup = BeautifulSoup(html or "", "html.parser")
    books: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in soup.select('tr[itemtype*="schema.org/Book"]'):
        title_link = row.select_one("a.bookTitle")
        author_link = row.select_one("a.authorName")
        if not title_link or not author_link:
            continue
        title = title_link.get_text(" ", strip=True)
        author = author_link.get_text(" ", strip=True)
        path = _goodreads_path(title_link.get("href"))
        identity = (title.casefold(), author.casefold())
        if not title or not author or not path or identity in seen:
            continue

        average = 0.0
        ratings_count = 0
        minirating = row.select_one(".minirating")
        rating_text = minirating.get_text(" ", strip=True) if minirating else ""
        rating_match = re.search(r"([0-5](?:\.\d+)?)\s+avg rating", rating_text)
        count_match = re.search(r"([\d,]+)\s+ratings?", rating_text)
        if rating_match:
            average = _number(rating_match.group(1))
        if count_match:
            ratings_count = _number(count_match.group(1), integer=True)

        activity_count = 0
        statistic = row.select_one(".statistic")
        statistic_text = statistic.get_text(" ", strip=True) if statistic else ""
        activity_match = re.search(r"([\d,]+)\s+people read it", statistic_text)
        if activity_match:
            activity_count = _number(activity_match.group(1), integer=True)

        seen.add(identity)
        books.append({
            "rank": len(books) + 1,
            "title": title,
            "author": author,
            "url": path,
            "average": round(float(average), 2) if 1 <= average <= 5 else 0,
            "ratings_count": int(ratings_count),
            "activity_count": int(activity_count),
        })
        if len(books) >= max(1, min(int(limit or 1), 60)):
            break
    return books
