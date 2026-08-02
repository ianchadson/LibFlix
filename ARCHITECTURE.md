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
       -> shelf wrappers render as stable skeletons
       -> cached hero descriptions render when already assembled
       -> missing active description hydrates through /api/book?description_only=1
  -> IntersectionObserver hydrates each complete shelf near the viewport
  -> user scrolls a shelf horizontally
  -> JS fetches /api/shelf/<topic>?page=N&mode=...&book_lang=...
  -> new book cards are inserted before the compact arrow button
```

Important behavior:

- Shelves are language-aware.
- The first shelf is labeled `Trending` in both fiction and non-fiction.
- Cached shelf labels are normalized during render so old cache files using
  labels such as `New & Popular` do not leak into the UI.
- The initial HTML contains shelf skeletons rather than hundreds of cards.
- A shelf hydrates a complete 40-book first page near the viewport, so
  progressive rendering never exposes an artificially short row.
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
- Only the active hero description is fetched. Side-book descriptions remain
  dormant until selection.

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

Discovery uses Open Library's unqualified relevance query, then applies a local
language guard to every record. This avoids hiding newly catalogued works whose
Open Library records do not yet contain language or cover metadata, while still
rejecting records explicitly tagged in another language. Coverless matches use
the standard placeholder rather than disappearing, and only one upstream search
is required per discovery page.

### Book Preview (`GET /book/<work_id>`)

```text
Browser requests /book/OL3431878W
  -> Flask reads assembled detail cache or card-backed local hint
  -> Flask renders title, author, cover, cached description, and work key
     without waiting for Open Library
  -> JS fetches /api/book?ol_key=...&book_lang=...
  -> local response is returned immediately and may report refreshing=true
  -> bounded background task refreshes missing/stale metadata
  -> cached subjects seed /api/similar without blocking the page
  -> JS fetches /api/search for download options
  -> shared download-ui.js renders responsive edition rows
```

Legacy `/preview?...&ol_key=/works/...` URLs redirect to the matching clean
book route and drop title, author, cover, mode, and language query noise.

Important behavior:

- `/api/book` accepts Open Library work keys only.
- Book HTML is local-first; upstream metadata is never required before first
  render.
- Assembled details stay fresh for seven days and remain eligible as stale
  fallback for up to 90 days.
- The browser performs at most two quiet detail/similar retries while a
  background refresh is running.
- Similar books are Open Library subject searches; subjects remain internal and
  are not rendered as chips in the preview UI.
- The More Like This shelf hides horizontal scrollbars.
- More Like This uses the shared card and quick-peek behavior.
- Long descriptions clamp on compact screens and can be expanded in place.
- Download result count summaries are hidden.
- A single result page has no pagination controls.

### Download Search (`GET /search`)

```text
Browser requests /search?q=...
  -> Flask renders search.html shell
  -> JS calls /api/search with filters
  -> shared download-ui.js renders responsive edition rows
  -> user can download or send to Kindle
```

This is intentionally separate from `/discover`. The global navbar search is
for discovery; download search is available from previews and direct `/search`
URLs.

The preview and direct-search pages share `_download_filters.html`,
`static/download-ui.js`, and the edition styles in `static/libflix.css`. Filter
state is sent to `/api/search` but direct-search history keeps only `q`, avoiding
URLs full of implementation parameters.

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

### CN Title Presentation

Chinese catalogs often mix Han titles, pinyin, and translated English titles.
LibFlix uses a deliberately split presentation model:

1. Shelf, category, search, and hero cards keep native Han titles, but replace
   pinyin-only labels with an English edition title from Open Library when one
   exists.
2. Book pages use the English edition title as the primary heading and show a
   verified Han title beneath it.
3. Download searches use the localized Chinese title, and result rows retain the
   exact edition title returned by the download source.

Browse-title lookups use an `IntersectionObserver` and a three-request browser
queue, so only nearby cards are enhanced and initial rendering is never blocked.
Both successful and empty lookups are cached for 30 days.

For the secondary book-page title, LibFlix first checks Chinese Open Library
editions. If they only contain pinyin, it collects their ISBNs and attempts a
bounded Douban mobile-metadata lookup. These server-side calls are limited to
four concurrent requests. Pinyin remains the final fallback when no Han title
can be verified.

### Open Library Helpers

| Function | Responsibility |
|---|---|
| `ol_get(path, params)` | Rate-limited, coalesced Open Library request with circuit breaker and durable stale fallback |
| `ol_get_work(ol_key)` | Work detail lookup |
| `build_book_detail(ol_key, lang)` | Assemble and persist the app-facing title, author, cover, description, subjects, and download aliases |
| `schedule_book_detail_refresh(ol_key, lang)` | Refresh one stale/missing assembled detail in a bounded executor |
| `shelf_query(topic, lang)` | Mode/topic/language Open Library query builder |
| `extract_book(record, lang)` | Normalize Open Library search record to app book card |
| `first_matching_edition(record, lang)` | Prefer an edition matching the active language |
| `edition_cover_id(edition)` | Pick a usable Open Library cover id |
| `resolve_chinese_title(ol_key)` | Resolve and cache a Han title from Chinese-edition ISBN metadata |
| `chinese_download_queries(ol_key, metadata)` | Build ordered download aliases from Chinese edition titles, cleaned suffixes, and Traditional-to-Simplified conversion |
| `resolve_english_title(ol_key)` | Resolve and cache a stable English display title for CN browsing |
| `english_description_for_work(ol_key, work)` | Prefer an English work or edition description and reject incompatible catalog text |
| `similar_subject_candidates(subjects)` | Exclude broad Open Library labels and select specific recommendation subjects |
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

The gateway uses a descriptive application user agent, spaces requests by at
least `OPENLIBRARY_MIN_INTERVAL`, and opens a short process-local circuit after
three consecutive failures. Expired SQLite values are not deleted during
lookup; they remain available as stale fallback while at most two refresh tasks
run in the background.

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
      "cover_url": "/olcover/240726/M",
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
  "subjects": ["Science", "Astronomy"],
  "refreshing": false,
  "cache": "fresh"
}
```

When only a local hint is available, the endpoint still returns a successful
payload with `refreshing: true`; a bounded background task assembles the missing
metadata. Non-Open-Library keys return `Book not found`.

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
| `format` | `epub`, `pdf`, `all` |
| `lang` | `English`, `all` |
| `dedup` | `0`, `1` |
| `page` | integer |

### `GET /download/<md5>`

Resolves the md5 through the active downloader and streams the remote file
through Flask.

### `POST /api/kindle/jobs`

Validates the selected file and Kindle/SMTP settings, writes a queued job row,
and returns immediately:

```json
{
  "success": true,
  "job_id": "7d29...",
  "status": "queued"
}
```

The supplied SMTP credentials remain only in process memory. They are not
stored in the SQLite job or event rows.

The request also carries the canonical Open Library title, author, work key,
description, language, and cover path plus the selected edition's publisher and
year. The worker passes these non-secret fields to `book_preparation.py` only
after the source download completes.

### `GET /api/kindle/jobs/<job_id>`

Returns the current status and events after an optional integer `cursor`:

```json
{
  "success": true,
  "job_id": "7d29...",
  "status": "running",
  "events": [
    {"id": 3, "type": "progress", "stage": "Downloading book", "progress": 42}
  ],
  "cursor": 3
}
```

The browser polls this route and stores the active id in `sessionStorage`, so
the selected row can restore progress after navigation or refresh.

### `POST /api/sendtokindle`

Downloads the selected file, builds an email attachment, and sends it through
the SMTP credentials supplied by the browser. This endpoint remains for
compatibility; the current UI uses background jobs.

Appending `?stream=1` returns newline-delimited JSON (`application/x-ndjson`)
instead of waiting for one final JSON response. Events contain `type`, `stage`,
and `progress`; download events also include transferred-size `detail` when
available. The final event is either `complete` at 100 or `error`. The original
non-streaming JSON behavior remains available for compatibility.

### Cover endpoints

`GET /olcover/<cover_id>/<size>` and
`GET /cover/<md5>/<size>?dir=<directory>` validate identifiers, fetch a bounded
upstream rendition, optionally convert it to a size-specific WebP thumbnail,
and store it under `LIBFLIX_DATA_DIR/covers`. Responses expose
`X-LibFlix-Cover-Cache` and long-lived browser caching.

### Health and browser timing

- `GET /api/health` reports local cache, loaded shelves, Open Library circuit,
  and Kindle-job state without adding a blocking external probe.
- `POST /api/metrics/web-vitals` validates and records small LCP, CLS, and INP
  payloads sent by the browser.

## Template Responsibilities

| Template | Responsibility |
|---|---|
| `_navbar.html` | Shared nav, category tabs, expandable discovery search/settings, Kindle sheet, transition overlay, toast, quick peek |
| `_book_card.html` | Shared card link, cover, placeholder, hover/focus metadata |
| `_download_filters.html` | Shared collapsible download filter form |
| `index.html` | Fixed-height hero, cover-stack carousel, homepage shelves, horizontal shelf infinite scroll |
| `category.html` | Category grid and vertical infinite scroll |
| `discover.html` | Open Library discovery result cards and vertical infinite scroll |
| `book.html` | Preview spotlight, async description, similar shelf, inline edition results |
| `search.html` | Direct download edition search page |
| `results.html` | Older server-rendered download table fallback |

Shared frontend assets:

| Asset | Responsibility |
|---|---|
| `static/libflix.css` | App tokens, responsive layout, navigation, cards, preview, filters, editions, modal, focus and reduced-motion states |
| `static/download-ui.js` | Edition rendering, format-aware actions, pagination, notifications, and friendly error mapping |

Backend attachment preparation:

| Module | Responsibility |
|---|---|
| `book_preparation.py` | Canonical filename cleanup, EPUB package metadata/cover repair, PDF metadata repair, and safe original-file fallback |

## Frontend Interaction Details

### Shared App Chrome

`_navbar.html` owns the wide-screen top bar, expandable search, expandable
settings menu, shared route-transition overlay, and quick-peek book preview
behavior.

The collapsed search and settings controls are icon-only. Search expands on
focus or click, then submits to `/discover`; settings expands to reveal the
fiction/non-fiction and EN/CN choices plus Kindle delivery settings.

The navbar exposes:

```js
window.LibFlixLoading = { show: showTransition, hide: hideTransition };
```

Same-origin links use a persistent app-shell navigation path. The destination
document is fetched, the existing navbar stays mounted, and `<main>` plus the
footer are replaced atomically after the response is ready. Page-specific style
and script tags are synchronized, body/title/language state is updated, and
browser history uses the same path on `popstate`. There is no document-level
fade: the current screen remains stable until replacement. Unsupported responses
or runtime failures fall back to a normal document navigation.

The navigation progress line starts after 320 ms and is contained by the mounted
navbar, so fast cached transitions have no visual loading state and slower
responses do not cover the current content. Full-document language changes keep
the shared LibFlix overlay because the server-selected locale remains
authoritative.

Pointer, focus, and touch intent populate a 16-entry, five-minute HTML cache with
at most four speculative requests at once. Navigation reuses both completed and
in-flight requests. A bounded set of loaded cover URLs lets imported card markup
start visible when the browser already has that image, avoiding a second
opacity/shimmer cycle.

Each page registers a cleanup callback for its observers, event listeners,
timers, and active requests before the app shell replaces it.

The Kindle sheet owns focus while open, locks body scrolling, focuses the first
field, supports password visibility, closes on Escape/backdrop click, and
returns focus to the invoking control. Settings are stored in localStorage and
sent to the backend only for an explicit Send to Kindle request. Active delivery
ids live in `sessionStorage`, while SMTP credentials remain out of the job
database.

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

The quick-peek element is `position: fixed`; pointer coordinates and viewport
clamping therefore remain in the same coordinate system even after scrolling.

### Download Edition UI

`download-ui.js` turns normalized `/api/search` results into one shared edition
component. Each row has stable cover geometry, a two-line title allowance,
author/publisher context, compact format metadata, and explicit Download and
Kindle actions. The API globally ranks the filtered result set before explicitly
flagging one row as `best_match`; the renderer does not infer that the source's
first row is best. The flagged row renders as the persistent primary option;
remaining editions live in a closed native `<details>` disclosure and are
expanded automatically if one contains an active resumed delivery.

MOBI and AZW/AZW3 candidates are removed before deduplication and ranking, so
unsupported Send to Kindle formats cannot appear as download actions or become
the recommended edition. The job API independently rejects those formats to
protect against stale clients or direct requests.

Ranking is dominated by normalized title similarity, including exact,
containment, token-overlap, and sequence checks. Author agreement is the next
strongest signal. Language agreement, EPUB/PDF suitability, plausible file
size, publisher/pages/cover completeness, and a bounded recency tie-breaker
follow. In English mode, Han text in title/author metadata receives a decisive
penalty and Chinese publisher branding receives a smaller source-quality
penalty. Those penalties are not applied in Chinese mode. The publication year
can no longer outweigh a poor title match.
Deduplication uses the same scorer to choose the strongest edition inside each
normalized title/author group before the remaining candidates are globally
sorted when the explicit `Best match` mode is active. Other sort modes retain
the source's requested ordering, but the highest-scoring row remains explicitly
flagged wherever it appears. The flagged row also exposes concise
`recommendation_reasons` so the interface can explain the decision.

The renderer also:

- uses Unicode-safe filenames and format-aware action labels
- starts a background Kindle job without adding another full-page loader
- sends the canonical book identity and selected edition metadata to the
  pre-upload preparation stage
- resumes interrupted source downloads from a validated HTTP byte range and
  rejects incomplete final byte counts
- polls incremental job events into a progress panel inside the selected edition
  row, using real byte progress for the file download and named states for
  attachment, authentication, and SMTP delivery
- restores a running job from `sessionStorage` after page navigation
- switches to an indeterminate bar only when the source omits content length
- preserves an explicit success or retryable error state at the end of delivery
- reports the prepared title and measured elapsed seconds in the completion
  event so the success toast reflects the file that was actually sent
- hides pagination when `total_pages <= 1`
- keeps edition actions inside the viewport on compact screens
- maps timeouts and network errors to user-facing recovery messages
- leaves raw counts out of the visible interface

Before SMTP upload, `book_preparation.py` applies a clean canonical filename.
For EPUB it edits only the OPF package: title is canonicalized, absent metadata
is filled, and a JPEG cover is added only if no cover declaration or cover image
already exists. The archive keeps `mimetype` first and uncompressed. PDF title
and missing author/description fields are updated through `pypdf`. Any parsing
or encryption problem returns the original file, so preparation cannot turn a
usable download into a failed send.

### Rendering Performance

Shelf wrappers and their fixed-size loading rows are server-rendered, while card
markup and images hydrate within a `120px` observer margin. High-priority hero,
download, and local loading surfaces keep bounded animation, while
`content-visibility` skips work for distant homepage shelves. Fixed dimensions
prevent async covers and result content from shifting the surrounding layout.

The navbar fetches documents only after 260 ms of pointer intent or an explicit
keyboard/touch interaction, keeps at most four prefetches active, and stores up
to 16 recent pages for five minutes. There is no idle sweep of category pages.
Book-card intent may prefetch that one clean book URL. Book cards also populate
an in-process hint index, so their detail route can render title, author, and
cover without making an Open Library request. Full descriptions, localized
titles, and similar-book subjects render from the assembled detail cache or
hydrate after the initial shell is visible.
Static CSS/JS assets carry an mtime version and receive immutable one-year cache
headers. Cover URLs point at LibFlix's persistent local cache. Pillow creates
size-specific WebP thumbnails when available, a daily background task warms the
first visible Trending covers, and only the active hero asks for a large
rendition.

### Homepage Shelf Infinite Scroll

Each shelf stores state on the shelf element:

```html
<div class="shelf" data-topic="history" data-page="0" data-total-pages="25"
     data-loading="0" data-deferred="1">
```

The first observer intersection calls page 1 and removes the skeleton. When the
row later approaches the right edge, `loadShelfMore()` requests subsequent pages
and inserts cards before the compact arrow button.

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
| Open Library JSON | memory | `ol:{path}:{params}` | 1 hour fresh |
| Open Library JSON | SQLite `api_cache.sqlite3` | SHA-256 of request key | 6 hours fresh; up to 90 days stale |
| Chinese title resolution | memory + SQLite | `chinese_title:v1:<ol_key>` | 30 days |
| CN English display title | memory + SQLite | `english_title:v1:<ol_key>` | 30 days |
| Assembled book detail | memory + SQLite | `book_detail:v3:<lang>:<work>` | 7 days fresh; up to 90 days stale |
| Similar books | memory + SQLite | `similar:v2:...` | 7 days fresh; up to 30 days stale |
| Download search results | memory + SQLite | `download_search:v6:...` | 15 minutes fresh; stale fallback |
| Homepage shelves | memory | `shelves_{lang}_{mode}` | 1 hour |
| Homepage shelves | disk | `shelf_cache_{lang}_{mode}.json` | immediate restart hydration; stale after 6 hours |
| Cover images | disk + browser | `covers/<source>/<hash>-<size>.*` | persistent disk; 30 days in browser |
| Kindle jobs/events | SQLite | UUID + ordered event ids | active lifecycle; old terminal jobs pruned |
| Versioned static assets | browser HTTP cache | `/static/...?...v=<mtime>` | 1 year immutable |

Runtime cache files are ignored by git.

SQLite runs in WAL mode with `synchronous=NORMAL`. Each cache update touches one
row instead of reading and rewriting the former multi-megabyte JSON object.
Rows older than the longest supported stale window are pruned at initialization.
Legacy `api_cache.json` is migrated once when the database does not yet exist.

All four language/mode shelf files are loaded before Flask begins serving.
Network refresh is not part of startup: a stale requested shelf set schedules a
single delayed background refresh guarded by `(language, mode)`.

The Open Library gateway spaces upstream calls, coalesces matching in-flight
requests, and opens a 60-second circuit after three consecutive failures. A
caller with stale data returns immediately during that circuit rather than
adding another timeout. Background metadata refresh is limited to two workers.

Shelf-cache startup also builds the lightweight book-hint index. Category,
discovery, shelf, and similar-book API extraction extends that index during the
process lifetime. A valid direct book URL with no hint still returns a stable
shell immediately and lets `/api/book` hydrate its identity and description.

CN book hydration also returns an ordered `download_queries` list. The client
tries the next alias only when the current Chinese-filtered search is empty and
retains the successful alias for pagination and retries. This covers edition
suffixes, mixed English/Chinese titles, alternate Open Library edition names,
Traditional/Simplified indexing differences, and a small explicit override map
for known catalog-title mismatches without displaying non-Chinese files.

More Like This uses up to two specific subjects instead of trusting the first
Open Library label. Broad labels such as Fiction, Biography, Fantasy, and
generic demographic tags are ignored. Candidate works found under both selected
subjects rank first, while normalized-title deduplication removes translated or
edition-level duplicates before the 12-card response is returned.

Cover cache paths are validated and hashed before use. A per-path lock prevents
duplicate cold downloads within one process. Responses include a cache outcome
header and `Server-Timing`; the first Trending covers are warmed once per day
without delaying startup.

## Operational Signals

- Every Flask response includes total request time in `Server-Timing`.
- Open Library, download search, and cover endpoints add operation-specific
  timing entries.
- `/api/health` reports local database, shelf, Open Library circuit, and Kindle
  job state without performing a slow external probe.
- The persistent navbar records LCP, CLS, and INP when supported and sends a
  small beacon to `/api/metrics/web-vitals` when the page becomes hidden.
- The code and cache behavior do not require a Cloudflare-specific rule. See
  [Performance and resilience](docs/PERFORMANCE_AND_RESILIENCE.md) for tuning,
  failure behavior, and verification.

## Discovery Source

Open Library remains the only source used to discover, rank, or label browse
cards. Douban is used only as an optional ISBN metadata fallback for the
secondary Chinese title on a book page when a Chinese Open Library edition lacks
Han characters. The language URL helper strips obsolete source parameters from
older links so current routes stay focused on mode, language, category, and
query state.
