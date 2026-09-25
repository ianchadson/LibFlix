import unittest
from unittest.mock import patch

import app
from publisher_catalog import supplement_search


class PublisherSearchTests(unittest.TestCase):
    def test_topic_search_never_uses_publisher_supplement(self):
        payload = {'intent': 'topic', 'topic_mode': True, 'display_query': 'poker',
                   'all_books': [], 'partial': False, 'sources': [],
                   'source_unavailable': False, 'filters': dict(app.TOPIC_FILTER_DEFAULTS)}
        with app.app.test_client() as client, \
                patch.object(app, 'fetch_topic_discovery_payload', return_value=payload), \
                patch.object(app, 'supplement_search') as supplement:
            response = client.get('/api/discover?q=Beneath+the+Cards&intent=topic')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['books'], [])
            supplement.assert_not_called()

    def test_cover_proxy_only_serves_reviewed_records(self):
        with app.app.test_client() as client, patch.object(app, 'cached_cover_response', return_value='cover') as serve:
            self.assertEqual(client.get('/publishercover/9798895658154').status_code, 200)
            self.assertEqual(serve.call_args.args[:3], ('publisher', '9798895658154', 'M'))
            serve.reset_mock()
            self.assertEqual(client.get('/publishercover/123').status_code, 404)
            serve.assert_not_called()

    def test_precise_title_author_and_both_editions(self):
        for query in ('"Beneath the Cards"', 'Garrett Adelstein',
                      'Beneath the Cards by Garrett Adelstein',
                      '979-8895658147', 'ISBN: 9798895658154'):
            with self.subTest(query=query):
                books, total, pages = supplement_search(query, [], 0, 0)
                self.assertEqual(total, 1)
                self.assertEqual(pages, 1)
                self.assertFalse(books[0]['download_available'])
                self.assertNotIn('ol_key', books[0])

    def test_no_broad_matches_or_later_pages_or_wrong_language(self):
        for query, page, lang in [('poker', 1, 'en'), ('cards', 1, 'en'),
                                  ('Beneath the Cards', 2, 'en'),
                                  ('Beneath the Cards', 1, 'cn')]:
            self.assertEqual(supplement_search(query, [], 0, 0, page, lang)[0], [])

    def test_prefers_canonical_match_without_mutating_cache(self):
        books = [{'title': 'Beneath the Cards: A Poker Journey',
                  'author': 'Garrett Adelstein', 'ol_key': '/works/OL123W'}]
        self.assertEqual(supplement_search('Beneath the Cards', books, 1, 1)[0], books)
        empty = []
        supplement_search('Beneath the Cards', empty, 0, 0)
        self.assertEqual(empty, [])

    def test_identity_api_and_cached_html(self):
        with app.app.test_client() as client, \
                patch.object(app, 'fetch_discovery_books', return_value=([], 0, 0)):
            response = client.get('/api/discover?q=Beneath+the+Cards&intent=identity')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['books'][0]['source'], 'publisher')
        with app.app.test_client() as client, \
                patch.object(app, 'cached_discovery_books', return_value=([], 0, 0)):
            html = client.get('/discover?q=Beneath+the+Cards&intent=identity').get_data(as_text=True)
            self.assertIn('class="download-unavailable"', html)
            self.assertIn('View publisher.', html)
            self.assertIn('data-isbn="9798895658154"', html)

    def test_known_metadata_survives_source_outage(self):
        books, total, _ = supplement_search('Beneath the Cards', [], None, 0)
        self.assertEqual(total, 1)
        self.assertEqual(len(books), 1)
        self.assertIsNone(supplement_search('Unknown', [], None, 0)[1])
