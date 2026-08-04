# Changelog

## 2026-08-04 - End-to-end latency pass

### Faster browsing and covers

- Reduced internal navigation responses to page-specific HTML, cancelled
  superseded requests, and restored per-history-entry scroll positions.
- Moved homepage, category, and discovery hydration to 12-card batches and
  deferred covers until they are within one viewport.
- Added canonical `.webp` cover URLs, cross-worker request coalescing, shared
  negative caching, single-fetch variant fan-out, and complete hero/trending
  background warming.
- Made cold discovery render immediately and hydrate through its API with a
  visible retry state when the source is unavailable.

### Faster Send to Kindle

- Promoted the smallest EPUB within the strongest accuracy tier as `Fastest to
  Kindle` and the default best match.
- Added atomic validated source-file reuse with a 24-hour TTL and 5 GiB LRU
  quota, including format, byte-count, and MD5 verification.
- Started SMTP and cover preparation in parallel with source work, streamed MIME
  attachments directly to SMTP, exposed byte/rate/ETA progress, and retried one
  interrupted upload with the same message id.
- Added a persistent global delivery tray, faster adaptive status polling, and
  optional server-managed relay configuration.
- Avoided cosmetic-only EPUB rewrites and full metadata rewrites for PDFs over
  20 MiB.

## 2026-08-02 - Resilient search for newly catalogued books

### Fixed

- Discovery now uses one unqualified Open Library relevance query and validates
  each result's language locally, so sparse records are not hidden behind a
  strict upstream language clause.
- Sparse works without language or cover metadata can appear with a cover
  placeholder instead of being silently dropped.
- Local validation rejects explicitly mismatched languages and keeps English
  and Chinese discovery isolated.
- Versioned the discovery result cache so previously cached empty searches do
  not mask recovered books.
- Cold book-detail requests now report that metadata is refreshing, allowing
  the page to retry instead of treating a first-time work as permanently absent.
- Download lookup waits for a real work title and author, then rejects unrelated
  editions below the title and author relevance thresholds.

## 2026-07-31 - Local-first performance and resilient delivery

### Faster page and metadata loading

- Made book HTML local-first: card hints and assembled detail cache entries
  render immediately instead of waiting for an Open Library work request.
- Added seven-day assembled book-detail caching with 90-day stale fallback and
  bounded background refresh.
- Added a rate-limited Open Library gateway with a descriptive user agent,
  request coalescing, short connect/read timeouts, a three-failure circuit
  breaker, and durable stale responses.
- Fixed empty cached category pages hiding a populated first shelf; useful local
  books now win over an empty upstream/cache result.
- Loaded SQLite and shelf hints during Gunicorn import so worker processes start
  with the same local-first behavior as the development server.
- Added stale fallback for download-source searches and reduced source search and
  resolver timeouts.

### App-style navigation

- Kept the navbar mounted across same-origin navigation while replacing page
  content, page-specific styles, and page-specific scripts.
- Added History API back/forward support and automatic full-navigation fallback.
- Removed root-level View Transition and body-entry fades so tab changes no
  longer dim or flicker the entire page.
- Kept the current content visible until the destination is ready and replaced
  the full-screen internal-route loader with a delayed navbar progress line.
- Added a bounded five-minute HTML cache that reuses completed and in-flight
  intent fetches across recent category visits.
- Preserved loaded cover state across page swaps so browser-cached images do not
  replay their shimmer and opacity animations.
- Limited intent prefetching to four concurrent documents, retained a bounded
  recent-page memory, and required 260 ms of stable pointer hover, keyboard
  focus, or touch intent.
- Added cleanup hooks for homepage timers, category/discovery observers, book
  retries, and active search requests before content replacement.

### Faster cover delivery

- Routed Open Library and download-result covers through validated persistent
  disk caches.
- Added size-specific WebP thumbnails when Pillow is available, with safe JPEG
  fallback when it is not.
- Added per-cover request coalescing, 30-day browser caching,
  `X-LibFlix-Cover-Cache`, and cover-specific `Server-Timing`.
- Added a once-daily bounded warm of the first visible Trending covers.
- Kept stable cover geometry and a subtle reduced-motion-aware loading shimmer.

### Reliable Send to Kindle

- Removed MOBI and AZW/AZW3 editions from download results and format controls
  because Send to Kindle does not accept them.
- Added matching server-side validation so stale clients and direct job requests
  cannot start an unsupported delivery.
- Replaced the UI's long-lived streaming request with background Kindle jobs.
- Persisted job state and ordered progress events in SQLite so any Gunicorn
  worker can answer browser polls.
- Restored active delivery progress from `sessionStorage` after navigation or a
  refresh.
- Retained the active job after repeated status-connection failures and exposed
  a safe `Check status` retry instead of starting a duplicate send.
- Kept SMTP credentials only in process memory and out of the job database.
- Restricted SMTP hosts to public addresses and secure submission ports, with
  implicit TLS support on port 465.
- Limited concurrent delivery workers and converted stale five-minute jobs into
  explicit interrupted failures instead of permanent progress stalls.
- Added bounded HTTP range resume for interrupted source transfers, including
  returned-offset validation, complete-byte verification, and visible resume
  progress.
- Added a visible pre-upload book-polishing stage using the canonical Open
  Library identity and selected edition metadata.
- Added clean Unicode-safe attachment titles for every supported format.
- Added non-destructive EPUB package repair for title and missing
  author/language/publisher/date/description/identifier metadata, including a
  cached canonical cover only when the EPUB has none. The largest displayed
  cover variant is reused before any network request.
- Added PDF title and missing author/description metadata with `pypdf`.
- Made metadata preparation fail open so malformed or encrypted containers are
  still delivered as their original bytes under the clean filename.

### Better download recommendations

- Renamed the book-page section from `Download options` to `Download`.
- Kept the best-ranked edition visible while collapsing remaining editions
  behind an `Other options` disclosure without a redundant result count.
- Added the cleaned attachment title and server-measured delivery duration to
  the successful Send to Kindle notification.
- Refined the completion notification with a check-circle status icon, clearer
  title hierarchy, compact timing text, and restrained success styling.
- Strengthened English-mode ranking against Chinese title/author metadata and
  demoted Chinese source branding without penalizing Chinese mode.
- Increased Kindle suitability weighting for EPUB and applied stronger
  implausible-file-size penalties.
- Added short recommendation reasons to explain title, author, language, format,
  and send-size signals behind the selected candidate.

### Observability and documentation

- Added `/api/health` for local database, shelf, Open Library circuit, and Kindle
  job status.
- Added request and operation `Server-Timing` plus a small
  `/api/metrics/web-vitals` receiver for LCP, CLS, and INP.
- Added regression coverage for stale metadata, category fallback, local-first
  book responses, cross-worker job events, credential non-persistence, EPUB
  package repair, cover preservation, PDF metadata, and malformed-file fallback.
- Added `docs/PERFORMANCE_AND_RESILIENCE.md` with latency model, cache layers,
  failure behavior, security boundaries, tuning, and headless verification.
- Documented that the implementation is entirely application code and requires
  no Cloudflare configuration change.

## 2026-07-24 - Loading and rendering performance

- Replaced whole-file `api_cache.json` reads and 15 MB rewrites with a
  WAL-enabled SQLite key/value cache, including one-time migration and expiry
  pruning.
- Changed startup to hydrate all language/mode shelf caches immediately and
  refresh stale shelf sets later without blocking the first page.
- Reduced initial homepage HTML by rendering stable shelf skeletons and
  hydrating complete 40-book rows only as they approach the viewport.
- Deferred hero descriptions until their book is active and added a lightweight
  description-only API response path.
- Deferred More Like This metadata and covers until that section approaches the
  viewport.
- Removed idle category-page prefetching in favor of pointer, focus, and touch
  intent.
- Removed synchronous Open Library work from book-page HTML responses by
  rendering card-backed book hints first and hydrating full details after
  paint. Book metadata and normalized download searches now persist in SQLite.
- Removed the artificial route-transition pause and enabled one-book document
  prefetch after deliberate card hover, focus, or touch.
- Added automatic CN download fallbacks using cleaned edition names, all
  Chinese Open Library edition aliases, Traditional-to-Simplified conversion,
  known catalog-title overrides, and a final English-title attempt while
  retaining the Chinese file filter.
- Replaced the source-order `Best match` label with global candidate ranking.
  Title and author similarity now dominate, EPUB-family formats are preferred
  for reading/Kindle workflows, malformed file sizes are penalized, and year is
  limited to a small tie-breaker.
- Fixed successful More Like This covers rendering a second full-height fallback
  panel because component `display:flex` styles overrode the HTML `hidden`
  attribute.
- Improved More Like This by skipping broad catalog subjects, combining up to
  two specific subjects, prioritizing works shared by both, and collapsing
  duplicate translated/edition titles.
- Batched visible CN display-title resolution into one endpoint with bounded
  server concurrency.
- Removed the blocking Bootstrap CDN dependency and versioned local CSS/JS for
  immutable caching.
- Sent Open Library covers directly to its CDN, preconnected the cover host, and
  limited large hero artwork to the active cover.
- Served first shelf/category API pages directly from the already deduplicated
  in-memory shelf cache.

## 2026-07-21 - Send to Kindle progress

- Added streamed Send to Kindle progress events without introducing a
  background job or storing SMTP credentials server-side.
- Added real byte-based download progress when content length is available and
  an indeterminate transfer state when it is not.
- Added responsive in-row delivery UI covering preparation, book download,
  attachment creation, email authentication, sending, completion, and failure.
- Kept the original JSON endpoint behavior for compatibility while the app uses
  newline-delimited progress streaming through `?stream=1`.
- Added accessible progress semantics, live stage announcements, mobile-safe
  layout, reduced-motion behavior, and retryable error presentation.
- Fixed translated Open Library works so book pages retain the selected EN or
  CN edition title and cover instead of reverting to a canonical title in a
  different language and searching downloads with it.
- Fixed CN download options defaulting to English, added an explicit Chinese
  filter, accepted bilingual source records, and preferred Han-character
  edition titles over pinyin when Open Library supplies both. CN book pages now
  search localized titles without forcing an English author token that Chinese
  source records do not contain.
- Standardized CN browsing on stable English edition titles where Open Library
  provides them, while book pages show a verified Chinese title beneath the
  primary heading and download searches retain localized edition titles.
- Added cached ISBN-based Han-title resolution for Chinese works that Open
  Library exposes only as pinyin, plus bounded lazy English-title enhancement
  for visible CN shelf cards and hero items.
- Clarified the Settings menu with a Kindle section and a status-aware Settings
  row, and preserved an existing app password when editing configured details.
- Fixed translated works displaying Dutch or other incompatible work-level
  descriptions by falling back to a matching English edition description.

## 2026-07-17 - Interface and download experience overhaul

### Reworked book and download surfaces

- Rebuilt book previews around a responsive cover-and-metadata spotlight with a
  cover-derived backdrop, readable long titles, async description cleanup, and
  mobile `Read more` clamping.
- Replaced the dense download tables on book previews and direct search with a
  shared responsive edition list.
- Added cover, title, author, publisher, format, year, size, page, and language
  hierarchy to each edition while keeping the primary actions easy to scan.
- Added format-aware Download labels, a dedicated Kindle action, a `Best match`
  marker, stable cover placeholders, and two-line mobile edition titles.
- Hid download pagination when only one page exists and replaced raw download
  source failures with short timeout/network recovery states.
- Consolidated preview and direct-search filters into one collapsible partial
  and kept direct-search URLs limited to the actual query.

### Improved shared application UI

- Added a shared visual system for navigation, cards, details, filters, edition
  rows, settings, focus states, empty states, and toast feedback.
- Moved Kindle configuration into the global Settings menu and redesigned it as
  a responsive sheet with password visibility, local persistence, focus return,
  body scroll locking, and a forget action.
- Restored More Like This as a single horizontal shelf with hidden scrollbars,
  hover-only card metadata, and the shared cursor-anchored quick peek.
- Matched the category heading-to-grid gap to homepage shelves while preserving
  the centered category grid.
- Removed provider-specific quick-peek loading copy and removed dangling source
  link labels from book descriptions.
- Removed body transforms from the page-entry fade so fixed modals, quick peek,
  and transition overlays stay attached to the viewport after scrolling.

### Reduced rendering work

- Replaced hundreds of offscreen shelf shimmer animations with static cover
  placeholders while retaining focused loading feedback for active surfaces.
- Kept stable card, cover, hero, toolbar, and edition dimensions to avoid layout
  shift during async image and result loading.
- Added shared reduced-motion handling and maintained one route-transition
  loader instead of stacking page-level loading overlays.

### Verification

- Added isolated headless Chromium coverage for desktop and mobile layouts,
  overflow, duplicate IDs, control names, image alternatives, quick-peek bounds,
  category alignment, edition actions, filter state, Kindle focus, description
  clamping, clean URLs, and console/request failures.

## 2026-07-05 - Hero, quick peek, and app polish

### Hardened the homepage hero

- Renamed the first homepage shelf and first top-nav category from `New` /
  `New & Popular` to `Trending` across fiction and non-fiction.
- Added cache-time shelf label normalization so older disk or memory shelf cache
  entries render with the current `Trending` label.
- Kept the hero at a fixed height while changing books so description length and
  cover dimensions do not move the shelf below it.
- Reworked hero title fitting to avoid character-level wraps, keep short titles
  on one line where possible, and scale long titles only enough to fit.
- Made side covers in the hero stack clickable selectors.
- Smoothed hero transitions across text, cover stack, and background layers.
- Removed the circular containers around hero carousel arrows.
- Added extra spacing above the first homepage shelf.

### Improved quick peek previews

- Added a hover/focus quick-peek overlay for book cards that fetches Open
  Library details through `/api/book`.
- Removed subject/category chips from the quick-peek overlay so the description
  has more room.
- Increased the quick-peek description allowance and viewport-bounded height.
- Anchored the quick-peek overlay to the latest cursor position, including while
  async Open Library details are loading, so it stays near the hovered card.

### Tightened shared app chrome

- Vertically centered the top navigation contents in the fixed-height desktop
  navbar.
- Kept collapsed search and settings controls as bare icons.
- Removed the visible `Go` label from the search submit control.
- Made the shared LibFlix loading overlay more consistent for internal links and
  forms by exposing `window.LibFlixLoading.show()` / `hide()` from the navbar
  partial and forcing the overlay visible before route changes.
- Documented the project preference for headless Playwright UI validation.

## 2026-07-04 - Open Library discovery and browsing UX refresh

### Added EN/CN discovery language switching

- Added an EN/CN language toggle in the navbar.
- Added `book_lang` query/cookie handling.
- English maps to Open Library `eng`.
- Chinese maps to Open Library `chi`.
- Open Library queries now include the active language filter.
- Open Library edition selection now prefers titles/records matching the active
  language.

### Changed search behavior

- The navbar search now searches Open Library discovery through `/discover`.
- Download search remains available from `/search` and from each book preview.
- This separates "find a book" from "find a downloadable file".

### Added discovery search page

- Added `templates/discover.html`.
- Added `/discover` and `/api/discover`.
- Discovery results render as book cards.
- Discovery pagination now matches category pages with automatic vertical
  infinite scroll and no visible provider label.

### Improved homepage shelves

- Increased initial shelf fetch volume.
- Homepage shelves now render up to 40 books per shelf.
- Added shelf-order dedupe so books from earlier homepage shelves are excluded
  from later shelves.
- Later shelves now refill from deeper Open Library pages when duplicates are
  removed.
- Homepage shelf refill uses bounded parallel Open Library candidate prefetching
  before applying shelf-order priority.
- Homepage JavaScript also removes duplicate cards from stale cached markup and
  newly loaded horizontal pages.
- Homepage hero now cycles through multiple featured books, removes the
  language/mode metadata chips, and gives the cover stack more visual space.
- Reworked the top navigation so wide screens keep category tabs in the primary
  row while search and mode/language settings expand from compact controls.
- Locked hero dimensions while cycling and changed cover swaps to a gradual
  crossfade over a subtle animated background.
- Added shared app-style page fade/loading transitions for internal navigation.
- Added clean browsing routes for mode/language/category/discovery paths while
  keeping older query-string URLs as redirects.
- Added clean book preview routes such as `/book/OL3431878W` and
  `/fiction/cn/book/OL3431878W`; legacy `/preview?...` URLs now redirect when
  an Open Library work key is present.
- Added horizontal infinite scroll for homepage shelf rows.
- Replaced the old full-height More tile with a compact round arrow button.
- Hidden visible horizontal scrollbars on homepage shelves.

### Improved category pages

- Category pages now use vertical infinite scroll.
- Removed the visible Load More button from category pages.
- Added a bottom scroll sentinel and IntersectionObserver loading.
- Added scroll and viewport-size fallback loading.
- Category grids keep appending `/api/category/<topic>` pages as the user nears
  the bottom.

### Removed visible count summaries

- Removed category count text such as `80 books`.
- Removed discovery summary text such as `x shown from y matches`.
- Removed download result summary text such as `x of y results`.
- Removed page summary text such as `Page x of y`.
- Collapsed preview-page download filters behind a compact `Filters` button so
  results stay closer to the top of the page.
- Kept API `total` and `total_pages` fields for pagination logic.

### Improved book detail shelves

- Hidden the horizontal scrollbar from the More Like This shelf.
- Similar books continue to load from Open Library subjects.

### Documentation

- Rewrote README to match the current Open Library-only architecture.
- Rewrote ARCHITECTURE.md with current route, API, caching, and frontend flow
  documentation.
- Added this changelog to capture the feature set and migration notes.
