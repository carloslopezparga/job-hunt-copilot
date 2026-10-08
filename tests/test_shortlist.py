from __future__ import annotations

import datetime as dt
from pathlib import Path

from jobhunt.models import Job
from jobhunt.shortlist import excerpt, render_markdown
from jobhunt.tracker import Tracker


def test_excerpt():
    assert excerpt("short\n\n- text") == "short text"
    long = "word " * 200
    out = excerpt(long, 50)
    assert len(out) <= 51 and out.endswith("…")


def test_render_markdown(tmp_path):
    with Tracker(tmp_path / "t.db") as t:
        t.upsert_jobs(
            [
                Job(
                    "lever",
                    "1",
                    "Pay|lane",
                    "paylane",
                    "Data Intern",
                    "https://x/1",
                    location="Madrid, Spain",
                    description="We use Python. " * 60,
                )
            ]
        )
        t.set_score(1, 91, "internship ✓ · Madrid, Spain (preferred)")
        records = t.jobs()
    md = render_markdown(records, dt.datetime(2026, 10, 8, 9, 30), 65,
                         {1: Path("output/0001-paylane")})  # fmt: skip
    assert md.startswith("# Job shortlist — 2026-10-08 09:30")
    assert "| 1 | 91 | Pay\\|lane | Data Intern | Madrid, Spain |" in md
    assert "## 91 · Pay|lane — Data Intern" in md
    assert "- **Why:** internship ✓ · Madrid, Spain (preferred)" in md
    assert "- **Link:** https://x/1" in md
    assert "`output/0001-paylane`" in md
    assert "> We use Python." in md and "…" in md


def test_render_empty():
    md = render_markdown([], dt.datetime(2026, 10, 8), 65)
    assert "0 new offer(s)" in md
