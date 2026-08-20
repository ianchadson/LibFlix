# LibFlix Architecture

## Overview

LibFlix is a Flask app with two distinct data paths:

1. **Discovery path:** Open Library powers browsing, shelves, category pages,
   strict identity search, book details, covers, and similar books. Topic
   search combines Open Library candidates with bounded Inventaire enrichment,
   while mapped Inventaire works can also preserve exact search and similar-book
   shelves during an Open Library outage. Every candidate still resolves to an
   Open Library work. An
   attributed Wikipedia index of NYT number-one history can annotate and rerank
   exact existing candidates; it never creates book identity.
2. **Download path:** the `downloaders/` package powers libgen search, download
   resolution, streaming, and Send to Kindle delivery.

Open Library work ids are the canonical identity boundary between these paths.
Inventaire can improve topic recall, provide a strict identity-search fallback,
and supply validated author labels or entity artwork, but only when `P648` maps
directly to that canonical work. It cannot create an unresolved native route or
download identity.
The NYT/Wikipedia signal is a ranking overlay rather than a discovery provider
and is reported under `ranking_sources`, not candidate `sources`.

The product has no user accounts, server-side reader profile, reading-progress
tracking, or personalized recommendations. Optional Saved, Recent, and Kindle
history lists remain in browser local storage and never affect ranking. SMTP
settings also remain local to the browser, and the server stores only bounded
operational and Web Vital aggregates without raw URLs, payloads, or IP
addresses.

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

### Topic Catalog (`GET /topics`)

`/topics` and `/cn/topics` render a zero-provider-fetch catalog of 30 canonical
nonfiction topics. The same catalog drives the weekly quality benchmark, so a
public browse entry cannot drift away from monitored queries. Featured links
also appear on the nonfiction homepage and `Topics` appears in desktop and
mobile browse navigation. Every link targets `/discover` with explicit
`intent=topic&type=nonfiction`; the catalog itself performs no API fanout and
loads no covers.

### Discovery Search (`GET /discover`)

```text
Navbar search form submits to /discover
  -> plan_topic_query(q, intent) selects topic or identity mode
       -> ISBN / OL work id / quoted title / "Title by Author": identity
       -> known broad topic or "books about ..." prefix: topic
       -> explicit About / Title or author selection overrides detection
  -> identity mode uses fetch_discovery_books(q, page, lang)
       -> strict Open Library title/author relevance path
       -> concurrent mapped Inventaire fallback when Open Library is unavailable
  -> topic mode renders a local cached window or an immediate loading shell
       -> /api/discover plans the raw topic plus at most two versioned aliases
       -> Open Library and Inventaire searches run concurrently
       -> Inventaire records require a valid Open Library-work P648 mapping
       -> candidates pass a local relevance gate
       -> a cached NYT #1 history index annotates exact book identities
       -> weighted RRF, bounded quality signals, dedupe, and author cap rank them
       -> page one separates Start here from the Explore window
  -> bottom scroll sentinel fetches stable /api/discover pages automatically
  -> clicking any card opens /book/<Open Library work id>
```

The route never searches the download source directly. Both intents end at an
Open Library work page; download discovery begins only after the user selects a
book identity.

Identity mode uses Open Library's unqualified relevance query, then applies
local language and identity-relevance guards to every record. Exact title/author
evidence ranks first; multi-token queries require at least two-thirds literal
coverage, which removes unrelated popularity filler without hiding sparse exact
works. Coverless matches use the standard placeholder rather than disappearing.

Topic mode uses a deterministic, versioned expansion corpus with more than 30
common topics. Each plan contains at most three normalized search terms and at
most two Inventaire semantic claims, keeping fanout and caching predictable.
Unknown topics stay on the identity path unless the user selects `About` or uses
a topic prefix such as `books about`.

Topic candidates must show subject, title, description, or approved semantic
evidence before they can rank. Weighted reciprocal-rank fusion combines native
provider and expansion ranks. Reading-log, Open Syllabus, rating, edition,
cover, Inventaire popularity, and cross-source-consensus signals are bounded
quality tie-breakers after that gate; they cannot admit unrelated filler. Work
identity dedupes cross-source records and stable keys break remaining ties.
Historical NYT number-one status adds at most a small secondary boost inside
the same relevance tier. Matching requires one unique exact normalized title
and author; the source cannot bypass the relevance gate.
Filters run before the final two-results-per-author diversity cap so discarded
language, type, or publication-period records cannot starve valid matches.

The topic UI shows up to six `Start here` cards, then a duplicate-free
`Explore <topic>` grid. Cards display one strongest factual reason plus a
bounded provider description when one is usable. Missing descriptions remain
empty instead of duplicating the reason chip. Heavily joined or malformed
source summaries are rejected. A collapsed Filters control supports Type,
Language, Published, and Best match / Newest sorting. The card action remains
`Find an edition`; there is no direct-send shortcut before identity
verification.

### Book Preview (`GET /book/<work_id>`)

```text
Browser requests /book/OL3431878W
  -> Flask reads assembled detail cache or card-backed local hint
  -> Flask renders title, author, cover, cached description, and work key
     without waiting for Open Library
  -> JS fetches /api/book?ol_key=...&book_lang=... only when primary content
     is still missing
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
- Content-ready and complete server renders skip duplicate detail hydration.
  A provisional render polls quietly only while primary content is missing.
- Populated title, author, cover, description, download results, and similar
  results are monotonic: later metadata may fill an empty slot but never tears
  down or restarts committed content.
- Provisional book HTML is `no-store`, and partial navigation honors that
  header instead of retaining an incomplete page in its five-minute cache.
- Similar books primarily use Open Library subject and author searches. During
  an outage, directly mapped Inventaire works may supply a partial subject or
  same-author shelf; semantic topic evidence can seed this path when canonical
  subjects are not yet available. Subjects remain internal and are not rendered
  as chips in the preview UI.
- Partial topic recovery compares snapshot identities and patches unchanged
  cards in place. Loaded covers and scroll state survive repeated provider polls;
  a full reconciliation occurs only when the ranked work window changes.
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

### Topic Discovery Planning And Ranking

`topic_discovery.py` is provider-neutral and contains no Flask or network state.
It validates and bounds provider payloads, while `app.py` owns transport,
caching, timeouts, circuits, and API pagination.

| Function | Responsibility |
|---|---|
| `plan_topic_query(query, intent)` | Detect identity/topic intent, honor an explicit override, strip supported topic prefixes, and build at most three deterministic versioned query terms |
| `build_openlibrary_request(...)` | Build bounded Open Library subject requests with topic ranking fields |
| `build_inventaire_request(...)` | Build bounded Inventaire work-text or approved semantic-claim requests |
| `parse_openlibrary_payload(...)` | Normalize Open Library results with canonical work keys, subjects, descriptions, covers, and bounded quality signals |
| `parse_inventaire_payload(...)` | Accept only work records with a valid Wikidata `P648` Open Library work mapping; reject edition and unresolved records |
| `apply_nyt_bestseller_signals(...)` | Annotate existing Open Library candidates with exact NYT #1-history matches without admitting new candidates |
| `merge_topic_candidates(...)` | Merge by work identity, apply the relevance gate and weighted RRF, and add bounded quality/consensus tie-breakers |
| `filter_topic_results(...)` | Apply type, language, publication-period, and Newest sorting, then enforce the final two-books-per-author cap |
| `fetch_topic_discovery_payload(...)` | Coordinate concurrent provider pages, stale-complete fallback, complete-window caching, and app-facing Open Library cards |
| `paginate_topic_discovery_payload(...)` | Split a cached ranked window into page-one `start_here` and stable 30-book Explore pages without duplicates |

Open Library records carry their work identity directly. Inventaire records can
participate only when `wdt:P648` normalizes to `OL...W`; `OL...M` edition ids,
unknown ids, and arbitrary provider URLs never cross the canonical boundary.
Hydrated authors and entity-image hashes are accepted only from mapped works and
are re-exposed through local validated fields. Supplemental-only cards still link to an
Open Library work and must satisfy the selected language-safety checks.

The expansion, ranker, and editorial-index revisions are included in cache
keys. Changing one invalidates old merged windows without changing route
shapes. A merged window carrying the ranking signal inherits the source index's
expiry and cannot outlive that index.

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
| `fetch_discovery_books(q, page, lang)` | Paginated strict Open Library identity-search source |
| `fetch_shelves(mode, lang)` | Homepage shelf builder using parallel candidate prefetch plus top-to-bottom dedupe |

The gateway uses a descriptive application user agent, spaces requests by at
least `OPENLIBRARY_MIN_INTERVAL`, and opens a short process-local circuit after
three consecutive failures. Expired SQLite values are not deleted during
lookup; they remain available as stale fallback without being promoted to a new
memory timestamp. Refresh queues are hard-capped so work cannot accumulate
without bound and recovery can retry after a circuit cooldown.

### Inventaire Gateway

`inventaire_get(path, params)` is separate from `ol_get`. It has its own request
spacing, connect/read timeouts, in-flight request coalescing, refresh executor,
three-failure circuit, and 60-second cooldown. Raw Inventaire responses are
cached for six hours and can remain eligible as stale fallback for seven days.
A failure in this gateway does not increment or open the Open Library circuit.
Both gateways stream and reject decoded JSON above the configured byte cap,
validate endpoint schemas before caching, and purge malformed cached entries.

Topic discovery submits bounded work to both gateways concurrently. The merged
request has an overall timeout (`TOPIC_PROVIDER_WAIT_TIMEOUT`, 10 seconds by
default) and a short grace period after a useful provider result arrives, so a
slow supplement cannot indefinitely hold the page. Provider availability is
reported independently and a usable response may be explicitly `partial`.

### Attributed NYT Number-One Overlay

`nyt_bestsellers.py` parses the public Wikipedia pages for the current and
previous calendar year's NYT number-one books into a bounded exact-match index.
A single background executor refreshes it outside the user-facing topic
provider deadline, while a filesystem lock prevents multiple Gunicorn workers
from cold-fetching it together. Requests reject redirects, non-HTML content,
oversized bodies, and malformed table schemas.

The index refreshes after 12 hours and remains usable as stale fallback for up
to seven days. Timeouts or page changes leave Open Library and Inventaire
ranking unchanged and never make a response `partial`. A result using the
signal is labelled `NYT #1 bestseller` and links its on-screen attribution to
the source Wikipedia page. This is historical number-one evidence, not a
complete or necessarily current NYT bestseller list.

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
`new_this_week` and `new_this_week_fiction` use newest records from the current
and previous publication year as a weekly refreshed editorial surface.
`short_reads` and `short_reads_fiction` require a median page count from 1 to
220. All shelves remain language-scoped and are deduplicated in display order.

### `GET /api/suggestions`

Returns up to eight title/author/ISBN suggestions from LibFlix's bounded local
catalog corpus. It performs no upstream request and is safe to call while the
user types.

### `GET /api/covers`

Recovers covers for at most 24 visible Open Library work keys in one request.
The endpoint checks card hints, stale or fresh assembled details,
alternate-language canonical details, and the local catalog corpus. It returns
only local proxy URLs. Missing details may schedule a bounded background refresh
but never make this request wait for Open Library.

### `GET /discover`

Renders auto-detected topic discovery or strict Open Library identity results.
On a cold topic request, HTML returns immediately with a loading shell; the
browser hydrates the ranked window from `/api/discover`.

Params:

| Param | Values | Purpose |
|---|---|---|
| `q` | string | Title, author, identifier, or broad-topic query |
| `intent` | `topic`, `identity` | Optional explicit override; omitted uses automatic detection |
| `page` | integer | 1-based stable merged-window page |
| `snapshot` | string | Page-2+ topic snapshot id supplied by the browser to prevent mixed rankings |
| `mode` | `fiction`, `nonfiction` | Maintains navbar mode |
| `book_lang` | `en`, `cn` | Language filter |
| `type` | `any`, `nonfiction`, `fiction` | Topic-mode book-type filter |
| `language` | `current`, `any`, `en`, `cn` | Topic-mode language filter |
| `published` | `any`, `recent`, `classic` | Topic-mode publication-period filter |
| `sort` | `best`, `newest` | Topic-mode ranking order |

### `GET /api/discover`

JSON endpoint backing discover hydration and pagination. Identity responses
retain the existing book-card contract and add intent metadata:

```json
{
  "success": true,
  "query": "Deep Work",
  "intent": "identity",
  "topic_mode": false,
  "books": [],
  "page": 1,
  "total_pages": 1,
  "total": 0
}
```

Topic responses add fields without removing or renaming the existing pagination
fields:

```json
{
  "success": true,
  "query": "focus",
  "intent": "topic",
  "topic_mode": true,
  "display_query": "focus",
  "start_here": [
    {
      "title": "Deep Work",
      "author": "Cal Newport",
      "ol_key": "/works/OL17713267W",
      "description": "A bounded provider description...",
      "reasons": ["Subject: Attention", "Matched by multiple sources"],
      "sources": ["inventaire", "openlibrary"]
    }
  ],
  "books": [],
  "page": 1,
  "total_pages": 1,
  "total": 0,
  "partial": false,
  "sources": ["inventaire", "openlibrary"],
  "source_unavailable": false,
  "retry_after": 0,
  "snapshot_id": "8d8aef784c6dce81a2d9",
  "filters": {
    "type": "any",
    "language": "current",
    "published": "any",
    "sort": "best"
  },
  "expansion_version": "topic-v1",
  "ranker_version": "rrf-v1"
}
```

`start_here` is populated only on page one and is excluded from `books` and
`total`; `total` counts the locally merged Explore window, not the sum of
provider totals. All pages are slices of the same cached ranked window, so a
work cannot move between or repeat across pages during that window's lifetime.
Page 2+ never triggers provider fanout: it requires a cached window and verifies
the browser's `snapshot` id. A missing window or changed id returns a no-store
409 so the browser reconciles page one instead of mixing rankings.
Cards may add `reasons`, `sources`, `subjects`, `published_year`, `languages`,
and ranking metadata; consumers must ignore unknown additive fields.

When one provider is late or unavailable, a usable response can return
`partial: true` plus the sources that contributed. Partial windows are not
persisted as complete results and are always `Cache-Control: no-store`. If a
complete cached window is stale while a refresh is partial, the complete window
is returned with optional `stale` and `refresh_partial` flags. `retry_after`
lets the browser wait past an open provider circuit instead of exhausting
retries during cooldown. If no canonical result or complete fallback is usable,
the endpoint returns HTTP 503 with `code: "source_unavailable"`, `partial: true`,
and `Cache-Control: no-store`.

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

The first response may contain an immediate `partial` set ranked from the local
catalog corpus. The browser keeps those cards visible while bounded remote
subject and same-author sources refine the shelf. Generic taxonomy tails such
as `General` are excluded before source selection, and author matches require
the displayed primary author rather than incidental contributor metadata.

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
| `scope` | `best`, `all`; `best` performs the narrow first pass used by book pages |

Book pages request `scope=best` first and render the recommended edition. A
later idle `scope=all` request expands aliases and alternatives under the
collapsed `Other options` disclosure without replacing an already-usable best
match on failure.

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

`GET /olcover/<cover_id>/<size>`, `GET /iacover/<archive_id>/<size>`,
`GET /invcover/<entity_image_hash>/<size>`, and
`GET /cover/<md5>/<size>?dir=<directory>` validate identifiers, fetch a bounded
upstream rendition, optionally convert it to a size-specific WebP thumbnail,
and store it under `LIBFLIX_DATA_DIR/covers`. Responses expose
`X-LibFlix-Cover-Cache`, `X-LibFlix-Cover-Source`, and long-lived browser
caching. Open Library cover requests can carry a validated Internet Archive
fallback; no arbitrary remote URL is accepted from the client.

### Health and browser timing

- `GET /api/health` reports local cache, loaded shelves, independent Open
  Library and Inventaire circuit state, and Kindle-job state without adding a
  blocking external probe.
- `POST /api/metrics/web-vitals` validates and records small LCP, CLS, and INP
  payloads sent by the browser.

## Template Responsibilities

| Template | Responsibility |
|---|---|
| `_navbar.html` | Shared nav, fixed instant-search palette, settings, browser-local library, Kindle sheet, route progress, toast, quick peek, and cover recovery |
| `_book_card.html` | Shared card link, cover, placeholder, hover/focus metadata |
| `_download_filters.html` | Shared collapsible download filter form |
| `index.html` | Fixed-height hero, cover-stack carousel, featured topic rail, homepage shelves, horizontal shelf infinite scroll |
| `topics.html` | Zero-fetch featured and grouped topic catalog with an explicit topic-search form |
| `category.html` | Category grid and vertical infinite scroll |
| `discover.html` | Automatic intent handling, topic Start here / Explore layout, reason chips, compact topic filters, identity cards, partial-source status, and vertical infinite scroll |
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

`_navbar.html` owns the wide-screen top bar, fixed search palette, expandable
settings menu, browser-local library, shared route progress, quick-peek book
preview behavior, and visible-card cover recovery.

The search and settings controls are icon-only. Search opens a viewport-fixed
palette without changing navbar geometry, queries `/api/suggestions` after a
short debounce, supports keyboard navigation, and submits to `/discover`.
Settings reveals fiction/non-fiction and EN/CN choices, `My Library`, and Kindle
delivery settings. On mobile, the bottom navigation controller is the sole
owner of Search, Browse, and Settings sheet state.

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
an in-process hint index, so their detail route can render title, author, cover,
and an available description without making an Open Library request. If those
primary fields are present, the browser skips duplicate detail polling. Missing
descriptions, localized titles, and similar-book subjects can still hydrate
after the initial shell is visible without replacing populated DOM.
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

### Browser-local continuity

`My Library` intentionally has no server API. Saved books, recent opens, and
completed Kindle sends use three versioned `localStorage` keys. Only bounded
presentation metadata is stored, each item can be removed locally, and none of
the lists feed search, shelves, or More Like This. This preserves utility
without creating accounts or a personalized recommendation profile.

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
| Inventaire JSON | memory + SQLite | `inventaire:{path}:{params}` | 6 hours fresh; up to 7 days stale |
| Complete topic window | memory + SQLite | `topic-discover:<expansion>:<ranker>:<cards>:<lang>:<query>:<filters>` | 30 minutes fresh; up to 24 hours stale |
| Chinese title resolution | memory + SQLite | `chinese_title:v1:<ol_key>` | 30 days |
| CN English display title | memory + SQLite | `english_title:v1:<ol_key>` | 30 days |
| Assembled book detail | memory + SQLite | `book_detail:v6:<lang>:<work>` | 7 days fresh; up to 90 days stale |
| Similar books | memory + SQLite | `similar:v8:...` | 7 days fresh; complete empty results use a 30-minute negative key |
| Download search results | memory + SQLite | `download_search:v11:...` | 15 minutes for complete searches; stale fallback; scope is part of the key |
| Homepage shelves | memory | `shelves_{lang}_{mode}` | 1 hour |
| Homepage shelves | disk | `shelf_cache_{lang}_{mode}.json` | immediate restart hydration; stale after 6 hours |
| Cover images | disk + browser | `covers/<source>/<hash>-<size>.*` | persistent disk; 30 days in browser |
| Kindle jobs/events | SQLite | UUID + ordered event ids | active lifecycle; old terminal jobs pruned |
| Versioned static assets | browser HTTP cache | `/static/...?...v=<mtime>` | 1 year immutable |
| Saved/recent/Kindle lists | browser localStorage | `libflix_saved_books_v1`, `libflix_recent_books_v1`, `libflix_kindle_history_v1` | device-local until removed |

Runtime cache files are ignored by git.

SQLite runs in WAL mode with `synchronous=NORMAL`. Each cache update touches one
row instead of reading and rewriting the former multi-megabyte JSON object.
Rows older than the longest supported stale window are pruned at initialization
and periodically on writes. Row count, aggregate payload bytes, individual
payload size, and process-local memory entry count are independently capped.
Legacy `api_cache.json` is migrated once when the database does not yet exist.

All four language/mode shelf files are loaded before Flask begins serving.
Network refresh is not part of startup: a stale requested shelf set schedules a
single delayed background refresh guarded by `(language, mode)`.

Topic cache keys include normalized query, active language, all four bounded
filters, expansion version, and ranker version. The cache stores the complete
ranked window before pagination, then page one takes up to six `Start here`
items and all pages slice the remaining Explore list in 30-book chunks. This
makes pagination stable; a content-derived snapshot id rejects cross-window
page mixing, prevents cross-page duplicates, and avoids adding incompatible
provider-reported totals.

Only complete topic windows are persisted. A partial live response remains
request-local so provider recovery can replace it immediately, and an empty
outage is never cached as an authoritative empty search. A stale complete window
remains preferable to a fresh partial refresh for up to 24 hours; response flags
tell the browser when it is showing that fallback. A valid complete empty result
can be cached because both source completion and canonical availability are
known.

The Open Library gateway spaces upstream calls, coalesces matching in-flight
requests, and opens a 60-second circuit after three consecutive failures. A
caller with stale data returns immediately during that circuit rather than
adding another timeout. Background metadata refresh uses two workers and
hard-capped pending sets for Open Library, Inventaire, book details, and
recommendations.

Inventaire uses an independent request clock, coalescing table, refresh pool,
timeouts, stale window, failure count, and 60-second circuit. The topic
coordinator can return useful Open Library results while Inventaire is degraded,
or a stale complete merged window while either source refresh is incomplete.

Shelf-cache startup also builds the lightweight book-hint index. Category,
discovery, shelf, and similar-book API extraction extends that index during the
process lifetime. A valid direct book URL with no hint still returns a stable
shell immediately and lets `/api/book` hydrate its identity and description.

Book hydration returns an ordered `download_queries` list, but the server owns
the canonical identity used for lookup. It searches non-overlapping two-query
batches, up to six source calls, and continues past weak PDF-only results until
a high-confidence EPUB is found or the bound is exhausted. This covers edition
suffixes, mixed English/Chinese titles, alternate Open Library edition names,
Traditional/Simplified indexing differences, ISBNs, and a small explicit
override map without displaying files outside the active language filter.

More Like This uses up to two specific subjects plus one same-author query
instead of trusting the first Open Library label. Broad labels such as Fiction,
Biography, Fantasy, and generic demographic tags are ignored. Candidate works
found under both selected subjects or by the same author rank first, while
normalized-title deduplication removes translated or edition-level duplicates.
The whole recommendation request remains capped at three origin searches.

Cover cache paths are validated and hashed before use. A per-path lock prevents
duplicate cold downloads within one process. Responses include a cache outcome
header and `Server-Timing`; the first Trending covers are warmed once per day
without delaying startup.

## Operational Signals

- Every Flask response includes total request time in `Server-Timing`.
- Open Library, download search, and cover endpoints add operation-specific
  timing entries.
- `/api/health` reports local database, shelf, independent Open Library and
  Inventaire circuit state, Kindle jobs, and writable limiter/metrics storage
  without a slow external probe.
- The persistent navbar records LCP, CLS, and INP when supported and sends a
  small beacon to `/api/metrics/web-vitals` when the page becomes hidden.
- `security_runtime.py` stores bounded hourly aggregates in `metrics.sqlite3`
  and hashed token-bucket identities in the separate `rate_limits.sqlite3`;
  raw request URLs, IP addresses, payloads, and error messages are not retained.
- Discovery, recommendations, book details, download search/delivery, Kindle,
  and Web Vital endpoints have weighted cross-worker limits that fail open if
  their SQLite store is unavailable and return `Retry-After` when capacity is
  exhausted. Per-client capacity is checked before global capacity.
- The mobile PWA service worker caches only its allowlisted, current-version
  static shell when every response is explicitly public. Dynamic, sensitive,
  private, and no-store routes are network-only.
- Proxy identity trust is disabled in the portable app. Current production
  enables it only behind Caddy's Cloudflare source allowlist and localhost-only
  `X-LibFlix-Client-IP` rewrite. See [Performance and resilience](docs/PERFORMANCE_AND_RESILIENCE.md)
  for tuning, failure behavior, and verification.

## Discovery Source Boundaries

Open Library remains canonical for homepage/category browsing, work identity,
detail hydration, descriptions, similar books, edition aliases, book routes,
and the handoff into download matching. Strict title/author identity search also
runs a concurrent Inventaire fallback, but admits only directly mapped Open
Library works that pass the same local language and relevance guards.

Broad-topic discovery can also use Inventaire work search and a small approved
set of semantic Wikidata subject claims. Inventaire contributes a candidate only
when its `P648` claim resolves directly to an Open Library work. The local
relevance gate and ranker may use provider rank, semantic agreement, and bounded
popularity. Validated entity-image hashes and hydrated author labels may fill
presentation gaps through LibFlix's local proxy; the work identity remains Open
Library canonical.

Douban is used only as an optional ISBN metadata fallback for the secondary
Chinese title on a book page when a Chinese Open Library edition lacks Han
characters. It is not a topic ranking source. The language URL helper strips
obsolete source parameters from older links so current routes stay focused on
mode, language, category, query, intent, and bounded filter state.

No source creates a user profile. LibFlix has no accounts, server-side library,
reading-progress tracking, or personalized recommendation model. Browser-local
Saved, Recent, and Kindle lists are display-only continuity tools; topic
expansion and ranking remain deterministic for the same query, language,
filters, provider payloads, and versioned ranker.
