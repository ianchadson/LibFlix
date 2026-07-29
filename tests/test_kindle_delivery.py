import smtplib
import unittest
from unittest.mock import patch

import app


class FakeDownloadResponse:
    headers = {"content-length": "4"}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield b"book"


class FakeSMTP:
    connections = 0

    def __init__(self, host, port, timeout=45):
        type(self).connections += 1
        self.connection_number = type(self).connections
        self.sock = None

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, context=None):
        return 220, b"ready"

    def login(self, user, password):
        return 235, b"authenticated"

    def send_message(self, message):
        if self.connection_number == 1:
            raise smtplib.SMTPServerDisconnected("Server not connected")
        return {}

    def quit(self):
        return 221, b"bye"

    def close(self):
        return None


class KindleDeliveryTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.connections = 0

    def test_disconnected_upload_reconnects_once(self):
        data = {
            "md5": "a" * 32,
            "title": "Test Book",
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


if __name__ == "__main__":
    unittest.main()
