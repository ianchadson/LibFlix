# LibFlix - Book Discovery Library

LibFlix is a Netflix-style web app for browsing books, previewing metadata, and
finding download options. Discovery is powered by Open Library. Downloads are
handled separately through the modular downloader layer, currently backed by
libgen.li.

The app supports fiction and non-fiction browsing, English and Chinese discovery
filters, Open Library search, book previews, similar-book shelves, inline
download search, direct downloads, and Send to Kindle via the user's SMTP
settings.

## Quick Start

```bash
pip install -r requirements.txt
python3 app.py

# App URL:
# http://127.0.0.1:5800
```

No API key is required. Open Library is the only discovery backend.

## Current Feature Set

### Discovery

- **Open Library-only discovery** - Google Books integration and all source
  switching UI were removed. Old URLs containing `source=google` are ignored and
  are not re-emitted by the app.
- **Fiction / Non-Fiction mode switch** - each mode has its own shelf set and
  category tabs.
- **EN / CN language toggle** - the navbar can switch discovery between English
  (`eng`) and Chinese (`chi`) Open Library records.
- **Search bar searches discovery first** - the global search bar routes to
  `/discover`, which searches Open Library for books. It does not jump directly
  to download search.
- **Download search is contextual** - download options are searched from the book
  preview page using the selected title and author.

### Homepage

- **Dynamic hero** - the homepage picks a random trending book from the active
  mode and language, then loads its Open Library description when available.
- **Fuller shelves by default** - shelf requests fetch larger Open Library
  batches and render up to 40 books per shelf initially.
- **No global shelf dedupe** - shelves keep their own results, avoiding short
  rows caused by books being removed because they appeared in an earlier shelf.
- **Horizontal infinite scroll** - homepage shelves automatically load another
  page when the user scrolls near the end of a row.
- **Compact More affordance** - a small round arrow button remains as a fallback
  at the end of each shelf instead of a full-height tile.
- **Hidden horizontal scrollbars** - homepage shelf rows hide scrollbars while
  preserving horizontal scrolling.

### Category Pages

- **Vertical infinite scroll** - category pages render the first batch
  server-side, then automatically append more books as the user nears the bottom
  of the grid.
- **No manual Load More button** - category pagination is automatic via a scroll
  sentinel and scroll fallback.
- **No visible total counts** - labels such as `80 books`, `x shown`, and
  result totals were removed because they do not help the browsing experience.

### Book Preview

- **Async Open Library details** - description and subject tags load after the
  preview shell renders.
- **More Like This** - the first subject loads a horizontal similar-books shelf.
- **Hidden More Like This scrollbar** - the shelf scrolls horizontally without a
  visible scrollbar.
- **Inline download options** - libgen download results load directly below the
  metadata for the current title and author.
- **Send to Kindle** - users can save SMTP and Kindle email settings in
  localStorage, then email a downloaded file to Kindle.

### Download Search

- **AJAX results** - download results update without a full page reload.
- **Filters** - sort, format, limit, language, and dedupe controls update the
  download search.
- **Deduplication** - results can be grouped by normalized title and author,
  keeping the highest-scored candidate.
- **No visible result totals** - count summaries were removed from search and
  preview download tables.

## Main Routes

| Route | Purpose |
|---|---|
| `/` | Homepage with hero and horizontal shelves |
| `/category/<topic>` | Category grid with vertical infinite scroll |
| `/discover?q=...` | Open Library discovery search results |
| `/preview?title=...&author=...&ol_key=...` | Book detail, similar books, download search |
| `/search?q=...` | Direct libgen download search page |
| `/download/<md5>` | Proxied file download |
| `/api/shelf/<topic>` | JSON endpoint for homepage shelf pagination |
| `/api/category/<topic>` | JSON endpoint for category infinite scroll |
| `/api/discover` | JSON endpoint for discovery search pagination |
| `/api/book` | JSON endpoint for Open Library work details |
| `/api/similar` | JSON endpoint for similar Open Library books |
| `/api/search` | JSON endpoint for libgen download search |
| `/api/sendtokindle` | Send selected download to Kindle via SMTP |

## Configuration

| Env Variable | Values | Purpose |
|---|---|---|
| `BOOK_LANG` | `en` or `cn` | Optional default discovery language |

Runtime Send to Kindle settings are configured in the browser and stored in
localStorage. The SMTP password is sent only to `/api/sendtokindle` when sending
a book.

## Runtime Cache Files

The app writes local runtime cache files to speed up restart and repeated API
requests. They are ignored by git.

| File Pattern | Purpose |
|---|---|
| `api_cache.json` | Disk cache for Open Library/API responses |
| `shelf_cache_<lang>_<mode>.json` | Warm shelf cache for each language and mode |
| `shelf_cache*.json` | Historical and current shelf cache files ignored by git |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for data flow, API contracts, caching
details, and template responsibilities.

See [CHANGELOG.md](CHANGELOG.md) for the recent Open Library-only discovery,
language, search, infinite scroll, and count-removal changes.

## Tech Stack

- **Backend:** Flask, requests, BeautifulSoup4
- **Frontend:** Bootstrap 5 and vanilla JavaScript
- **Discovery:** Open Library Search/Works/Covers APIs
- **Downloads:** Modular downloader interface, currently libgen.li
- **Port:** 5800

## Verification

Useful local checks:

```bash
python3 -m compileall app.py
python3 app.py
```

Then open:

```text
http://127.0.0.1:5800
http://127.0.0.1:5800/category/history?mode=nonfiction&book_lang=en
http://127.0.0.1:5800/discover?q=三体&mode=fiction&book_lang=cn
```
