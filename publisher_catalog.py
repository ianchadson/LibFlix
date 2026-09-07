"""Reviewed publisher metadata for identity search only, never download discovery.

Records are deliberately curated, not a claim of complete publisher coverage.
Prefer an Open Library match when one becomes available.
"""
import re
import unicodedata


PUBLISHER_BOOKS = (
    {
        "title": "Beneath the Cards",
        "author": "Garrett Adelstein",
        "isbn": "9798895658154",
        "alternate_isbns": ["9798895658147"],
        "publisher_url": "https://www.simonandschuster.com.au/books/Beneath-the-Cards/Garrett-Adelstein/9798895658154",
        "cover_url": "/publishercover/9798895658154",
        "cover_source_url": "https://d28hgpri8am2if.cloudfront.net/book_images/onix/cvr9798895658154/beneath-the-cards-9798895658154_lg.jpg",
        "source": "publisher",
        "download_available": False,
    },
)


def _normalize(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", str(value)).casefold()))


def supplement_search(query, books, total, total_pages, page=1, lang="en"):
    """Merge first-page, precise identity matches without mutating cached results."""
    if page != 1 or lang != "en":
        return books, total, total_pages
    normalized = _normalize(query)
    isbn = re.sub(r"[\s-]", "", query.casefold()).removeprefix("isbn:")
    additions = []
    for record in PUBLISHER_BOOKS:
        title, author = _normalize(record["title"]), _normalize(record["author"])
        identities = {title, author, f"{title} by {author}", f"{title} {author}"}
        isbns = {record["isbn"], *record["alternate_isbns"]}
        if normalized not in identities and isbn not in isbns:
            continue
        if any(
            str(book.get("isbn", "")) in isbns
            or (_normalize(book.get("title", "").split(":")[0]) == title
                and _normalize(book.get("author", "")) == author)
            for book in books
        ):
            continue
        additions.append({**record, "alternate_isbns": list(record["alternate_isbns"])})
    if not additions:
        return books, total, total_pages
    return additions + list(books), (total or 0) + len(additions), max(1, total_pages or 0)
