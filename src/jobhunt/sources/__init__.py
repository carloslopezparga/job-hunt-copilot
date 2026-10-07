"""Job-board connectors and discovery orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from jobhunt.models import Company, Job
from jobhunt.profile import SECTORS
from jobhunt.sources.ashby import AshbySource
from jobhunt.sources.base import (
    Fetcher,
    FixtureFetcher,
    HttpFetcher,
    JobSource,
    SourceError,
)
from jobhunt.sources.greenhouse import GreenhouseSource
from jobhunt.sources.lever import LeverSource

SOURCES: dict[str, type[JobSource]] = {
    cls.name: cls for cls in (GreenhouseSource, LeverSource, AshbySource)
}

__all__ = [
    "SOURCES",
    "AshbySource",
    "DiscoveryResult",
    "Fetcher",
    "FixtureFetcher",
    "GreenhouseSource",
    "HttpFetcher",
    "JobSource",
    "LeverSource",
    "SourceError",
    "discover",
    "load_companies",
]


def load_companies(path: str | Path) -> list[Company]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = data.get("companies", data) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected a 'companies' list")
    companies: list[Company] = []
    for i, raw in enumerate(entries):
        try:
            company = Company(
                name=str(raw["name"]),
                ats=str(raw["ats"]).lower(),
                slug=str(raw["slug"]),
                sector=str(raw["sector"]),
                region=str(raw.get("region") or ""),
                notes=str(raw.get("notes") or ""),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"{path}: entry {i} needs name, ats, slug and sector") from exc
        if company.ats not in SOURCES:
            raise ValueError(f"{path}: {company.name}: unknown ats '{company.ats}'")
        if company.sector not in SECTORS:
            raise ValueError(f"{path}: {company.name}: unknown sector '{company.sector}'")
        companies.append(company)
    return companies


@dataclass
class DiscoveryResult:
    jobs: list[Job] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # company name -> error
    counts: dict[str, int] = field(default_factory=dict)  # company name -> jobs found


def discover(companies: list[Company], fetcher: Fetcher) -> DiscoveryResult:
    """Fetch every company's board. One failing board never aborts the run."""
    result = DiscoveryResult()
    for company in companies:
        source = SOURCES[company.ats](fetcher)
        try:
            jobs = source.fetch(company)
        except SourceError as exc:
            result.errors[company.name] = str(exc)
            continue
        result.counts[company.name] = len(jobs)
        result.jobs.extend(jobs)
    return result
