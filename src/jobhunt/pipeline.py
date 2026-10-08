"""Shared steps used by several CLI commands: scoring the tracker and location filters."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from jobhunt.generator import PreparedApplication, prepare_application
from jobhunt.generator.llm import Polisher
from jobhunt.location import matches
from jobhunt.models import Company, Job
from jobhunt.profile import Profile
from jobhunt.scoring import assess_location, deduplicate, score_job, scoring_fingerprint
from jobhunt.sources import Fetcher, discover
from jobhunt.tracker import JobRecord, Tracker

FINGERPRINT_KEY = "scoring_fingerprint"


@dataclass
class DiscoverySummary:
    inserted: int
    updated: int
    duplicates: int  # already tracked under another id, or repeated within this fetch
    boards_ok: dict[str, int]  # company name -> postings found
    errors: dict[str, str]  # company name -> error


def discover_tracked(
    tracker: Tracker, companies: list[Company], fetcher: Fetcher
) -> DiscoverySummary:
    """Fetch the boards and store the postings in the tracker. One failing board never
    aborts the run."""
    result = discover(companies, fetcher)
    jobs = deduplicate(result.jobs)
    stats = tracker.upsert_jobs(jobs)
    return DiscoverySummary(
        inserted=stats.inserted,
        updated=stats.updated,
        duplicates=stats.duplicates + len(result.jobs) - len(jobs),
        boards_ok=result.counts,
        errors=result.errors,
    )


def prepare_record(
    tracker: Tracker,
    profile: Profile,
    record: JobRecord,
    out_dir: Path,
    today: dt.date,
    *,
    lang: str | None = None,
    polisher: Polisher | None = None,
    allow_placeholders: bool = False,
) -> PreparedApplication:
    """Write CV, letter and LinkedIn note for ``record`` and mark it prepared. Sends nothing."""
    result = prepare_application(
        record.job,
        profile,
        out_dir,
        lang=lang,
        polisher=polisher,
        record_id=record.id,
        score=record.score,
        explanation=record.score_explanation,
        today=today,
        allow_placeholders=allow_placeholders,
    )
    tracker.set_prepared_dir(record.id, str(result.directory))
    if record.status == "new":
        tracker.set_status(record.id, "prepared", note=f"material in {result.directory}")
    return result


@dataclass
class ScoringRun:
    scored: int
    reason: str  # "new jobs", "profile changed", "migrated database" or "forced"

    @property
    def rescored_all(self) -> bool:
        return self.reason != "new jobs"


def score_tracked(
    tracker: Tracker, profile: Profile, today: dt.date, force: bool = False
) -> ScoringRun:
    """Score unscored jobs; re-score everything if forced or if the profile (or the
    scoring logic) changed since the last scoring."""
    fingerprint = scoring_fingerprint(profile)
    stored = tracker.get_meta(FINGERPRINT_KEY)
    if force:
        reason, records = "forced", tracker.jobs()
    elif stored is not None and stored != fingerprint:
        reason, records = "profile changed", tracker.jobs()
    elif stored is None and tracker.has_scores():
        # A database scored by a version that never stored a fingerprint: its scores come
        # from unknown rules, so recompute them all instead of trusting them.
        reason, records = "migrated database", tracker.jobs()
    else:
        reason, records = "new jobs", tracker.jobs(unscored_only=True)
    for record in records:
        result = score_job(record.job, profile, today)
        tracker.set_score(record.id, result.score, result.explanation)
    tracker.set_meta(FINGERPRINT_KEY, fingerprint)
    return ScoringRun(scored=len(records), reason=reason)


@dataclass
class LocationFilter:
    text: str | None = None  # keep jobs with a place matching this ("Madrid", "Remote - EU")
    no_visa: bool = False  # drop jobs whose every place needs a visa
    remote_only: bool = False  # keep jobs that can be done remotely from a workable place
    tiers: list[str] = field(default_factory=list)  # empty = any tier

    @classmethod
    def from_profile(cls, profile: Profile) -> LocationFilter:
        rf = profile.preferences.locations.run_filter
        return cls(no_visa=rf.no_visa, remote_only=rf.remote_only, tiers=list(rf.tiers))

    def accepts(self, job: Job, profile: Profile) -> bool:
        assessment = assess_location(job, profile)
        if self.no_visa and assessment.needs_visa:
            return False
        if self.tiers and assessment.tier not in self.tiers:
            return False
        if self.remote_only and not assessment.remote:
            return False
        return not self.text or any(matches(p, self.text) for p in assessment.places)


def apply_filter(
    records: Iterable[JobRecord], profile: Profile, flt: LocationFilter
) -> list[JobRecord]:
    return [r for r in records if flt.accepts(r.job, profile)]
