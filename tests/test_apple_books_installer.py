import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class AppleBooksInstallerTests(unittest.TestCase):
    def test_missing_installer_is_not_a_successful_download(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(app, 'APPLE_BOOKS_SHORTCUT_PATH', str(Path(directory) / 'missing.shortcut')):
            response = app.app.test_client().get('/apple-books-shortcut')
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('attachment', response.headers.get('Content-Disposition', ''))
