"""Shared steps used by several CLI commands: scoring the tracker and location filters."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field

from jobhunt.location import matches
from jobhunt.models import Job
from jobhunt.profile import Profile
from jobhunt.scoring import assess_location, score_job, scoring_fingerprint
from jobhunt.tracker import JobRecord, Tracker

FINGERPRINT_KEY = "scoring_fingerprint"


@dataclass
class ScoringRun:
    scored: int
    reason: str  # "new jobs", "profile changed" or "forced"


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
