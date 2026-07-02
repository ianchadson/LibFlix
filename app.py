import re, os, warnings, time, random, threading
from urllib.parse import urljoin, quote
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, Response, stream_with_context, jsonify

warnings.filterwarnings("ignore", category=requests.packages.urllib3.exceptions.InsecureRequestWarning)

MIRROR = "https://libgen.li"
OL = "https://openlibrary.org"
CACHE = {}
CACHE_TTL_OL = 600
CACHE_TTL_LG = 300

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
    try:
        r = requests.get(f"{OL}{path}", params=params, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        cache_set(key, data)
        return data
    except:
        return None

def ol_get_work(ol_key):
    return ol_get(ol_key + ".json")

SHELVES_DEF = [
    ("Trending Now", "trending"),
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

def is_english_title(title):
    return bool(re.match(r'^[\x20-\x7E\s\-\'.,!?;:()"&]+$', title))

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

def fetch_one_shelf(name, topic):
    try:
        if topic == "trending":
            params = {"q": "subject:Nonfiction -subject:Fiction", "sort": "rating", "limit": 30}
        else:
            params = {"q": f"subject:{topic} -subject:Fiction", "sort": "rating", "limit": 30}
        data = ol_get("/search.json", params)
        works = (data or {}).get("docs", [])[:30]
        books = []
        for w in works:
            b = extract_book(w)
            if b is not None:
                books.append(b)
                if len(books) >= 20:
                    break
        return {"name": name, "books": books}
    except:
        return {"name": name, "books": []}

def fetch_shelves():
    shelves = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_one_shelf, name, topic): name for name, topic in SHELVES_DEF}
        for f in as_completed(futures):
            shelves.append(f.result())
    shelves.sort(key=lambda s: [n for n, _ in SHELVES_DEF].index(s["name"]))
    return shelves

@dataclass
class LibgenBook:
    id: str = ""
    title: str = ""
    author: str = ""
    publisher: str = ""
    year: str = ""
    language: str = ""
    pages: str = ""
    size: str = ""
    ext: str = ""
    md5: str = ""
    cover_dir: str = ""

def fetch_search(query, sort="y", order="DESC", page=1, res=25):
    key = f"lg:{query}:{sort}:{order}:{page}:{res}"
    cached = cache_get(key, CACHE_TTL_LG)
    if cached:
        return cached
    params = {
        "req": query,
        "columns[]": ["t", "a", "s", "y", "p", "i", "l", "la", "qi"],
        "sort": sort, "sortmode": order,
        "page": page, "res": res,
        "gmode": 1,
        "topics[]": ["l", "f"], "curtab": "f",
    }
    r = requests.get(f"{MIRROR}/index.php", params=params, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    html = r.text
    cache_set(key, html)
    return html

def parse_results(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", id="tablelibgen")
    if not table:
        return []
    books = []
    rows = table.find_all("tr")
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 9:
            continue
        tds = list(row.find_all("td"))
        md5 = ""
        mlink = tds[-1].find("a")
        if mlink and mlink.get("href"):
            m = re.search(r'md5=([a-f0-9]{32})', mlink["href"])
            if m:
                md5 = m.group(1)
        link_a = tds[0].find("a")
        title = link_a.get_text(strip=True) if link_a else tds[0].get_text(strip=True)
        title = re.sub(r'\s+', ' ', title).strip()
        year = tds[3].get_text(" ", strip=True)
        year = re.sub(r'[;|].*$', '', year).strip()
        year = re.sub(r'\s+P\s+\d+.*$', '', year).strip()
        size_link = tds[6].find("a")
        size = size_link.get_text(strip=True) if size_link else tds[6].get_text(" ", strip=True)
        first_html = str(tds[0])
        l_match = re.search(r'l (\d+)', first_html)
        cover_dir = ""
        if l_match:
            l_num = int(l_match.group(1))
            cover_dir = str(l_num // 1000 * 1000)
        books.append(LibgenBook(
            title=title, author=tds[1].get_text(" ", strip=True),
            publisher=tds[2].get_text(" ", strip=True), year=year,
            language=tds[4].get_text(" ", strip=True),
            pages=tds[5].get_text(" ", strip=True), size=size,
            ext=tds[7].get_text(" ", strip=True), md5=md5,
            cover_dir=cover_dir,
        ))
    return books

def resolve_download(md5):
    try:
        r = requests.get(f"{MIRROR}/ads.php", params={"md5": md5}, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a"):
            href = a.get("href", "")
            if "get.php" in href or "download" in href.lower() or "/dl/" in href:
                return urljoin(r.url, href)
        for a in soup.find_all("a"):
            href = a.get("href", "")
            if href.startswith("http") and ("libgen" in href or "booksdl" in href):
                return urljoin(r.url, href)
        txt = r.text
        m = re.search(r'https?://[^"\']+\.(?:epub|pdf|mobi|djvu|zip|rar)[^"\']*', txt, re.I)
        if m:
            return m.group(0)
        m = re.search(r'(?:href|HREF)=["\']([^"\']+)["\']', txt)
        if m:
            return urljoin(r.url, m.group(1))
    except Exception:
        return f"{MIRROR}/ads.php?md5={md5}"
    return f"{MIRROR}/ads.php?md5={md5}"

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
    fmt_scores = {"epub": 30, "pdf": 20, "mobi": 15, "azw3": 15, "djvu": 10, "chm": 5, "txt": 3}
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

def total_search_results(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    paginator = soup.find("div", class_="paginator")
    if paginator:
        m = re.search(r'Paginator\("(\w+)",\s*(\d+)', str(paginator))
        if m:
            return int(m.group(2))
    return 0

def extract_desc(work):
    desc = work.get("description", "")
    if isinstance(desc, dict):
        desc = desc.get("value", "")
    return desc

app = Flask(__name__)

SORT_OPTIONS = {
    "y": "Year", "id": "ID", "title": "Title",
    "author": "Author", "filesize": "Size", "extension": "Extension",
    "time_added": "Date Added"
}

def get_shelves():
    cached = cache_get("shelves", CACHE_TTL_OL)
    if cached:
        return cached
    shelves = fetch_shelves()
    cache_set("shelves", shelves)
    return shelves

@app.route("/")
def index():
    shelves = get_shelves()
    hero = None
    if shelves:
        trending = shelves[0].get("books", [])
        if trending:
            hero = random.choice(trending)
    return render_template("index.html", shelves=shelves, hero=hero)

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        shelves = get_shelves()
        return render_template("index.html", shelves=shelves, error="Enter a search query.")
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
        sort_options=SORT_OPTIONS)

@app.route("/preview")
def preview():
    title = request.args.get("title", "").strip()
    author = request.args.get("author", "").strip()
    ol_key = request.args.get("ol_key", "").strip()
    cover_url = request.args.get("cover", "").strip()
    return render_template("book.html",
        title=title, author=author, cover_url=cover_url,
        ol_key=ol_key)

@app.route("/api/similar")
def api_similar():
    subject = request.args.get("subject", "").strip()
    ol_key = request.args.get("ol_key", "").strip()
    if not subject:
        return jsonify({"success": False, "error": "No subject"})
    data = ol_get("/search.json", {"subject": subject, "sort": "rating", "limit": 12})
    docs = (data or {}).get("docs", [])
    books = []
    for w in docs:
        b = extract_book(w)
        if b["ol_key"] != ol_key:
            books.append(b)
    return jsonify({"success": True, "books": books})

@app.route("/api/book")
def api_book():
    ol_key = request.args.get("ol_key", "").strip()
    if not ol_key:
        return jsonify({"success": False, "error": "No ol_key provided"})
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
        html = fetch_search(q, sort=sort_field, order=order, page=page, res=limit)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    books = parse_results(html)
    total = total_search_results(html)

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
        cover_url = f"/cover/{b.md5}?dir={b.cover_dir}" if b.cover_dir and b.md5 else ""
        result_books.append({
            "idx": i + 1 + (page - 1) * limit,
            "title": b.title,
            "author": b.author,
            "publisher": b.publisher,
            "year": b.year,
            "language": b.language,
            "pages": b.pages,
            "size": b.size,
            "ext": b.ext,
            "md5": b.md5,
            "cover_url": cover_url,
        })
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
    url = resolve_download(md5)
    filename = request.args.get("filename", f"{md5}.epub")
    def generate():
        try:
            r = requests.get(url, stream=True, timeout=120,
                             headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
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
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0", "Referer": f"{MIRROR}/"})
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
        r = requests.get(url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 100:
            resp = Response(r.content, mimetype=r.headers.get("content-type", "image/jpeg"))
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
    except:
        pass
    return "", 404

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

    dl_url = resolve_download(md5)
    try:
        r = requests.get(dl_url, stream=True, timeout=120,
                         headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
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
    print("Warming cache: fetching shelves...")
    t0 = time.time()
    shelves = fetch_shelves()
    cache_set("shelves", shelves)
    print(f"Cache warmed: {len(shelves)} shelves in {time.time()-t0:.1f}s")

if __name__ == "__main__":
    threading.Thread(target=warm_cache, daemon=True).start()
    app.run(host="0.0.0.0", port=5800, debug=True, use_reloader=False)
