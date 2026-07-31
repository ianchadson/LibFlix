# Performance and Resilience

This document describes the application-level performance work in LibFlix. It
covers the code paths, cache behavior, failure modes, instrumentation, and
verification used by the current implementation.

No Cloudflare setting is required by this design. A reverse proxy or CDN may
still cache versioned static files and cover responses, but every behavior below
works in Flask and the browser without edge-specific configuration.

## Goals

1. Render a useful page from local data before waiting for an upstream service.
2. Keep navigation visually continuous without hiding genuine failures.
3. Avoid duplicate requests and bursts against Open Library.
4. Make repeat cover loads effectively local.
5. Let Send to Kindle continue across navigation and report real progress.
6. Keep animation bounded and responsive on mobile and lower-power devices.
7. Expose enough timing and health data to diagnose regressions.

## Request Model

### Homepage and category pages

- Shelf files and their book hints load before the app begins serving.
- The initial homepage document contains the hero and fixed-size shelf
  placeholders instead of every card and cover.
- Shelves hydrate near the viewport and horizontal pages load near the row end.
- Category page 1 can use the populated shelf cache when an older empty category
  cache entry exists.
- An empty upstream response does not overwrite a useful local first page.
- Later category and discovery pages continue to use infinite scroll.

### Book pages

- A card records a lightweight local hint containing the work key, title,
  author, and cover.
- `GET /book/<work_id>` renders from that hint or an assembled book-detail cache.
- The HTML response does not wait for Open Library.
- `GET /api/book` returns the best local data immediately and marks the response
  as `refreshing` when a background update is running.
- The browser may retry a refreshing detail or similar-books response, but the
  download search begins independently.
- A stale description is preferred to an empty loading surface.

### Same-origin navigation

The navbar owns a small persistent app shell:

1. Pointer intent starts at most four concurrent same-origin prefetches after a
   stable hover delay; recently prefetched pages are remembered in a bounded set.
2. A normal internal click fetches the destination HTML.
3. The existing navbar remains mounted while the main content and footer swap.
4. Page-specific CSS and JavaScript are synchronized.
5. History and `popstate` use the same path.
6. Any unsupported response or JavaScript error falls back to normal browser
   navigation.

The route loader waits 180 ms before becoming visible. Fast local transitions
therefore do not flash a loading overlay, while slower transitions still provide
feedback. Language changes use a full navigation so the server-selected locale
and cookie remain authoritative.

## Open Library Gateway

All Open Library JSON requests pass through one helper rather than calling the
provider directly from individual routes.

### Courtesy and load control

- A descriptive `LibFlix/1.0` user agent includes `LIBFLIX_CONTACT`.
- Requests are spaced by `OPENLIBRARY_MIN_INTERVAL`, defaulting to 1.05 seconds
  per process.
- Each stale-metadata refresh class (raw Open Library, assembled book details,
  and similar books) is capped at two workers; the shared request interval still
  governs their upstream calls.
- Concurrent requests for the same cache key coalesce around one upstream call.
- Connection and read timeouts default to 3 and 8 seconds.

### Circuit breaker

Three consecutive Open Library failures open a 60-second circuit. During that
window, callers receive stale local data immediately instead of repeatedly
waiting for a host that is already known to be unavailable. A later successful
request resets the failure count.

The breaker is process-local. SQLite stale data is shared by all Gunicorn
workers, so each worker can still render useful content during an outage.

### Durable stale fallback

Open Library responses have a normal fresh TTL and may remain eligible as stale
fallback for up to 90 days. Expiration no longer deletes a row during lookup.
The route can return that stale value while a bounded refresh runs in the
background.

This is intentional for book metadata: titles, authors, descriptions, and
subjects change infrequently, while an upstream timeout is immediately visible
to the user.

## Cache Layers

| Layer | Scope | Main use |
|---|---|---|
| In-process memory | one worker | hottest metadata, shelves, request coalescing |
| SQLite WAL cache | all workers | metadata, assembled book details, search results, Kindle jobs |
| Shelf JSON files | all workers/restarts | instant homepage/category seed data |
| Cover files | all workers/restarts | size-specific Open Library and download-result images |
| Browser HTTP cache | one browser | immutable static assets and long-lived cover responses |

SQLite rows are retained for the longest supported stale window and pruned
during initialization. WAL mode and `synchronous=NORMAL` keep independent reads
cheap while avoiding whole-cache rewrites.

### Important cache keys

| Data | Key family | Fresh lifetime |
|---|---|---|
| Open Library JSON | `ol:*` | 6 hours on disk |
| Assembled book detail | `book_detail:v3:*` | 7 days |
| Similar books | `similar:v2:*` | 7 days |
| Download source search | `download_search:v4:*` | 15 minutes |
| CN/English title helpers | language-specific keys | 30 days |

Stale lifetimes are longer than fresh lifetimes. Routes only use stale data when
the fresh value is unavailable or while a refresh is pending.

## Cover Pipeline

Browser requests use local endpoints:

```text
/olcover/<cover_id>/<size>
/cover/<md5>/<size>?dir=<cover_directory>
```

Both endpoints:

- validate identifiers before constructing an upstream URL;
- use stable hashed disk paths;
- share concurrent fetches through a per-path lock;
- request only the required `S`, `M`, or `L` rendition;
- store a size-specific WebP thumbnail when Pillow is installed;
- fall back to the provider JPEG without breaking the request when conversion
  is unavailable;
- return a 30-day browser cache policy with a stale allowance;
- expose `X-LibFlix-Cover-Cache: HIT|MISS`;
- include `Server-Timing` for cache/fetch diagnosis.

The first visible Trending covers are warmed in the background once per day.
Warming uses three workers and never blocks startup or a page response.

Cover elements keep stable dimensions and a restrained shimmer until the image
decodes. The animation stops after load and respects reduced-motion settings.

## Download Search

The download source is independent from discovery. A source timeout does not
prevent the book page, description, or More Like This shelf from rendering.

- Search and resolver calls use bounded connect/read timeouts.
- A fresh result is cached for 15 minutes.
- A stale successful result can be returned when the source is unavailable.
- Search timing is included in `Server-Timing`.
- Failure states are scoped to Download options and offer a retry.

### Best for Kindle ranking

Ranking is deterministic and evaluates:

- exact and approximate title agreement;
- author agreement;
- active file-language agreement;
- EPUB, PDF, MOBI, and AZW3 suitability;
- plausible file size;
- publisher, page, and cover completeness;
- a small publication-year tie-breaker.

English mode applies an explicit penalty to Han characters in title/author
metadata and a smaller penalty to Chinese source branding in publisher data.
Chinese mode does not apply those penalties.

The selected result displays short evidence labels such as `Strong title
match`, `Author match`, `English`, `Kindle-ready EPUB`, and `Easy to send`.

## Background Send to Kindle

The browser starts a delivery with:

```http
POST /api/kindle/jobs
```

The server returns a job id immediately. The selected row polls:

```http
GET /api/kindle/jobs/<job_id>?cursor=<last_event>
```

Job state and progress events are stored in SQLite, which lets any Gunicorn
worker answer a later poll. The active job id is stored in `sessionStorage`, so
navigation or a refresh can restore the progress panel.

Temporary polling failures enter a reconnecting state with bounded backoff. If
the status connection remains unavailable, the UI keeps the job id and offers
`Check status`; it does not start a duplicate delivery or claim that the
background send failed.

### Security and lifecycle

- SMTP credentials are validated and kept only in process memory.
- Credentials are never written to SQLite or returned by the status API.
- SMTP hosts must resolve to public addresses.
- Only secure submission ports 465, 587, and 2525 are accepted.
- Port 465 uses implicit TLS; other accepted ports upgrade with STARTTLS.
- At most two deliveries run concurrently in a process.
- Jobs left running for more than five minutes are marked interrupted rather
  than appearing permanently stuck.
- Completed and failed jobs are periodically pruned.

The older `/api/sendtokindle` endpoint remains available for compatibility, but
the current UI uses background jobs.

## Observability

### Health

`GET /api/health` reports:

- application status;
- SQLite availability;
- loaded shelf state;
- Open Library circuit state;
- active Kindle job counts.

It is intentionally local and lightweight. It does not make a blocking external
probe on every health request.

### Server timing

Key responses attach `Server-Timing` entries for work such as:

- total Flask request duration;
- metadata cache/fetch;
- download search;
- cover cache/fetch.

These values are visible in browser developer tools and can be collected by a
reverse proxy without changing application behavior.

### Browser metrics

The persistent navbar observes LCP, CLS, and INP when supported. Values are sent
with `sendBeacon` to `POST /api/metrics/web-vitals` at page hide. The endpoint
validates and logs small metric payloads; it does not add a third-party analytics
dependency.

## Failure Behavior

| Failure | User-visible behavior |
|---|---|
| Open Library timeout | Cached page/detail remains visible; refresh retries later |
| Open Library repeated outage | Circuit opens briefly; stale data returns without repeated waits |
| Empty cached category page | Populated shelf page 1 is used when available |
| Download source timeout | Book page remains usable; Download options show retry state |
| Cover source timeout | Stable placeholder remains; other content is unaffected |
| Browser navigation fetch fails | Normal document navigation takes over |
| App restarts during Kindle job | Job becomes interrupted with a retryable error |
| Kindle status connection drops | Delivery id is retained; Check status safely resumes polling |
| SMTP validation fails | Delivery stops before the attachment is sent |

## Tuning

The safest tuning order is:

1. Keep the default timeouts and inspect `Server-Timing`.
2. Confirm the SQLite and cover directories are writable.
3. Confirm cached responses are producing cover `HIT` headers.
4. Increase the Open Library interval if upstream rate limits appear.
5. Only increase read timeout when real successful responses regularly exceed
   eight seconds; longer timeouts also make cold failures feel slower.

Do not compensate for a blocked upstream by increasing concurrency. LibFlix is
designed to serve stale local data while the source recovers.

## Verification

Run the backend suite:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile app.py downloaders/base.py downloaders/libgen.py
git diff --check
```

Run UI checks with headless isolated Chromium. Verify:

1. Navigate Home -> category -> book -> Back without replacing the navbar.
2. Scroll a category, hover a later card, and confirm Quick Look stays by the
   cursor.
3. Confirm desktop and 390 px mobile layouts have no horizontal overflow.
4. Request the same cover twice and confirm `MISS` then `HIT`.
5. Delay a route response and confirm the shared loader appears after 180 ms.
6. Confirm a fast route does not flash the loader.
7. Start a mocked Kindle job, navigate away and back, and confirm progress
   resumes through completion.
8. Disable the download source and confirm the book page remains usable.

## Deployment Boundary

All changes in this performance pass are contained in the repository:

- Python application and downloader code;
- HTML templates;
- local CSS and JavaScript;
- Python dependencies;
- tests and documentation.

Deploy the code with the existing workflow. No DNS, Cloudflare Worker, cache
rule, transform rule, tunnel, or Cloudflare setting is part of the change.
