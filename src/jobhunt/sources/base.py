"""Common connector interface and pluggable JSON fetchers.

Connectors only call the *public, documented* job-board APIs that Greenhouse,
Lever and Ashby expose for career pages. No authentication, no scraping, no
LinkedIn. Fetching is delegated to a ``Fetcher`` so the parsing logic can be
tested offline with JSON fixtures.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol

from jobhunt.models import Company, Job
from jobhunt.textutils import contains_term, looks_like_internship, looks_remote

USER_AGENT = "job-hunt-copilot/0.1 (+personal internship search; human-in-the-loop)"


class SourceError(RuntimeError):
    """A board could not be fetched or parsed."""

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found  # the board does not exist (HTTP 404 / missing fixture)


class Fetcher(Protocol):
    def get_json(self, url: str, *, key: str) -> Any:
        """Return the decoded JSON at ``url``. ``key`` identifies the board (source_slug)."""


class HttpFetcher:
    """Polite stdlib HTTP client: timeout, small retry with backoff, delay between calls."""

    def __init__(self, timeout: float = 20.0, retries: int = 2, delay: float = 0.5) -> None:
        self.timeout = timeout
        self.retries = retries
        self.delay = delay

    def get_json(self, url: str, *, key: str) -> Any:  # noqa: ARG002 - key unused online
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                time.sleep(self.delay)
                return payload
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise SourceError(f"board not found (404): {url}", not_found=True) from exc
                last_error = exc
                if exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(2**attempt)
        raise SourceError(f"failed to fetch {url}: {last_error}")


class FixtureFetcher:
    """Offline fetcher: reads ``<directory>/<source>_<slug>.json``."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def get_json(self, url: str, *, key: str) -> Any:  # noqa: ARG002 - url unused offline
        path = self.directory / f"{key}.json"
        if not path.exists():
            raise SourceError(f"no fixture {path.name} in {self.directory}", not_found=True)
        return json.loads(path.read_text(encoding="utf-8"))


class JobSource(ABC):
    """Base class for an ATS connector."""

    name: str = ""

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher

    @abstractmethod
    def board_url(self, company: Company) -> str: ...

    @abstractmethod
    def parse(self, payload: Any, company: Company) -> list[Job]: ...

    def fetch(self, company: Company) -> list[Job]:
        payload = self.fetcher.get_json(self.board_url(company), key=f"{self.name}_{company.slug}")
        try:
            return self.parse(payload, company)
        except (KeyError, TypeError, AttributeError) as exc:
            raise SourceError(
                f"unexpected {self.name} payload for {company.slug}: {exc!r}"
            ) from exc


# --------------------------------------------------------------------------- helpers


def iso_date(value: Any) -> str | None:
    """Parse ISO-8601 strings or epoch milliseconds into YYYY-MM-DD."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int | float):
            return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).date().isoformat()
        text = str(value).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(text).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def normalize_workplace(explicit: str | None, *texts: str) -> str:
    value = (explicit or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if value in ("remote", "fullyremote"):
        return "remote"
    if value == "hybrid":
        return "hybrid"
    if value in ("onsite", "inoffice", "office"):
        return "onsite"
    if looks_remote(*texts):
        return "remote"
    if any(contains_term(t, term) for t in texts if t for term in ("hybrid", "híbrido")):
        return "hybrid"  # e.g. "Hybrid - Madrid"
    return "unknown"


_EMPLOYMENT_ALIASES = {
    "intern": "internship",
    "internship": "internship",
    "fulltime": "full_time",
    "permanent": "full_time",
    "parttime": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "temporary",
    "temp": "temporary",
    "fixedterm": "temporary",
}


def normalize_employment(explicit: str | None, title: str) -> str:
    """Map an ATS employment value to EMPLOYMENT_TYPES. An intern-like title always wins."""
    value = (explicit or "").strip().lower()
    key = value.replace("-", "").replace("_", "").replace(" ", "")
    if looks_like_internship(title) or key in ("intern", "internship"):
        return "internship"
    if key in _EMPLOYMENT_ALIASES:
        return _EMPLOYMENT_ALIASES[key]
    if looks_like_internship(value):
        return "internship"
    return "unknown"


def unique(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item and item.strip():
            seen.setdefault(item.strip(), None)
    return list(seen)
