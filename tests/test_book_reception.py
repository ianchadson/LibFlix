import unittest
from pathlib import Path
from unittest.mock import patch

import app


SEARCH_HTML = """
<table>
  <tr itemtype="http://schema.org/Book">
    <td><a class="bookTitle" href="/book/show/10-project-hail-mary-summary">Project Hail Mary Summary</a></td>
    <td><a class="authorName">Editorial Companion</a></td>
  </tr>
  <tr itemtype="http://schema.org/Book">
    <td><a class="bookTitle" href="/book/show/20-project-hail-mary">Project Hail Mary</a></td>
    <td><a class="authorName">Andy Weir</a></td>
  </tr>
</table>
"""


BOOK_HTML = """
<html>
  <head>
    <script type="application/ld+json">
      {
        "@type": "Book",
        "name": "Project Hail Mary",
        "author": [{"@type": "Person", "name": "Andy Weir"}],
        "aggregateRating": {
          "ratingValue": "4.51",
          "ratingCount": "1,840,371",
          "reviewCount": "255,813"
        }
      }
    </script>
  </head>
  <body>
    <article class="ReviewCard">
      <div class="ReviewerProfile__name"><a>Careful Reader</a></div>
      <div class="RatingStars" aria-label="Rating 5 out of 5"></div>
      <div class="ReviewText__content">
        A thoughtful science-fiction adventure with a warm friendship at its
        centre and enough momentum to make a long book feel unexpectedly quick.
        <button>more</button>
      </div>
      <a href="/review/show/123">Read review</a>
    </article>
    <article class="ReviewCard">
      <div class="ReviewerProfile__name"><a>Spoiler Reader</a></div>
      <div class="ReviewText__content">
        <span class="SpoilerWarning">Spoiler</span>
        The ending reveals everything that should remain hidden.
      </div>
      <a href="/review/show/456">Read review</a>
    </article>
  </body>
</html>
"""


BOOKMARKS_HTML = """
<html>
  <h1 class="book_detail_title">Project Hail Mary</h1>
  <div class="book_detail_author"><span itemprop="name">Andy Weir</span></div>
  <span itemprop="review">
    <div class="bookmarks_pullquote_reviewer">
      <span class="review_rating rave">Rave</span>
      <span itemprop="author"><span itemprop="name">A. Critic</span></span>
      <a class="bookmarks_source_link">The Daily Review</a>
    </div>
    <div class="bookmarks_a_review_pullquote" itemprop="reviewBody">
      A propulsive and generous science-fiction adventure whose technical detail
      never obscures its unexpectedly warm emotional core.
    </div>
  </span>
</html>
"""


class GoodreadsParserTests(unittest.TestCase):
    def test_search_prefers_exact_canonical_title_and_author(self):
        result = app.parse_goodreads_search(
            SEARCH_HTML,
            "Project Hail Mary",
            "Andy Weir",
        )

        self.assertEqual(result["title"], "Project Hail Mary")
        self.assertEqual(
            result["url"],
            "https://www.goodreads.com/book/show/20-project-hail-mary",
        )

    def test_book_parser_returns_bounded_public_excerpt_and_aggregate(self):
        result = app.parse_goodreads_book(
            BOOK_HTML,
            "Project Hail Mary",
            "Andy Weir",
            "https://www.goodreads.com/book/show/20-project-hail-mary",
        )

        self.assertEqual(result["average"], 4.51)
        self.assertEqual(result["ratings_count"], 1840371)
        self.assertEqual(result["reviews_count"], 255813)
        self.assertEqual(len(result["reviews"]), 1)
        self.assertEqual(result["reviews"][0]["reviewer"], "Careful Reader")
        self.assertEqual(result["reviews"][0]["rating"], 5)
        self.assertNotIn("more", result["reviews"][0]["excerpt"])
        self.assertLessEqual(
            len(result["reviews"][0]["excerpt"]),
            app.GOODREADS_REVIEW_MAX_CHARS + 3,
        )

    def test_book_parser_rejects_wrong_identity(self):
        self.assertIsNone(app.parse_goodreads_book(
            BOOK_HTML,
            "Artemis",
            "Andy Weir",
            "https://www.goodreads.com/book/show/20-project-hail-mary",
        ))

    def test_goodreads_urls_cannot_escape_the_expected_host(self):
        self.assertEqual(app.safe_goodreads_url("https://evil.example/book/show/1"), "")
        self.assertEqual(app.safe_goodreads_url("/user/show/1"), "")

    def test_book_marks_supplies_critic_excerpt_when_goodreads_is_blocked(self):
        result = app.parse_bookmarks_reception(
            BOOKMARKS_HTML,
            "Project Hail Mary",
            "Andy Weir",
            "https://bookmarks.reviews/reviews/project-hail-mary/",
        )

        self.assertEqual(result["source"], "Book Marks")
        self.assertEqual(result["reviews"][0]["sentiment"], "Rave")
        self.assertEqual(
            result["reviews"][0]["reviewer"],
            "A. Critic · The Daily Review",
        )


class ReceptionAssemblyTests(unittest.TestCase):
    def test_open_library_rating_is_normalized(self):
        with patch.object(app, "ol_get", return_value={
            "summary": {"average": 4.502793, "count": 179},
        }):
            rating = app.fetch_openlibrary_reception("/works/OL1W")

        self.assertEqual(rating["average"], 4.5)
        self.assertEqual(rating["ratings_count"], 179)

    def test_goodreads_is_primary_and_open_library_remains_visible(self):
        goodreads = {
            "source": "Goodreads",
            "average": 4.51,
            "ratings_count": 100,
            "reviews_count": 20,
            "url": "https://www.goodreads.com/book/show/20-project-hail-mary",
            "reviews": [{
                "reviewer": "Reader",
                "rating": 5,
                "excerpt": "A sufficiently long and useful public review excerpt for this test.",
                "url": "https://www.goodreads.com/review/show/123",
            }],
        }
        openlibrary = {
            "source": "Open Library",
            "average": 4.4,
            "ratings_count": 12,
            "reviews_count": 0,
            "url": "https://openlibrary.org/works/OL1W",
        }
        with (
            patch.object(app, "get_book_detail", return_value=({
                "title": "Project Hail Mary",
                "author": "Andy Weir",
            }, "memory")),
            patch.object(app, "fetch_goodreads_reception", return_value=goodreads),
            patch.object(app, "fetch_bookmarks_reception", return_value=None),
            patch.object(app, "fetch_openlibrary_reception", return_value=openlibrary),
        ):
            payload = app.build_book_reception("OL1W", "en")

        self.assertTrue(payload["success"])
        self.assertEqual(payload["rating"]["source"], "Goodreads")
        self.assertEqual(payload["other_ratings"][0]["source"], "Open Library")
        self.assertEqual(len(payload["reviews"]), 1)
        self.assertEqual(payload["reviews_source"]["source"], "Goodreads")

    def test_fast_stage_uses_book_marks_without_waiting_for_goodreads(self):
        book_marks = {
            "source": "Book Marks",
            "url": "https://bookmarks.reviews/reviews/project-hail-mary/",
            "reviews": [{
                "reviewer": "A. Critic · The Daily Review",
                "rating": 0,
                "sentiment": "Rave",
                "excerpt": "A sufficiently long and useful public review excerpt for this test.",
                "url": "https://bookmarks.reviews/reviews/project-hail-mary/",
            }],
        }
        openlibrary = {
            "source": "Open Library",
            "average": 4.4,
            "ratings_count": 12,
            "reviews_count": 0,
            "url": "https://openlibrary.org/works/OL1W",
        }
        with (
            patch.object(app, "get_book_detail", return_value=({
                "title": "Project Hail Mary",
                "author": "Andy Weir",
            }, "memory")),
            patch.object(app, "fetch_goodreads_reception") as goodreads,
            patch.object(app, "fetch_bookmarks_reception", return_value=book_marks),
            patch.object(app, "fetch_openlibrary_reception", return_value=openlibrary),
        ):
            payload = app.build_book_reception(
                "OL1W",
                "en",
                include_goodreads=False,
            )

        goodreads.assert_not_called()
        self.assertEqual(payload["rating"]["source"], "Open Library")
        self.assertEqual(payload["reviews_source"]["source"], "Book Marks")


class ReceptionEndpointTests(unittest.TestCase):
    def test_slow_goodreads_queue_is_bounded_separately(self):
        with (
            patch.object(app, "BOOK_RECEPTION_ENRICHING", {"existing"}),
            patch.object(app, "BOOK_RECEPTION_ENRICH_PENDING_LIMIT", 1),
            patch.object(app.BOOK_RECEPTION_ENRICH_EXECUTOR, "submit") as submit,
        ):
            scheduled = app.schedule_book_reception_enrichment(
                "OL1W",
                "en",
                "new",
            )

        self.assertFalse(scheduled)
        submit.assert_not_called()

    def test_endpoint_returns_cached_reception_without_refresh(self):
        cached = {
            "success": True,
            "rating": {
                "source": "Open Library",
                "average": 4.2,
                "ratings_count": 9,
                "url": "https://openlibrary.org/works/OL1W",
            },
            "other_ratings": [],
            "reviews": [],
        }
        with (
            patch.object(app, "cached_book_reception", return_value=(cached, "memory")),
            patch.object(app, "schedule_book_reception_refresh") as refresh,
        ):
            response = app.app.test_client().get(
                "/api/book-reception?ol_key=/works/OL1W&book_lang=en"
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertFalse(response.get_json()["refreshing"])
        refresh.assert_not_called()

    def test_cold_endpoint_schedules_background_fetch(self):
        with (
            patch.object(app, "cached_book_reception", return_value=(None, "miss")),
            patch.object(app, "cache_get", return_value=None),
            patch.object(app, "schedule_book_reception_refresh", return_value=True),
            patch.object(app, "BOOK_RECEPTION_REFRESHING", {
                app.book_reception_cache_key("OL1W", "en")
            }),
        ):
            response = app.app.test_client().get(
                "/api/book-reception?ol_key=/works/OL1W&book_lang=en"
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["refreshing"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_endpoint_rejects_non_work_keys(self):
        response = app.app.test_client().get(
            "/api/book-reception?ol_key=https://evil.example/book"
        )

        self.assertEqual(response.status_code, 400)


class ReceptionTemplateTests(unittest.TestCase):
    def test_book_page_loads_reviews_lazily_without_external_scripts(self):
        template = (Path(app.APP_DIR) / "templates" / "book.html").read_text()

        self.assertIn('id="reviewsSection"', template)
        self.assertIn("/api/book-reception?", template)
        self.assertIn("rootMargin: '700px 0px'", template)
        self.assertNotIn("goodreads.com/api", template)
        self.assertNotIn("<script src=\"https://www.goodreads.com", template)

    def test_review_shelf_scrolls_and_every_card_has_a_rating_cue(self):
        template = (Path(app.APP_DIR) / "templates" / "book.html").read_text()
        stylesheet = (Path(app.APP_DIR) / "static" / "libflix.css").read_text()

        self.assertIn('aria-label="Reader review excerpts"', template)
        self.assertIn('tabindex="0"', template)
        self.assertIn("function reviewRatingCue(review, reviewSourceName)", template)
        self.assertIn("reviewRatingCue(review, reviewSourceName)", template)
        self.assertIn("grid-auto-flow: column", stylesheet)
        self.assertIn("overflow-x: auto", stylesheet)
        self.assertIn("scroll-snap-type: inline proximity", stylesheet)

    def test_review_loader_is_compact_and_does_not_assume_a_card_count(self):
        template = (Path(app.APP_DIR) / "templates" / "book.html").read_text()
        stylesheet = (Path(app.APP_DIR) / "static" / "libflix.css").read_text()

        self.assertIn('class="reviews-loading-spinner"', template)
        self.assertIn("Loading reviews", template)
        self.assertNotIn("review-skeletons", template)
        self.assertNotIn("reviews-summary-skeleton", template)
        self.assertIn(".reviews-loading-spinner", stylesheet)
        self.assertNotIn(".review-skeletons", stylesheet)


if __name__ == "__main__":
    unittest.main()
