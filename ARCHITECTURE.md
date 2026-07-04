# LibFlix Architecture

## Overview

LibFlix is a Flask-based web application that transforms libgen.li into a
"Netflix for books" browsing and download experience. It supports **two book
discovery backends**: Open Library (free, no key) and Google Books (faster,
richer metadata, requires free API key).

## Core UX Flows

### 1. Homepage Browsing (`GET /`)
```
User visits /
  → Server returns cached shelves (warmed on startup, persisted to disk)
  → Hero picks random trending book for the selected mode (with description from source)
  → 12 horizontal shelves render immediately (~3ms with cache hit)
  → User clicks a book card → `/preview?title=...&ol_key=...`
  → Horizontal shelves fetch `/api/shelf/<topic>?page=N` when user scrolls near the end
  → Category tabs in navbar → `/category/<topic>` (explicit Load More pagination)
```

### 2. Category Browsing (`GET /category/<topic>`)
```
Page shell renders instantly with first batch of books (server-side)
  → User clicks Load More
  → GET /api/category/<topic>?page=N
  → New books appended to grid, book count updates
  → Loading spinner shown during fetch
```

### 3. Book Preview (`GET /preview?title=&author=&ol_key=`)
```
Page shell renders instantly (cover, title, author)
  → JS fetches `/api/book?ol_key=` for description + subjects (async)
  → Description appears with skeleton shimmer while loading (HTML stripped)
  → Subjects become clickable tags
  → First subject triggers `/api/similar?subject=` → "More Like This" shelf
  → Libgen download options shown inline (auto-searches title + author)
```

### 4. Search (`GET /search?q=...`)
```
Page renders instantly with search form + spinner
  → JS calls `/api/search?q=...&sort=...&page=...` via fetch
  → Results appear as table with cover thumbnails
  → Filters (sort, order, format, lang, dedup) trigger re-fetch via JS
  → Pagination is AJAX-based — no full page reload
  → "Download" streams file from libgen via Flask proxy
  → "Send to Kindle" opens settings modal on first use, emails file on subsequent
```

## Key Components

### `app.py` (~760 lines)

| Component | Lines | Purpose |
|---|---|---|
| Cache layer | 18-42 | In-memory TTL-based cache for OL/GB API (10min) and libgen HTML (15min) |
| Book source config | 17 | `BOOK_SOURCE` env var: `openlibrary` (default) or `google` |
| `SHELVES_DEF` / `FICTION_SHELVES_DEF` | app.py | Separate non-fiction and fiction subject categories for homepage shelves |
| `ol_get` | 27-40 | Open Library API caller with caching |
| `gb_get` | 109-123 | Google Books API caller with caching (uses `GOOGLE_BOOKS_API_KEY` env) |
| `extract_book` / `gb_extract_book` | 65-84, 125-140 | Filters OL/GB results to English + cover + author only |
| `fetch_one_shelf` | 144-175 | Fetches a single category from OL or GB (based on `BOOK_SOURCE`) |
| `fetch_shelves` | 177-185 | Parallel (6 workers) fetcher for all 12 shelves |
| `fetch_search` | 195-212 | Libgen.li search with caching |
| `parse_results` | 215-260 | BS4 parser for libgen's HTML table |
| `dedup` | 340-349 | Groups by normalized title+author, keeps highest-scored format |
| `strip_html` | 363-368 | Strips HTML tags and unescapes entities from descriptions |
| `GET /` | 428-456 | Returns cached shelves + random hero |
| `GET /category/<topic>` | 458-465 | Server-rendered category page with first batch of books |
| `GET /api/category/<topic>` | app.py | Paginated JSON endpoint for explicit Load More category browsing |
| `GET /api/shelf/<topic>` | app.py | Paginated JSON endpoint for horizontal shelf expansion |
| `GET /preview` | 477-485 | Instant page render, async description load |
| `GET /api/book` | 521-558 | JSON endpoint for book details (OL or GB) |
| `GET /api/similar` | 498-520 | JSON endpoint for similar books |
| `GET /api/search` | 401-445 | JSON endpoint for libgen search |
| `GET /download/<md5>` | 530-539 | Proxies file download from libgen |
| `POST /api/sendtokindle` | 562-620 | Downloads from libgen, emails via user's SMTP |
| `/olcover` / `/gbcover` | 607-633 | Proxies cover images (S/M/L sizes) |
| `warm_cache` | 636-648 | Background thread on startup, loads disk cache instantly |

### Templates

| Template | Purpose |
|---|---|
| `templates/_navbar.html` | Shared navbar — logo, search bar, category tabs inline (responsive, scrollable) |
| `templates/_book_card.html` | Shared book card partial (cover, title, author overlay) |
| `templates/index.html` | Homepage — hero + 12 shelves, uses `_navbar` and `_book_card` |
| `templates/category.html` | Category browsing — server-rendered first batch + explicit Load More pagination |
| `templates/search.html` | Two-phase search — instant shell + async results + filter controls + Kindle send |
| `templates/book.html` | Book preview — detail view with async description + inline libgen results + Kindle send |

### Data Flow

```
Browser                    Flask Server                  External APIs
-------                    ------------                  -------------
[Homepage]                 GET /
  ↓                           ↓ (cache hit ~3ms)
  render shelves              fetch_shelves() ──→ ThreadPool(6) ──→ OL or GB API
  render hero                                             ↓
  ↓                           ↓                          cache 10min
[click category tab]        GET /category/<topic>
  ↓                           ↓ fetch_one_shelf (first batch)
  render grid                 ↓ render with _book_card partials
  ↓
  Click Load More ────────→ GET /api/category/<topic>?page=N ──→ OL or GB API (paginated/cacheable)
  → append books to grid     ↓ return JSON
  ↓
[click card]                GET /preview?ol_key=X
  ↓                           ↓ (instant)
  render shell                render book.html
  fetch /api/book ──────────→ GET /api/book ──────────→ OL work API or GB volume API
  fetch /api/similar ───────→ GET /api/similar ───────→ OL or GB subject search
  fetch /api/search ────────→ GET /api/search ────────→ libgen.li HTML
  ↓ render results table
[click Download]             GET /download/<md5>
  ↓                           ↓ resolve_download() ─→ libgen.li ads.php
  stream file                 ↓ stream from dl link
  ↓
[click Send to Kindle]       POST /api/sendtokindle
  ↓                           ↓ resolve + download
  show sending overlay        ↓ email via SMTP (user's Gmail)
  show ✓ Sent                 ↓ return JSON
```

## API Reference

### `GET /api/category/<topic>`
Fetch paginated books for a category shelf.

**Params:** `page` (1-based)

### `GET /api/shelf/<topic>`
Fetch paginated books for a homepage horizontal shelf.

**Params:** `page` (1-based), `mode` (`fiction` or `nonfiction`)

**Returns:**
```json
{
  "success": true,
  "books": [{"title": "...", "author": "...", "cover_url": "/gbcover/...", "ol_key": "..."}],
  "page": 1,
  "total_pages": 16,
  "total": 300
}
```

### `GET /api/search`
Search libgen.li for books.

**Params:** `q`, `sort` (y|title|author|filesize|extension), `order` (ASC|DESC), `limit`, `page`, `format` (epub|pdf|mobi|all), `lang` (English|all), `dedup` (0|1)

**Returns:**
```json
{
  "success": true, "query": "atomic habits", "total": 42,
  "total_pages": 2, "page": 1,
  "books": [{"idx": 1, "title": "Atomic Habits", "author": "James Clear", "year": "2019", "ext": "epub", "size": "2.5 MB", "md5": "...", "cover_url": "/cover/...?dir=4322000", "publisher": "", "language": "English", "pages": ""}]
}
```

### `GET /api/book`
Fetch book details (description + subjects) from the configured source.

**Params:** `ol_key` (OL work key `/works/OL123W` or GB volume ID)

**Returns:** `{ success, title, description, subjects[] }`

### `GET /api/similar`
Find similar books by subject.

**Params:** `subject`, `ol_key` (to exclude self)

**Returns:** `{ success, books: [{ title, author, cover_url, ol_key }] }`

### `POST /api/sendtokindle`
Email a book to Kindle via SMTP.

**Body:** `{ md5, title, ext, kindle_email, smtp_host, smtp_port, smtp_user, smtp_pass, sender_email }`

**Returns:** `{ success }` or `{ success: false, error }`

## Caching Strategy

| Data | TTL | Key Format | Warmed On |
|---|---|---|---|
| Open Library / Google Books API | 10 min | `ol:{path}:{params}` or `gb:{path}:{params}` | First request |
| Open Library / Google Books API disk cache | 6 hours | hashed cache key in `api_cache.json` | Reused across restarts |
| Google cover validation | 7 days | `gbcover-check:{volume_id}:zoom3` in memory + `api_cache.json` | First verified cover |
| Libgen search HTML | 15 min | `lg:{query}:{sort}:{order}:{page}:{limit}` | First search |
| Homepage shelves | 1 hour | `shelves_{mode}` (in-memory + `shelf_cache_{mode}.json` on disk) | **Server startup** (instant from disk, refreshed in background) |
| Cover images | 24h | (HTTP Cache-Control) | First load |

## Configuration

### Book Source
Set via environment variable:
```bash
export BOOK_SOURCE=google          # Use Google Books API
export BOOK_SOURCE=openlibrary     # Use Open Library (default)
```

### Google Books API Key
Required only when `BOOK_SOURCE=google`:
```bash
export GOOGLE_BOOKS_API_KEY="your-key-here"
```
Get a free key from https://console.cloud.google.com/apis/credentials
(1,000 requests/day free tier). Never hardcode the key.

### Templates

All templates use shared partials (`_navbar.html`, `_book_card.html`) for consistency. The navbar includes the Fiction / Non-Fiction switch and mode-specific category tabs. Category pages use explicit Load More pagination to keep Google Books quota use user-driven. Searching/navigating shows a full-screen loading overlay (triggered by `a[href^="/search"]` and `a[href^="/preview"]` click handlers).
