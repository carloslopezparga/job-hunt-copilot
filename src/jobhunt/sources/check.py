"""Verify job boards against the live APIs before trusting them.

`jobhunt check-boards` runs this over companies.yaml (to find dead boards) or over a
candidate list (to decide which new companies to add). Only boards that answer with
real postings, and optionally with postings in Spain/the EU, are promoted.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from jobhunt.location import FREE_MOVEMENT, job_places
from jobhunt.models import Company, Job
from jobhunt.sources import SOURCES, Fetcher, SourceError
from jobhunt.textutils import looks_like_internship


@dataclass
class BoardCheck:
    company: Company
    status: str  # ok | empty | not_found | error
    total: int = 0
    spain: int = 0  # postings with a place in Spain
    eu: int = 0  # postings with a place where an EU citizen needs no visa (EU/EEA/CH)
    interns: int = 0
    error: str = ""

    @property
    def summary(self) -> str:
        if self.status == "ok":
            return (f"{self.total} jobs · {self.spain} in Spain · {self.eu} EU/no-visa · "
                    f"{self.interns} internships")  # fmt: skip
        return self.error or self.status


def _in_spain(job: Job) -> bool:
    return any(p.country == "ES" for p in job_places(job))


def _eu_workable(job: Job) -> bool:
    """At least one place an EU citizen can work without a visa (EU/EEA/CH, 'Remote - EU')."""
    return any(p.needs_visa(FREE_MOVEMENT) is False for p in job_places(job))


def check_board(company: Company, fetcher: Fetcher) -> BoardCheck:
    try:
        jobs = SOURCES[company.ats](fetcher).fetch(company)
    except SourceError as exc:
        return BoardCheck(company, "not_found" if exc.not_found else "error", error=str(exc))
    if not jobs:
        return BoardCheck(company, "empty", error="board exists but has 0 postings")
    return BoardCheck(
        company,
        "ok",
        total=len(jobs),
        spain=sum(_in_spain(j) for j in jobs),
        eu=sum(_eu_workable(j) for j in jobs),
        interns=sum(
            j.employment_type == "internship" or looks_like_internship(j.title) for j in jobs
        ),  # fmt: skip
    )


def qualifies(check: BoardCheck, min_eu_jobs: int) -> bool:
    return check.status == "ok" and check.eu >= min_eu_jobs


def _existing_keys(text: str) -> set[tuple[str, str]]:
    keys = set()
    for m in re.finditer(r"ats:\s*([\w-]+)\s*,\s*slug:\s*([^\s,}]+)", text):
        keys.add((m.group(1).lower(), m.group(2).strip("'\"").lower()))
    return keys


def _yaml_scalar(value: str) -> str:
    return value if re.fullmatch(r"[\w.-]+", value) else '"' + value.replace('"', '\\"') + '"'


def promote(checks: list[BoardCheck], target: str | Path, today: dt.date) -> list[Company]:
    """Append verified companies to ``target`` (companies.yaml), skipping ones already there."""
    path = Path(target)
    text = path.read_text(encoding="utf-8")
    known = _existing_keys(text)
    added: list[Company] = []
    lines = []
    for check in checks:
        c = check.company
        if (c.ats, c.slug.lower()) in known:
            continue
        known.add((c.ats, c.slug.lower()))
        region = f", region: {c.region}" if c.region else ""
        lines.append(
            f"  - {{name: {_yaml_scalar(c.name)}, ats: {c.ats}, slug: {_yaml_scalar(c.slug)}, "
            f"sector: {c.sector}{region}}}  # {check.summary}"
        )
        added.append(c)
    if lines:
        header = f"\n  # --- verified with `jobhunt check-boards` on {today.isoformat()} ---\n"
        if not text.endswith("\n"):
            text += "\n"
        path.write_text(text + header + "\n".join(lines) + "\n", encoding="utf-8")
    return added
