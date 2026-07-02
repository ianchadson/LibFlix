# LibFlix Architecture

## Overview

LibFlix is a Flask-based web application that transforms libgen.li into a
"Netflix for books" browsing and download experience. It uses Open Library's
free API for book discovery (trending, subject shelves, descriptions) and
libgen.li for file downloads.

## Core UX Flows

### 1. Homepage Browsing (`GET /`)
```
User visits /
  → Server returns cached shelves (warmed on startup)
  → Hero picks random trending non-fiction book
  → 12 horizontal shelves render immediately (~3ms)
  → User clicks a book card → `/preview?title=...&ol_key=...`
  → User clicks "Download" on hero → `/search?q=...`
```

### 2. Book Preview (`GET /preview?title=&author=&ol_key=`)
```
Page shell renders instantly (cover, title, author)
  → JS fetches `/api/book?ol_key=` for description + subjects (async)
  → Description appears with skeleton shimmer while loading
  → Subjects become clickable tags
  → First subject triggers `/api/similar?subject=` → "More Like This" shelf
  → User clicks "Search LibGen" → `/search?q=...`
```

### 3. Search (`GET /search?q=...`)
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

### `app.py` (565 lines)

| Component | Lines | Purpose |
|---|---|---|
| Cache layer | 14-38 | In-memory TTL-based cache for OL API responses (10min) and libgen HTML (5min) |
| `SHELVES_DEF` | 45-57 | 12 non-fiction subject categories for homepage shelves |
| `extract_book` | 60-77 | Filters OL results to English + cover + author only |
| `fetch_shelves` | 91-110 | Parallel (6 workers) fetcher for all 12 shelves |
| `fetch_search` | 128-146 | Libgen.li search with caching |
| `parse_results` | 148-182 | BS4 parser for libgen's HTML table |
| `dedup` | 258-267 | Groups by normalized title+author, keeps highest-scored format |
| `GET /` | 305-314 | Returns cached shelves + random hero |
| `GET /preview` | 323-342 | Instant page render, async description load |
| `GET /api/search` | 358-444 | JSON endpoint for libgen search |
| `GET /download/<md5>` | 446-461 | Proxies file download from libgen |
| `POST /api/sendtokindle` | 475-522 | Downloads from libgen, emails via user's SMTP |
| `olcover` | 529-540 | Proxies Open Library cover images (S/M/L sizes) |
| `warm_cache` | 546-550 | Background thread on startup |

### Templates

| Template | Purpose |
|---|---|
| `templates/index.html` | Homepage — hero + 12 draggable shelves, error states |
| `templates/search.html` | Two-phase search — instant shell + async results + filter controls + Kindle send |
| `templates/book.html` | Book preview — detail view with async description + similar shelf |
| ~~`templates/results.html`~~ | *Replaced by search.html (client-side render)* |

### Data Flow

```
Browser                    Flask Server                  External APIs
-------                    ------------                  -------------
[Homepage]                 GET /
  ↓                           ↓ (cache hit ~3ms)
  render shelves              fetch_shelves() ──→ ThreadPool(6) ──→ Open Library API
  render hero                                             ↓
  ↓                           ↓                          cache 10min
[click card]               GET /preview?ol_key=X
  ↓                           ↓ (instant)
  render shell                render book.html
  fetch /api/book ──────────→ GET /api/book ──────────→ Open Library work API
  fetch /api/similar ───────→ GET /api/similar ───────→ Open Library subject search
  ↓
[click Search LibGen]      GET /search?q=...
  ↓                           ↓ (instant)
  render spinner              render search.html
  fetch /api/search ────────→ GET /api/search ────────→ libgen.li HTML
                                parse_results(bs4)
                                dedup()
                                return JSON
  ↓                           ↓ cache 5min
  render results table
  ↓
[click Download]            GET /download/<md5>
  ↓                           ↓ resolve_download() ─→ libgen.li ads.php
  stream file                 ↓ stream from dl link
  ↓
[click Send to Kindle]      POST /api/sendtokindle
  ↓                           ↓ resolve + download
  show sending overlay        ↓ email via SMTP (user's Gmail)
  show ✓ Sent                 ↓ return JSON
```

## API Reference

### `GET /api/search`
Search libgen.li for books.

**Params:** `q`, `sort` (y\|title\|author\|filesize\|extension), `order` (ASC\|DESC), `limit`, `page`, `format` (epub\|pdf\|mobi\|all), `lang` (English\|all), `dedup` (0\|1)

**Returns:**
```json
{
  "success": true,
  "query": "atomic habits",
  "total": 42,
  "total_pages": 2,
  "page": 1,
  "books": [
    {
      "idx": 1, "title": "Atomic Habits", "author": "James Clear",
      "year": "2019", "ext": "epub", "size": "2.5 MB",
      "md5": "65e5bdb6807bb180ed3af728f3327226",
      "cover_url": "/cover/65e5bdb68...?dir=4322000",
      "publisher": "", "language": "English", "pages": ""
    }
  ]
}
```

### `GET /api/book`
Fetch Open Library work details.

**Params:** `ol_key` (e.g. `/works/OL21628013W`)

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
| Open Library API responses | 10 min | `ol:{path}:{params}` | First request |
| Libgen search HTML | 5 min | `lg:{query}:{sort}:{order}:{page}:{limit}` | First search |
| Homepage shelves | 10 min | `shelves` | **Server startup** (background thread) |
| Cover images | 24h | (HTTP Cache-Control) | First load |

## Non-Fiction Focus

The app is curated for non-fiction content:

1. **Shelf subjects** exclude fiction: `q=subject:{topic} -subject:Fiction`
2. **Trending** sources from non-fiction search: `q=subject:Nonfiction -subject:Fiction`
3. **Book filter** drops non-English titles (ASCII heuristic), books without covers, books without authors
4. **Hero** randomly picks from the (filtered) trending shelf
