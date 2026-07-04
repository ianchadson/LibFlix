import re, os, json, html as htmlmod, warnings, time, random, threading, hashlib
from urllib.parse import urljoin, quote, urlencode
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, Response, stream_with_context, jsonify

# Modular download source — see ``downloaders/`` package.
from downloaders import DOWNLOADER
from downloaders.base import Book, SESSION as DL_SESSION
from downloaders.libgen import MIRROR

warnings.filterwarnings("ignore", category=requests.packages.urllib3.exceptions.InsecureRequestWarning)

OL = "https://openlibrary.org"
CACHE = {}
CACHE_TTL_OL = 3600
API_DISK_CACHE_TTL = 21600
COVER_CHECK_TTL = 604800
SHELF_DISK_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shelf_cache.json")
API_DISK_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_cache.json")

BOOK_SOURCES = {"openlibrary", "google"}

def normalize_book_source(source):
    source = (source or "").strip().lower()
    return source if source in BOOK_SOURCES else None

DEFAULT_BOOK_SOURCE = normalize_book_source(os.environ.get("BOOK_SOURCE")) or "openlibrary"
GOOGLE_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")

def get_book_source():
    return (
        normalize_book_source(request.args.get("source"))
        or normalize_book_source(request.cookies.get("book_source"))
        or DEFAULT_BOOK_SOURCE
    )

def source_url(source):
    args = request.args.to_dict(flat=True)
    args["source"] = source
    query = urlencode(args)
    return request.path + (f"?{query}" if query else "")

SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=20))
SESSION.mount("http://", HTTPAdapter(pool_connections=10, pool_maxsize=20))
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
DISK_CACHE_LOCK = threading.Lock()

def disk_cache_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

def disk_cache_get(key, ttl=API_DISK_CACHE_TTL):
    cache_key = disk_cache_key(key)
    try:
        with DISK_CACHE_LOCK:
            with open(API_DISK_CACHE, "r") as f:
                data = json.load(f)
        item = data.get(cache_key)
        if item and time.time() - item.get("t", 0) < ttl:
            return item.get("d")
    except:
        pass
    return None

def disk_cache_set(key, data):
    cache_key = disk_cache_key(key)
    try:
        with DISK_CACHE_LOCK:
            try:
                with open(API_DISK_CACHE, "r") as f:
                    cache = json.load(f)
            except:
                cache = {}
            cache[cache_key] = {"t": time.time(), "d": data}
            tmp = API_DISK_CACHE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cache, f)
            os.replace(tmp, API_DISK_CACHE)
    except:
        pass

def cache_get(key, ttl=CACHE_TTL_OL):
    v = CACHE.get(key)
    if v and time.time() - v["t"] < ttl:
        return v["d"]
    return None

def cache_set(key, data):
    CACHE[key] = {"d": data, "t": time.time()}

def ol_get(path, params=None):
    key = f"ol:{path}:{str(params)}"
    cached = cache_get(key, CACHE_TTL_OL)
    if cached:
        return cached
    cached = disk_cache_get(key)
    if cached:
        cache_set(key, cached)
        return cached
    try:
        r = SESSION.get(f"{OL}{path}", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        cache_set(key, data)
        disk_cache_set(key, data)
        return data
    except:
        return None

def ol_get_work(ol_key):
    return ol_get(ol_key + ".json")

SHELVES_DEF = [
    ("New & Popular", "trending"),
    ("Personal Development", "self_help"),
    ("Business & Finance", "business"),
    ("Science & Technology", "technology"),
    ("Psychology & Philosophy", "psychology"),
    ("History", "history"),
    ("Biography & Memoir", "biography"),
    ("Health & Wellness", "health"),
    ("Education & Reference", "education"),
    ("Politics & Society", "politics"),
    ("Non-Fiction Classics", "classics"),
    ("Award-Winning Non-Fiction", "award"),
]

FICTION_SHELVES_DEF = [
    ("New Releases", "trending_fiction"),
    ("Science Fiction", "science_fiction"),
    ("Fantasy", "fantasy"),
    ("Mystery & Thriller", "mystery"),
    ("Romance", "romance"),
    ("Horror", "horror"),
    ("Historical Fiction", "historical_fiction"),
    ("Adventure", "adventure"),
    ("Young Adult", "young_adult"),
    ("Graphic Novels", "graphic_novels"),
    ("Literary Fiction", "literary_fiction"),
    ("Contemporary Fiction", "contemporary_fiction"),
]

def get_shelves_def(mode="nonfiction"):
    return FICTION_SHELVES_DEF if mode == "fiction" else SHELVES_DEF

FICTION_TOPICS = {topic for _, topic in FICTION_SHELVES_DEF}

def shelf_query(topic):
    if topic == "trending":
        return "subject:Nonfiction -subject:Fiction", "rating"
    if topic == "trending_fiction":
        return "subject:Fiction", "rating"
    if topic in FICTION_TOPICS:
        return f"subject:{topic.replace('_', ' ')} subject:Fiction", "rating"
    return f"subject:{topic.replace('_', ' ')} -subject:Fiction", "rating"

def gb_shelf_query(topic):
    if topic == "trending":
        return "subject:Nonfiction", "newest"
    if topic == "trending_fiction":
        return "subject:Fiction", "newest"
    return f"subject:{topic.replace('_', ' ')}", "relevance"

def gb_search_shelf_books(q, order, want=25):
    books = []
    seen = set()
    for start_index in (0, 40, 80):
        data = gb_get("/volumes", {"q": q, "orderBy": order, "maxResults": 40, "startIndex": start_index})
        for item in (data or {}).get("items", []):
            b = gb_extract_book(item)
            if not b:
                continue
            key = (normalize_title(b["title"]), normalize_author(b.get("author", "")))
            if key in seen:
                continue
            seen.add(key)
            books.append(b)
        verified = verify_covers(books)
        if len(verified) >= want:
            return verified[:want]
    return verify_covers(books)[:want]

def is_english_title(title):
    return bool(re.match(r'^[\x20-\x7E\s\-\'.,!?;:()"&]+$', title))

_ENGLISH_WORDS = frozenset(
    "the is a an of to in and that this with for on as by from or but not was has "
    "have are be been his her their its which who when where what how why will would "
    "can could should about into over under after before between among through during "
    "while also more most some such only own than then there here one two first new "
    "story book author memoir life world history man woman people time year".split()
)

def is_english_text(text, threshold=4):
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if len(words) < 4:
        return False
    hits = sum(1 for w in words if w in _ENGLISH_WORDS)
    return hits >= threshold

def extract_book(w):
    title = w.get("title", "")
    if not title or not is_english_title(title):
        return None
    cover_id = w.get("cover_i") or w.get("cover_id")
    if not cover_id:
        return None
    author = ""
    authors = w.get("author_name") or w.get("authors", [])
    if isinstance(authors, list):
        for a in authors:
            if isinstance(a, dict):
                author = a.get("name", "")
            else:
                author = a
            break
    if not author:
        return None
    cover_url = f"/olcover/{cover_id}"
    ol_key = w.get("key", "")
    return {"title": title, "author": author, "cover_url": cover_url, "ol_key": ol_key}

def gb_get(path, params=None):
    if not GOOGLE_API_KEY:
        return None
    key = f"gb:{path}:{str(params)}"
    cached = cache_get(key, CACHE_TTL_OL)
    if cached:
        return cached
    cached = disk_cache_get(key)
    if cached:
        cache_set(key, cached)
        return cached
    try:
        p = dict(params or {})
        p["key"] = GOOGLE_API_KEY
        r = SESSION.get(f"https://www.googleapis.com/books/v1{path}", params=p, timeout=15)
        r.raise_for_status()
        data = r.json()
        cache_set(key, data)
        disk_cache_set(key, data)
        return data
    except:
        return None

def gb_extract_book(item):
    v = item.get("volumeInfo", {})
    title = v.get("title", "")
    if not title or not is_english_title(title):
        return None
    authors = v.get("authors", [])
    author = authors[0] if authors else ""
    if not author:
        return None
    images = v.get("imageLinks", {})
    thumb = (images.get("thumbnail") or images.get("smallThumbnail") or "")
    if not thumb:
        return None
    gb_id = item.get("id", "")
    cover_url = thumb.replace("http://", "https://").replace("zoom=1", "zoom=3").split("&edge=curl")[0].rstrip("&")
    return {"title": title, "author": author, "cover_url": cover_url, "ol_key": gb_id}

def verify_covers(books):
    """Remove books whose cover is a Google placeholder (< 5KB). Runs HEAD requests in parallel."""
    def check(b):
        gid = b.get("ol_key", "")
        if not gid:
            return None
        cache_key = f"gbcover-check:{gid}:zoom3"
        cached = cache_get(cache_key, COVER_CHECK_TTL)
        if cached is None:
            cached = disk_cache_get(cache_key, COVER_CHECK_TTL)
            if cached is not None:
                cache_set(cache_key, cached)
        if cached is not None:
            return b if cached else None
        try:
            h = SESSION.head(
                f"https://books.google.com/books/content?id={gid}&printsec=frontcover&img=1&zoom=3&source=gbs_api",
                timeout=3,
            )
            cl = int(h.headers.get("content-length", 0))
            ok = h.status_code == 200 and cl >= 10000
            cache_set(cache_key, ok)
            disk_cache_set(cache_key, ok)
            return b if ok else None
        except:
            return None
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(check, books))
    return [b for b in results if b is not None]

def fetch_one_shelf(name, topic, source=None):
    source = source or DEFAULT_BOOK_SOURCE
    try:
        if source == "google":
            q, order = gb_shelf_query(topic)
            books = gb_search_shelf_books(q, order, want=25)
            return {"name": name, "topic": topic, "books": books}

        q, sort = shelf_query(topic)
        params = {"q": q, "sort": sort, "limit": 50}
        data = ol_get("/search.json", params)
        works = (data or {}).get("docs", [])[:50]
        books = []
        for w in works:
            b = extract_book(w)
            if b is not None:
                books.append(b)
                if len(books) >= 20:
                    break
        return {"name": name, "topic": topic, "books": books}
    except:
        return {"name": name, "topic": topic, "books": []}

def fetch_category_books(topic, page=1, source=None):
    source = source or DEFAULT_BOOK_SOURCE
    if source == "google":
        q, order = gb_shelf_query(topic)
        start_index = (page - 1) * 40
        data = gb_get("/volumes", {"q": q, "orderBy": order, "maxResults": 40, "startIndex": start_index})
        total = data.get("totalItems", 0) if data else 0
        books = []
        for item in (data or {}).get("items", [])[:40]:
            b = gb_extract_book(item)
            if b:
                books.append(b)
        books = verify_covers(books)[:30]
        total_pages = min(25, max(1, (total + 39) // 40))
        return books, total, total_pages

    q, sort = shelf_query(topic)
    params = {"q": q, "sort": sort, "limit": 30, "page": page}
    data = ol_get("/search.json", params)
    total = data.get("numFound", 0) if data else 0
    books = []
    for w in (data or {}).get("docs", [])[:30]:
        b = extract_book(w)
        if b:
            books.append(b)
            if len(books) >= 20:
                break
    total_pages = min(25, max(1, (total + 29) // 30))
    return books, total, total_pages

def fetch_shelves(mode="nonfiction", source=None):
    source = source or DEFAULT_BOOK_SOURCE
    sd = get_shelves_def(mode)
    shelves = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_one_shelf, name, topic, source): name for name, topic in sd}
        for f in as_completed(futures):
            shelves.append(f.result())
    shelves.sort(key=lambda s: [n for n, _ in sd].index(s["name"]))
    return shelves

# ---------------------------------------------------------------------------
# Download source
# ---------------------------------------------------------------------------
# All libgen.li-specific code (search, parse, resolve, cover) now lives in
# ``downloaders/libgen.py``.  The Flask routes below call ``DOWNLOADER`` so
# changing the source is a one-line change in ``downloaders/__init__.py``.
DOWNLOADER  # noqa: F821 — imported at top of file

def normalize_title(title):
    t = title.lower().strip()
    t = re.sub(r'[;,.:!?()\[\]{}"\'/\\]', ' ', t)
    t = re.sub(r'\d+(st|nd|rd|th)\s*(ed|edition|edn)?', ' ', t)
    t = re.sub(r'\d+\s*\.?\s*ed(ition)?\b', ' ', t)
    t = re.sub(r'\bfirst\s+edition\b', ' ', t)
    t = re.sub(r'\b(reprint|paperback|hardcover|hardback|paperbound)\b', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:60]

def normalize_author(author):
    a = author.lower().strip()
    a = re.sub(r'[^\w\s]', ' ', a)
    a = a.split(';')[0]
    words = sorted(w for w in a.split() if len(w) > 2 and not w.isdigit())
    return ' '.join(words)[:60]

def parse_size_bytes(size_str):
    m = re.match(r'([\d.]+)\s*(KB|MB|GB|B)', size_str.strip().upper())
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "B": return val
    if unit == "KB": return val * 1024
    if unit == "MB": return val * 1024 * 1024
    if unit == "GB": return val * 1024 * 1024 * 1024
    return val

def book_score(book):
    score = 0
    fmt_scores = {"epub": 50, "pdf": 20, "mobi": 20, "azw3": 20, "djvu": 10, "chm": 5, "txt": 3}
    score += fmt_scores.get(book.ext.lower(), 0)
    score += 20 if book.language.lower() == "english" else 0
    try:
        y = int(book.year)
        if 1900 <= y <= 2030:
            score += y - 1900
    except:
        pass
    bytes_val = parse_size_bytes(book.size)
    if book.ext.lower() == "epub" and (bytes_val < 50000 or bytes_val > 200 * 1024 * 1024):
        score -= 10
    elif book.ext.lower() == "pdf" and (bytes_val < 100000 or bytes_val > 500 * 1024 * 1024):
        score -= 10
    else:
        score += 5
    if book.publisher.strip():
        score += 5
    try:
        if int(book.pages) > 0:
            score += 5
    except:
        pass
    if getattr(book, "cover_dir", ""):
        score += 3
    return score

def dedup(books):
    groups = {}
    for b in books:
        key = (normalize_title(b.title), normalize_author(b.author))
        groups.setdefault(key, []).append(b)
    best = []
    for key, group in groups.items():
        group.sort(key=book_score, reverse=True)
        best.append(group[0])
    return best

def strip_html(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = htmlmod.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()

def extract_desc(work):
    desc = work.get("description", "")
    if isinstance(desc, dict):
        desc = desc.get("value", "")
    return strip_html(desc)

app = Flask(__name__)

@app.after_request
def no_cache(resp):
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    resp.set_cookie("book_source", get_book_source(), max_age=31536000, samesite="Lax")
    return resp

@app.context_processor
def inject_book_source():
    return {
        "book_source": get_book_source(),
        "source_url": source_url,
    }

@app.template_filter("size_url")
def size_url(url, size="M"):
    if not url:
        return url
    zoom = {"S": "1", "M": "3", "L": "5"}.get(size, "3")
    if url.startswith("/"):
        return f"{url.rstrip('/')}/{size}"
    if "zoom=" in url:
        return re.sub(r'zoom=\d+', f'zoom={zoom}', url)
    return url

SORT_OPTIONS = {
    "y": "Year", "id": "ID", "title": "Title",
    "author": "Author", "filesize": "Size", "extension": "Extension",
    "time_added": "Date Added"
}

def disk_load_shelves(mode="nonfiction", source=None):
    source = source or DEFAULT_BOOK_SOURCE
    path = SHELF_DISK_CACHE.replace(".json", f"_{source}_{mode}.json")
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if data and isinstance(data, list) and len(data) > 0:
            return data
    except:
        pass
    return None

def disk_save_shelves(shelves, mode="nonfiction", source=None):
    source = source or DEFAULT_BOOK_SOURCE
    path = SHELF_DISK_CACHE.replace(".json", f"_{source}_{mode}.json")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(shelves, f)
        os.replace(tmp, path)
    except:
        pass

def get_shelves(mode="nonfiction", source=None):
    source = source or DEFAULT_BOOK_SOURCE
    ckey = f"shelves_{source}_{mode}"
    cached = cache_get(ckey, CACHE_TTL_OL)
    if cached:
        return cached
    disk = disk_load_shelves(mode, source)
    if disk:
        cache_set(ckey, disk)
        return disk
    shelves = fetch_shelves(mode, source)
    cache_set(ckey, shelves)
    disk_save_shelves(shelves, mode, source)
    return shelves

def dedup_across_shelves(shelves):
    seen = set()
    for shelf in shelves:
        deduped = []
        for book in shelf["books"]:
            key = (normalize_title(book["title"]), normalize_author(book.get("author", "")))
            if key not in seen:
                seen.add(key)
                deduped.append(book)
        shelf["books"] = deduped
        if "topic" not in shelf:
            shelf["topic"] = next((topic for name, topic in SHELVES_DEF + FICTION_SHELVES_DEF if name == shelf.get("name")), "")
    return shelves

@app.route("/")
def index():
    mode = request.args.get("mode", "nonfiction")
    if mode not in ("fiction", "nonfiction"):
        mode = "nonfiction"
    source = get_book_source()
    shelves = get_shelves(mode, source)
    shelves = dedup_across_shelves(shelves) if shelves else shelves
    hero = None
    if shelves:
        trending = shelves[0].get("books", [])
        if trending:
            candidates = random.sample(trending, min(len(trending), 8))
            for b in candidates:
                hero = dict(b)
                hero["description"] = ""
                if hero.get("ol_key"):
                    if source == "google":
                        data = gb_get(f"/volumes/{hero['ol_key']}")
                        if data:
                            v = data.get("volumeInfo", {})
                            desc = v.get("description", "")
                            if isinstance(desc, dict):
                                desc = desc.get("value", "")
                            desc = strip_html(desc)
                            if desc and is_english_text(desc):
                                hero["description"] = desc[:300]
                                break
                    else:
                        work = ol_get_work(hero["ol_key"])
                        if work:
                            desc = extract_desc(work)
                            if desc and is_english_text(desc):
                                hero["description"] = desc[:300]
                                break
    return render_template("index.html", shelves=shelves, hero=hero, mode=mode)

@app.route("/category/<topic>")
def category_page(topic):
    mode = request.args.get("mode", "nonfiction")
    source = get_book_source()
    sd = get_shelves_def(mode)
    valid_topics = {t for _, t in sd}
    if topic not in valid_topics:
        return render_template("category.html", shelf={"name": topic.capitalize(), "books": []}, topic=topic, mode=mode)
    name = {t: n for n, t in sd}.get(topic, topic.capitalize())
    shelf = fetch_one_shelf(name, topic, source)
    return render_template("category.html", shelf=shelf, topic=topic, mode=mode)

@app.route("/api/category/<topic>")
def api_category(topic):
    page = int(request.args.get("page", 1))
    mode = request.args.get("mode", "nonfiction")
    source = get_book_source()
    sd = get_shelves_def(mode)
    valid_topics = {t for _, t in sd}
    if topic not in valid_topics:
        return jsonify({"success": False, "error": "Invalid topic"})

    books, total, total_pages = fetch_category_books(topic, page, source)
    return jsonify({
        "success": True, "books": books,
        "page": page, "total_pages": total_pages, "total": total,
    })

@app.route("/api/shelf/<topic>")
def api_shelf(topic):
    page = int(request.args.get("page", 1))
    mode = request.args.get("mode", "nonfiction")
    source = get_book_source()
    sd = get_shelves_def(mode)
    valid_topics = {t for _, t in sd}
    if topic not in valid_topics:
        return jsonify({"success": False, "error": "Invalid topic"})
    books, total, total_pages = fetch_category_books(topic, page, source)
    return jsonify({"success": True, "books": books, "page": page, "total_pages": total_pages, "total": total})

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    mode = request.args.get("mode", "nonfiction")
    if mode not in ("fiction", "nonfiction"):
        mode = "nonfiction"
    source = get_book_source()
    if not q:
        shelves = get_shelves(mode, source)
        return render_template("index.html", shelves=shelves, error="Enter a search query.", mode=mode)
    sort = request.args.get("sort", "y")
    order = request.args.get("order", "DESC").upper()
    limit = int(request.args.get("limit", 25))
    page = int(request.args.get("page", 1))
    fmt = request.args.get("format", "epub")
    lang = request.args.get("lang", "English")
    dedup_on = request.args.get("dedup", "1") == "1"
    return render_template("search.html",
        query=q, sort=sort, order=order, limit=limit,
        page=page, fmt=fmt, lang=lang, dedup_on=dedup_on,
        sort_options=SORT_OPTIONS, mode=mode)

@app.route("/preview")
def preview():
    title = request.args.get("title", "").strip()
    author = request.args.get("author", "").strip()
    ol_key = request.args.get("ol_key", "").strip()
    cover_url = request.args.get("cover", "").strip()
    mode = request.args.get("mode", "nonfiction")
    return render_template("book.html",
        title=title, author=author, cover_url=cover_url,
        ol_key=ol_key, mode=mode)

@app.route("/api/similar")
def api_similar():
    subject = request.args.get("subject", "").strip()
    ol_key = request.args.get("ol_key", "").strip()
    source = get_book_source()
    if not subject:
        return jsonify({"success": False, "error": "No subject"})

    if source == "google":
        data = gb_get("/volumes", {"q": f"subject:{subject}", "orderBy": "relevance", "maxResults": 12})
        items = (data or {}).get("items", [])[:12]
        books = []
        for item in items:
            b = gb_extract_book(item)
            if b and b["ol_key"] != ol_key:
                books.append(b)
        return jsonify({"success": True, "books": books})

    data = ol_get("/search.json", {"subject": subject, "sort": "rating", "limit": 12})
    docs = (data or {}).get("docs", [])
    books = []
    for w in docs:
        b = extract_book(w)
        if b and b["ol_key"] != ol_key:
            books.append(b)
    return jsonify({"success": True, "books": books})

@app.route("/api/book")
def api_book():
    ol_key = request.args.get("ol_key", "").strip()
    source = get_book_source()
    if not ol_key:
        return jsonify({"success": False, "error": "No ol_key provided"})

    if ol_key.startswith("/works/"):
        source = "openlibrary"
    elif source == "openlibrary":
        source = "google"

    if source == "google":
        data = gb_get(f"/volumes/{ol_key}")
        if not data:
            return jsonify({"success": False, "error": "Book not found"})
        v = data.get("volumeInfo", {})
        desc = v.get("description", "")
        if isinstance(desc, dict):
            desc = desc.get("value", "")
        desc = strip_html(desc)
        categories = v.get("categories", [])[:12]
        return jsonify({
            "success": True,
            "title": v.get("title", ""),
            "description": desc,
            "subjects": categories,
        })

    work = ol_get_work(ol_key)
    if not work:
        return jsonify({"success": False, "error": "Book not found"})
    description = extract_desc(work)
    subjects = work.get("subjects", [])[:20]
    return jsonify({
        "success": True,
        "title": work.get("title", ""),
        "description": description,
        "subjects": subjects,
    })

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"success": False, "error": "No query provided"})
    sort = request.args.get("sort", "y")
    order = request.args.get("order", "DESC").upper()
    limit = int(request.args.get("limit", 25))
    page = int(request.args.get("page", 1))
    fmt = request.args.get("format", "epub")
    lang = request.args.get("lang", "English")
    dedup_on = request.args.get("dedup", "1") == "1"

    sort_field = "y" if sort == "year" else sort
    try:
        books, total = DOWNLOADER.search(q, sort=sort_field, order=order, page=page, limit=limit)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

    lang_filter = None if lang == "all" else lang
    fmt_filter = None if fmt == "all" else fmt
    filtered = []
    for b in books:
        if lang_filter and b.language.lower() != lang_filter.lower():
            continue
        if fmt_filter and b.ext.lower() != fmt_filter.lower():
            continue
        filtered.append(b)
    books = filtered
    if dedup_on:
        books = dedup(books)

    total_pages = (total + limit - 1) // limit if total else 1
    result_books = []
    for i, b in enumerate(books):
        d = b.to_dict(i + 1 + (page - 1) * limit)
        cover_dir = getattr(b, "cover_dir", "")
        d["cover_url"] = f"/cover/{d['md5']}?dir={cover_dir}" if cover_dir and d['md5'] else ""
        result_books.append(d)
    return jsonify({
        "success": True,
        "query": q,
        "books": result_books,
        "total": total,
        "total_pages": total_pages,
        "page": page,
        "sort": sort,
        "order": order,
        "limit": limit,
        "format": fmt,
        "lang": lang,
        "dedup_on": dedup_on,
    })

@app.route("/download/<md5>")
def download(md5):
    url = DOWNLOADER.resolve_download(md5)
    filename = request.args.get("filename", f"{md5}.epub")
    def generate():
        try:
            r = SESSION.get(url, stream=True, timeout=120, allow_redirects=True)
            r.raise_for_status()
            yield from r.iter_content(chunk_size=65536)
        except:
            yield b""
    resp = Response(stream_with_context(generate()),
                    mimetype="application/octet-stream")
    resp.headers["Content-Disposition"] = f"attachment; filename=\"{filename}\""
    return resp

@app.route("/cover/<md5>")
def cover(md5):
    cover_dir = request.args.get("dir", "")
    if not cover_dir:
        return "", 404
    url = f"{MIRROR}/covers/{cover_dir}/{md5}.jpg"
    try:
        r = SESSION.get(url, timeout=15, headers={"Referer": f"{MIRROR}/"})
        if r.status_code == 200 and len(r.content) > 100:
            resp = Response(r.content, mimetype=r.headers.get("content-type", "image/jpeg"))
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
    except:
        pass
    return "", 404

@app.route("/olcover/<int:cover_id>")
@app.route("/olcover/<int:cover_id>/<size>")
def olcover(cover_id, size="M"):
    s = size.upper() if size in ("S", "M", "L") else "M"
    url = f"https://covers.openlibrary.org/b/id/{cover_id}-{s}.jpg"
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code == 200 and len(r.content) > 100:
            resp = Response(r.content, mimetype=r.headers.get("content-type", "image/jpeg"))
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
    except:
        pass
    return "", 404

@app.route("/gbcover/<gb_id>")
@app.route("/gbcover/<gb_id>/<size>")
def gbcover(gb_id, size="M"):
    zooms = {"S": ["1", "2"], "M": ["3", "5", "2"], "L": ["5", "3"]}
    for zoom in zooms.get(size.upper(), ["3", "5"]):
        url = f"https://books.google.com/books/content?id={gb_id}&printsec=frontcover&img=1&zoom={zoom}&source=libflix"
        try:
            r = SESSION.get(url, timeout=10, headers={"Referer": "https://books.google.com/"})
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code == 200 and "image" in ct and len(r.content) > 10000:
                resp = Response(r.content, mimetype=ct)
                resp.headers["Cache-Control"] = "public, max-age=86400"
                return resp
        except:
            pass
    resp = Response("", status=404, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

@app.route("/api/sendtokindle", methods=["POST"])
def api_sendtokindle():
    import smtplib, tempfile
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email.mime.text import MIMEText
    from email import encoders

    data = request.get_json(silent=True) or {}
    md5 = data.get("md5", "")
    title = data.get("title", "book")
    ext = data.get("ext", "epub")
    kindle_email = data.get("kindle_email", "").strip()
    smtp_host = data.get("smtp_host", "").strip()
    smtp_port = int(data.get("smtp_port", 587))
    smtp_user = data.get("smtp_user", "").strip()
    smtp_pass = data.get("smtp_pass", "")
    sender_email = data.get("sender_email", smtp_user)

    if not all([md5, kindle_email, smtp_host, smtp_user, smtp_pass]):
        return jsonify({"success": False, "error": "Missing required fields"}), 400

    dl_url = DOWNLOADER.resolve_download(md5)
    try:
        r = SESSION.get(dl_url, stream=True, timeout=120, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        return jsonify({"success": False, "error": f"Download failed: {e}"})

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        for chunk in r.iter_content(chunk_size=65536):
            tmp.write(chunk)
        tmp_path = tmp.name

    try:
        msg = MIMEMultipart()
        msg["From"] = sender_email
        msg["To"] = kindle_email
        msg["Subject"] = f"Sent by LibFlix: {title}"

        body = MIMEText(f"Book sent from LibFlix.\n\nTitle: {title}\nFormat: {ext}")
        msg.attach(body)

        with open(tmp_path, "rb") as f:
            attachment = MIMEBase("application", "octet-stream")
            attachment.set_payload(f.read())
            encoders.encode_base64(attachment)
            attachment.add_header("Content-Disposition", f"attachment; filename=\"{title[:80]}.{ext}\"")
            msg.attach(attachment)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        os.unlink(tmp_path)
        return jsonify({"success": True})
    except smtplib.SMTPAuthenticationError:
        os.unlink(tmp_path)
        return jsonify({"success": False, "error": "SMTP auth failed. For Gmail, use an App Password."})
    except Exception as e:
        os.unlink(tmp_path)
        return jsonify({"success": False, "error": str(e)})

def warm_cache():
    source = DEFAULT_BOOK_SOURCE
    for mode in ("nonfiction", "fiction"):
        disk = disk_load_shelves(mode, source)
        if disk:
            cache_set(f"shelves_{source}_{mode}", disk)
            print(f"Loaded {len(disk)} {source} {mode} shelves from disk cache (instant)", flush=True)
    print(f"Warming cache: fetching fresh {source} shelves...", flush=True)
    t0 = time.time()
    for mode in ("nonfiction", "fiction"):
        shelves = fetch_shelves(mode, source)
        cache_set(f"shelves_{source}_{mode}", shelves)
        disk_save_shelves(shelves, mode, source)
        print(f"  {mode}: {len(shelves)} shelves", flush=True)
    print(f"Cache warmed in {time.time()-t0:.1f}s", flush=True)

if __name__ == "__main__":
    threading.Thread(target=warm_cache, daemon=True).start()
    app.run(host="0.0.0.0", port=5800, debug=True, use_reloader=False)
