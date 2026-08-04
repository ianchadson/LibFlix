import unittest

from topic_discovery import (
    TOPIC_EXPANSIONS,
    build_inventaire_request,
    build_openlibrary_request,
    candidate_to_book,
    filter_topic_results,
    merge_topic_candidates,
    parse_inventaire_payload,
    parse_openlibrary_payload,
    plan_topic_query,
)


class TopicIntentTests(unittest.TestCase):
    def test_auto_intent_preserves_identity_and_detects_broad_topics(self):
        for query in ("9781455586691", "OL17713267W", '"Deep Work"', "Deep Work by Cal Newport"):
            with self.subTest(query=query):
                self.assertEqual(plan_topic_query(query).intent, "identity")
        for query in ("focus", "meditation", "startups", "mental health", "artificial intelligence"):
            with self.subTest(query=query):
                self.assertEqual(plan_topic_query(query).intent, "topic")

    def test_explicit_intent_override_always_wins(self):
        self.assertEqual(plan_topic_query("focus", "identity").intent, "identity")
        self.assertEqual(plan_topic_query("The Age of AI", "topic").intent, "topic")

    def test_topic_corpus_is_versioned_bounded_and_has_thirty_subjects(self):
        self.assertGreaterEqual(len(TOPIC_EXPANSIONS), 30)
        for topic in TOPIC_EXPANSIONS:
            with self.subTest(topic=topic):
                plan = plan_topic_query(topic)
                self.assertEqual(plan.intent, "topic")
                self.assertGreaterEqual(len(plan.queries), 1)
                self.assertLessEqual(len(plan.queries), 3)
                self.assertTrue(plan.expansion_version)

    def test_provider_requests_are_bounded_and_do_not_embed_query_grammar(self):
        plan = plan_topic_query('books about focus:*) OR (*:*', "topic")
        path, params = build_openlibrary_request(plan, 0, limit=1000)
        self.assertEqual(path, "/search.json")
        self.assertLessEqual(params["limit"], 100)
        self.assertNotIn(":", params["subject"])
        inv_path, inv_params, _ = build_inventaire_request(plan, 0, limit=1000)
        self.assertEqual(inv_path, "/search")
        self.assertLessEqual(int(dict(inv_params)["limit"]), 40)

    def test_any_language_does_not_add_an_upstream_language_filter(self):
        plan = plan_topic_query("focus", "topic")
        _, ol_params = build_openlibrary_request(plan, 0, language="any")
        _, inv_params, _ = build_inventaire_request(plan, 0, language="any")

        self.assertNotIn("lang", ol_params)
        self.assertNotIn("lang", dict(inv_params))

    def test_singular_topic_is_preserved_before_aliases(self):
        plan = plan_topic_query("startup", "topic")

        self.assertEqual(plan.queries[0], "startup")
        self.assertIn("startups", plan.queries)


class TopicProviderParsingTests(unittest.TestCase):
    def test_openlibrary_parser_bounds_fields_and_keeps_work_identity(self):
        page = parse_openlibrary_payload({"docs": [{
            "key": "/works/OL17713267W",
            "title": "Deep Work",
            "author_name": ["Cal Newport"],
            "cover_i": 7988607,
            "language": ["eng"],
            "subject": ["Attention", "Distraction (Psychology)"],
            "first_publish_year": 2016,
            "ratings_count": 191,
            "ratings_average": 3.84,
            "readinglog_count": 4350,
            "osp_count": 70,
            "edition_count": 40,
        }]}, "attention")
        self.assertTrue(page.available)
        self.assertEqual(page.candidates[0].work_key, "/works/OL17713267W")
        self.assertEqual(page.candidates[0].cover_id, 7988607)

    def test_inventaire_accepts_work_p648_and_rejects_edition_or_unresolved(self):
        payload = {"results": [
            {
                "uri": "wd:Q54408847",
                "label": "Deep Work",
                "description": "2016 book by Cal Newport",
                "claims": {
                    "wdt:P648": ["OL17713267W"],
                    "wdt:P407": ["wd:Q1860"],
                },
                "_popularity": 28,
            },
            {
                "uri": "inv:edition",
                "label": "Unsafe edition",
                "claims": {"wdt:P648": ["OL123M"]},
            },
            {"uri": "inv:none", "label": "Unresolved", "claims": {}},
        ]}
        page = parse_inventaire_payload(payload, "deep work", semantic=True)
        self.assertEqual(len(page.candidates), 1)
        candidate = page.candidates[0]
        self.assertEqual(candidate.work_key, "/works/OL17713267W")
        self.assertEqual(candidate.authors, ("Cal Newport",))
        self.assertEqual(candidate.languages, ("eng",))
        self.assertEqual(candidate.description, "2016 book by Cal Newport")

    def test_inventaire_preserves_known_non_supported_language_as_other(self):
        page = parse_inventaire_payload({"results": [{
            "uri": "wd:Q2",
            "label": "German Meditation",
            "description": "book by Example Author",
            "claims": {
                "wdt:P648": ["OL2W"],
                "wdt:P407": ["wd:Q188"],
            },
        }]}, "meditation")

        self.assertEqual(page.candidates[0].languages, ("other",))
        results = merge_topic_candidates(
            [page],
            plan_topic_query("meditation"),
        )
        self.assertEqual(
            filter_topic_results(results, language="current", current_language="en"),
            [],
        )


class TopicRankingTests(unittest.TestCase):
    @staticmethod
    def ol_record(key, title, author, subjects, **metrics):
        return {
            "key": key,
            "title": title,
            "author_name": [author],
            "language": ["eng"],
            "subject": subjects,
            "cover_i": metrics.pop("cover_i", 100),
            **metrics,
        }

    def test_focus_quality_ranking_rejects_concentration_camp_false_positive(self):
        records = [
            self.ol_record(
                "/works/OL1W", "The Hiding Place", "Corrie ten Boom",
                ["Concentration camps", "World War, 1939-1945"],
                ratings_count=5000, ratings_average=4.8, readinglog_count=10000,
            ),
            self.ol_record(
                "/works/OL2W", "Deep Work", "Cal Newport",
                ["Attention", "Distraction", "Productivity"],
                ratings_count=191, ratings_average=3.84, readinglog_count=4350,
            ),
            self.ol_record(
                "/works/OL3W", "Stolen Focus", "Johann Hari",
                ["Attention", "Distraction (Psychology)"],
                ratings_count=13, ratings_average=4.07, readinglog_count=303,
            ),
            self.ol_record(
                "/works/OL4W", "Popular but unrelated", "Filler Author",
                ["Cooking"], ratings_count=9000, ratings_average=5, readinglog_count=20000,
            ),
        ]
        page = parse_openlibrary_payload({"docs": records}, "attention")
        results = merge_topic_candidates([page], plan_topic_query("focus"))
        titles = [result.candidate.title for result in results]
        self.assertEqual(titles[:2], ["Deep Work", "Stolen Focus"])
        self.assertNotIn("The Hiding Place", titles)
        self.assertNotIn("Popular but unrelated", titles)

    def test_short_ai_alias_matches_tokens_not_substrings(self):
        records = [
            self.ol_record(
                "/works/OL1W", "Sailing the Pacific", "Popular Author",
                ["Travel"], ratings_count=9000, ratings_average=5,
                readinglog_count=20000,
            ),
            self.ol_record(
                "/works/OL2W", "Artificial Intelligence", "Real Author",
                ["Artificial intelligence"], readinglog_count=5,
            ),
        ]
        page = parse_openlibrary_payload({"docs": records}, "ai")
        results = merge_topic_candidates([page], plan_topic_query("ai"))

        self.assertEqual(
            [result.candidate.title for result in results],
            ["Artificial Intelligence"],
        )

    def test_cjk_topic_matches_unsegmented_title(self):
        record = self.ol_record(
            "/works/OL8W",
            "冥想入门",
            "测试作者",
            ["心理学"],
        )
        page = parse_openlibrary_payload({"docs": [record]}, "冥想")
        results = merge_topic_candidates(
            [page],
            plan_topic_query("冥想", "topic"),
        )

        self.assertEqual([item.candidate.title for item in results], ["冥想入门"])

    def test_cross_source_identity_fusion_and_reasons(self):
        ol = parse_openlibrary_payload({"docs": [self.ol_record(
            "/works/OL2W", "Deep Work", "Cal Newport", ["Attention"],
            readinglog_count=4350,
        )]}, "attention")
        inv = parse_inventaire_payload({"results": [{
            "uri": "wd:deep",
            "label": "Deep Work",
            "description": "2016 book by Cal Newport",
            "claims": {"wdt:P648": ["OL2W"]},
            "_popularity": 28,
        }]}, "attention", semantic=True)
        results = merge_topic_candidates([ol, inv], plan_topic_query("focus"))
        self.assertEqual(len(results), 1)
        book = candidate_to_book(results[0])
        self.assertEqual(book["ol_key"], "/works/OL2W")
        self.assertIn("Matched by multiple sources", book["reasons"])

    def test_filters_and_newest_sort_are_applied_after_relevance(self):
        records = [
            self.ol_record("/works/OL1W", "Modern Meditation", "A", ["Meditation", "Nonfiction"], first_publish_year=2022),
            self.ol_record("/works/OL2W", "Classic Meditation", "B", ["Meditation", "Nonfiction"], first_publish_year=1970),
            self.ol_record("/works/OL3W", "Meditation Novel", "C", ["Meditation", "Fiction"], first_publish_year=2024),
        ]
        results = merge_topic_candidates(
            [parse_openlibrary_payload({"docs": records}, "meditation")],
            plan_topic_query("meditation"),
        )
        recent = filter_topic_results(results, book_type="nonfiction", published="recent", sort="newest")
        self.assertEqual([item.candidate.title for item in recent], ["Modern Meditation"])
        classic = filter_topic_results(results, published="classic")
        self.assertEqual([item.candidate.title for item in classic], ["Classic Meditation"])

    def test_filters_run_before_author_diversity_cap(self):
        records = [
            self.ol_record(
                "/works/OL1W", "Meditation 2000", "Same Author", ["Meditation"],
                first_publish_year=2000, readinglog_count=300,
            ),
            self.ol_record(
                "/works/OL2W", "Meditation 2001", "Same Author", ["Meditation"],
                first_publish_year=2001, readinglog_count=200,
            ),
            self.ol_record(
                "/works/OL3W", "Meditation 2025", "Same Author", ["Meditation"],
                first_publish_year=2025, readinglog_count=100,
            ),
        ]
        uncapped = merge_topic_candidates(
            [parse_openlibrary_payload({"docs": records}, "meditation")],
            plan_topic_query("meditation"),
            author_cap=200,
        )
        recent = filter_topic_results(
            uncapped,
            published="recent",
            author_cap=2,
        )

        self.assertEqual([item.candidate.title for item in recent], ["Meditation 2025"])

    def test_author_cap_and_stable_order(self):
        records = [
            self.ol_record(f"/works/OL{i}W", f"Mindfulness {i}", "Same Author", ["Mindfulness"], readinglog_count=100 - i)
            for i in range(1, 5)
        ] + [
            self.ol_record("/works/OL9W", "Mindfulness Elsewhere", "Other Author", ["Mindfulness"], readinglog_count=1)
        ]
        page = parse_openlibrary_payload({"docs": records}, "mindfulness")
        first = merge_topic_candidates([page], plan_topic_query("meditation"), author_cap=2)
        second = merge_topic_candidates([page], plan_topic_query("meditation"), author_cap=2)
        self.assertEqual(
            [item.candidate.work_key for item in first],
            [item.candidate.work_key for item in second],
        )
        self.assertEqual(sum(item.candidate.authors == ("Same Author",) for item in first), 2)


if __name__ == "__main__":
    unittest.main()
