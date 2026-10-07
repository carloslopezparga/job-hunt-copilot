"""Exercise the real HTTP code path against a local server (no internet required)."""

from __future__ import annotations

import http.server
import threading
from functools import partial

import pytest

from jobhunt.models import Company
from jobhunt.sources import GreenhouseSource, HttpFetcher, SourceError


@pytest.fixture
def server(fixtures_dir):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):  # keep test output clean
            pass

    handler = partial(Handler, directory=str(fixtures_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class LocalGreenhouse(GreenhouseSource):
    base = ""

    def board_url(self, company):
        return f"{self.base}/greenhouse_{company.slug}.json"


def test_http_fetcher_downloads_and_parses(server):
    source = LocalGreenhouse(HttpFetcher(timeout=5, retries=0, delay=0))
    source.base = server
    jobs = source.fetch(Company("Northwind Quant", "greenhouse", "northwindquant", "quant_trading"))
    assert len(jobs) == 5


def test_http_fetcher_404_is_source_error(server):
    fetcher = HttpFetcher(timeout=5, retries=0, delay=0)
    with pytest.raises(SourceError, match="404"):
        fetcher.get_json(f"{server}/missing.json", key="x")
