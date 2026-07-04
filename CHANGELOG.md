# Changelog

## 2026-07-04 - Open Library-only discovery and browsing UX refresh

### Removed Google Books discovery

- Removed Google Books API functionality from the Flask backend.
- Removed the Google/Open Library source toggle from the navbar.
- Removed app-generated `source=` query parameters.
- Removed Google cover fallback/proxy behavior.
- Removed Google-specific cache and cover validation code paths.
- Old URLs that still contain `source=google` are tolerated but ignored.

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
