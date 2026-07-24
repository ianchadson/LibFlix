# LibFlix - Book Discovery Library

LibFlix is a Netflix-style web app for browsing books, previewing metadata, and
finding download options. Discovery is powered by Open Library. Downloads are
handled separately through the modular downloader layer, currently backed by
libgen.li.

The app supports fiction and non-fiction browsing, English and Chinese discovery
filters, Open Library search, book previews, similar-book shelves, inline
download search, direct downloads, and Send to Kindle via the user's SMTP
settings.

## About

LibFlix is a local-first book discovery interface for browsing public Open
Library metadata with a polished streaming-app style UI. It focuses on fast
category browsing, clean book previews, contextual download lookup, and a
low-friction path from discovery to Send to Kindle.

## Quick Start

```bash
pip install -r requirements.txt
python3 app.py

# App URL:
# http://127.0.0.1:5800
```

No API key is required. Open Library is the only discovery backend.

## Screenshots

### Homepage

![LibFlix homepage with fixed hero and trending shelf](screenshots/readme-home.png)

### Search and Settings

![Expanded search and browse settings controls](screenshots/readme-controls.png)

### Quick Peek

![Hover quick peek over a book card](screenshots/readme-quick-peek.png)

## Current Feature Set

### Discovery

- **Open Library discovery** - browsing, shelves, categories, metadata, covers,
  similar books, and discovery search all use Open Library with no API key.
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
- **Expandable discovery search** - the global search opens from a compact icon
  control, animates into a full search field, and routes to `/discover`. It does
  not jump directly to download search.
- **Download search is contextual** - download options are searched from the book
  preview page using the selected title and author.

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
- **App-style route transitions** - internal page navigation fades through a
  lightweight LibFlix loading overlay instead of exposing a blank wait. Shared
  navigation and form handling use `window.LibFlixLoading.show()` when present.
- **Clean browsing URLs** - main home, mode, language, category, and discovery
  routes use paths like `/fiction`, `/cn/category/history`, and
  `/fiction/discover?q=dune` instead of exposing mode/language query args.
- **Trending naming** - the first shelf and first top-nav category are labeled
  `Trending` across fiction and non-fiction. Cached shelf labels are normalized
  at render time so older `New & Popular` cache files do not leak into the UI.
- **Progressively hydrated shelves** - the homepage sends stable shelf skeletons
  in the initial document, then loads a complete 40-book row as each shelf
  approaches the viewport. Rows never appear artificially short, while distant
  shelves add no initial card or image cost.
- **Shelf-order dedupe** - books shown in an earlier homepage shelf are removed
  from all later shelves. Later shelves are refilled from deeper Open Library
  pages where possible so rows stay useful without repeating entries.
- **Horizontal infinite scroll** - homepage shelves automatically load another
  page when the user scrolls near the end of a row.
- **Intent-aware navigation prefetch** - internal pages are prefetched only
  after deliberate pointer hover, keyboard focus, or touch intent. The app does
  not speculatively download every category after page load.
- **Instant book shells** - every rendered card registers its title, author,
  cover, and work key as a lightweight server hint. Opening that card can
  render the book page immediately while descriptions and secondary metadata
  hydrate independently.
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
- **Async Open Library details** - the description loads after the preview shell
  renders, strips source markup, and collapses behind `Read more` on smaller
  screens when needed.
- **More Like This** - the first subject loads a single-row horizontal
  similar-books shelf with the same hover quick peek used elsewhere. Its API
  request waits until the section approaches the viewport.
- **Hidden More Like This scrollbar** - the shelf scrolls horizontally without a
  visible scrollbar.
- **Inline edition picker** - download candidates appear as responsive edition
  rows with cover, title, author, publisher, format, year, size, pages, and
  language instead of a dense table.
- **Collapsible download filters** - format, sort, language, page size, and
  dedupe controls share one compact `Filters` panel across preview and direct
  download search pages. `Best match` is the explicit default; selecting year,
  recently added, file size, title, or author preserves that requested order.
- **Clear actions** - every available edition has explicit format-aware Download
  and Kindle actions. `Best match` is assigned only after globally ranking all
  filtered candidates by title and author similarity, language, reading format,
  file sanity, and metadata quality.
- **Send to Kindle settings** - the global Settings menu opens a keyboard-safe
  Kindle sheet with password visibility, local browser storage, a forget
  action, and a visible configured / configure-connection state.
- **Live Kindle delivery progress** - the selected edition expands to show a
  responsive progress bar, current delivery stage, transferred file size, and
  clear completion or failure state while LibFlix downloads and emails the file.

### Download Search

- **AJAX results** - download results update without a full page reload.
- **Responsive edition display** - direct search uses the same scannable edition
  rows and actions as the book preview, including two-line titles on mobile.
- **Filters** - sort, format, page size, language, and dedupe controls update the
  download search without exposing filter state in the URL.
- **Deduplication** - results can be grouped by normalized title and author,
  keeping the highest-scored candidate.
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
- **Single transition loader** - route changes use one shared LibFlix overlay;
  it never waits for covers or download-source results, while local AJAX
  loaders remain scoped to the content they are updating.
- **Progressive cover loading** - cover geometry stays stable while images load,
  with lightweight placeholders and animation reserved for high-priority areas.
- **Direct Open Library covers** - cover images load from Open Library's CDN
  with one preconnected high-resolution active cover and medium side-stack
  covers, avoiding an extra Flask proxy hop.
- **On-demand hero metadata** - only the active hero description is requested;
  later descriptions hydrate when their book becomes active.
- **Accessible interaction** - pages provide a skip link, named icon controls,
  visible keyboard focus, modal focus return, scroll locking, reduced-motion
  fallbacks, and non-selectable app chrome while content remains selectable.

## Main Routes

| Route | Purpose |
|---|---|
| `/` | Homepage with hero and horizontal shelves |
| `/category/<topic>` | Category grid with vertical infinite scroll |
| `/discover?q=...` | Open Library discovery search results |
| `/book/OL...W` | Book detail, similar books, download search |
| `/fiction/cn/book/OL...W` | Book detail with clean mode/language context |
| `/search?q=...` | Direct libgen download search page |
| `/download/<md5>` | Proxied file download |
| `/api/shelf/<topic>` | JSON endpoint for homepage shelf pagination |
| `/api/category/<topic>` | JSON endpoint for category infinite scroll |
| `/api/discover` | JSON endpoint for discovery search pagination |
| `/api/book` | JSON endpoint for Open Library work details |
| `/api/cn-display-title` | Cached English display-title lookup for CN browse cards |
| `/api/cn-display-titles` | Batched English display-title lookup for visible CN cards |
| `/api/similar` | JSON endpoint for similar Open Library books |
| `/api/search` | JSON endpoint for libgen download search |
| `/api/sendtokindle` | Send selected download to Kindle via SMTP |

## Configuration

| Env Variable | Values | Purpose |
|---|---|---|
| `BOOK_LANG` | `en` or `cn` | Optional default discovery language |

Runtime Send to Kindle settings are configured in the browser and stored in
localStorage. The SMTP password is sent only to `/api/sendtokindle` when sending
a book.

## Runtime Cache Files

The app writes local runtime cache files to speed up restart and repeated API
requests. They are ignored by git.

| File Pattern | Purpose |
|---|---|
| `api_cache.sqlite3` | WAL-enabled keyed cache for Open Library/API responses |
| `shelf_cache_<lang>_<mode>.json` | Warm shelf cache for each language and mode |
| `shelf_cache*.json` | Historical and current shelf cache files ignored by git |

Legacy `api_cache.json` data is migrated once into SQLite and removed after a
successful migration. Shelf files are loaded immediately at startup; stale
shelves refresh after a delay without blocking the first page.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for data flow, API contracts, caching
details, and template responsibilities.

See [CHANGELOG.md](CHANGELOG.md) for the recent Open Library-only discovery,
language, expandable toolbar, infinite scroll, and count-removal changes.

## Tech Stack

- **Backend:** Flask, requests, BeautifulSoup4
- **Frontend:** Local CSS and vanilla JavaScript
- **Discovery:** Open Library Search/Works/Covers APIs
- **Downloads:** Modular downloader interface, currently libgen.li
- **Port:** 5800

## Verification

Useful local checks:

```bash
python3 -m compileall app.py
python3 app.py
```

For UI validation, use headless Playwright unless an interactive visible browser
is explicitly needed. Use isolated Chromium contexts and test:

```text
http://127.0.0.1:5800
http://127.0.0.1:5800/category/history
http://127.0.0.1:5800/fiction/cn/discover?q=三体
http://127.0.0.1:5800/book/OL82563W
http://127.0.0.1:5800/search?q=Harry%20Potter
```
