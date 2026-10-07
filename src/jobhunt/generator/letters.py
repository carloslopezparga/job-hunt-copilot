"""Cover letters (150-200 words) and LinkedIn notes (<= 300 chars) from Jinja2 templates.

Length is controlled by *removing or adding sentences built from profile.yaml*,
never by padding with invented content. If the profile is too thin to reach the
minimum length, the shorter letter is kept and a warning asks the user to add
real material to their profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jinja2 import Environment, PackageLoader, StrictUndefined

from jobhunt.generator.tailoring import (
    TailoredProfile,
    format_long_month,
    human_join,
    language_name,
    lower_first,
)
from jobhunt.profile import localize
from jobhunt.textutils import word_count

LETTER_MIN_WORDS = 150
LETTER_MAX_WORDS = 200
NOTE_MAX_CHARS = 300
MAX_LEVEL = 5

_env = Environment(
    loader=PackageLoader("jobhunt.generator", "templates"),
    undefined=StrictUndefined,
    keep_trailing_newline=False,
    autoescape=False,  # plain-text output
)

_GREETING = {
    "en": "Dear {company} Hiring Team,",
    "es": "Estimado equipo de selección de {company}:",
}
_SIGNOFF = {"en": "Kind regards,", "es": "Atentamente,"}


@dataclass
class Letter:
    body: str
    text: str  # greeting + body + sign-off
    words: int
    level: int
    warnings: list[str] = field(default_factory=list)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +([.,;:])", r"\1", text)
    text = re.sub(r"\.\.+", ".", text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "\n\n".join(paragraphs)


def _sentence(bullet: str, protected: set[str]) -> str:
    """Turn a CV bullet into a clause: strip trailing period, lower first letter safely."""
    bullet = bullet.strip().rstrip(".")
    first = re.split(r"[\s\-/]", bullet, maxsplit=1)[0].lower()
    return bullet if first in protected else lower_first(bullet)


def _language_phrase(tp: TailoredProfile) -> str:
    parts = []
    for lang in tp.languages:
        level = lang.level
        if level.lower() == "native":
            level = "nativo" if tp.lang == "es" else "native"
        detail = f"{level}, {lang.certificate}" if lang.certificate else level
        parts.append(f"{language_name(lang.language, tp.lang)} ({detail})")
    return human_join(parts, tp.lang)


def letter_context(tp: TailoredProfile) -> dict:
    lang = tp.lang
    protected = {s.lower() for s in [*tp.skills_highlighted, *tp.skills_other, *tp.keywords]}
    edu = tp.education[0]
    grad = ""
    if edu.end and re.fullmatch(r"\d{4}-\d{2}", edu.end):
        grad = edu.end[:4]
    # Letters never mix languages: sentences without a translation are left out.
    ranked_exp = sorted(tp.experience, key=lambda i: i.relevance, reverse=True)
    usable_exp = [e for e in ranked_exp if e.translated_bullets()]
    exp = usable_exp[0] if usable_exp else None
    others = usable_exp[1:]
    usable_projects = [p for p in tp.projects if p.translated_bullets()]
    project = usable_projects[0] if usable_projects else None
    skills = tp.skills_highlighted[:5]
    return {
        "level": MAX_LEVEL,
        "job": tp.job,
        "degree": localize(edu.degree, lang),
        "institution": edu.institution,
        "grad": grad,
        "grade": edu.grade,
        "exp": exp,
        "exp_bullets": [_sentence(b, protected) for b in (exp.translated_bullets() if exp else [])],
        "other_exp": others[0] if others else None,
        "other_exp_bullet": (
            _sentence(others[0].translated_bullets()[0], protected) if others else ""
        ),
        "project": project,
        "project_bullets": [
            _sentence(b, protected) for b in (project.translated_bullets() if project else [])
        ],
        "skills": human_join(skills, lang),
        "languages": _language_phrase(tp),
        "achievement": (
            tp.achievements_translated[0].rstrip(".") if tp.achievements_translated else ""
        ),
        "motivation": tp.motivation if tp.motivation_translated else "",
        "window": tp.window,
        "window_start": format_long_month(tp.window.start, lang) if tp.window else "",
        "window_end": format_long_month(tp.window.end, lang) if tp.window else "",
    }


def render_letter(tp: TailoredProfile) -> Letter:
    template = _env.get_template(f"cover_letter.{tp.lang}.j2")
    context = letter_context(tp)
    candidates: list[tuple[int, str, int]] = []
    for level in range(1, MAX_LEVEL + 1):
        body = _clean(template.render(**{**context, "level": level}))
        candidates.append((level, body, word_count(body)))

    within = [c for c in candidates if LETTER_MIN_WORDS <= c[2] <= LETTER_MAX_WORDS]
    warnings: list[str] = []
    if within:
        level, body, words = within[-1]  # richest letter that fits
    else:
        short_enough = [c for c in candidates if c[2] <= LETTER_MAX_WORDS]
        level, body, words = short_enough[-1] if short_enough else candidates[0]
        if words < LETTER_MIN_WORDS:
            warnings.append(
                f"cover letter has {words} words (< {LETTER_MIN_WORDS}): the profile does not "
                "contain enough relevant material. Add real bullets/projects/motivations to "
                "profile.yaml rather than padding the letter."
            )
        else:
            warnings.append(
                f"cover letter has {words} words (> {LETTER_MAX_WORDS}): shorten long bullets "
                "or motivations in profile.yaml, or trim it by hand."
            )
    greeting = _GREETING[tp.lang].format(company=tp.job.company)
    text = f"{greeting}\n\n{body}\n\n{_SIGNOFF[tp.lang]}\n{tp.full_name}\n"
    return Letter(body=body, text=text, words=words, level=level, warnings=warnings)


def render_linkedin_note(tp: TailoredProfile) -> tuple[str, list[str]]:
    lang = tp.lang
    edu = tp.education[0]
    field_name = localize(edu.field_of_study, lang) or localize(edu.degree, lang)
    exp = tp.top_experience
    if exp:
        highlight = (
            f"experiencia relevante: {exp.title} en {exp.organization}"
            if lang == "es"
            else f"relevant experience: {exp.title} at {exp.organization}"
        )
    else:
        highlight = ""
    context = {
        "job": tp.job,
        "first_name": tp.first_name,
        "field": field_name,
        "field_article": "an" if field_name[:1].lower() in "aeiou" else "a",
        "school": edu.short_name or edu.institution,
        "highlight": highlight,
    }
    template = _env.get_template(f"linkedin_note.{lang}.j2")
    notes = [_clean(template.render(**context, level=lvl)) for lvl in (3, 2, 1)]
    for note in notes:
        if len(note) <= NOTE_MAX_CHARS:
            return note, []
    return notes[-1], [
        f"LinkedIn note is {len(notes[-1])} characters (limit {NOTE_MAX_CHARS}); shorten it by hand"
    ]
