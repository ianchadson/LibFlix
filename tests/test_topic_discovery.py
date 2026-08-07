import unittest
from dataclasses import replace

from topic_discovery import (
    DiscoveryCandidate,
    DiscoveryResult,
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

    def test_parenting_expands_to_child_development(self):
        self.assertIn("child development", plan_topic_query("parenting").queries)

    def test_productivity_expands_to_getting_things_done(self):
        self.assertIn(
            "getting things done",
            plan_topic_query("productivity").queries,
        )


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

    def test_focus_rejects_unrelated_subject_senses(self):
        records = [
            self.ol_record(
                "/works/OL1W", "Using Focus Groups", "Researcher",
                ["Focus groups", "Qualitative research"],
            ),
            self.ol_record(
                "/works/OL2W", "Ford Focus Repair Manual", "Mechanic",
                ["Focus automobile", "Maintenance and repair"],
            ),
            self.ol_record(
                "/works/OL3W", "The Grammar of Focus", "Linguist",
                ["Focus (Linguistics)", "Pragmatics"],
            ),
            self.ol_record(
                "/works/OL4W", "My One Word", "Helpful Author",
                ["Attention", "Focus (Linguistics)", "Self-help"],
            ),
            self.ol_record(
                "/works/OL5W", "Focus on Today", "Useful Author",
                ["Focus", "Productivity"],
            ),
            self.ol_record(
                "/works/OL6W", "Breaking Boundaries", "Theatre Historian",
                ["Focus Theatre (Dublin, Ireland)"],
            ),
            self.ol_record(
                "/works/OL7W", "Family Man", "Organization Historian",
                ["Focus on the Family (Organization)"],
            ),
            self.ol_record(
                "/works/OL8W", "Creating Drop-in Centers", "Social Worker",
                ["Family Focus, Inc."],
            ),
        ]
        page = parse_openlibrary_payload({"docs": records}, "focus")
        results = merge_topic_candidates([page], plan_topic_query("focus"))
        titles = [result.candidate.title for result in results]

        self.assertIn("My One Word", titles)
        self.assertIn("Focus on Today", titles)
        self.assertNotIn("Using Focus Groups", titles)
        self.assertNotIn("Ford Focus Repair Manual", titles)
        self.assertNotIn("The Grammar of Focus", titles)
        self.assertNotIn("Breaking Boundaries", titles)
        self.assertNotIn("Family Man", titles)
        self.assertNotIn("Creating Drop-in Centers", titles)

    def test_productivity_rejects_industrial_engineering_senses(self):
        records = [
            self.ol_record(
                "/works/OL1W", "Well Productivity Handbook", "Engineer",
                ["Petroleum engineering", "Oil wells"],
            ),
            self.ol_record(
                "/works/OL2W", "MOST Work Measurement Systems", "Engineer",
                ["Work measurement", "Industrial engineering", "Productivity"],
            ),
            self.ol_record(
                "/works/OL3W", "Personal Productivity", "Useful Author",
                ["Time management", "Personal efficiency"],
            ),
        ]

        results = merge_topic_candidates(
            [parse_openlibrary_payload({"docs": records}, "productivity")],
            plan_topic_query("productivity"),
        )
        titles = [result.candidate.title for result in results]

        self.assertEqual(titles, ["Personal Productivity"])

    def test_communication_rejects_nonhuman_and_clinical_senses(self):
        records = [
            self.ol_record(
                "/works/OL1W", "Intelligent Life in the Universe", "Astronomer",
                ["Interstellar communication", "Astronomy"],
            ),
            self.ol_record(
                "/works/OL2W", "Clinical Language", "Clinician",
                ["Communication disorders", "Neurology"],
            ),
            self.ol_record(
                "/works/OL3W", "How to Talk", "Useful Author",
                ["Interpersonal communication", "Conversation"],
            ),
        ]

        results = merge_topic_candidates(
            [parse_openlibrary_payload({"docs": records}, "communication")],
            plan_topic_query("communication"),
        )

        self.assertEqual(
            [result.candidate.title for result in results],
            ["How to Talk"],
        )

    def test_startups_rejects_children_activity_books(self):
        records = [
            self.ol_record(
                "/works/OL1W", "50 Money-Making Ideas for Kids", "Author",
                ["Entrepreneurship", "Juvenile literature"],
            ),
            self.ol_record(
                "/works/OL2W", "The Startup Owner's Manual", "Founder",
                ["Startups", "Entrepreneurship"],
            ),
        ]
        results = merge_topic_candidates(
            [parse_openlibrary_payload({"docs": records}, "startups")],
            plan_topic_query("startups"),
        )

        self.assertEqual(
            [result.candidate.title for result in results],
            ["The Startup Owner's Manual"],
        )

    def test_investing_rejects_legal_and_industrial_subject_collisions(self):
        records = [
            self.ol_record(
                "/works/OL1W", "No Choirboy", "Author",
                ["Capital punishment", "Death row inmates", "Capital investments"],
            ),
            self.ol_record(
                "/works/OL2W", "Advances in Management Research", "Editor",
                ["Operations research", "Investments", "Manufacturing"],
            ),
            self.ol_record(
                "/works/OL3W", "Investing for the Long Term", "Benjamin Graham",
                ["Investments", "Investment strategy", "Stocks", "Bonds"],
            ),
        ]
        results = merge_topic_candidates(
            [parse_openlibrary_payload({"docs": records}, "investing")],
            plan_topic_query("investing"),
        )

        self.assertEqual(
            [result.candidate.title for result in results],
            ["Investing for the Long Term"],
        )

    def test_ambiguous_topic_titles_do_not_defeat_intent(self):
        cases = (
            ("habits", "Habits of the Heart", ["Sociology"]),
            (
                "mental health",
                "Eligible for Execution",
                ["Mental health", "Capital punishment", "Insanity (Law)"],
            ),
            (
                "writing",
                "Writing Women in Late Medieval and Early Modern Spain",
                [],
            ),
            (
                "health",
                "Health Sciences Information Sources",
                [],
            ),
        )
        for index, (topic, title, subjects) in enumerate(cases, start=1):
            with self.subTest(topic=topic, title=title):
                page = parse_openlibrary_payload({"docs": [self.ol_record(
                    f"/works/OL{index}W",
                    title,
                    "Author",
                    subjects,
                )]}, topic)

                self.assertEqual(
                    merge_topic_candidates([page], plan_topic_query(topic)),
                    [],
                )

    def test_duplicate_work_records_do_not_repeat_a_title_and_author(self):
        records = [
            self.ol_record(
                "/works/OL1W", "Focus", "Daniel Goleman", ["Focus"],
                readinglog_count=500,
            ),
            self.ol_record(
                "/works/OL2W", "Focus", "Daniel Goleman", ["Attention"],
                readinglog_count=100,
            ),
        ]

        results = merge_topic_candidates(
            [parse_openlibrary_payload({"docs": records}, "focus")],
            plan_topic_query("focus"),
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].candidate.title, "Focus")

    def test_duplicate_titles_ignore_a_leading_article(self):
        records = [
            self.ol_record(
                "/works/OL1W",
                "The 7 Habits of Highly Effective Teens",
                "Sean Covey",
                ["Habits"],
            ),
            self.ol_record(
                "/works/OL2W",
                "7 Habits of Highly Effective Teens",
                "Sean Covey",
                ["Habits"],
            ),
        ]

        results = merge_topic_candidates(
            [parse_openlibrary_payload({"docs": records}, "habits")],
            plan_topic_query("habits"),
        )

        self.assertEqual(len(results), 1)

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

    def test_subject_does_not_reverse_match_a_longer_expansion(self):
        page = parse_openlibrary_payload({"docs": [self.ol_record(
            "/works/OL3W",
            "Contract Pricing",
            "Government Office",
            ["Management"],
        )]}, "time management", 1)

        results = merge_topic_candidates(
            [page],
            plan_topic_query("productivity"),
        )

        self.assertEqual(results, [])

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
        self.assertEqual(book["description"], "2016 book by Cal Newport")
        self.assertIn("Matched by multiple sources", book["reasons"])

    def test_result_description_is_bounded_for_topic_cards(self):
        description = "A focused summary. " * 100
        result = DiscoveryResult(
            candidate=DiscoveryCandidate(
                provider="openlibrary",
                provider_id="/works/OL2W",
                native_rank=0,
                query_rank=0,
                title="Deep Work",
                work_key="/works/OL2W",
                description=description,
            ),
            score=1,
            reasons=("Subject: Attention",),
            sources=("openlibrary",),
        )

        book = candidate_to_book(result)

        self.assertTrue(book["description"].startswith("A focused summary."))
        self.assertLessEqual(len(book["description"]), 2_000)

    def test_description_repairs_joined_source_paragraphs(self):
        page = parse_openlibrary_payload({"docs": [{
            "key": "/works/OL2W",
            "title": "Rapt",
            "author_name": ["Winifred Gallagher"],
            "description": "the interested lifeIn Rapt. attention.Gallagher explains.",
        }]}, "focus", 0)

        self.assertEqual(
            page.candidates[0].description,
            "the interested life In Rapt. attention. Gallagher explains.",
        )

    def test_heavily_joined_description_is_rejected(self):
        page = parse_openlibrary_payload({"docs": [{
            "key": "/works/OL2W",
            "title": "Rapt",
            "author_name": ["Winifred Gallagher"],
            "description": "lifeIn Rapt questionsCan we focus?driving onward.attention.Gallagher explains.",
        }]}, "focus", 0)

        self.assertEqual(page.candidates[0].description, "")

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

    def test_nonfiction_filter_recognizes_subjects_ending_in_fiction(self):
        page = parse_openlibrary_payload({"docs": [self.ol_record(
            "/works/OL9W",
            "From Head to Toe",
            "Eric Carle",
            ["Physical fitness", "Animals, fiction", "Children's fiction"],
        )]}, "fitness")
        results = merge_topic_candidates([page], plan_topic_query("fitness"))

        self.assertEqual(
            filter_topic_results(results, book_type="nonfiction"),
            [],
        )

    def test_novel_is_exact_and_does_not_hide_novelty_or_coronavirus(self):
        records = [
            self.ol_record(
                "/works/OL1W",
                "Coronavirus Medicine",
                "Doctor",
                ["Novel coronavirus infections", "Medicine", "Nonfiction"],
            ),
            self.ol_record(
                "/works/OL2W",
                "The Psychology of Novelty",
                "Psychologist",
                ["Novelty (Psychology)", "Psychology", "Nonfiction"],
            ),
            self.ol_record(
                "/works/OL3W",
                "A Fictional Health Story",
                "Novelist",
                ["Health", "Novel"],
            ),
        ]
        self.assertEqual(
            [
                candidate.fiction
                for candidate in parse_openlibrary_payload(
                    {"docs": records},
                    "health",
                ).candidates
            ],
            [False, False, True],
        )

    def test_nonfiction_filter_rejects_untyped_inventaire_text_matches(self):
        raw = DiscoveryResult(
            candidate=DiscoveryCandidate(
                provider="inventaire",
                provider_id="wd:novel",
                native_rank=1,
                query_rank=0,
                title="Doctor Sleep",
                work_key="/works/OL1W",
            ),
            score=1,
            reasons=("Related: Sleep",),
            sources=("inventaire",),
        )
        semantic = DiscoveryResult(
            candidate=replace(
                raw.candidate,
                provider_id="wd:mindfulness",
                title="Mindfulness in Plain English",
                work_key="/works/OL2W",
                semantic_terms=("meditation",),
            ),
            score=1,
            reasons=("Related: Meditation",),
            sources=("inventaire",),
        )

        filtered = filter_topic_results([raw, semantic], book_type="nonfiction")

        self.assertEqual([item.candidate.title for item in filtered], [
            "Mindfulness in Plain English",
        ])

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
