"""Render the shortlist produced by `jobhunt run` (markdown export, short summaries)."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from jobhunt.tracker import JobRecord

EXCERPT_CHARS = 400


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """First ``limit`` characters of a description, whitespace collapsed, cut at a word."""
    flat = re.sub(r"\s+", " ", (text or "").replace("- ", " ")).strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0].rstrip(",.;:")
    return f"{cut}…"


def _md(text: str) -> str:
    """Escape characters that would break markdown tables/headings."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(
    records: list[JobRecord],
    generated_at: dt.datetime,
    threshold: float,
    prepared: dict[int, Path] | None = None,
) -> str:
    prepared = prepared or {}
    lines = [
        f"# Job shortlist — {generated_at:%Y-%m-%d %H:%M}",
        "",
        f"{len(records)} new offer(s) since the last run with score ≥ {threshold:g}, "
        "after the location filters in profile.yaml. Nothing has been sent: review and "
        "apply yourself.",
        "",
    ]
    if records:
        lines += ["| # | Score | Company | Title | Location |", "|---|---|---|---|---|"]
        for r in records:
            lines.append(
                f"| {r.id} | {r.score} | {_md(r.job.company)} | {_md(r.job.title)} | "
                f"{_md(r.job.location or r.job.workplace_type)} |"
            )
        lines.append("")
    for r in records:
        lines += [
            f"## {r.score} · {r.job.company} — {r.job.title}",
            "",
            f"- **Location:** {r.job.location or '-'} ({r.job.workplace_type})",
            f"- **Score:** {r.score}/100",
            f"- **Why:** {r.score_explanation or '-'}",
            f"- **Link:** {r.job.url}",
            f"- **Tracker id:** {r.id}",
        ]
        if r.id in prepared:
            lines.append(f"- **Prepared material:** `{prepared[r.id]}`")
        lines += ["", f"> {excerpt(r.job.description) or '(no description)'}", ""]
    return "\n".join(lines).rstrip() + "\n"


def write_markdown(path: str | Path, content: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
