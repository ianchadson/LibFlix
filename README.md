# LibFlix - Book Discovery and Delivery

LibFlix is a Netflix-style web app for browsing books, previewing metadata, and
finding download options. Open Library remains the canonical identity for every
rendered work and the primary source for browsing, details, covers, and similar
books. Broad-topic, exact-search, cover, and recommendation recovery can also
use Inventaire through a tightly gated Open Library-work mapping. An attributed
public-web index of NYT number-one history can add a bounded ranking tie-breaker
to exact book matches. Downloads are handled separately through the modular
downloader layer, currently backed by libgen.li.

The app supports fiction and non-fiction browsing, English and Chinese discovery
filters, intent-aware topic and identity search, book previews, similar-book
shelves, inline download search, direct downloads, and Send to Kindle through
LibFlix's managed sender in production, with user SMTP retained as a local
development fallback.

## About

LibFlix is a local-first book discovery interface for browsing public Open
Library metadata with a polished streaming-app style UI. It focuses on fast
category browsing, clean book previews, contextual download lookup, and a
low-friction path from discovery to Send to Kindle.

LibFlix is intentionally a find-and-send utility, not a reading platform. It
has no user accounts, server-side profile, reading-progress tracking, or
personalized recommendation model. An optional `My Library` keeps saved books,
recently opened books, and completed Kindle deliveries only in that browser's
local storage; those lists never influence discovery or recommendations. The
server retains only bounded operational and Web Vital aggregates without raw
URLs, payloads, or IP addresses.

## Quick Start

```bash
pip install -r requirements.txt
python3 app.py

# App URL:
# http://127.0.0.1:5800
```

No API key is required. Open Library provides the canonical catalog, Inventaire
supplements broad-topic searches with public semantic metadata, and Wikipedia's
current and previous year pages provide the optional NYT number-one signal.

## Screenshots

### Homepage

![LibFlix homepage with fixed hero and trending shelf](screenshots/readme-home.png)

### Search and Settings

![Expanded search and browse settings controls](screenshots/readme-controls.png)

### Quick Peek

![Hover quick peek over a book card](screenshots/readme-quick-peek.png)

## Current Feature Set

### Discovery

- **Intent-aware search** - `/discover` automatically treats known broad topics
  such as `focus`, `meditation`, or `startups` as topic searches. ISBNs, Open
  Library ids, quoted titles, and `Title by Author` queries stay on the strict
  identity path. The app keeps this distinction automatic so the discovery
  page does not need a persistent intent switch.
- **Browseable topic catalog** - `/topics` groups 30 quality-monitored starting
  points and the nonfiction homepage exposes a compact featured-topic rail.
  Topic links open the same ranked discovery pipeline explicitly, without
  fetching providers or covers merely to render the catalog.
- **Deterministic topic expansion** - a versioned corpus covers more than 30
  common topics and expands each request to at most three bounded search terms.
  Prefixes such as `books about` also opt an otherwise unknown query into topic
  mode. No per-request language model or user profile is involved.
- **Safe multi-source candidates** - Open Library subject results are combined
  with Inventaire text or semantic results. Inventaire records are admitted
  only when their Wikidata `P648` claim maps directly to an Open Library work;
  edition ids and unresolved native records are rejected. The mapped work key
  remains canonical, while validated Inventaire author labels and entity-image
  hashes can fill missing presentation metadata through LibFlix's own proxy.
- **Attributed NYT number-one signal** - a bounded background web request reads
  Wikipedia's current and previous calendar-year NYT number-one tables. A unique
  exact title-and-author match can reorder an already-relevant book within its
  relevance tier, but cannot create a card or make an unrelated book pass the
  topic gate. This is a historical #1 signal rather than a complete current
  bestseller list; attribution appears only when a ranked result uses it.
- **Relevance-first ranking** - subject, title, description, and semantic
  evidence must pass a local relevance gate before popularity can matter.
  Weighted reciprocal-rank fusion combines source/query ranks, then bounded
  reading, syllabus, rating, edition, source-consensus, cover, and optional
  historical number-one signals break close calls. Duplicate works merge and one
  author is capped at two results.
- **Topic browsing surface** - topic results lead with up to six `Start here`
  books, followed by a deduplicated `Explore <topic>` grid. Cards can explain
  concise signals such as subject match, multiple-source agreement, widely
  read, frequently assigned, or highly rated, and lead to `Find an edition`.
- **Compact topic filters** - one collapsed panel supports book type, language,
  publication period, and Best match / Newest sorting without adding accounts
  or saved searches.
- **Stable resilient pagination** - a complete ranked window is cached before
  it is divided into stable 30-book Explore pages. `Start here` is returned only
  on page one and is excluded from Explore. A short snapshot id prevents pages
  from different rankings being mixed. A stale complete window wins over a
  newly partial provider response, while partial outages are no-store and never
  persisted as authoritative empty results.
- **Canonical Open Library catalog** - browsing, shelves, categories, metadata,
  covers, similar books, identity search, and every rendered book work key use
  Open Library identity with no API key. Mapped Inventaire results can keep
  exact title/author search and recommendations usable while Open Library is
  temporarily unavailable.
- **Sparse-record recovery** - discovery validates language locally so newly
  catalogued Open Library works remain searchable before language or cover
  metadata has been assigned, without mixing explicitly foreign-language
  records into EN or CN results.
- **Reviewed identity recovery** - a small strict set of previously reported
  canonical title/author identities remains searchable during a complete
  catalog outage. It restores the real book route without inventing cover art
  or download availability.
- **Consistent CN title presentation** - CN shelves and hero items use stable
  English edition titles when Open Library provides them. Book pages pair that
  title with a verified Chinese title, while download rows preserve the exact
  source-edition title used to find the file.
- **Resilient CN download matching** - empty Chinese searches automatically
  retry cleaned edition names, every Chinese Open Library edition alias,
  Simplified Chinese forms, and finally the English title. The file-language
  filter remains Chinese throughout the fallback chain.
- **Expandable browse settings** - Fiction / Non-Fiction and EN / CN controls
  live in the top-right Settings menu instead of always occupying the toolbar.
  Each mode has its own shelf set and category tabs, while EN/CN maps to English
  (`eng`) and Chinese (`chi`) Open Library records.
- **Instant search palette** - the global search opens as a fixed command
  palette, so the navbar and page never reflow. It searches a bounded local
  catalog index while the user types, supports keyboard selection and recent
  terms, and provides direct shortcuts to Trending, New This Week, Short Reads,
  and Topics. Submitting still routes to `/discover`; it never jumps directly
  to download search.
- **Download search is contextual** - download options are searched from the book
  preview page using the selected title and author.
- **Locally reranked identity discovery** - identity-search results must retain
  literal Open Library title/author evidence before rendering. Exact identity
  matches rank first and unrelated popularity filler is removed locally. Exact
  ISBNs, common AI acronym forms, and high-confidence title typos remain
  searchable.

### Homepage

- **Cycleable hero** - the homepage builds a small featured set from active
  mode/language trending books, shows larger cover art, and lets users cycle
  through the featured titles from arrows, dots, or the cover stack itself.
- **Stable animated hero** - the hero keeps a fixed height while the text,
  backdrop, and cover stack transition between books. Title fitting avoids
  ugly character wrapping, keeps short titles on one line where possible, and
  scales long titles only enough to stay readable.
- **Immersive cover backdrop** - the hero uses low-cost cover blur, light sweep,
  grid, static, and glint effects, with reduced-motion fallbacks.
- **App-like top navigation** - category tabs stay at the top on wide screens,
  while search and browse settings expand from compact controls with lightweight
  animation. The search and settings affordances are icon-only in the collapsed
  state.
- **Seamless route changes** - internal navigation keeps the current page and
  navbar visible until the destination is ready, then swaps content atomically.
  A slim navbar progress cue appears only when a response is genuinely slow.
- **Clean browsing URLs** - main home, mode, language, category, and discovery
  routes use paths like `/fiction`, `/cn/category/history`, and
  `/fiction/discover?q=dune` instead of exposing mode/language query args.
- **Trending naming** - the first shelf and first top-nav category are labeled
  `Trending` across fiction and non-fiction. Cached shelf labels are normalized
  at render time so older `New & Popular` cache files do not leak into the UI.
- **Editorial discovery rails** - both fiction and non-fiction place `New This
  Week` and `Short Reads` directly after Trending. The former is a weekly
  refreshed surface of recently published catalog records; the latter is
  bounded to works with a median length of 220 pages or fewer. Neither rail is
  personalized.
- **Top 10 treatment** - the first ten Trending books receive large rank
  numerals without changing the underlying canonical work order.
- **Progressively hydrated shelves** - the homepage sends stable shelf skeletons
  in the initial document, then appends 12-card batches as each shelf approaches
  the viewport or row end. Distant shelves add no initial card or image cost.
- **Shelf-order dedupe** - books shown in an earlier homepage shelf are removed
  from all later shelves. Later shelves are refilled from deeper Open Library
  pages where possible so rows stay useful without repeating entries.
- **Horizontal infinite scroll** - homepage shelves automatically load another
  page when the user scrolls near the end of a row.
- **Intent-aware navigation cache** - internal pages are fetched after deliberate
  pointer hover, keyboard focus, or touch intent and retained briefly in a
  bounded in-memory cache. Revisiting a recent category avoids another request.
- **Instant book shells** - every rendered card registers its title, author,
  cover, available description, and work key as a lightweight server hint.
  Opening a content-ready card renders the book page immediately without a
  duplicate detail request; genuinely missing fields hydrate independently.
- **Compact More affordance** - a small round arrow button remains as a fallback
  at the end of each shelf instead of a full-height tile.
- **Hidden horizontal scrollbars** - homepage shelf rows hide scrollbars while
  preserving horizontal scrolling.
- **Hover quick peek** - book cards keep title/author overlays hidden until
  hover/focus, then fetch Open Library details for a cursor-anchored quick peek
  panel. The panel prioritizes title, author, and a longer description, and
  stays within the viewport near the cursor.

### Category Pages

- **Vertical infinite scroll** - category pages render the first batch
  server-side, then automatically append more books as the user nears the bottom
  of the grid.
- **No manual Load More button** - category and discovery pagination are
  automatic via scroll sentinels and scroll fallbacks.
- **No visible total counts** - labels such as `80 books`, `x shown`, and
  result totals were removed because they do not help the browsing experience.

### Book Preview

- **Focused book spotlight** - cover, title, author, and description use a
  responsive reading layout with a restrained cover-derived backdrop.
- **Stable async details** - cached descriptions render with the preview shell;
  a missing description can fill once after render. Provider markup and broken
  boundaries are cleaned, malformed summaries fall through to edition
  metadata, and populated content is never torn down or rewritten.
- **Monotonic secondary content** - downloads and More Like This may transition
  from one loading state to results, but later metadata cannot restart or hide
  an already-visible result set.
- **More Like This** - up to two specific subjects plus the current author feed
  a bounded, locally ranked single-row shelf. Multi-subject and same-author
  books win over tangential popularity matches, while broad taxonomy labels are
  discarded before they can seed unrelated results. A local corpus supplies an
  immediate partial shelf; bounded Open Library and directly mapped Inventaire
  work then refine it in place without blanking visible cards. Its API request
  waits until the section approaches the viewport.
- **Hidden More Like This scrollbar** - the shelf scrolls horizontally without a
  visible scrollbar.
- **Inline edition picker** - download candidates appear as responsive edition
  rows with cover, title, author, publisher, format, year, size, pages, and
  language instead of a dense table. The recommended edition stays visible;
  alternative editions remain collapsed under `Other options` until requested.
- **Best-first download hydration** - a narrow first pass finds and renders the
  recommended edition as soon as possible. Full alias expansion and alternative
  editions continue quietly during idle time, preserving the visible best match
  if a later source request fails.
- **Collapsible download filters** - format, sort, language, page size, and
  dedupe controls share one compact `Filters` panel across preview and direct
  download search pages. `Best match` is the explicit default; selecting year,
  recently added, file size, title, or author preserves that requested order.
- **Clear actions** - every available edition has explicit format-aware Download
  and Kindle actions. `Best match` is assigned only after globally ranking all
  filtered candidates by title and author similarity, language, reading format,
  file sanity, and metadata quality.
- **Kindle-compatible results only** - MOBI and AZW/AZW3 editions are excluded
  from download results because Send to Kindle does not accept them. EPUB and
  PDF remain available.
- **Send to Kindle settings** - the global Settings menu opens a keyboard-safe
  Kindle sheet with password visibility, local browser storage, a forget
  action, and a visible configured / configure-connection state.
- **Live Kindle delivery progress** - the selected edition expands to show a
  responsive progress bar, current delivery stage, transferred file size, and
  clear completion or failure state while LibFlix downloads and emails the file.
  The success notification includes the cleaned attachment title and
  server-measured delivery time.
  Delivery runs as a background job, survives page navigation, and restores its
  latest status when the user returns. Interrupted source transfers resume from
  their verified byte offset instead of restarting or silently accepting a
  partial file. Verified source files are reused for 24 hours, SMTP setup runs in
  parallel with preparation, and upload progress reports real bytes, rate, and
  ETA from the streamed SMTP transfer. SMTP credentials stay in process memory
  and are never written to the job database.
- **Kindle-ready book preparation** - immediately before upload, LibFlix applies
  the clean Open Library title to the attachment. EPUB files also receive
  missing author, language, publisher, date, description, work identifier, and
  cover metadata without re-encoding their chapters. A cover already displayed
  by LibFlix is reused from the server cache instead of downloaded again. PDFs
  receive a clean title plus missing author/description metadata. Unsupported
  MOBI/AZW files are excluded before rendering or delivery.
  Unsupported, malformed, or encrypted files fall back to the unmodified
  original rather than blocking delivery.
- **Kindle-aware recommendations** - the best candidate is selected by title,
  author, language, format, file sanity, and metadata quality. The chosen row
  explains its strongest signals, while English mode rejects Chinese
  title/author metadata and heavily demotes Chinese publisher branding. Among
  equally accurate matches, the smallest plausible EPUB is selected and marked
  `Fastest to Kindle`.

### Download Search

- **AJAX results** - download results update without a full page reload.
- **Responsive edition display** - direct search uses the same scannable edition
  rows and actions as the book preview, including two-line titles on mobile.
- **Filters** - sort, format, page size, language, and dedupe controls update the
  download search without exposing filter state in the URL.
- **Deduplication** - results can be grouped by normalized title and author,
  keeping the highest-scored candidate.
- **Identity-aware aliases** - a work retains bounded canonical, edition,
  author, and ISBN signals on the server. Lookup searches two aliases at a time,
  up to six total, and continues past weak PDF-only batches until it finds a
  high-confidence EPUB or exhausts that bound.
- **Completeness-aware caching** - complete alias searches retain the normal
  15-minute cache. Partial source failures can still show usable results but are
  not persisted as a complete answer.
- **No visible result totals** - count summaries were removed from search and
  preview download lists.
- **Compact pagination** - pagination appears only when there is more than one
  result page.
- **Resilient states** - timeouts and unreachable download sources produce
  short recovery messages instead of raw backend exceptions.

### Shared Interface

- **Consistent dark UI system** - navigation, details, filters, edition rows,
  settings, focus states, empty states, and notifications share one restrained
  visual language in `static/libflix.css`.
- **Non-blocking route feedback** - internal route changes never cover or fade
  the whole interface. A delayed navbar progress line handles slow responses;
  the full LibFlix overlay remains available only for true document-level
  operations. Local AJAX loaders stay scoped to the content they update.
- **Persistent app shell** - same-origin page changes fetch and replace the
  content area while preserving the top navigation. History navigation,
  page-specific styles/scripts, focus, and scroll restoration continue to work,
  with a normal browser navigation as the automatic fallback.
- **Progressive cover loading** - cover geometry stays stable while images load,
  with a subtle shimmer and no layout shift. Covers already loaded during the
  session retain their visible state across app-shell page swaps.
- **Visible-cover repair** - cards that reach the viewport without a cover are
  checked in one bounded local batch against cached hints, assembled details,
  alternate-language metadata, and the local catalog corpus. Missing upstream
  detail refreshes are scheduled in the background rather than blocking paint.
- **Focused book transitions** - opening a visible cover uses a short native
  View Transition into the book spotlight when supported. The rest of the page
  remains stable and reduced-motion users receive an immediate swap.
- **Persistent optimized covers** - Open Library, Inventaire, Internet Archive,
  and download-result covers pass through a validated local cache. Open Library
  cover URLs can carry a validated Archive fallback, and download rows reuse the
  canonical book cover when an edition has none. When Pillow is available, LibFlix stores
  compact size-specific WebP thumbnails; repeat requests are served from disk
  and canonical `.webp` URLs can be cached by the CDN. Likely hero covers and
  the full Trending shelf are warmed in the background once per day.
- **On-demand hero metadata** - only the active hero description is requested;
  later descriptions hydrate when their book becomes active.
- **Local-first book pages** - cached card hints and assembled book details
  render immediately. Open Library refreshes happen in the background and stale
  data remains usable during an upstream outage.
- **Resilient discovery** - Open Library and Inventaire use independent
  rate-limited, coalesced gateways with bounded timeouts, circuit breakers, and
  durable stale fallback. Topic search has a bounded overall provider wait and
  can identify a partial response without letting one source failure poison a
  complete cached result. Literal title/author search can accept a strict,
  directly mapped Inventaire work or a reviewed canonical identity while Open
  Library is down. An unavailable upstream no longer turns a cached category,
  topic, or book into a blank page. The Wikipedia editorial refresh runs
  outside the provider deadline and its absence or failure never makes Topics
  partial.
- **Stable partial recovery** - repeated topic recovery polls patch unchanged
  cards in place, preserving decoded covers and scroll position instead of
  rebuilding the shelf. Semantic topic evidence is retained as an internal
  recommendation seed when canonical subjects are not yet cached.
- **Operational visibility** - `/api/health` reports local dependency state,
  server timing is attached to key responses, and lightweight browser
  performance metrics are accepted by `/api/metrics/web-vitals`.
- **Durable bounded telemetry** - request/error timings and browser Web Vitals
  are stored as hourly aggregates in SQLite without raw URLs, payloads, IPs, or
  exception messages.
- **Public-service guardrails** - discovery, recommendation, book-detail,
  download search, file delivery, Kindle, and metric endpoints use weighted
  cross-worker token buckets. Metadata refresh queues, in-memory entries, and
  the durable metadata cache are hard-capped. Responses include a restrictive
  CSP (with only Cloudflare's injected analytics host added), frame/plugin
  blocking, a restrictive permissions policy, and HTTPS HSTS.
- **Installable mobile app** - LibFlix includes standalone icons/manifest, an
  install action, a safe offline screen, and Home/Search/Browse/Settings bottom
  navigation. The service worker never stores downloads, Kindle traffic,
  credentials, covers, or private/no-store responses.
- **Browser-local My Library** - Saved, Recent, and Kindle tabs provide useful
  continuity without an account. Entries remain on the current device, can be
  removed at any time, and are not sent to the server or used to rank books.
- **Accessible interaction** - pages provide a skip link, named icon controls,
  visible keyboard focus, modal focus return, scroll locking, reduced-motion
  fallbacks, and non-selectable app chrome while content remains selectable.

## Main Routes

| Route | Purpose |
|---|---|
| `/` | Homepage with hero and horizontal shelves |
| `/topics` | Grouped nonfiction topic catalog with direct ranked-discovery links |
| `/category/<topic>` | Category grid with vertical infinite scroll |
| `/discover?q=...` | Auto-detected topic or Open Library identity discovery |
| `/book/OL...W` | Book detail, similar books, download search |
| `/fiction/cn/book/OL...W` | Book detail with clean mode/language context |
| `/search?q=...` | Direct libgen download search page |
| `/download/<md5>` | Verified local file download, with a streamed-source fallback |
| `/api/shelf/<topic>` | JSON endpoint for homepage shelf pagination |
| `/api/category/<topic>` | JSON endpoint for category infinite scroll |
| `/api/discover` | JSON endpoint for discovery search pagination |
| `/api/book` | JSON endpoint for Open Library work details |
| `/api/suggestions` | Bounded local title/author/ISBN suggestions for the search palette |
| `/api/covers` | Batched local cover recovery for currently visible work keys |
| `/api/cn-display-title` | Cached English display-title lookup for CN browse cards |
| `/api/cn-display-titles` | Batched English display-title lookup for visible CN cards |
| `/api/similar` | JSON endpoint for canonical similar books with mapped Inventaire outage fallback |
| `/api/search` | JSON endpoint for libgen download search |
| `/api/download/prepare/<md5>` | Streams source preparation progress before an EPUB/PDF download |
| `/api/kindle/jobs` | Start a background Send to Kindle delivery |
| `/api/kindle/jobs/<job_id>` | Poll incremental delivery status |
| `/api/sendtokindle` | Compatibility endpoint for synchronous/streaming delivery |
| `/cover/<md5>/<size>.webp` | Canonical cached download-result cover |
| `/olcover/<cover_id>/<size>.webp` | Canonical cached Open Library cover |
| `/iacover/<archive_id>/<size>.webp` | Canonical cached Internet Archive cover |
| `/invcover/<entity_image_hash>/<size>.webp` | Validated cached Inventaire entity cover |
| `/api/health` | Local cache, source, and job health summary |
| `/api/metrics/web-vitals` | Lightweight browser performance metric receiver |

## Configuration

| Env Variable | Default | Purpose |
|---|---|---|
| `BOOK_LANG` | `en` | Optional default discovery language |
| `LIBFLIX_DATA_DIR` | repository directory | Writable location for SQLite, shelf, cover, and marker caches |
| `LIBFLIX_CONTACT` | repository URL | Contact value included in the Open Library and Inventaire user agent |
| `OPENLIBRARY_MIN_INTERVAL` | `1.05` seconds | Minimum process-wide interval between upstream Open Library requests |
| `OPENLIBRARY_CONNECT_TIMEOUT` | `3` seconds | Open Library connection timeout |
| `OPENLIBRARY_READ_TIMEOUT` | `15` seconds | Open Library response timeout |
| `INVENTAIRE_MIN_INTERVAL` | `0.5` seconds | Minimum process-wide interval between upstream Inventaire requests |
| `INVENTAIRE_CONNECT_TIMEOUT` | `2.5` seconds | Inventaire connection timeout |
| `INVENTAIRE_READ_TIMEOUT` | `5` seconds | Inventaire response timeout |
| `NYT_CONNECT_TIMEOUT` | `3` seconds | Wikipedia NYT-history page connection timeout |
| `NYT_READ_TIMEOUT` | `8` seconds | Wikipedia NYT-history page response timeout |
| `TOPIC_PROVIDER_WAIT_TIMEOUT` | `10` seconds | Bounded total wait for concurrent topic providers |
| `LIBFLIX_UPSTREAM_JSON_MAX_BYTES` | `2097152` | Maximum decoded Open Library or Inventaire JSON response size |
| `LIBFLIX_OL_REFRESH_PENDING_LIMIT` | `12` | Maximum active and queued stale Open Library refreshes per worker |
| `LIBFLIX_INVENTAIRE_REFRESH_PENDING_LIMIT` | `12` | Maximum active and queued stale Inventaire refreshes per worker |
| `KINDLE_SOURCE_CACHE_TTL` | `86400` seconds | Idle lifetime for a verified EPUB/PDF source copy |
| `KINDLE_SOURCE_CACHE_MAX_BYTES` | `5368709120` | Shared source-cache byte quota |
| `KINDLE_PDF_METADATA_MAX_BYTES` | `20971520` | Largest PDF rewritten solely to improve metadata |
| `KINDLE_RELAY_HOST` | `smtp.resend.com` | Managed SMTP relay hostname |
| `KINDLE_RELAY_PORT` | `465` | Managed relay TLS submission port |
| `KINDLE_RELAY_USER` | `resend` | Managed relay username |
| `KINDLE_RELAY_PASSWORD` | empty | Managed relay password |
| `KINDLE_RELAY_PASSWORD_FILE` | `/opt/libflix/shared/resend-api-key` | File-backed relay secret stored outside releases |
| `KINDLE_RELAY_SENDER` | `libflix@fomalhaut.app` | Verified From address used by the managed relay |
| `KINDLE_RELAY_MAX_ATTACHMENT_MB` | `28` | Raw attachment ceiling below Resend's encoded message limit |
| `LIBFLIX_TRUST_PROXY_HEADERS` | `0` | Accept Caddy's overwritten `X-LibFlix-Client-IP` only across the localhost Caddy-to-Gunicorn hop; enable only after applying the documented Caddy source allowlist/header rewrite |
| `LIBFLIX_RATE_LIMITING_ENABLED` | `1` | Runtime protection switch; disable only in isolated regression-test environments |
| `LIBFLIX_MEMORY_CACHE_MAX_ENTRIES` | `4096` | Maximum process-local metadata cache entries |
| `LIBFLIX_API_CACHE_MAX_ROWS` | `20000` | Maximum durable metadata cache rows |
| `LIBFLIX_API_CACHE_MAX_BYTES` | `268435456` | Maximum combined serialized durable metadata payload bytes |
| `LIBFLIX_API_CACHE_MAX_PAYLOAD_BYTES` | `2097152` | Maximum serialized size of one durable cache entry |
| `LIBFLIX_BOOK_REFRESH_PENDING_LIMIT` | `16` | Maximum active and queued book-detail refreshes per worker |
| `LIBFLIX_SIMILAR_REFRESH_PENDING_LIMIT` | `12` | Maximum active and queued recommendation refreshes per worker |

Without a managed relay, Send to Kindle settings are configured in the browser
and stored in localStorage. The SMTP password is sent only when starting a
delivery, remains in process memory for that job, and is never stored in SQLite.
SMTP targets are restricted to public addresses on secure submission ports.
Production uses the verified `libflix@fomalhaut.app` sender through Resend. The
domain has DKIM, SPF, MX, and DMARC records, while the domain-limited sending key
is provisioned once at `/opt/libflix/shared/resend-api-key` with owner
`libflix`, mode `0600`. Normal releases leave this file untouched; a recovery
copy is held in the protected GitHub `production` environment secret
`RESEND_API_KEY`. Neither the key nor relay credentials are returned to the
browser or written to SQLite.

When the password file or explicit `KINDLE_RELAY_PASSWORD` is available, the
server ignores browser SMTP credentials and stores only the user's Kindle
address in localStorage. Managed delivery accepts only `@kindle.com`
recipients, rejects prepared attachments above 28 MB, and remains covered by
the application's per-client and global delivery limits.

The app remains proxy-agnostic with proxy identity trust disabled. The current
Cloudflare production deployment enables per-client limiting only after Caddy
allowlists official Cloudflare source networks, removes public identity headers,
and overwrites `X-LibFlix-Client-IP` on its localhost Gunicorn upstream.

## Runtime Cache Files

The app writes local runtime cache files to speed up restart and repeated API
requests. They are ignored by git.

| File Pattern | Purpose |
|---|---|
| `api_cache.sqlite3` | WAL-enabled, row/byte-capped cache for metadata, source results, Kindle job events, and stale fallback |
| `rate_limits.sqlite3` | Hashed, cross-worker token buckets with bounded retention |
| `metrics.sqlite3` | Bounded hourly request, error, and Web Vital aggregates |
| `shelf_cache_<lang>_<mode>.json` | Warm shelf cache for each language and mode |
| `shelf_cache*.json` | Historical and current shelf cache files ignored by git |
| `covers/openlibrary/...` | Size-specific cached Open Library covers |
| `covers/internetarchive/...` | Size-specific cached Internet Archive fallbacks |
| `covers/inventaire/...` | Size-specific cached Inventaire entity covers |
| `covers/downloads/...` | Size-specific cached download-result covers |
| `covers/.warm-complete` | Daily successful hero/trending cover warm marker |
| `kindle-source-cache/...` | Validated 24-hour EPUB/PDF source cache |
| `kindle-delivery.lock` | Cross-worker Send to Kindle queue lock |

Legacy `api_cache.json` data is migrated once into SQLite and removed after a
successful migration. Shelf files and book hints are loaded before serving;
stale shelves refresh after a delay without blocking the first page. Expired
metadata can be served stale while a bounded background refresh runs. Memory
and SQLite cache pruning continues periodically after startup.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for data flow, API contracts, caching
details, and template responsibilities.

See [Performance and resilience](docs/PERFORMANCE_AND_RESILIENCE.md) for the
latency model, failure behavior, tuning controls, observability, and verification
checklist.

See [CHANGELOG.md](CHANGELOG.md) for dated implementation details.

## Tech Stack

- **Backend:** Flask, requests, BeautifulSoup4
- **Frontend:** Local CSS and vanilla JavaScript
- **Discovery:** Open Library Search/Works/Covers APIs, mapped Inventaire identity/topic metadata and covers, and an attributed Wikipedia NYT number-one history signal
- **Downloads:** Modular downloader interface, currently libgen.li
- **Port:** 5800

## Verification

Useful local checks:

```bash
LIBFLIX_DATA_DIR="$(mktemp -d)" LIBFLIX_RATE_LIMITING_ENABLED=0 \
  python3 -m unittest discover -s tests -v
python3 -m py_compile app.py topic_discovery.py nyt_bestsellers.py book_preparation.py kindle_delivery.py security_runtime.py downloaders/base.py downloaders/libgen.py
python3 app.py
```

For UI validation, use headless Playwright with an isolated Chromium context.
Validate persistent navigation, browser history, delayed route loading,
quick-peek positioning after scroll, fixed desktop/mobile search, My Library,
editorial shelf hydration, progressive edition loading, cover cache hits, and
background Kindle progress. Do not use the installed Chrome profile.

```text
http://127.0.0.1:5800
http://127.0.0.1:5800/category/history
http://127.0.0.1:5800/discover?q=focus
http://127.0.0.1:5800/discover?q=focus&intent=identity
http://127.0.0.1:5800/fiction/cn/discover?q=三体
http://127.0.0.1:5800/book/OL82563W
http://127.0.0.1:5800/search?q=Harry%20Potter
```
