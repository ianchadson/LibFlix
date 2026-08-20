# Changelog

## 2026-08-21 - Instant discovery and local continuity

- Replaced the expanding navbar search with a fixed desktop/mobile palette that
  keeps page geometry stable, supports keyboard navigation and recent terms,
  and returns bounded title/author/ISBN suggestions from the local catalog
  without waiting for an upstream provider.
- Added non-personal `New This Week` and `Short Reads` shelves for fiction and
  non-fiction, plus Top 10 numerals on Trending. The new rails use the existing
  near-viewport hydration, infinite horizontal loading, and shelf-order dedupe.
- Added browser-local Saved, Recent, and Kindle history tabs under `My Library`.
  No account or server profile was introduced, and these lists do not affect
  discovery or recommendations.
- Changed book-page edition lookup to render the best candidate from a narrow
  first pass, then populate collapsed alternatives during idle time without
  removing a usable result after a later source failure.
- Seeded More Like This from the local catalog for immediate paint, preserved
  visible cards during remote refinement, rejected broad taxonomy subjects, and
  required displayed-author agreement for same-author recommendations.
- Added batched visible-card cover recovery across local hints, assembled
  details, alternate-language metadata, and the catalog corpus, with bounded
  background detail refresh for unresolved works.
- Added focused cover-to-book View Transitions while retaining stable app-shell
  navigation and reduced-motion behavior.
- Fixed duplicate mobile Settings handlers that caused the sheet to open and
  close in the same tap.
- Added API, cache, privacy, performance, and regression documentation for the
  new search, cover, recommendation, download, and local-library paths.

## 2026-08-17 - Discovery and cover outage hardening

- Kept strict title/author discovery available through directly mapped
  Inventaire works when Open Library is unavailable.
- Corrected topic fallback text search to use the reader's actual query, raised
  the useful-result threshold before ending provider waits, and hydrated mapped
  author labels without admitting unresolved native records.
- Added a validated Inventaire cover proxy and Open Library-to-Internet Archive
  cover fallback, including MIME detection when image conversion is unavailable.
- Replaced question-mark artwork failures with an intentional book treatment and
  reused canonical book covers for download editions missing their own image.
- Ensured failed lazy cover requests cannot be repainted as broken-image glyphs
  by card sizing rules while the intentional fallback is active.
- Preserved reviewed exact-title identities, including The Energy Game and The
  Art of Simple Living, through complete catalog outages without inventing a
  cover or downloadable edition.
- Preserved decoded covers and card nodes across unchanged partial-source polls,
  and moved the mobile recovery message below the intent/filter controls.
- Added mapped Inventaire subject and same-author recovery for More Like This,
  retained semantic topic evidence as an internal recommendation seed, and
  rejected meditation/murder title collisions from topical shelves.
- Versioned topic-card caches and added outage, route, security, cover, and Kindle
  metadata regression coverage.

## 2026-08-17 - Managed Kindle sender

- Verified `fomalhaut.app` for Resend with DKIM, SPF, MX, and DMARC records.
- Added file-backed production relay credentials outside release directories
  and configured `libflix@fomalhaut.app` as the approved sender.
- Removed legacy SMTP credentials from managed-browser storage; only the
  user's `@kindle.com` destination remains local.
- Added a provider-safe 28 MB prepared-attachment ceiling and exact approved
  sender guidance in Kindle settings.
- Documented secret ownership, recovery storage, deployment persistence, and
  managed-delivery safeguards.

## 2026-08-07 - Stable book hydration and restored descriptions

- Preserved bounded provider descriptions in topic results, repaired minor
  joined-word source defects, rejected heavily malformed summaries, and used a
  clean edition description before declaring a book description unavailable.
- Sanitized legacy assembled-detail cache entries on read so an older malformed
  summary cannot bypass the new quality checks during cache migration.
- Added real two-line descriptions to topic cards without duplicating reason
  chips when a source has no usable summary.
- Made book-page hydration monotonic: populated title, author, description, and
  cover nodes are no longer rewritten; decoded covers replace placeholders
  atomically; and expanded descriptions stay expanded.
- Stopped richer metadata from restarting an already-visible download search or
  recommendation shelf. Complete and content-ready pages skip duplicate detail
  polling, while provisional HTML bypasses both browser and in-app page caches.
- Kept Quick Peek in a bounded loading/retry state while a description refresh
  is still pending instead of flashing a false unavailable message.

## 2026-08-07 - Aligned Topics and attributed NYT #1 history

- Unified the topic result header, Start here, Explore, and grid widths; aligned
  homepage Topics with Trending; and stabilized reason/action baselines.
- Added a bounded, no-key background fetch of the current and previous
  Wikipedia NYT number-one year pages as an exact title-and-author ranking
  signal. Open Library remains canonical and the signal never supplies books.
- Added fail-closed HTML validation, seven-day stale fallback, versioned topic
  cache invalidation, one strongest reason per card, and source attribution.

## 2026-08-05 - Browseable topic catalog

- Added a dedicated `/topics` surface with featured starting points, six
  compact groups, and an explicit freeform topic search.
- Added an `Explore by topic` rail to the nonfiction homepage and a `Topics`
  entry to desktop and mobile browse navigation.
- Kept topic browsing zero-fetch until a topic is selected: the catalog loads
  no provider results or covers and every link enters `/discover` explicitly as
  a nonfiction topic query.
- Made the 30 public topics the source of truth for the weekly production
  quality benchmark, preventing the browse catalog and monitored queries from
  drifting apart.

## 2026-08-04 - Relevance-first topic discovery

### Topic intent and source safety

- Added automatic topic-versus-identity intent detection to `/discover`, with a
  visible `About` / `Title or author` override. ISBNs, Open Library work ids,
  quoted titles, and title-by-author queries retain the strict identity path.
- Added a deterministic, versioned expansion corpus for more than 30 broad
  topics. Each request fans out to no more than three normalized terms and two
  approved Inventaire semantic claims; no per-request language model or user
  profile is involved.
- Added a provider-neutral topic pipeline that combines Open Library subject
  results with bounded Inventaire work enrichment. Inventaire candidates are
  accepted only when Wikidata `P648` maps directly to an Open Library work;
  edition ids, unresolved records, and arbitrary provider covers are rejected.
- Kept Open Library canonical for every rendered work identity, book route,
  detail, cover, download alias, and Send to Kindle handoff.

### Ranking and topic UX

- Added a hard local relevance gate over subject, title, description, and
  approved semantic evidence before any popularity signal can matter.
- Added weighted reciprocal-rank fusion across provider and expansion ranks,
  followed by bounded reading-log, syllabus, rating, edition, cover,
  Inventaire-popularity, and source-consensus tie-breakers.
- Merged duplicate work identities, made remaining ordering deterministic, and
  capped each author at two topic results.
- Added up to six `Start here` cards followed by a duplicate-free
  `Explore <topic>` grid, concise factual reason chips, and `Find an edition`
  actions that preserve identity verification before download or Kindle work.
- Kept titles and authors visible on coverless topic cards, including when a
  remote cover fails after the card is rendered.
- Added a compact topic Filters panel for Type, Language, Published, and Best
  match / Newest sorting.

### Pagination, failure behavior, and API

- Cached complete ranked windows before stable 30-book Explore pagination;
  page-one `Start here` items are excluded from Explore and provider totals are
  never summed. Content-derived snapshot ids reject page mixing when a ranking
  changes between scroll requests.
- Added independent Open Library and Inventaire request spacing, coalescing,
  timeouts, stale caches, hard-capped refresh queues, response-byte/schema
  validation, and circuit breakers behind a bounded overall topic-provider wait.
- Prefer a stale complete topic window over a newly partial refresh. Partial
  windows and outage empties are not persisted as authoritative results, all
  partial responses are no-store, and a source-less outage returns a no-store
  503 with circuit-aware retry timing.
- Extended `/api/discover` additively with `intent`, `topic_mode`,
  `display_query`, `start_here`, `partial`, `sources`, `source_unavailable`,
  normalized `filters`, `retry_after`, `snapshot_id`, and expansion/ranker
  version fields while retaining the existing card and pagination fields.
- Added weighted topic-request rate costs, cheap cached-page reads, weekly
  paced 30-topic quality monitoring, and a post-deploy production topic-surface
  smoke check.
- Rebuilt hydrated topic cards and Quick Peek metadata with DOM text/attribute
  APIs so provider titles, authors, and descriptions cannot become executable
  markup.
- Kept Topic Discovery stateless: no accounts, personal library, reading
  history, reading-progress tracking, or personalized recommendation profile
  was added.

## 2026-08-04 - Identity accuracy, runtime protection, and mobile app shell

### Search and recommendation accuracy

- Preserved canonical/work/edition title aliases, all available authors, and
  bounded ISBN identity signals from Open Library metadata.
- Search server-owned aliases in two-query batches, capped at six source calls,
  and continue past weak PDF-only batches until a high-confidence EPUB appears.
- Keep partial alias failures out of persistent result caches and use bounded
  negative/partial caching for empty More Like This refreshes.
- Added local discovery reranking and literal relevance thresholds that remove
  unrelated provider filler before rendering.
- Reworked More Like This to combine up to two specific subjects with a
  same-author source, capped at three Open Library searches.

### Public-service protection and observability

- Added a restrictive CSP, frame/plugin blocking, permissions policy, HSTS
  behind the trusted TLS proxy, and other browser security headers.
- Allowed Cloudflare's integrity-protected injected analytics beacon while
  keeping analytics submission on the same origin.
- Added weighted cross-worker SQLite token-bucket limits for discovery,
  recommendations, book details, download, Kindle, and browser metrics, with
  hashed identities and `Retry-After`.
- Hard-capped metadata refresh queues, memory/durable cache growth, individual
  cache payloads, and request bodies; health now verifies enforcement and
  telemetry databases are writable.
- Replaced log-only Web Vitals with bounded hourly SQLite aggregates and added
  durable request/error timing aggregates without raw URLs, IPs, or payloads.

### Installable mobile experience

- Added a standalone PWA manifest, app icons, install affordance, safe offline
  screen, and a narrowly scoped service worker.
- Replaced the long mobile category strip with Home, Search, Browse, and
  Settings navigation plus an accessible category sheet; desktop stays intact.
- Explicitly excluded downloads, Kindle routes/settings, credentials, covers,
  and private/no-store responses from service-worker storage.

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

- At this release, the navbar searched Open Library discovery through
  `/discover`; the later Topic Discovery release adds multi-source topic mode
  while retaining this Open Library identity path.
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

- Rewrote README to match the then-current Open Library-only architecture;
  the later Topic Discovery entry supersedes that source description.
- Rewrote ARCHITECTURE.md with current route, API, caching, and frontend flow
  documentation.
- Added this changelog to capture the feature set and migration notes.
