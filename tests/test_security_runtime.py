import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from security_runtime import (
    MetricEvent,
    RateLimitRule,
    SQLiteMetrics,
    SQLiteRateLimiter,
    SecurityHeadersConfig,
    apply_security_headers,
    normalize_metric_route,
    request_client_identity,
)


class DummyRequest:
    def __init__(self, *, secure=False, headers=None, remote_addr="127.0.0.1"):
        self.is_secure = secure
        self.headers = headers or {}
        self.remote_addr = remote_addr


class DummyResponse:
    def __init__(self):
        self.headers = {}


class SecurityHeaderTests(unittest.TestCase):
    def test_compat_policy_secures_capabilities_without_breaking_inline_ui(self):
        response = apply_security_headers(DummyResponse(), DummyRequest(secure=True))

        csp = response.headers["Content-Security-Policy"]
        self.assertIn(
            "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com",
            csp,
        )
        self.assertIn("style-src 'self' 'unsafe-inline'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Strict-Transport-Security"], "max-age=31536000")

    def test_hsts_is_not_sent_for_plain_http_or_untrusted_proxy_header(self):
        request = DummyRequest(headers={"X-Forwarded-Proto": "https"})
        response = apply_security_headers(DummyResponse(), request)

        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_hsts_accepts_explicitly_trusted_proxy_scheme(self):
        request = DummyRequest(headers={"X-Forwarded-Proto": "https, http"})
        config = SecurityHeadersConfig(trust_forwarded_proto=True)
        response = apply_security_headers(DummyResponse(), request, config)

        self.assertIn("Strict-Transport-Security", response.headers)

    def test_client_identity_ignores_forged_cloudflare_headers(self):
        request = DummyRequest(
            headers={
                "CF-Ray": "forged",
                "CF-Connecting-IP": "203.0.113.7",
                "X-Forwarded-For": "203.0.113.8",
            },
            remote_addr="10.0.0.2",
        )

        self.assertEqual(request_client_identity(request), "10.0.0.2")

    def test_rewritten_client_header_requires_a_trusted_immediate_peer(self):
        untrusted = DummyRequest(
            headers={"X-LibFlix-Client-IP": "203.0.113.7"},
            remote_addr="198.51.100.8",
        )
        trusted = DummyRequest(
            headers={"X-LibFlix-Client-IP": "203.0.113.7"},
            remote_addr="127.0.0.1",
        )

        self.assertEqual(
            request_client_identity(
                untrusted,
                trusted_client_ip_header="X-LibFlix-Client-IP",
                trusted_proxy_networks=("127.0.0.1/32",),
            ),
            "198.51.100.8",
        )
        self.assertEqual(
            request_client_identity(
                trusted,
                trusted_client_ip_header="X-LibFlix-Client-IP",
                trusted_proxy_networks=("127.0.0.1/32",),
            ),
            "203.0.113.7",
        )


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.tempdir.name, "runtime.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_bucket_is_shared_across_limiter_instances_and_returns_retry_after(self):
        first_worker = SQLiteRateLimiter(self.database)
        second_worker = SQLiteRateLimiter(self.database)
        rule = RateLimitRule(2, 10)

        first = first_worker.check("search", "203.0.113.7", rule, now=100)
        second = second_worker.check("search", "203.0.113.7", rule, now=100)
        denied = first_worker.check("search", "203.0.113.7", rule, now=100)

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.retry_after, 5)
        self.assertEqual(denied.response_headers["Retry-After"], "5")

    def test_tokens_refill_over_time(self):
        limiter = SQLiteRateLimiter(self.database)
        rule = RateLimitRule(1, 10)
        self.assertTrue(limiter.check("download", "client", rule, now=100).allowed)
        self.assertFalse(limiter.check("download", "client", rule, now=105).allowed)
        self.assertTrue(limiter.check("download", "client", rule, now=110).allowed)

    def test_identity_rows_are_hashed_and_safely_bounded(self):
        limiter = SQLiteRateLimiter(self.database, max_entries=100)
        rule = RateLimitRule(1, 60)
        for index in range(105):
            limiter.check("search", f"client-{index}", rule, now=100 + index)

        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT identity_hash FROM rate_limit_buckets"
            ).fetchall()
        self.assertLessEqual(len(rows), 100 + limiter._prune_batch - 1)
        self.assertFalse(any("client-" in row[0] for row in rows))

    def test_identity_pruning_is_periodic_not_per_insert(self):
        limiter = SQLiteRateLimiter(self.database, max_entries=100)
        rule = RateLimitRule(10, 60)

        limiter.check("search", "first", rule, now=100)
        limiter.check("search", "second", rule, now=101)

        self.assertEqual(limiter._new_entries_since_prune, 1)

    def test_storage_failure_fails_open(self):
        limiter = SQLiteRateLimiter(os.path.join("/dev/null", "runtime.sqlite3"))
        decision = limiter.check("search", "client", RateLimitRule(1, 60))

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.degraded)
        self.assertEqual(limiter.degraded_checks, 1)

    def test_limiter_healthcheck_verifies_writable_storage(self):
        self.assertTrue(SQLiteRateLimiter(self.database).healthcheck())
        self.assertFalse(
            SQLiteRateLimiter(os.path.join("/dev/null", "runtime.sqlite3")).healthcheck()
        )


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.tempdir.name, "metrics.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_dynamic_paths_are_collapsed(self):
        self.assertEqual(normalize_metric_route("/book/OL24739863W"), "/book/<work_id>")
        self.assertEqual(normalize_metric_route("/category/user-controlled"), "/category/<topic>")
        self.assertEqual(normalize_metric_route("/unknown/arbitrary/path"), "/other")
        self.assertEqual(normalize_metric_route("/<private-token-123>"), "/other")

    def test_web_vitals_are_durable_aggregates_not_raw_events(self):
        first_worker = SQLiteMetrics(self.database)
        second_worker = SQLiteMetrics(self.database)
        payload = {
            "path": "/book/OL24739863W",
            "lcp": 400,
            "inp": 50,
            "cls": 0.01,
            "ignored": "raw payload",
        }
        self.assertTrue(first_worker.record_web_vitals(payload, now=7_200))
        self.assertTrue(second_worker.record_web_vitals(payload, now=7_300))

        summary = first_worker.summary(since=7_000)
        lcp = next(item for item in summary if item["metric"] == "web.lcp_ms")
        self.assertEqual(lcp["route"], "/book/<work_id>")
        self.assertEqual(lcp["count"], 2)
        self.assertEqual(lcp["average"], 400)

        with closing(sqlite3.connect(self.database)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(metric_hourly)")
            }
        self.assertNotIn("payload", columns)
        self.assertNotIn("path", columns)

    def test_request_timings_and_errors_are_aggregated(self):
        metrics = SQLiteMetrics(self.database)
        metrics.record_request("/api/search", "GET", 503, 125.5, now=7_200)

        summary = metrics.summary(since=7_000)
        names = {item["metric"] for item in summary}
        self.assertEqual(
            names,
            {"http.requests", "http.duration_ms", "http.errors"},
        )

    def test_storage_failure_is_counted_without_raising(self):
        metrics = SQLiteMetrics(os.path.join("/dev/null", "metrics.sqlite3"))

        self.assertFalse(metrics.record_request("/", "GET", 200, 1))
        self.assertEqual(metrics.dropped_writes, 1)

    def test_metrics_healthcheck_verifies_writable_storage(self):
        self.assertTrue(SQLiteMetrics(self.database).healthcheck())
        self.assertFalse(
            SQLiteMetrics(os.path.join("/dev/null", "metrics.sqlite3")).healthcheck()
        )

    def test_retention_and_hard_row_cap_are_enforced(self):
        metrics = SQLiteMetrics(self.database, retention_seconds=3_600, max_rows=100)
        metrics.record([MetricEvent("http.requests", 1, "/", "old")], now=0)
        events = [
            MetricEvent("http.requests", 1, "/", f"label-{index}")
            for index in range(105)
        ]
        metrics.record(events, now=7_200)

        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM metric_hourly").fetchone()[0]
            old = connection.execute(
                "SELECT COUNT(*) FROM metric_hourly WHERE bucket_start = 0"
            ).fetchone()[0]
        self.assertEqual(count, 100)
        self.assertEqual(old, 0)


if __name__ == "__main__":
    unittest.main()
