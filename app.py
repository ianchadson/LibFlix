import re, os, io, json, html as htmlmod, warnings, time, random, threading, hashlib, sqlite3, unicodedata, uuid, ipaddress, socket, fcntl, math
from contextlib import contextmanager
from difflib import SequenceMatcher
from urllib.parse import urljoin, quote, urlencode, urlsplit, parse_qs
from dataclasses import dataclass, replace
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    as_completed,
    wait,
)

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, Response, stream_with_context, jsonify, g, redirect, send_file, has_request_context, got_request_exception
from flask.testing import FlaskClient
from opencc import OpenCC

from book_preparation import prepare_book_for_kindle
from kindle_delivery import (
    KindleSourceCache,
    SourceFileError,
    stream_smtp_attachment,
    validate_source_file,
)
from security_runtime import (
    RateLimitRule,
    SQLiteMetrics,
    SQLiteRateLimiter,
    SecurityHeadersConfig,
    apply_security_headers,
    json_rate_limit_body,
    request_client_identity,
)
from topic_discovery import (
    BROWSE_TOPIC_GROUPS as TOPIC_BROWSE_GROUPS,
    EXPANSION_VERSION as TOPIC_EXPANSION_VERSION,
    FEATURED_TOPIC_QUERIES,
    RANKER_VERSION as TOPIC_RANKER_VERSION,
    build_inventaire_request,
    build_openlibrary_request,
    candidate_to_book,
    filter_topic_results,
    merge_topic_candidates,
    normalize_text as normalize_topic_text,
    parse_inventaire_payload,
    parse_openlibrary_payload,
    plan_topic_query,
)

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # Pillow is installed from requirements in production.
    Image = None
    UnidentifiedImageError = OSError

# Modular download source — see ``downloaders/`` package.
from downloaders import DOWNLOADER
from downloaders.base import Book, SESSION as DL_SESSION
from downloaders.libgen import MIRROR

warnings.filterwarnings("ignore", category=requests.packages.urllib3.exceptions.InsecureRequestWarning)

OL = "https://openlibrary.org"
INVENTAIRE = "https://inventaire.io/api"
CACHE = {}
CACHE_TTL_OL = 3600
API_DISK_CACHE_TTL = 21600
CHINESE_TITLE_CACHE_TTL = 2592000
EXTERNAL_METADATA_TTL = 2592000
EXTERNAL_METADATA_FAILURE_TTL = 120
API_CACHE_RETENTION_TTL = 7776000
MEMORY_CACHE_MAX_ENTRIES = max(256, int(os.environ.get("LIBFLIX_MEMORY_CACHE_MAX_ENTRIES", "4096")))
API_CACHE_MAX_ROWS = max(500, int(os.environ.get("LIBFLIX_API_CACHE_MAX_ROWS", "20000")))
API_CACHE_MAX_BYTES = max(16 * 1024**2, int(os.environ.get("LIBFLIX_API_CACHE_MAX_BYTES", str(256 * 1024**2))))
API_CACHE_MAX_PAYLOAD_BYTES = max(64 * 1024, int(os.environ.get("LIBFLIX_API_CACHE_MAX_PAYLOAD_BYTES", str(2 * 1024**2))))
UPSTREAM_JSON_MAX_BYTES = max(
    256 * 1024,
    min(
        API_CACHE_MAX_PAYLOAD_BYTES,
        int(os.environ.get("LIBFLIX_UPSTREAM_JSON_MAX_BYTES", str(2 * 1024**2))),
    ),
)
API_CACHE_PRUNE_INTERVAL = 300
OL_STALE_TTL = 7776000
BOOK_DETAIL_FRESH_TTL = 604800
BOOK_DETAIL_STALE_TTL = 7776000
SIMILAR_FRESH_TTL = 604800
SIMILAR_STALE_TTL = 2592000
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("LIBFLIX_DATA_DIR") or APP_DIR
SHELF_DISK_CACHE = os.path.join(DATA_DIR, "shelf_cache.json")
API_DISK_CACHE = os.path.join(DATA_DIR, "api_cache.json")
API_SQLITE_CACHE = os.path.join(DATA_DIR, "api_cache.sqlite3")
COVER_CACHE_DIR = os.path.join(DATA_DIR, "covers")
KINDLE_SOURCE_CACHE_DIR = os.path.join(DATA_DIR, "kindle-source-cache")
KINDLE_DELIVERY_LOCK_FILE = os.path.join(DATA_DIR, "kindle-delivery.lock")
RATE_LIMIT_SQLITE = os.path.join(DATA_DIR, "rate_limits.sqlite3")
METRICS_SQLITE = os.path.join(DATA_DIR, "metrics.sqlite3")
KINDLE_SOURCE_CACHE_TTL = max(0, int(os.environ.get("KINDLE_SOURCE_CACHE_TTL", "86400")))
KINDLE_SOURCE_CACHE_MAX_BYTES = max(
    0,
    int(os.environ.get("KINDLE_SOURCE_CACHE_MAX_BYTES", str(5 * 1024**3))),
)
KINDLE_RELAY_HOST = os.environ.get("KINDLE_RELAY_HOST", "").strip()
KINDLE_RELAY_PORT = os.environ.get("KINDLE_RELAY_PORT", "587").strip()
KINDLE_RELAY_USER = os.environ.get("KINDLE_RELAY_USER", "").strip()
KINDLE_RELAY_PASSWORD = os.environ.get("KINDLE_RELAY_PASSWORD", "")
KINDLE_RELAY_SENDER = os.environ.get("KINDLE_RELAY_SENDER", "").strip()
SHELF_REFRESH_TTL = 21600
OL_LIST_FIELDS = "key,title,author_name,cover_i,cover_id,language"
OL_COVER_FIELDS = f"{OL_LIST_FIELDS},editions,editions.title,editions.language,editions.covers,editions.cover_i,editions.cover_id"
OL_IDENTITY_FIELDS = f"{OL_COVER_FIELDS},alternative_title,isbn,editions.author_name,editions.isbn_10,editions.isbn_13"
# Backwards-compatible name for list/cover consumers; identity fields are
# intentionally reserved for a single work-detail lookup.
OL_BOOK_FIELDS = OL_COVER_FIELDS
OL_DISCOVERY_IDENTIFIER_FIELDS = f"{OL_LIST_FIELDS},isbn"
OL_COVER_IDENTIFIER_FIELDS = f"{OL_COVER_FIELDS},isbn"
OL_SIMILAR_FIELDS = f"{OL_LIST_FIELDS},subject"
SHELF_BOOK_TARGET = 40
SHELF_SEARCH_LIMIT = 100
SHELF_MAX_OPEN_LIBRARY_PAGES = 25
SHELF_REFILL_OPEN_LIBRARY_PAGES = 4
DISCOVERY_PAGE_SIZE = 30
DISCOVERY_SEARCH_LIMIT = DISCOVERY_PAGE_SIZE
DISCOVERY_RAW_PREFIX_LIMIT = 5
DISCOVERY_COVER_PAGE_SIZE = DISCOVERY_PAGE_SIZE - DISCOVERY_RAW_PREFIX_LIMIT
DOWNLOAD_ALIAS_SEARCH_LIMIT = 6
DOWNLOAD_ALIAS_BATCH_SIZE = 2
DOWNLOAD_IDENTITY_VALUE_LIMIT = 12
SIMILAR_MAX_ORIGIN_QUERIES = 3
SIMILAR_EMPTY_TTL = 1800
SIMILAR_PARTIAL_TTL = 60
TOPIC_DISCOVERY_PAGE_SIZE = 30
TOPIC_DISCOVERY_START_COUNT = 6
TOPIC_DISCOVERY_WINDOW = 126
TOPIC_MERGED_FRESH_TTL = 1800
TOPIC_MERGED_STALE_TTL = 86400
TOPIC_PARTIAL_FRESH_TTL = 90
TOPIC_LOCAL_CORPUS_TTL = 300
TOPIC_LOCAL_CORPUS_MAX_ROWS = 1600
TOPIC_LOCAL_CORPUS_MAX_RECORDS = 12000
TOPIC_LOCAL_CORPUS_MATCH_LIMIT = 100
TOPIC_LOCAL_READY_CANDIDATES = 4
TOPIC_PROVIDER_WAIT_TIMEOUT = max(
    3.0,
    min(float(os.environ.get("TOPIC_PROVIDER_WAIT_TIMEOUT", "10")), 15.0),
)
INVENTAIRE_FRESH_TTL = 21600
INVENTAIRE_STALE_TTL = 604800
INVENTAIRE_CONNECT_TIMEOUT = max(
    1.0,
    min(float(os.environ.get("INVENTAIRE_CONNECT_TIMEOUT", "2.5")), 5.0),
)
INVENTAIRE_READ_TIMEOUT = max(
    2.0,
    min(float(os.environ.get("INVENTAIRE_READ_TIMEOUT", "5")), 10.0),
)
INVENTAIRE_MIN_INTERVAL = max(
    0.34,
    float(os.environ.get("INVENTAIRE_MIN_INTERVAL", "0.5")),
)
IDENTITY_QUERY_JSON_MAX_BYTES = 512
IDENTITY_QUERY_VALUE_MAX_CHARS = 120
TRUST_PROXY_HEADERS = os.environ.get("LIBFLIX_TRUST_PROXY_HEADERS", "0").strip().casefold() not in {
    "0", "false", "no", "off",
}

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
    "steve jobs": ["Steve Jobs:A Biography", "史蒂夫·乔布斯传"],
    "the big short": ["大空头"],
}
KNOWN_WORK_METADATA = {
    "OL16085155W": {
        "title": "Steve Jobs",
        "localized_title": "史蒂夫·乔布斯传",
        "download_title": "史蒂夫·乔布斯传",
        "author": "Walter Isaacson",
        "cover_url": "/olcover/12374726/M.webp",
    },
}
GENERIC_SIMILAR_SUBJECTS = {
    "action/adventure", "biography", "business", "competition", "contests",
    "fantasy", "fiction", "games", "health & fitness", "history", "independence",
    "interdependence", "interpersonal relations", "juvenile fiction",
    "juvenile works", "new york times bestseller", "open library staff picks",
    "personal narratives",
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

def clean_topics_url(mode=None, lang=None):
    return f"{clean_prefix('nonfiction', lang)}/topics"

def clean_discover_url(mode=None, lang=None):
    return f"{clean_prefix(mode, lang)}/discover"

def topic_discover_url(query, mode=None, lang=None):
    return clean_discover_url("nonfiction", lang) + "?" + urlencode({
        "q": str(query or "").strip(),
        "intent": "topic",
        "type": "nonfiction",
    })

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
    if endpoint == "topics_page":
        return clean_topics_url(mode, lang)
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
OL_CONTACT = os.environ.get("LIBFLIX_CONTACT", "https://github.com/ianchadson/LibFlix")
SESSION.headers.update({"User-Agent": f"LibFlix/1.0 ({OL_CONTACT})"})
DISK_CACHE_LOCK = threading.Lock()
CHINESE_TITLE_LOOKUP_SEMAPHORE = threading.BoundedSemaphore(4)
SHELF_REFRESH_LOCK = threading.Lock()
SHELF_REFRESHING = set()
SQLITE_CACHE_READY = False
BOOK_HINTS = {}
BOOK_HINTS_LOCK = threading.Lock()
OPENCC_T2S = OpenCC("t2s")
OL_GATEWAY_LOCK = threading.Lock()
OL_STATE_LOCK = threading.Lock()
OL_INFLIGHT_LOCK = threading.Lock()
OL_INFLIGHT = {}
OL_REFRESHING = set()
OL_REFRESH_LOCK = threading.Lock()
OL_REFRESH_PENDING_LIMIT = max(
    2,
    int(os.environ.get("LIBFLIX_OL_REFRESH_PENDING_LIMIT", "12")),
)
OL_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="openlibrary")
OL_LAST_REQUEST_AT = 0.0
OL_FAILURES = 0
OL_CIRCUIT_OPEN_UNTIL = 0.0
OL_MIN_INTERVAL = max(0.34, float(os.environ.get("OPENLIBRARY_MIN_INTERVAL", "1.05")))
OL_CONNECT_TIMEOUT = max(1.0, float(os.environ.get("OPENLIBRARY_CONNECT_TIMEOUT", "3")))
OL_READ_TIMEOUT = max(2.0, float(os.environ.get("OPENLIBRARY_READ_TIMEOUT", "15")))
OL_CIRCUIT_FAILURE_THRESHOLD = 3
OL_CIRCUIT_COOLDOWN = 60
INVENTAIRE_GATEWAY_LOCK = threading.Lock()
INVENTAIRE_STATE_LOCK = threading.Lock()
INVENTAIRE_INFLIGHT_LOCK = threading.Lock()
INVENTAIRE_INFLIGHT = {}
INVENTAIRE_REFRESH_LOCK = threading.Lock()
INVENTAIRE_REFRESHING = set()
INVENTAIRE_REFRESH_PENDING_LIMIT = max(
    2,
    int(os.environ.get("LIBFLIX_INVENTAIRE_REFRESH_PENDING_LIMIT", "12")),
)
INVENTAIRE_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="inventaire")
INVENTAIRE_LAST_REQUEST_AT = 0.0
INVENTAIRE_FAILURES = 0
INVENTAIRE_CIRCUIT_OPEN_UNTIL = 0.0
INVENTAIRE_CIRCUIT_FAILURE_THRESHOLD = 3
INVENTAIRE_CIRCUIT_COOLDOWN = 60
TOPIC_OL_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="topic-openlibrary")
TOPIC_INVENTAIRE_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="topic-inventaire")
TOPIC_OL_SLOTS = threading.BoundedSemaphore(6)
TOPIC_INVENTAIRE_SLOTS = threading.BoundedSemaphore(6)
TOPIC_LOCAL_CORPUS_LOCK = threading.Lock()
TOPIC_LOCAL_CORPUS_DATABASE = ""
TOPIC_LOCAL_CORPUS_BUILT_AT = 0.0
TOPIC_LOCAL_CORPUS_RECORDS = ()
BOOK_DETAIL_REFRESHING = set()
BOOK_DETAIL_REFRESH_LOCK = threading.Lock()
BOOK_DETAIL_REFRESH_PENDING_LIMIT = max(2, int(os.environ.get("LIBFLIX_BOOK_REFRESH_PENDING_LIMIT", "16")))
BOOK_DETAIL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="book-detail")
SIMILAR_REFRESHING = set()
SIMILAR_REFRESH_LOCK = threading.Lock()
SIMILAR_REFRESH_PENDING_LIMIT = max(2, int(os.environ.get("LIBFLIX_SIMILAR_REFRESH_PENDING_LIMIT", "12")))
SIMILAR_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="similar-books")
COVER_LOCK_STRIPES = tuple(threading.Lock() for _ in range(64))
COVER_STATE_LOCK = threading.Lock()
COVER_FAILURES = {}
COVER_VALIDATED_FILES = {}
COVER_NEGATIVE_TTL = 300
COVER_FAILURE_LIMIT = 4096
COVER_VALIDATION_LIMIT = 8192
COVER_ORIGIN_SEMAPHORE = threading.BoundedSemaphore(4)
COVER_WARM_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="cover-warm")
COVER_WARM_COORDINATOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cover-warm-batch")
ARCHIVE_DESCRIPTION_LOCKS = tuple(threading.Lock() for _ in range(32))
ARCHIVE_DESCRIPTION_MAX_IDENTIFIERS = 2
KINDLE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kindle-delivery")
KINDLE_LEGACY_SEMAPHORE = threading.BoundedSemaphore(2)

def disk_cache_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


MEMORY_CACHE_PRUNE_LOCK = threading.Lock()
MEMORY_CACHE_NEXT_PRUNE_AT = 0.0
DISK_CACHE_PRUNE_LOCK = threading.Lock()
DISK_CACHE_NEXT_PRUNE_AT = 0.0


def prune_disk_cache(connection, now=None):
    """Bound durable cache age, row count, and total serialized payload size."""

    now = time.time() if now is None else float(now)
    connection.execute(
        "DELETE FROM api_cache WHERE created_at < ?",
        (now - API_CACHE_RETENTION_TTL,),
    )
    count = connection.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0]
    overflow = max(0, count - API_CACHE_MAX_ROWS)
    if overflow:
        connection.execute(
            "DELETE FROM api_cache WHERE rowid IN ("
            "SELECT rowid FROM api_cache ORDER BY created_at ASC LIMIT ?)",
            (overflow,),
        )
    rows = connection.execute(
        "SELECT rowid, LENGTH(payload) FROM api_cache ORDER BY created_at DESC"
    ).fetchall()
    retained_bytes = 0
    remove = []
    for row_id, payload_bytes in rows:
        payload_bytes = max(0, int(payload_bytes or 0))
        if retained_bytes + payload_bytes <= API_CACHE_MAX_BYTES:
            retained_bytes += payload_bytes
        else:
            remove.append((row_id,))
    if remove:
        connection.executemany("DELETE FROM api_cache WHERE rowid = ?", remove)

@contextmanager
def disk_cache_connection(timeout=5):
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    connection = sqlite3.connect(API_SQLITE_CACHE, timeout=timeout)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def initialize_disk_cache():
    global SQLITE_CACHE_READY
    if SQLITE_CACHE_READY:
        return
    with DISK_CACHE_LOCK:
        if SQLITE_CACHE_READY:
            return
        database_exists = os.path.exists(API_SQLITE_CACHE)
        migrated_legacy_cache = False
        with disk_cache_connection(timeout=10) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS api_cache ("
                "cache_key TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS api_cache_created_at ON api_cache(created_at)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS kindle_jobs ("
                "job_id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "status TEXT NOT NULL, events TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS kindle_jobs_updated_at ON kindle_jobs(updated_at)"
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
            prune_disk_cache(connection)
            connection.execute(
                "DELETE FROM kindle_jobs WHERE updated_at < ?",
                (time.time() - 86400,),
            )
        if migrated_legacy_cache:
            try:
                os.unlink(API_DISK_CACHE)
            except OSError:
                pass
        SQLITE_CACHE_READY = True

def disk_cache_entry(key):
    cache_key = disk_cache_key(key)
    initialize_disk_cache()
    try:
        with disk_cache_connection(timeout=5) as connection:
            row = connection.execute(
                "SELECT created_at, payload FROM api_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            return {"created_at": row[0], "age": max(0, time.time() - row[0]), "data": json.loads(row[1])}
    except (sqlite3.Error, ValueError):
        pass
    return None

def disk_cache_get(key, ttl=API_DISK_CACHE_TTL):
    entry = disk_cache_entry(key)
    if not entry or entry["age"] >= ttl:
        return None
    return entry["data"]

def disk_cache_get_stale(key, ttl=API_CACHE_RETENTION_TTL):
    entry = disk_cache_entry(key)
    if not entry or entry["age"] >= ttl:
        return None
    return entry["data"]

def disk_cache_set(key, data):
    global DISK_CACHE_NEXT_PRUNE_AT
    cache_key = disk_cache_key(key)
    initialize_disk_cache()
    try:
        payload = json.dumps(data, separators=(",", ":"))
        if len(payload.encode("utf-8")) > API_CACHE_MAX_PAYLOAD_BYTES:
            return
        now = time.time()
        with disk_cache_connection(timeout=5) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO api_cache(cache_key, created_at, payload) VALUES (?, ?, ?)",
                (cache_key, now, payload),
            )
            with DISK_CACHE_PRUNE_LOCK:
                prune_due = now >= DISK_CACHE_NEXT_PRUNE_AT
                if prune_due:
                    DISK_CACHE_NEXT_PRUNE_AT = now + API_CACHE_PRUNE_INTERVAL
            if prune_due:
                prune_disk_cache(connection, now)
    except (sqlite3.Error, TypeError, ValueError):
        pass


def disk_cache_delete(key):
    cache_key = disk_cache_key(key)
    initialize_disk_cache()
    try:
        with disk_cache_connection(timeout=5) as connection:
            connection.execute(
                "DELETE FROM api_cache WHERE cache_key = ?",
                (cache_key,),
            )
    except sqlite3.Error:
        pass

def cache_get(key, ttl=CACHE_TTL_OL):
    v = CACHE.get(key)
    if v and time.time() - v["t"] < ttl:
        return v["d"]
    return None

def cache_set(key, data):
    global MEMORY_CACHE_NEXT_PRUNE_AT
    now = time.time()
    CACHE[key] = {"d": data, "t": now}
    if len(CACHE) <= MEMORY_CACHE_MAX_ENTRIES and now < MEMORY_CACHE_NEXT_PRUNE_AT:
        return
    with MEMORY_CACHE_PRUNE_LOCK:
        if now >= MEMORY_CACHE_NEXT_PRUNE_AT:
            expired_before = now - API_CACHE_RETENTION_TTL
            for cache_key, item in list(CACHE.items()):
                if float(item.get("t", 0)) < expired_before:
                    CACHE.pop(cache_key, None)
            MEMORY_CACHE_NEXT_PRUNE_AT = now + API_CACHE_PRUNE_INTERVAL
        overflow = len(CACHE) - MEMORY_CACHE_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(CACHE, key=lambda cache_key: CACHE[cache_key].get("t", 0))[:overflow]
            for cache_key in oldest:
                CACHE.pop(cache_key, None)

def add_server_timing(name, started_at=None, duration=None, description=""):
    if not has_request_context():
        return
    if duration is None:
        duration = (time.perf_counter() - started_at) * 1000 if started_at is not None else 0
    timings = getattr(g, "server_timings", None)
    if timings is None:
        timings = []
        g.server_timings = timings
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "", str(name))[:32] or "app"
    safe_description = re.sub(r'["\\\\]', "", str(description))[:80]
    item = f"{safe_name};dur={max(0, duration):.1f}"
    if safe_description:
        item += f';desc="{safe_description}"'
    timings.append(item)


def bounded_upstream_json(response, maximum=UPSTREAM_JSON_MAX_BYTES):
    """Decode a JSON object without allowing an origin to fill worker memory."""
    content_length = response.headers.get("Content-Length", "")
    if content_length:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError):
            declared_length = 0
        if declared_length > maximum:
            raise ValueError("Upstream JSON response is too large")
    chunks = []
    received = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        received += len(chunk)
        if received > maximum:
            raise ValueError("Upstream JSON response is too large")
        chunks.append(chunk)
    data = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Upstream JSON response is not an object")
    return data


def openlibrary_payload_valid(path, data):
    if not isinstance(data, dict):
        return False
    if path == "/search.json":
        return isinstance(data.get("docs"), list)
    if path.endswith("/editions.json"):
        return isinstance(data.get("entries"), list)
    return True


def inventaire_payload_valid(path, data):
    if not isinstance(data, dict):
        return False
    if path == "/search":
        return isinstance(data.get("results"), list)
    if path == "/entities/by-uris":
        return isinstance(data.get("entities"), dict)
    return True


def purge_provider_cache(key):
    CACHE.pop(key, None)
    disk_cache_delete(key)

def openlibrary_status():
    with OL_STATE_LOCK:
        return {
            "circuit_open": time.monotonic() < OL_CIRCUIT_OPEN_UNTIL,
            "failures": OL_FAILURES,
            "retry_after": max(0, round(OL_CIRCUIT_OPEN_UNTIL - time.monotonic())),
        }

def _openlibrary_failure():
    global OL_FAILURES, OL_CIRCUIT_OPEN_UNTIL
    with OL_STATE_LOCK:
        OL_FAILURES += 1
        if OL_FAILURES >= OL_CIRCUIT_FAILURE_THRESHOLD:
            OL_CIRCUIT_OPEN_UNTIL = time.monotonic() + OL_CIRCUIT_COOLDOWN

def _openlibrary_success():
    global OL_FAILURES, OL_CIRCUIT_OPEN_UNTIL
    with OL_STATE_LOCK:
        OL_FAILURES = 0
        OL_CIRCUIT_OPEN_UNTIL = 0.0

def _openlibrary_request(path, params=None):
    global OL_LAST_REQUEST_AT
    if openlibrary_status()["circuit_open"]:
        return None
    started = time.perf_counter()
    try:
        with OL_GATEWAY_LOCK:
            wait_for = OL_MIN_INTERVAL - (time.monotonic() - OL_LAST_REQUEST_AT)
            if wait_for > 0:
                time.sleep(wait_for)
            OL_LAST_REQUEST_AT = time.monotonic()
        response = SESSION.get(
            f"{OL}{path}",
            params=params,
            timeout=(OL_CONNECT_TIMEOUT, OL_READ_TIMEOUT),
            stream=True,
        )
        try:
            response.raise_for_status()
            data = bounded_upstream_json(response)
            if not openlibrary_payload_valid(path, data):
                raise ValueError("Open Library returned an invalid schema")
        finally:
            response.close()
        _openlibrary_success()
        add_server_timing("openlibrary", started, description="origin")
        return data
    except (requests.RequestException, ValueError):
        _openlibrary_failure()
        add_server_timing("openlibrary", started, description="failed")
        return None

def _refresh_ol_cache(key, path, params):
    try:
        data = _openlibrary_request(path, params)
        if data is not None:
            cache_set(key, data)
            disk_cache_set(key, data)
    finally:
        with OL_REFRESH_LOCK:
            OL_REFRESHING.discard(key)

def schedule_ol_refresh(key, path, params=None):
    with OL_REFRESH_LOCK:
        if (
            key in OL_REFRESHING
            or len(OL_REFRESHING) >= OL_REFRESH_PENDING_LIMIT
            or openlibrary_status()["circuit_open"]
        ):
            return False
        OL_REFRESHING.add(key)
    try:
        OL_REFRESH_EXECUTOR.submit(_refresh_ol_cache, key, path, params)
    except RuntimeError:
        with OL_REFRESH_LOCK:
            OL_REFRESHING.discard(key)
        return False
    return True

def ol_get(path, params=None, allow_stale=True):
    key = f"ol:{path}:{str(params)}"
    cached = cache_get(key, CACHE_TTL_OL)
    if cached is not None:
        if openlibrary_payload_valid(path, cached):
            add_server_timing("olcache", duration=0, description="memory")
            return cached
        purge_provider_cache(key)
    cached = disk_cache_get(key)
    if cached is not None:
        if openlibrary_payload_valid(path, cached):
            cache_set(key, cached)
            add_server_timing("olcache", duration=0, description="disk")
            return cached
        purge_provider_cache(key)
    stale = disk_cache_get_stale(key, OL_STALE_TTL) if allow_stale else None
    if stale is not None:
        if openlibrary_payload_valid(path, stale):
            schedule_ol_refresh(key, path, params)
            add_server_timing("olcache", duration=0, description="stale")
            return stale
        purge_provider_cache(key)

    with OL_INFLIGHT_LOCK:
        event = OL_INFLIGHT.get(key)
        leader = event is None
        if leader:
            event = threading.Event()
            OL_INFLIGHT[key] = event
    if not leader:
        event.wait(OL_CONNECT_TIMEOUT + OL_READ_TIMEOUT + 2)
        shared = cache_get(key, CACHE_TTL_OL) or disk_cache_get(key)
        if shared is not None and openlibrary_payload_valid(path, shared):
            add_server_timing("olcache", duration=0, description="shared")
            return shared
        if shared is not None:
            purge_provider_cache(key)
        return None

    try:
        data = _openlibrary_request(path, params)
        if data is not None:
            cache_set(key, data)
            disk_cache_set(key, data)
        return data
    finally:
        with OL_INFLIGHT_LOCK:
            inflight = OL_INFLIGHT.pop(key, None)
            if inflight:
                inflight.set()

def ol_get_work(ol_key):
    return ol_get(ol_key + ".json")


def inventaire_status():
    with INVENTAIRE_STATE_LOCK:
        return {
            "circuit_open": time.monotonic() < INVENTAIRE_CIRCUIT_OPEN_UNTIL,
            "failures": INVENTAIRE_FAILURES,
            "retry_after": max(
                0,
                round(INVENTAIRE_CIRCUIT_OPEN_UNTIL - time.monotonic()),
            ),
        }


def _inventaire_failure():
    global INVENTAIRE_FAILURES, INVENTAIRE_CIRCUIT_OPEN_UNTIL
    with INVENTAIRE_STATE_LOCK:
        INVENTAIRE_FAILURES += 1
        if INVENTAIRE_FAILURES >= INVENTAIRE_CIRCUIT_FAILURE_THRESHOLD:
            INVENTAIRE_CIRCUIT_OPEN_UNTIL = (
                time.monotonic() + INVENTAIRE_CIRCUIT_COOLDOWN
            )


def _inventaire_success():
    global INVENTAIRE_FAILURES, INVENTAIRE_CIRCUIT_OPEN_UNTIL
    with INVENTAIRE_STATE_LOCK:
        INVENTAIRE_FAILURES = 0
        INVENTAIRE_CIRCUIT_OPEN_UNTIL = 0.0


def _provider_params_key(params):
    if not params:
        return ""
    if isinstance(params, dict):
        values = sorted(params.items(), key=lambda item: str(item[0]))
    else:
        values = list(params)
    return urlencode(values, doseq=True)


def _inventaire_request(path, params=None):
    global INVENTAIRE_LAST_REQUEST_AT
    if inventaire_status()["circuit_open"]:
        return None
    started = time.perf_counter()
    try:
        with INVENTAIRE_GATEWAY_LOCK:
            wait_for = INVENTAIRE_MIN_INTERVAL - (
                time.monotonic() - INVENTAIRE_LAST_REQUEST_AT
            )
            if wait_for > 0:
                time.sleep(wait_for)
            INVENTAIRE_LAST_REQUEST_AT = time.monotonic()
        response = SESSION.get(
            f"{INVENTAIRE}{path}",
            params=params,
            timeout=(INVENTAIRE_CONNECT_TIMEOUT, INVENTAIRE_READ_TIMEOUT),
            stream=True,
        )
        try:
            response.raise_for_status()
            data = bounded_upstream_json(response)
            if not inventaire_payload_valid(path, data):
                raise ValueError("Inventaire returned an invalid schema")
        finally:
            response.close()
        _inventaire_success()
        add_server_timing("inventaire", started, description="origin")
        return data
    except (requests.RequestException, ValueError):
        _inventaire_failure()
        add_server_timing("inventaire", started, description="failed")
        return None


def _refresh_inventaire_cache(key, path, params):
    try:
        data = _inventaire_request(path, params)
        if data is not None:
            cache_set(key, data)
            disk_cache_set(key, data)
    finally:
        with INVENTAIRE_REFRESH_LOCK:
            INVENTAIRE_REFRESHING.discard(key)


def schedule_inventaire_refresh(key, path, params=None):
    with INVENTAIRE_REFRESH_LOCK:
        if (
            key in INVENTAIRE_REFRESHING
            or len(INVENTAIRE_REFRESHING) >= INVENTAIRE_REFRESH_PENDING_LIMIT
            or inventaire_status()["circuit_open"]
        ):
            return False
        INVENTAIRE_REFRESHING.add(key)
    try:
        INVENTAIRE_REFRESH_EXECUTOR.submit(
            _refresh_inventaire_cache,
            key,
            path,
            params,
        )
    except RuntimeError:
        with INVENTAIRE_REFRESH_LOCK:
            INVENTAIRE_REFRESHING.discard(key)
        return False
    return True


def inventaire_get(path, params=None, allow_stale=True):
    """Cached, coalesced Inventaire request with independent failure state."""
    key = f"inventaire:{path}:{_provider_params_key(params)}"
    cached = cache_get(key, INVENTAIRE_FRESH_TTL)
    if cached is not None:
        if inventaire_payload_valid(path, cached):
            add_server_timing("invcache", duration=0, description="memory")
            return cached
        purge_provider_cache(key)
    cached = disk_cache_get(key, INVENTAIRE_FRESH_TTL)
    if cached is not None:
        if inventaire_payload_valid(path, cached):
            cache_set(key, cached)
            add_server_timing("invcache", duration=0, description="disk")
            return cached
        purge_provider_cache(key)
    stale = (
        disk_cache_get_stale(key, INVENTAIRE_STALE_TTL)
        if allow_stale else None
    )
    if stale is not None:
        if inventaire_payload_valid(path, stale):
            schedule_inventaire_refresh(key, path, params)
            add_server_timing("invcache", duration=0, description="stale")
            return stale
        purge_provider_cache(key)

    with INVENTAIRE_INFLIGHT_LOCK:
        event = INVENTAIRE_INFLIGHT.get(key)
        leader = event is None
        if leader:
            event = threading.Event()
            INVENTAIRE_INFLIGHT[key] = event
    if not leader:
        event.wait(INVENTAIRE_CONNECT_TIMEOUT + INVENTAIRE_READ_TIMEOUT + 1)
        shared = (
            cache_get(key, INVENTAIRE_FRESH_TTL)
            or disk_cache_get(key, INVENTAIRE_FRESH_TTL)
        )
        if shared is not None and inventaire_payload_valid(path, shared):
            return shared
        if shared is not None:
            purge_provider_cache(key)
        return None

    try:
        data = _inventaire_request(path, params)
        if data is not None:
            cache_set(key, data)
            disk_cache_set(key, data)
        return data
    finally:
        with INVENTAIRE_INFLIGHT_LOCK:
            inflight = INVENTAIRE_INFLIGHT.pop(key, None)
            if inflight:
                inflight.set()

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

TOPIC_BROWSE_INDEX = {
    topic["query"]: topic
    for group in TOPIC_BROWSE_GROUPS
    for topic in group["topics"]
}
TOPIC_FEATURED = tuple(
    TOPIC_BROWSE_INDEX[query]
    for query in FEATURED_TOPIC_QUERIES
)

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

def search_record_language_codes(record):
    codes = set()
    for field in ("language", "languages"):
        values = record.get(field) or []
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if isinstance(value, dict):
                value = value.get("key") or value.get("code") or ""
            code = str(value or "").strip().lower().rsplit("/", 1)[-1]
            if code:
                codes.add(code)
    return codes

def discovery_fallback_matches_language(record, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    desired = BOOK_LANG_CONFIG[lang]["ol_lang"]
    records = [record, *((record.get("editions") or {}).get("docs") or [])]
    known_codes = set()
    for candidate in records:
        codes = search_record_language_codes(candidate)
        known_codes.update(codes)
        if desired in codes:
            return True
    if known_codes:
        return False
    return title_matches_lang(record.get("title", ""), lang)

def first_matching_edition(w, lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    editions = (w.get("editions") or {}).get("docs", [])

    def best(candidates):
        candidates = list(candidates)
        return next(
            (edition for edition in candidates if edition_cover_id(edition)),
            candidates[0] if candidates else None,
        )

    if lang == "cn":
        matched = best(
            ed for ed in editions
            if record_has_lang(ed, lang) and is_chinese_title(ed.get("title", ""))
        )
        if matched:
            return matched
        matched = best(ed for ed in editions if is_chinese_title(ed.get("title", "")))
        if matched:
            return matched
    matched = best(ed for ed in editions if record_has_lang(ed, lang))
    if matched:
        return matched
    matched = best(ed for ed in editions if title_matches_lang(ed.get("title", ""), lang))
    if matched:
        return matched
    return None

def valid_cover_id(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None

def edition_cover_id(ed):
    covers = ed.get("covers")
    if isinstance(covers, list):
        for cover in covers:
            cover_id = valid_cover_id(cover)
            if cover_id:
                return cover_id
    return valid_cover_id(ed.get("cover_i")) or valid_cover_id(ed.get("cover_id"))

def open_library_cover_url(cover_id, size="M"):
    cover_id = valid_cover_id(cover_id)
    if not cover_id:
        return ""
    size = size if size in ("S", "M", "L") else "M"
    return f"/olcover/{cover_id}/{size}.webp"

def edition_archive_identifier(edition):
    values = edition.get("ocaid") or []
    if not isinstance(values, list):
        values = [values]
    for value in values:
        identifier = str(value or "").strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", identifier):
            return identifier
    return ""

def archive_cover_url(identifier, size="M"):
    identifier = str(identifier or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", identifier):
        return ""
    size = size if size in ("S", "M", "L") else "M"
    return f"/iacover/{identifier}/{size}.webp"

def localize_cover_url(url, size="M"):
    url = str(url or "")
    local = re.fullmatch(r"/olcover/(\d+)(?:/[SML](?:\.webp)?)?", url)
    archive = re.fullmatch(r"/iacover/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?:/[SML](?:\.webp)?)?", url)
    if archive:
        return archive_cover_url(archive.group(1), size)
    remote = re.search(r"covers\.openlibrary\.org/b/id/(\d+)-[SML]\.jpg", url)
    match = local or remote
    return open_library_cover_url(match.group(1), size) if match else url

def canonicalize_book_covers(books, size="M"):
    """Upgrade cached legacy cover paths at every JSON/HTML boundary."""
    normalized = []
    for book in books or []:
        book_copy = dict(book)
        book_copy["cover_url"] = localize_cover_url(book_copy.get("cover_url", ""), size)
        normalized.append(book_copy)
    return normalized

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

def preferred_work_editions(entries, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    desired = BOOK_LANG_CONFIG[lang]["ol_lang"]

    def rank(item):
        index, edition = item
        codes = edition_language_codes(edition)
        title = str(edition.get("title") or "")
        return (
            desired in codes,
            title_matches_lang(title, lang),
            bool(edition_cover_id(edition)),
            bool(edition_archive_identifier(edition)),
            -index,
        )

    return [
        edition for _, edition in sorted(
            enumerate(entries or []),
            key=rank,
            reverse=True,
        )
    ]

def work_cover_id(work):
    for value in (work or {}).get("covers") or []:
        cover_id = valid_cover_id(value)
        if cover_id:
            return cover_id
    return ""

def bounded_identity_values(values, limit=DOWNLOAD_IDENTITY_VALUE_LIMIT):
    """Return compact, stable identity signals without trusting provider size."""
    unique = []
    seen = set()
    for value in values or []:
        if isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            candidates = [value]
        for candidate in candidates:
            candidate = re.sub(r"\s+", " ", str(candidate or "")).strip(" /|｜-–—")
            if not candidate or len(candidate) > 180:
                continue
            key = unicodedata.normalize("NFKC", candidate).casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
            if len(unique) >= limit:
                return unique
    return unique

def normalize_isbn(value):
    value = re.sub(r"[^0-9Xx]", "", str(value or ""))
    return value.upper() if len(value) in (10, 13) else ""

def collect_book_identity_metadata(metadata=None, work=None, search_record=None, edition=None):
    """Collect title, author and ISBN aliases already present in OL responses."""
    metadata = metadata or {}
    work = work or {}
    search_record = search_record or {}
    edition = edition or {}
    edition_docs = (search_record.get("editions") or {}).get("docs") or []

    title_values = [
        metadata.get("download_title"),
        metadata.get("localized_title"),
        metadata.get("title"),
        metadata.get("title_aliases"),
        edition.get("title"),
        search_record.get("title"),
        search_record.get("alternative_title"),
        work.get("title"),
        work.get("other_titles"),
    ]
    for source in (work, search_record, edition, *edition_docs):
        title = str(source.get("title") or "").strip()
        subtitle = str(source.get("subtitle") or "").strip()
        if title and subtitle:
            title_values.append(f"{title}: {subtitle}")
    title_values.extend(item.get("title") for item in edition_docs)

    author_values = [
        metadata.get("authors"),
        metadata.get("author"),
        search_record.get("author_name"),
        edition.get("author_name"),
    ]
    author_values.extend(item.get("author_name") for item in edition_docs)

    isbn_values = [metadata.get("isbns"), search_record.get("isbn")]
    for source in (edition, *edition_docs):
        isbn_values.extend((source.get("isbn_13"), source.get("isbn_10")))
    raw_isbns = bounded_identity_values(isbn_values, limit=24)
    isbns = bounded_identity_values(
        [normalized for value in raw_isbns if (normalized := normalize_isbn(value))],
        limit=8,
    )
    return {
        "title_aliases": bounded_identity_values(title_values),
        "authors": bounded_identity_values(author_values, limit=6),
        "isbns": isbns,
    }

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
        *(metadata.get("title_aliases") or []),
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
    for isbn in metadata.get("isbns") or []:
        add(isbn)
    return queries[:10]

def english_download_queries(metadata=None):
    metadata = metadata or {}
    title = str(
        metadata.get("download_title") or metadata.get("title") or ""
    ).strip()
    authors = bounded_identity_values([
        metadata.get("authors"),
        metadata.get("author"),
    ], limit=6)
    author = authors[0] if authors else ""
    titles = bounded_identity_values([
        title,
        metadata.get("title_aliases"),
        metadata.get("localized_title"),
        metadata.get("title"),
    ])
    queries = []

    def add(value):
        value = re.sub(r"\s+", " ", str(value or "")).strip(" /|｜-–—")
        if value and value not in queries:
            queries.append(value)

    add(f"{title} {author}" if author else title)
    add(title)
    alternate_titles = [
        alternate for alternate in titles
        if normalize_title(alternate) != normalize_title(title)
    ]
    if alternate_titles:
        add(f"{alternate_titles[0]} {author}" if author else alternate_titles[0])
    for isbn in (metadata.get("isbns") or [])[:2]:
        add(isbn)
    for alternate in alternate_titles:
        add(alternate)
    for alternate_author in authors[1:]:
        add(f"{title} {alternate_author}")
    cleaned = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title).strip()
    add(f"{cleaned} {author}" if author and cleaned != title else cleaned)
    base_title = re.split(r"\s*[:–—]\s*", cleaned, maxsplit=1)[0].strip()
    if len(base_title) >= 4 and base_title != cleaned:
        add(f"{base_title} {author}" if author else base_title)
        add(base_title)
    return queries[:6]

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
    catalog_qualified = []
    seen = set()
    for index, subject in enumerate(subjects):
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
        catalog_metadata = normalized.startswith(("united states,", "great britain,")) or any(
            qualifier in normalized
            for qualifier in (
                "juvenile literature",
                ", biography",
                ", fiction",
                ", history",
                ", personal narratives",
            )
        )
        target = catalog_qualified if catalog_metadata else candidates
        target.append((index, subject))

    def subject_terms(value):
        return [
            term for term in re.findall(r"[a-z0-9]+", value.casefold())
            if term not in {"and", "of", "the"}
        ]

    def subject_family(value):
        aliases = {"crises": "crisis"}
        family = []
        for term in subject_terms(value):
            term = aliases.get(term, term)
            if len(term) > 5 and term.endswith("s") and not term.endswith("ss"):
                term = term[:-1]
            if term not in family:
                family.append(term)
        return tuple(family)

    ranked = sorted(
        candidates,
        key=lambda item: (len(subject_terms(item[1])) < 2, item[0]),
    )
    ranked.extend(sorted(
        catalog_qualified,
        key=lambda item: (len(subject_terms(item[1])) < 2, item[0]),
    ))
    selected = []
    seen_families = set()
    for _, subject in ranked:
        family = subject_family(subject)
        if not family or family in seen_families:
            continue
        seen_families.add(family)
        selected.append(subject)
        if len(selected) == 2:
            break
    return selected or subjects[:1]

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

def extract_book(w, lang=None, allow_missing_cover=False):
    lang = lang or DEFAULT_BOOK_LANG
    edition = first_matching_edition(w, lang)
    title = (edition or {}).get("title") or w.get("title", "")
    if not title:
        return None
    if lang == "en" and not title_matches_lang(title, lang):
        return None
    if lang == "cn" and not (title_matches_lang(title, lang) or record_has_lang(w, lang) or record_has_lang(edition or {}, lang)):
        return None
    cover_id = (
        edition_cover_id(edition or {})
        or valid_cover_id(w.get("cover_i"))
        or valid_cover_id(w.get("cover_id"))
    )
    if not cover_id and lang != "cn" and not allow_missing_cover:
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
    result = {
        "title": title,
        "localized_title": localized_title,
        "download_title": download_title,
        "author": hint.get("author") or english_hint.get("author", ""),
        "cover_url": hint.get("cover_url") or english_hint.get("cover_url", ""),
        "ol_key": ol_key,
    }
    result.update(collect_book_identity_metadata(result))
    return result

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
    if cached and (cached[0] or page > 1):
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
    if result[0]:
        cache_set(ckey, result)
    elif page == 1:
        shelf = next(
            (item for item in disk_load_shelves(mode, lang) or [] if item.get("topic") == topic),
            None,
        )
        if shelf and shelf.get("books"):
            result = (shelf["books"][:SHELF_BOOK_TARGET], len(shelf["books"]), 1)
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

DISCOVERY_STOP_WORDS = frozenset({
    "a", "about", "an", "and", "at", "book", "books", "by", "for", "from",
    "in", "of", "on", "or", "the", "to", "with",
})

def normalize_relevance_text(value):
    value = normalize_match_text(value)
    value = re.sub(r"\bartificial\s+intelligence\b", "ai", value)
    value = re.sub(r"\ba\s+i\b", "ai", value)
    return value

def relevance_tokens(value):
    tokens = normalize_relevance_text(value).split()
    meaningful = [token for token in tokens if token not in DISCOVERY_STOP_WORDS]
    return meaningful or tokens

def discovery_identifier(value):
    isbn = normalize_isbn(value)
    if isbn:
        return "isbn", isbn
    work_id = work_id_from_ol_key(str(value or "").strip().upper())
    return ("work", work_id) if work_id else ("", "")

def discovery_record_relevance(record, query):
    """Score local title/author evidence and reject provider filler."""
    identifier_type, identifier = discovery_identifier(query)
    if identifier_type == "isbn":
        editions = (record.get("editions") or {}).get("docs") or []
        isbn_values = [record.get("isbn")]
        for edition in editions:
            isbn_values.extend((edition.get("isbn_10"), edition.get("isbn_13")))
        candidate_isbns = bounded_identity_values(isbn_values, limit=24)
        if identifier in {normalize_isbn(value) for value in candidate_isbns}:
            return 2200
        return 0
    if identifier_type == "work":
        return 2200 if work_id_from_ol_key(record.get("key", "")) == identifier else 0

    query_text = normalize_relevance_text(query)
    if not query_text:
        return 0
    query_tokens = set(relevance_tokens(query))
    editions = (record.get("editions") or {}).get("docs") or []
    titles = bounded_identity_values([
        record.get("title"),
        record.get("alternative_title"),
        [edition.get("title") for edition in editions],
    ])
    authors = bounded_identity_values([
        record.get("author_name"),
        [edition.get("author_name") for edition in editions],
    ], limit=8)
    best_score = 0
    best_title_evidence = set()
    best_fuzzy_ratio = 0.0
    best_fuzzy_min_length = 0
    for title in titles:
        title_text = normalize_relevance_text(title)
        title_tokens = set(relevance_tokens(title))
        title_evidence = query_tokens & title_tokens
        overlap = len(title_evidence)
        if len(title_evidence) > len(best_title_evidence):
            best_title_evidence = title_evidence
        coverage = overlap / max(len(query_tokens), 1)
        precision = overlap / max(len(title_tokens), 1)
        score = round(520 * coverage + 180 * precision)
        if title_text == query_text:
            score += 700
        elif query_text in title_text or title_text in query_text:
            score += 360
        else:
            ratio = SequenceMatcher(None, title_text, query_text).ratio()
            if ratio > best_fuzzy_ratio:
                best_fuzzy_ratio = ratio
                best_fuzzy_min_length = min(len(query_text), len(title_text))
            if ratio >= 0.62:
                score += round(220 * ratio)
        best_score = max(best_score, score)
    author_tokens = set(relevance_tokens(" ".join(authors)))
    author_evidence = query_tokens & author_tokens
    author_overlap = len(author_evidence)
    combined_evidence = best_title_evidence | author_evidence
    best_score += round(360 * author_overlap / max(len(query_tokens), 1))

    # Open Library can append highly rated but unrelated books. Require at
    # least two thirds of a multi-token identity query to be evidenced by the
    # title/author. Fuzzy similarity only reranks literal candidates.
    minimum_coverage = 1.0 if len(query_tokens) <= 1 else 2 / 3
    if len(combined_evidence) / max(len(query_tokens), 1) < minimum_coverage:
        # A narrow high-confidence typo path preserves useful fuzzy matching
        # without re-admitting merely popular provider filler.
        if best_fuzzy_ratio < 0.88 or best_fuzzy_min_length < 5:
            return 0
        best_score += round(300 * best_fuzzy_ratio)
    return best_score

def rank_discovery_records(records, query):
    ranked = []
    for index, record in enumerate(records or []):
        score = discovery_record_relevance(record, query)
        if score <= 0:
            continue
        ranked.append((score, index, record))
    return [
        record for score, index, record in sorted(
            ranked,
            key=lambda item: (-item[0], item[1]),
        )
    ]


TOPIC_FILTER_VALUES = {
    "type": {"any", "nonfiction", "fiction"},
    "language": {"current", "any", "en", "cn"},
    "published": {"any", "recent", "classic"},
    "sort": {"best", "newest"},
}
TOPIC_FILTER_DEFAULTS = {
    "type": "any",
    "language": "current",
    "published": "any",
    "sort": "best",
}
INVENTAIRE_CLAIM_TERMS = {
    "wd:Q108458": "meditation",
    "wd:Q341045": "mindfulness",
    "wd:Q6501338": "attention",
    "wd:Q129238": "startups",
    "wd:Q3908516": "entrepreneurship",
}


def normalize_topic_filters(values=None):
    values = values or {}
    normalized = {}
    for name, allowed in TOPIC_FILTER_VALUES.items():
        value = str(values.get(name) or TOPIC_FILTER_DEFAULTS[name]).strip().lower()
        normalized[name] = value if value in allowed else TOPIC_FILTER_DEFAULTS[name]
    return normalized


def topic_discovery_cache_key(query, lang, filters):
    normalized_filters = normalize_topic_filters(filters)
    filter_key = ":".join(normalized_filters[name] for name in (
        "type", "language", "published", "sort",
    ))
    return (
        f"topic-discover:{TOPIC_EXPANSION_VERSION}:{TOPIC_RANKER_VERSION}:"
        f"{lang}:{normalize_topic_text(query)}:{filter_key}"
    )


def _topic_cache_entry(key):
    entries = []
    memory = CACHE.get(key)
    if isinstance(memory, dict) and isinstance(memory.get("d"), dict):
        entries.append({
            "age": max(0, time.time() - float(memory.get("t", 0))),
            "data": memory["d"],
            "source": "memory",
        })
    disk = disk_cache_entry(key)
    if disk and isinstance(disk.get("data"), dict):
        entries.append({**disk, "source": "disk"})
    return min(entries, key=lambda item: item["age"]) if entries else None


def cached_topic_discovery_payload(query, lang, filters, *, allow_stale=True):
    key = topic_discovery_cache_key(query, lang, filters)
    entry = _topic_cache_entry(key)
    if not entry:
        return None
    payload = entry["data"]
    partial = bool(payload.get("partial"))
    fresh_ttl = TOPIC_PARTIAL_FRESH_TTL if partial else TOPIC_MERGED_FRESH_TTL
    if entry["age"] < fresh_ttl:
        if entry.get("source") == "disk":
            cache_set(key, payload)
        return dict(payload)
    if (
        allow_stale
        and not partial
        and entry["age"] < TOPIC_MERGED_STALE_TTL
    ):
        stale = dict(payload)
        stale["stale"] = True
        return stale
    return None


TOPIC_LOCAL_CORPUS_FIELDS = (
    "key", "title", "author_name", "cover_i", "language", "subject",
    "description", "first_publish_year", "ratings_count", "ratings_average",
    "readinglog_count", "osp_count", "edition_count", "isbn",
)
TOPIC_LOCAL_HETEROGENEOUS_SUBJECTS = 16
TOPIC_LOCAL_MAX_TRUSTED_SUBJECTS = 64


def _topic_cached_record_quality(record):
    def number(name):
        try:
            return max(0.0, float(record.get(name) or 0))
        except (TypeError, ValueError):
            return 0.0

    subjects = record.get("subject")
    subject_count = len(subjects) if isinstance(subjects, list) else 0
    return (
        math.log1p(number("readinglog_count")) * 4
        + math.log1p(number("ratings_count")) * 2
        + math.log1p(number("edition_count"))
        + min(subject_count, 20) * 0.12
        + (3 if record.get("cover_i") else 0)
        + (1 if record.get("description") else 0)
    )


def _topic_cached_record_text(record):
    description = record.get("description")
    if isinstance(description, dict):
        description = description.get("value") or description.get("en") or ""
    subjects = record.get("subject")
    if not isinstance(subjects, (list, tuple)):
        subjects = (subjects,) if subjects else ()
    return normalize_topic_text(" ".join((
        str(record.get("title") or "")[:500],
        str(description or "")[:2000],
        " ".join(str(subject)[:200] for subject in subjects[:64]),
    )))


def _topic_cached_record_subjects(record):
    subjects = record.get("subject")
    if not isinstance(subjects, (list, tuple)):
        subjects = (subjects,) if subjects else ()
    return tuple(dict.fromkeys(
        normalized
        for subject in subjects
        if (normalized := normalize_topic_text(str(subject)[:200]))
    ))


def _topic_cached_record_primary_text(record):
    description = record.get("description")
    if isinstance(description, dict):
        description = description.get("value") or description.get("en") or ""
    return normalize_topic_text(" ".join((
        str(record.get("title") or "")[:500],
        str(description or "")[:2000],
    )))


def _topic_cached_record_has_engagement(record):
    for name in (
        "readinglog_count", "ratings_count", "edition_count", "osp_count",
    ):
        try:
            if float(record.get(name) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _topic_cached_record_is_coherent(record, plan):
    """Reject weak one-off matches in noisy saved-provider metadata.

    Open Library occasionally aggregates dozens of unrelated subjects onto one
    work. Those records are useful as a provider-outage fallback only when the
    topic is supported by the title/description, multiple planned queries, or
    measurable engagement. A provider-truncated maximum-size list must have
    primary-text support. Live provider results keep their normal ranking.
    """
    subjects = _topic_cached_record_subjects(record)
    description = record.get("description")
    if isinstance(description, dict):
        description = description.get("value") or description.get("en") or ""
    if not subjects and not str(description or "").strip():
        title = normalize_topic_text(str(record.get("title") or "")[:500])
        original = normalize_topic_text(plan.queries[0]) if plan.queries else ""
        if _topic_cached_text_contains(title, original):
            return True
        if any(
            " " in normalize_topic_text(query)
            and _topic_cached_text_contains(title, query)
            for query in plan.queries
        ):
            return True
        if plan.display_query == "sales" and _topic_cached_text_contains(
            title,
            "selling",
        ):
            return True
        return _topic_cached_record_has_engagement(record)
    if len(subjects) < TOPIC_LOCAL_HETEROGENEOUS_SUBJECTS:
        return True
    primary_text = _topic_cached_record_primary_text(record)
    if any(
        _topic_cached_text_contains(primary_text, query)
        for query in plan.queries
    ):
        return True
    matched_queries = {
        query
        for query in plan.queries
        if any(
            _topic_cached_text_contains(subject, query)
            for subject in subjects
        )
    }
    if len(subjects) >= TOPIC_LOCAL_MAX_TRUSTED_SUBJECTS:
        return False
    if plan.display_query == "productivity" and matched_queries:
        personal_signals = (
            "employee empowerment", "motivation", "leadership",
            "personnel management", "job satisfaction", "time management",
            "conduct of life", "organizational efficiency",
            "self management", "goals",
        )
        industrial_senses = (
            "well productivity", "oil well", "petroleum engineering",
            "work measurement", "industrial engineering", "manufacturing",
        )
        if (
            any(
                _topic_cached_text_contains(subject, signal)
                for subject in subjects
                for signal in personal_signals
            )
            and not any(
                _topic_cached_text_contains(subject, sense)
                for subject in subjects
                for sense in industrial_senses
            )
        ):
            return True
    return (
        len(matched_queries) > 1
        or _topic_cached_record_has_engagement(record)
    )


def _topic_local_openlibrary_corpus():
    """Build a bounded searchable corpus from durable Open Library responses."""
    global TOPIC_LOCAL_CORPUS_DATABASE, TOPIC_LOCAL_CORPUS_BUILT_AT
    global TOPIC_LOCAL_CORPUS_RECORDS

    now = time.monotonic()
    if (
        TOPIC_LOCAL_CORPUS_DATABASE == API_SQLITE_CACHE
        and TOPIC_LOCAL_CORPUS_BUILT_AT > 0
        and now - TOPIC_LOCAL_CORPUS_BUILT_AT < TOPIC_LOCAL_CORPUS_TTL
    ):
        return TOPIC_LOCAL_CORPUS_RECORDS
    with TOPIC_LOCAL_CORPUS_LOCK:
        now = time.monotonic()
        if (
            TOPIC_LOCAL_CORPUS_DATABASE == API_SQLITE_CACHE
            and TOPIC_LOCAL_CORPUS_BUILT_AT > 0
            and now - TOPIC_LOCAL_CORPUS_BUILT_AT < TOPIC_LOCAL_CORPUS_TTL
        ):
            return TOPIC_LOCAL_CORPUS_RECORDS
        initialize_disk_cache()
        rows = []
        try:
            with disk_cache_connection(timeout=5) as connection:
                rows = connection.execute(
                    "SELECT payload FROM api_cache "
                    "WHERE payload LIKE ? ORDER BY created_at DESC LIMIT ?",
                    ('%"docs"%', TOPIC_LOCAL_CORPUS_MAX_ROWS),
                ).fetchall()
        except sqlite3.Error:
            rows = []

        records = {}
        for (serialized,) in rows:
            try:
                payload = json.loads(serialized)
            except (TypeError, ValueError):
                continue
            docs = payload.get("docs") if isinstance(payload, dict) else None
            if not isinstance(docs, list):
                continue
            for raw_record in docs:
                if not isinstance(raw_record, dict):
                    continue
                work_key = str(raw_record.get("key") or "").strip()
                if not re.fullmatch(r"/?works/OL\d+W", work_key, re.I):
                    continue
                record = {
                    field: raw_record[field]
                    for field in TOPIC_LOCAL_CORPUS_FIELDS
                    if field in raw_record
                }
                normalized_key = work_id_from_ol_key(work_key)
                current = records.get(normalized_key)
                if current is not None:
                    if _topic_cached_record_quality(record) > current[1]:
                        records[normalized_key] = (
                            record,
                            _topic_cached_record_quality(record),
                        )
                    continue
                if len(records) >= TOPIC_LOCAL_CORPUS_MAX_RECORDS:
                    continue
                records[normalized_key] = (
                    record,
                    _topic_cached_record_quality(record),
                )

        TOPIC_LOCAL_CORPUS_RECORDS = tuple(
            (record, _topic_cached_record_text(record), quality)
            for record, quality in records.values()
        )
        TOPIC_LOCAL_CORPUS_DATABASE = API_SQLITE_CACHE
        TOPIC_LOCAL_CORPUS_BUILT_AT = time.monotonic()
        return TOPIC_LOCAL_CORPUS_RECORDS


def _topic_cached_text_contains(text, phrase):
    phrase = normalize_topic_text(phrase)
    if not text or not phrase:
        return False
    if re.search(r"[\u3400-\u9fff]", phrase):
        return phrase in text
    return bool(re.search(rf"(?:^| ){re.escape(phrase)}(?:$| )", text))


def _topic_cached_openlibrary_pages(plan, provider_lang, filters):
    started = time.perf_counter()
    corpus = _topic_local_openlibrary_corpus()
    if not corpus:
        return []
    filter_language = filters.get("language") != "any"
    selected_language = (
        provider_lang if provider_lang in BOOK_LANGS else DEFAULT_BOOK_LANG
    )
    newest = filters.get("sort") == "newest"
    pages = []
    for query_rank, query in enumerate(plan.queries):
        matches = []
        for record, search_text, quality in corpus:
            if not _topic_cached_text_contains(search_text, query):
                continue
            if not _topic_cached_record_is_coherent(record, plan):
                continue
            if filter_language and not discovery_fallback_matches_language(
                record,
                selected_language,
            ):
                continue
            try:
                year = int(record.get("first_publish_year") or 0)
            except (TypeError, ValueError):
                year = 0
            matches.append((year if newest else 0, quality, record))
        matches.sort(key=lambda item: (-item[0], -item[1]))
        page = parse_openlibrary_payload(
            {"docs": [item[2] for item in matches[:TOPIC_LOCAL_CORPUS_MATCH_LIMIT]]},
            query,
            query_rank,
        )
        if page.candidates:
            pages.append(page)
    add_server_timing("topiccache", started, description="local-corpus")
    return pages


def _topic_openlibrary_page(plan, query_rank, provider_lang, filters):
    path, params = build_openlibrary_request(
        plan,
        query_rank,
        language=provider_lang,
        limit=50,
    )
    if filters.get("sort") == "newest":
        params["sort"] = "new"
    data = ol_get(path, params)
    query = plan.queries[query_rank]
    if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
        return [parse_openlibrary_payload(None, query, query_rank)]
    data = dict(data)
    records = data["docs"]
    if filters.get("language") != "any":
        selected_lang = (
            provider_lang if provider_lang in BOOK_LANGS else DEFAULT_BOOK_LANG
        )
        data["docs"] = [
            record for record in records
            if isinstance(record, dict)
            and discovery_fallback_matches_language(record, selected_lang)
        ]
    return [parse_openlibrary_payload(data, query, query_rank)]


def _inventaire_entity_params(uris):
    params = [("uris", uri) for uri in uris]
    params.extend(("attributes", attribute) for attribute in (
        "info", "labels", "descriptions", "claims", "image", "popularity",
    ))
    return params


def _inventaire_semantic_term(claim, fallback):
    value = str(claim or "").rsplit("=", 1)[-1]
    return INVENTAIRE_CLAIM_TERMS.get(value, fallback)


def _topic_inventaire_pages(plan, query_rank, provider_lang, filters):
    path, params, semantic = build_inventaire_request(
        plan,
        query_rank,
        language=provider_lang,
        limit=20,
    )
    data = inventaire_get(path, params)
    fallback_query = plan.queries[min(query_rank, len(plan.queries) - 1)]
    claim = next((value for name, value in params if name == "claim"), "")
    semantic_query = _inventaire_semantic_term(claim, fallback_query)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return [
            parse_inventaire_payload(
                None,
                semantic_query,
                query_rank,
                semantic=semantic,
            )
        ]
    data = dict(data)
    raw_results = [
        result for result in data["results"][:20]
        if isinstance(result, dict)
    ]
    uris = []
    for result in raw_results:
        uri = str(result.get("uri") or "").strip()
        if re.fullmatch(r"(?:wd:Q\d+|inv:[a-f0-9]{32})", uri):
            uris.append(uri)
    entity_data = (
        inventaire_get("/entities/by-uris", _inventaire_entity_params(uris))
        if uris else {"entities": {}}
    )
    entities = (
        entity_data.get("entities")
        if isinstance(entity_data, dict)
        and isinstance(entity_data.get("entities"), dict)
        else {}
    )
    hydration_incomplete = bool(
        uris
        and (
            not isinstance(entity_data, dict)
            or not isinstance(entity_data.get("entities"), dict)
            or any(uri not in entities for uri in uris)
        )
    )
    enriched = []
    for result in raw_results:
        uri = str(result.get("uri") or "")
        entity = entities.get(uri) if isinstance(entities, dict) else None
        combined = dict(result)
        if isinstance(entity, dict):
            for name in (
                "labels", "descriptions", "claims", "image", "popularity",
            ):
                if entity.get(name) not in (None, {}, []):
                    combined[name] = entity[name]
            combined["uri"] = uri
        enriched.append(combined)
    data["results"] = enriched
    if isinstance(entity_data, dict) and entity_data.get("warnings"):
        data["warnings"] = entity_data["warnings"]
    inv_page = parse_inventaire_payload(
        data,
        semantic_query,
        query_rank,
        semantic=semantic,
    )
    if hydration_incomplete:
        unavailable = parse_inventaire_payload(
            None,
            semantic_query,
            query_rank,
            semantic=semantic,
        )
        return [inv_page, unavailable]
    return [inv_page]


def _topic_provider_pages(plan, lang, filters, *, fallback_ready=False):
    selected_language = filters.get("language", "current")
    provider_lang = (
        "any" if selected_language == "any"
        else lang if selected_language == "current"
        else selected_language
    )
    futures = {}
    saturated = False

    def submit_bounded(executor, slots, source, function, *args):
        nonlocal saturated
        if not slots.acquire(blocking=False):
            saturated = True
            return

        def run():
            try:
                return function(*args)
            finally:
                slots.release()

        try:
            future = executor.submit(run)
        except RuntimeError:
            slots.release()
            saturated = True
            return
        futures[future] = source

    ol_ranks = list(range(len(plan.queries)))
    if plan.display_query == "focus" and len(ol_ranks) > 1:
        ol_ranks[0], ol_ranks[1] = ol_ranks[1], ol_ranks[0]
    for query_rank in ol_ranks:
        submit_bounded(
            TOPIC_OL_EXECUTOR,
            TOPIC_OL_SLOTS,
            "openlibrary",
            _topic_openlibrary_page,
            plan,
            query_rank,
            provider_lang,
            filters,
        )

    if plan.inventaire_claims:
        raw_rank = len(plan.queries) - 1
        inventaire_ranks = [raw_rank]
        inventaire_ranks.extend(
            rank for rank in range(len(plan.inventaire_claims))
            if rank != raw_rank
        )
        inventaire_ranks = inventaire_ranks[:2]
    else:
        inventaire_ranks = [0]
    for query_rank in inventaire_ranks:
        submit_bounded(
            TOPIC_INVENTAIRE_EXECUTOR,
            TOPIC_INVENTAIRE_SLOTS,
            "inventaire",
            _topic_inventaire_pages,
            plan,
            query_rank,
            provider_lang,
            filters,
        )

    pending = set(futures)
    pages = []
    failed_sources = set()
    deadline = time.monotonic() + TOPIC_PROVIDER_WAIT_TIMEOUT
    grace_deadline = (
        min(deadline, time.monotonic() + 1.35)
        if fallback_ready else None
    )
    while pending and time.monotonic() < deadline:
        current_deadline = min(
            deadline,
            grace_deadline if grace_deadline is not None else deadline,
        )
        timeout = max(0, current_deadline - time.monotonic())
        if timeout <= 0:
            break
        completed, pending = wait(
            pending,
            timeout=timeout,
            return_when=FIRST_COMPLETED,
        )
        if not completed:
            break
        for future in completed:
            source = futures[future]
            try:
                future_pages = future.result()
            except Exception:
                failed_sources.add(source)
                continue
            pages.extend(future_pages)
            openlibrary_ready = any(
                page.provider in {"openlibrary", "inventaire"}
                and page.available
                and page.candidates
                for page in future_pages
                if page.provider == "openlibrary"
            )
            inventaire_ready = (
                any(
                    page.provider == "inventaire"
                    and page.available
                    and page.candidates
                    and page.query_rank == len(plan.queries) - 1
                    for page in pages
                )
                or (
                    not any(
                        futures[pending_future] == "inventaire"
                        for pending_future in pending
                    )
                    and any(
                        page.provider == "inventaire"
                        and page.available
                        and page.candidates
                        for page in pages
                    )
                )
            )
            if (openlibrary_ready or inventaire_ready) and grace_deadline is None:
                grace_deadline = min(deadline, time.monotonic() + 1.35)
        if grace_deadline is not None and time.monotonic() >= grace_deadline:
            break
    available_sources = {
        page.provider for page in pages if page.available
    }
    partial = bool(saturated or pending or failed_sources or any(
        not page.available or page.warnings for page in pages
    ))
    canonical_available = any(
        page.provider == "openlibrary" and page.available
        for page in pages
    )
    return pages, sorted(available_sources), partial, canonical_available


def _topic_books_from_results(results, lang="en", language_filter="current"):
    effective_language = (
        lang if language_filter == "current" else language_filter
    )
    books = []
    for result in results:
        # Native supplemental cards require a direct Open Library work mapping.
        # Chinese presentation additionally requires a Chinese language claim;
        # English/current and Any may retain an unknown-language mapped work.
        # Covers remain Open-Library-only; arbitrary provider images are never
        # proxied.
        if (
            "openlibrary" not in result.sources
            and (
                "inventaire" not in result.sources
                or (
                    effective_language == "cn"
                    and not {"chi", "zho", "zh", "cn"}.intersection(
                        normalize_topic_text(value)
                        for value in result.candidate.languages
                    )
                )
            )
        ):
            continue
        book = candidate_to_book(result)
        cover_id = book.pop("cover_id", None)
        book["cover_url"] = open_library_cover_url(cover_id)
        book["ol_key"] = normalize_topic_work_key(book.get("ol_key"))
        if not book["ol_key"] or not book.get("title"):
            continue
        remember_book_hint(book, lang)
        books.append(book)
    return canonicalize_book_covers(books)


def normalize_topic_work_key(value):
    match = re.fullmatch(r"/?works/(OL\d+W)", str(value or ""), re.I)
    return f"/works/{match.group(1).upper()}" if match else ""


def fetch_topic_discovery_payload(query, lang, filters):
    filters = normalize_topic_filters(filters)
    cached = cached_topic_discovery_payload(
        query,
        lang,
        filters,
        allow_stale=False,
    )
    if cached is not None:
        return cached
    stale_complete = cached_topic_discovery_payload(
        query,
        lang,
        filters,
        allow_stale=True,
    )
    plan = plan_topic_query(query, "topic")
    selected_language = filters.get("language", "current")
    provider_lang = (
        "any" if selected_language == "any"
        else lang if selected_language == "current"
        else selected_language
    )
    cached_pages = _topic_cached_openlibrary_pages(
        plan,
        provider_lang,
        filters,
    )
    cached_preview = merge_topic_candidates(
        cached_pages,
        plan,
        limit=200,
        author_cap=200,
        require_openlibrary=True,
    )
    cached_preview = filter_topic_results(
        cached_preview,
        book_type=filters["type"],
        language=filters["language"],
        current_language=lang,
        published=filters["published"],
        sort=filters["sort"],
        author_cap=2,
    )
    cached_eligible_keys = {
        result.candidate.work_key
        for result in cached_preview
        if result.candidate.work_key
    }
    pages, available_sources, partial, _canonical_available = _topic_provider_pages(
        plan,
        lang,
        filters,
        fallback_ready=(len(cached_preview) >= TOPIC_LOCAL_READY_CANDIDATES),
    )
    supplemental_work_keys = set()
    openlibrary_pages = [
        page for page in pages if page.provider == "openlibrary"
    ]
    openlibrary_incomplete = (
        len(openlibrary_pages) < len(plan.queries)
        or any(not page.available for page in openlibrary_pages)
    )
    if partial and cached_pages and openlibrary_incomplete:
        live_work_keys = {
            candidate.work_key
            for page in openlibrary_pages
            if page.available
            for candidate in page.candidates
            if candidate.work_key
        }
        supplemental_pages = [
            replace(
                page,
                candidates=tuple(
                    candidate for candidate in page.candidates
                    if (
                        candidate.work_key in cached_eligible_keys
                        and candidate.work_key not in live_work_keys
                    )
                ),
            )
            for page in cached_pages
        ]
        supplemental_pages = [
            page for page in supplemental_pages if page.candidates
        ]
        if supplemental_pages:
            pages.extend(supplemental_pages)
            supplemental_work_keys = {
                candidate.work_key
                for page in supplemental_pages
                for candidate in page.candidates
                if candidate.work_key
            }
    merged = merge_topic_candidates(
        pages,
        plan,
        limit=200,
        author_cap=200,
        require_openlibrary=True,
    )
    merged = filter_topic_results(
        merged,
        book_type=filters["type"],
        language=filters["language"],
        current_language=lang,
        published=filters["published"],
        sort=filters["sort"],
        author_cap=2,
    )[:TOPIC_DISCOVERY_WINDOW]
    cache_fallback = any(
        result.candidate.work_key in supplemental_work_keys
        for result in merged
    )
    books = _topic_books_from_results(merged, lang, filters["language"])
    sources = sorted({
        source
        for book in books
        for source in book.get("sources", [])
        if source in {"openlibrary", "inventaire"}
    })
    retry_after = max(
        openlibrary_status().get("retry_after", 0),
        inventaire_status().get("retry_after", 0),
        math.ceil(OL_CONNECT_TIMEOUT + OL_READ_TIMEOUT + 2),
    ) if partial else 0
    if partial and stale_complete and not stale_complete.get("partial"):
        fallback = dict(stale_complete)
        fallback["stale"] = True
        fallback["refresh_partial"] = True
        fallback["retry_after"] = retry_after
        return fallback
    payload = {
        "intent": "topic",
        "topic_mode": True,
        "display_query": plan.display_query,
        "all_books": books,
        "partial": partial,
        "cache_fallback": cache_fallback,
        "sources": sources,
        "source_unavailable": bool(partial and not available_sources and not books),
        "retry_after": retry_after,
        "filters": filters,
        "expansion_version": TOPIC_EXPANSION_VERSION,
        "ranker_version": TOPIC_RANKER_VERSION,
        "snapshot_id": topic_discovery_snapshot_id(books),
    }
    # Persist only complete windows. Partial windows remain request-local so a
    # provider recovery can immediately replace them; an outage is never stored
    # as an authoritative empty result.
    if not partial:
        key = topic_discovery_cache_key(query, lang, filters)
        cache_set(key, payload)
        disk_cache_set(key, payload)
    return payload


def topic_discovery_snapshot_id(books):
    identities = [
        "\0".join((
            str(book.get("ol_key") or ""),
            normalize_topic_text(book.get("title")),
            normalize_topic_text(book.get("author")),
        ))
        for book in books or []
    ]
    return hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()[:20]


def paginate_topic_discovery_payload(payload, page):
    books = list(payload.get("all_books") or [])
    if len(books) >= 12:
        start_count = TOPIC_DISCOVERY_START_COUNT
    else:
        start_count = min(4, len(books))
    start_here = books[:start_count] if page == 1 else []
    explore = books[start_count:]
    total_pages = max(
        1,
        (len(explore) + TOPIC_DISCOVERY_PAGE_SIZE - 1)
        // TOPIC_DISCOVERY_PAGE_SIZE,
    )
    start = (page - 1) * TOPIC_DISCOVERY_PAGE_SIZE
    paged = dict(payload)
    paged.pop("all_books", None)
    paged.update({
        "start_here": start_here,
        "books": explore[start:start + TOPIC_DISCOVERY_PAGE_SIZE],
        "page": page,
        "total_pages": total_pages,
        "total": len(explore),
        "snapshot_id": payload.get("snapshot_id") or topic_discovery_snapshot_id(books),
    })
    return paged

def cached_discovery_books(q, page=1, lang=None):
    """Return discovery data without ever waiting on Open Library."""
    lang = lang or DEFAULT_BOOK_LANG
    ckey = f"discover:v8:{lang}:{q}:{page}"
    cached = cache_get(ckey, 900)
    if cached is None:
        cached = disk_cache_get(ckey, 900)
        if cached is not None:
            cache_set(ckey, cached)
    if cached is not None:
        books, total, total_pages = cached
        return canonicalize_book_covers(books), total, total_pages
    return cached


def fetch_discovery_books(q, page=1, lang=None):
    lang = lang or DEFAULT_BOOK_LANG
    ckey = f"discover:v8:{lang}:{q}:{page}"
    cached = cached_discovery_books(q, page, lang)
    if cached is not None:
        return cached

    covered_query = (
        f"{q} cover_i:* language:{BOOK_LANG_CONFIG[lang]['ol_lang']}"
    )

    def fetch_search(query, limit):
        try:
            identifier_type, _ = discovery_identifier(q)
            if lang == "cn":
                fields = OL_COVER_IDENTIFIER_FIELDS if identifier_type else OL_BOOK_FIELDS
            else:
                fields = OL_DISCOVERY_IDENTIFIER_FIELDS if identifier_type else OL_LIST_FIELDS
            return ol_get("/search.json", {
                "q": query,
                "limit": limit,
                "page": page,
                "fields": fields,
            })
        except Exception:
            return None

    # Preserve exact sparse matches while fetching a cover-rich result set in
    # parallel so cover quality does not add a second origin wait.
    with ThreadPoolExecutor(max_workers=2) as pool:
        raw_future = pool.submit(fetch_search, q, DISCOVERY_SEARCH_LIMIT)
        covered_future = pool.submit(
            fetch_search,
            covered_query,
            DISCOVERY_COVER_PAGE_SIZE,
        )
        raw_data = raw_future.result()
        covered_data = covered_future.result()

    if raw_data is None and covered_data is None:
        return [], None, 1

    raw_data = raw_data or {}
    covered_data = covered_data or {}

    books = []
    seen_keys = set()

    def append_records(records, *, allow_missing_cover, maximum=None):
        added = 0
        for record in records:
            if not discovery_fallback_matches_language(record, lang):
                continue
            book = extract_book(
                record,
                lang,
                allow_missing_cover=allow_missing_cover,
            )
            if not book:
                continue
            if book_seen(book, seen_keys):
                if book.get("cover_url"):
                    existing = next(
                        (
                            candidate for candidate in books
                            if candidate.get("ol_key") == book.get("ol_key")
                        ),
                        None,
                    )
                    if existing and not existing.get("cover_url"):
                        existing["cover_url"] = book["cover_url"]
                continue
            books.append(book)
            remember_book(book, seen_keys)
            added += 1
            if len(books) >= DISCOVERY_PAGE_SIZE or (
                maximum is not None and added >= maximum
            ):
                break
        return added

    raw_records = rank_discovery_records(
        raw_data.get("docs", [])[:DISCOVERY_SEARCH_LIMIT],
        q,
    )
    covered_records = rank_discovery_records(
        covered_data.get("docs", [])[:DISCOVERY_COVER_PAGE_SIZE],
        q,
    )
    if page == 1:
        append_records(
            raw_records,
            allow_missing_cover=True,
            maximum=DISCOVERY_RAW_PREFIX_LIMIT,
        )
    covered_added = append_records(covered_records, allow_missing_cover=False)

    # If Open Library has no covered matches, retain the sparse-result
    # fallback rather than making an exact but coverless book undiscoverable.
    if covered_added == 0 and len(books) < DISCOVERY_PAGE_SIZE:
        append_records(raw_records, allow_missing_cover=True)

    result_data = covered_data if covered_added else raw_data
    result_limit = DISCOVERY_COVER_PAGE_SIZE if covered_added else DISCOVERY_SEARCH_LIMIT
    total = result_data.get("numFound", 0)
    total_pages = min(
        25,
        max(1, (total + result_limit - 1) // result_limit),
    )
    result = (books, total, total_pages)
    cache_set(ckey, result)
    disk_cache_set(ckey, result)
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

def identity_match(score_fn, candidate, primary="", aliases=None):
    targets = bounded_identity_values([primary, aliases], limit=DOWNLOAD_IDENTITY_VALUE_LIMIT)
    if not targets:
        return 0, ""
    return max(
        ((score_fn(candidate, target), target) for target in targets),
        key=lambda item: item[0],
    )

def source_metadata_language_penalty(book, preferred_language):
    if (preferred_language or "").casefold() != "english":
        return 0
    if is_chinese_title(book.title) or is_chinese_title(book.author):
        return -500
    if is_chinese_title(book.publisher):
        return -140
    return 0

HIDDEN_KINDLE_FORMATS = frozenset({"azw", "azw3", "mobi"})
KINDLE_DELIVERY_FORMATS = frozenset({"epub", "pdf"})

def is_visible_kindle_format(extension):
    extension = re.sub(r"[^a-z0-9]", "", str(extension or "").casefold())
    return extension not in HIDDEN_KINDLE_FORMATS

def is_kindle_delivery_format(extension):
    extension = re.sub(r"[^a-z0-9]", "", str(extension or "").casefold())
    return extension in KINDLE_DELIVERY_FORMATS

def kindle_accuracy_score(
    book,
    target_title="",
    target_author="",
    preferred_language="",
    *,
    target_titles=None,
    target_authors=None,
):
    score, _ = identity_match(title_match_score, book.title, target_title, target_titles)
    author_score, _ = identity_match(
        author_match_score, book.author, target_author, target_authors
    )
    score += author_score
    if preferred_language:
        score += 80 if book_matches_language(book, preferred_language) else -200
    return score + source_metadata_language_penalty(book, preferred_language)

def kindle_delivery_size_score(book):
    """Prefer smaller files without overruling meaningful title/author evidence."""
    size_bytes = parse_size_bytes(book.size)
    extension = (book.ext or "").casefold()
    if not size_bytes:
        return 0
    if extension == "epub":
        if size_bytes < 50000 or size_bytes > 50 * 1024 * 1024:
            return -140
        size_mb = size_bytes / (1024 * 1024)
        return max(0, 62 - round(max(0, size_mb - 0.5) * 3))
    if extension == "pdf":
        if size_bytes < 100000 or size_bytes > 100 * 1024 * 1024:
            return -130
        size_mb = size_bytes / (1024 * 1024)
        return max(0, 30 - round(max(0, size_mb - 1.0)))
    return 12 if size_bytes <= 25 * 1024 * 1024 else 0

def fastest_kindle_candidate(
    books,
    target_title="",
    target_author="",
    preferred_language="",
    *,
    target_titles=None,
    target_authors=None,
):
    accurate_books = [book for book in books if is_kindle_delivery_format(book.ext)]
    if not accurate_books:
        return None
    best_accuracy = max(
        kindle_accuracy_score(
            book, target_title, target_author, preferred_language,
            target_titles=target_titles, target_authors=target_authors,
        )
        for book in accurate_books
    )
    candidates = [
        book for book in books
        if (book.ext or "").casefold() == "epub"
        and 50000 <= parse_size_bytes(book.size) <= 50 * 1024 * 1024
        and kindle_accuracy_score(
            book, target_title, target_author, preferred_language,
            target_titles=target_titles, target_authors=target_authors,
        )
            >= best_accuracy - 40
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda book: parse_size_bytes(book.size))

def book_score(
    book,
    target_title="",
    target_author="",
    preferred_language="",
    *,
    target_titles=None,
    target_authors=None,
):
    score = kindle_accuracy_score(
        book, target_title, target_author, preferred_language,
        target_titles=target_titles, target_authors=target_authors,
    )
    fmt_scores = {"epub": 160, "pdf": 70, "txt": 12, "djvu": -20, "chm": -30}
    score += fmt_scores.get(book.ext.lower(), 0)
    try:
        y = int(book.year)
        if 1900 <= y <= 2030:
            score += max(0, min(20, round((y - 1980) * 0.4)))
    except (TypeError, ValueError):
        pass
    score += kindle_delivery_size_score(book)
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

def recommendation_reasons(
    book,
    target_title="",
    target_author="",
    preferred_language="",
    *,
    fastest_to_kindle=False,
    target_titles=None,
    target_authors=None,
):
    reasons = []
    title_score, _ = identity_match(
        title_match_score, book.title, target_title, target_titles
    )
    author_score, _ = identity_match(
        author_match_score, book.author, target_author, target_authors
    )
    extension = (book.ext or "").lower()
    size_bytes = parse_size_bytes(book.size)
    if title_score >= 900:
        reasons.append("Strong title match")
    if target_author and author_score >= 180:
        reasons.append("Author match")
    if extension == "epub":
        reasons.append("Kindle-ready EPUB")
    elif extension == "pdf":
        reasons.append("Readable PDF")
    if fastest_to_kindle:
        reasons.append("Fastest to Kindle")
    if preferred_language and book_matches_language(book, preferred_language):
        reasons.append(preferred_language)
    if size_bytes and size_bytes <= 25 * 1024 * 1024:
        reasons.append("Easy to send")
    try:
        if int(book.pages) > 0:
            reasons.append("Complete page data")
    except (TypeError, ValueError):
        pass
    return reasons[:4]

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

def rank_download_books(
    books,
    target_title="",
    target_author="",
    preferred_language="",
    *,
    target_titles=None,
    target_authors=None,
):
    fastest = fastest_kindle_candidate(
        books,
        target_title=target_title,
        target_author=target_author,
        preferred_language=preferred_language,
        target_titles=target_titles,
        target_authors=target_authors,
    )
    scorer = lambda book: (
        book_score(
            book,
            target_title=target_title,
            target_author=target_author,
            preferred_language=preferred_language,
            target_titles=target_titles,
            target_authors=target_authors,
        )
        + (55 if book is fastest else 0)
    )
    return [
        book for _, book in sorted(
            enumerate(books),
            key=lambda item: (scorer(item[1]), -item[0]),
            reverse=True,
        )
    ], scorer

def download_book_is_relevant(
    book,
    target_title="",
    target_author="",
    *,
    target_titles=None,
    target_authors=None,
):
    title_score, matched_title = identity_match(
        title_match_score, book.title, target_title, target_titles
    )
    if not target_title or title_score < 600:
        return False
    candidate_title = normalize_match_text(book.title)
    normalized_target = normalize_match_text(matched_title or target_title)
    author_score, _ = identity_match(
        author_match_score, book.author, target_author, target_authors
    )
    if (
        candidate_title
        and candidate_title != normalized_target
        and candidate_title in normalized_target
        and len(candidate_title.replace(" ", ""))
            < len(normalized_target.replace(" ", "")) * 0.55
        and author_score < 180
    ):
        return False
    if target_author and book.author.strip() and author_score < 70:
        return False
    if target_author and not book.author.strip() and title_score < 950:
        return False
    return True

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
    values = desc if isinstance(desc, list) else [desc]
    cleaned = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("value", "")
        if not isinstance(value, str):
            continue
        value = strip_html(value)
        value = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)", "", value)
        value = re.sub(r"(?:\*\*|__|`)", "", value)
        value = re.sub(r"\[source\]\[\d+\]", "", value, flags=re.I)
        value = re.sub(r"\[\d+\]:\s*\S+", "", value)
        value = re.sub(r"\s+", " ", value).strip().strip('"').strip()
        if value:
            cleaned.append(value)
    return max(cleaned, key=len, default="")

def description_rank(candidate):
    word_count = len(candidate.split())
    awkward_lead = candidate.startswith(("'",)) or bool(
        re.match(r"^[A-Z][A-Z !'-]{12,}", candidate)
    )
    return awkward_lead, not 40 <= word_count <= 350, abs(word_count - 120)

def archive_description_candidate(value):
    if isinstance(value, dict):
        value = value.get("value", "")
    if not isinstance(value, str):
        return ""
    candidate = strip_html(value).strip().strip('"').strip()
    candidate = re.sub(r"\s+", " ", candidate)
    lower = candidate.casefold()
    if (
        len(candidate.split()) < 12
        or lower in {"print version record", "electronic reproduction"}
        or lower.startswith((
            "includes bibliographical references",
            "cover; half title;",
            "contents;",
        ))
        or re.match(r"^(?:[ivxlcdm]+,?\s+)?\d+\s+pages?\b", lower)
    ):
        return ""
    return candidate if is_english_text(candidate) else ""

def archive_description_lock(identifier):
    digest = hashlib.sha256(identifier.encode("utf-8")).digest()
    return ARCHIVE_DESCRIPTION_LOCKS[int.from_bytes(digest[:2], "big") % len(ARCHIVE_DESCRIPTION_LOCKS)]

def archive_cache_get(key, ttl):
    cached = cache_get(key, ttl)
    if cached is None:
        cached = disk_cache_get(key, ttl)
        if cached is not None:
            cache_set(key, cached)
    return cached

def archive_description(identifier):
    identifier = str(identifier or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", identifier):
        return "", True
    ckey = f"archive_description:v1:{identifier}"
    failure_key = f"archive_description_failure:v1:{identifier}"
    cached = archive_cache_get(ckey, EXTERNAL_METADATA_TTL)
    if cached is not None:
        return cached.get("description", ""), True
    if archive_cache_get(failure_key, EXTERNAL_METADATA_FAILURE_TTL) is not None:
        return "", False

    with archive_description_lock(identifier):
        cached = archive_cache_get(ckey, EXTERNAL_METADATA_TTL)
        if cached is not None:
            return cached.get("description", ""), True
        if archive_cache_get(failure_key, EXTERNAL_METADATA_FAILURE_TTL) is not None:
            return "", False
        try:
            response = SESSION.get(
                f"https://archive.org/metadata/{identifier}",
                timeout=(2, 5),
            )
            response.raise_for_status()
            metadata = (response.json() or {}).get("metadata") or {}
        except (requests.RequestException, ValueError):
            failure = {"failed_at": time.time()}
            cache_set(failure_key, failure)
            disk_cache_set(failure_key, failure)
            return "", False
        raw_descriptions = metadata.get("description") or []
        if not isinstance(raw_descriptions, list):
            raw_descriptions = [raw_descriptions]
        candidates = [
            candidate
            for candidate in map(archive_description_candidate, raw_descriptions)
            if candidate
        ]
        description = min(candidates, key=description_rank) if candidates else ""
        payload = {"description": description}
        cache_set(ckey, payload)
        disk_cache_set(ckey, payload)
        return description, True

def english_description_result(ol_key, work=None):
    work = work or ol_get_work(ol_key) or {}
    description = extract_desc(work)
    if is_english_text(description):
        return description, True

    editions_data = ol_get(f"{ol_key}/editions.json", {"limit": 100})
    if editions_data is None:
        return "", False
    candidates = []
    archive_identifiers = []
    for edition in editions_data.get("entries", []):
        archive_id = edition_archive_identifier(edition)
        if archive_id and archive_id not in archive_identifiers:
            archive_identifiers.append(archive_id)
        language_codes = edition_language_codes(edition)
        candidate = extract_desc(edition)
        if candidate and (
            "eng" in language_codes
            or (not language_codes and is_english_text(candidate))
        ):
            candidates.append(candidate)
    if candidates:
        return min(candidates, key=description_rank), True

    archive_complete = True
    for identifier in archive_identifiers[:ARCHIVE_DESCRIPTION_MAX_IDENTIFIERS]:
        candidate, complete = archive_description(identifier)
        archive_complete = archive_complete and complete
        if candidate:
            return candidate, True
    return "", archive_complete

def english_description_for_work(ol_key, work=None):
    return english_description_result(ol_key, work)[0]

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

def search_record_for_work(ol_key, lang=None):
    if not re.fullmatch(r"/works/OL\d+W", ol_key or ""):
        return {}
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    queries = [
        f"key:{ol_key} language:{BOOK_LANG_CONFIG[lang]['ol_lang']}",
        f"key:{ol_key}",
    ]
    for query in dict.fromkeys(queries):
        data = ol_get("/search.json", {
            "q": query,
            "limit": 1,
            "fields": OL_IDENTITY_FIELDS,
        })
        record = ((data or {}).get("docs") or [{}])[0]
        if record.get("key") == ol_key:
            return record
    return {}

def known_book_metadata(work_id, lang=None):
    known = KNOWN_WORK_METADATA.get(work_id)
    if not known:
        return None
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    result = dict(known)
    if lang != "cn":
        result["localized_title"] = ""
        result["download_title"] = result.get("title", "")
    result["ol_key"] = ol_key_from_work_id(work_id)
    result.update(collect_book_identity_metadata(result))
    return result

def book_metadata_from_work(work_id, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    ckey = f"book_meta:v3:{lang}:{work_id}"
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
    search_record = search_record_for_work(ol_key, lang)
    if not work and not search_record:
        result = known_book_metadata(work_id, lang)
        if result:
            result["_complete"] = False
            remember_book_hint(result, lang)
        return result
    edition = first_matching_edition(search_record, lang)
    cover_id = (
        edition_cover_id(edition or {})
        or valid_cover_id(search_record.get("cover_i"))
        or valid_cover_id(search_record.get("cover_id"))
        or work_cover_id(work)
    )
    archive_id = edition_archive_identifier(edition or {})
    editions_checked = False
    if not cover_id and not archive_id:
        editions_data = ol_get(f"{ol_key}/editions.json", {"limit": 100})
        editions_checked = editions_data is not None
        if editions_data is not None:
            preferred_editions = preferred_work_editions(
                editions_data.get("entries", []),
                lang,
            )
            cover_edition = next(
                (candidate for candidate in preferred_editions if edition_cover_id(candidate)),
                None,
            )
            archive_edition = next(
                (candidate for candidate in preferred_editions if edition_archive_identifier(candidate)),
                None,
            )
            cover_id = edition_cover_id(cover_edition or {})
            archive_id = edition_archive_identifier(archive_edition or {})
    authors = search_record.get("author_name") or []
    selected_title = (edition or {}).get("title") or search_record.get("title") or (work or {}).get("title", "")
    title = selected_title
    localized_title = ""
    download_title = selected_title
    if lang == "cn":
        localized_title = selected_title if is_chinese_title(selected_title) else resolve_chinese_title(ol_key)
        title = resolve_english_title(ol_key) or localized_title or selected_title
        download_title = localized_title or selected_title or title
        if localized_title == title:
            localized_title = ""
    primary_author = (authors[0] if authors else "") or first_work_author(work or {})
    result = {
        "title": title,
        "localized_title": localized_title,
        "download_title": download_title,
        "author": primary_author,
        "cover_url": open_library_cover_url(cover_id) or archive_cover_url(archive_id),
        "ol_key": ol_key,
        "_complete": bool(work) and (bool(search_record) or editions_checked),
    }
    result.update(collect_book_identity_metadata(
        result,
        work=work,
        search_record=search_record,
        edition=edition,
    ))
    if result["_complete"]:
        cache_set(ckey, result)
        disk_cache_set(ckey, result)
    remember_book_hint(result, lang)
    return result

def book_detail_cache_key(work_id, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    return f"book_detail:v5:{lang}:{work_id}"

def cached_book_detail(work_id, lang=None, allow_stale=True):
    key = book_detail_cache_key(work_id, lang)
    cached = cache_get(key, BOOK_DETAIL_FRESH_TTL)
    if cached is not None:
        return cached, "memory"
    cached = disk_cache_get(key, BOOK_DETAIL_FRESH_TTL)
    if cached is not None:
        cache_set(key, cached)
        return cached, "disk"
    if allow_stale:
        stale = disk_cache_get_stale(key, BOOK_DETAIL_STALE_TTL)
        if stale is not None:
            return stale, "stale"
        normalized_lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
        for version in ("v4", "v3"):
            legacy_key = f"book_detail:{version}:{normalized_lang}:{work_id}"
            legacy = (
                cache_get(legacy_key, BOOK_DETAIL_STALE_TTL)
                or disk_cache_get_stale(legacy_key, BOOK_DETAIL_STALE_TTL)
            )
            if legacy is not None:
                legacy = {**legacy, "complete": False}
                return legacy, "stale"
    return None, "miss"

def alternate_canonical_book_detail(work_id, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    other_lang = "cn" if lang == "en" else "en"
    candidates = []
    for version in ("v5", "v4", "v3"):
        key = f"book_detail:{version}:{other_lang}:{work_id}"
        detail = (
            cache_get(key, BOOK_DETAIL_STALE_TTL)
            or disk_cache_get_stale(key, BOOK_DETAIL_STALE_TTL)
        )
        if detail:
            candidates.append(detail)
    return max(
        candidates,
        key=lambda detail: (
            bool(detail.get("description")),
            bool(detail.get("cover_url")),
            len(detail.get("subjects") or []),
        ),
        default={},
    )

def merge_canonical_book_detail(detail, canonical):
    if not detail or not canonical:
        return detail
    merged = dict(detail)
    for field in (
        "description", "cover_url", "subjects", "similar_subjects",
        "title_aliases", "authors", "isbns", "download_queries",
    ):
        if not merged.get(field) and canonical.get(field):
            merged[field] = canonical[field]
    return merged

def fallback_book_detail(work_id, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    metadata_key = f"book_meta:v3:{lang}:{work_id}"
    metadata = (
        cache_get(metadata_key, BOOK_DETAIL_STALE_TTL)
        or disk_cache_get_stale(metadata_key, BOOK_DETAIL_STALE_TTL)
        or cache_get(f"book_meta:v2:{lang}:{work_id}", BOOK_DETAIL_STALE_TTL)
        or disk_cache_get_stale(f"book_meta:v2:{lang}:{work_id}", BOOK_DETAIL_STALE_TTL)
        or hinted_book_metadata(work_id, lang)
        or known_book_metadata(work_id, lang)
        or {}
    )
    canonical = alternate_canonical_book_detail(work_id, lang)
    if not metadata and canonical:
        metadata = canonical
    if not metadata:
        return None
    identity = collect_book_identity_metadata(metadata)
    if lang == "cn":
        download_queries = bounded_identity_values([
            metadata.get("download_title"),
            metadata.get("localized_title"),
            identity.get("title_aliases"),
            identity.get("isbns"),
        ], limit=10)
    else:
        download_queries = english_download_queries({**metadata, **identity})
    return {
        "success": True,
        "title": metadata.get("title", ""),
        "localized_title": metadata.get("localized_title", ""),
        "download_title": metadata.get("download_title") or metadata.get("title", ""),
        "author": metadata.get("author", ""),
        "cover_url": localize_cover_url(
            metadata.get("cover_url", "") or canonical.get("cover_url", "")
        ),
        "download_queries": download_queries,
        **identity,
        "description": canonical.get("description", ""),
        "subjects": canonical.get("subjects", []),
        "similar_subjects": canonical.get("similar_subjects", []),
        "complete": False,
    }

def build_book_detail(work_id, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    ol_key = ol_key_from_work_id(work_id)
    if not ol_key:
        return None
    work = ol_get_work(ol_key) or {}
    metadata = book_metadata_from_work(work_id, lang) or fallback_book_detail(work_id, lang) or {}
    if metadata.get("success"):
        metadata = {
            key: value for key, value in metadata.items()
            if key in {
                "title", "localized_title", "download_title", "author", "cover_url",
                "title_aliases", "authors", "isbns", "_complete",
            }
        }
    if not work and not metadata:
        return None
    description, description_complete = english_description_result(ol_key, work)
    subjects = work.get("subjects", [])[:20]
    identity = collect_book_identity_metadata(metadata, work=work)
    enriched_metadata = {**metadata, **identity}
    detail = {
        "success": True,
        "title": metadata.get("title") or work.get("title", ""),
        "localized_title": metadata.get("localized_title", ""),
        "download_title": metadata.get("download_title") or metadata.get("title") or work.get("title", ""),
        "author": metadata.get("author", ""),
        "cover_url": localize_cover_url(metadata.get("cover_url", "")),
        "download_queries": (
            chinese_download_queries(ol_key, enriched_metadata)
            if lang == "cn"
            else english_download_queries(enriched_metadata)
        ),
        **identity,
        "description": description,
        "subjects": subjects,
        "similar_subjects": similar_subject_candidates(subjects),
        "complete": (
            bool(work)
            and description_complete
            and metadata.get("_complete", bool(work))
        ),
    }
    if detail["title"] or detail["author"]:
        key = book_detail_cache_key(work_id, lang)
        cache_set(key, detail)
        disk_cache_set(key, detail)
        remember_book_hint({**detail, "ol_key": ol_key}, lang)
        return detail
    return None

def _refresh_book_detail(work_id, lang):
    refresh_key = (lang, work_id)
    try:
        build_book_detail(work_id, lang)
    finally:
        with BOOK_DETAIL_REFRESH_LOCK:
            BOOK_DETAIL_REFRESHING.discard(refresh_key)

def schedule_book_detail_refresh(work_id, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    refresh_key = (lang, work_id)
    with BOOK_DETAIL_REFRESH_LOCK:
        if refresh_key in BOOK_DETAIL_REFRESHING:
            return False
        if len(BOOK_DETAIL_REFRESHING) >= BOOK_DETAIL_REFRESH_PENDING_LIMIT:
            return False
        BOOK_DETAIL_REFRESHING.add(refresh_key)
    BOOK_DETAIL_EXECUTOR.submit(_refresh_book_detail, work_id, lang)
    return True

def normalize_book_detail_recommendations(detail):
    if not detail:
        return detail
    detail["similar_subjects"] = similar_subject_candidates(detail.get("subjects", []))
    return detail

def get_book_detail(work_id, lang=None):
    lang = normalize_book_lang(lang) or DEFAULT_BOOK_LANG
    detail, cache_state = cached_book_detail(work_id, lang)
    if detail is not None:
        detail = merge_canonical_book_detail(
            detail,
            alternate_canonical_book_detail(work_id, lang),
        )
        detail = normalize_book_detail_recommendations(detail)
        detail["cover_url"] = localize_cover_url(detail.get("cover_url", ""))
        if cache_state == "stale" or not detail.get("complete"):
            schedule_book_detail_refresh(work_id, lang)
        return detail, cache_state
    fallback = fallback_book_detail(work_id, lang)
    fallback = normalize_book_detail_recommendations(fallback)
    schedule_book_detail_refresh(work_id, lang)
    return fallback, "fallback" if fallback else "miss"

class LibFlixTestClient(FlaskClient):
    """Mark in-process test requests without a forgeable HTTP header."""

    def open(self, *args, **kwargs):
        environ_overrides = kwargs.setdefault("environ_overrides", {})
        environ_overrides.setdefault("libflix.test_client", True)
        return super().open(*args, **kwargs)


app = Flask(__name__)
app.test_client_class = LibFlixTestClient
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
app.config["RATE_LIMITING_ENABLED"] = os.environ.get(
    "LIBFLIX_RATE_LIMITING_ENABLED", "1"
).strip().casefold() not in {"0", "false", "no", "off"}

RUNTIME_RATE_LIMITER = SQLiteRateLimiter(RATE_LIMIT_SQLITE)
RUNTIME_METRICS = SQLiteMetrics(METRICS_SQLITE)
RUNTIME_SECURITY_HEADERS = SecurityHeadersConfig(trust_forwarded_proto=True)
RUNTIME_RATE_LIMIT_RULES = {
    "api_discover": ("discovery", RateLimitRule(24, 60)),
    "api_similar": ("similar", RateLimitRule(36, 60)),
    "api_book": ("book-detail", RateLimitRule(24, 60)),
    "api_search": ("search", RateLimitRule(24, 60)),
    "download": ("download", RateLimitRule(12, 300)),
    "api_create_kindle_job": ("kindle", RateLimitRule(4, 600)),
    "api_sendtokindle": ("kindle", RateLimitRule(4, 600)),
    "api_kindle_job": ("kindle-status", RateLimitRule(60, 60)),
    "web_vitals": ("metrics", RateLimitRule(30, 60)),
}
RUNTIME_GLOBAL_RATE_LIMIT_RULES = {
    "api_discover": ("discovery-global", RateLimitRule(120, 60)),
    "api_similar": ("similar-global", RateLimitRule(240, 60)),
    "api_book": ("book-detail-global", RateLimitRule(120, 60)),
    "api_search": ("search-global", RateLimitRule(120, 60)),
    "download": ("download-global", RateLimitRule(60, 300)),
    "api_create_kindle_job": ("kindle-global", RateLimitRule(10, 600)),
    "api_sendtokindle": ("kindle-global", RateLimitRule(10, 600)),
    "api_kindle_job": ("kindle-status-global", RateLimitRule(600, 60)),
}
RUNTIME_METRICS_SKIPPED_ENDPOINTS = {
    "static", "favicon", "cover_default", "cover", "olcover", "iacover",
}


def trusted_proxy_client_identity():
    return request_client_identity(
        request,
        trusted_client_ip_header=(
            "X-LibFlix-Client-IP" if TRUST_PROXY_HEADERS else ""
        ),
        trusted_proxy_networks=("127.0.0.1/32", "::1/128"),
    )


def rate_limit_client_identity():
    """Return a usable end-client identity, or none for an unconfigured proxy.

    A loopback peer is normally the local reverse proxy.  Without the explicit
    trusted-header setting, treating that peer as the client would collapse
    every visitor into one small per-client bucket.  Global protection remains
    active in that safe-default configuration.
    """

    identity = trusted_proxy_client_identity()
    if TRUST_PROXY_HEADERS:
        return identity
    try:
        if ipaddress.ip_address(request.remote_addr or "").is_loopback:
            return None
    except ValueError:
        pass
    return identity


def request_rate_limit_cost():
    if request.endpoint == "api_discover":
        try:
            if int(request.args.get("page", 1)) > 1:
                return 1
        except (TypeError, ValueError):
            return 1
        query = request.args.get("q", "")
        plan = plan_topic_query(query, request.args.get("intent", ""))
        # A cold topic request can perform three Open Library searches plus two
        # Inventaire searches and their entity hydrations.
        return 7 if plan.intent == "topic" else 2
    if request.endpoint == "api_similar":
        subject_count = len([
            value for value in request.args.getlist("subject")[:2] if value.strip()
        ])
        return min(SIMILAR_MAX_ORIGIN_QUERIES, max(1, subject_count + bool(request.args.get("author", "").strip())))
    if request.endpoint != "api_search" or request.args.get("page", "1") != "1":
        return 1
    if re.fullmatch(r"/works/OL\d+W", request.args.get("ol_key", "")):
        return DOWNLOAD_ALIAS_SEARCH_LIMIT
    raw_aliases = request.args.get("search_aliases") or "[]"
    if len(raw_aliases.encode("utf-8")) > IDENTITY_QUERY_JSON_MAX_BYTES:
        return DOWNLOAD_ALIAS_SEARCH_LIMIT
    try:
        aliases = json.loads(raw_aliases)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        aliases = []
    return min(DOWNLOAD_ALIAS_SEARCH_LIMIT, 1 + len(aliases)) if isinstance(aliases, list) else 1

@app.before_request
def start_request_timing():
    g.request_started_at = time.perf_counter()
    g.server_timings = []
    if (
        request.content_length is not None
        and request.content_length > app.config["MAX_CONTENT_LENGTH"]
    ):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": "Request body is too large"}), 413
        return Response("Request body is too large", status=413, mimetype="text/plain")
    if (
        not app.config["RATE_LIMITING_ENABLED"]
        or request.environ.get("libflix.test_client")
        and not request.environ.get("libflix.enforce_rate_limits")
    ):
        return None
    rule_config = RUNTIME_RATE_LIMIT_RULES.get(request.endpoint)
    if not rule_config:
        return None
    cost = request_rate_limit_cost()
    global_config = RUNTIME_GLOBAL_RATE_LIMIT_RULES.get(request.endpoint)
    checks = []
    client_identity = rate_limit_client_identity()
    # Check the narrowest bucket first so a client that is already exhausted
    # cannot keep draining capacity reserved for every other visitor.
    if client_identity:
        checks.append((*rule_config, client_identity))
    if global_config:
        checks.append((*global_config, "all-clients"))
    if not checks:
        return None
    decision = None
    client_decision = None
    for bucket, rule, identity in checks:
        decision = RUNTIME_RATE_LIMITER.check(bucket, identity, rule, cost=cost)
        if not decision.allowed:
            break
        if identity != "all-clients":
            client_decision = decision
    header_decision = client_decision or decision
    g.rate_limit_headers = header_decision.response_headers
    if decision.allowed:
        return None
    response = Response(
        json_rate_limit_body(decision.retry_after),
        status=429,
        mimetype="application/json",
    )
    response.headers.update(decision.response_headers)
    return response


@got_request_exception.connect_via(app)
def record_request_exception(_sender, exception, **_extra):
    RUNTIME_METRICS.record_exception(request.path, exception)


def compact_partial_navigation(resp):
    """Strip the persistent shell from app-navigation HTML responses."""
    if (
        request.headers.get("X-LibFlix-Navigation") != "partial"
        or request.method not in ("GET", "HEAD")
        or resp.status_code >= 400
        or resp.mimetype != "text/html"
    ):
        return resp
    try:
        document = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
        for tag in document.find_all("style"):
            if not tag.has_attr("data-page-style"):
                tag.decompose()
        for tag in document.find_all("script"):
            if not tag.has_attr("data-page-script"):
                tag.decompose()
        for selector in (".skip-link", "#appTransition", "#kindleModal", "#appToast"):
            for node in document.select(selector):
                node.decompose()
        resp.set_data(str(document))
        resp.headers["X-LibFlix-Partial"] = "1"
        resp.headers["Vary"] = "X-LibFlix-Navigation"
    except (TypeError, ValueError):
        # A complete document is always a valid client fallback.
        pass
    return resp

@app.route("/language/<lang>")
def switch_language(lang):
    lang = normalize_book_lang(lang)
    if not lang:
        return redirect("/")
    target = request.args.get("next", "/")
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    g.book_lang_override = lang
    return redirect(target)

@app.after_request
def cache_headers(resp):
    resp = compact_partial_navigation(resp)
    duration_ms = 0.0
    if getattr(g, "request_started_at", None) is not None:
        duration_ms = max(0.0, (time.perf_counter() - g.request_started_at) * 1000)
        add_server_timing("app", g.request_started_at, description=request.endpoint or "request")
    if getattr(g, "server_timings", None):
        resp.headers["Server-Timing"] = ", ".join(g.server_timings)
    if request.endpoint == "switch_language":
        resp.headers["Cache-Control"] = "no-store"
    elif getattr(g, "cache_control_override", None):
        resp.headers["Cache-Control"] = g.cache_control_override
    elif request.method in ("GET", "HEAD") and resp.status_code < 400:
        if request.path.startswith(("/api/kindle/", "/api/health")):
            resp.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if request.args.get("v")
                else "public, max-age=3600"
            )
        elif resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "private, max-age=90, stale-while-revalidate=600"
        elif request.path.startswith(("/api/book", "/api/category", "/api/shelf", "/api/discover", "/api/cn-display-title")):
            resp.headers["Cache-Control"] = "private, max-age=600, stale-while-revalidate=3600"
        elif request.path.startswith("/api/search"):
            resp.headers["Cache-Control"] = "private, max-age=120"
    if request.path == "/static/libflix-sw.js":
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    if not request.path.startswith("/static/") and (
        resp.mimetype == "text/html" or request.path.startswith("/api/")
    ):
        resp.set_cookie("book_lang", get_book_lang(), max_age=31536000, samesite="Lax")
    if getattr(g, "rate_limit_headers", None):
        resp.headers.update(g.rate_limit_headers)
    if request.endpoint not in RUNTIME_METRICS_SKIPPED_ENDPOINTS:
        RUNTIME_METRICS.record_request(
            request.path,
            request.method,
            resp.status_code,
            duration_ms,
        )
    return apply_security_headers(resp, request, RUNTIME_SECURITY_HEADERS)


@app.errorhandler(413)
def request_too_large(_error):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "Request body is too large"}), 413
    return Response("Request body is too large", status=413, mimetype="text/plain")


@app.route("/api/health")
def api_health():
    database = {
        "ready": False,
        "writable": os.access(DATA_DIR, os.W_OK),
        "cache_entries": 0,
    }
    job_counts = {}
    try:
        initialize_disk_cache()
        with disk_cache_connection(timeout=1) as connection:
            database["cache_entries"] = connection.execute(
                "SELECT COUNT(*) FROM api_cache"
            ).fetchone()[0]
            job_counts = {
                status: count
                for status, count in connection.execute(
                    "SELECT status, COUNT(*) FROM kindle_jobs GROUP BY status"
                ).fetchall()
            }
        database["ready"] = True
    except (OSError, sqlite3.Error):
        pass
    rate_limiter_ready = RUNTIME_RATE_LIMITER.healthcheck()
    metrics_ready = RUNTIME_METRICS.healthcheck()
    payload = {
        "success": database["ready"] and rate_limiter_ready and metrics_ready,
        "service": "libflix",
        "database": database,
        "openlibrary": openlibrary_status(),
        "inventaire": inventaire_status(),
        "cache": {
            "memory_entries": len(CACHE),
            "book_refreshes": len(BOOK_DETAIL_REFRESHING),
            "similar_refreshes": len(SIMILAR_REFRESHING),
            "loaded_shelf_sets": sum(
                1 for key in CACHE if str(key).startswith("shelves_")
            ),
        },
        "kindle_jobs": job_counts,
        "runtime_protection": {
            "rate_limiter_ready": rate_limiter_ready,
            "metrics_ready": metrics_ready,
            "rate_limiter_degraded_checks": RUNTIME_RATE_LIMITER.degraded_checks,
            "metrics_dropped_writes": RUNTIME_METRICS.dropped_writes,
            "trusted_client_header": TRUST_PROXY_HEADERS,
        },
    }
    return jsonify(payload), 200 if payload["success"] else 503

@app.route("/favicon.ico")
def favicon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="12" fill="#f20d1b"/>'
        '<path d="M18 14h9v28h19v8H18z" fill="#fff"/>'
        '</svg>'
    )
    response = Response(svg, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response

@app.route("/api/metrics/web-vitals", methods=["POST"])
def web_vitals():
    if request.content_length and request.content_length > 4096:
        return "", 413
    if len(request.get_data(cache=True)) > 4096:
        return "", 413
    payload = request.get_json(silent=True) or {}
    metrics = {}
    path = payload.get("path")
    if isinstance(path, str) and path.startswith("/") and len(path) <= 200:
        metrics["path"] = path
    for key in ("navigation", "fcp", "lcp", "inp"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 600000:
            metrics[key] = value
    cls = payload.get("cls")
    if isinstance(cls, (int, float)) and not isinstance(cls, bool) and 0 <= cls <= 100:
        metrics["cls"] = cls
    if metrics:
        RUNTIME_METRICS.record_web_vitals(metrics)
    return "", 204

@app.context_processor
def inject_book_context():
    static_paths = (
        os.path.join(app.static_folder, "libflix.css"),
        os.path.join(app.static_folder, "download-ui.js"),
        os.path.join(app.static_folder, "libflix-pwa.js"),
        os.path.join(app.static_folder, "libflix-sw.js"),
        os.path.join(app.static_folder, "manifest.webmanifest"),
        os.path.join(app.static_folder, "libflix-offline.html"),
        os.path.join(app.static_folder, "icons", "libflix-icon-192.png"),
        os.path.join(app.static_folder, "icons", "libflix-icon-512.png"),
        os.path.join(app.static_folder, "icons", "libflix-icon-maskable-512.png"),
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
        "topics_url": clean_topics_url,
        "topic_url": topic_discover_url,
        "discover_url": clean_discover_url,
        "book_url": book_url,
        "asset_version": asset_version,
        "kindle_managed_relay": bool(
            KINDLE_RELAY_HOST and KINDLE_RELAY_USER and KINDLE_RELAY_PASSWORD
        ),
    }

@app.template_filter("size_url")
def size_url(url, size="M"):
    if not url:
        return url
    zoom = {"S": "1", "M": "3", "L": "5"}.get(size, "3")
    local_cover = re.fullmatch(r"/olcover/(\d+)(?:/[SML](?:\.webp)?)?", url)
    if local_cover:
        return open_library_cover_url(local_cover.group(1), size)
    archive_cover = re.fullmatch(
        r"/iacover/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?:/[SML](?:\.webp)?)?",
        url,
    )
    if archive_cover:
        return archive_cover_url(archive_cover.group(1), size)
    remote_cover = re.search(r"covers\.openlibrary\.org/b/id/(\d+)-[SML]\.jpg", url)
    if remote_cover:
        return open_library_cover_url(remote_cover.group(1), size)
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
                schedule_cover_warm(cover_warm_jobs_for_shelves(shelves), force=True)
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
            match = re.fullmatch(
                r"/olcover/(\d+)(?:/[SML](?:\.webp)?)?",
                book_copy.get("cover_url", ""),
            )
            if not match:
                match = re.search(
                    r"covers\.openlibrary\.org/b/id/(\d+)-[SML]\.jpg",
                    book_copy.get("cover_url", ""),
                )
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
    schedule_cover_warm(cover_warm_jobs_for_shelves(shelves), force=True)
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
                hero_items = []
                for book in hero_books:
                    work_id = work_id_from_ol_key(book.get("ol_key", ""))
                    detail, _ = cached_book_detail(work_id, lang) if work_id else (None, "miss")
                    hero_items.append(dict(book, description=(detail or {}).get("description", "")))
                hero = hero_items[0]
    return render_template(
        "index.html",
        shelves=shelves,
        hero=hero,
        hero_books=hero_books,
        hero_items=hero_items,
        featured_topics=TOPIC_FEATURED,
        topic_groups=TOPIC_BROWSE_GROUPS,
        mode=mode,
        error=error,
    )

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

@app.route("/topics", defaults={"clean_mode": None, "clean_lang": None})
@app.route("/cn/topics", defaults={"clean_mode": "nonfiction", "clean_lang": "cn"})
def topics_page(clean_mode, clean_lang):
    mode = "nonfiction"
    lang = clean_lang or get_book_lang()
    g.mode_override = mode
    g.book_lang_override = lang
    if clean_mode is None and ("mode" in request.args or "book_lang" in request.args):
        return preserve_query_redirect(clean_topics_url(mode, lang))
    return render_template(
        "topics.html",
        topic_groups=TOPIC_BROWSE_GROUPS,
        featured_topics=TOPIC_FEATURED,
        mode=mode,
    )

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
    if len(q) > 200:
        return Response("Search query is too long", status=400, mimetype="text/plain")

    requested_page = max(1, int(request.args.get("page", 1)))
    requested_intent = request.args.get("intent", "")
    query_plan = plan_topic_query(q, requested_intent)
    topic_mode = query_plan.intent == "topic"
    topic_filters = normalize_topic_filters(request.args)
    start_here = []
    partial = False
    refresh_partial = False
    snapshot_id = ""
    sources = []
    if topic_mode:
        cached_payload = cached_topic_discovery_payload(
            q,
            lang,
            topic_filters,
            allow_stale=True,
        )
        initial_loading = cached_payload is None
        if cached_payload is None:
            books, total, total_pages = [], 0, max(1, requested_page)
            page = requested_page - 1
            search_unavailable = False
        else:
            paged_payload = paginate_topic_discovery_payload(
                cached_payload,
                requested_page,
            )
            books = paged_payload["books"]
            start_here = paged_payload["start_here"]
            total = paged_payload["total"]
            total_pages = paged_payload["total_pages"]
            partial = bool(paged_payload.get("partial"))
            refresh_partial = bool(
                paged_payload.get("stale")
                or paged_payload.get("refresh_partial")
            )
            sources = paged_payload.get("sources") or []
            snapshot_id = paged_payload.get("snapshot_id") or ""
            search_unavailable = bool(paged_payload.get("source_unavailable"))
            page = requested_page
    else:
        cached_result = cached_discovery_books(q, requested_page, lang)
        initial_loading = cached_result is None
        if cached_result is None:
            books, total, total_pages = [], 0, max(1, requested_page)
            page = requested_page - 1
        else:
            books, total, total_pages = cached_result
            page = requested_page
        search_unavailable = total is None
    if search_unavailable:
        g.cache_control_override = "no-store"
    return render_template(
        "discover.html",
        query=q,
        books=books,
        total=total or 0,
        page=page,
        total_pages=total_pages,
        search_unavailable=search_unavailable,
        initial_loading=initial_loading,
        requested_page=requested_page,
        mode=mode,
        search_value=q,
        topic_mode=topic_mode,
        display_query=query_plan.display_query,
        start_here=start_here,
        partial=partial,
        refresh_partial=refresh_partial,
        snapshot_id=snapshot_id,
        sources=sources,
        active_filters=topic_filters,
    )

@app.route("/api/discover")
def api_discover():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"success": False, "error": "No query provided"})
    if len(q) > 200:
        return jsonify({"success": False, "error": "Search query is too long"}), 400
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid page"}), 400
    if page < 1 or page > 100:
        return jsonify({"success": False, "error": "Invalid page"}), 400
    lang = normalize_book_lang(request.args.get("book_lang")) or get_book_lang()
    query_plan = plan_topic_query(q, request.args.get("intent", ""))
    if query_plan.intent == "topic":
        filters = normalize_topic_filters(request.args)
        if page > 1:
            payload = cached_topic_discovery_payload(
                q,
                lang,
                filters,
                allow_stale=True,
            )
            if payload is None:
                g.cache_control_override = "no-store"
                return jsonify({
                    "success": False,
                    "error": "Topic snapshot is not ready. Load page 1 first.",
                    "code": "snapshot_unavailable",
                    "intent": "topic",
                    "topic_mode": True,
                    "display_query": query_plan.display_query,
                }), 409
            if payload.get("stale"):
                payload["refresh_partial"] = True
            requested_snapshot = request.args.get("snapshot", "").strip()
            current_snapshot = payload.get("snapshot_id") or topic_discovery_snapshot_id(
                payload.get("all_books") or []
            )
            if requested_snapshot and requested_snapshot != current_snapshot:
                g.cache_control_override = "no-store"
                return jsonify({
                    "success": False,
                    "error": "Topic results changed. Refreshing the first page.",
                    "code": "snapshot_changed",
                    "intent": "topic",
                    "topic_mode": True,
                    "display_query": query_plan.display_query,
                    "snapshot_id": current_snapshot,
                }), 409
        else:
            payload = fetch_topic_discovery_payload(q, lang, filters)
        paged = paginate_topic_discovery_payload(payload, page)
        if paged.get("partial") or paged.get("refresh_partial"):
            g.cache_control_override = "no-store"
        if paged.get("source_unavailable"):
            return jsonify({
                "success": False,
                "error": "Book search is temporarily unavailable.",
                "code": "source_unavailable",
                "intent": "topic",
                "topic_mode": True,
                "display_query": query_plan.display_query,
                "partial": True,
                "sources": paged.get("sources") or [],
                "retry_after": paged.get("retry_after") or 0,
            }), 503
        return jsonify({"success": True, "query": q, **paged})

    books, total, total_pages = fetch_discovery_books(q, page, lang)
    if total is None:
        g.cache_control_override = "no-store"
        return jsonify({
            "success": False,
            "error": "Book search is temporarily unavailable.",
            "code": "source_unavailable",
        }), 503
    return jsonify({
        "success": True,
        "query": q,
        "intent": "identity",
        "topic_mode": False,
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
    detail, detail_cache = get_book_detail(work_id, lang)
    if detail is None:
        detail = {
            "title": "Book",
            "localized_title": "",
            "download_title": "",
            "author": "",
            "cover_url": "",
            "description": "",
            "similar_subjects": [],
            "download_queries": [],
            "complete": False,
        }
    return render_template(
        "book.html",
        mode=mode,
        ol_key=ol_key,
        detail_cache=detail_cache,
        **detail,
    )

def similar_cache_key(ol_key, subjects, lang, current_title="", current_authors=None):
    normalized = "|".join(sorted(subject.casefold() for subject in subjects))
    identity = "|".join(
        normalize_match_text(value)
        for value in bounded_identity_values([current_title, current_authors], limit=7)
    )
    return f"similar:v5:{lang}:{ol_key}:{normalized}:{identity}"

def build_similar_books(
    ol_key,
    subjects,
    lang,
    current_title="",
    current_authors=None,
    with_status=False,
):
    subjects = bounded_identity_values(subjects, limit=2)
    current_authors = bounded_identity_values(current_authors, limit=6)

    def fetch_source(source):
        source_type, value = source
        field = "subject" if source_type == "subject" else "author"
        data = ol_get("/search.json", {
            "q": f'{field}:"{value}" language:{BOOK_LANG_CONFIG[lang]["ol_lang"]}',
            "sort": "rating",
            "limit": 30 if source_type == "subject" else 18,
            "fields": OL_SIMILAR_FIELDS,
        })
        if data is None:
            raise requests.RequestException("Open Library recommendation source unavailable")
        return data.get("docs", [])

    sources = [("subject", subject) for subject in subjects]
    if current_authors and len(sources) < SIMILAR_MAX_ORIGIN_QUERIES:
        sources.append(("author", current_authors[0]))
    source_docs = []
    complete = True
    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
        futures = {
            pool.submit(fetch_source, source): source
            for source in sources[:SIMILAR_MAX_ORIGIN_QUERIES]
        }
        for future, source in futures.items():
            try:
                source_docs.append((source, future.result()))
            except Exception:
                complete = False
                source_docs.append((source, []))

    candidates = {}
    sequence = 0
    for source, docs in source_docs:
        seen_in_source = set()
        for record in docs:
            book = extract_book(record, lang)
            if not book or book["ol_key"] == ol_key:
                continue
            key = book["ol_key"]
            entry = candidates.setdefault(key, {
                "book": book,
                "record": record,
                "matches": 0,
                "confirmed_subject_matches": 0,
                "confirmed_specific_subject_matches": 0,
                "author_source": False,
                "order": sequence,
            })
            if key not in seen_in_source:
                if source[0] == "subject":
                    source_subject = normalize_match_text(source[1])
                    record_subjects = {
                        normalize_match_text(subject)
                        for subject in bounded_identity_values(
                            record.get("subject"),
                            limit=40,
                        )
                    }
                    if source_subject and source_subject in record_subjects:
                        entry["matches"] += 1
                        entry["confirmed_subject_matches"] += 1
                        if (
                            str(source[1]).strip().casefold() not in GENERIC_SIMILAR_SUBJECTS
                            and len([
                                term for term in re.findall(
                                    r"[a-z0-9]+",
                                    str(source[1]).casefold(),
                                )
                                if term not in {"and", "of", "the"}
                            ]) >= 2
                        ):
                            entry["confirmed_specific_subject_matches"] += 1
                else:
                    entry["author_source"] = True
                seen_in_source.add(key)
            sequence += 1

    if not current_title:
        current_title = (ol_get_work(ol_key) or {}).get("title", "")
    current_title_key = normalize_title(current_title)
    current_title_tokens = set(relevance_tokens(current_title))
    seen_titles = {current_title_key} if current_title_key else set()
    books = []
    required_subject_matches = min(2, len(subjects))

    def candidate_rank(entry):
        record_authors = bounded_identity_values(
            entry["record"].get("author_name"),
            limit=8,
        )
        author_score = max((
            identity_match(
                author_match_score,
                candidate_author,
                current_authors[0] if current_authors else "",
                current_authors,
            )[0]
            for candidate_author in record_authors
        ), default=0)
        title_overlap = len(
            current_title_tokens & set(relevance_tokens(entry["book"].get("title", "")))
        )
        title_score = title_match_score(
            entry["book"].get("title", ""),
            current_title,
        )
        entry["author_score"] = author_score
        entry["title_overlap"] = title_overlap
        entry["title_score"] = title_score
        return (
            entry["matches"] * 320
            + max(0, author_score) * 2
            + title_overlap * 110
            + max(0, title_score - 600) // 2
            + (100 if entry["author_source"] and author_score >= 180 else 0)
        )

    ranked = sorted(
        candidates.values(),
        key=lambda entry: (-candidate_rank(entry), entry["order"]),
    )

    def append_candidate(entry):
        book = entry["book"]
        title_key = normalize_title(book.get("title", ""))
        if not title_key or title_key in seen_titles:
            return False
        seen_titles.add(title_key)
        books.append(book)
        return True

    for entry in ranked:
        if required_subject_matches > 1 and not (
            entry["matches"] >= required_subject_matches
            or entry.get("author_score", 0) >= 180
            or entry.get("title_overlap", 0) >= 2
            or entry.get("title_score", 0) >= 850
        ):
            continue
        if not entry["matches"] and entry.get("author_score", 0) < 180:
            continue
        append_candidate(entry)
        if len(books) >= 12:
            break

    # Preserve the strict intersection/same-author tier above, then fill an
    # otherwise sparse shelf with candidates whose returned Open Library record
    # explicitly confirms one of the selected multi-token subject sources.
    if required_subject_matches > 1 and len(books) < 3:
        for entry in ranked:
            if not entry.get("confirmed_specific_subject_matches"):
                continue
            append_candidate(entry)
            if len(books) >= 6:
                break
    return (books, complete) if with_status else books

def similar_empty_cache_key(cache_key):
    return f"{cache_key}:empty"

def similar_partial_cache_key(cache_key):
    return f"{cache_key}:partial"

def _refresh_similar_books(
    cache_key,
    ol_key,
    subjects,
    lang,
    current_title="",
    current_authors=None,
):
    try:
        books, complete = build_similar_books(
            ol_key,
            subjects,
            lang,
            current_title=current_title,
            current_authors=current_authors,
            with_status=True,
        )
        payload = {
            "success": True,
            "books": books,
            "refreshing": not complete,
            "partial": not complete,
        }
        if complete and books:
            cache_set(cache_key, payload)
            disk_cache_set(cache_key, payload)
        elif complete:
            payload["negative"] = True
            empty_key = similar_empty_cache_key(cache_key)
            cache_set(empty_key, payload)
            disk_cache_set(empty_key, payload)
        else:
            cache_set(similar_partial_cache_key(cache_key), payload)
    finally:
        with SIMILAR_REFRESH_LOCK:
            SIMILAR_REFRESHING.discard(cache_key)

def schedule_similar_refresh(
    cache_key,
    ol_key,
    subjects,
    lang,
    current_title="",
    current_authors=None,
):
    with SIMILAR_REFRESH_LOCK:
        if cache_key in SIMILAR_REFRESHING:
            return False
        if len(SIMILAR_REFRESHING) >= SIMILAR_REFRESH_PENDING_LIMIT:
            return False
        SIMILAR_REFRESHING.add(cache_key)
    SIMILAR_EXECUTOR.submit(
        _refresh_similar_books,
        cache_key,
        ol_key,
        subjects,
        lang,
        current_title,
        current_authors,
    )
    return True

@app.route("/api/similar")
def api_similar():
    subjects = [
        subject.strip()[:120]
        for subject in request.args.getlist("subject")[:2]
        if subject.strip()
    ]
    ol_key = request.args.get("ol_key", "").strip()
    current_title = request.args.get("title", "").strip()[:180]
    current_author = request.args.get("author", "").strip()[:120]
    current_authors = bounded_identity_values([
        current_author,
        parse_bounded_json_list(request.args.get("author_aliases"), 6),
    ], limit=6)
    lang = get_book_lang()
    if not subjects and not current_authors:
        return jsonify({"success": False, "error": "No subject or author"})
    if not re.fullmatch(r"/works/OL\d+W", ol_key):
        return jsonify({"success": False, "error": "Invalid Open Library work"}), 400
    cache_key = similar_cache_key(
        ol_key,
        subjects,
        lang,
        current_title,
        current_authors,
    )
    payload = cache_get(cache_key, SIMILAR_FRESH_TTL) or disk_cache_get(cache_key, SIMILAR_FRESH_TTL)
    if payload:
        payload = {**payload, "books": canonicalize_book_covers(payload.get("books", []))}
        add_server_timing("similar", duration=0, description="cache")
        return jsonify(payload)
    empty_key = similar_empty_cache_key(cache_key)
    empty = cache_get(empty_key, SIMILAR_EMPTY_TTL) or disk_cache_get(empty_key, SIMILAR_EMPTY_TTL)
    if empty:
        add_server_timing("similar", duration=0, description="negative-cache")
        return jsonify(empty)
    partial = cache_get(similar_partial_cache_key(cache_key), SIMILAR_PARTIAL_TTL)
    stale = disk_cache_get_stale(cache_key, SIMILAR_STALE_TTL)
    if stale:
        stale = {**stale, "books": canonicalize_book_covers(stale.get("books", []))}
        if not partial:
            schedule_similar_refresh(
                cache_key, ol_key, subjects, lang, current_title, current_authors
            )
        add_server_timing(
            "similar",
            duration=0,
            description="stale-cooldown" if partial else "stale",
        )
        g.cache_control_override = "private, max-age=15"
        return jsonify({**stale, "refreshing": True})
    if partial:
        add_server_timing("similar", duration=0, description="partial-cache")
        g.cache_control_override = "private, max-age=5"
        return jsonify(partial)
    scheduled = schedule_similar_refresh(
        cache_key, ol_key, subjects, lang, current_title, current_authors
    )
    if not scheduled:
        with SIMILAR_REFRESH_LOCK:
            already_refreshing = cache_key in SIMILAR_REFRESHING
        if not already_refreshing:
            g.cache_control_override = "no-store"
            return jsonify({
                "success": False,
                "error": "Recommendations are busy. Please try again shortly.",
                "code": "refresh_capacity",
            }), 503
    add_server_timing("similar", duration=0, description="background")
    g.cache_control_override = "private, max-age=2"
    return jsonify({"success": True, "books": [], "refreshing": True})

@app.route("/api/book")
def api_book():
    ol_key = request.args.get("ol_key", "").strip()
    if not ol_key:
        return jsonify({"success": False, "error": "No ol_key provided"})
    if not re.fullmatch(r"/works/OL\d+W", ol_key):
        return jsonify({"success": False, "error": "Invalid Open Library work"}), 400
    work_id = work_id_from_ol_key(ol_key)
    detail, cache_state = get_book_detail(work_id, get_book_lang())
    if not detail:
        status = 202 if cache_state == "miss" else 404
        if status == 202:
            g.cache_control_override = "no-store"
        return jsonify({
            "success": False,
            "error": "Book details are loading" if status == 202 else "Book not found",
            "refreshing": status == 202,
            "cache": cache_state,
        }), status
    response_payload = dict(detail)
    response_payload["refreshing"] = cache_state in ("stale", "fallback", "miss") or not detail.get("complete")
    response_payload["cache"] = cache_state
    if response_payload["refreshing"]:
        g.cache_control_override = "no-store"
    if request.args.get("description_only") == "1":
        response_payload = {
            "success": True,
            "description": detail.get("description", ""),
            "refreshing": response_payload["refreshing"],
            "cache": cache_state,
        }
    add_server_timing("book", duration=0, description=cache_state)
    return jsonify(response_payload)

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

def download_cover_url(md5, cover_dir, size="S"):
    md5 = str(md5 or "").lower()
    cover_dir = str(cover_dir or "")
    size = size if size in ("S", "M", "L") else "S"
    if not re.fullmatch(r"[a-f0-9]{32}", md5):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", cover_dir):
        return ""
    return f"/cover/{md5}/{size}.webp?{urlencode({'dir': cover_dir})}"

def parse_bounded_json_list(
    value,
    limit=DOWNLOAD_IDENTITY_VALUE_LIMIT,
    *,
    max_bytes=IDENTITY_QUERY_JSON_MAX_BYTES,
    max_chars=IDENTITY_QUERY_VALUE_MAX_CHARS,
):
    raw_value = str(value or "")
    if len(raw_value.encode("utf-8")) > max_bytes:
        return []
    try:
        parsed = json.loads(raw_value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return []
    if not isinstance(parsed, list):
        return []
    return [
        item for item in bounded_identity_values(parsed, limit=limit)
        if len(item) <= max_chars
    ]

@dataclass
class DownloadAliasSearchOutcome:
    books: list
    total: int
    complete: bool
    errors: list
    queries: list

def download_book_merge_key(book):
    key = str(getattr(book, "book_id", "") or "").casefold()
    if key:
        return key
    return "|".join((
        normalize_title(book.title),
        normalize_author(book.author),
        str(book.ext or "").casefold(),
        str(book.size or "").casefold(),
    ))

def merge_download_books(existing, additions):
    merged = list(existing or [])
    seen = {download_book_merge_key(book) for book in merged}
    for book in additions or []:
        key = download_book_merge_key(book)
        if key in seen:
            continue
        seen.add(key)
        merged.append(book)
    return merged

def search_download_aliases(queries, *, sort, order, page, limit):
    """Search a small identity set and merge results in query priority order."""
    queries = bounded_identity_values(queries, limit=DOWNLOAD_ALIAS_SEARCH_LIMIT)
    if not queries:
        return DownloadAliasSearchOutcome([], 0, True, [], [])
    if len(queries) == 1:
        try:
            books, total = DOWNLOADER.search(
                queries[0], sort=sort, order=order, page=page, limit=limit
            )
            return DownloadAliasSearchOutcome(books, total, True, [], queries)
        except Exception as error:
            return DownloadAliasSearchOutcome([], 0, False, [error], queries)

    results = [None] * len(queries)
    errors = []
    with ThreadPoolExecutor(max_workers=min(3, len(queries))) as pool:
        futures = {
            pool.submit(
                DOWNLOADER.search,
                query,
                sort=sort,
                order=order,
                page=page,
                limit=limit,
            ): index
            for index, query in enumerate(queries)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as error:
                errors.append(error)

    successful = [result for result in results if result is not None]
    merged = []
    totals = []
    for books, total in successful:
        totals.append(total or 0)
        merged = merge_download_books(merged, books)
    return DownloadAliasSearchOutcome(
        merged,
        max(totals, default=len(merged)),
        not errors,
        errors,
        queries,
    )

def filter_download_candidates(
    books,
    *,
    lang_filter,
    fmt_filter,
    target_title,
    target_author,
    target_titles,
    target_authors,
):
    filtered = []
    for book in books:
        if not is_visible_kindle_format(book.ext):
            continue
        if lang_filter and not book_matches_language(book, lang_filter):
            continue
        if fmt_filter and book.ext.lower() != fmt_filter.lower():
            continue
        if not download_book_is_relevant(
            book,
            target_title,
            target_author,
            target_titles=target_titles,
            target_authors=target_authors,
        ):
            continue
        filtered.append(book)
    return filtered

def has_high_confidence_epub(
    books,
    *,
    target_title,
    target_author,
    target_titles,
    target_authors,
):
    author_targets = bounded_identity_values([target_author, target_authors], limit=6)
    for book in books:
        if (book.ext or "").casefold() != "epub":
            continue
        title_score, _ = identity_match(
            title_match_score, book.title, target_title, target_titles
        )
        author_score, _ = identity_match(
            author_match_score, book.author, target_author, target_authors
        )
        if title_score < 900:
            continue
        if author_targets and author_score < 180 and title_score < 980:
            continue
        return True
    return False

def preferred_download_search_error(errors):
    return (
        next((error for error in errors if isinstance(error, requests.Timeout)), None)
        or next((error for error in errors if isinstance(error, requests.RequestException)), None)
        or (errors[0] if errors else RuntimeError("No download search completed"))
    )

def server_download_identity(ol_key, lang):
    work_id = work_id_from_ol_key(ol_key)
    if not work_id:
        return {}
    detail, _ = get_book_detail(work_id, lang)
    return detail or {}

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
    if fmt not in ("all", "epub", "pdf"):
        fmt = "all"
    default_download_lang = "Chinese" if get_book_lang() == "cn" else "English"
    lang = request.args.get("lang", default_download_lang)
    if lang not in ("English", "Chinese", "all"):
        lang = default_download_lang
    dedup_on = request.args.get("dedup", "1") == "1"
    ol_key = request.args.get("ol_key", "").strip()
    identity_detail = server_download_identity(ol_key, get_book_lang())
    target_title = (
        request.args.get("target_title", "").strip()
        or identity_detail.get("title", "")
        or q
    )[:180]
    target_author = (
        request.args.get("target_author", "").strip()
        or identity_detail.get("author", "")
    )[:180]
    fallback_search_aliases = parse_bounded_json_list(
        request.args.get("search_aliases"),
        3,
        max_bytes=768,
        max_chars=80,
    )
    target_titles = bounded_identity_values([
        identity_detail.get("title_aliases"),
        parse_bounded_json_list(request.args.get("target_title_aliases"), 4),
    ], limit=DOWNLOAD_IDENTITY_VALUE_LIMIT)
    target_authors = bounded_identity_values([
        identity_detail.get("authors"),
        parse_bounded_json_list(request.args.get("target_author_aliases"), 3),
    ], limit=6)
    planned_queries = bounded_identity_values(
        [q, identity_detail.get("download_queries"), fallback_search_aliases],
        limit=DOWNLOAD_ALIAS_SEARCH_LIMIT,
    )
    if page > 1:
        planned_queries = [q]
    identity_signature = json.dumps(
        [planned_queries, target_titles, target_authors],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result_cache_key = (
        f"download_search:v10:{sort}:{order}:{limit}:{page}:"
        f"{fmt}:{lang}:{int(dedup_on)}:{target_title}:{target_author}:"
        f"{identity_signature}"
    )
    cached_result = cache_get(result_cache_key, 900)
    if cached_result is None:
        cached_result = disk_cache_get(result_cache_key, 900)
        if cached_result is not None:
            cache_set(result_cache_key, cached_result)
    if cached_result is not None:
        add_server_timing("downloads", duration=0, description="cache")
        return jsonify(cached_result)

    sort_field = "y" if sort in ("year", "best_match") else sort
    download_started = time.perf_counter()
    lang_filter = None if lang == "all" else lang
    fmt_filter = None if fmt == "all" else fmt
    books = []
    total = 0
    searched_queries = []
    search_complete = True
    search_errors = []
    try:
        for offset in range(0, len(planned_queries), DOWNLOAD_ALIAS_BATCH_SIZE):
            batch = planned_queries[offset:offset + DOWNLOAD_ALIAS_BATCH_SIZE]
            outcome = search_download_aliases(
                batch,
                sort=sort_field,
                order=order,
                page=page,
                limit=limit,
            )
            searched_queries.extend(outcome.queries)
            books = merge_download_books(books, outcome.books)
            total = max(total, outcome.total)
            search_complete = search_complete and outcome.complete
            search_errors.extend(outcome.errors)
            filtered_preview = filter_download_candidates(
                books,
                lang_filter=lang_filter,
                fmt_filter=fmt_filter,
                target_title=target_title,
                target_author=target_author,
                target_titles=target_titles,
                target_authors=target_authors,
            )
            if (
                page == 1
                and sort == "best_match"
                and has_high_confidence_epub(
                    filtered_preview,
                    target_title=target_title,
                    target_author=target_author,
                    target_titles=target_titles,
                    target_authors=target_authors,
                )
            ):
                break
        books = filter_download_candidates(
            books,
            lang_filter=lang_filter,
            fmt_filter=fmt_filter,
            target_title=target_title,
            target_author=target_author,
            target_titles=target_titles,
            target_authors=target_authors,
        )
        if search_errors and not books:
            raise preferred_download_search_error(search_errors)
    except requests.Timeout:
        stale = disk_cache_get_stale(result_cache_key, 604800)
        if stale:
            g.cache_control_override = "private, max-age=15"
            add_server_timing("downloads", download_started, description="stale-timeout")
            return jsonify({**stale, "stale": True})
        return jsonify({
            "success": False,
            "error": "The download source timed out.",
            "code": "source_timeout",
        }), 504
    except requests.RequestException:
        stale = disk_cache_get_stale(result_cache_key, 604800)
        if stale:
            g.cache_control_override = "private, max-age=15"
            add_server_timing("downloads", download_started, description="stale-unavailable")
            return jsonify({**stale, "stale": True})
        return jsonify({
            "success": False,
            "error": "The download source is temporarily unreachable.",
            "code": "source_unavailable",
        }), 503
    except Exception:
        stale = disk_cache_get_stale(result_cache_key, 604800)
        if stale:
            g.cache_control_override = "private, max-age=15"
            add_server_timing("downloads", download_started, description="stale-error")
            return jsonify({**stale, "stale": True})
        return jsonify({
            "success": False,
            "error": "Downloads could not be checked right now.",
            "code": "search_failed",
        }), 502

    _, scorer = rank_download_books(
        books,
        target_title=target_title,
        target_author=target_author,
        preferred_language=lang_filter or "",
        target_titles=target_titles,
        target_authors=target_authors,
    )
    if dedup_on:
        books = dedup(books, scorer)
    fastest_book = fastest_kindle_candidate(
        books,
        target_title=target_title,
        target_author=target_author,
        preferred_language=lang_filter or "",
        target_titles=target_titles,
        target_authors=target_authors,
    )
    if sort == "best_match":
        books, scorer = rank_download_books(
            books,
            target_title=target_title,
            target_author=target_author,
            preferred_language=lang_filter or "",
            target_titles=target_titles,
            target_authors=target_authors,
        )
        if fastest_book in books:
            books = [fastest_book] + [book for book in books if book is not fastest_book]
        best_book = fastest_book or (max(books, key=scorer) if books else None)
    else:
        _, scorer = rank_download_books(
            books,
            target_title=target_title,
            target_author=target_author,
            preferred_language=lang_filter or "",
            target_titles=target_titles,
            target_authors=target_authors,
        )
        best_book = max(books, key=scorer) if books else None

    matched_total = len(books)
    books = books[:limit]
    multi_identity_search = len(planned_queries) > 1
    if multi_identity_search:
        total = matched_total
        total_pages = 1
    else:
        total = max(total or 0, len(books))
        total_pages = (total + limit - 1) // limit if total else 1
    result_books = []
    for i, b in enumerate(books):
        d = b.to_dict(i + 1 + (page - 1) * limit)
        cover_dir = getattr(b, "cover_dir", "")
        d["cover_url"] = download_cover_url(d["md5"], cover_dir)
        d["recommendation_reasons"] = recommendation_reasons(
            b,
            target_title=target_title,
            target_author=target_author,
            preferred_language=lang_filter or "",
            fastest_to_kindle=b is fastest_book,
            target_titles=target_titles,
            target_authors=target_authors,
        )
        d["kindle_compatible"] = is_kindle_delivery_format(b.ext)
        d["fastest_to_kindle"] = b is fastest_book
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
        "searched_queries": searched_queries,
        "complete": search_complete,
        "partial": not search_complete,
    }
    if search_complete:
        cache_set(result_cache_key, result)
        disk_cache_set(result_cache_key, result)
    else:
        g.cache_control_override = "private, max-age=5"
    add_server_timing(
        "downloads",
        download_started,
        description="origin" if search_complete else "partial",
    )
    return jsonify(result)

@app.route("/download/<md5>")
def download(md5):
    if not re.fullmatch(r"[a-fA-F0-9]{32}", md5 or ""):
        return jsonify({"success": False, "error": "Invalid download identifier."}), 404
    md5 = md5.lower()
    filename = request.args.get("filename", f"{md5}.epub")
    filename = re.sub(r'[\r\n\\/\"<>|:*?]+', ' ', filename)
    filename = re.sub(r'\s+', ' ', filename).strip()[:140] or f"{md5}.epub"
    ascii_filename = filename.encode("ascii", "ignore").decode().strip()
    ascii_filename = re.sub(r'[^A-Za-z0-9._ -]+', '', ascii_filename) or f"{md5}.epub"
    upstream = None
    chunks = None
    first_chunk = b""
    range_header = request.headers.get("Range", "")
    if not re.fullmatch(r"bytes=\d*-\d*", range_header):
        range_header = ""
    for attempt in range(2):
        url = DOWNLOADER.resolve_download(md5)
        if not url:
            DOWNLOADER.invalidate_download(md5)
            continue
        try:
            upstream = DL_SESSION.get(
                url,
                stream=True,
                timeout=(5, 60),
                allow_redirects=True,
                headers={"Range": range_header} if range_header else None,
            )
            upstream.raise_for_status()
            chunks = upstream.iter_content(chunk_size=65536)
            first_chunk = next(chunks, b"")
            content_type = upstream.headers.get("Content-Type", "").lower()
            leading = first_chunk.lstrip()[:64].lower()
            if (
                not first_chunk
                or "text/html" in content_type
                or leading.startswith((b"<!doctype html", b"<html"))
            ):
                raise requests.RequestException("Download source returned a web page")
            break
        except (requests.RequestException, OSError):
            if upstream is not None:
                upstream.close()
            upstream = None
            chunks = None
            first_chunk = b""
            DOWNLOADER.invalidate_download(md5)
            if attempt == 0:
                continue
    if upstream is None or chunks is None or not first_chunk:
        return jsonify({
            "success": False,
            "error": "The download source did not return a working file link.",
        }), 502

    def generate():
        try:
            yield first_chunk
            yield from chunks
        finally:
            upstream.close()

    resp = Response(stream_with_context(generate()),
                    status=upstream.status_code,
                    mimetype=(upstream.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0])
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quote(filename)}'
    )
    for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
        value = upstream.headers.get(header)
        if value:
            resp.headers[header] = value
    return resp

def cover_dimensions(size):
    return {
        "S": (120, 180),
        "M": (360, 540),
        "L": (720, 1080),
    }.get(size, (360, 540))

def cover_cache_path(namespace, identity, size):
    digest = hashlib.sha256(f"{namespace}:{identity}:{size}".encode("utf-8")).hexdigest()
    extension = "webp" if Image is not None else "jpg"
    return os.path.join(COVER_CACHE_DIR, namespace, digest[:2], f"{digest}.{extension}")

def cover_identity_lock_path(namespace, identity, cache_path):
    cache_root = os.path.abspath(COVER_CACHE_DIR)
    candidate = os.path.abspath(cache_path)
    try:
        lock_root = cache_root if os.path.commonpath((cache_root, candidate)) == cache_root else os.path.dirname(candidate)
    except ValueError:
        lock_root = os.path.dirname(candidate)
    digest = hashlib.sha256(f"{namespace}:{identity}".encode("utf-8")).hexdigest()
    return os.path.join(lock_root, ".locks", digest[:2], f"{digest}.lock")

@contextmanager
def filesystem_lock(lock_path):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

@contextmanager
def cover_lock(lock_path):
    digest = hashlib.sha256(lock_path.encode("utf-8")).digest()
    stripe = COVER_LOCK_STRIPES[int.from_bytes(digest[:2], "big") % len(COVER_LOCK_STRIPES)]
    with stripe:
        with filesystem_lock(lock_path):
            yield

def _prune_cover_failures_locked(now):
    for path, failed_at in list(COVER_FAILURES.items()):
        if now - failed_at >= COVER_NEGATIVE_TTL:
            COVER_FAILURES.pop(path, None)
    overflow = len(COVER_FAILURES) - COVER_FAILURE_LIMIT
    if overflow > 0:
        oldest = sorted(COVER_FAILURES, key=COVER_FAILURES.get)[:overflow]
        for path in oldest:
            COVER_FAILURES.pop(path, None)

def cover_failure_marker_path(cache_path):
    if not os.path.isabs(cache_path):
        return ""
    cache_root = os.path.abspath(COVER_CACHE_DIR)
    candidate = os.path.abspath(cache_path)
    try:
        marker_root = cache_root if os.path.commonpath((cache_root, candidate)) == cache_root else os.path.dirname(candidate)
    except ValueError:
        marker_root = os.path.dirname(candidate)
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return os.path.join(marker_root, ".failures", digest[:2], f"{digest}.failed")

def recent_cover_failure(cache_path):
    now = time.time()
    with COVER_STATE_LOCK:
        _prune_cover_failures_locked(now)
        failed_at = COVER_FAILURES.get(cache_path, 0)
        if failed_at and now - failed_at < COVER_NEGATIVE_TTL:
            return True
    marker = cover_failure_marker_path(cache_path)
    if not marker:
        return False
    try:
        failed_at = os.path.getmtime(marker)
        if now - failed_at < COVER_NEGATIVE_TTL:
            with COVER_STATE_LOCK:
                COVER_FAILURES[cache_path] = failed_at
            return True
        os.unlink(marker)
    except OSError:
        pass
    return False

def remember_cover_failure(cache_path):
    now = time.time()
    with COVER_STATE_LOCK:
        _prune_cover_failures_locked(now)
        if cache_path not in COVER_FAILURES and len(COVER_FAILURES) >= COVER_FAILURE_LIMIT:
            oldest = min(COVER_FAILURES, key=COVER_FAILURES.get, default=None)
            if oldest is not None:
                COVER_FAILURES.pop(oldest, None)
        COVER_FAILURES[cache_path] = now
    marker = cover_failure_marker_path(cache_path)
    if not marker:
        return
    temporary = ""
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        temporary = f"{marker}.{uuid.uuid4().hex}.tmp"
        with open(temporary, "w") as marker_file:
            marker_file.write(str(now))
        os.replace(temporary, marker)
    except OSError:
        pass
    finally:
        try:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass

def clear_cover_failure(cache_path):
    with COVER_STATE_LOCK:
        COVER_FAILURES.pop(cache_path, None)
    marker = cover_failure_marker_path(cache_path)
    if marker:
        try:
            os.unlink(marker)
        except OSError:
            pass

def cover_file_fingerprint(cache_path):
    stat = os.stat(cache_path)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

def forget_validated_cover(cache_path):
    with COVER_STATE_LOCK:
        COVER_VALIDATED_FILES.pop(cache_path, None)

def remember_validated_cover(cache_path, fingerprint=None):
    try:
        fingerprint = fingerprint or cover_file_fingerprint(cache_path)
    except OSError:
        return
    with COVER_STATE_LOCK:
        if cache_path not in COVER_VALIDATED_FILES and len(COVER_VALIDATED_FILES) >= COVER_VALIDATION_LIMIT:
            COVER_VALIDATED_FILES.pop(next(iter(COVER_VALIDATED_FILES)), None)
        COVER_VALIDATED_FILES[cache_path] = fingerprint

def cover_cache_file_is_valid(cache_path):
    try:
        fingerprint = cover_file_fingerprint(cache_path)
        if fingerprint[2] <= 100:
            forget_validated_cover(cache_path)
            return False
        with COVER_STATE_LOCK:
            if COVER_VALIDATED_FILES.get(cache_path) == fingerprint:
                return True
        if Image is None:
            remember_validated_cover(cache_path, fingerprint)
            return True
        with Image.open(cache_path) as source:
            dimensions_valid = (
                source.width >= 40
                and source.height >= 60
                and source.width <= source.height * 1.5
                and source.height <= source.width * 3.5
            )
            if not dimensions_valid:
                forget_validated_cover(cache_path)
                return False
            source.verify()
        remember_validated_cover(cache_path, fingerprint)
        return True
    except (OSError, UnidentifiedImageError):
        forget_validated_cover(cache_path)
        return False

def _replace_cover_file(cache_path, writer):
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    temporary = f"{cache_path}.{uuid.uuid4().hex}.tmp"
    try:
        writer(temporary)
        os.replace(temporary, cache_path)
        remember_validated_cover(cache_path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass

def write_optimized_cover_variants(content, targets):
    targets = list(dict.fromkeys(targets))
    if not targets:
        return
    if Image is None:
        def write_original(temporary):
            with open(temporary, "wb") as output:
                output.write(content)

        for index, (cache_path, _size) in enumerate(targets):
            try:
                _replace_cover_file(cache_path, write_original)
            except OSError:
                if index == 0:
                    raise
        return

    with Image.open(io.BytesIO(content)) as source:
        if (
            source.width < 40
            or source.height < 60
            or source.width > source.height * 1.5
            or source.height > source.width * 3.5
        ):
            raise UnidentifiedImageError("Cover image has invalid dimensions")
        source.load()
        converted = source.convert("RGB")
        for index, (cache_path, size) in enumerate(targets):
            try:
                def save_variant(temporary, requested_size=size):
                    variant = converted.copy()
                    variant.thumbnail(cover_dimensions(requested_size), Image.Resampling.LANCZOS)
                    variant.save(temporary, format="WEBP", quality=82, method=4)
                _replace_cover_file(cache_path, save_variant)
            except OSError:
                if index == 0:
                    raise

def write_optimized_cover(content, cache_path, size):
    write_optimized_cover_variants(content, [(cache_path, size)])

def cover_variant_sizes(namespace, requested_size):
    sizes = ("S", "M", "L")
    if namespace == "openlibrary":
        return sizes[:sizes.index(requested_size) + 1]
    return sizes

def ensure_cover_cached(namespace, identity, size, source_url, referer=""):
    size = size if size in ("S", "M", "L") else "M"
    cache_path = cover_cache_path(namespace, identity, size)
    hit = cover_cache_file_is_valid(cache_path)
    if hit:
        clear_cover_failure(cache_path)
        return cache_path, True
    if recent_cover_failure(cache_path):
        return None, False
    lock_path = cover_identity_lock_path(namespace, identity, cache_path)
    with cover_lock(lock_path):
        hit = cover_cache_file_is_valid(cache_path)
        if hit:
            clear_cover_failure(cache_path)
            return cache_path, True
        if recent_cover_failure(cache_path):
            return None, False
        try:
            if os.path.exists(cache_path):
                os.unlink(cache_path)
                forget_validated_cover(cache_path)
        except OSError:
            pass
        headers = {"Referer": referer} if referer else {}
        origin_slot = COVER_ORIGIN_SEMAPHORE.acquire(timeout=2)
        if not origin_slot:
            return None, False
        try:
            response = SESSION.get(
                source_url,
                timeout=(3, 10),
                headers=headers,
            )
            response.raise_for_status()
            if (
                len(response.content) <= 100
                or not response.headers.get("Content-Type", "").lower().startswith("image/")
                or urlsplit(response.url).path.endswith("/images/notfound.png")
            ):
                raise UnidentifiedImageError("Cover source returned no usable image")
            targets = [(cache_path, size)]
            for variant_size in cover_variant_sizes(namespace, size):
                variant_path = cover_cache_path(namespace, identity, variant_size)
                if variant_path == cache_path or cover_cache_file_is_valid(variant_path):
                    continue
                targets.append((variant_path, variant_size))
            write_optimized_cover_variants(response.content, targets)
        except (requests.RequestException, OSError, UnidentifiedImageError):
            remember_cover_failure(cache_path)
            return None, False
        finally:
            COVER_ORIGIN_SEMAPHORE.release()
    clear_cover_failure(cache_path)
    return cache_path, False

def cached_cover_response(namespace, identity, size, source_url, referer=""):
    started = time.perf_counter()
    cache_path, hit = ensure_cover_cached(namespace, identity, size, source_url, referer)
    if not cache_path:
        return "", 404
    response = send_file(
        cache_path,
        mimetype="image/webp" if Image is not None else "image/jpeg",
        conditional=True,
        max_age=2592000,
    )
    response.headers["Cache-Control"] = "public, max-age=2592000, stale-while-revalidate=604800, immutable"
    response.headers["X-LibFlix-Cover-Cache"] = "HIT" if hit else "MISS"
    add_server_timing("cover", started, description="hit" if hit else "miss")
    return response

@app.route("/cover/<md5>")
def cover_default(md5):
    return cover(md5, "S")

@app.route("/cover/<md5>/<size>")
@app.route("/cover/<md5>/<size>.webp")
def cover(md5, size):
    if not re.fullmatch(r"[a-fA-F0-9]{32}", md5):
        return "", 404
    cover_dir = request.args.get("dir", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", cover_dir):
        return "", 404
    url = f"{MIRROR}/covers/{cover_dir}/{md5}.jpg"
    return cached_cover_response("downloads", f"{cover_dir}:{md5.lower()}", size.upper(), url, f"{MIRROR}/")

@app.route("/olcover/<int:cover_id>")
@app.route("/olcover/<int:cover_id>/<size>")
@app.route("/olcover/<int:cover_id>/<size>.webp")
def olcover(cover_id, size="M"):
    if cover_id <= 0 or cover_id > 100_000_000:
        return "", 404
    s = size.upper() if size.upper() in ("S", "M", "L") else "M"
    url = f"https://covers.openlibrary.org/b/id/{cover_id}-{s}.jpg"
    return cached_cover_response("openlibrary", str(cover_id), s, url)

@app.route("/iacover/<identifier>")
@app.route("/iacover/<identifier>/<size>")
@app.route("/iacover/<identifier>/<size>.webp")
def iacover(identifier, size="M"):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", identifier):
        return "", 404
    s = size.upper() if size.upper() in ("S", "M", "L") else "M"
    url = f"https://archive.org/services/img/{identifier}"
    return cached_cover_response("internetarchive", identifier, s, url)


def _cover_file_as_jpeg(cache_path):
    if not cache_path:
        return b""
    try:
        with open(cache_path, "rb") as cover_file:
            content = cover_file.read()
        if Image is None:
            return content if content.startswith(b"\xff\xd8") else b""
        with Image.open(io.BytesIO(content)) as source:
            source.thumbnail((1200, 1800), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            source.convert("RGB").save(output, format="JPEG", quality=88, optimize=True)
            return output.getvalue()
    except (OSError, UnidentifiedImageError):
        return b""


def _cached_cover_variant(namespace, identity):
    for size in ("L", "M", "S"):
        expected = cover_cache_path(namespace, identity, size)
        base, extension = os.path.splitext(expected)
        candidates = (expected, f"{base}.jpg" if extension != ".jpg" else f"{base}.webp")
        for candidate in candidates:
            try:
                if os.path.getsize(candidate) > 100:
                    return candidate
            except OSError:
                continue
    return ""


def _kindle_cover_bytes(cover_url):
    parsed = urlsplit(str(cover_url or "").strip())
    if parsed.scheme or parsed.netloc:
        return b""
    open_library_match = re.fullmatch(r"/olcover/(\d+)(?:/[SML])?(?:\.webp)?", parsed.path)
    if open_library_match:
        cover_id = open_library_match.group(1)
        cached = _cached_cover_variant("openlibrary", cover_id)
        if cached:
            return _cover_file_as_jpeg(cached)
        source_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
        cache_path, _ = ensure_cover_cached(
            "openlibrary",
            cover_id,
            "L",
            source_url,
        )
        return _cover_file_as_jpeg(cache_path)

    archive_match = re.fullmatch(
        r"/iacover/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?:/[SML])?(?:\.webp)?",
        parsed.path,
    )
    if archive_match:
        identifier = archive_match.group(1)
        cached = _cached_cover_variant("internetarchive", identifier)
        if cached:
            return _cover_file_as_jpeg(cached)
        cache_path, _ = ensure_cover_cached(
            "internetarchive",
            identifier,
            "L",
            f"https://archive.org/services/img/{identifier}",
        )
        return _cover_file_as_jpeg(cache_path)

    download_match = re.fullmatch(r"/cover/([a-fA-F0-9]{32})(?:/[SML])?(?:\.webp)?", parsed.path)
    cover_dir = (parse_qs(parsed.query).get("dir") or [""])[0]
    if download_match and re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", cover_dir):
        md5 = download_match.group(1).lower()
        identity = f"{cover_dir}:{md5}"
        cached = _cached_cover_variant("downloads", identity)
        if cached:
            return _cover_file_as_jpeg(cached)
        source_url = f"{MIRROR}/covers/{cover_dir}/{md5}.jpg"
        cache_path, _ = ensure_cover_cached(
            "downloads",
            identity,
            "L",
            source_url,
            f"{MIRROR}/",
        )
        return _cover_file_as_jpeg(cache_path)
    return b""


def _kindle_progress(stage, progress=None, detail=""):
    event = {
        "type": "progress",
        "stage": stage,
        "progress": progress,
        "timestamp": round(time.time(), 3),
    }
    if detail:
        event["detail"] = detail
    return event


class KindleProgressTracker:
    def __init__(self):
        self.started_at = time.perf_counter()
        self.stage_started_at = self.started_at
        self.stage = ""
        self.stage_durations = {}

    def event(self, stage, progress=None, detail=""):
        now = time.perf_counter()
        if stage != self.stage:
            if self.stage:
                self.stage_durations[self.stage] = round(
                    self.stage_durations.get(self.stage, 0)
                    + now - self.stage_started_at,
                    3,
                )
            self.stage = stage
            self.stage_started_at = now
        event = _kindle_progress(stage, progress, detail)
        event["elapsed_seconds"] = round(now - self.started_at, 3)
        event["stage_elapsed_seconds"] = round(now - self.stage_started_at, 3)
        return event

    def durations(self):
        durations = dict(self.stage_durations)
        if self.stage:
            durations[self.stage] = round(
                durations.get(self.stage, 0) + time.perf_counter() - self.stage_started_at,
                3,
            )
        return durations


RESOLVE_HEARTBEAT_SECONDS = 4
DOWNLOAD_MAX_ATTEMPTS = 4
DOWNLOAD_READ_TIMEOUT = 60


def _resolve_download_progress(md5, emit=None, abort_check=None):
    emit = emit or _kindle_progress
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(DOWNLOADER.resolve_download, md5)
        while True:
            try:
                return future.result(timeout=RESOLVE_HEARTBEAT_SECONDS)
            except FutureTimeoutError:
                if abort_check:
                    abort_check()
                yield emit("Finding book file", None, "Waiting for the download source")


def _format_transfer_size(value):
    value = max(0, int(value or 0))
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024


def _download_book_progress(
    url,
    destination,
    *,
    md5="",
    extension="",
    emit=None,
    abort_check=None,
):
    emit = emit or _kindle_progress
    downloaded = os.path.getsize(destination) if os.path.exists(destination) else 0
    total_bytes = 0
    last_reported_progress = 10
    last_reported_bytes = downloaded
    last_error = None

    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        resume_at = downloaded
        headers = {"Accept-Encoding": "identity"}
        if resume_at:
            headers["Range"] = f"bytes={resume_at}-"
        response = None
        try:
            if abort_check:
                abort_check()
            response = SESSION.get(
                url,
                stream=True,
                timeout=(10, DOWNLOAD_READ_TIMEOUT),
                allow_redirects=True,
                headers=headers,
            )
            if resume_at and response.status_code == 416:
                complete_range = re.fullmatch(
                    r"bytes \*/(\d+)",
                    response.headers.get("content-range", ""),
                )
                complete_size = int(complete_range.group(1)) if complete_range else 0
                if complete_size and downloaded == complete_size:
                    if md5 and extension:
                        validation = validate_source_file(
                            destination,
                            md5,
                            extension,
                            expected_size=complete_size,
                        )
                        return downloaded, complete_size, validation
                    return downloaded, complete_size
                with open(destination, "wb"):
                    pass
                downloaded = 0
                total_bytes = 0
                raise requests.RequestException("Download range was no longer valid")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if "text/html" in content_type or "application/xhtml" in content_type:
                with open(destination, "wb"):
                    pass
                downloaded = 0
                total_bytes = 0
                raise SourceFileError("Download source returned a web page")

            append = False
            content_range = response.headers.get("content-range", "")
            range_match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
            if resume_at and response.status_code == 206:
                if not range_match or int(range_match.group(1)) != resume_at:
                    with open(destination, "wb"):
                        pass
                    downloaded = 0
                    total_bytes = 0
                    raise requests.RequestException("Download source returned a mismatched byte range")
                append = True
            elif resume_at:
                downloaded = 0
                resume_at = 0

            if range_match and range_match.group(3).isdigit():
                total_bytes = int(range_match.group(3))
            elif not append:
                try:
                    total_bytes = int(response.headers.get("content-length", "") or 0)
                except (TypeError, ValueError):
                    total_bytes = 0

            mode = "ab" if append else "wb"
            with open(destination, mode) as output:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    if abort_check:
                        abort_check()
                    output.write(chunk)
                    downloaded += len(chunk)
                    if total_bytes:
                        current = 10 + int(min(downloaded / total_bytes, 1) * 55)
                        if current >= last_reported_progress + 2:
                            last_reported_progress = current
                            detail = f"{_format_transfer_size(downloaded)} of {_format_transfer_size(total_bytes)}"
                            yield emit("Downloading book", current, detail)
                    elif downloaded - last_reported_bytes >= 1024 * 1024:
                        last_reported_bytes = downloaded
                        yield emit(
                            "Downloading book",
                            None,
                            f"{_format_transfer_size(downloaded)} downloaded",
                        )
            if total_bytes and downloaded != total_bytes:
                raise requests.exceptions.ChunkedEncodingError(
                    f"Download ended at {downloaded} of {total_bytes} bytes"
                )
            if md5 and extension:
                validation = validate_source_file(
                    destination,
                    md5,
                    extension,
                    expected_size=total_bytes,
                )
                return downloaded, total_bytes, validation
            return downloaded, total_bytes
        except (requests.RequestException, OSError, SourceFileError) as error:
            last_error = error
            if isinstance(error, SourceFileError):
                try:
                    with open(destination, "wb"):
                        pass
                except OSError:
                    pass
            try:
                downloaded = os.path.getsize(destination)
            except OSError:
                downloaded = 0
            if attempt >= DOWNLOAD_MAX_ATTEMPTS:
                break
            detail = f"Continuing from {_format_transfer_size(downloaded)}"
            if total_bytes:
                detail += f" of {_format_transfer_size(total_bytes)}"
            yield emit("Resuming book download", None, detail)
            time.sleep(min(attempt, 3))
            if md5:
                DOWNLOADER.invalidate_download(md5)
                refreshed_url = yield from _resolve_download_progress(
                    md5,
                    emit=emit,
                    abort_check=abort_check,
                )
                if refreshed_url:
                    url = refreshed_url
        finally:
            if response is not None:
                try:
                    response.close()
                except requests.RequestException:
                    pass

    raise last_error or RuntimeError("Book download could not be completed")


def _open_smtp_connection(host, port, timeout=45):
    import smtplib, ssl

    if int(port) == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        server.ehlo()
        if server.sock:
            import socket
            server.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return server
    server = smtplib.SMTP(host, port, timeout=timeout)
    try:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        if server.sock:
            import socket
            server.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return server
    except Exception:
        server.close()
        raise


def _close_smtp_connection(server):
    if not server:
        return
    try:
        server.quit()
    except Exception:
        server.close()


def _format_eta(seconds):
    seconds = max(0, int(round(seconds or 0)))
    if seconds < 60:
        return f"{seconds}s left"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder}s left" if remainder else f"{minutes}m left"


def _send_attachment_progress(
    server,
    *,
    sender,
    recipient,
    title,
    author,
    extension,
    attachment_path,
    filename,
    message_id,
    emit=None,
):
    emit = emit or _kindle_progress
    body_lines = [f"Book sent from LibFlix.\n\nTitle: {title}"]
    if author:
        body_lines.append(f"Author: {author}")
    body_lines.append(f"Format: {extension.upper()}")
    mime_subtype = {
        "epub": "epub+zip",
        "pdf": "pdf",
    }.get(extension, "octet-stream")
    for update in stream_smtp_attachment(
        server,
        sender=sender,
        recipient=recipient,
        subject=f"Sent by LibFlix: {title}",
        body="\n".join(body_lines),
        attachment_path=attachment_path,
        filename=filename,
        mime_subtype=mime_subtype,
        message_id=message_id,
    ):
        fraction = update.sent / max(update.total, 1)
        progress = 80 + min(19, int(fraction * 19))
        detail = (
            f"{_format_transfer_size(update.sent)} of {_format_transfer_size(update.total)}"
            f" · {_format_transfer_size(update.rate)}/s · {_format_eta(update.eta_seconds)}"
        )
        event = emit("Uploading to email", progress, detail)
        event.update({
            "uploaded_bytes": int(update.sent),
            "upload_total_bytes": int(update.total),
            "upload_rate_bytes_per_second": round(float(update.rate), 1),
            "upload_eta_seconds": round(float(update.eta_seconds), 1),
        })
        yield event


def _configured_kindle_relay():
    if not (KINDLE_RELAY_HOST and KINDLE_RELAY_USER and KINDLE_RELAY_PASSWORD):
        return None
    try:
        port = int(KINDLE_RELAY_PORT)
    except (TypeError, ValueError):
        # Preserve the invalid value so payload validation can report the
        # relay configuration error instead of asking for user SMTP details.
        port = KINDLE_RELAY_PORT
    return {
        "host": KINDLE_RELAY_HOST,
        "port": port,
        "user": KINDLE_RELAY_USER,
        "password": KINDLE_RELAY_PASSWORD,
        "sender": KINDLE_RELAY_SENDER or KINDLE_RELAY_USER,
        "managed": True,
    }


def _kindle_smtp_configuration(data):
    relay = _configured_kindle_relay()
    if relay:
        return relay
    return {
        "host": str(data.get("smtp_host") or "").strip(),
        "port": data.get("smtp_port", 587),
        "user": str(data.get("smtp_user") or "").strip(),
        "password": data.get("smtp_pass") or "",
        "sender": str(data.get("sender_email") or data.get("smtp_user") or "").strip(),
        "managed": False,
    }


def _open_authenticated_smtp(configuration, cancel_event=None):
    started = time.perf_counter()
    server = None
    try:
        server = _open_smtp_connection(configuration["host"], configuration["port"])
        server.login(configuration["user"], configuration["password"])
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Delivery preparation was cancelled")
        return server, round(time.perf_counter() - started, 3)
    except Exception:
        _close_smtp_connection(server)
        raise


def _future_failure(future):
    if future and future.done():
        future.result()


def _kindle_source_cache():
    return KindleSourceCache(
        KINDLE_SOURCE_CACHE_DIR,
        ttl=KINDLE_SOURCE_CACHE_TTL,
        max_bytes=KINDLE_SOURCE_CACHE_MAX_BYTES,
    )


def _kindle_delivery_error(error):
    import smtplib

    if isinstance(error, smtplib.SMTPServerDisconnected):
        return "The email server disconnected during upload. Try the smaller edition or check the provider's attachment limit."
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "The email upload timed out. Try the smaller edition and send again."
    if isinstance(error, smtplib.SMTPDataError):
        return "The email provider rejected the attachment. Try a smaller edition or check its attachment limit."
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "The email provider rejected the Kindle address. Check the address and approved sender settings."
    return str(error)


def _send_to_kindle_events(data):
    import smtplib

    md5 = str(data.get("md5") or "").casefold()
    ext = re.sub(r"[^a-z0-9]", "", data.get("ext", "epub").lower()) or "epub"
    kindle_email = data.get("kindle_email", "").strip()
    smtp_configuration = _kindle_smtp_configuration(data)
    source_cache = _kindle_source_cache()
    tracker = KindleProgressTracker()
    tmp_path = None
    prepared_path = None
    source_path = ""
    server = None
    smtp_future = None
    cover_future = None
    parallel_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kindle-warm")
    parallel_cancel = threading.Event()
    parallel_durations = {}
    progress = 3
    message_id = f"<libflix-{md5}-{uuid.uuid4().hex}@fomalhaut.app>"

    def timed_cover_load():
        started = time.perf_counter()
        value = _kindle_cover_bytes(data.get("cover_url"))
        return value, round(time.perf_counter() - started, 3)

    try:
        smtp_future = parallel_executor.submit(
            _open_authenticated_smtp,
            smtp_configuration,
            parallel_cancel,
        )
        if ext == "epub" and data.get("cover_url"):
            cover_future = parallel_executor.submit(timed_cover_load)

        yield tracker.event("Preparing delivery", progress, "Starting secure delivery tasks")
        source_path = source_cache.get(md5, ext)
        source_cache_hit = bool(source_path)
        if source_cache_hit:
            progress = 65
            yield tracker.event(
                "Book file ready",
                progress,
                "Using a recent verified copy",
            )
        else:
            yield tracker.event("Finding book file", None, "Checking the download source")
            dl_url = yield from _resolve_download_progress(
                md5,
                emit=tracker.event,
                abort_check=lambda: _future_failure(smtp_future),
            )
            if not dl_url:
                raise RuntimeError("The download source did not return a file link.")

            progress = 10
            yield tracker.event("Connecting to book source", progress)
            yield tracker.event("Downloading book", progress, "Starting transfer")
            tmp_path = source_cache.temporary_path(md5, ext)
            downloaded, total_bytes, validation = yield from _download_book_progress(
                dl_url,
                tmp_path,
                md5=md5,
                extension=ext,
                emit=tracker.event,
                abort_check=lambda: _future_failure(smtp_future),
            )
            source_path = source_cache.commit(tmp_path, md5, ext, validation)
            if source_path != tmp_path:
                tmp_path = None

        progress = 68
        yield tracker.event("Polishing book details", progress, "Checking title, metadata, and cover")
        identifier = ""
        ol_key = str(data.get("ol_key") or "").strip()
        if re.fullmatch(r"/works/OL\d+W", ol_key):
            identifier = f"https://openlibrary.org{ol_key}"
        cover_bytes = b""
        if cover_future:
            try:
                cover_bytes, parallel_durations["cover_fetch"] = cover_future.result()
            except Exception:
                cover_bytes = b""
        prepared = prepare_book_for_kindle(
            source_path,
            ext,
            {
                "canonical_title": data.get("canonical_title") or data.get("title"),
                "title": data.get("title"),
                "author": data.get("author"),
                "language": data.get("language"),
                "publisher": data.get("publisher"),
                "year": data.get("year"),
                "description": data.get("description"),
                "identifier": identifier,
            },
            cover_loader=(lambda: cover_bytes) if cover_future else None,
        )
        if prepared.temporary:
            prepared_path = prepared.path
        if prepared.warning:
            app.logger.info("Kindle preparation kept original %s: %s", ext, prepared.warning)
        title = prepared.title
        attachment_path = prepared.path
        attachment_size = os.path.getsize(attachment_path)
        updates = [field for field in prepared.updated_fields if field != "cover"]
        detail_parts = []
        if updates:
            detail_parts.append("Updated " + ", ".join(updates))
        if prepared.cover_added:
            detail_parts.append("Added cover")
        if not detail_parts:
            detail_parts.append("Clean filename ready")
        progress = 74
        yield tracker.event("Building Kindle delivery", progress, "; ".join(detail_parts))

        progress = 80
        yield tracker.event("Connecting to email", progress, "Finalising the secure connection")
        server, parallel_durations["smtp_connect_and_login"] = smtp_future.result()
        smtp_future = None
        try:
            code, _ = server.noop()
            if code != 250:
                raise smtplib.SMTPServerDisconnected("Warm email connection expired")
            yield tracker.event(
                "Sending to Kindle",
                progress,
                f"Uploading {_format_transfer_size(attachment_size)} attachment",
            )
            try:
                yield from _send_attachment_progress(
                    server,
                    sender=smtp_configuration["sender"],
                    recipient=kindle_email,
                    title=title,
                    author=prepared.author,
                    extension=ext,
                    attachment_path=attachment_path,
                    filename=prepared.filename,
                    message_id=message_id,
                    emit=tracker.event,
                )
            except (smtplib.SMTPServerDisconnected, TimeoutError, ConnectionError, OSError):
                _close_smtp_connection(server)
                server = None
                yield tracker.event("Reconnecting to email", None, "The first upload connection was interrupted")
                server, reconnect_duration = _open_authenticated_smtp(smtp_configuration)
                parallel_durations["smtp_reconnect"] = reconnect_duration
                yield from _send_attachment_progress(
                    server,
                    sender=smtp_configuration["sender"],
                    recipient=kindle_email,
                    title=title,
                    author=prepared.author,
                    extension=ext,
                    attachment_path=attachment_path,
                    filename=prepared.filename,
                    message_id=message_id,
                    emit=tracker.event,
                )
        finally:
            _close_smtp_connection(server)
            server = None

        progress = 100
        complete = tracker.event("Sent to Kindle", progress)
        complete.update({
            "type": "complete",
            "success": True,
            "title": title,
            "attachment_bytes": attachment_size,
            "source_cache_hit": source_cache_hit,
            "stage_durations": tracker.durations(),
            "parallel_durations": parallel_durations,
        })
        yield complete
    except smtplib.SMTPAuthenticationError:
        failed = tracker.event("Sign-in failed", progress)
        failed.update({
            "type": "error",
            "success": False,
            "error": "SMTP auth failed. For Gmail, use an App Password.",
            "stage_durations": tracker.durations(),
        })
        yield failed
    except Exception as e:
        app.logger.warning("Kindle delivery failed at %s%% (%s): %s", progress, type(e).__name__, e)
        failed = tracker.event("Delivery failed", progress)
        failed.update({
            "type": "error",
            "success": False,
            "error": _kindle_delivery_error(e),
            "stage_durations": tracker.durations(),
        })
        yield failed
    finally:
        parallel_cancel.set()
        _close_smtp_connection(server)
        if smtp_future and smtp_future.done():
            try:
                warmed_server, _ = smtp_future.result()
            except Exception:
                warmed_server = None
            _close_smtp_connection(warmed_server)
        parallel_executor.shutdown(wait=False, cancel_futures=True)
        for path in {tmp_path, prepared_path}:
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass

def _valid_delivery_email(value):
    value = str(value or "").strip()
    if len(value) > 320 or re.search(r"[\s\x00-\x1f\x7f]", value):
        return False
    return bool(re.fullmatch(r"[^@]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", value))


def validate_kindle_payload(data):
    relay = _configured_kindle_relay()
    required = ("md5", "kindle_email")
    if not relay:
        required += ("smtp_host", "smtp_user", "smtp_pass")
    if not all(data.get(field) for field in required):
        return "Missing required fields"
    if not re.fullmatch(r"[a-fA-F0-9]{32}", str(data.get("md5", ""))):
        return "Invalid book identifier"
    extension = re.sub(r"[^a-z0-9]", "", str(data.get("ext", "epub")).casefold()) or "epub"
    if not is_kindle_delivery_format(extension):
        return "This file type is not supported by Send to Kindle; use EPUB or PDF"
    smtp_configuration = _kindle_smtp_configuration(data)
    try:
        port = int(smtp_configuration["port"])
    except (TypeError, ValueError):
        return "SMTP port must be a number"
    if port not in (465, 587, 2525):
        return "Use a secure SMTP port: 465, 587, or 2525"
    host = str(smtp_configuration["host"]).strip().rstrip(".")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        return "Invalid SMTP hostname"
    if not _valid_delivery_email(data.get("kindle_email")):
        return "Invalid Kindle email address"
    if relay and not str(data.get("kindle_email", "")).strip().casefold().endswith("@kindle.com"):
        return "Managed delivery requires an @kindle.com address"
    if not _valid_delivery_email(smtp_configuration.get("sender")):
        return "Invalid sender email address"
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror:
        return "The SMTP hostname could not be resolved"
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return "Private or local SMTP servers are not supported"
    metadata_limits = {
        "title": 220,
        "canonical_title": 220,
        "author": 240,
        "publisher": 240,
        "year": 32,
        "language": 24,
        "description": 4000,
        "cover_url": 300,
        "ol_key": 32,
    }
    for field, limit in metadata_limits.items():
        value = unicodedata.normalize("NFKC", str(data.get(field) or ""))
        value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
        data[field] = re.sub(r"\s+", " ", value).strip()[:limit]
    if data["ol_key"] and not re.fullmatch(r"/works/OL\d+W", data["ol_key"]):
        data["ol_key"] = ""
    if not smtp_configuration.get("managed"):
        data["smtp_port"] = port
        data["smtp_host"] = host
        data["sender_email"] = smtp_configuration["sender"]
    else:
        for field in ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "sender_email"):
            data.pop(field, None)
    data["ext"] = extension
    return ""

def kindle_job_create():
    initialize_disk_cache()
    job_id = uuid.uuid4().hex
    now = time.time()
    queued = [_kindle_progress("Queued for delivery", 1)]
    with disk_cache_connection(timeout=5) as connection:
        connection.execute(
            "INSERT INTO kindle_jobs(job_id, created_at, updated_at, status, events) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, now, now, "queued", json.dumps(queued, separators=(",", ":"))),
        )
    return job_id

def kindle_job_append(job_id, event):
    initialize_disk_cache()
    with disk_cache_connection(timeout=5) as connection:
        row = connection.execute(
            "SELECT events FROM kindle_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            return
        events = json.loads(row[0])
        events.append(event)
        status = "complete" if event.get("type") == "complete" else "failed" if event.get("type") == "error" else "running"
        connection.execute(
            "UPDATE kindle_jobs SET updated_at = ?, status = ?, events = ? WHERE job_id = ?",
            (time.time(), status, json.dumps(events, separators=(",", ":")), job_id),
        )

def kindle_job_get(job_id, cursor=0):
    initialize_disk_cache()
    with disk_cache_connection(timeout=5) as connection:
        row = connection.execute(
            "SELECT created_at, updated_at, status, events FROM kindle_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if not row:
        return None
    events = json.loads(row[3])
    status = row[2]
    if status in ("queued", "running") and time.time() - row[1] > 300:
        timeout_event = {
            "type": "error",
            "success": False,
            "stage": "Delivery interrupted",
            "progress": None,
            "timestamp": round(time.time(), 3),
            "error": "The delivery worker stopped responding. Start the delivery again.",
        }
        kindle_job_append(job_id, timeout_event)
        events.append(timeout_event)
        status = "failed"
    cursor = max(0, min(int(cursor or 0), len(events)))
    return {
        "job_id": job_id,
        "status": status,
        "events": events[cursor:],
        "cursor": len(events),
    }

def _acquire_global_kindle_slot():
    os.makedirs(os.path.dirname(KINDLE_DELIVERY_LOCK_FILE), mode=0o700, exist_ok=True)
    handle = open(KINDLE_DELIVERY_LOCK_FILE, "a+", encoding="utf-8")
    try:
        os.chmod(KINDLE_DELIVERY_LOCK_FILE, 0o600)
        queued_at = time.perf_counter()
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError:
                waited = max(1, int(time.perf_counter() - queued_at))
                yield _kindle_progress(
                    "Queued for delivery",
                    1,
                    f"Waiting for the active delivery · {waited}s",
                )
                time.sleep(4)
    except Exception:
        handle.close()
        raise

def _release_global_kindle_slot(handle):
    if not handle:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()

def _locked_kindle_events(data):
    handle = None
    try:
        handle = yield from _acquire_global_kindle_slot()
        yield from _send_to_kindle_events(data)
    finally:
        _release_global_kindle_slot(handle)

def run_kindle_job(job_id, data):
    try:
        for event in _locked_kindle_events(data):
            kindle_job_append(job_id, event)
    except Exception as error:
        kindle_job_append(job_id, {
            "type": "error",
            "success": False,
            "stage": "Delivery failed",
            "progress": None,
            "error": _kindle_delivery_error(error),
        })

@app.route("/api/kindle/jobs", methods=["POST"])
def api_create_kindle_job():
    data = request.get_json(silent=True) or {}
    error = validate_kindle_payload(data)
    if error:
        return jsonify({"success": False, "error": error}), 400
    job_id = kindle_job_create()
    KINDLE_EXECUTOR.submit(run_kindle_job, job_id, dict(data))
    return jsonify({
        "success": True,
        "job_id": job_id,
        "status_url": f"/api/kindle/jobs/{job_id}",
    }), 202

@app.route("/api/kindle/jobs/<job_id>")
def api_kindle_job(job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return jsonify({"success": False, "error": "Invalid delivery job"}), 400
    cursor = request.args.get("cursor", "0")
    cursor = int(cursor) if cursor.isdigit() else 0
    job = kindle_job_get(job_id, cursor)
    if not job:
        return jsonify({"success": False, "error": "Delivery job not found"}), 404
    return jsonify({"success": True, **job})


@app.route("/api/sendtokindle", methods=["POST"])
def api_sendtokindle():
    data = request.get_json(silent=True) or {}
    error = validate_kindle_payload(data)
    if error:
        return jsonify({"success": False, "error": error}), 400
    if not KINDLE_LEGACY_SEMAPHORE.acquire(blocking=False):
        response = jsonify({
            "success": False,
            "error": "Kindle delivery is busy. Please try again shortly.",
            "retry_after": 15,
        })
        response.status_code = 429
        response.headers["Retry-After"] = "15"
        return response

    if request.args.get("stream") == "1":
        def stream_events():
            for event in _locked_kindle_events(data):
                yield json.dumps(event, ensure_ascii=False) + "\n"

        response = Response(stream_with_context(stream_events()), mimetype="application/x-ndjson")
        response.call_on_close(KINDLE_LEGACY_SEMAPHORE.release)
        response.headers["Cache-Control"] = "no-cache, no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    try:
        events = list(_locked_kindle_events(data))
    finally:
        KINDLE_LEGACY_SEMAPHORE.release()
    final = events[-1] if events else {"success": False, "error": "Delivery did not complete"}
    status = 200 if final.get("success") else 502
    return jsonify(final), status

def load_cached_shelves():
    warm_jobs = []
    for lang in BOOK_LANGS:
        for mode in ("nonfiction", "fiction"):
            disk = disk_load_shelves(mode, lang)
            if not disk:
                continue
            shelves = normalize_shelf_labels(disk, mode)
            for shelf in shelves:
                for book in shelf.get("books", []):
                    remember_book_hint(book, lang)
            warm_jobs.extend(cover_warm_jobs_for_shelves(shelves))
            cache_set(f"shelves_{lang}_{mode}", shelves)
            print(f"Loaded {len(shelves)} Open Library {lang} {mode} shelves from disk cache", flush=True)
    schedule_cover_warm(warm_jobs)

def cover_warm_jobs_for_shelves(shelves):
    trending = shelves[0].get("books", []) if shelves else []
    cover_ids = []
    for book in trending:
        match = re.fullmatch(
            r"/olcover/(\d+)(?:/[SML](?:\.webp)?)?",
            str(book.get("cover_url", "")),
        )
        if match and match.group(1) not in cover_ids:
            cover_ids.append(match.group(1))
    # Any of the first 16 books may be selected as the large hero. Every
    # Trending cover can enter the first hydrated shelf at medium size.
    return (
        [(cover_id, "L") for cover_id in cover_ids[:16]]
        + [(cover_id, "M") for cover_id in cover_ids]
    )

def cover_warm_marker_is_fresh(marker):
    try:
        return time.time() - os.path.getmtime(marker) < 86400
    except OSError:
        return False

def write_cover_warm_marker(marker):
    temporary = f"{marker}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "w") as marker_file:
            marker_file.write(str(time.time()))
        os.replace(temporary, marker)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass

def _run_cover_warm_batch(jobs, marker, force=False):
    lock_path = os.path.join(COVER_CACHE_DIR, ".warm.lock")
    with filesystem_lock(lock_path):
        if not force and cover_warm_marker_is_fresh(marker):
            return False

        def warm(cover_id, size):
            url = f"https://covers.openlibrary.org/b/id/{cover_id}-{size}.jpg"
            return ensure_cover_cached("openlibrary", cover_id, size, url)

        futures = [COVER_WARM_EXECUTOR.submit(warm, cover_id, size) for cover_id, size in jobs]
        completed = True
        for future in as_completed(futures):
            try:
                cache_path, _hit = future.result()
                if not cache_path:
                    completed = False
            except Exception:
                completed = False
        if not force and completed:
            write_cover_warm_marker(marker)
        return completed

def schedule_cover_warm(jobs, force=False):
    jobs = list(dict.fromkeys(jobs))
    if not jobs:
        return None
    os.makedirs(COVER_CACHE_DIR, exist_ok=True)
    marker = os.path.join(COVER_CACHE_DIR, ".warm-complete")
    if not force and cover_warm_marker_is_fresh(marker):
        return None
    return COVER_WARM_COORDINATOR.submit(_run_cover_warm_batch, jobs, marker, force)

initialize_disk_cache()
load_cached_shelves()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5800, debug=True, use_reloader=False)
