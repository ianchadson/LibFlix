import re, os, json, html as htmlmod, warnings, time, random, threading, hashlib, sqlite3, unicodedata
from difflib import SequenceMatcher
from urllib.parse import urljoin, quote, urlencode
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, Response, stream_with_context, jsonify, g, redirect
from opencc import OpenCC

# Modular download source — see ``downloaders/`` package.
from downloaders import DOWNLOADER
from downloaders.base import Book, SESSION as DL_SESSION
from downloaders.libgen import MIRROR

warnings.filterwarnings("ignore", category=requests.packages.urllib3.exceptions.InsecureRequestWarning)

OL = "https://openlibrary.org"
CACHE = {}
CACHE_TTL_OL = 3600
API_DISK_CACHE_TTL = 21600
CHINESE_TITLE_CACHE_TTL = 2592000
SHELF_DISK_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shelf_cache.json")
API_DISK_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_cache.json")
API_SQLITE_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_cache.sqlite3")
SHELF_REFRESH_TTL = 21600
OL_BOOK_FIELDS = "key,title,author_name,cover_i,cover_id,language,editions,editions.title,editions.language,editions.covers,editions.cover_i,editions.cover_id"
SHELF_BOOK_TARGET = 40
SHELF_SEARCH_LIMIT = 100
SHELF_MAX_OPEN_LIBRARY_PAGES = 25
SHELF_REFILL_OPEN_LIBRARY_PAGES = 4

BOOK_LANGS = {"en", "cn"}
BOOK_LANG_CONFIG = {
    "en": {
        "label": "EN",
        "ol_lang": "eng",
    },
    "cn": {
        "label": "CN",
        "ol_lang": "chi",
    },
}
CHINESE_DOWNLOAD_TITLE_ALIASES = {
    "the big short": ["大空头"],
}
GENERIC_SIMILAR_SUBJECTS = {
    "action/adventure", "biography", "business", "competition", "contests",
    "fantasy", "fiction", "games", "history", "independence",
    "interdependence", "interpersonal relations", "juvenile fiction",
    "juvenile works", "open library staff picks", "personal narratives",
    "poverty", "psychology", "science", "self-help", "sisters", "survival",
    "teen fiction", "television programs",
}

def normalize_book_lang(lang):
    lang = (lang or "").strip().lower()
    aliases = {"zh": "cn", "chi": "cn", "chinese": "cn", "cn": "cn", "en": "en", "eng": "en", "english": "en"}
    return aliases.get(lang) if aliases.get(lang) in BOOK_LANGS else None

DEFAULT_BOOK_LANG = normalize_book_lang(os.environ.get("BOOK_LANG")) or "en"

def get_book_lang():
    override = getattr(g, "book_lang_override", None)
    if override:
        return override
    return (
        normalize_book_lang(request.args.get("book_lang"))
        or normalize_book_lang(request.cookies.get("book_lang"))
        or DEFAULT_BOOK_LANG
    )

def clean_prefix(mode=None, lang=None):
    mode = mode if mode in ("fiction", "nonfiction") else "nonfiction"
    lang = normalize_book_lang(lang) or get_book_lang()
    parts = []
    if mode == "fiction":
        parts.append("fiction")
    if lang == "cn":
        parts.append("cn")
    return "/" + "/".join(parts) if parts else ""

def clean_home_url(mode=None, lang=None):
    return clean_prefix(mode, lang) or "/"

def clean_category_url(topic, mode=None, lang=None):
    return f"{clean_prefix(mode, lang)}/category/{topic}"

def clean_discover_url(mode=None, lang=None):
    return f"{clean_prefix(mode, lang)}/discover"

def work_id_from_ol_key(ol_key):
    ol_key = (ol_key or "").strip()
    if ol_key.startswith("/works/"):
        return ol_key.rsplit("/", 1)[-1]
    if ol_key.startswith("works/"):
        return ol_key.rsplit("/", 1)[-1]
    if re.match(r"^OL\d+W$", ol_key):
        return ol_key
    return ""

def ol_key_from_work_id(work_id):
    work_id = (work_id or "").strip()
    if not re.match(r"^OL\d+W$", work_id):
        return ""
    return f"/works/{work_id}"

def clean_book_url(ol_key, mode=None, lang=None):
    work_id = work_id_from_ol_key(ol_key)
    if not work_id:
        return "/preview"
    return f"{clean_prefix(mode, lang)}/book/{quote(work_id)}"

def book_url(book, mode=None, lang=None):
    if not book:
        return "/preview"
    return clean_book_url(book.get("ol_key"), mode, lang)

def preserve_query_redirect(path, drop=("mode", "book_lang")):
    args = request.args.to_dict(flat=True)
    for key in drop:
        args.pop(key, None)
    query = urlencode(args)
    return redirect(path + (f"?{query}" if query else ""))

def lang_url(lang):
    mode = request.args.get("mode") if request.args.get("mode") in ("fiction", "nonfiction") else None
    mode = mode or getattr(g, "mode_override", None) or "nonfiction"
    endpoint = request.endpoint or ""
    topic = (request.view_args or {}).get("topic")
    if endpoint == "category_page" and topic:
        return clean_category_url(topic, mode, lang)
    if endpoint == "discover":
        path = clean_discover_url(mode, lang)
        args = request.args.to_dict(flat=True)
        args.pop("mode", None)
        args.pop("book_lang", None)
        query = urlencode(args)
        return path + (f"?{query}" if query else "")
    if endpoint == "book_page":
        work_id = (request.view_args or {}).get("work_id")
        if work_id:
            return clean_book_url(work_id, mode, lang)
    return clean_home_url(mode, lang)

SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=20))
SESSION.mount("http://", HTTPAdapter(pool_connections=10, pool_maxsize=20))
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
DISK_CACHE_LOCK = threading.Lock()
CHINESE_TITLE_LOOKUP_SEMAPHORE = threading.BoundedSemaphore(4)
SHELF_REFRESH_LOCK = threading.Lock()
SHELF_REFRESHING = set()
SQLITE_CACHE_READY = False
BOOK_HINTS = {}
BOOK_HINTS_LOCK = threading.Lock()
OPENCC_T2S = OpenCC("t2s")

def disk_cache_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

def initialize_disk_cache():
    global SQLITE_CACHE_READY
    if SQLITE_CACHE_READY:
        return
    with DISK_CACHE_LOCK:
        if SQLITE_CACHE_READY:
            return
        database_exists = os.path.exists(API_SQLITE_CACHE)
        migrated_legacy_cache = False
        with sqlite3.connect(API_SQLITE_CACHE, timeout=10) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS api_cache ("
                "cache_key TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS api_cache_created_at ON api_cache(created_at)"
            )
            if not database_exists and os.path.exists(API_DISK_CACHE):
                try:
                    with open(API_DISK_CACHE, "r") as legacy_file:
                        legacy = json.load(legacy_file)
                    rows = [
                        (cache_key, item.get("t", 0), json.dumps(item.get("d")))
                        for cache_key, item in legacy.items()
                        if isinstance(item, dict) and "d" in item
                    ]
                    connection.executemany(
                        "INSERT OR REPLACE INTO api_cache(cache_key, created_at, payload) VALUES (?, ?, ?)",
                        rows,
                    )
                    migrated_legacy_cache = bool(rows)
                except (OSError, ValueError, sqlite3.Error):
                    pass
            connection.execute(
                "DELETE FROM api_cache WHERE created_at < ?",
                (time.time() - CHINESE_TITLE_CACHE_TTL,),
            )
        if migrated_legacy_cache:
            try:
                os.unlink(API_DISK_CACHE)
            except OSError:
                pass
        SQLITE_CACHE_READY = True

def disk_cache_get(key, ttl=API_DISK_CACHE_TTL):
    cache_key = disk_cache_key(key)
    initialize_disk_cache()
    try:
        with sqlite3.connect(API_SQLITE_CACHE, timeout=5) as connection:
            row = connection.execute(
                "SELECT created_at, payload FROM api_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            if time.time() - row[0] >= ttl:
                connection.execute("DELETE FROM api_cache WHERE cache_key = ?", (cache_key,))
                return None
            return json.loads(row[1])
    except (sqlite3.Error, ValueError):
        pass
    return None

def disk_cache_set(key, data):
    cache_key = disk_cache_key(key)
    initialize_disk_cache()
    try:
        payload = json.dumps(data, separators=(",", ":"))
        with sqlite3.connect(API_SQLITE_CACHE, timeout=5) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO api_cache(cache_key, created_at, payload) VALUES (?, ?, ?)",
                (cache_key, time.time(), payload),
            )
    except (sqlite3.Error, TypeError, ValueError):
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
    if cached is not None:
        return cached
    cached = disk_cache_get(key)
    if cached is not None:
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
    ("Trending", "trending"),
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
    ("Trending", "trending_fiction"),
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

def shelf_query(topic, lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    lang_filter = f" language:{BOOK_LANG_CONFIG[lang]['ol_lang']}"
    if topic == "trending":
        return f"subject:Nonfiction -subject:Fiction{lang_filter}", "rating"
    if topic == "trending_fiction":
        return f"subject:Fiction{lang_filter}", "rating"
    if topic in FICTION_TOPICS:
        return f"subject:{topic.replace('_', ' ')} subject:Fiction{lang_filter}", "rating"
    return f"subject:{topic.replace('_', ' ')} -subject:Fiction{lang_filter}", "rating"

def is_english_title(title):
    return bool(re.match(r'^[\x20-\x7E\s\-\'.,!?;:()"&]+$', title))

def is_chinese_title(title):
    return bool(re.search(r'[\u3400-\u9fff]', title or ""))

def title_matches_lang(title, lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    if lang == "cn":
        return is_chinese_title(title)
    return is_english_title(title)

_ENGLISH_WORDS = frozenset(
    "the is a an of to in and that this with for on as by from or but not was has "
    "have are be been his her their its which who when where what how why will would "
    "can could should about into over under after before between among through during "
    "while also more most some such only own than then there here one two first new "
    "story book author memoir life world history man woman people time year".split()
)

def is_english_text(text, threshold=4, min_ratio=0.18):
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if len(words) < 4:
        return False
    hits = sum(1 for w in words if w in _ENGLISH_WORDS)
    return hits >= threshold and hits / len(words) >= min_ratio

def text_matches_lang(text, lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    if lang == "cn":
        return is_chinese_title(text)
    return is_english_text(text)

def record_has_lang(record, lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    ol_lang = BOOK_LANG_CONFIG[lang]["ol_lang"]
    languages = record.get("language") or []
    return ol_lang in languages

def first_matching_edition(w, lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    editions = (w.get("editions") or {}).get("docs", [])
    if lang == "cn":
        for ed in editions:
            if record_has_lang(ed, lang) and is_chinese_title(ed.get("title", "")):
                return ed
        for ed in editions:
            if is_chinese_title(ed.get("title", "")):
                return ed
    for ed in editions:
        if record_has_lang(ed, lang):
            return ed
    for ed in editions:
        title = ed.get("title", "")
        if title_matches_lang(title, lang):
            return ed
    return None

def edition_cover_id(ed):
    covers = ed.get("covers")
    if isinstance(covers, list) and covers:
        return covers[0]
    return ed.get("cover_i") or ed.get("cover_id")

def open_library_cover_url(cover_id, size="M"):
    if not cover_id:
        return ""
    size = size if size in ("S", "M", "L") else "M"
    return f"https://covers.openlibrary.org/b/id/{cover_id}-{size}.jpg"

def edition_language_codes(edition):
    codes = set()
    for language in edition.get("languages") or []:
        if isinstance(language, dict):
            key = language.get("key", "")
            if key:
                codes.add(key.rsplit("/", 1)[-1].lower())
        elif language:
            codes.add(str(language).lower())
    return codes

def chinese_download_queries(ol_key, metadata=None):
    ckey = f"chinese_download_queries:v1:{ol_key}"
    cached = cache_get(ckey, CHINESE_TITLE_CACHE_TTL)
    if cached is None:
        cached = disk_cache_get(ckey, CHINESE_TITLE_CACHE_TTL)
        if cached is not None:
            cache_set(ckey, cached)
    edition_titles = list((cached or {}).get("titles", []))
    if cached is None:
        editions_data = ol_get(f"{ol_key}/editions.json", {"limit": 100}) or {}
        for edition in editions_data.get("entries", []):
            title = str(edition.get("title") or "").strip()
            if title and (is_chinese_title(title) or "chi" in edition_language_codes(edition)):
                edition_titles.append(title)
        edition_titles = list(dict.fromkeys(edition_titles))
        payload = {"titles": edition_titles}
        cache_set(ckey, payload)
        disk_cache_set(ckey, payload)

    metadata = metadata or {}
    explicit_aliases = CHINESE_DOWNLOAD_TITLE_ALIASES.get(
        str(metadata.get("title") or "").strip().casefold(),
        [],
    )
    source_titles = [
        *explicit_aliases,
        metadata.get("download_title", ""),
        metadata.get("localized_title", ""),
        *edition_titles,
    ]
    queries = []

    def add(value):
        value = re.sub(r"\s+", " ", str(value or "")).strip(" /|｜-–—")
        if value and value not in queries:
            queries.append(value)

    for title in source_titles:
        title = str(title or "").strip()
        cleaned = re.sub(r"\s*[\(\（\[【].*?[\)\）\]】]\s*", " ", title)
        chinese_parts = [
            part.strip()
            for part in re.split(r"[/|｜]", cleaned)
            if is_chinese_title(part)
        ]
        preferred = chinese_parts or [cleaned]
        for candidate in preferred:
            add(candidate)
            if is_chinese_title(candidate):
                add(OPENCC_T2S.convert(candidate))
        add(title)
        if is_chinese_title(title):
            add(OPENCC_T2S.convert(title))

    add(metadata.get("title", ""))
    return queries[:10]

def similar_subject_candidates(subjects):
    subjects = [
        re.sub(r"\s+", " ", str(subject or "")).strip()
        for subject in subjects or []
    ]
    series = [
        subject for subject in subjects
        if subject.casefold().startswith("series:") and len(subject) > 7
    ]
    if series:
        return series[:1]
    candidates = []
    seen = set()
    for subject in subjects:
        normalized = subject.casefold()
        if (
            not subject
            or normalized in GENERIC_SIMILAR_SUBJECTS
            or ":" in subject
            or len(subject) < 5
            or normalized in seen
        ):
            continue
        seen.add(normalized)
        candidates.append(subject)
        if len(candidates) == 2:
            break
    return candidates or subjects[:1]

def resolve_chinese_title(ol_key):
    if not re.fullmatch(r"/works/OL\d+W", ol_key or ""):
        return ""
    ckey = f"chinese_title:v1:{ol_key}"
    cached = cache_get(ckey, CHINESE_TITLE_CACHE_TTL)
    if cached is None:
        cached = disk_cache_get(ckey, CHINESE_TITLE_CACHE_TTL)
        if cached is not None:
            cache_set(ckey, cached)
    if cached is not None:
        return cached.get("title", "")

    editions_data = ol_get(f"{ol_key}/editions.json", {"limit": 100}) or {}
    chinese_editions = [
        edition for edition in editions_data.get("entries", [])
        if "chi" in edition_language_codes(edition)
    ]
    for edition in chinese_editions:
        title = str(edition.get("title") or "").strip()
        if is_chinese_title(title):
            result = {"title": title}
            cache_set(ckey, result)
            disk_cache_set(ckey, result)
            return title

    isbns = []
    for edition in chinese_editions:
        for field in ("isbn_13", "isbn_10"):
            for isbn in edition.get(field) or []:
                normalized = re.sub(r"[^0-9Xx]", "", str(isbn))
                if normalized and normalized not in isbns:
                    isbns.append(normalized)

    title = ""
    for isbn in isbns[:8]:
        try:
            with CHINESE_TITLE_LOOKUP_SEMAPHORE:
                response = SESSION.get(
                    f"https://m.douban.com/rexxar/api/v2/book/isbn/{isbn}",
                    timeout=8,
                    headers={"Referer": "https://book.douban.com/"},
                )
            if response.status_code != 200:
                continue
            candidate = str(response.json().get("title") or "").strip()
            if is_chinese_title(candidate):
                title = candidate
                break
        except (requests.RequestException, ValueError):
            continue

    result = {"title": title}
    cache_set(ckey, result)
    disk_cache_set(ckey, result)
    return title

def resolve_english_title(ol_key):
    if not re.fullmatch(r"/works/OL\d+W", ol_key or ""):
        return ""
    ckey = f"english_title:v1:{ol_key}"
    cached = cache_get(ckey, CHINESE_TITLE_CACHE_TTL)
    if cached is None:
        cached = disk_cache_get(ckey, CHINESE_TITLE_CACHE_TTL)
        if cached is not None:
            cache_set(ckey, cached)
    if cached is not None:
        return cached.get("title", "")

    data = ol_get("/search.json", {
        "q": f"key:{ol_key} language:eng",
        "limit": 1,
        "fields": OL_BOOK_FIELDS,
    })
    record = ((data or {}).get("docs") or [{}])[0]
    edition = first_matching_edition(record, "en")
    title = str((edition or {}).get("title") or "").strip()
    if not title_matches_lang(title, "en"):
        title = ""
    result = {"title": title}
    cache_set(ckey, result)
    disk_cache_set(ckey, result)
    return title

def extract_book(w, lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    edition = first_matching_edition(w, lang)
    title = (edition or {}).get("title") or w.get("title", "")
    if not title:
        return None
    if lang == "en" and not title_matches_lang(title, lang):
        return None
    if lang == "cn" and not (title_matches_lang(title, lang) or record_has_lang(w, lang) or record_has_lang(edition or {}, lang)):
        return None
    cover_id = edition_cover_id(edition or {}) or w.get("cover_i") or w.get("cover_id")
    if not cover_id and lang != "cn":
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
    cover_url = open_library_cover_url(cover_id)
    ol_key = w.get("key", "")
    book = {"title": title, "author": author, "cover_url": cover_url, "ol_key": ol_key}
    remember_book_hint(book, lang)
    return book

def remember_book_hint(book, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    ol_key = str((book or {}).get("ol_key") or "").strip()
    if not re.fullmatch(r"/works/OL\d+W", ol_key):
        return
    hint = {
        "title": str(book.get("title") or "").strip(),
        "author": str(book.get("author") or "").strip(),
        "cover_url": str(book.get("cover_url") or "").strip(),
        "ol_key": ol_key,
    }
    with BOOK_HINTS_LOCK:
        current = BOOK_HINTS.get((lang, ol_key), {})
        BOOK_HINTS[(lang, ol_key)] = {
            key: value or current.get(key, "")
            for key, value in hint.items()
        }

def hinted_book_metadata(work_id, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    ol_key = ol_key_from_work_id(work_id)
    if not ol_key:
        return None
    with BOOK_HINTS_LOCK:
        local_hint = dict(BOOK_HINTS.get((lang, ol_key), {}))
        english_hint = dict(BOOK_HINTS.get(("en", ol_key), {}))
    hint = local_hint or english_hint
    if not hint:
        return None
    selected_title = hint.get("title") or english_hint.get("title") or "Book"
    localized_title = ""
    title = selected_title
    download_title = selected_title
    if lang == "cn":
        local_title = local_hint.get("title", "")
        english_title = english_hint.get("title", "")
        if english_title:
            title = english_title
        if local_title and is_chinese_title(local_title):
            localized_title = local_title
            download_title = local_title
    return {
        "title": title,
        "localized_title": localized_title,
        "download_title": download_title,
        "author": hint.get("author") or english_hint.get("author", ""),
        "cover_url": hint.get("cover_url") or english_hint.get("cover_url", ""),
        "ol_key": ol_key,
    }

def book_identity_keys(book):
    keys = []
    ol_key = str(book.get("ol_key") or "").strip()
    if ol_key:
        keys.append(("ol", ol_key))
    title_key = normalize_title(book.get("title", ""))
    author_key = normalize_author(book.get("author", ""))
    if title_key and author_key:
        keys.append(("ta", title_key, author_key))
    elif title_key:
        keys.append(("t", title_key))
    return keys

def book_seen(book, seen_keys):
    return any(key in seen_keys for key in book_identity_keys(book))

def remember_book(book, seen_keys):
    for key in book_identity_keys(book):
        seen_keys.add(key)

def select_unique_books(books, seen_keys=None, target=SHELF_BOOK_TARGET):
    seen_keys = seen_keys if seen_keys is not None else set()
    selected = []
    for book in books:
        if book_seen(book, seen_keys):
            continue
        selected.append(book)
        remember_book(book, seen_keys)
        if len(selected) >= target:
            break
    return selected

def fetch_topic_page_books(topic, page=1, lang=None, limit=SHELF_SEARCH_LIMIT):
    lang = lang or DEFAULT_BOOK_LANG
    q, sort = shelf_query(topic, lang)
    params = {"q": q, "sort": sort, "limit": limit, "page": page, "fields": OL_BOOK_FIELDS}
    data = ol_get("/search.json", params)
    total = data.get("numFound", 0) if data else 0
    books = []
    for w in (data or {}).get("docs", [])[:limit]:
        b = extract_book(w, lang)
        if b:
            books.append(b)
    total_pages = min(SHELF_MAX_OPEN_LIBRARY_PAGES, max(1, (total + limit - 1) // limit))
    return books, total, total_pages

def collect_unique_topic_books(topic, lang=None, seen_keys=None, target=SHELF_BOOK_TARGET, max_pages=SHELF_REFILL_OPEN_LIBRARY_PAGES):
    seen_keys = seen_keys if seen_keys is not None else set()
    selected = []
    total = 0
    total_pages = 1
    for page in range(1, max_pages + 1):
        page_books, total, total_pages = fetch_topic_page_books(topic, page, lang)
        for book in page_books:
            if book_seen(book, seen_keys):
                continue
            selected.append(book)
            remember_book(book, seen_keys)
            if len(selected) >= target:
                return selected, total, total_pages
        if page >= total_pages:
            break
    return selected, total, total_pages

def prefetch_topic_pages(topics, lang=None, max_pages=SHELF_REFILL_OPEN_LIBRARY_PAGES):
    lang = lang or DEFAULT_BOOK_LANG
    candidate_pages = {}
    jobs = [(topic, page) for topic in topics for page in range(1, max_pages + 1)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_topic_page_books, topic, page, lang): (topic, page) for topic, page in jobs}
        for future in as_completed(futures):
            topic, page = futures[future]
            try:
                candidate_pages[(topic, page)] = future.result()
            except:
                candidate_pages[(topic, page)] = ([], 0, 1)
    return candidate_pages

def select_unique_from_prefetched(topic, candidate_pages, seen_keys, target=SHELF_BOOK_TARGET, max_pages=SHELF_REFILL_OPEN_LIBRARY_PAGES):
    selected = []
    for page in range(1, max_pages + 1):
        page_books = candidate_pages.get((topic, page), ([], 0, 1))[0]
        for book in page_books:
            if book_seen(book, seen_keys):
                continue
            selected.append(book)
            remember_book(book, seen_keys)
            if len(selected) >= target:
                return selected
    return selected

def fetch_one_shelf(name, topic, lang=None, mode="nonfiction"):
    lang = lang or DEFAULT_BOOK_LANG
    try:
        books, _, _ = fetch_category_page_books(topic, 1, mode, lang)
        return {"name": name, "topic": topic, "books": books}
    except:
        return {"name": name, "topic": topic, "books": []}

def fetch_category_page_books(topic, page=1, mode="nonfiction", lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    page = max(1, page)
    ckey = f"category_page:{lang}:{mode}:{topic}:{page}"
    cached = cache_get(ckey, 900)
    if cached:
        return cached
    if page == 1:
        shelf = next(
            (item for item in get_shelves(mode, lang) if item.get("topic") == topic),
            None,
        )
        if shelf and shelf.get("books"):
            result = (shelf["books"][:SHELF_BOOK_TARGET], len(shelf["books"]), SHELF_MAX_OPEN_LIBRARY_PAGES)
            cache_set(ckey, result)
            return result
    target = SHELF_BOOK_TARGET * page
    seen_keys = seen_keys_before_shelf(topic, mode, lang)
    max_pages = min(SHELF_MAX_OPEN_LIBRARY_PAGES, max(SHELF_REFILL_OPEN_LIBRARY_PAGES, page + 2))
    books, total, total_pages = collect_unique_topic_books(topic, lang, seen_keys, target, max_pages)
    start = SHELF_BOOK_TARGET * (page - 1)
    result = (books[start:target], total, total_pages)
    cache_set(ckey, result)
    return result

def fetch_category_books(topic, page=1, lang=None, mode="nonfiction"):
    return fetch_category_page_books(topic, page, mode, lang)

def fetch_shelf_page_books(topic, page=1, mode="nonfiction", lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    page = max(1, page)
    ckey = f"shelf_page:{lang}:{mode}:{topic}:{page}"
    cached = cache_get(ckey, 900)
    if cached:
        return cached
    if page == 1:
        shelf = next(
            (item for item in get_shelves(mode, lang) if item.get("topic") == topic),
            None,
        )
        if shelf and shelf.get("books"):
            result = (shelf["books"][:SHELF_BOOK_TARGET], len(shelf["books"]), SHELF_MAX_OPEN_LIBRARY_PAGES)
            cache_set(ckey, result)
            return result
    target = SHELF_BOOK_TARGET * page
    seen_keys = seen_keys_before_shelf(topic, mode, lang)
    max_pages = min(SHELF_MAX_OPEN_LIBRARY_PAGES, max(SHELF_REFILL_OPEN_LIBRARY_PAGES, page + 2))
    books, total, total_pages = collect_unique_topic_books(topic, lang, seen_keys, target, max_pages)
    start = SHELF_BOOK_TARGET * (page - 1)
    result = (books[start:target], total, total_pages)
    cache_set(ckey, result)
    return result

def fetch_discovery_books(q, page=1, lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    ckey = f"discover:{lang}:{q}:{page}"
    cached = cache_get(ckey, 900)
    if cached:
        return cached
    limit = 60
    lang_query = f"{q} language:{BOOK_LANG_CONFIG[lang]['ol_lang']}"
    data = ol_get("/search.json", {"q": lang_query, "limit": limit, "page": page, "fields": OL_BOOK_FIELDS})
    total = data.get("numFound", 0) if data else 0
    books = []
    for w in (data or {}).get("docs", [])[:limit]:
        b = extract_book(w, lang)
        if b:
            books.append(b)
            if len(books) >= 30:
                break
    total_pages = min(25, max(1, (total + limit - 1) // limit))
    result = (books, total, total_pages)
    cache_set(ckey, result)
    return result

def fetch_shelves(mode="nonfiction", lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    sd = get_shelves_def(mode)
    candidate_pages = prefetch_topic_pages([topic for _, topic in sd], lang)
    shelves = []
    seen_keys = set()
    for name, topic in sd:
        books = select_unique_from_prefetched(topic, candidate_pages, seen_keys, SHELF_BOOK_TARGET)
        shelves.append({"name": name, "topic": topic, "books": books})
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

def normalize_match_text(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[\(\[\{（【].*?[\)\]\}）】]", " ", value)
    value = re.sub(r"[^\w\u3400-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def title_match_score(candidate, target):
    candidate = normalize_match_text(candidate)
    target = normalize_match_text(target)
    if not candidate or not target:
        return 0
    if candidate == target:
        return 1000
    shorter, longer = sorted((candidate, target), key=len)
    if shorter in longer:
        return 900 + round(80 * len(shorter) / max(len(longer), 1))
    candidate_tokens = set(candidate.split())
    target_tokens = set(target.split())
    overlap = len(candidate_tokens & target_tokens)
    token_score = 0
    if overlap:
        containment = overlap / max(min(len(candidate_tokens), len(target_tokens)), 1)
        union = len(candidate_tokens | target_tokens)
        token_score = round(500 * containment + 250 * overlap / max(union, 1))
    sequence_score = round(700 * SequenceMatcher(None, candidate, target).ratio())
    return max(token_score, sequence_score)

def author_match_score(candidate, target):
    candidate = normalize_match_text(candidate)
    target = normalize_match_text(target)
    if not target:
        return 0
    if not candidate:
        return -40
    if candidate == target:
        return 240
    candidate_tokens = set(candidate.split())
    target_tokens = set(target.split())
    if target_tokens and target_tokens <= candidate_tokens:
        return 220
    overlap = len(candidate_tokens & target_tokens)
    token_score = round(180 * overlap / max(len(target_tokens), 1))
    sequence_score = round(140 * SequenceMatcher(None, candidate, target).ratio())
    return max(token_score, sequence_score)

def book_score(book, target_title="", target_author="", preferred_language=""):
    score = title_match_score(book.title, target_title)
    score += author_match_score(book.author, target_author)
    fmt_scores = {"epub": 120, "mobi": 85, "azw3": 85, "pdf": 60, "djvu": 25, "chm": 10, "txt": 8}
    score += fmt_scores.get(book.ext.lower(), 0)
    if preferred_language:
        score += 80 if book_matches_language(book, preferred_language) else -200
    try:
        y = int(book.year)
        if 1900 <= y <= 2030:
            score += max(0, min(20, round((y - 1980) * 0.4)))
    except (TypeError, ValueError):
        pass
    bytes_val = parse_size_bytes(book.size)
    if book.ext.lower() == "epub" and (bytes_val < 50000 or bytes_val > 200 * 1024 * 1024):
        score -= 100
    elif book.ext.lower() == "pdf" and (bytes_val < 100000 or bytes_val > 500 * 1024 * 1024):
        score -= 100
    elif bytes_val:
        score += 20
    if book.publisher.strip():
        score += 12
    try:
        if int(book.pages) > 0:
            score += 12
    except (TypeError, ValueError):
        pass
    if getattr(book, "cover_dir", ""):
        score += 6
    return score

def dedup(books, scorer=None):
    scorer = scorer or book_score
    groups = {}
    for b in books:
        key = (normalize_title(b.title), normalize_author(b.author))
        groups.setdefault(key, []).append(b)
    best = []
    for group in groups.values():
        best.append(max(group, key=scorer))
    return best

def rank_download_books(books, target_title="", target_author="", preferred_language=""):
    scorer = lambda book: book_score(
        book,
        target_title=target_title,
        target_author=target_author,
        preferred_language=preferred_language,
    )
    return [
        book for _, book in sorted(
            enumerate(books),
            key=lambda item: (scorer(item[1]), -item[0]),
            reverse=True,
        )
    ], scorer

def book_matches_language(book, language):
    if not language or language == "all":
        return True
    values = {
        part.strip().lower()
        for part in re.split(r"[;,/]", book.language or "")
        if part.strip()
    }
    return language.lower() in values

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
    desc = strip_html(desc)
    desc = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)", "", desc)
    desc = re.sub(r"(?:\*\*|__|`)", "", desc)
    desc = re.sub(r"\[source\]\[\d+\]", "", desc, flags=re.I)
    desc = re.sub(r"\[\d+\]:\s*\S+", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc

def english_description_for_work(ol_key, work=None):
    work = work or ol_get_work(ol_key) or {}
    description = extract_desc(work)
    if is_english_text(description):
        return description

    editions_data = ol_get(f"{ol_key}/editions.json", {"limit": 100}) or {}
    candidates = []
    for edition in editions_data.get("entries", []):
        if "eng" not in edition_language_codes(edition):
            continue
        candidate = extract_desc(edition)
        if is_english_text(candidate):
            candidates.append(candidate)
    if not candidates:
        return ""

    def description_rank(candidate):
        word_count = len(candidate.split())
        awkward_lead = candidate.startswith(('"', "'")) or bool(
            re.match(r"^[A-Z][A-Z !'-]{12,}", candidate)
        )
        return awkward_lead, not 40 <= word_count <= 350, abs(word_count - 120)

    return min(candidates, key=description_rank)

def first_work_author(work):
    authors = work.get("authors") or []
    for item in authors:
        author_ref = (item or {}).get("author") if isinstance(item, dict) else None
        key = (author_ref or {}).get("key") if isinstance(author_ref, dict) else None
        if not key:
            continue
        author = ol_get(key + ".json")
        name = (author or {}).get("name", "").strip()
        if name:
            return name
    return ""

def book_metadata_from_work(work_id, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    ckey = f"book_meta:{lang}:{work_id}"
    cached = cache_get(ckey, API_DISK_CACHE_TTL)
    if cached is None:
        cached = disk_cache_get(ckey, API_DISK_CACHE_TTL)
        if cached is not None:
            cache_set(ckey, cached)
    if cached:
        return cached
    ol_key = ol_key_from_work_id(work_id)
    if not ol_key:
        return None
    work = ol_get_work(ol_key)
    if not work:
        return None

    search_data = ol_get("/search.json", {
        "q": f"key:{ol_key} language:{BOOK_LANG_CONFIG[lang]['ol_lang']}",
        "limit": 1,
        "fields": OL_BOOK_FIELDS,
    })
    search_record = ((search_data or {}).get("docs") or [{}])[0]
    edition = first_matching_edition(search_record, lang)
    covers = work.get("covers") or []
    cover_id = edition_cover_id(edition or {}) or search_record.get("cover_i") or (covers[0] if covers else "")
    authors = search_record.get("author_name") or []
    selected_title = (edition or {}).get("title") or search_record.get("title") or work.get("title", "")
    title = selected_title
    localized_title = ""
    download_title = selected_title
    if lang == "cn":
        localized_title = selected_title if is_chinese_title(selected_title) else resolve_chinese_title(ol_key)
        title = resolve_english_title(ol_key) or localized_title or selected_title
        download_title = localized_title or selected_title or title
        if localized_title == title:
            localized_title = ""
    result = {
        "title": title,
        "localized_title": localized_title,
        "download_title": download_title,
        "author": (authors[0] if authors else "") or first_work_author(work),
        "cover_url": open_library_cover_url(cover_id),
        "ol_key": ol_key,
    }
    cache_set(ckey, result)
    disk_cache_set(ckey, result)
    remember_book_hint(result, lang)
    return result

app = Flask(__name__)

@app.after_request
def cache_headers(resp):
    if request.method in ("GET", "HEAD") and resp.status_code < 400:
        if resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "private, max-age=90, stale-while-revalidate=600"
        elif request.path.startswith(("/api/book", "/api/category", "/api/shelf", "/api/discover", "/api/cn-display-title")):
            resp.headers["Cache-Control"] = "private, max-age=600, stale-while-revalidate=3600"
        elif request.path.startswith("/api/search"):
            resp.headers["Cache-Control"] = "private, max-age=120"
        elif request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if request.args.get("v")
                else "public, max-age=3600"
            )
    if resp.mimetype == "text/html" or request.path.startswith("/api/"):
        resp.set_cookie("book_lang", get_book_lang(), max_age=31536000, samesite="Lax")
    return resp

@app.context_processor
def inject_book_context():
    static_paths = (
        os.path.join(app.static_folder, "libflix.css"),
        os.path.join(app.static_folder, "download-ui.js"),
    )
    asset_version = max(
        (int(os.path.getmtime(path)) for path in static_paths if os.path.exists(path)),
        default=1,
    )
    return {
        "book_lang": get_book_lang(),
        "book_lang_label": BOOK_LANG_CONFIG[get_book_lang()]["label"],
        "lang_url": lang_url,
        "home_url": clean_home_url,
        "category_url": clean_category_url,
        "discover_url": clean_discover_url,
        "book_url": book_url,
        "asset_version": asset_version,
    }

@app.template_filter("size_url")
def size_url(url, size="M"):
    if not url:
        return url
    zoom = {"S": "1", "M": "3", "L": "5"}.get(size, "3")
    if url.startswith("/"):
        return f"{url.rstrip('/')}/{size}"
    if "covers.openlibrary.org/b/id/" in url:
        return re.sub(r"-[SML]\.jpg(?:\?.*)?$", f"-{size}.jpg", url)
    if "zoom=" in url:
        return re.sub(r'zoom=\d+', f'zoom={zoom}', url)
    return url

SORT_OPTIONS = {
    "y": "Year", "id": "ID", "title": "Title",
    "author": "Author", "filesize": "Size", "extension": "Extension",
    "time_added": "Date Added"
}

def shelf_cache_path(mode="nonfiction", lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    return SHELF_DISK_CACHE.replace(".json", f"_{lang}_{mode}.json")

def disk_load_shelves(mode="nonfiction", lang=None):
    path = shelf_cache_path(mode, lang)
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if data and isinstance(data, list) and len(data) > 0:
            return data
    except:
        pass
    return None

def disk_save_shelves(shelves, mode="nonfiction", lang=None):
    path = shelf_cache_path(mode, lang)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(shelves, f)
        os.replace(tmp, path)
    except:
        pass

def shelf_cache_is_fresh(mode="nonfiction", lang=None):
    try:
        return time.time() - os.path.getmtime(shelf_cache_path(mode, lang)) < SHELF_REFRESH_TTL
    except OSError:
        return False

def schedule_shelf_refresh(mode="nonfiction", lang=None, delay=3):
    lang = lang or DEFAULT_BOOK_LANG
    refresh_key = (lang, mode)
    if shelf_cache_is_fresh(mode, lang):
        return
    with SHELF_REFRESH_LOCK:
        if refresh_key in SHELF_REFRESHING:
            return
        SHELF_REFRESHING.add(refresh_key)

    def refresh():
        try:
            if delay:
                time.sleep(delay)
            shelves = normalize_shelf_labels(fetch_shelves(mode, lang), mode)
            if shelves:
                cache_set(f"shelves_{lang}_{mode}", shelves)
                disk_save_shelves(shelves, mode, lang)
        finally:
            with SHELF_REFRESH_LOCK:
                SHELF_REFRESHING.discard(refresh_key)

    threading.Thread(target=refresh, daemon=True, name=f"shelf-refresh-{lang}-{mode}").start()

def normalize_shelf_labels(shelves, mode="nonfiction"):
    names_by_topic = {topic: name for name, topic in get_shelves_def(mode)}
    normalized = []
    for shelf in shelves or []:
        shelf_copy = dict(shelf)
        shelf_copy["books"] = []
        for book in shelf.get("books", []):
            book_copy = dict(book)
            match = re.fullmatch(r"/olcover/(\d+)(?:/[SML])?", book_copy.get("cover_url", ""))
            if match:
                book_copy["cover_url"] = open_library_cover_url(match.group(1))
            shelf_copy["books"].append(book_copy)
        topic = shelf_copy.get("topic", "")
        if topic in names_by_topic:
            shelf_copy["name"] = names_by_topic[topic]
        normalized.append(shelf_copy)
    return normalized

def get_shelves(mode="nonfiction", lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    ckey = f"shelves_{lang}_{mode}"
    cached = cache_get(ckey, CACHE_TTL_OL)
    if cached:
        schedule_shelf_refresh(mode, lang)
        return normalize_shelf_labels(cached, mode)
    disk = disk_load_shelves(mode, lang)
    if disk:
        shelves = dedupe_and_refill_shelves(disk, mode, lang)
        shelves = normalize_shelf_labels(shelves, mode)
        cache_set(ckey, shelves)
        schedule_shelf_refresh(mode, lang)
        return shelves
    shelves = fetch_shelves(mode, lang)
    shelves = normalize_shelf_labels(shelves, mode)
    cache_set(ckey, shelves)
    disk_save_shelves(shelves, mode, lang)
    return shelves

def dedupe_and_refill_shelves(shelves, mode="nonfiction", lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    by_topic = {shelf.get("topic"): shelf for shelf in shelves}
    by_name = {shelf.get("name"): shelf for shelf in shelves}
    sd = get_shelves_def(mode)
    candidate_pages = None
    seen_keys = set()
    output = []
    for name, topic in sd:
        shelf = by_topic.get(topic) or by_name.get(name) or {"name": name, "topic": topic, "books": []}
        books = select_unique_books(shelf.get("books", []), seen_keys, SHELF_BOOK_TARGET)
        if len(books) < SHELF_BOOK_TARGET:
            if candidate_pages is None:
                candidate_pages = prefetch_topic_pages([shelf_topic for _, shelf_topic in sd], lang)
            extra = select_unique_from_prefetched(topic, candidate_pages, seen_keys, SHELF_BOOK_TARGET - len(books))
            books.extend(extra)
        output.append({"name": name, "topic": topic, "books": books})
    return output

def seen_keys_before_shelf(topic, mode="nonfiction", lang=None):
    seen_keys = set()
    shelves = get_shelves(mode, lang)
    by_topic = {shelf.get("topic"): shelf for shelf in shelves}
    for _, shelf_topic in get_shelves_def(mode):
        if shelf_topic == topic:
            break
        for book in by_topic.get(shelf_topic, {}).get("books", []):
            remember_book(book, seen_keys)
    return seen_keys

def dedup_across_shelves(shelves):
    seen = set()
    for shelf in shelves:
        deduped = []
        for book in shelf["books"]:
            if not book_seen(book, seen):
                remember_book(book, seen)
                deduped.append(book)
        shelf["books"] = deduped
        if "topic" not in shelf:
            shelf["topic"] = next((topic for name, topic in SHELVES_DEF + FICTION_SHELVES_DEF if name == shelf.get("name")), "")
    return shelves

def render_home(mode="nonfiction", lang=None, error=None):
    mode = mode if mode in ("fiction", "nonfiction") else "nonfiction"
    lang = normalize_book_lang(lang) or get_book_lang()
    g.mode_override = mode
    g.book_lang_override = lang
    shelves = get_shelves(mode, lang)
    hero = None
    hero_books = []
    hero_items = []
    if shelves:
        trending = shelves[0].get("books", [])
        hero_books = trending[:7]
        if trending:
            hero = dict(random.choice(trending[:min(len(trending), 16)]))
            if hero:
                hero_key = hero.get("ol_key") or f"{hero.get('title')}|{hero.get('author')}"
                hero_books = [hero] + [
                    b for b in hero_books
                    if (b.get("ol_key") or f"{b.get('title')}|{b.get('author')}") != hero_key
                ]
                hero_books = hero_books[:7]
                hero_items = [dict(book, description="") for book in hero_books]
                hero = hero_items[0]
    return render_template("index.html", shelves=shelves, hero=hero, hero_books=hero_books, hero_items=hero_items, mode=mode, error=error)

@app.route("/", defaults={"clean_mode": None, "clean_lang": None})
@app.route("/fiction", defaults={"clean_mode": "fiction", "clean_lang": None})
@app.route("/cn", defaults={"clean_mode": "nonfiction", "clean_lang": "cn"})
@app.route("/fiction/cn", defaults={"clean_mode": "fiction", "clean_lang": "cn"})
def index(clean_mode, clean_lang):
    mode = clean_mode or request.args.get("mode", "nonfiction")
    if mode not in ("fiction", "nonfiction"):
        mode = "nonfiction"
    lang = clean_lang or get_book_lang()
    if clean_mode is None and ("mode" in request.args or "book_lang" in request.args):
        return preserve_query_redirect(clean_home_url(mode, lang))
    return render_home(mode, lang)

@app.route("/category/<topic>", defaults={"clean_mode": None, "clean_lang": None})
@app.route("/fiction/category/<topic>", defaults={"clean_mode": "fiction", "clean_lang": None})
@app.route("/cn/category/<topic>", defaults={"clean_mode": "nonfiction", "clean_lang": "cn"})
@app.route("/fiction/cn/category/<topic>", defaults={"clean_mode": "fiction", "clean_lang": "cn"})
def category_page(topic, clean_mode, clean_lang):
    mode = clean_mode or request.args.get("mode", "nonfiction")
    if mode not in ("fiction", "nonfiction"):
        mode = "nonfiction"
    lang = clean_lang or get_book_lang()
    g.mode_override = mode
    g.book_lang_override = lang
    if clean_mode is None and ("mode" in request.args or "book_lang" in request.args):
        return preserve_query_redirect(clean_category_url(topic, mode, lang))
    sd = get_shelves_def(mode)
    valid_topics = {t for _, t in sd}
    if topic not in valid_topics:
        return render_template("category.html", shelf={"name": topic.capitalize(), "books": []}, topic=topic, mode=mode)
    name = {t: n for n, t in sd}.get(topic, topic.capitalize())
    shelf = fetch_one_shelf(name, topic, lang, mode)
    return render_template("category.html", shelf=shelf, topic=topic, mode=mode)

@app.route("/api/category/<topic>")
def api_category(topic):
    page = int(request.args.get("page", 1))
    mode = request.args.get("mode", "nonfiction")
    lang = get_book_lang()
    sd = get_shelves_def(mode)
    valid_topics = {t for _, t in sd}
    if topic not in valid_topics:
        return jsonify({"success": False, "error": "Invalid topic"})

    books, total, total_pages = fetch_category_books(topic, page, lang, mode)
    return jsonify({
        "success": True, "books": books,
        "page": page, "total_pages": total_pages, "total": total,
    })

@app.route("/api/shelf/<topic>")
def api_shelf(topic):
    page = int(request.args.get("page", 1))
    mode = request.args.get("mode", "nonfiction")
    lang = get_book_lang()
    sd = get_shelves_def(mode)
    valid_topics = {t for _, t in sd}
    if topic not in valid_topics:
        return jsonify({"success": False, "error": "Invalid topic"})
    books, total, total_pages = fetch_shelf_page_books(topic, page, mode, lang)
    return jsonify({"success": True, "books": books, "page": page, "total_pages": total_pages, "total": total})

@app.route("/discover", defaults={"clean_mode": None, "clean_lang": None})
@app.route("/fiction/discover", defaults={"clean_mode": "fiction", "clean_lang": None})
@app.route("/cn/discover", defaults={"clean_mode": "nonfiction", "clean_lang": "cn"})
@app.route("/fiction/cn/discover", defaults={"clean_mode": "fiction", "clean_lang": "cn"})
def discover(clean_mode, clean_lang):
    q = request.args.get("q", "").strip()
    mode = clean_mode or request.args.get("mode", "nonfiction")
    if mode not in ("fiction", "nonfiction"):
        mode = "nonfiction"
    lang = clean_lang or get_book_lang()
    g.mode_override = mode
    g.book_lang_override = lang
    if clean_mode is None and ("mode" in request.args or "book_lang" in request.args):
        return preserve_query_redirect(clean_discover_url(mode, lang))
    if not q:
        return render_home(mode, lang, error="Enter a search query.")

    page = int(request.args.get("page", 1))
    books, total, total_pages = fetch_discovery_books(q, page, lang)
    return render_template(
        "discover.html",
        query=q,
        books=books,
        total=total,
        page=page,
        total_pages=total_pages,
        mode=mode,
        search_value=q,
    )

@app.route("/api/discover")
def api_discover():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"success": False, "error": "No query provided"})
    page = int(request.args.get("page", 1))
    lang = get_book_lang()
    books, total, total_pages = fetch_discovery_books(q, page, lang)
    return jsonify({
        "success": True,
        "query": q,
        "books": books,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    mode = request.args.get("mode", "nonfiction")
    if mode not in ("fiction", "nonfiction"):
        mode = "nonfiction"
    if not q:
        shelves = get_shelves(mode, get_book_lang())
        return render_template("index.html", shelves=shelves, error="Enter a search query.", mode=mode)
    sort = request.args.get("sort", "best_match")
    order = request.args.get("order", "DESC").upper()
    limit = int(request.args.get("limit", 25)) if request.args.get("limit", "25").isdigit() else 25
    limit = limit if limit in (25, 50, 100) else 25
    page = int(request.args.get("page", 1)) if request.args.get("page", "1").isdigit() else 1
    page = max(1, page)
    fmt = request.args.get("format", "all")
    default_download_lang = "Chinese" if get_book_lang() == "cn" else "English"
    lang = request.args.get("lang", default_download_lang)
    if lang not in ("English", "Chinese", "all"):
        lang = default_download_lang
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
    lang = get_book_lang()
    if ol_key:
        return redirect(clean_book_url(ol_key, mode, lang), code=301)
    return render_template("book.html",
        title=title, author=author, cover_url=cover_url,
        ol_key=ol_key, mode=mode)

@app.route("/book/<work_id>", defaults={"clean_mode": None, "clean_lang": None})
@app.route("/fiction/book/<work_id>", defaults={"clean_mode": "fiction", "clean_lang": None})
@app.route("/cn/book/<work_id>", defaults={"clean_mode": "nonfiction", "clean_lang": "cn"})
@app.route("/fiction/cn/book/<work_id>", defaults={"clean_mode": "fiction", "clean_lang": "cn"})
def book_page(work_id, clean_mode, clean_lang):
    mode = clean_mode or request.args.get("mode", "nonfiction")
    if mode not in ("fiction", "nonfiction"):
        mode = "nonfiction"
    lang = clean_lang or get_book_lang()
    g.mode_override = mode
    g.book_lang_override = lang
    if clean_mode is None and ("mode" in request.args or "book_lang" in request.args):
        return preserve_query_redirect(clean_book_url(work_id, mode, lang))
    ol_key = ol_key_from_work_id(work_id)
    if not ol_key:
        return render_template("book.html", title="Book not found", author="", cover_url="", ol_key="", mode=mode), 404
    ckey = f"book_meta:{lang}:{work_id}"
    book = cache_get(ckey, API_DISK_CACHE_TTL) or disk_cache_get(ckey, API_DISK_CACHE_TTL)
    if book:
        cache_set(ckey, book)
    else:
        book = hinted_book_metadata(work_id, lang)
    if book is None:
        book = {
            "title": "Book",
            "localized_title": "",
            "download_title": "",
            "author": "",
            "cover_url": "",
            "ol_key": ol_key,
        }
    return render_template("book.html", mode=mode, **book)

@app.route("/api/similar")
def api_similar():
    subjects = [
        subject.strip()
        for subject in request.args.getlist("subject")[:2]
        if subject.strip()
    ]
    ol_key = request.args.get("ol_key", "").strip()
    lang = get_book_lang()
    if not subjects:
        return jsonify({"success": False, "error": "No subject"})

    def fetch_subject(subject):
        data = ol_get("/search.json", {
            "q": f"subject:{subject} language:{BOOK_LANG_CONFIG[lang]['ol_lang']}",
            "sort": "rating",
            "limit": 30,
            "fields": OL_BOOK_FIELDS,
        })
        return (data or {}).get("docs", [])

    subject_docs = []
    with ThreadPoolExecutor(max_workers=len(subjects)) as pool:
        futures = [pool.submit(fetch_subject, subject) for subject in subjects]
        for future in futures:
            try:
                subject_docs.append(future.result())
            except Exception:
                subject_docs.append([])

    candidates = {}
    sequence = 0
    for docs in subject_docs:
        seen_in_subject = set()
        for record in docs:
            book = extract_book(record, lang)
            if not book or book["ol_key"] == ol_key:
                continue
            key = book["ol_key"]
            entry = candidates.setdefault(key, {"book": book, "matches": 0, "order": sequence})
            if key not in seen_in_subject:
                entry["matches"] += 1
                seen_in_subject.add(key)
            sequence += 1

    current_work = ol_get_work(ol_key) or {}
    current_title = normalize_title(current_work.get("title", ""))
    seen_titles = {current_title} if current_title else set()
    books = []
    ranked = sorted(
        candidates.values(),
        key=lambda entry: (-entry["matches"], entry["order"]),
    )
    for entry in ranked:
        book = entry["book"]
        title_key = normalize_title(book.get("title", ""))
        if not title_key or title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        books.append(book)
        if len(books) == 12:
            break
    return jsonify({"success": True, "books": books})

@app.route("/api/book")
def api_book():
    ol_key = request.args.get("ol_key", "").strip()
    if not ol_key:
        return jsonify({"success": False, "error": "No ol_key provided"})
    if not ol_key.startswith("/works/"):
        return jsonify({"success": False, "error": "Book not found"})

    work = ol_get_work(ol_key)
    if not work:
        return jsonify({"success": False, "error": "Book not found"})
    description = english_description_for_work(ol_key, work)
    if request.args.get("description_only") == "1":
        return jsonify({"success": True, "description": description})
    metadata = book_metadata_from_work(work_id_from_ol_key(ol_key), get_book_lang()) or {}
    subjects = work.get("subjects", [])[:20]
    similar_subjects = similar_subject_candidates(subjects)
    download_queries = (
        chinese_download_queries(ol_key, metadata)
        if get_book_lang() == "cn"
        else []
    )
    return jsonify({
        "success": True,
        "title": metadata.get("title") or work.get("title", ""),
        "localized_title": metadata.get("localized_title", ""),
        "download_title": metadata.get("download_title", ""),
        "author": metadata.get("author", ""),
        "cover_url": metadata.get("cover_url", ""),
        "download_queries": download_queries,
        "description": description,
        "subjects": subjects,
        "similar_subjects": similar_subjects,
    })

@app.route("/api/cn-display-title")
def api_cn_display_title():
    ol_key = request.args.get("ol_key", "").strip()
    if not re.fullmatch(r"/works/OL\d+W", ol_key):
        return jsonify({"success": False, "error": "Invalid Open Library work"}), 400
    title = resolve_english_title(ol_key)
    return jsonify({"success": bool(title), "title": title, "ol_key": ol_key})

@app.route("/api/cn-display-titles")
def api_cn_display_titles():
    ol_keys = []
    for ol_key in request.args.getlist("ol_key")[:24]:
        ol_key = ol_key.strip()
        if re.fullmatch(r"/works/OL\d+W", ol_key) and ol_key not in ol_keys:
            ol_keys.append(ol_key)
    if not ol_keys:
        return jsonify({"success": False, "error": "No valid Open Library works"}), 400

    titles = {}
    with ThreadPoolExecutor(max_workers=min(4, len(ol_keys))) as pool:
        futures = {pool.submit(resolve_english_title, ol_key): ol_key for ol_key in ol_keys}
        for future in as_completed(futures):
            ol_key = futures[future]
            try:
                title = future.result()
            except Exception:
                title = ""
            if title:
                titles[ol_key] = title
    return jsonify({"success": True, "titles": titles})

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"success": False, "error": "No query provided"})
    sort = request.args.get("sort", "best_match")
    order = request.args.get("order", "DESC").upper()
    limit = int(request.args.get("limit", 25)) if request.args.get("limit", "25").isdigit() else 25
    limit = limit if limit in (25, 50, 100) else 25
    page = int(request.args.get("page", 1)) if request.args.get("page", "1").isdigit() else 1
    page = max(1, min(page, 500))
    fmt = request.args.get("format", "all").lower()
    if fmt not in ("all", "epub", "pdf", "mobi"):
        fmt = "all"
    default_download_lang = "Chinese" if get_book_lang() == "cn" else "English"
    lang = request.args.get("lang", default_download_lang)
    if lang not in ("English", "Chinese", "all"):
        lang = default_download_lang
    dedup_on = request.args.get("dedup", "1") == "1"
    target_title = request.args.get("target_title", "").strip() or q
    target_author = request.args.get("target_author", "").strip()
    result_cache_key = (
        f"download_search:v2:{q}:{sort}:{order}:{limit}:{page}:"
        f"{fmt}:{lang}:{int(dedup_on)}:{target_title}:{target_author}"
    )
    cached_result = cache_get(result_cache_key, 900)
    if cached_result is None:
        cached_result = disk_cache_get(result_cache_key, 900)
        if cached_result is not None:
            cache_set(result_cache_key, cached_result)
    if cached_result is not None:
        return jsonify(cached_result)

    sort_field = "y" if sort in ("year", "best_match") else sort
    try:
        books, total = DOWNLOADER.search(q, sort=sort_field, order=order, page=page, limit=limit)
    except requests.Timeout:
        return jsonify({
            "success": False,
            "error": "The download source timed out.",
            "code": "source_timeout",
        }), 504
    except requests.RequestException:
        return jsonify({
            "success": False,
            "error": "The download source is temporarily unreachable.",
            "code": "source_unavailable",
        }), 503
    except Exception:
        return jsonify({
            "success": False,
            "error": "Downloads could not be checked right now.",
            "code": "search_failed",
        }), 502

    lang_filter = None if lang == "all" else lang
    fmt_filter = None if fmt == "all" else fmt
    filtered = []
    for b in books:
        if lang_filter and not book_matches_language(b, lang_filter):
            continue
        if fmt_filter and b.ext.lower() != fmt_filter.lower():
            continue
        filtered.append(b)
    books = filtered
    scorer = lambda book: book_score(
        book,
        target_title=target_title,
        target_author=target_author,
        preferred_language=lang_filter or "",
    )
    if dedup_on:
        books = dedup(books, scorer)
    best_book = max(books, key=scorer) if books else None
    if sort == "best_match":
        books, scorer = rank_download_books(
            books,
            target_title=target_title,
            target_author=target_author,
            preferred_language=lang_filter or "",
        )

    total_pages = (total + limit - 1) // limit if total else 1
    result_books = []
    for i, b in enumerate(books):
        d = b.to_dict(i + 1 + (page - 1) * limit)
        cover_dir = getattr(b, "cover_dir", "")
        d["cover_url"] = f"/cover/{d['md5']}?dir={cover_dir}" if cover_dir and d['md5'] else ""
        d["best_match"] = b is best_book
        result_books.append(d)
    result = {
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
    }
    cache_set(result_cache_key, result)
    disk_cache_set(result_cache_key, result)
    return jsonify(result)

@app.route("/download/<md5>")
def download(md5):
    url = DOWNLOADER.resolve_download(md5)
    filename = request.args.get("filename", f"{md5}.epub")
    filename = re.sub(r'[\r\n\\/\"<>|:*?]+', ' ', filename)
    filename = re.sub(r'\s+', ' ', filename).strip()[:140] or f"{md5}.epub"
    ascii_filename = filename.encode("ascii", "ignore").decode().strip()
    ascii_filename = re.sub(r'[^A-Za-z0-9._ -]+', '', ascii_filename) or f"{md5}.epub"
    def generate():
        try:
            r = SESSION.get(url, stream=True, timeout=120, allow_redirects=True)
            r.raise_for_status()
            yield from r.iter_content(chunk_size=65536)
        except:
            yield b""
    resp = Response(stream_with_context(generate()),
                    mimetype="application/octet-stream")
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quote(filename)}'
    )
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
            resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
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
            resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
            return resp
    except:
        pass
    return "", 404

def _kindle_progress(stage, progress=None, detail=""):
    event = {"type": "progress", "stage": stage, "progress": progress}
    if detail:
        event["detail"] = detail
    return event


def _format_transfer_size(value):
    value = max(0, int(value or 0))
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024


def _send_to_kindle_events(data):
    import smtplib, tempfile
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email.mime.text import MIMEText
    from email import encoders

    md5 = data.get("md5", "")
    title = re.sub(r"[\r\n]+", " ", data.get("title", "book")).strip() or "book"
    ext = re.sub(r"[^a-z0-9]", "", data.get("ext", "epub").lower()) or "epub"
    kindle_email = data.get("kindle_email", "").strip()
    smtp_host = data.get("smtp_host", "").strip()
    smtp_port = data.get("smtp_port", 587)
    smtp_user = data.get("smtp_user", "").strip()
    smtp_pass = data.get("smtp_pass", "")
    sender_email = data.get("sender_email", smtp_user)
    tmp_path = None
    progress = 3

    try:
        yield _kindle_progress("Preparing delivery", progress)
        dl_url = DOWNLOADER.resolve_download(md5)
        if not dl_url:
            raise RuntimeError("The download source did not return a file link.")

        progress = 10
        yield _kindle_progress("Connecting to book source", progress)
        r = SESSION.get(dl_url, stream=True, timeout=120, allow_redirects=True)
        r.raise_for_status()
        try:
            total_bytes = int(r.headers.get("content-length", "") or 0)
        except (TypeError, ValueError):
            total_bytes = 0

        downloaded = 0
        last_reported_progress = progress
        last_reported_bytes = 0
        yield _kindle_progress("Downloading book", progress, "Starting transfer")
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp_path = tmp.name
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                tmp.write(chunk)
                downloaded += len(chunk)
                if total_bytes:
                    current = 10 + int(min(downloaded / total_bytes, 1) * 55)
                    if current >= last_reported_progress + 2:
                        last_reported_progress = current
                        progress = current
                        detail = f"{_format_transfer_size(downloaded)} of {_format_transfer_size(total_bytes)}"
                        yield _kindle_progress("Downloading book", progress, detail)
                elif downloaded - last_reported_bytes >= 1024 * 1024:
                    last_reported_bytes = downloaded
                    yield _kindle_progress("Downloading book", None, f"{_format_transfer_size(downloaded)} downloaded")

        progress = 68
        yield _kindle_progress("Building Kindle document", progress, _format_transfer_size(downloaded))

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

        progress = 78
        yield _kindle_progress("Connecting to email", progress)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            progress = 84
            yield _kindle_progress("Signing in securely", progress)
            server.login(smtp_user, smtp_pass)
            progress = 92
            yield _kindle_progress("Sending to Kindle", progress)
            server.send_message(msg)

        progress = 100
        yield {"type": "complete", "success": True, "stage": "Sent to Kindle", "progress": progress}
    except smtplib.SMTPAuthenticationError:
        yield {
            "type": "error",
            "success": False,
            "stage": "Sign-in failed",
            "progress": progress,
            "error": "SMTP auth failed. For Gmail, use an App Password.",
        }
    except Exception as e:
        yield {
            "type": "error",
            "success": False,
            "stage": "Delivery failed",
            "progress": progress,
            "error": str(e),
        }
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@app.route("/api/sendtokindle", methods=["POST"])
def api_sendtokindle():
    data = request.get_json(silent=True) or {}
    required = ("md5", "kindle_email", "smtp_host", "smtp_user", "smtp_pass")
    if not all(data.get(field) for field in required):
        return jsonify({"success": False, "error": "Missing required fields"}), 400
    try:
        data["smtp_port"] = int(data.get("smtp_port", 587))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "SMTP port must be a number"}), 400

    if request.args.get("stream") == "1":
        def stream_events():
            for event in _send_to_kindle_events(data):
                yield json.dumps(event, ensure_ascii=False) + "\n"

        response = Response(stream_with_context(stream_events()), mimetype="application/x-ndjson")
        response.headers["Cache-Control"] = "no-cache, no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    events = list(_send_to_kindle_events(data))
    final = events[-1] if events else {"success": False, "error": "Delivery did not complete"}
    status = 200 if final.get("success") else 502
    return jsonify(final), status

def load_cached_shelves():
    for lang in BOOK_LANGS:
        for mode in ("nonfiction", "fiction"):
            disk = disk_load_shelves(mode, lang)
            if not disk:
                continue
            shelves = normalize_shelf_labels(disk, mode)
            for shelf in shelves:
                for book in shelf.get("books", []):
                    remember_book_hint(book, lang)
            cache_set(f"shelves_{lang}_{mode}", shelves)
            print(f"Loaded {len(shelves)} Open Library {lang} {mode} shelves from disk cache", flush=True)

if __name__ == "__main__":
    initialize_disk_cache()
    load_cached_shelves()
    app.run(host="0.0.0.0", port=5800, debug=True, use_reloader=False)
