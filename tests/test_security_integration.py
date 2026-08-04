import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from security_runtime import RateLimitDecision


class SecurityIntegrationTests(unittest.TestCase):
    def test_responses_receive_security_headers_and_https_hsts(self):
        response = app.app.test_client().get(
            "/api/health",
            headers={"X-Forwarded-Proto": "https"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Strict-Transport-Security"], "max-age=31536000")

    def test_service_worker_can_control_root_but_is_never_immutably_cached(self):
        response = app.app.test_client().get("/static/libflix-sw.js?v=test")
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
        self.assertEqual(
            response.headers["Cache-Control"],
            "no-cache, max-age=0, must-revalidate",
        )

    def test_expensive_endpoint_returns_json_429_with_retry_after(self):
        denied = RateLimitDecision(
            allowed=False,
            limit=1,
            remaining=0,
            retry_after=17,
        )
        with (
            patch.dict(app.app.config, {"RATE_LIMITING_ENABLED": True}),
            patch.object(app.RUNTIME_RATE_LIMITER, "check", return_value=denied),
        ):
            response = app.app.test_client().get(
                "/api/search?q=test",
                environ_overrides={"libflix.enforce_rate_limits": True},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "17")
        self.assertEqual(response.get_json()["retry_after"], 17)

    def test_web_vitals_are_persisted_and_http_request_is_aggregated(self):
        with (
            patch.object(app.RUNTIME_METRICS, "record_web_vitals") as web_vitals,
            patch.object(app.RUNTIME_METRICS, "record_request") as request_metric,
        ):
            response = app.app.test_client().post(
                "/api/metrics/web-vitals",
                json={"path": "/book/OL1W", "lcp": 420, "cls": 0.01},
            )

        self.assertEqual(response.status_code, 204)
        web_vitals.assert_called_once()
        request_metric.assert_called_once()

    def test_search_aliases_are_charged_by_upstream_fanout(self):
        denied = RateLimitDecision(False, 24, 0, retry_after=3)
        aliases = json.dumps(["one", "two", "three"])
        with (
            patch.dict(app.app.config, {"RATE_LIMITING_ENABLED": True}),
            patch.object(app.RUNTIME_RATE_LIMITER, "check", return_value=denied) as check,
        ):
            response = app.app.test_client().get(
                "/api/search",
                query_string={"q": "book", "search_aliases": aliases},
                environ_overrides={"libflix.enforce_rate_limits": True},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(check.call_args.kwargs["cost"], 4)

    def test_server_owned_book_identity_search_uses_maximum_cost(self):
        denied = RateLimitDecision(False, 24, 0, retry_after=3)
        with (
            patch.dict(app.app.config, {"RATE_LIMITING_ENABLED": True}),
            patch.object(app.RUNTIME_RATE_LIMITER, "check", return_value=denied) as check,
        ):
            response = app.app.test_client().get(
                "/api/search",
                query_string={"q": "book", "ol_key": "/works/OL1W"},
                environ_overrides={"libflix.enforce_rate_limits": True},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            check.call_args.kwargs["cost"],
            app.DOWNLOAD_ALIAS_SEARCH_LIMIT,
        )

    def test_exhausted_client_does_not_consume_global_capacity(self):
        denied = RateLimitDecision(False, 24, 0, retry_after=3)
        with (
            patch.dict(app.app.config, {"RATE_LIMITING_ENABLED": True}),
            patch.object(app, "TRUST_PROXY_HEADERS", True),
            patch.object(app.RUNTIME_RATE_LIMITER, "check", return_value=denied) as check,
        ):
            response = app.app.test_client().get(
                "/api/search?q=book",
                headers={"X-LibFlix-Client-IP": "203.0.113.7"},
                environ_overrides={"libflix.enforce_rate_limits": True},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args.args[0:2], ("search", "203.0.113.7"))

    def test_unconfigured_loopback_proxy_uses_only_global_bucket(self):
        allowed = RateLimitDecision(True, 120, 119, retry_after=0)
        with (
            patch.dict(app.app.config, {"RATE_LIMITING_ENABLED": True}),
            patch.object(app, "TRUST_PROXY_HEADERS", False),
            patch.object(app.RUNTIME_RATE_LIMITER, "check", return_value=allowed) as check,
            patch.object(app.DOWNLOADER, "search", return_value=([], 0)),
        ):
            response = app.app.test_client().get(
                "/api/search?q=book",
                environ_overrides={"libflix.enforce_rate_limits": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args.args[0:2], ("search-global", "all-clients"))

    def test_global_json_body_cap_returns_api_error(self):
        with patch.dict(app.app.config, {"RATE_LIMITING_ENABLED": False}):
            response = app.app.test_client().post(
                "/api/kindle/jobs",
                data=b"x" * (65 * 1024),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["error"], "Request body is too large")

    def test_content_length_cap_runs_before_a_view_reads_the_body(self):
        response = app.app.test_client().get(
            "/api/health",
            data=b"x" * (65 * 1024),
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["error"], "Request body is too large")

    def test_metadata_endpoints_have_weighted_client_and_global_limits(self):
        self.assertIn("api_discover", app.RUNTIME_RATE_LIMIT_RULES)
        self.assertIn("api_similar", app.RUNTIME_RATE_LIMIT_RULES)
        self.assertIn("api_book", app.RUNTIME_RATE_LIMIT_RULES)
        self.assertIn("api_discover", app.RUNTIME_GLOBAL_RATE_LIMIT_RULES)
        self.assertIn("api_similar", app.RUNTIME_GLOBAL_RATE_LIMIT_RULES)
        self.assertIn("api_book", app.RUNTIME_GLOBAL_RATE_LIMIT_RULES)

        with app.app.test_request_context(
            "/api/similar?ol_key=/works/OL1W&subject=One&subject=Two&author=A"
        ):
            self.assertEqual(app.request_rate_limit_cost(), 3)

    def test_topic_discovery_cost_tracks_fanout_but_cached_pages_are_cheap(self):
        with app.app.test_request_context(
            "/api/discover?q=unmapped+topic&intent=topic"
        ):
            self.assertEqual(app.request_rate_limit_cost(), 7)
        with app.app.test_request_context(
            "/api/discover?q=focus&intent=topic&page=2"
        ):
            self.assertEqual(app.request_rate_limit_cost(), 1)
        with app.app.test_request_context(
            "/api/discover?q=Deep+Work&intent=identity"
        ):
            self.assertEqual(app.request_rate_limit_cost(), 2)

    def test_topic_cost_is_applied_to_client_and_global_buckets(self):
        allowed = RateLimitDecision(True, 24, 17, retry_after=0)
        denied = RateLimitDecision(False, 120, 0, retry_after=5)
        with (
            patch.dict(app.app.config, {"RATE_LIMITING_ENABLED": True}),
            patch.object(app, "TRUST_PROXY_HEADERS", True),
            patch.object(
                app.RUNTIME_RATE_LIMITER,
                "check",
                side_effect=[allowed, denied],
            ) as check,
        ):
            response = app.app.test_client().get(
                "/api/discover?q=unknown&intent=topic",
                headers={"X-LibFlix-Client-IP": "203.0.113.8"},
                environ_overrides={"libflix.enforce_rate_limits": True},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(check.call_args_list[0].args[:2], ("discovery", "203.0.113.8"))
        self.assertEqual(check.call_args_list[1].args[:2], ("discovery-global", "all-clients"))
        self.assertTrue(all(call.kwargs["cost"] == 7 for call in check.call_args_list))

    def test_provider_metadata_is_rendered_without_inner_html_sinks(self):
        discover = (Path(app.APP_DIR) / "templates" / "discover.html").read_text()
        create_card = discover.split("function createBookLink", 1)[1].split(
            "function initializeRenderedKeys", 1
        )[0]
        navbar = (Path(app.APP_DIR) / "templates" / "_navbar.html").read_text()
        quick_peek = navbar.split("const renderQuickPeek", 1)[1].split(
            "const positionQuickPeek", 1
        )[0]

        self.assertNotIn("innerHTML", create_card)
        self.assertIn("titleNode.textContent = displayTitle", create_card)
        self.assertNotIn("innerHTML", quick_peek)
        self.assertIn("descriptionNode.textContent", quick_peek)

    def test_topic_benchmark_pacing_matches_weighted_client_budget(self):
        workflow = (
            Path(app.APP_DIR) / ".github" / "workflows" / "topic-quality.yml"
        ).read_text()
        sustainable_delay = 7 / (24 / 60)

        self.assertLessEqual(sustainable_delay, 18)
        self.assertIn("--delay 18", workflow)

    def test_noncanonical_book_key_is_rejected_before_refresh(self):
        with patch.object(app, "get_book_detail") as detail:
            response = app.app.test_client().get(
                "/api/book?ol_key=/works/not-a-work"
            )

        self.assertEqual(response.status_code, 400)
        detail.assert_not_called()

    def test_health_fails_when_runtime_protection_storage_is_unavailable(self):
        with patch.object(app.RUNTIME_RATE_LIMITER, "healthcheck", return_value=False):
            response = app.app.test_client().get("/api/health")

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["runtime_protection"]["rate_limiter_ready"])

    def test_background_metadata_queues_are_hard_capped(self):
        with (
            patch.object(app, "BOOK_DETAIL_REFRESHING", {("en", "OL1W")}),
            patch.object(app, "BOOK_DETAIL_REFRESH_PENDING_LIMIT", 1),
            patch.object(app.BOOK_DETAIL_EXECUTOR, "submit") as book_submit,
        ):
            self.assertFalse(app.schedule_book_detail_refresh("OL2W", "en"))
        book_submit.assert_not_called()

        with (
            patch.object(app, "SIMILAR_REFRESHING", {"existing"}),
            patch.object(app, "SIMILAR_REFRESH_PENDING_LIMIT", 1),
            patch.object(app.SIMILAR_EXECUTOR, "submit") as similar_submit,
        ):
            self.assertFalse(
                app.schedule_similar_refresh(
                    "new", "/works/OL2W", ["History"], "en"
                )
            )
        similar_submit.assert_not_called()

    def test_memory_cache_evicts_oldest_entries_at_its_cap(self):
        with (
            patch.object(app, "CACHE", {}),
            patch.object(app, "MEMORY_CACHE_MAX_ENTRIES", 2),
            patch.object(app, "MEMORY_CACHE_NEXT_PRUNE_AT", float("inf")),
        ):
            app.cache_set("first", 1)
            app.cache_set("second", 2)
            app.cache_set("third", 3)

            self.assertEqual(len(app.CACHE), 2)
            self.assertNotIn("first", app.CACHE)

    def test_disk_cache_pruning_enforces_row_and_byte_caps(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE api_cache ("
            "cache_key TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO api_cache(cache_key, created_at, payload) VALUES (?, ?, ?)",
            [(str(index), float(index), "data") for index in range(4)],
        )

        with (
            patch.object(app, "API_CACHE_MAX_ROWS", 3),
            patch.object(app, "API_CACHE_MAX_BYTES", 8),
        ):
            app.prune_disk_cache(connection, now=4)

        rows = connection.execute(
            "SELECT cache_key FROM api_cache ORDER BY created_at"
        ).fetchall()
        self.assertEqual(rows, [("2",), ("3",)])

    def test_limiter_and_metrics_use_separate_databases(self):
        self.assertNotEqual(
            app.RUNTIME_RATE_LIMITER.database_path,
            app.RUNTIME_METRICS.database_path,
        )

    def test_app_ignores_public_cloudflare_headers_for_identity(self):
        with (
            patch.object(app, "TRUST_PROXY_HEADERS", True),
            app.app.test_request_context(
                "/",
                headers={
                    "CF-Ray": "forged",
                    "CF-Connecting-IP": "203.0.113.7",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            ),
        ):
            self.assertEqual(app.trusted_proxy_client_identity(), "127.0.0.1")

    def test_legacy_kindle_concurrency_cap_returns_retry_after(self):
        self.assertTrue(app.KINDLE_LEGACY_SEMAPHORE.acquire(blocking=False))
        self.assertTrue(app.KINDLE_LEGACY_SEMAPHORE.acquire(blocking=False))
        try:
            with patch.object(app, "validate_kindle_payload", return_value=""):
                response = app.app.test_client().post(
                    "/api/sendtokindle",
                    json={},
                )
        finally:
            app.KINDLE_LEGACY_SEMAPHORE.release()
            app.KINDLE_LEGACY_SEMAPHORE.release()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "15")


if __name__ == "__main__":
    unittest.main()
