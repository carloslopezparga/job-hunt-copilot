"""Prepare application material (CV PDF, cover letter, LinkedIn note) for one job.

Nothing here sends anything: files are written to disk for the user to review,
edit and submit themselves.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from jobhunt.generator.cv_pdf import render_cv_pdf
from jobhunt.generator.letters import (
    LETTER_MAX_WORDS,
    LETTER_MIN_WORDS,
    NOTE_MAX_CHARS,
    render_letter,
    render_linkedin_note,
)
from jobhunt.generator.llm import NullPolisher, Polisher, fact_guard
from jobhunt.generator.tailoring import TailoredProfile, tailor
from jobhunt.models import Job
from jobhunt.profile import Profile
from jobhunt.textutils import fold, guess_language, word_count

__all__ = ["PreparedApplication", "choose_language", "prepare_application", "slugify"]


class IncompleteProfileError(ValueError):
    """profile.yaml still contains TODO placeholders."""


@dataclass
class PreparedApplication:
    directory: Path
    lang: str
    files: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    letter_words: int = 0
    note_chars: int = 0


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")
    return slug[:max_len].rstrip("-") or "job"


def choose_language(job: Job, profile: Profile, requested: str | None = None) -> str:
    """Explicit choice > Spanish-language posting > profile default (first language)."""
    allowed = profile.preferences.application_languages or ["en"]
    if requested:
        return requested
    if "es" in allowed and guess_language(f"{job.title}\n{job.description}") == "es":
        return "es"
    return allowed[0]


def _facts_text(tp: TailoredProfile) -> str:
    """Plain-text dump of the candidate facts the polisher may use (and the guard allows)."""
    return yaml.safe_dump(
        {
            "name": tp.full_name,
            "summary": tp.summary,
            "education": [
                {"degree": str(e.degree), "institution": e.institution, "end": e.end,
                 "grade": e.grade, "short": e.short_name, "field": str(e.field_of_study)}
                for e in tp.education
            ],
            "experience": [
                {"role": i.title, "org": i.organization, "bullets": i.bullets}
                for i in tp.experience
            ],
            "projects": [
                {"name": i.title, "description": i.description, "bullets": i.bullets}
                for i in tp.projects
            ],
            "skills": tp.skills_highlighted + tp.skills_other,
            "languages": [
                f"{lang.language} {lang.level} {lang.certificate}" for lang in tp.languages
            ],
            "achievements": tp.achievements,
            "motivation": tp.motivation,
        },
        allow_unicode=True,
        sort_keys=False,
    )  # fmt: skip


def _polish(
    polisher: Polisher,
    text: str,
    *,
    kind: str,
    tp: TailoredProfile,
    limits: str,
    within_limits,  # noqa: ANN001 - Callable[[str], bool]
    warnings: list[str],
) -> str:
    if isinstance(polisher, NullPolisher):
        return text
    facts = _facts_text(tp)
    try:
        polished = polisher.polish(text, kind=kind, lang=tp.lang, facts=facts, limits=limits)
    except Exception as exc:  # noqa: BLE001 - any backend failure falls back to templates
        warnings.append(f"LLM polishing of the {kind} failed ({exc}); kept the template version")
        return text
    sources = f"{facts}\n{tp.job.title}\n{tp.job.company}\n{tp.job.description}"
    guard = fact_guard(text, polished, sources)
    if not guard.ok:
        warnings.append(
            f"LLM {kind} rejected by the fact guard ({'; '.join(guard.problems)}); "
            "kept the template version"
        )
        return text
    if not within_limits(polished):
        warnings.append(f"LLM {kind} broke the length limits; kept the template version")
        return text
    return polished


def _job_sheet(job: Job, score: int | None, explanation: str | None, record_id: int | None) -> str:
    lines = [
        f"# {job.title} — {job.company}",
        "",
        f"- Tracker id: {record_id if record_id is not None else '-'}",
        f"- Score: {score if score is not None else '-'}  ({explanation or 'not scored'})",
        f"- Location: {job.location or '-'} · workplace: {job.workplace_type} · "
        f"contract: {job.employment_type}",
        f"- Posted: {job.posted_at or '-'} · source: {job.source}",
        f"- Apply here (manually): {job.url}",
        "",
        "## Before you send",
        "",
        "- [ ] Read the CV and letter: every line must be true and defensible in an interview",
        "- [ ] Replace [Name] in the LinkedIn note with the recipient's name",
        "- [ ] Check the posting is still open and the requirements match",
        "- [ ] Submit yourself on the company site, then run "
        f"`jobhunt status {record_id if record_id is not None else '<id>'} applied`",
        "",
        "## Job description",
        "",
        job.description or "(no description)",
        "",
    ]
    return "\n".join(lines)


def prepare_application(
    job: Job,
    profile: Profile,
    out_dir: str | Path,
    *,
    lang: str | None = None,
    polisher: Polisher | None = None,
    record_id: int | None = None,
    score: int | None = None,
    explanation: str | None = None,
    today: dt.date | None = None,
    allow_placeholders: bool = False,
) -> PreparedApplication:
    holes = profile.placeholders()
    if holes and not allow_placeholders:
        raise IncompleteProfileError(
            "profile.yaml still has TODO placeholders: "
            + ", ".join(holes[:8])
            + (" ..." if len(holes) > 8 else "")
        )
    polisher = polisher or NullPolisher()
    lang = choose_language(job, profile, lang)
    tp = tailor(profile, job, lang, today=today)

    prefix = f"{record_id:04d}-" if record_id is not None else ""
    directory = Path(out_dir) / f"{prefix}{slugify(job.company, 20)}-{slugify(job.title)}"
    directory.mkdir(parents=True, exist_ok=True)
    result = PreparedApplication(directory=directory, lang=lang)
    result.warnings += [f"translation: {w}" for w in tp.warnings]

    letter = render_letter(tp)
    result.warnings += letter.warnings
    body = _polish(
        polisher,
        letter.body,
        kind="cover letter",
        tp=tp,
        limits=f"Keep it between {LETTER_MIN_WORDS} and {LETTER_MAX_WORDS} words.",
        within_limits=lambda t: LETTER_MIN_WORDS <= word_count(t) <= LETTER_MAX_WORDS,
        warnings=result.warnings,
    )
    letter_text = letter.text.replace(letter.body, body)
    result.letter_words = word_count(body)

    note, note_warnings = render_linkedin_note(tp)
    result.warnings += note_warnings
    note = _polish(
        polisher,
        note,
        kind="LinkedIn connection note",
        tp=tp,
        limits=f"Hard limit: {NOTE_MAX_CHARS} characters. Keep the [Name]/[Nombre] placeholder.",
        within_limits=lambda t: len(t) <= NOTE_MAX_CHARS,
        warnings=result.warnings,
    )
    result.note_chars = len(note)

    result.files["cv"] = render_cv_pdf(tp, directory / f"cv_{lang}.pdf")
    result.files["cover_letter"] = directory / f"cover_letter_{lang}.txt"
    result.files["cover_letter"].write_text(letter_text, encoding="utf-8")
    result.files["linkedin_note"] = directory / f"linkedin_note_{lang}.txt"
    result.files["linkedin_note"].write_text(note + "\n", encoding="utf-8")
    result.files["job"] = directory / "job.md"
    result.files["job"].write_text(_job_sheet(job, score, explanation, record_id), encoding="utf-8")
    if result.warnings:
        result.files["warnings"] = directory / "warnings.txt"
        result.files["warnings"].write_text("\n".join(result.warnings) + "\n", encoding="utf-8")
    return result
