import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

import app


class FictionGenreCatalogTests(unittest.TestCase):
    def test_catalog_is_grouped_complete_and_unique(self):
        genres = [
            genre
            for group in app.FICTION_GENRE_GROUPS
            for genre in group["genres"]
        ]
        topics = [genre["topic"] for genre in genres]

        self.assertEqual(len(app.FICTION_GENRE_GROUPS), 6)
        self.assertEqual(len(genres), 40)
        self.assertEqual(len(topics), len(set(topics)))
        self.assertIn("dark_fantasy", topics)
        self.assertIn("fiction_classics", topics)
        self.assertIn("romantasy", topics)
        self.assertIn("cozy_mystery", topics)
        self.assertTrue(set(topics).issubset(app.FICTION_TOPICS))

    def test_compound_genres_have_specific_language_aware_queries(self):
        romantasy, sort = app.shelf_query("romantasy", "en")
        romantic_comedy, _ = app.shelf_query("romantic_comedy", "en")
        psychological_horror, _ = app.shelf_query("psychological_horror", "cn")

        self.assertEqual(sort, "rating")
        self.assertIn("subject_key:romantasy", romantasy)
        self.assertIn("subject_key:fiction", romantasy)
        self.assertIn("language:eng", romantasy)
        self.assertIn("subject_key:romantic_comedy", romantic_comedy)
        self.assertIn("subject_key:psychological_horror", psychological_horror)
        self.assertIn("subject_key:psychology", psychological_horror)
        self.assertIn("language:chi", psychological_horror)

    def test_broad_home_genres_exclude_known_provider_noise(self):
        science_fiction, _ = app.shelf_query("science_fiction", "en")
        mystery, _ = app.shelf_query("mystery", "en")
        contemporary, _ = app.shelf_query("contemporary_fiction", "en")

        self.assertIn("-subject_key:fantasy", science_fiction)
        self.assertIn("subject_key:mystery_fiction", mystery)
        self.assertIn("subject_key:domestic_fiction", contemporary)
        self.assertNotIn("subject_key:literary_fiction", contemporary)

    def test_all_genres_use_exact_subject_keys(self):
        for genre in app.FICTION_GENRE_INDEX.values():
            self.assertIn("subject_key:", genre["query"], genre["topic"])
            self.assertNotIn("subject:", genre["query"], genre["topic"])

    def test_shelf_ranking_defers_derivatives_and_repeated_authors(self):
        records = [
            {"title": "A Study Guide Summary", "author_name": ["Guide Writer"]},
            {"title": "First", "author_name": ["Writer A"]},
            {"title": "Second", "author_name": ["Writer A"]},
        ] + [
            {
                "title": f"Book {index}",
                "author_name": [f"Writer Person{index}"],
            }
            for index in range(2, 14)
        ]

        ranked = app.rank_shelf_records("cozy_mystery", records)
        first_authors = [record["author_name"][0] for record in ranked[:12]]

        self.assertNotIn("A Study Guide Summary", [
            record["title"] for record in ranked[:12]
        ])
        self.assertEqual(len(first_authors), len(set(first_authors)))
        self.assertGreater(
            [record["title"] for record in ranked].index("Second"), 11
        )

    def test_home_shelves_remain_curated(self):
        shelf_topics = [topic for _, topic in app.FICTION_SHELVES_DEF]

        self.assertLess(len(shelf_topics), len(app.FICTION_GENRE_INDEX))
        self.assertIn("dark_fantasy", shelf_topics)
        self.assertIn("fiction_classics", shelf_topics)
        self.assertNotIn("cozy_mystery", shelf_topics)


class FictionGenrePageTests(unittest.TestCase):
    def test_genres_page_is_zero_fetch_and_contains_all_categories(self):
        with (
            patch.object(app, "cache_get", return_value=[]),
            patch.object(app, "disk_load_shelves", return_value=[]),
            patch.object(app, "get_shelves") as shelf_provider,
            patch.object(app, "fetch_one_shelf") as category_provider,
        ):
            response = app.app.test_client().get("/fiction/genres")

        self.assertEqual(response.status_code, 200)
        shelf_provider.assert_not_called()
        category_provider.assert_not_called()
        page = BeautifulSoup(response.data, "html.parser")
        self.assertEqual(page.select_one("h1").get_text(strip=True), "Genres")
        self.assertEqual(len(page.select(".genre-group")), 6)
        self.assertEqual(len(page.select(".genre-card")), 40)
        self.assertIsNotNone(page.select_one('input#genreSearch[type="search"]'))
        self.assertIn(b"filterGenres", response.data)
        self.assertIsNotNone(page.select_one('a[href="/fiction/category/dark_fantasy"]'))
        self.assertIsNotNone(page.select_one('a[href="/fiction/category/fiction_classics"]'))
        self.assertIsNotNone(page.select_one('.cat-tab.active[href="/fiction/genres"]'))
        self.assertFalse(page.select(".genre-group p"))

    def test_chinese_genres_route_preserves_language(self):
        with (
            patch.object(app, "cache_get", return_value=[]),
            patch.object(app, "disk_load_shelves", return_value=[]),
        ):
            response = app.app.test_client().get("/fiction/cn/genres")

        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.data, "html.parser")
        self.assertEqual(page.html.get("lang"), "zh")
        self.assertIsNotNone(
            page.select_one('a[href="/fiction/cn/category/dark_fantasy"]')
        )
        english = page.select_one('a[title="English books"]')
        self.assertIn("next=/fiction/genres", english["href"])

    def test_fiction_home_has_compact_genre_entry_point(self):
        with patch.object(app, "get_shelves", return_value=[]):
            fiction = app.app.test_client().get("/fiction")
            nonfiction = app.app.test_client().get("/")

        fiction_page = BeautifulSoup(fiction.data, "html.parser")
        self.assertEqual(len(fiction_page.select(".fiction-genre-card")), 6)
        self.assertIsNotNone(fiction_page.select_one('a[href="/fiction/genres"]'))
        self.assertIn(b">Browse genres</h2>", fiction.data)
        self.assertNotIn(b">Browse genres</h2>", nonfiction.data)

    def test_non_shelf_genre_is_a_valid_category(self):
        shelf = {"name": "Cozy Mystery", "topic": "cozy_mystery", "books": []}
        with patch.object(app, "fetch_one_shelf", return_value=shelf) as fetch:
            response = app.app.test_client().get(
                "/fiction/category/cozy_mystery"
            )

        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with(
            "Cozy Mystery", "cozy_mystery", "en", "fiction"
        )
        self.assertIn(b"Cozy Mystery", response.data)

    def test_non_shelf_genre_renders_one_progressive_batch(self):
        books = [
            {"title": f"Book {index}", "ol_key": f"/works/OL{index}W"}
            for index in range(12)
        ]
        with (
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "cache_set"),
            patch.object(app, "disk_load_shelves", return_value=[]),
            patch.object(app, "get_shelves") as shelf_provider,
            patch.object(
                app,
                "collect_unique_topic_books",
                return_value=(books, 254, 3),
            ) as collect,
        ):
            result = app.fetch_category_page_books(
                "cozy_mystery", 1, "fiction", "en"
            )

        shelf_provider.assert_not_called()
        self.assertEqual(len(result[0]), app.CATEGORY_PAGE_SIZE)
        self.assertEqual(collect.call_args.args[3], app.CATEGORY_PAGE_SIZE)
        self.assertEqual(
            collect.call_args.kwargs["search_limit"],
            app.CATEGORY_SEARCH_LIMIT,
        )
        self.assertEqual(result[2], 22)

        category_template = (
            Path(app.APP_DIR) / "templates" / "category.html"
        ).read_text()
        self.assertIn("loadMore(true)", category_template)
        self.assertIn("category-load-placeholder", category_template)
        self.assertIn("setLoadError(true)", category_template)
        self.assertIn("window.innerHeight + 24", category_template)
        self.assertIn("Math.min(24, window.innerHeight * .08)", category_template)

    def test_genre_artwork_reuses_cached_shelf_covers(self):
        shelves = [
            {
                "topic": "fantasy",
                "books": [
                    {"cover_url": f"/olcover/{cover_id}/M"}
                    for cover_id in range(1, 13)
                ],
            }
        ]

        groups = app.fiction_genre_groups_with_artwork(shelves)
        fantasy_group = groups[0]

        self.assertEqual(fantasy_group["genres"][0]["artwork"], (
            "/olcover/1/M",
            "/olcover/2/M",
        ))
        self.assertTrue(all(
            len(genre["artwork"]) == 2
            for genre in fantasy_group["genres"]
        ))


if __name__ == "__main__":
    unittest.main()
