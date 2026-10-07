"""Render a tailored one-page CV to PDF with reportlab.

Layout: header, summary, education, experience, projects, skills, languages,
achievements. Job-relevant keywords found in the candidate's own bullets are
set in bold; skills that the job mentions are listed first and bolded.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from jobhunt.generator.tailoring import (
    TailoredItem,
    TailoredProfile,
    date_range,
    language_name,
)
from jobhunt.profile import localize

ACCENT = colors.HexColor("#1F3A5F")

_HEADINGS = {
    "en": {
        "summary": "Profile",
        "education": "Education",
        "experience": "Experience",
        "projects": "Projects",
        "skills": "Skills",
        "languages": "Languages",
        "achievements": "Achievements",
        "expected": "expected",
        "grade": "Grade",
        "native": "Native",
    },
    "es": {
        "summary": "Perfil",
        "education": "Formación",
        "experience": "Experiencia",
        "projects": "Proyectos",
        "skills": "Habilidades",
        "languages": "Idiomas",
        "achievements": "Logros",
        "expected": "prevista",
        "grade": "Nota media",
        "native": "Nativo",
    },
}


def _safe(text: str) -> str:
    """Escape for reportlab mini-HTML and drop characters the base-14 fonts cannot draw."""
    text = escape(text or "")
    return text.encode("cp1252", errors="replace").decode("cp1252")


def highlight(text: str, keywords: list[str]) -> str:
    """Escape ``text`` and wrap whole-word keyword matches in <b>…</b>."""
    safe = _safe(text)
    terms = sorted({k for k in keywords if k}, key=len, reverse=True)
    if not terms:
        return safe
    pattern = re.compile(
        r"(?<![\w])(" + "|".join(re.escape(_safe(t)) for t in terms) + r")(?![\w])",
        re.IGNORECASE,
    )
    return pattern.sub(r"<b>\1</b>", safe)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    body = ParagraphStyle("body", parent=base, fontName="Helvetica", fontSize=9.3, leading=12)
    return {
        "name": ParagraphStyle(
            "name", parent=body, fontName="Helvetica-Bold", fontSize=18, leading=22,
            textColor=ACCENT,
        ),
        "contact": ParagraphStyle("contact", parent=body, fontSize=8.8, textColor=colors.grey),
        "heading": ParagraphStyle(
            "heading", parent=body, fontName="Helvetica-Bold", fontSize=10.5, leading=13,
            textColor=ACCENT, spaceBefore=7, spaceAfter=1,
        ),
        "body": body,
        "item": ParagraphStyle("item", parent=body, fontName="Helvetica-Bold"),
        "date": ParagraphStyle("date", parent=body, alignment=TA_RIGHT, textColor=colors.grey),
        "bullet": ParagraphStyle("bullet", parent=body, leftIndent=10, bulletIndent=2),
    }  # fmt: skip


def _heading(title: str, s: dict[str, ParagraphStyle]) -> list:
    return [
        Paragraph(_safe(title.upper()), s["heading"]),
        HRFlowable(width="100%", thickness=0.6, color=ACCENT, spaceAfter=3),
    ]


def _row(left: str, right: str, s: dict[str, ParagraphStyle], width: float) -> Table:
    table = Table(
        [[Paragraph(left, s["item"]), Paragraph(_safe(right), s["date"])]],
        colWidths=[width * 0.64, width * 0.36],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _item_block(
    item: TailoredItem, tp: TailoredProfile, s: dict[str, ParagraphStyle], width: float
) -> KeepTogether:
    lang = tp.lang
    if item.organization:
        left = f"{_safe(item.title)} — {_safe(item.organization)}"
        right = date_range(item.start, item.end, lang)
        if item.location:
            right = f"{right} · {item.location}" if right else item.location
    else:
        left = _safe(item.title)
        if item.url:
            left += f' <font size="8" color="grey">({_safe(item.url)})</font>'
        right = date_range(item.start, item.end, lang)
    flow: list = [_row(left, right, s, width)]
    if item.description:
        flow.append(Paragraph(f"<i>{highlight(item.description, tp.keywords)}</i>", s["body"]))
    for bullet in item.bullets:
        flow.append(Paragraph(highlight(bullet, tp.keywords), s["bullet"], bulletText="•"))
    flow.append(Spacer(1, 3))
    return KeepTogether(flow)


def build_cv_story(tp: TailoredProfile, width: float) -> list:
    s = _styles()
    h = _HEADINGS.get(tp.lang, _HEADINGS["en"])
    story: list = [Paragraph(_safe(tp.full_name), s["name"])]
    contact = [tp.email, tp.phone, tp.location, *tp.links]
    story.append(Paragraph(" · ".join(_safe(c) for c in contact if c), s["contact"]))

    if tp.summary:
        story += _heading(h["summary"], s)
        story.append(Paragraph(highlight(tp.summary, tp.keywords), s["body"]))

    story += _heading(h["education"], s)
    for edu in tp.education:
        degree = localize(edu.degree, tp.lang)
        right = date_range(edu.start, edu.end, tp.lang)
        if edu.expected and right:
            right += f" ({h['expected']})"
        flow: list = [_row(f"{_safe(degree)} — {_safe(edu.institution)}", right, s, width)]
        if edu.grade:
            flow.append(Paragraph(f"{h['grade']}: {_safe(edu.grade)}", s["body"]))
        for item in edu.highlights:
            flow.append(
                Paragraph(
                    highlight(localize(item, tp.lang), tp.keywords), s["bullet"], bulletText="•"
                )
            )
        story.append(KeepTogether(flow))

    if tp.experience:
        story += _heading(h["experience"], s)
        story += [_item_block(item, tp, s, width) for item in tp.experience]

    if tp.projects:
        story += _heading(h["projects"], s)
        story += [_item_block(item, tp, s, width) for item in tp.projects]

    skills = [f"<b>{_safe(x)}</b>" for x in tp.skills_highlighted]
    skills += [_safe(x) for x in tp.skills_other]
    if skills:
        story += _heading(h["skills"], s)
        story.append(Paragraph(", ".join(skills), s["body"]))

    if tp.languages:
        story += _heading(h["languages"], s)
        parts = []
        for lang in tp.languages:
            level = h["native"] if lang.level.lower() == "native" else lang.level
            name = language_name(lang.language, tp.lang)
            text = f"<b>{_safe(name[:1].upper() + name[1:])}</b>: {_safe(level)}"
            if lang.certificate:
                text += f" ({_safe(lang.certificate)})"
            parts.append(text)
        story.append(Paragraph(" · ".join(parts), s["body"]))

    if tp.achievements:
        story += _heading(h["achievements"], s)
        for a in tp.achievements:
            story.append(Paragraph(highlight(a, tp.keywords), s["bullet"], bulletText="•"))
    return story


def render_cv_pdf(tp: TailoredProfile, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    margin = 1.5 * cm
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=1.3 * cm,
        bottomMargin=1.3 * cm,
        title=f"CV — {tp.full_name}",
        author=tp.full_name,
        subject=f"{tp.job.title} at {tp.job.company}",
    )
    frame_padding = 12  # SimpleDocTemplate frames have 6pt padding on each side
    doc.build(build_cv_story(tp, doc.width - frame_padding))
    return path
