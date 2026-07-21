# Changelog

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
