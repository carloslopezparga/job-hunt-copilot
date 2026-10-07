"""Load and validate ``profile.yaml``.

The profile is the single source of truth about the candidate. This module only
parses and checks it; it never adds, rewrites or embellishes any content.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLACEHOLDER_MARKER = "TODO"
SECTORS = ("ai_tech", "quant_trading", "investment_banking", "consulting", "fintech")

# A localized text is either a plain string or a mapping {"en": ..., "es": ...}.
LocalizedText = str | dict[str, str]


class ProfileError(ValueError):
    """Raised when profile.yaml is missing or structurally invalid."""


def localize(value: LocalizedText | None, lang: str, fallback: str = "en") -> str:
    """Return ``value`` in ``lang`` (falling back to ``fallback``, then any language)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in (lang, fallback):
            text = value.get(key)
            if text:
                return str(text).strip()
        for text in value.values():
            if text:
                return str(text).strip()
        return ""
    return str(value).strip()


def has_translation(value: LocalizedText | None, lang: str) -> bool:
    """True if ``value`` is a plain string or explicitly provides ``lang``."""
    if value is None or isinstance(value, str):
        return True
    return bool(value.get(lang))


@dataclass
class Personal:
    full_name: str
    email: str
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""
    work_authorization: list[str] = field(default_factory=lambda: ["EU"])

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0] if self.full_name.split() else ""


@dataclass
class Education:
    institution: str
    degree: LocalizedText
    location: str = ""
    start: str = ""
    end: str = ""
    expected: bool = False
    grade: str = ""
    highlights: list[LocalizedText] = field(default_factory=list)


@dataclass
class Experience:
    organization: str
    role: str
    location: str = ""
    start: str = ""
    end: str = ""
    bullets: list[LocalizedText] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


@dataclass
class Project:
    name: str
    date: str = ""
    url: str = ""
    description: LocalizedText = ""
    bullets: list[LocalizedText] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


@dataclass
class Language:
    language: str
    level: str
    certificate: str = ""


@dataclass
class Skills:
    technical: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)

    def all(self) -> list[str]:
        seen: dict[str, str] = {}
        for skill in [*self.technical, *self.tools, *self.domains]:
            seen.setdefault(skill.lower(), skill)
        return list(seen.values())


@dataclass
class AvailabilityWindow:
    label: str
    start: dt.date
    end: dt.date
    remote_only: bool = False

    def overlaps(self, start: dt.date, end: dt.date) -> bool:
        return self.start <= end and start <= self.end


@dataclass
class LocationPrefs:
    preferred: list[str] = field(default_factory=list)
    acceptable: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)


@dataclass
class Preferences:
    roles: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=lambda: list(SECTORS))
    contract_types: list[str] = field(default_factory=lambda: ["internship"])
    exclude_title_keywords: list[str] = field(default_factory=list)
    locations: LocationPrefs = field(default_factory=LocationPrefs)
    availability: list[AvailabilityWindow] = field(default_factory=list)
    application_languages: list[str] = field(default_factory=lambda: ["en"])


@dataclass
class ScoringConfig:
    min_score_to_prepare: float = 65
    weights: dict[str, float] = field(default_factory=dict)


@dataclass
class Profile:
    personal: Personal
    summaries: dict[str, LocalizedText]
    education: list[Education]
    experience: list[Experience]
    projects: list[Project]
    languages: list[Language]
    skills: Skills
    achievements: list[LocalizedText]
    motivations: dict[str, LocalizedText]
    preferences: Preferences
    scoring: ScoringConfig
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def summary_for(self, sector: str | None) -> LocalizedText:
        if sector and sector in self.summaries:
            return self.summaries[sector]
        return self.summaries.get("default", "")

    def placeholders(self) -> list[str]:
        """Dotted paths of fields that still contain the TODO placeholder."""
        return _find_placeholders(self.raw)


# --------------------------------------------------------------------------- parsing


def _as_list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProfileError(f"'{where}' must be a list")
    return value


def _as_dict(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileError(f"'{where}' must be a mapping")
    return value


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _str_list(value: Any, where: str) -> list[str]:
    return [_str(v) for v in _as_list(value, where) if _str(v)]


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    value = data.get(key)
    if value in (None, "", [], {}):
        raise ProfileError(f"missing required field '{where}.{key}'")
    return value


def _parse_date(value: Any, where: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ProfileError(f"'{where}' must be a date in YYYY-MM-DD format") from exc


def _find_placeholders(node: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _find_placeholders(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += _find_placeholders(value, f"{path}[{i}]")
    elif isinstance(node, str) and PLACEHOLDER_MARKER in node:
        found.append(path)
    return found


def parse_profile(data: dict[str, Any]) -> Profile:
    if not isinstance(data, dict):
        raise ProfileError("profile must be a YAML mapping")

    personal_raw = _as_dict(data.get("personal"), "personal")
    personal = Personal(
        full_name=_str(_require(personal_raw, "full_name", "personal")),
        email=_str(_require(personal_raw, "email", "personal")),
        phone=_str(personal_raw.get("phone")),
        location=_str(personal_raw.get("location")),
        linkedin=_str(personal_raw.get("linkedin")),
        github=_str(personal_raw.get("github")),
        website=_str(personal_raw.get("website")),
        work_authorization=_str_list(
            personal_raw.get("work_authorization", ["EU"]), "personal.work_authorization"
        ),
    )

    summaries = _as_dict(data.get("summaries"), "summaries")
    if not summaries.get("default"):
        raise ProfileError("missing required field 'summaries.default'")

    education = []
    for i, e in enumerate(_as_list(data.get("education"), "education")):
        where = f"education[{i}]"
        e = _as_dict(e, where)
        education.append(
            Education(
                institution=_str(_require(e, "institution", where)),
                degree=_require(e, "degree", where),
                location=_str(e.get("location")),
                start=_str(e.get("start")),
                end=_str(e.get("end")),
                expected=bool(e.get("expected", False)),
                grade=_str(e.get("grade")),
                highlights=_as_list(e.get("highlights"), f"{where}.highlights"),
            )
        )
    if not education:
        raise ProfileError("at least one 'education' entry is required")

    experience = []
    for i, e in enumerate(_as_list(data.get("experience"), "experience")):
        where = f"experience[{i}]"
        e = _as_dict(e, where)
        experience.append(
            Experience(
                organization=_str(_require(e, "organization", where)),
                role=_str(_require(e, "role", where)),
                location=_str(e.get("location")),
                start=_str(e.get("start")),
                end=_str(e.get("end")),
                bullets=_as_list(e.get("bullets"), f"{where}.bullets"),
                skills=_str_list(e.get("skills"), f"{where}.skills"),
            )
        )

    projects = []
    for i, p in enumerate(_as_list(data.get("projects"), "projects")):
        where = f"projects[{i}]"
        p = _as_dict(p, where)
        projects.append(
            Project(
                name=_str(_require(p, "name", where)),
                date=_str(p.get("date")),
                url=_str(p.get("url")),
                description=p.get("description") or "",
                bullets=_as_list(p.get("bullets"), f"{where}.bullets"),
                skills=_str_list(p.get("skills"), f"{where}.skills"),
            )
        )

    languages = []
    for i, lang in enumerate(_as_list(data.get("languages"), "languages")):
        where = f"languages[{i}]"
        lang = _as_dict(lang, where)
        languages.append(
            Language(
                language=_str(_require(lang, "language", where)),
                level=_str(_require(lang, "level", where)),
                certificate=_str(lang.get("certificate")),
            )
        )

    skills_raw = _as_dict(data.get("skills"), "skills")
    skills = Skills(
        technical=_str_list(skills_raw.get("technical"), "skills.technical"),
        tools=_str_list(skills_raw.get("tools"), "skills.tools"),
        domains=_str_list(skills_raw.get("domains"), "skills.domains"),
    )

    prefs_raw = _as_dict(data.get("preferences"), "preferences")
    loc_raw = _as_dict(prefs_raw.get("locations"), "preferences.locations")
    windows = []
    for i, w in enumerate(_as_list(prefs_raw.get("availability"), "preferences.availability")):
        where = f"preferences.availability[{i}]"
        w = _as_dict(w, where)
        start = _parse_date(_require(w, "start", where), f"{where}.start")
        end = _parse_date(_require(w, "end", where), f"{where}.end")
        if end < start:
            raise ProfileError(f"'{where}': end is before start")
        windows.append(
            AvailabilityWindow(
                label=_str(w.get("label")) or f"window {i + 1}",
                start=start,
                end=end,
                remote_only=bool(w.get("remote_only", False)),
            )
        )
    sectors = _str_list(prefs_raw.get("sectors", list(SECTORS)), "preferences.sectors")
    unknown = sorted(set(sectors) - set(SECTORS))
    if unknown:
        raise ProfileError(
            f"unknown sector(s) {unknown} in preferences.sectors; valid: {list(SECTORS)}"
        )
    preferences = Preferences(
        roles=[r.lower() for r in _str_list(prefs_raw.get("roles"), "preferences.roles")],
        sectors=sectors,
        contract_types=[
            c.lower()
            for c in _str_list(
                prefs_raw.get("contract_types", ["internship"]), "preferences.contract_types"
            )
        ],
        exclude_title_keywords=[
            k.lower()
            for k in _str_list(
                prefs_raw.get("exclude_title_keywords"), "preferences.exclude_title_keywords"
            )
        ],
        locations=LocationPrefs(
            preferred=_str_list(loc_raw.get("preferred"), "preferences.locations.preferred"),
            acceptable=_str_list(loc_raw.get("acceptable"), "preferences.locations.acceptable"),
            avoid=_str_list(loc_raw.get("avoid"), "preferences.locations.avoid"),
        ),
        availability=windows,
        application_languages=_str_list(
            prefs_raw.get("application_languages", ["en"]), "preferences.application_languages"
        )
        or ["en"],
    )

    scoring_raw = _as_dict(data.get("scoring"), "scoring")
    weights_raw = _as_dict(scoring_raw.get("weights"), "scoring.weights")
    try:
        weights = {str(k): float(v) for k, v in weights_raw.items()}
        min_score = float(scoring_raw.get("min_score_to_prepare", 65))
    except (TypeError, ValueError) as exc:
        raise ProfileError("scoring weights and min_score_to_prepare must be numbers") from exc
    if any(v < 0 for v in weights.values()):
        raise ProfileError("scoring weights must be non-negative")

    return Profile(
        personal=personal,
        summaries=summaries,
        education=education,
        experience=experience,
        projects=projects,
        languages=languages,
        skills=skills,
        achievements=_as_list(data.get("achievements"), "achievements"),
        motivations=_as_dict(data.get("motivations"), "motivations"),
        preferences=preferences,
        scoring=ScoringConfig(min_score_to_prepare=min_score, weights=weights),
        raw=data,
    )


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    if not path.exists():
        raise ProfileError(
            f"{path} not found. Run `jobhunt init` or copy profile.template.yaml to {path}."
        )
    with path.open(encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ProfileError(f"{path} is not valid YAML: {exc}") from exc
    return parse_profile(data or {})
