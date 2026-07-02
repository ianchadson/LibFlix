# LibFlix — Non-Fiction Library

A "Netflix for books" web app that combines **Open Library** for discovery and
**libgen.li** for downloads. Curated for non-fiction: self-development,
business, science, history, and more.

## Quick Start

```bash
pip install -r requirements.txt
python3 app.py
# → http://localhost:5800
```

## Features

- **12 non-fiction shelves** — Trending, Personal Development, Business, Science, Psychology, History, Biography, Health, Education, Politics, Classics, Awards
- **Dynamic hero** — random featured book on each visit
- **Instant search** — page shell renders immediately, results load via AJAX
- **One-click download** — streams EPUB/PDF/MOBI from libgen through Flask proxy
- **Send to Kindle** — email books directly to your Kindle via SMTP (Gmail app password)
- **Dedup** — groups editions by normalized title+author, keeps highest-quality format
- **Previews** — description from Open Library, similar books by subject
- **Filters** — sort by year/title/size, filter by format/language, toggle dedup

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed data flows, API reference, and caching strategy.

## Tech Stack

- **Backend:** Flask, BeautifulSoup4, requests
- **Frontend:** Bootstrap 5, vanilla JS (no framework)
- **APIs:** Open Library (free, no key), libgen.li (scraped HTML)
- **Port:** 5800 (avoids macOS AirPlay conflict on 5000)

## Configuration

No API keys needed. Send to Kindle requires a Gmail app password and your
Kindle email (configured in-browser — stored in localStorage).
