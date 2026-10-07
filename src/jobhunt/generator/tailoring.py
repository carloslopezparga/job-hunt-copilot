"""Select, reorder and highlight profile content for a specific job.

Tailoring never writes new claims: it only ranks the candidate's own bullets,
projects and skills by how relevant they are to the job, and marks which terms
to highlight. Every string that reaches a document comes from profile.yaml.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from jobhunt.models import Job
from jobhunt.profile import (
    AvailabilityWindow,
    Education,
    Experience,
    Language,
    Profile,
    Project,
    has_translation,
    localize,
)
from jobhunt.scoring import best_window
from jobhunt.textutils import contains_term, find_terms

MAX_PROJECTS = 3
MAX_BULLETS_PER_ITEM = 4


@dataclass
class TailoredItem:
    """An experience or project with its bullets ranked for the job."""

    title: str
    organization: str
    location: str
    start: str
    end: str
    bullets: list[str]
    description: str = ""
    url: str = ""
    relevance: float = 0.0
    matched: list[str] = field(default_factory=list)
    untranslated: set[str] = field(default_factory=set)  # bullets shown in fallback language

    def translated_bullets(self) -> list[str]:
        return [b for b in self.bullets if b not in self.untranslated]


@dataclass
class TailoredProfile:
    lang: str
    job: Job
    full_name: str
    first_name: str
    email: str
    phone: str
    location: str
    links: list[str]
    summary: str
    education: list[Education]
    experience: list[TailoredItem]
    projects: list[TailoredItem]
    skills_highlighted: list[str]
    skills_other: list[str]
    languages: list[Language]
    achievements: list[str]
    achievements_translated: list[str]  # only those available in `lang` (for letters)
    motivation: str
    motivation_translated: bool
    keywords: list[str]
    window: AvailabilityWindow | None
    warnings: list[str] = field(default_factory=list)

    @property
    def top_experience(self) -> TailoredItem | None:
        ranked = sorted(self.experience, key=lambda i: i.relevance, reverse=True)
        return ranked[0] if ranked else None

    @property
    def top_project(self) -> TailoredItem | None:
        return self.projects[0] if self.projects else None


def job_keywords(profile: Profile, job: Job) -> list[str]:
    """Profile skills/tags (and preferred role terms) that the job mentions."""
    vocabulary: dict[str, str] = {}
    for term in profile.skills.all():
        vocabulary.setdefault(term.lower(), term)
    for item in [*profile.experience, *profile.projects]:
        for term in item.skills:
            vocabulary.setdefault(term.lower(), term)
    text = f"{job.title}\n{job.description}"
    return find_terms(text, list(vocabulary.values()))


def _relevance(text: str, tags: list[str], keywords: list[str]) -> tuple[float, list[str]]:
    in_text = [k for k in keywords if contains_term(text, k)]
    in_tags = [k for k in keywords if k.lower() in {t.lower() for t in tags}]
    matched = list(dict.fromkeys([*in_tags, *in_text]))
    return len(in_tags) * 1.0 + len(in_text) * 1.5, matched


def _rank_bullets(bullets: list[str], keywords: list[str]) -> list[str]:
    scored = [(sum(contains_term(b, k) for k in keywords), -i, b) for i, b in enumerate(bullets)]
    scored.sort(reverse=True)
    return [b for *_, b in scored][:MAX_BULLETS_PER_ITEM]


def _sort_key_date(value: str) -> str:
    return "9999-99" if value.lower() in ("present", "actualidad", "now", "") else value


def _translate(value, lang: str, where: str, warnings: list[str]) -> str:  # noqa: ANN001
    if not has_translation(value, lang):
        warnings.append(f"no '{lang}' text for {where}; used the English version")
    return localize(value, lang)


def _untranslated(values: list, lang: str) -> set[str]:  # noqa: ANN001
    return {localize(v, lang) for v in values if not has_translation(v, lang)}


def _tailor_experience(
    exp: Experience, idx: int, lang: str, keywords: list[str], warnings: list[str]
) -> TailoredItem:
    bullets = [
        _translate(b, lang, f"experience[{idx}].bullets[{j}]", warnings)
        for j, b in enumerate(exp.bullets)
    ]
    relevance, matched = _relevance(f"{exp.role} " + " ".join(bullets), exp.skills, keywords)
    return TailoredItem(
        title=exp.role,
        organization=exp.organization,
        location=exp.location,
        start=exp.start,
        end=exp.end,
        bullets=_rank_bullets([b for b in bullets if b], keywords),
        relevance=relevance,
        matched=matched,
        untranslated=_untranslated(exp.bullets, lang),
    )


def _tailor_project(
    proj: Project, idx: int, lang: str, keywords: list[str], warnings: list[str]
) -> TailoredItem:
    bullets = [
        _translate(b, lang, f"projects[{idx}].bullets[{j}]", warnings)
        for j, b in enumerate(proj.bullets)
    ]
    description = _translate(proj.description, lang, f"projects[{idx}].description", warnings)
    relevance, matched = _relevance(
        f"{proj.name} {description} " + " ".join(bullets), proj.skills, keywords
    )
    return TailoredItem(
        title=proj.name,
        organization="",
        location="",
        start=proj.date,
        end="",
        bullets=_rank_bullets([b for b in bullets if b], keywords),
        description=description,
        url=proj.url,
        relevance=relevance,
        matched=matched,
        untranslated=_untranslated(proj.bullets, lang),
    )


def tailor(
    profile: Profile, job: Job, lang: str = "en", today: dt.date | None = None
) -> TailoredProfile:
    warnings: list[str] = []
    keywords = job_keywords(profile, job)

    experience = [
        _tailor_experience(e, i, lang, keywords, warnings) for i, e in enumerate(profile.experience)
    ]
    # CVs stay reverse-chronological; relevance decides bullets and the letter's focus.
    experience.sort(key=lambda i: _sort_key_date(i.end or i.start), reverse=True)

    projects = [
        _tailor_project(p, i, lang, keywords, warnings) for i, p in enumerate(profile.projects)
    ]
    projects.sort(key=lambda i: (i.relevance, i.start), reverse=True)

    all_skills = profile.skills.all()
    highlighted = [s for s in all_skills if any(s.lower() == k.lower() for k in keywords)]
    other = [s for s in all_skills if s not in highlighted]

    summary_value = profile.summary_for(job.sector)
    summary = _translate(summary_value, lang, "summaries", warnings)

    motivation, motivation_translated = "", True
    if job.sector and job.sector in profile.motivations:
        value = profile.motivations[job.sector]
        motivation = _translate(value, lang, f"motivations.{job.sector}", warnings)
        motivation_translated = has_translation(value, lang)

    p = profile.personal
    return TailoredProfile(
        lang=lang,
        job=job,
        full_name=p.full_name,
        first_name=p.first_name,
        email=p.email,
        phone=p.phone,
        location=p.location,
        links=[u for u in (p.linkedin, p.github, p.website) if u],
        summary=summary,
        education=profile.education,
        experience=experience,
        projects=projects[:MAX_PROJECTS],
        skills_highlighted=highlighted,
        skills_other=other,
        languages=profile.languages,
        achievements=[
            _translate(a, lang, f"achievements[{i}]", warnings)
            for i, a in enumerate(profile.achievements)
        ],
        achievements_translated=[
            localize(a, lang) for a in profile.achievements if has_translation(a, lang)
        ],
        motivation=motivation,
        motivation_translated=motivation_translated,
        keywords=keywords,
        window=best_window(job, profile, today),
        warnings=list(dict.fromkeys(warnings)),
    )


# --------------------------------------------------------------------------- utils

_MONTHS = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
}
_MONTHS_LONG = {
    "en": [
        "January", "February", "March", "April", "May", "June", "July", "August",
        "September", "October", "November", "December",
    ],
    "es": [
        "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
        "septiembre", "octubre", "noviembre", "diciembre",
    ],
}  # fmt: skip
_PRESENT = {"en": "Present", "es": "Actualidad"}


def format_month(value: str, lang: str) -> str:
    """'2024-10' -> 'Oct 2024' / 'oct 2024'; 'present' -> 'Present' / 'Actualidad'."""
    if not value:
        return ""
    if value.lower() in ("present", "actualidad", "now"):
        return _PRESENT.get(lang, "Present")
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", value)
    if not m:
        return value
    return f"{_MONTHS.get(lang, _MONTHS['en'])[int(m.group(2)) - 1]} {m.group(1)}"


def format_long_month(day: dt.date, lang: str) -> str:
    month = _MONTHS_LONG.get(lang, _MONTHS_LONG["en"])[day.month - 1]
    return f"{month} de {day.year}" if lang == "es" else f"{month} {day.year}"


def date_range(start: str, end: str, lang: str) -> str:
    a, b = format_month(start, lang), format_month(end, lang)
    return f"{a} – {b}" if a and b else a or b


def lower_first(text: str) -> str:
    """Lower-case the first letter unless it starts an acronym or proper noun-like token."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


_LANGUAGE_NAMES_ES = {
    "spanish": "español",
    "english": "inglés",
    "french": "francés",
    "german": "alemán",
    "italian": "italiano",
    "portuguese": "portugués",
    "catalan": "catalán",
    "chinese": "chino",
    "mandarin": "chino mandarín",
    "japanese": "japonés",
    "arabic": "árabe",
    "dutch": "neerlandés",
    "russian": "ruso",
}


def language_name(name: str, lang: str) -> str:
    """Localize common language names ('English' -> 'inglés'); unknown names pass through."""
    if lang == "es":
        return _LANGUAGE_NAMES_ES.get(name.strip().lower(), name)
    return name


def human_join(items: list[str], lang: str) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    conj = " y " if lang == "es" else " and "
    return ", ".join(items[:-1]) + conj + items[-1]
