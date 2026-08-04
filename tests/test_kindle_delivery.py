import hashlib
import os
import smtplib
import tempfile
import time
import unittest
from email import policy
from email.parser import BytesParser
from unittest.mock import patch

import app

BOOK_BYTES = b"PK\x03\x04libflix-test-book"
BOOK_MD5 = hashlib.md5(BOOK_BYTES).hexdigest()


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "content-length": str(len(BOOK_BYTES)),
        "content-type": "application/epub+zip",
    }

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield BOOK_BYTES

    def close(self):
        return None


class InterruptedDownloadResponse:
    status_code = 200
    headers = {"content-length": "10"}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield b"book"
        raise app.requests.exceptions.ChunkedEncodingError("connection interrupted")

    def close(self):
        return None


class ResumedDownloadResponse:
    status_code = 206
    headers = {"content-length": "6", "content-range": "bytes 4-9/10"}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield b"keeper"

    def close(self):
        return None


class FullDownloadResponse:
    status_code = 200
    headers = {"content-length": "10"}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield b"bookkeeper"

    def close(self):
        return None


class FakeSMTP:
    connections = 0
    messages = []
    fail_first = True

    def __init__(self, host, port, timeout=45):
        type(self).connections += 1
        self.connection_number = type(self).connections
        self.sock = None
        self.does_esmtp = False
        self.buffer = bytearray()

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, context=None):
        return 220, b"ready"

    def login(self, user, password):
        return 235, b"authenticated"

    def noop(self):
        return 250, b"ok"

    def mail(self, sender, options=()):
        return 250, b"ok"

    def rcpt(self, recipient):
        return 250, b"ok"

    def docmd(self, command):
        self.buffer.clear()
        return 354, b"continue"

    def send(self, content):
        if self.fail_first and self.connection_number == 1:
            raise smtplib.SMTPServerDisconnected("Server not connected")
        self.buffer.extend(content)

    def getreply(self):
        content = bytes(self.buffer)
        if content.endswith(b".\r\n"):
            content = content[:-3]
        type(self).messages.append(BytesParser(policy=policy.default).parsebytes(content))
        return 250, b"accepted"

    def quit(self):
        return 221, b"bye"

    def close(self):
        return None


class KindleDeliveryTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.connections = 0
        FakeSMTP.messages = []
        FakeSMTP.fail_first = True
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_source_cache = app.KINDLE_SOURCE_CACHE_DIR
        app.KINDLE_SOURCE_CACHE_DIR = os.path.join(self.tempdir.name, "source-cache")

    def tearDown(self):
        app.KINDLE_SOURCE_CACHE_DIR = self.original_source_cache
        self.tempdir.cleanup()

    def test_disconnected_upload_reconnects_once(self):
        data = {
            "md5": BOOK_MD5,
            "title": "Noisy_Test_Book [EPUB]",
            "canonical_title": "Test Book",
            "author": "Example Author",
            "ext": "epub",
            "kindle_email": "reader@kindle.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "sender@example.com",
            "smtp_pass": "app-password",
        }

        with (
            patch.object(app.DOWNLOADER, "resolve_download", return_value="https://example.com/book.epub"),
            patch.object(app.SESSION, "get", return_value=FakeDownloadResponse()),
            patch("smtplib.SMTP", FakeSMTP),
        ):
            events = list(app._send_to_kindle_events(data))

        self.assertEqual(FakeSMTP.connections, 2)
        self.assertIn("Reconnecting to email", [event.get("stage") for event in events])
        self.assertEqual(events[-1]["type"], "complete")
        self.assertTrue(events[-1]["success"])
        attachment = next(
            part for part in FakeSMTP.messages[-1].walk()
            if part.get_content_disposition() == "attachment"
        )
        self.assertEqual(attachment.get_filename(), "Test Book.epub")
        self.assertIn("Polishing book details", [event.get("stage") for event in events])
        self.assertEqual(events[-1]["title"], "Test Book")
        self.assertGreaterEqual(events[-1]["elapsed_seconds"], 0)
        self.assertIn("stage_durations", events[-1])
        self.assertTrue(all("timestamp" in event for event in events))
        upload_events = [
            event for event in events
            if event.get("stage") == "Uploading to email"
        ]
        self.assertTrue(upload_events)
        upload = upload_events[-1]
        self.assertEqual(upload["uploaded_bytes"], len(BOOK_BYTES))
        self.assertEqual(upload["upload_total_bytes"], len(BOOK_BYTES))
        self.assertGreater(upload["upload_rate_bytes_per_second"], 0)
        self.assertEqual(upload["upload_eta_seconds"], 0)

    def test_slow_download_resolution_emits_heartbeat(self):
        data = {
            "md5": BOOK_MD5,
            "title": "Test Book",
            "ext": "epub",
            "kindle_email": "reader@kindle.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "sender@example.com",
            "smtp_pass": "app-password",
        }

        def slow_resolve(book_id):
            time.sleep(0.03)
            return "https://example.com/book.epub"

        with (
            patch.object(app.DOWNLOADER, "resolve_download", side_effect=slow_resolve),
            patch.object(app.SESSION, "get", return_value=FakeDownloadResponse()),
            patch("smtplib.SMTP", FakeSMTP),
            patch.object(app, "RESOLVE_HEARTBEAT_SECONDS", 0.01),
        ):
            events = list(app._send_to_kindle_events(data))

        finding_events = [event for event in events if event.get("stage") == "Finding book file"]
        self.assertGreaterEqual(len(finding_events), 2)
        self.assertTrue(any(event.get("progress") is None for event in finding_events))
        self.assertEqual(events[-1]["type"], "complete")

    def test_kindle_cover_reuses_displayed_cached_variant(self):
        md5 = "c" * 32
        cover_dir = "123000"
        identity = f"{cover_dir}:{md5}"
        with tempfile.TemporaryDirectory() as cache_dir:
            original_cache_dir = app.COVER_CACHE_DIR
            app.COVER_CACHE_DIR = cache_dir
            try:
                cached_path = app.cover_cache_path("downloads", identity, "S")
                os.makedirs(os.path.dirname(cached_path), exist_ok=True)
                with open(cached_path, "wb") as cached:
                    cached.write(b"displayed-cover" * 20)
                with (
                    patch.object(app, "_cover_file_as_jpeg", return_value=b"reused") as convert,
                    patch.object(app, "ensure_cover_cached") as fetch_cover,
                ):
                    result = app._kindle_cover_bytes(f"/cover/{md5}/S?dir={cover_dir}")
            finally:
                app.COVER_CACHE_DIR = original_cache_dir

        self.assertEqual(result, b"reused")
        convert.assert_called_once_with(cached_path)
        fetch_cover.assert_not_called()

    def test_interrupted_download_resumes_without_corrupting_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as target:
            destination = target.name
        self.addCleanup(lambda: os.path.exists(destination) and os.unlink(destination))

        with (
            patch.object(
                app.SESSION,
                "get",
                side_effect=[InterruptedDownloadResponse(), ResumedDownloadResponse()],
            ) as get,
            patch.object(app.time, "sleep"),
        ):
            events = []
            progress = app._download_book_progress("https://example.com/book.epub", destination)
            while True:
                try:
                    events.append(next(progress))
                except StopIteration as complete:
                    downloaded, total = complete.value
                    break

        with open(destination, "rb") as downloaded_file:
            self.assertEqual(downloaded_file.read(), b"bookkeeper")
        self.assertEqual((downloaded, total), (10, 10))
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].kwargs["headers"]["Range"], "bytes=4-")
        self.assertIn("Resuming book download", [event.get("stage") for event in events])

    def test_range_ignoring_source_restarts_instead_of_appending(self):
        with tempfile.NamedTemporaryFile(delete=False) as target:
            destination = target.name
        self.addCleanup(lambda: os.path.exists(destination) and os.unlink(destination))

        with (
            patch.object(
                app.SESSION,
                "get",
                side_effect=[InterruptedDownloadResponse(), FullDownloadResponse()],
            ),
            patch.object(app.time, "sleep"),
        ):
            progress = app._download_book_progress("https://example.com/book.epub", destination)
            while True:
                try:
                    next(progress)
                except StopIteration as complete:
                    downloaded, total = complete.value
                    break

        with open(destination, "rb") as downloaded_file:
            self.assertEqual(downloaded_file.read(), b"bookkeeper")
        self.assertEqual((downloaded, total), (10, 10))


if __name__ == "__main__":
    unittest.main()
