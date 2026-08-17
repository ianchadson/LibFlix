import os
import tempfile
import unittest
from unittest.mock import patch

import app


class KindleJobTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_rate_limiting = app.app.config["RATE_LIMITING_ENABLED"]
        app.app.config["RATE_LIMITING_ENABLED"] = False
        self.original_database = app.API_SQLITE_CACHE
        self.original_ready = app.SQLITE_CACHE_READY
        self.original_delivery_lock = app.KINDLE_DELIVERY_LOCK_FILE
        app.API_SQLITE_CACHE = os.path.join(self.tempdir.name, "jobs.sqlite3")
        app.KINDLE_DELIVERY_LOCK_FILE = os.path.join(self.tempdir.name, "kindle-delivery.lock")
        app.SQLITE_CACHE_READY = False
        app.initialize_disk_cache()

    def tearDown(self):
        app.app.config["RATE_LIMITING_ENABLED"] = self.original_rate_limiting
        app.API_SQLITE_CACHE = self.original_database
        app.SQLITE_CACHE_READY = self.original_ready
        app.KINDLE_DELIVERY_LOCK_FILE = self.original_delivery_lock
        self.tempdir.cleanup()

    def test_job_events_are_visible_across_database_connections(self):
        job_id = app.kindle_job_create()
        app.kindle_job_append(job_id, {
            "type": "progress",
            "stage": "Downloading book",
            "progress": 40,
        })
        app.kindle_job_append(job_id, {
            "type": "complete",
            "success": True,
            "stage": "Sent to Kindle",
            "progress": 100,
        })

        job = app.kindle_job_get(job_id)

        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["events"][-1]["progress"], 100)
        self.assertEqual(job["cursor"], 3)

    def test_job_storage_never_contains_smtp_credentials(self):
        job_id = app.kindle_job_create()
        with patch.object(
            app,
            "_send_to_kindle_events",
            return_value=iter([{"type": "complete", "success": True, "progress": 100}]),
        ):
            app.run_kindle_job(job_id, {"smtp_pass": "secret-value"})

        with open(app.API_SQLITE_CACHE, "rb") as database:
            content = database.read()

        self.assertNotIn(b"secret-value", content)

    def test_create_job_returns_pollable_job_id(self):
        payload = {
            "md5": "a" * 32,
            "title": "Book",
            "ext": "epub",
            "kindle_email": "reader@kindle.com",
            "smtp_host": "smtp.user.example",
            "smtp_port": 465,
            "smtp_user": "user@example.com",
            "smtp_pass": "user-secret",
            "sender_email": "user@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "sender@example.com",
            "smtp_pass": "secret",
        }
        public_dns = [(2, 1, 6, "", ("8.8.8.8", 587))]
        with (
            app.app.test_client() as client,
            patch.object(app.socket, "getaddrinfo", return_value=public_dns) as getaddrinfo,
            patch.object(app.KINDLE_EXECUTOR, "submit") as submit,
        ):
            response = client.post("/api/kindle/jobs", json=payload)
            body = response.get_json()

        self.assertEqual(response.status_code, 202)
        self.assertRegex(body["job_id"], r"^[a-f0-9]{32}$")
        submit.assert_called_once()

    def test_create_job_rejects_unsupported_mobi_format(self):
        payload = {
            "md5": "a" * 32,
            "title": "Book",
            "ext": "mobi",
            "kindle_email": "reader@kindle.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "sender@example.com",
            "smtp_pass": "secret",
        }
        with (
            app.app.test_client() as client,
            patch.object(app.KINDLE_EXECUTOR, "submit") as submit,
        ):
            response = client.post("/api/kindle/jobs", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("not supported", response.get_json()["error"])
        submit.assert_not_called()

    def test_create_job_rejects_visible_but_undeliverable_fb2_format(self):
        payload = {
            "md5": "a" * 32,
            "title": "Book",
            "ext": "fb2",
            "kindle_email": "reader@kindle.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "sender@example.com",
            "smtp_pass": "secret",
        }
        with (
            app.app.test_client() as client,
            patch.object(app.KINDLE_EXECUTOR, "submit") as submit,
        ):
            response = client.post("/api/kindle/jobs", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("EPUB or PDF", response.get_json()["error"])
        submit.assert_not_called()

    def test_managed_relay_keeps_existing_user_smtp_optional(self):
        payload = {
            "md5": "a" * 32,
            "title": "Book",
            "ext": "epub",
            "kindle_email": "reader@kindle.com",
        }
        public_dns = [(2, 1, 6, "", ("8.8.8.8", 587))]
        with (
            patch.object(app, "KINDLE_RELAY_HOST", "smtp.relay.example"),
            patch.object(app, "KINDLE_RELAY_PORT", "587"),
            patch.object(app, "KINDLE_RELAY_USER", "relay@example.com"),
            patch.object(app, "KINDLE_RELAY_PASSWORD", "relay-secret"),
            patch.object(app, "KINDLE_RELAY_SENDER", "books@example.com"),
            patch.object(app.socket, "getaddrinfo", return_value=public_dns) as getaddrinfo,
            patch.object(app.KINDLE_EXECUTOR, "submit") as submit,
            app.app.test_client() as client,
        ):
            response = client.post("/api/kindle/jobs", json=payload)

        self.assertEqual(response.status_code, 202)
        submitted_payload = submit.call_args.args[2]
        self.assertNotIn("relay-secret", repr(submitted_payload))
        self.assertNotIn("user-secret", repr(submitted_payload))
        self.assertNotIn("smtp_pass", submitted_payload)
        self.assertNotIn("smtp_host", submitted_payload)
        getaddrinfo.assert_called_once_with(
            "smtp.relay.example",
            587,
            type=app.socket.SOCK_STREAM,
        )

    def test_invalid_managed_relay_port_does_not_fall_back_to_user_smtp(self):
        payload = {
            "md5": "a" * 32,
            "title": "Book",
            "ext": "epub",
            "kindle_email": "reader@kindle.com",
        }
        with (
            patch.object(app, "KINDLE_RELAY_HOST", "smtp.relay.example"),
            patch.object(app, "KINDLE_RELAY_PORT", "not-a-port"),
            patch.object(app, "KINDLE_RELAY_USER", "relay@example.com"),
            patch.object(app, "KINDLE_RELAY_PASSWORD", "relay-secret"),
            patch.object(app, "KINDLE_RELAY_SENDER", "books@example.com"),
            patch.object(app.socket, "getaddrinfo") as getaddrinfo,
            patch.object(app.KINDLE_EXECUTOR, "submit") as submit,
            app.app.test_client() as client,
        ):
            response = client.post("/api/kindle/jobs", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "SMTP port must be a number")
        getaddrinfo.assert_not_called()
        submit.assert_not_called()

    def test_managed_relay_rejects_non_kindle_recipients(self):
        payload = {
            "md5": "a" * 32,
            "title": "Book",
            "ext": "epub",
            "kindle_email": "reader@example.com",
        }
        with (
            patch.object(app, "KINDLE_RELAY_HOST", "smtp.relay.example"),
            patch.object(app, "KINDLE_RELAY_PORT", "587"),
            patch.object(app, "KINDLE_RELAY_USER", "relay@example.com"),
            patch.object(app, "KINDLE_RELAY_PASSWORD", "relay-secret"),
            patch.object(app, "KINDLE_RELAY_SENDER", "books@example.com"),
            patch.object(app.socket, "getaddrinfo") as getaddrinfo,
            patch.object(app.KINDLE_EXECUTOR, "submit") as submit,
            app.app.test_client() as client,
        ):
            response = client.post("/api/kindle/jobs", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("@kindle.com", response.get_json()["error"])
        getaddrinfo.assert_not_called()
        submit.assert_not_called()

    def test_managed_relay_reads_file_backed_secret_without_exposing_it(self):
        secret_path = os.path.join(self.tempdir.name, "resend-api-key")
        with open(secret_path, "w", encoding="utf-8") as secret_file:
            secret_file.write("server-only-secret")
        os.chmod(secret_path, 0o600)

        with (
            patch.object(app, "KINDLE_RELAY_PASSWORD", ""),
            patch.object(app, "KINDLE_RELAY_PASSWORD_FILE", secret_path),
            patch.object(app, "KINDLE_RELAY_HOST", "smtp.resend.com"),
            patch.object(app, "KINDLE_RELAY_PORT", "465"),
            patch.object(app, "KINDLE_RELAY_USER", "resend"),
            patch.object(app, "KINDLE_RELAY_SENDER", "libflix@fomalhaut.app"),
        ):
            relay = app._configured_kindle_relay()
            with app.app.test_request_context("/"):
                context = app.inject_book_context()

        self.assertEqual(relay["password"], "server-only-secret")
        self.assertEqual(relay["sender"], "libflix@fomalhaut.app")
        self.assertTrue(context["kindle_managed_relay"])
        self.assertEqual(context["kindle_managed_sender"], "libflix@fomalhaut.app")
        self.assertNotIn("server-only-secret", repr(context))


if __name__ == "__main__":
    unittest.main()
