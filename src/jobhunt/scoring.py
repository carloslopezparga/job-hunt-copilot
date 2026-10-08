"""Score job postings 0-100 against the profile and filter duplicates.

Each component returns a value in [0, 1] plus a short note; the final score is
the weighted average scaled to 100. Weights come from ``profile.scoring.weights``
(missing keys fall back to ``DEFAULT_WEIGHTS``).
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field

from jobhunt.location import LocationAssessment, assess
from jobhunt.models import Job
from jobhunt.profile import AvailabilityWindow, Profile
from jobhunt.textutils import contains_term, find_terms, fold, looks_like_internship, normalize_key

# Bump when scoring logic changes so stored scores are recomputed automatically.
SCORING_VERSION = "2"

DEFAULT_WEIGHTS: dict[str, float] = {
    "role": 20,
    "contract": 20,
    "location": 20,
    "skills": 15,
    "sector": 10,
    "dates": 10,
    "remote": 5,
}
EXCLUDED_CAP = 15
VISA_CAP = 30  # below any sensible min_score_to_prepare: never shortlisted
SKILLS_FOR_FULL_MARKS = 5

# Location tier -> (component value, multiplier on the final score). The multiplier makes
# the location priority bite even when a profile gives the location weight little room.
LOCATION_TIERS: dict[str, tuple[float, float]] = {
    "preferred": (1.0, 1.0),
    "acceptable": (0.7, 1.0),
    "unknown": (0.4, 0.95),
    "other": (0.3, 0.85),
    "avoided": (0.1, 0.6),
    "visa": (0.0, 1.0),  # capped at VISA_CAP instead
}


@dataclass
class ScoreResult:
    score: int
    components: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    excluded: str | None = None
    location: LocationAssessment | None = None

    @property
    def explanation(self) -> str:
        parts = [f"excluded: {self.excluded}"] if self.excluded else []
        parts += [note for note in self.notes.values() if note]
        return " · ".join(parts)


# --------------------------------------------------------------------------- dates

_MONTHS = {
    **{fold(calendar.month_name[i]): i for i in range(1, 13)},
    **{fold(calendar.month_abbr[i]): i for i in range(1, 13)},
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_MONTHS.pop("", None)
_MONTHS.pop("may", None)  # avoid matching the verb "may"; "may 2027" is handled below
_MONTH_RE = "|".join(sorted(map(re.escape, [*_MONTHS, "may"]), key=len, reverse=True))

_SEASONS = {
    "spring": (3, 5),
    "primavera": (3, 5),
    "summer": (6, 9),
    "verano": (6, 9),
    "fall": (9, 12),
    "autumn": (9, 12),
    "otono": (9, 12),
    "winter": (1, 3),
    "invierno": (1, 3),
}
_SEASON_RE = "|".join(_SEASONS)


@dataclass(frozen=True)
class Period:
    start: dt.date
    end: dt.date
    label: str


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def extract_periods(text: str, today: dt.date) -> list[Period]:
    """Find explicit time periods such as 'Summer 2027', 'June 2027', 'off-cycle 2027', 'ASAP'."""
    t = fold(text)
    periods: list[Period] = []

    for m in re.finditer(rf"\b({_SEASON_RE})\s*(?:of\s+|de\s+|del\s+)?(20\d\d)\b", t):
        start_m, end_m = _SEASONS[m.group(1)]
        year = int(m.group(2))
        periods.append(
            Period(dt.date(year, start_m, 1), _month_end(year, end_m), f"{m.group(1)} {year}")
        )

    for m in re.finditer(rf"\b({_MONTH_RE})\.?\s*(?:de\s+|of\s+)?(20\d\d)\b", t):
        month = 5 if m.group(1) == "may" else _MONTHS[m.group(1)]
        year = int(m.group(2))
        periods.append(
            Period(dt.date(year, month, 1), _month_end(year, month), f"{m.group(1)} {year}")
        )

    for m in re.finditer(r"\boff[\s-]?cycle\s*(?:internship\s*)?(?:\(?\s*)?(20\d\d)\b", t):
        year = int(m.group(1))
        periods.append(Period(dt.date(year, 1, 1), dt.date(year, 12, 31), f"off-cycle {year}"))

    if re.search(r"\b(asap|as soon as possible|immediate(ly)?|lo antes posible)\b", t):
        periods.append(Period(today, today + dt.timedelta(days=60), "ASAP"))

    if not periods:
        # Fall back to a bare year (e.g. "Quant Intern 2027"), only near intern-ish words.
        for m in re.finditer(r"\b(intern\w*|placement|graduate|class of)\D{0,20}(20\d\d)\b", t):
            year = int(m.group(2))
            periods.append(Period(dt.date(year, 1, 1), dt.date(year, 12, 31), str(year)))
    return periods


def _matching_windows(
    periods: list[Period], windows: list[AvailabilityWindow]
) -> list[AvailabilityWindow]:
    return [w for w in windows if any(w.overlaps(p.start, p.end) for p in periods)]


# ---------------------------------------------------------------------- components


def _role(job: Job, profile: Profile) -> tuple[float, str]:
    roles = profile.preferences.roles
    if not roles:
        return 0.5, "role: no preferences set"
    in_title = find_terms(job.title, roles)
    if in_title:
        return 1.0, f"role '{in_title[0]}' ✓"
    in_desc = find_terms(job.description, roles)
    if in_desc:
        return 0.4, f"role '{in_desc[0]}' in description"
    return 0.0, "role ✗"


def _sector(job: Job, profile: Profile) -> tuple[float, str]:
    if not job.sector:
        return 0.5, "sector unknown"
    if job.sector in profile.preferences.sectors:
        return 1.0, f"{job.sector} ✓"
    return 0.0, f"{job.sector} ✗"


def _contract(job: Job, profile: Profile) -> tuple[float, str]:
    wanted = set(profile.preferences.contract_types)
    is_intern = job.employment_type == "internship" or looks_like_internship(job.title)
    if is_intern:
        return (1.0, "internship ✓") if "internship" in wanted else (0.2, "internship ✗")
    if job.employment_type in wanted:
        return 1.0, f"{job.employment_type} ✓"
    if job.employment_type == "unknown":
        if "internship" in wanted and looks_like_internship(job.description[:1500]):
            return 0.5, "contract unclear (mentions internship)"
        if find_terms(job.description[:1500], ["full-time", "full time", "permanent"]):
            return 0.05, "likely full-time ✗"
        return 0.2, "contract unclear"
    return 0.0, f"{job.employment_type.replace('_', '-')} ✗"


def best_window(
    job: Job, profile: Profile, today: dt.date | None = None
) -> AvailabilityWindow | None:
    """The availability window that best fits the job, or None if none fits.

    Prefers windows whose remote constraint the job satisfies; for undated jobs any
    such window qualifies.
    """
    today = today or dt.date.today()
    windows = profile.preferences.availability
    periods = [
        p for p in extract_periods(f"{job.title}\n{job.description}", today) if p.end >= today
    ]
    if periods:
        candidates = [w for w in windows if any(w.overlaps(p.start, p.end) for p in periods)]
    else:
        candidates = [w for w in windows if w.end >= today]
    candidates.sort(key=lambda w: (w.remote_only and not job.is_remote, w.start))
    return candidates[0] if candidates else None


def _dates(
    job: Job, profile: Profile, today: dt.date
) -> tuple[float, str, list[AvailabilityWindow], str | None]:
    """Returns (value, note, compatible availability windows, cap reason if stale)."""
    windows = profile.preferences.availability
    periods = extract_periods(f"{job.title}\n{job.description}", today)
    if not windows:
        return 0.5, "", windows, None
    if not periods:  # undated: could fit any window
        return 0.5, "dates not stated", windows, None
    current = [p for p in periods if p.end >= today]
    if not current:
        stale = None
        title_periods = extract_periods(job.title, today)
        if title_periods and all(p.end < today for p in title_periods):
            stale = f"{title_periods[0].label} already past"
        return 0.0, f"{periods[0].label} already past", [], stale
    pairs = [(p, w) for w in windows for p in current if w.overlaps(p.start, p.end)]
    if not pairs:
        return 0.1, f"{current[0].label} outside availability", [], None
    # Prefer a window whose remote constraint the job satisfies, then the tightest period.
    pairs.sort(key=lambda pw: (pw[1].remote_only and not job.is_remote, pw[0].end - pw[0].start))
    period, window = pairs[0]
    compatible = [w for w in windows if any(w is pw for _, pw in pairs)]
    return 1.0, f"{period.label} fits '{window.label}'", compatible, None


def _remote(
    job: Job, windows: list[AvailabilityWindow], no_availability: bool, remote_ok: bool
) -> tuple[float, str]:
    if remote_ok:
        return 1.0, "remote ✓"
    if not windows:
        if no_availability:
            return 0.6, ""
        return 0.0, "no compatible window"
    if any(not w.remote_only for w in windows):
        return 1.0, "on-site OK" if job.workplace_type != "unknown" else "workplace not stated"
    return {"hybrid": 0.2, "onsite": 0.0}.get(job.workplace_type, 0.3), "needs remote ✗"


def _skills(job: Job, profile: Profile) -> tuple[float, str]:
    skills = profile.skills.all()
    for item in [*profile.experience, *profile.projects]:
        skills += [s for s in item.skills if s.lower() not in {x.lower() for x in skills}]
    matched = find_terms(f"{job.title}\n{job.description}", skills)
    if not matched:
        return 0.0, "skills: no overlap"
    value = min(1.0, len(matched) / SKILLS_FOR_FULL_MARKS)
    return value, "skills: " + ", ".join(matched[:4])


def assess_location(job: Job, profile: Profile) -> LocationAssessment:
    prefs = profile.preferences.locations
    return assess(
        job, prefs.preferred, prefs.acceptable, prefs.avoid, profile.personal.work_authorization
    )


def _location(assessment: LocationAssessment) -> tuple[float, str]:
    value, _ = LOCATION_TIERS[assessment.tier]
    return value, assessment.label()


# --------------------------------------------------------------------------- score


def effective_weights(profile: Profile) -> dict[str, float]:
    weights = dict(DEFAULT_WEIGHTS)
    weights.update({k: v for k, v in profile.scoring.weights.items() if k in DEFAULT_WEIGHTS})
    return weights


def score_job(job: Job, profile: Profile, today: dt.date | None = None) -> ScoreResult:
    today = today or dt.date.today()
    weights = effective_weights(profile)
    components: dict[str, float] = {}
    notes: dict[str, str] = {}

    components["contract"], notes["contract"] = _contract(job, profile)
    components["role"], notes["role"] = _role(job, profile)
    components["sector"], notes["sector"] = _sector(job, profile)
    location = assess_location(job, profile)
    remote_ok = location.remote or (job.workplace_type == "remote" and location.tier != "visa")
    components["dates"], notes["dates"], windows, stale = _dates(job, profile, today)
    components["remote"], notes["remote"] = _remote(
        job, windows, no_availability=not profile.preferences.availability, remote_ok=remote_ok
    )
    components["skills"], notes["skills"] = _skills(job, profile)
    components["location"], notes["location"] = _location(location)

    total_weight = sum(weights.values()) or 1.0
    raw = sum(weights[k] * components[k] for k in components) / total_weight * 100
    score = round(raw * LOCATION_TIERS[location.tier][1])

    keyword = next(
        (kw for kw in profile.preferences.exclude_title_keywords if contains_term(job.title, kw)),
        None,
    )
    excluded = f"'{keyword}' in title" if keyword else stale
    if excluded:
        score = min(score, EXCLUDED_CAP)
    elif location.needs_visa:
        score = min(score, VISA_CAP)
    return ScoreResult(
        score=score, components=components, notes=notes, excluded=excluded, location=location
    )


def scoring_fingerprint(profile: Profile) -> str:
    """Changes whenever the profile or the scoring logic changes."""
    return f"v{SCORING_VERSION}:{profile.fingerprint()}"


# ----------------------------------------------------------------------- dedup


def dedup_key(job: Job) -> str:
    """Same company + same normalized title + same primary city => duplicate."""
    city = (job.location or (job.locations[0] if job.locations else "")).split(",")[0]
    title = normalize_key(job.title)
    return f"{normalize_key(job.company)}|{title}|{normalize_key(city)}"


def deduplicate(jobs: list[Job]) -> list[Job]:
    """Drop duplicates (same uid, or same dedup_key); keep the most recently posted one."""
    best: dict[str, Job] = {}
    order: list[str] = []
    seen_uids: set[str] = set()
    for job in jobs:
        if job.uid in seen_uids:
            continue
        seen_uids.add(job.uid)
        key = dedup_key(job)
        if key not in best:
            best[key] = job
            order.append(key)
        elif (job.posted_at or "") > (best[key].posted_at or ""):
            best[key] = job
    return [best[k] for k in order]


def rank(
    jobs: list[Job], profile: Profile, today: dt.date | None = None
) -> list[tuple[Job, ScoreResult]]:
    """Deduplicate, score and sort jobs by descending score."""
    scored = [(job, score_job(job, profile, today)) for job in deduplicate(jobs)]
    return sorted(scored, key=lambda pair: pair[1].score, reverse=True)
