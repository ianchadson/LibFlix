"""Shared, validated storage and streaming transport for Kindle deliveries."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import smtplib
import tempfile
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formatdate, make_msgid


SUPPORTED_EXTENSIONS = frozenset({"epub", "pdf"})


class SourceFileError(ValueError):
    """The downloaded source is not the requested book file."""


@dataclass(frozen=True)
class SourceValidation:
    size: int
    digest: str


def _normal_extension(value: str) -> str:
    extension = re.sub(r"[^a-z0-9]", "", str(value or "").casefold())
    if extension not in SUPPORTED_EXTENSIONS:
        raise SourceFileError("Unsupported Kindle source format")
    return extension


def _valid_magic(header: bytes, extension: str) -> bool:
    leading = header.lstrip()[:1024]
    lowered = leading.lower()
    if lowered.startswith((b"<!doctype html", b"<html")) or b"<html" in lowered[:256]:
        return False
    if extension == "epub":
        return header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    return b"%PDF-" in header[:1024]


def validate_source_file(
    path: str,
    expected_md5: str,
    extension: str,
    *,
    expected_size: int = 0,
) -> SourceValidation:
    """Validate type, byte count, and the LibGen content hash in one pass."""
    expected_md5 = str(expected_md5 or "").casefold()
    if not re.fullmatch(r"[a-f0-9]{32}", expected_md5):
        raise SourceFileError("Invalid source identifier")
    extension = _normal_extension(extension)
    digest = hashlib.md5()  # nosec B324 - the upstream identifier is an MD5 content key
    total = 0
    header = bytearray()
    try:
        with open(path, "rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                if len(header) < 1024:
                    header.extend(chunk[: 1024 - len(header)])
                digest.update(chunk)
                total += len(chunk)
    except OSError as error:
        raise SourceFileError("Downloaded book could not be read") from error

    if not total:
        raise SourceFileError("Downloaded book was empty")
    if expected_size and total != int(expected_size):
        raise SourceFileError(
            f"Downloaded byte count did not match ({total} of {int(expected_size)})"
        )
    if not _valid_magic(bytes(header), extension):
        raise SourceFileError("Download source returned the wrong file type")
    actual_md5 = digest.hexdigest()
    if actual_md5 != expected_md5:
        raise SourceFileError("Downloaded book did not match its source identifier")
    return SourceValidation(total, actual_md5)


class KindleSourceCache:
    """Atomic shared cache with an idle TTL and a byte-based LRU quota."""

    def __init__(self, root: str, *, ttl: int = 86400, max_bytes: int = 5 * 1024**3):
        self.root = os.path.abspath(root)
        self.ttl = max(0, int(ttl))
        self.max_bytes = max(0, int(max_bytes))

    def _paths(self, book_id: str, extension: str) -> tuple[str, str]:
        book_id = str(book_id or "").casefold()
        if not re.fullmatch(r"[a-f0-9]{32}", book_id):
            raise SourceFileError("Invalid source identifier")
        extension = _normal_extension(extension)
        directory = os.path.join(self.root, book_id[:2])
        path = os.path.join(directory, f"{book_id}.{extension}")
        return path, f"{path}.json"

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def discard(self, path: str) -> None:
        if path and os.path.commonpath((self.root, os.path.abspath(path))) == self.root:
            self._unlink(path)

    def temporary_path(self, book_id: str, extension: str) -> str:
        final_path, _ = self._paths(book_id, extension)
        directory = os.path.dirname(final_path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(
            prefix=f".{book_id}.",
            suffix=".part",
            dir=directory,
            delete=False,
        )
        temporary.close()
        return temporary.name

    def get(self, book_id: str, extension: str) -> str:
        if not self.ttl or not self.max_bytes:
            return ""
        path, metadata_path = self._paths(book_id, extension)
        try:
            stat = os.stat(path)
            if time.time() - stat.st_mtime > self.ttl:
                raise SourceFileError("Cached source expired")
            with open(metadata_path, "r", encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
            if (
                metadata.get("digest") != str(book_id).casefold()
                or int(metadata.get("size") or 0) != stat.st_size
                or metadata.get("extension") != _normal_extension(extension)
            ):
                raise SourceFileError("Cached source metadata did not match")
            with open(path, "rb") as source:
                header = source.read(1024)
            if not _valid_magic(header, _normal_extension(extension)):
                raise SourceFileError("Cached source type did not match")
            now = time.time()
            os.utime(path, (now, now))
            os.utime(metadata_path, (now, now))
            return path
        except (OSError, ValueError, TypeError, json.JSONDecodeError, SourceFileError):
            self._unlink(path)
            self._unlink(metadata_path)
            return ""

    def commit(
        self,
        temporary_path: str,
        book_id: str,
        extension: str,
        validation: SourceValidation | None = None,
    ) -> str:
        if not self.ttl or not self.max_bytes:
            return temporary_path
        final_path, metadata_path = self._paths(book_id, extension)
        if validation is None:
            validation = validate_source_file(temporary_path, book_id, extension)
        if validation.digest != str(book_id).casefold():
            raise SourceFileError("Source validation did not match cache key")
        if os.path.getsize(temporary_path) != validation.size:
            raise SourceFileError("Source changed after validation")
        if validation.size > self.max_bytes:
            return temporary_path

        directory = os.path.dirname(final_path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.replace(temporary_path, final_path)
        os.chmod(final_path, 0o600)
        metadata = {
            "digest": validation.digest,
            "extension": _normal_extension(extension),
            "size": validation.size,
            "validated_at": time.time(),
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{book_id}.",
            suffix=".json.part",
            dir=directory,
            delete=False,
        ) as output:
            json.dump(metadata, output, separators=(",", ":"), sort_keys=True)
            metadata_temporary = output.name
        os.replace(metadata_temporary, metadata_path)
        os.chmod(metadata_path, 0o600)
        self.prune()
        return final_path

    def prune(self) -> None:
        if not os.path.isdir(self.root):
            return
        now = time.time()
        entries: list[tuple[float, int, str]] = []
        for directory, _, filenames in os.walk(self.root):
            for filename in filenames:
                if not filename.endswith((".epub", ".pdf")):
                    continue
                path = os.path.join(directory, filename)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                metadata_path = f"{path}.json"
                if self.ttl and now - stat.st_mtime > self.ttl:
                    self._unlink(path)
                    self._unlink(metadata_path)
                    continue
                entries.append((stat.st_mtime, stat.st_size, path))

        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= self.max_bytes:
                break
            self._unlink(path)
            self._unlink(f"{path}.json")
            total -= size


@dataclass(frozen=True)
class UploadProgress:
    sent: int
    total: int
    rate: float
    eta_seconds: float


def _header_block(message: EmailMessage) -> bytes:
    return b"".join(
        SMTP_POLICY.fold_binary(name, value)
        for name, value in message.raw_items()
    ) + b"\r\n"


def _dot_stuff(content: bytes) -> bytes:
    content = content.replace(b"\r\n.", b"\r\n..")
    return b"." + content if content.startswith(b".") else content


def _base64_wire_size(raw_size: int) -> int:
    encoded = 4 * math.ceil(max(0, raw_size) / 3)
    lines = math.ceil(encoded / 76) if encoded else 0
    return encoded + lines * 2


def stream_smtp_attachment(
    server,
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    attachment_path: str,
    filename: str,
    mime_subtype: str,
    message_id: str = "",
    progress_interval: float = 0.5,
    progress_bytes: int = 256 * 1024,
):
    """Stream one MIME attachment through SMTP DATA and yield true byte progress."""
    attachment_size = os.path.getsize(attachment_path)
    boundary = f"===============libflix-{uuid.uuid4().hex}=="

    outer = EmailMessage(policy=SMTP_POLICY)
    outer["From"] = sender
    outer["To"] = recipient
    outer["Subject"] = subject
    outer["Date"] = formatdate(localtime=False)
    outer["Message-ID"] = message_id or make_msgid(domain="libflix.fomalhaut.app")
    outer["MIME-Version"] = "1.0"
    outer.set_type("multipart/mixed")
    outer.set_boundary(boundary)

    text_part = EmailMessage(policy=SMTP_POLICY)
    text_part.set_content(body)
    attachment = EmailMessage(policy=SMTP_POLICY)
    attachment.set_type(f"application/{mime_subtype}")
    attachment["Content-Transfer-Encoding"] = "base64"
    attachment.add_header("Content-Disposition", "attachment", filename=filename)

    prefix = (
        _header_block(outer)
        + f"--{boundary}\r\n".encode("ascii")
        + text_part.as_bytes(policy=SMTP_POLICY)
        + f"\r\n--{boundary}\r\n".encode("ascii")
        + _header_block(attachment)
    )
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    wire_size = len(prefix) + _base64_wire_size(attachment_size) + len(suffix)

    options = []
    if getattr(server, "does_esmtp", False) and server.has_extn("size"):
        options.append(f"size={wire_size}")
    code, response = server.mail(sender, options)
    if code != 250:
        raise smtplib.SMTPSenderRefused(code, response, sender)
    code, response = server.rcpt(recipient)
    if code not in (250, 251):
        raise smtplib.SMTPRecipientsRefused({recipient: (code, response)})
    code, response = server.docmd("DATA")
    if code != 354:
        raise smtplib.SMTPDataError(code, response)

    server.send(_dot_stuff(prefix))
    started = time.perf_counter()
    sent = 0
    last_reported_at = started
    last_reported_bytes = 0
    with open(attachment_path, "rb") as source:
        while True:
            raw = source.read(57 * 1024)
            if not raw:
                break
            encoded = base64.encodebytes(raw).replace(b"\n", b"\r\n")
            server.send(encoded)
            sent += len(raw)
            now = time.perf_counter()
            if (
                sent == attachment_size
                or sent - last_reported_bytes >= max(1, progress_bytes)
                or now - last_reported_at >= max(0.05, progress_interval)
            ):
                elapsed = max(now - started, 0.001)
                rate = sent / elapsed
                remaining = max(0, attachment_size - sent)
                yield UploadProgress(sent, attachment_size, rate, remaining / max(rate, 1))
                last_reported_at = now
                last_reported_bytes = sent

    server.send(_dot_stuff(suffix))
    server.send(b".\r\n")
    code, response = server.getreply()
    if code != 250:
        raise smtplib.SMTPDataError(code, response)
