# LibFlix Architecture

## Overview

LibFlix is a Flask app with two distinct data paths:

1. **Discovery path:** Open Library powers browsing, shelves, category pages,
   search discovery, book details, covers, and similar books.
2. **Download path:** the `downloaders/` package powers libgen search, download
   resolution, streaming, and Send to Kindle delivery.

Discovery has a single backend. Open Library is the source for browsing,
metadata, covers, similar books, and discovery search.

## User-Facing Flow Map

### Homepage (`GET /`)

```text
Browser requests /
  -> Flask reads mode and book_lang
  -> get_shelves(mode, lang)
       -> memory cache
       -> disk shelf cache
       -> normalize shelf labels for current definitions
       -> shelf-order dedupe and refill
       -> Open Library search when cache is cold or a shelf needs refill
  -> render index.html with fixed-height cycleable hero + shelves
  -> user scrolls a shelf horizontally
  -> JS fetches /api/shelf/<topic>?page=N&mode=...&book_lang=...
  -> new book cards are inserted before the compact arrow button
```

Important behavior:

- Shelves are language-aware.
- The first shelf is labeled `Trending` in both fiction and non-fiction.
- Cached shelf labels are normalized during render so old cache files using
  labels such as `New & Popular` do not leak into the UI.
- Each shelf initially renders up to 40 books.
- Shelves are deduped in top-to-bottom priority order. A book that appears in
  an earlier shelf is excluded from later shelves.
- Later shelves try to refill from deeper Open Library pages after duplicates
  are removed.
- Horizontal scrollbars are hidden.
- The compact More button is a fallback; normal loading is scroll-triggered.
- The hero's carousel controls are fixed within the hero and do not shift when
  titles, authors, descriptions, or covers change.
- Hero side covers, dots, and arrow buttons can all change the active featured
  book.

### Category Page (`GET /category/<topic>`)

```text
Browser requests /category/history?mode=nonfiction&book_lang=en
  -> Flask validates topic against the active mode
  -> fetch_one_shelf(name, topic, lang)
  -> render category.html with first batch
  -> IntersectionObserver watches a bottom sentinel
  -> user scrolls near bottom
  -> JS fetches /api/category/<topic>?page=N&mode=...&book_lang=...
  -> cards append to the grid
```

Important behavior:

- There is no visible Load More button.
- A scroll listener acts as a fallback when IntersectionObserver is unavailable.
- The loading spinner appears only while a page is being fetched.
- Category pages do not show total count labels.

### Discovery Search (`GET /discover`)

```text
Navbar search form submits to /discover
  -> Flask uses fetch_discovery_books(q, page, lang)
  -> Open Library search results render as book cards
  -> bottom scroll sentinel fetches /api/discover automatically
  -> clicking a card opens /book/<work_id>
```

This route searches Open Library discovery data only. It does not search the
download source directly.

### Book Preview (`GET /book/<work_id>`)

```text
Browser requests /book/OL3431878W
  -> Flask resolves /works/OL3431878W through Open Library
  -> Flask renders book.html with title, author, cover, and work key
  -> JS fetches /api/book?ol_key=...&book_lang=...
  -> description and subject tags render asynchronously
  -> first subject triggers /api/similar
  -> JS fetches /api/search for download options
```

Legacy `/preview?...&ol_key=/works/...` URLs redirect to the matching clean
book route and drop title, author, cover, mode, and language query noise.

Important behavior:

- `/api/book` accepts Open Library work keys only.
- Similar books are Open Library subject searches.
- The More Like This shelf hides horizontal scrollbars.
- Download result count summaries are hidden.

### Download Search (`GET /search`)

```text
Browser requests /search?q=...
  -> Flask renders search.html shell
  -> JS calls /api/search with filters
  -> libgen results render in a table
  -> user can download or send to Kindle
```

This is intentionally separate from `/discover`. The global navbar search is
for discovery; download search is available from previews and direct `/search`
URLs.

## Backend Components

### Discovery Configuration

```python
BOOK_LANGS = {"en", "cn"}
BOOK_LANG_CONFIG = {
    "en": {"label": "EN", "ol_lang": "eng"},
    "cn": {"label": "CN", "ol_lang": "chi"},
}
```

- `get_book_lang()` reads `book_lang` from query string or cookie.
- `lang_url()` preserves the current route and query, but strips obsolete
  `source` parameters from old links.
- `shelf_query(topic, lang)` adds an Open Library language filter to each query.

### Open Library Helpers

| Function | Responsibility |
|---|---|
| `ol_get(path, params)` | Cached Open Library JSON request |
| `ol_get_work(ol_key)` | Work detail lookup |
| `shelf_query(topic, lang)` | Mode/topic/language Open Library query builder |
| `extract_book(record, lang)` | Normalize Open Library search record to app book card |
| `first_matching_edition(record, lang)` | Prefer an edition matching the active language |
| `edition_cover_id(edition)` | Pick a usable Open Library cover id |
| `fetch_one_shelf(name, topic, lang)` | Server-rendered first shelf/category batch |
| `fetch_category_books(topic, page, lang)` | Paginated category/home shelf JSON source |
| `collect_unique_topic_books(topic, lang, seen_keys, target)` | Pull deeper Open Library pages until a shelf has unique books or pages are exhausted |
| `prefetch_topic_pages(topics, lang, max_pages)` | Fetch bounded candidate pages for homepage shelves in parallel |
| `select_unique_from_prefetched(topic, candidate_pages, seen_keys, target)` | Select unique shelf books from prefetched candidates without more network calls |
| `normalize_shelf_labels(shelves, mode)` | Re-map cached shelf names to the current fiction/non-fiction shelf definitions |
| `dedupe_and_refill_shelves(shelves, mode, lang)` | Apply homepage shelf priority and top up later shelves |
| `seen_keys_before_shelf(topic, mode, lang)` | Build exclusion keys from all earlier homepage shelves |
| `fetch_shelf_page_books(topic, page, mode, lang)` | Return logical horizontal shelf pages after cross-shelf dedupe |
| `fetch_discovery_books(q, page, lang)` | Paginated `/discover` JSON source |
| `fetch_shelves(mode, lang)` | Homepage shelf builder using parallel candidate prefetch plus top-to-bottom dedupe |

### Download Helpers

Download logic is intentionally modular:

```text
downloaders/
  __init__.py      selects the active downloader
  base.py          downloader protocol and shared session
  libgen.py        libgen.li implementation
```

The Flask layer uses `DOWNLOADER.search()` and
`DOWNLOADER.resolve_download()` rather than hardcoding libgen behavior in the
route handlers.

## Routes And API Contracts

### `GET /`

Renders homepage shelves and hero.

Params:

| Param | Values | Purpose |
|---|---|---|
| `mode` | `fiction`, `nonfiction` | Active browsing mode |
| `book_lang` | `en`, `cn` | Active discovery language |

### `GET /category/<topic>`

Renders the first page of a category grid. The template then handles infinite
scroll by calling `/api/category/<topic>`.

### `GET /api/category/<topic>`

Params:

| Param | Values | Purpose |
|---|---|---|
| `page` | integer | 1-based Open Library page |
| `mode` | `fiction`, `nonfiction` | Validates topic against mode |
| `book_lang` | `en`, `cn` | Language filter |

Returns:

```json
{
  "success": true,
  "books": [
    {
      "title": "A Brief History of Time",
      "author": "Stephen Hawking",
      "cover_url": "/olcover/240726",
      "ol_key": "/works/OL82563W"
    }
  ],
  "page": 2,
  "total_pages": 25,
  "total": 12345
}
```

### `GET /api/shelf/<topic>`

Same shape as `/api/category/<topic>`. Used by horizontal homepage shelves.

### `GET /discover`

Renders discovery results from Open Library.

Params:

| Param | Values | Purpose |
|---|---|---|
| `q` | string | Title, author, or subject-like discovery query |
| `page` | integer | 1-based results page |
| `mode` | `fiction`, `nonfiction` | Maintains navbar mode |
| `book_lang` | `en`, `cn` | Language filter |

### `GET /api/discover`

JSON endpoint backing discover pagination. Returns the same book-card shape as
category and shelf APIs.

### `GET /api/book`

Params:

| Param | Values | Purpose |
|---|---|---|
| `ol_key` | `/works/...` | Open Library work key |
| `book_lang` | `en`, `cn` | Active language context |

Returns:

```json
{
  "success": true,
  "title": "Cosmos",
  "description": "...",
  "subjects": ["Science", "Astronomy"]
}
```

Non-Open-Library keys return `Book not found`.

### `GET /api/similar`

Params:

| Param | Purpose |
|---|---|
| `subject` | Open Library subject string |
| `ol_key` | Current work key, excluded from results |
| `book_lang` | Language filter |

### `GET /api/search`

Libgen download search.

Params:

| Param | Values |
|---|---|
| `q` | query string |
| `sort` | `y`, `id`, `title`, `author`, `filesize`, `extension`, `time_added` |
| `order` | `ASC`, `DESC` |
| `limit` | `25`, `50`, `100` |
| `format` | `epub`, `pdf`, `mobi`, `all` |
| `lang` | `English`, `all` |
| `dedup` | `0`, `1` |
| `page` | integer |

### `GET /download/<md5>`

Resolves the md5 through the active downloader and streams the remote file
through Flask.

### `POST /api/sendtokindle`

Downloads the selected file, builds an email attachment, and sends it through
the SMTP credentials supplied by the browser.

## Template Responsibilities

| Template | Responsibility |
|---|---|
| `_navbar.html` | Shared nav, wide-screen category tabs, expandable discovery search, expandable mode/language settings |
| `_book_card.html` | Shared card link, cover, placeholder, hover/focus metadata |
| `index.html` | Fixed-height hero, cover-stack carousel, homepage shelves, horizontal shelf infinite scroll |
| `category.html` | Category grid and vertical infinite scroll |
| `discover.html` | Open Library discovery result cards and vertical infinite scroll |
| `book.html` | Preview metadata, similar shelf, download results, Kindle modal |
| `search.html` | Direct download search page and Kindle modal |
| `results.html` | Older server-rendered download table fallback |

## Frontend Interaction Details

### Shared App Chrome

`_navbar.html` owns the wide-screen top bar, expandable search, expandable
settings menu, shared route-transition overlay, and quick-peek book preview
behavior.

The collapsed search and settings controls are icon-only. Search expands on
focus or click, then submits to `/discover`; settings expands to reveal the
fiction/non-fiction and EN/CN choices.

The navbar exposes:

```js
window.LibFlixLoading = { show: showTransition, hide: hideTransition };
```

Internal links and forms call `showTransition()` before navigation where the
browser can do so safely. This keeps slow Open Library and libgen-backed pages
feeling like an app transition rather than a blank page wait. Legacy result
redirects also use the shared loader when available.

### Homepage Hero

The hero is intentionally fixed-height. Text updates, backdrop layers, and cover
stack layers animate inside that stable frame so the first shelf below the hero
does not jump while the active book changes.

Hero title fitting is handled in the browser:

- titles use normal word wrapping, never character wrapping
- short titles prefer `white-space: nowrap`
- long titles scale down only enough to fit their container
- the carousel control bar stays pinned inside the hero regardless of text
  height

The cover stack is ordered around the active book. The primary cover is centered,
side covers show neighboring books, and clicking a side cover jumps to that
book. Arrow buttons and dots call the same render path.

The background combines the active cover blur, a drifting cover strip, light
sweep, grid/static overlays, and cover glints. `prefers-reduced-motion` disables
the continuous animations while preserving the static composition.

### Book Card Quick Peek

Book cards are quiet by default. Title and author overlays appear on hover/focus,
and `_navbar.html` attaches a delegated quick-peek overlay for cards that expose
an Open Library work key.

Quick peek behavior:

- waits briefly before opening so normal cursor movement does not spam requests
- shows title and author immediately from card data
- fetches `/api/book?ol_key=...&book_lang=...` for description details
- caches successful detail responses per work key
- tracks the latest pointer position and repositions on `pointermove`
- clamps itself to the viewport so it stays near the cursor and does not drift
  off screen
- omits subject/category tags to reserve space for the description

### Homepage Shelf Infinite Scroll

Each shelf stores state on the shelf element:

```html
<div class="shelf" data-topic="history" data-page="1" data-total-pages="25" data-loading="0">
```

When the row scroll position approaches the right edge, `loadShelfMore()` calls
`/api/shelf/<topic>` and inserts cards before the compact arrow button.

Homepage book cards carry Open Library key, title, and author data attributes.
The browser runs a shelf-priority sweep on initial render and after horizontal
loads, removing any duplicate card from later shelves if an earlier shelf has
already claimed the same work/title-author identity.

### Category Infinite Scroll

`category.html` uses:

- `#scrollSentinel` at the bottom of the grid
- `IntersectionObserver` with a `700px` root margin
- `window.scroll` fallback via `nearPageBottom()`
- `fillViewportIfNeeded()` for short initial grids

### Hidden Counts

The UI intentionally avoids visible total/count text in browsing surfaces. The
API still returns `total` and `total_pages` so pagination logic can work, but
templates do not render count summaries such as:

- `80 books`
- `x shown`
- `x of y results`
- `Page x of y`

### Hidden Scrollbars

Homepage shelves and the More Like This shelf keep scroll behavior but hide
visible scrollbars using:

```css
scrollbar-width: none;
-ms-overflow-style: none;
```

and WebKit scrollbar hiding rules.

## Caching Strategy

| Data | Store | Key Pattern | TTL / Lifetime |
|---|---|---|---|
| Open Library JSON | memory | `ol:{path}:{params}` | 1 hour |
| Open Library JSON | disk `api_cache.json` | SHA-256 of request key | 6 hours |
| Homepage shelves | memory | `shelves_{lang}_{mode}` | 1 hour |
| Homepage shelves | disk | `shelf_cache_{lang}_{mode}.json` | reused on restart |
| Cover images | HTTP response | `/olcover/<cover_id>/<size>` | 24 hours |

Runtime cache files are ignored by git.

## Discovery Source

Open Library is the only discovery source documented for the app. The language
URL helper strips obsolete source parameters from older links so current routes
stay focused on mode, language, category, and query state.
