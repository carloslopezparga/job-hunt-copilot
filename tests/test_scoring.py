from __future__ import annotations

import copy
import datetime as dt

import pytest

from jobhunt.models import Job
from jobhunt.profile import AvailabilityWindow
from jobhunt.scoring import (
    EXCLUDED_CAP,
    VISA_CAP,
    dedup_key,
    deduplicate,
    extract_periods,
    rank,
    score_job,
)
from jobhunt.sources import FixtureFetcher, discover, load_companies

TODAY = dt.date(2026, 10, 7)


def make_job(**overrides) -> Job:
    base = dict(
        source="greenhouse",
        external_id="1",
        company="Northwind Quant",
        company_slug="northwindquant",
        sector="quant_trading",
        title="Quantitative Research Intern - Summer 2027",
        url="https://example.test/1",
        location="Madrid, Spain",
        locations=["Madrid, Spain"],
        workplace_type="onsite",
        employment_type="internship",
        description="Summer 2027 internship. Python, statistics, probability, C++, pandas.",
        posted_at="2026-09-15",
    )
    base.update(overrides)
    return Job(**base)


@pytest.fixture
def fixture_jobs(fixtures_dir):
    companies = load_companies(fixtures_dir / "companies.sample.yaml")
    return discover(companies, FixtureFetcher(fixtures_dir)).jobs


# ------------------------------------------------------------------ components


def test_ideal_internship_scores_high(profile):
    result = score_job(make_job(), profile, TODAY)
    assert result.score >= 90
    assert result.excluded is None
    assert result.components["contract"] == 1.0
    assert result.components["role"] == 1.0
    assert result.components["dates"] == 1.0
    assert "summer 2027 fits" in result.explanation
    assert "\n" not in result.explanation  # one line


def test_full_time_scores_lower_than_internship(profile):
    intern = score_job(make_job(), profile, TODAY).score
    full_time = score_job(
        make_job(title="Quantitative Researcher", employment_type="full_time"), profile, TODAY
    ).score
    assert full_time < intern - 15


def test_excluded_keyword_caps_score(profile):
    result = score_job(make_job(title="Senior Quant Research Intern"), profile, TODAY)
    assert result.score <= EXCLUDED_CAP
    assert result.explanation.startswith("excluded: 'senior' in title")


def test_past_season_in_title_is_stale(profile):
    result = score_job(
        make_job(title="Quant Intern - Summer 2026", description="Summer 2026 programme"),
        profile,
        TODAY,
    )
    assert result.score <= EXCLUDED_CAP
    assert "already past" in result.explanation


def test_onsite_job_in_remote_only_window_penalised(profile):
    job = make_job(
        title="Quantitative Research Intern",
        description="Internship from November 2026 to February 2027, in office.",
    )
    result = score_job(job, profile, TODAY)
    assert result.components["dates"] == 1.0  # dates fit the academic-year window...
    assert result.components["remote"] == 0.0  # ...but that window is remote-only
    remote = score_job(
        make_job(description=job.description, workplace_type="remote"), profile, TODAY
    )
    assert remote.components["remote"] == 1.0
    assert remote.score > result.score


def test_undated_job_is_neutral_on_dates(profile):
    result = score_job(
        make_job(title="Quant Research Intern", description="Python"), profile, TODAY
    )
    assert result.components["dates"] == 0.5
    assert "dates not stated" in result.explanation


def test_sector_not_wanted(profile):
    p = copy.deepcopy(profile)
    p.preferences.sectors = ["ai_tech"]
    assert score_job(make_job(), p, TODAY).components["sector"] == 0.0


def test_location_tiers_and_visa(profile):
    def score(location, **kw):
        return score_job(make_job(location=location, locations=[], **kw), profile, TODAY)

    madrid = score("Madrid, Spain")
    barcelona = score("Barcelona, Spain")
    amsterdam = score("Amsterdam, Netherlands")
    tallinn = score("Tallinn, Estonia")
    seoul = score("Seoul")
    unknown_city = score("Springfield")
    london = score("London, United Kingdom")
    ny = score("New York, NY")

    assert madrid.location.tier == "preferred" and madrid.components["location"] == 1.0
    assert barcelona.location.tier == amsterdam.location.tier == "acceptable"
    assert tallinn.location.tier == "acceptable"  # EU country
    assert unknown_city.location.tier == "unknown"
    assert madrid.score > barcelona.score > unknown_city.score
    # outside every list and no visa problem: allowed but clearly lower
    zurich = score("Zurich, Switzerland")
    assert zurich.location.tier == "other"
    assert barcelona.score - zurich.score >= 15
    # visa needed: capped, never near the top
    for result in (london, ny, seoul):
        assert result.location.tier == "visa"
        assert result.score <= VISA_CAP
        assert "visa needed" in result.explanation
    assert "visa needed: GB" in london.explanation and "visa needed: US" in ny.explanation


def test_visa_is_not_needed_when_authorized(profile):
    p = copy.deepcopy(profile)
    p.personal.work_authorization = ["EU", "UK"]
    london = score_job(make_job(location="London, United Kingdom", locations=[]), p, TODAY)
    assert london.location.tier == "other" and london.score > VISA_CAP


def test_multi_location_job_counts_best_place(profile):
    job = make_job(location="New York, NY; London, UK; Madrid, Spain", locations=[])
    result = score_job(job, profile, TODAY)
    assert result.location.tier == "preferred"
    assert "Madrid, Spain (preferred, +2 more)" in result.explanation


def test_remote_eu_job_is_acceptable_and_remote(profile):
    job = make_job(location="Remote - EU", locations=[], workplace_type="unknown")
    result = score_job(job, profile, TODAY)
    assert result.location.tier == "acceptable"
    assert result.location.remote
    assert result.components["remote"] == 1.0
    us_remote = score_job(make_job(location="Remote - US", locations=[]), profile, TODAY)
    assert us_remote.location.tier == "visa" and us_remote.score <= VISA_CAP


def test_location_avoid_list(profile):
    p = copy.deepcopy(profile)
    p.preferences.locations.avoid = ["Madrid"]
    assert score_job(make_job(), p, TODAY).components["location"] < 0.2


def test_skills_component_counts_matches(profile):
    none = score_job(make_job(description="Summer 2027. Sales and marketing."), profile, TODAY)
    some = score_job(make_job(), profile, TODAY)
    assert none.components["skills"] == 0.0
    assert some.components["skills"] == 1.0
    assert "skills: Python" in some.explanation


def test_weights_are_configurable(profile):
    job = make_job(sector="consulting")  # unwanted sector => sector component is 0
    p = copy.deepcopy(profile)
    p.preferences.sectors = ["ai_tech"]
    p.scoring.weights = {"sector": 0}
    ignore_sector = score_job(job, p, TODAY).score
    p.scoring.weights = {"sector": 60}
    sector_heavy = score_job(job, p, TODAY).score
    assert ignore_sector > sector_heavy
    # unknown keys are ignored, defaults fill the gaps
    p.scoring.weights = {"bogus": 1000}
    assert score_job(job, p, TODAY).score == score_job(job, profile_with_sectors(p), TODAY).score


def profile_with_sectors(p):
    q = copy.deepcopy(p)
    q.scoring.weights = {}
    return q


def test_no_availability_configured(profile):
    p = copy.deepcopy(profile)
    p.preferences.availability = []
    result = score_job(make_job(), p, TODAY)
    assert 0 <= result.score <= 100


# ----------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    "text, label, start, end",
    [
        ("Summer 2027 internship", "summer 2027", "2027-06-01", "2027-09-30"),
        ("Prácticas verano de 2027", "verano 2027", "2027-06-01", "2027-09-30"),
        ("starting January 2027", "january 2027", "2027-01-01", "2027-01-31"),
        ("incorporación en junio de 2027", "junio 2027", "2027-06-01", "2027-06-30"),
        ("Off-Cycle 2027", "off-cycle 2027", "2027-01-01", "2027-12-31"),
        ("Start date: May 2027", "may 2027", "2027-05-01", "2027-05-31"),
        ("Start ASAP", "ASAP", "2026-10-07", "2026-12-06"),
        ("Quant Intern 2027", "2027", "2027-01-01", "2027-12-31"),
    ],
)
def test_extract_periods(text, label, start, end):
    periods = extract_periods(text, TODAY)
    assert periods, text
    p = periods[0]
    assert (p.label, p.start.isoformat(), p.end.isoformat()) == (label, start, end)


def test_extract_periods_ignores_plain_text():
    assert extract_periods("We may hire engineers. Founded by 20 people.", TODAY) == []


def test_window_overlap():
    w = AvailabilityWindow("x", dt.date(2027, 6, 1), dt.date(2027, 12, 31))
    assert w.overlaps(dt.date(2027, 5, 1), dt.date(2027, 6, 1))
    assert not w.overlaps(dt.date(2027, 1, 1), dt.date(2027, 5, 31))


# ----------------------------------------------------------------------- dedup


def test_dedup_key_ignores_punctuation_and_case():
    a = make_job(title="ML Research Intern, Summer 2027", location="Zurich, Switzerland")
    b = make_job(title="ml research intern - summer 2027", location="Zurich", external_id="2")
    assert dedup_key(a) == dedup_key(b)


def test_deduplicate_keeps_latest_and_same_uid_once():
    old = make_job(external_id="1", posted_at="2026-09-01")
    new = make_job(external_id="2", posted_at="2026-09-30", title=old.title + " ")
    other_city = make_job(external_id="3", location="Amsterdam, Netherlands")
    result = deduplicate([old, old, new, other_city])
    assert [j.external_id for j in result] == ["2", "3"]


def test_rank_on_fixtures(profile, fixture_jobs):
    ranked = rank(fixture_jobs, profile, TODAY)
    assert len(ranked) == len(fixture_jobs) - 1  # one cross-posted Helix duplicate
    scores = [r.score for _, r in ranked]
    assert scores == sorted(scores, reverse=True)
    top_titles = [j.title for j, r in ranked if r.score >= 65]
    assert "Data Science Intern (Remote)" in top_titles  # Remote - Europe
    assert "Prácticas Strategy & Operations" in top_titles  # Madrid
    # London internship needs a UK visa for an EU-only candidate: capped, out of the top
    london = next(r for j, r in ranked if j.title == "Quantitative Research Intern - Summer 2027")
    assert london.score <= VISA_CAP and london.location.tier == "visa"
    # Madrid (preferred) outranks Amsterdam (acceptable) even with fewer skill matches
    order = [j.title for j, _ in ranked]
    assert order.index("Prácticas Strategy & Operations") < order.index(
        "Software Engineer Intern (Off-Cycle 2027)"
    )
    bottom = {j.title for j, r in ranked if r.score <= EXCLUDED_CAP}
    assert bottom == {
        "Senior Quantitative Developer",
        "Staff Machine Learning Engineer",
        "Quantitative Trading Intern - Summer 2026",
    }
    helix = [j for j, _ in ranked if j.company == "Helix AI" and "Research" in j.title]
    assert [j.external_id[-1] for j in helix] == ["5"]  # most recent copy kept
