"""Download source abstractions for LibFlix.

Each downloader implements a common interface so the Flask routes can be
source-agnostic — adding a new download source (Anna's Archive, Z-Library,
etc.) only requires adding a new module here and registering it in
``get_downloader``.
"""

from downloaders.base import Book, Downloader
from downloaders.libgen import LibgenDownloader


def get_downloader(name: str = "libgen") -> Downloader:
    """Return the named download source (default: libgen)."""
    sources = {"libgen": LibgenDownloader}
    cls = sources.get(name, LibgenDownloader)
    return cls()


# Active downloader used by the Flask app routes.
DOWNLOADER: Downloader = get_downloader()