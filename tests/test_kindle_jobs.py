import os
import tempfile
import unittest
from unittest.mock import patch

import app


class KindleJobTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_database = app.API_SQLITE_CACHE
        self.original_ready = app.SQLITE_CACHE_READY
        app.API_SQLITE_CACHE = os.path.join(self.tempdir.name, "jobs.sqlite3")
        app.SQLITE_CACHE_READY = False
        app.initialize_disk_cache()

    def tearDown(self):
        app.API_SQLITE_CACHE = self.original_database
        app.SQLITE_CACHE_READY = self.original_ready
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
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "sender@example.com",
            "smtp_pass": "secret",
        }
        public_dns = [(2, 1, 6, "", ("8.8.8.8", 587))]
        with (
            app.app.test_client() as client,
            patch.object(app.socket, "getaddrinfo", return_value=public_dns),
            patch.object(app.KINDLE_EXECUTOR, "submit") as submit,
        ):
            response = client.post("/api/kindle/jobs", json=payload)
            body = response.get_json()

        self.assertEqual(response.status_code, 202)
        self.assertRegex(body["job_id"], r"^[a-f0-9]{32}$")
        submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
