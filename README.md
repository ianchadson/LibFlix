# LibFlix — Book Discovery Library

A "Netflix for books" web app that combines **Open Library** or **Google Books**
for discovery and **libgen.li** for downloads. Includes separate fiction and
non-fiction browsing modes with curated shelves and category tabs.

## Quick Start

```bash
pip install -r requirements.txt

# Default (Open Library, no key needed):
python3 app.py

# Or with Google Books (faster, richer metadata):
export GOOGLE_BOOKS_API_KEY="your-key"
export BOOK_SOURCE=google
python3 app.py

# → http://localhost:5800
```

## Features

- **Fiction / Non-Fiction switch** — separate shelf sets and category tabs
- **12 non-fiction shelves** — New & Popular, Personal Development, Business, Science, Psychology, History, Biography, Health, Education, Politics, Classics, Awards
- **12 fiction shelves** — New Releases, Sci-Fi, Fantasy, Mystery, Romance, Horror, Historical Fiction, Adventure, YA, Graphic Novels, Literary Fiction, Contemporary Fiction
- **Dynamic hero** — random featured book with synopsis on each visit
- **Category browsing** — dedicated category pages with explicit **Load More** pagination
- **Horizontal shelf expansion** — homepage shelves load more cached pages as you scroll right or press More
- **Google Books backend** (optional) — faster, richer descriptions, clean categories, API key required
- **Open Library backend** (default) — free, no key needed
- **Inline download search** — download options embedded in the book preview page (auto-searches title + author)
- **Persistent discovery cache** — category pages, book details, similar books, and cover checks are cached across restarts
- **Instant search** — page shell renders immediately, results load via AJAX
- **One-click download** — streams EPUB/PDF/MOBI from libgen through Flask proxy
- **Send to Kindle** — email books directly to your Kindle via SMTP (Gmail app password)
- **Dedup** — groups editions by normalized title+author, keeps highest-quality format (EPUB preferred, newest year, has publisher/pages)
- **Previews** — async description + subject tags + "More Like This" shelf
- **Filters** — sort by year/title/size, filter by format/language, toggle dedup

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed data flows, API reference,
and caching strategy.

## Tech Stack

- **Backend:** Flask, BeautifulSoup4, requests (with Session + connection pooling)
- **Frontend:** Bootstrap 5, vanilla JS (no framework)
- **Discovery APIs:** Open Library (free, no key) or Google Books (1,000 req/day free tier)
- **Download source:** modular downloader interface, currently libgen.li (scraped HTML)
- **Port:** 5800 (avoids macOS AirPlay conflict on 5000)

## Configuration

| Env Variable | Values | Purpose |
|---|---|---|
| `BOOK_SOURCE` | `openlibrary` (default) or `google` | Choose book discovery backend |
| `GOOGLE_BOOKS_API_KEY` | Your API key | Required when `BOOK_SOURCE=google` |

Get a Google Books API key: https://console.cloud.google.com/apis/credentials

Send to Kindle requires a Gmail app password and your Kindle email (configured
in-browser — stored in localStorage, never sent to the server).

## Performance

| Metric | Open Library | Google Books |
|---|---|---|
| Homepage books | ~131 | ~207 |
| Shelf response | ~3-5s (cold) | ~1-2s (cold) |
| Description coverage | ~40% | ~90% |
| Cache warm | 2.8s | ~1.5s |
| Disk cache on restart | Instant | Instant |

Discovery API responses are stored in `api_cache.json` for 6 hours, and Google
cover validation checks are cached for 7 days. Category pages use an explicit
Load More button rather than automatic infinite scroll to avoid burning through
Google Books quota accidentally. Runtime cache files are ignored by git.
