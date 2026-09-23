"""Real-Debrid download source.

Real-Debrid exposes no torrent *search* endpoint and has disabled the
``/torrents/instantAvailability`` endpoint, so this provider:

1. searches a configurable public torrent index (default ``apibay.org``, The
   Pirate Bay's JSON API) for ebook torrents;
2. resolves a chosen torrent by adding its magnet to the user's Real-Debrid
   account, selecting the ebook file(s), and waiting a bounded time for the
   cached download to become available;
3. returns the Real-Debrid direct HTTPS link so LibFlix can stream it through
   the normal ``/download/<id>`` proxy.

The provider is strictly fail-closed: it never renders a source page (so no
pop-ups or ad scripts can run), refuses off-source hosts, and deletes a torrent
it could not resolve so the account is not left cluttered.  The API token is
read from the environment or a file-backed secret outside releases and is never
written by LibFlix.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from typing import Iterator, List, Optional, Tuple

import requests

from downloaders.base import (
    Book,
    Downloader,
    SESSION,
    SourceFetchError,
    cache_delete,
    cache_get,
    cache_set,
    decode_source_id,
    encode_source_id,
    fetch_bounded,
    host_is_allowed,
    human_size,
)

RD_API = os.environ.get("LIBFLIX_RD_API", "https://api.real-debrid.com/rest/1.0")
TORRENT_INDEX = os.environ.get("LIBFLIX_TORRENT_INDEX", "https://apibay.org")
RD_ALLOWED_HOSTS = ("apibay.org",)
RD_DOWNLOAD_HOSTS = ("real-debrid.com",)

# Real-Debrid direct links stay valid for a few hours; refresh well before that.
RD_LINK_TTL = 3_600
RD_SEARCH_TTL = 900
RD_RESOLVE_WAIT = float(os.environ.get("LIBFLIX_RD_WAIT", "25"))
RD_POLL_INTERVAL = 1.5
RD_PAGE_SIZE = 25

EBOOK_EXTENSIONS = ("epub", "pdf", "mobi", "azw3", "azw", "fb2", "djvu")

# Torrent categories that are never books.
_NON_BOOK_CATEGORY_HINTS = re.compile(
    r"\b(1080p|720p|2160p|x264|x265|h264|hevc|bluray|brrip|web-?dl|webrip|"
    r"camrip|hdtv|s\d{2}e\d{2}|season|complete series|discography|flac|mp3)\b",
    re.IGNORECASE,
)


RD_KEY_FILE = os.environ.get(
    "LIBFLIX_REALDEBRID_KEY_FILE",
    "/opt/libflix/shared/realdebrid-api-key",
)


def _api_key() -> str:
    key = (
        os.environ.get("LIBFLIX_REALDEBRID_KEY")
        or os.environ.get("LIBFLIX_RD_KEY")
        or ""
    ).strip()
    if key:
        return key
    # File-backed like the relay secret: provisioned once outside releases.
    try:
        with open(RD_KEY_FILE, "r", encoding="utf-8") as secret_file:
            return secret_file.read().strip()
    except OSError:
        return ""


def is_enabled() -> bool:
    return bool(_api_key())


def _api_headers() -> dict:
    return {"Authorization": f"Bearer {_api_key()}"}


def _infer_extension(name: str) -> str:
    lowered = str(name or "").casefold()
    for extension in EBOOK_EXTENSIONS:
        if re.search(rf"\.{extension}\b", lowered):
            return extension
    for extension in ("epub", "pdf"):
        if extension in lowered:
            return extension
    return "epub"


def _clean_title(name: str) -> str:
    value = re.sub(r"\.(epub|pdf|mobi|azw3?|fb2|djvu)\b", " ", str(name or ""), flags=re.I)
    value = re.sub(r"[_\.]+", " ", value)
    value = re.sub(r"\s*[\[\(](epub|pdf|mobi|retail|ebook)[\]\)]\s*", " ", value, flags=re.I)
    value = re.sub(r"\s*[-–]\s*[A-Za-z0-9._ ]{0,4}$", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -–|")
    return value[:180]


def _infer_author(name: str) -> str:
    value = _clean_title(name)
    match = re.search(r"\bby\s+([^,|(]{2,60})$", value, re.I)
    if match:
        return match.group(1).strip()[:120]
    if " - " in value:
        return value.rsplit(" - ", 1)[1].strip()[:120]
    return ""


def _guess_language(name: str) -> str:
    """Map an obvious script to the labels LibGen uses (mostly ASCII ebooks)."""
    for char in str(name or ""):
        codepoint = ord(char)
        if 0x0400 <= codepoint <= 0x04FF:
            return "Russian"
        if 0x4E00 <= codepoint <= 0x9FFF:
            return "Chinese"
        if 0x0600 <= codepoint <= 0x06FF:
            return "Arabic"
        if 0x0E00 <= codepoint <= 0x0E7F:
            return "Thai"
    return "English"


class RealDebridDownloader(Downloader):
    name = "realdebrid"

    # ---------------------------------------------------------------- search
    def search(
        self,
        query: str,
        *,
        sort: str = "y",
        order: str = "DESC",
        page: int = 1,
        limit: int = 25,
    ) -> Tuple[List[Book], int]:
        query = str(query or "").strip()
        if not query or not is_enabled():
            return [], 0
        cache_key = f"rd-search:{query.casefold()}:{page}:{limit}"
        cached = cache_get(cache_key, RD_SEARCH_TTL)
        if cached is not None:
            return cached, len(cached)

        rows = self._index_search(query, category="601")
        if not rows:
            rows = [
                row for row in self._index_search(query, category="")
                if self._looks_like_book(row)
            ]
        books = [book for book in (self._row_to_book(row) for row in rows) if book]
        if limit:
            books = books[: max(1, int(limit))]
        cache_set(cache_key, books)
        return books, len(books)

    def _index_search(self, query: str, *, category: str) -> List[dict]:
        params = {"q": query}
        if category:
            params["cat"] = category
        try:
            payload = fetch_bounded(
                f"{TORRENT_INDEX.rstrip('/')}/q.php",
                allowed_hosts=RD_ALLOWED_HOSTS,
                params=params,
                timeout=(4, 12),
                max_bytes=1_500_000,
            )
        except SourceFetchError:
            return []
        try:
            rows = json.loads(payload)
        except (TypeError, ValueError):
            return []
        if not isinstance(rows, list):
            return []
        valid = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            info_hash = str(row.get("info_hash") or "").casefold()
            if not re.fullmatch(r"[a-f0-9]{40}", info_hash):
                continue
            if info_hash == "0" * 40:
                continue
            valid.append(row)
        return valid

    @staticmethod
    def _looks_like_book(row: dict) -> bool:
        name = str(row.get("name") or "")
        if str(row.get("category") or "") == "601":
            return True
        if re.search(r"\.(epub|pdf|mobi|azw3?|fb2|djvu)\b", name, re.I):
            return True
        return False

    def _row_to_book(self, row: dict) -> Optional[Book]:
        info_hash = str(row.get("info_hash") or "").casefold()
        name = str(row.get("name") or "")
        if not re.fullmatch(r"[a-f0-9]{40}", info_hash):
            return None
        if _NON_BOOK_CATEGORY_HINTS.search(name) and not re.search(
            r"\.(epub|pdf)\b", name, re.I
        ):
            return None
        book_id = encode_source_id("realdebrid", info_hash)
        if not book_id:
            return None
        try:
            size_bytes = int(row.get("size") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        extension = _infer_extension(name)
        cache_set(f"rd-ext:{info_hash}", extension)
        return Book(
            book_id=book_id,
            title=_clean_title(name) or name[:180],
            author=_infer_author(name),
            language=_guess_language(name),
            size=human_size(size_bytes),
            ext=extension,
            source="realdebrid",
        )

    # ------------------------------------------------------- resolve + fetch
    def resolve_download(self, book_id: str) -> str:
        source, native_id = decode_source_id(book_id)
        if source != "realdebrid" or not re.fullmatch(r"[a-f0-9]{40}", native_id):
            return ""
        if not is_enabled():
            return ""

        cache_key = f"rd-direct:{native_id}"
        cached = cache_get(cache_key, RD_LINK_TTL)
        if cached and cached.get("url"):
            return cached["url"]

        torrent_id = ""
        try:
            expected_ext = cache_get(f"rd-ext:{native_id}", RD_LINK_TTL) or ""
            torrent_id = self._find_existing_torrent(native_id) or self._add_magnet(native_id)
            if not torrent_id:
                return ""
            files = self._select_files(torrent_id, expected_ext)
            info = self._wait_for_download(torrent_id)
            if not info or info.get("status") != "downloaded":
                self._delete_torrent(torrent_id)
                return ""
            link = self._pick_link(torrent_id, info, files, expected_ext)
            if not link:
                self._delete_torrent(torrent_id)
                return ""
            direct = self._unrestrict(link)
            if not direct:
                self._delete_torrent(torrent_id)
                return ""
            cache_set(cache_key, {
                "url": direct["url"],
                "torrent_id": torrent_id,
                "filename": direct.get("filename", ""),
                "filesize": direct.get("filesize", 0),
                "ext": expected_ext,
            })
            return direct["url"]
        except Exception:
            if torrent_id:
                self._delete_torrent(torrent_id)
            return ""

    def invalidate_download(self, book_id: str) -> None:
        source, native_id = decode_source_id(book_id)
        if source != "realdebrid":
            return
        cache_key = f"rd-direct:{native_id}"
        cached = cache_get(cache_key, RD_LINK_TTL)
        cache_delete(cache_key)
        if cached and cached.get("torrent_id"):
            self._delete_torrent(cached["torrent_id"])

    def resolved_filename(self, book_id: str) -> str:
        source, native_id = decode_source_id(book_id)
        if source != "realdebrid":
            return ""
        cached = cache_get(f"rd-direct:{native_id}", RD_LINK_TTL)
        if cached and cached.get("filename"):
            return str(cached["filename"])
        return ""

    def cover_url(self, book: Book) -> Optional[str]:
        return None

    # ------------------------------------------------------------- RD helpers
    def _rd_request(self, method: str, path: str, **kwargs):
        url = f"{RD_API.rstrip('/')}/{path.lstrip('/')}"
        response = SESSION.request(
            method,
            url,
            headers=_api_headers(),
            timeout=(5, 20),
            allow_redirects=False,
            **kwargs,
        )
        return response

    def _find_existing_torrent(self, info_hash: str) -> str:
        try:
            response = self._rd_request("GET", "torrents")
            if response.status_code != 200:
                return ""
            for torrent in response.json():
                if str(torrent.get("hash") or "").casefold() == info_hash:
                    return str(torrent.get("id") or "")
        except (ValueError, requests.RequestException):
            return ""
        return ""

    def _add_magnet(self, info_hash: str) -> str:
        magnet = f"magnet:?xt=urn:btih:{info_hash}"
        try:
            response = self._rd_request("POST", "torrents/addMagnet", data={"magnet": magnet})
            if response.status_code in (200, 201):
                return str(response.json().get("id") or "")
        except (ValueError, requests.RequestException):
            return ""
        return ""

    def _select_files(self, torrent_id: str, expected_ext: str = "") -> List[str]:
        """Select the expected ebook file, otherwise every ebook file present."""
        files: List[dict] = []
        for _ in range(10):
            info = self._torrent_info(torrent_id)
            if not info:
                return []
            status = info.get("status")
            if status == "waiting_files_selection":
                files = info.get("files") or []
                break
            if status in ("magnet_conversion", "queued"):
                time.sleep(RD_POLL_INTERVAL)
                continue
            break
        ebook_files = [
            entry for entry in files
            if re.search(r"\.(epub|pdf|mobi|azw3?|fb2|djvu)\b", str(entry.get("path") or ""), re.I)
        ]
        expected = []
        if expected_ext:
            pattern = re.compile(rf"\.{re.escape(expected_ext)}\b", re.I)
            expected = [entry for entry in ebook_files if pattern.search(str(entry.get("path") or ""))]
        chosen = expected or ebook_files
        selected_ids = [str(entry.get("id")) for entry in chosen]
        selection = ",".join(selected_ids) if selected_ids else "all"
        try:
            self._rd_request("POST", f"torrents/selectFiles/{torrent_id}", data={"files": selection})
        except requests.RequestException:
            return []
        return selected_ids

    def _torrent_info(self, torrent_id: str) -> dict:
        try:
            response = self._rd_request("GET", f"torrents/info/{torrent_id}")
            if response.status_code != 200:
                return {}
            return response.json()
        except (ValueError, requests.RequestException):
            return {}

    def _wait_for_download(self, torrent_id: str) -> dict:
        deadline = time.monotonic() + max(0.0, RD_RESOLVE_WAIT)
        info: dict = {}
        while True:
            info = self._torrent_info(torrent_id)
            status = info.get("status")
            if status in ("downloaded", "dead", "error", "virus", "magnet_error"):
                return info
            if time.monotonic() >= deadline:
                return info
            time.sleep(RD_POLL_INTERVAL)

    @staticmethod
    def _pick_link(torrent_id: str, info: dict, selected_ids: List[str], expected_ext: str = "") -> str:
        files = info.get("files") or []
        wanted_ids = {str(value) for value in selected_ids}
        candidates = [
            entry for entry in files
            if (not wanted_ids or str(entry.get("id")) in wanted_ids) and entry.get("link")
        ]
        if expected_ext:
            pattern = re.compile(rf"\.{re.escape(expected_ext)}\b", re.I)
            for entry in candidates:
                if pattern.search(str(entry.get("path") or "")):
                    return str(entry["link"])
        if candidates:
            return str(candidates[0]["link"])
        links = info.get("links") or []
        return str(links[0]) if links else ""

    def _unrestrict(self, link: str) -> dict:
        try:
            response = self._rd_request("POST", "unrestrict/link", data={"link": link})
            if response.status_code != 200:
                return {}
            payload = response.json()
        except (ValueError, requests.RequestException):
            return {}
        url = str(payload.get("download") or "")
        if not host_is_allowed(url, RD_DOWNLOAD_HOSTS):
            return {}
        return {
            "url": url,
            "filename": str(payload.get("filename") or ""),
            "filesize": payload.get("filesize") or 0,
        }

    def _delete_torrent(self, torrent_id: str) -> None:
        try:
            self._rd_request("DELETE", f"torrents/delete/{torrent_id}")
        except requests.RequestException:
            pass

    # ---------------------------------------------------------------- stream
    def stream_file(self, url: str) -> Iterator[bytes]:
        if not host_is_allowed(url, RD_DOWNLOAD_HOSTS):
            raise SourceFetchError("Off-source download refused")
        response = SESSION.get(url, stream=True, timeout=120, allow_redirects=True)
        response.raise_for_status()
        yield from response.iter_content(chunk_size=65536)
