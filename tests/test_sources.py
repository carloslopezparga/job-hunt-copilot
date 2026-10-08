from __future__ import annotations

import json

import pytest

from jobhunt.models import Company
from jobhunt.sources import (
    AshbySource,
    FixtureFetcher,
    GreenhouseSource,
    LeverSource,
    SourceError,
    discover,
    load_companies,
)
from jobhunt.sources.base import iso_date, normalize_employment, normalize_workplace

NORTHWIND = Company("Northwind Quant", "greenhouse", "northwindquant", "quant_trading")
PAYLANE = Company("Paylane", "lever", "paylane", "fintech", region="eu")
HELIX = Company("Helix AI", "ashby", "helixai", "ai_tech")


class RecordingFetcher:
    """Returns a canned payload and records the URL requested."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def get_json(self, url, *, key):
        self.calls.append((url, key))
        return self.payload


@pytest.fixture
def fetcher(fixtures_dir):
    return FixtureFetcher(fixtures_dir)


# ------------------------------------------------------------------ greenhouse


def test_greenhouse_parses_jobs(fetcher):
    jobs = GreenhouseSource(fetcher).fetch(NORTHWIND)
    assert len(jobs) == 5
    job = jobs[0]
    assert job.uid == "greenhouse:northwindquant:7012345"
    assert job.title == "Quantitative Research Intern - Summer 2027"
    assert job.company == "Northwind Quant"
    assert job.sector == "quant_trading"
    assert job.location == "London, United Kingdom"
    assert job.employment_type == "internship"
    assert job.workplace_type == "unknown"
    assert job.department == "Quantitative Research"
    assert job.posted_at == "2026-09-15"
    assert job.url.startswith("https://boards.greenhouse.io/")
    # HTML-escaped content is decoded into readable text with bullet points.
    assert "<p>" not in job.description and "&lt;" not in job.description
    assert "- Analyse large financial time series datasets in Python" in job.description


def test_greenhouse_employment_from_metadata_and_title(fetcher):
    jobs = {j.external_id: j for j in GreenhouseSource(fetcher).fetch(NORTHWIND)}
    assert jobs["7011111"].employment_type == "full_time"
    assert jobs["7013333"].employment_type == "unknown"  # no metadata, non-intern title


def test_greenhouse_url():
    fetcher = RecordingFetcher({"jobs": []})
    GreenhouseSource(fetcher).fetch(NORTHWIND)
    assert fetcher.calls == [
        (
            "https://boards-api.greenhouse.io/v1/boards/northwindquant/jobs?content=true",
            "greenhouse_northwindquant",
        )
    ]


# ----------------------------------------------------------------------- lever


def test_lever_parses_jobs(fetcher):
    jobs = LeverSource(fetcher).fetch(PAYLANE)
    assert [j.external_id[-4:] for j in jobs] == ["aaaa", "bbbb", "cccc"]
    intern = jobs[0]
    assert intern.workplace_type == "remote"
    assert intern.is_remote is True
    assert intern.employment_type == "internship"
    assert intern.location == "Remote - Europe"
    assert intern.department == "Data / Data Science"
    assert intern.posted_at == "2026-09-21"
    # lists[] sections are folded into the description
    assert "What you'll do" in intern.description
    assert "- Build fraud-detection features with Python and SQL" in intern.description
    backend = jobs[1]
    assert backend.workplace_type == "hybrid"
    assert backend.employment_type == "full_time"
    assert backend.locations == ["Paris, France", "Berlin, Germany"]
    assert jobs[2].workplace_type == "onsite"


def test_lever_eu_and_global_hosts():
    fetcher = RecordingFetcher([])
    LeverSource(fetcher).fetch(PAYLANE)
    LeverSource(fetcher).fetch(Company("X", "lever", "xco", "fintech"))
    assert fetcher.calls[0][0] == "https://api.eu.lever.co/v0/postings/paylane?mode=json"
    assert fetcher.calls[1][0] == "https://api.lever.co/v0/postings/xco?mode=json"


def test_lever_rejects_non_list_payload():
    with pytest.raises(SourceError, match="unexpected lever payload"):
        LeverSource(RecordingFetcher({"ok": False})).fetch(PAYLANE)


# ----------------------------------------------------------------------- ashby


def test_ashby_parses_and_skips_unlisted(fetcher):
    jobs = AshbySource(fetcher).fetch(HELIX)
    ids = [j.external_id[-1] for j in jobs]
    assert ids == ["1", "2", "3", "5"]  # 4 is isListed: false
    research = jobs[0]
    assert research.employment_type == "internship"
    assert research.workplace_type == "onsite"
    assert research.locations == ["Zurich, Switzerland", "London, United Kingdom"]
    assert research.department == "Research / Applied ML"
    assert "Summer 2027" in research.description
    remote = jobs[1]
    assert remote.workplace_type == "remote"
    staff = jobs[2]
    assert staff.employment_type == "full_time"
    assert staff.workplace_type == "hybrid"


def test_ashby_url():
    fetcher = RecordingFetcher({"jobs": []})
    AshbySource(fetcher).fetch(HELIX)
    assert fetcher.calls[0][0] == (
        "https://api.ashbyhq.com/posting-api/job-board/helixai?includeCompensation=true"
    )


def test_ashby_malformed_payload_is_source_error():
    with pytest.raises(SourceError):
        AshbySource(RecordingFetcher({"jobs": [{"no_title": True}]})).fetch(HELIX)


# ------------------------------------------------------------------- discovery


def test_discover_collects_errors_without_aborting(fixtures_dir, fetcher):
    companies = load_companies(fixtures_dir / "companies.sample.yaml")
    result = discover(companies, fetcher)
    assert result.counts == {"Northwind Quant": 5, "Paylane": 3, "Helix AI": 4}
    assert set(result.errors) == {"Ghost Corp"}
    assert len(result.jobs) == 12


def test_repo_companies_file_is_valid(root):
    companies = load_companies(root / "companies.yaml")
    assert len(companies) >= 30
    assert {c.ats for c in companies} == {"greenhouse", "lever", "ashby"}
    assert len({(c.ats, c.slug) for c in companies}) == len(companies)


def test_load_companies_validates(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text(
        json.dumps(
            {"companies": [{"name": "X", "ats": "workday", "slug": "x", "sector": "fintech"}]}
        )
    )
    with pytest.raises(ValueError, match="unknown ats"):
        load_companies(bad)


# --------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-09-25T14:03:11.000+00:00", "2026-09-25"),
        ("2026-09-28T10:15:02-04:00", "2026-09-28"),
        ("2026-09-28T10:15:02Z", "2026-09-28"),
        (1790000000000, "2026-09-21"),
        ("garbage", None),
        (None, None),
    ],
)
def test_iso_date(value, expected):
    assert iso_date(value) == expected


@pytest.mark.parametrize(
    "explicit, title, expected",
    [
        ("Intern", "Analyst", "internship"),
        ("FullTime", "Software Engineer", "full_time"),
        ("Full-time", "Summer Analyst 2027", "internship"),
        (None, "Werkstudent Data Science", "internship"),
        ("", "Internal Audit Manager", "unknown"),
        ("Part-time", "Data Engineer", "part_time"),
    ],
)
def test_normalize_employment(explicit, title, expected):
    assert normalize_employment(explicit, title) == expected


@pytest.mark.parametrize(
    "explicit, texts, expected",
    [
        ("Remote", (), "remote"),
        ("OnSite", (), "onsite"),
        ("hybrid", (), "hybrid"),
        (None, ("Data Intern", "Remote - EU"), "remote"),
        (None, ("Data Intern", "London"), "unknown"),
        (None, ("Data Intern", "Hybrid - Madrid"), "hybrid"),
        (None, ("Data Intern", "Madrid (híbrido)"), "hybrid"),
        (None, ("Data Intern", "Remote - Spain; Hybrid - Madrid"), "remote"),
    ],
)
def test_normalize_workplace(explicit, texts, expected):
    assert normalize_workplace(explicit, *texts) == expected
