import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from bs4 import BeautifulSoup

import app
from topic_discovery import (
    BROWSE_TOPIC_GROUPS,
    BROWSE_TOPIC_QUERIES,
    FEATURED_TOPIC_QUERIES,
    TOPIC_EXPANSIONS,
)


class TopicBrowseCatalogTests(unittest.TestCase):
    def test_public_catalog_is_unique_canonical_and_quality_monitored(self):
        self.assertEqual(len(BROWSE_TOPIC_QUERIES), 30)
        self.assertEqual(len(BROWSE_TOPIC_QUERIES), len(set(BROWSE_TOPIC_QUERIES)))
        self.assertTrue(set(BROWSE_TOPIC_QUERIES).issubset(TOPIC_EXPANSIONS))
        self.assertNotIn("startup", BROWSE_TOPIC_QUERIES)
        self.assertNotIn("ai", BROWSE_TOPIC_QUERIES)
        self.assertTrue(set(FEATURED_TOPIC_QUERIES).issubset(BROWSE_TOPIC_QUERIES))
        self.assertEqual(len(BROWSE_TOPIC_GROUPS), 6)

    def test_topic_url_is_encoded_explicit_and_nonfiction(self):
        url = app.topic_discover_url("mental health", "fiction", "cn")
        parsed = urlsplit(url)

        self.assertEqual(parsed.path, "/cn/discover")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "q": ["mental health"],
                "intent": ["topic"],
                "type": ["nonfiction"],
            },
        )


class TopicBrowsePageTests(unittest.TestCase):
    def test_topics_page_is_zero_fetch_and_links_to_semantic_discovery(self):
        with (
            patch.object(app, "fetch_topic_discovery_payload") as topic_provider,
            patch.object(app, "fetch_discovery_books") as identity_provider,
        ):
            response = app.app.test_client().get("/topics")

        self.assertEqual(response.status_code, 200)
        topic_provider.assert_not_called()
        identity_provider.assert_not_called()
        page = BeautifulSoup(response.data, "html.parser")
        self.assertEqual(page.select_one("h1").get_text(" ", strip=True), "Explore topics")
        self.assertEqual(len(page.select(".featured-topic")), 4)
        self.assertEqual(len(page.select(".topic-group")), 6)
        self.assertIsNotNone(page.select_one('a[aria-current="page"][href="/topics"]'))
        self.assertIsNotNone(page.select_one('.cat-tab.active[href="/topics"]'))
        focus = page.select_one('.topic-link[href^="/discover?q=focus"]')
        self.assertIsNotNone(focus)
        focus_query = parse_qs(urlsplit(focus["href"]).query)
        self.assertEqual(focus_query["intent"], ["topic"])
        self.assertEqual(focus_query["type"], ["nonfiction"])
        search = page.select_one("form.topic-search")
        self.assertEqual(search.get("action"), "/discover")
        self.assertEqual(search.select_one('input[name="intent"]')["value"], "topic")
        self.assertEqual(search.select_one('input[name="type"]')["value"], "nonfiction")

    def test_chinese_topics_route_preserves_language_and_nonfiction_mode(self):
        response = app.app.test_client().get("/cn/topics")

        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.data, "html.parser")
        self.assertEqual(page.html.get("lang"), "zh")
        focus = page.select_one('.topic-link[href^="/cn/discover?q=focus"]')
        self.assertIsNotNone(focus)
        english = page.select_one('a[title="English books"]')
        self.assertIn("next=/topics", english["href"])

    def test_topics_are_intentionally_nonfiction_only(self):
        client = app.app.test_client()
        redirect = client.get("/topics?mode=fiction")

        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(redirect.headers["Location"], "/topics")
        self.assertEqual(client.get("/fiction/topics").status_code, 404)

    def test_home_has_a_compact_topic_entry_point_but_fiction_does_not(self):
        with patch.object(app, "get_shelves", return_value=[]):
            nonfiction = app.app.test_client().get("/")
            fiction = app.app.test_client().get("/fiction")

        self.assertIn(b"Explore by topic", nonfiction.data)
        self.assertIn(b"Browse all topics", nonfiction.data)
        self.assertIn(b'href="/topics"', nonfiction.data)
        self.assertNotIn(b"Explore by topic", fiction.data)
        self.assertNotIn(b'href="/topics"', fiction.data)

    def test_deploy_smoke_check_covers_the_topic_catalog(self):
        workflow = (
            Path(app.APP_DIR) / ".github" / "workflows" / "deploy.yml"
        ).read_text()

        self.assertIn("https://libflix.fomalhaut.app/topics", workflow)
        self.assertIn("grep -q 'Explore topics'", workflow)


if __name__ == "__main__":
    unittest.main()
