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

    def test_each_home_has_its_mode_specific_browse_entry_point(self):
        with patch.object(app, "get_shelves", return_value=[]):
            nonfiction = app.app.test_client().get("/")
            fiction = app.app.test_client().get("/fiction")

        page = BeautifulSoup(nonfiction.data, "html.parser")
        cards = page.select(".home-topic-card")
        self.assertEqual(len(cards), 6)
        self.assertEqual(cards[0]["href"], "/topics#topicGroup1")
        self.assertEqual(cards[-1]["href"], "/topics#topicGroup6")
        self.assertTrue(all(card.select_one("svg") for card in cards))
        self.assertFalse(any("/discover" in card["href"] for card in cards))
        self.assertIn(b'id="homeTopicsTitle">Browse by topic</h2>', nonfiction.data)
        self.assertIn(b">See all <", nonfiction.data)
        self.assertNotIn(b"The reading map", nonfiction.data)
        self.assertNotIn(b"Choose a direction", nonfiction.data)
        self.assertNotIn(b"Move from a broad interest", nonfiction.data)
        self.assertIn(b'href="/topics"', nonfiction.data)
        self.assertNotIn(b'id="homeTopicsTitle">Browse by topic</h2>', fiction.data)
        self.assertNotIn(b'href="/topics"', fiction.data)
        self.assertIn(b'id="homeGenresTitle">Browse genres</h2>', fiction.data)
        self.assertIn(b'href="/fiction/genres"', fiction.data)

    def test_topic_artwork_uses_cached_shelves_without_changing_catalog(self):
        shelves = [
            {
                "topic": "self_help",
                "books": [
                    {"cover_url": f"/olcover/{cover_id}/M"}
                    for cover_id in range(1, 9)
                ],
            }
        ]

        groups = app.topic_groups_with_artwork(shelves)

        self.assertEqual(len(groups), 6)
        self.assertEqual(len(groups[0]["artwork"]), 3)
        self.assertEqual(groups[0]["topics"][0]["cover_url"], "/olcover/4/M")
        self.assertEqual(
            groups[0]["topics"][0]["artwork"],
            ("/olcover/4/M", "/olcover/5/M"),
        )
        self.assertEqual(
            [topic["query"] for group in groups for topic in group["topics"]],
            list(app.TOPIC_BROWSE_INDEX),
        )

    def test_featured_topics_use_distinct_cached_artwork_when_available(self):
        shelves = [
            {
                "topic": "self_help",
                "books": [
                    {"cover_url": f"/olcover/{cover_id}/M"}
                    for cover_id in range(1, 24)
                ],
            }
        ]

        groups = app.topic_groups_with_artwork(shelves)
        featured = app.featured_topics_with_artwork(groups)
        focus_topic = groups[0]["topics"][0]

        self.assertEqual(focus_topic["artwork"], ("/olcover/4/M", "/olcover/5/M"))
        self.assertEqual(
            focus_topic["featured_artwork"],
            ("/olcover/14/M", "/olcover/15/M"),
        )
        self.assertEqual(featured[0]["artwork"], focus_topic["featured_artwork"])
        self.assertNotEqual(featured[0]["artwork"], focus_topic["artwork"])

    def test_deploy_smoke_check_covers_the_topic_catalog(self):
        workflow = (
            Path(app.APP_DIR) / ".github" / "workflows" / "deploy.yml"
        ).read_text()

        self.assertIn("https://libflix.fomalhaut.app/topics", workflow)
        self.assertIn("grep -q 'Explore topics'", workflow)


if __name__ == "__main__":
    unittest.main()
