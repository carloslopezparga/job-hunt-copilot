"""Scoring rules checked against REAL postings downloaded from the public board APIs
(tests/fixtures/real_offers.json, fetched 2026-10-08). No network is used here."""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json

import pytest

from jobhunt.models import Job
from jobhunt.profile import Language
from jobhunt.scoring import HARD_CAP, assess_location, score_job

TODAY = dt.date(2026, 10, 8)


@pytest.fixture(scope="module")
def offers():
    from tests.conftest import FIXTURES

    raw = json.loads((FIXTURES / "real_offers.json").read_text(encoding="utf-8"))["offers"]
    return {name: Job.from_dict(data) for name, data in raw.items()}


def at(job: Job, location: str, **changes) -> Job:
    """The same real posting, moved somewhere else."""
    return dataclasses.replace(job, location=location, locations=[location], **changes)


def score(job, profile):
    return score_job(job, profile, TODAY)


# ------------------------------------------------------------------ location levels


def test_madrid_beats_every_other_place_by_a_wide_margin(offers, profile):
    base = offers["imc_macro_analyst_intern_amsterdam"]
    places = ["Madrid, Spain", "Barcelona, Spain", "Remote - Spain", "Amsterdam, Netherlands"]
    scores = [score(at(base, place), profile).score for place in places]
    assert scores == sorted(scores, reverse=True) and len(set(scores)) == 4, scores
    madrid, _, _, amsterdam = scores
    assert madrid - amsterdam >= 20  # used to be ~5%


def test_levels_are_reported(offers, profile):
    base = offers["imc_macro_analyst_intern_amsterdam"]
    levels = {
        place: assess_location(at(base, place), profile).level
        for place in ["Madrid", "Barcelona", "Remote - Spain", "Remote - EU", "Berlin", "Zurich"]
    }
    assert levels == {
        "Madrid": "home", "Barcelona": "country", "Remote - Spain": "remote",
        "Remote - EU": "remote", "Berlin": "abroad", "Zurich": "abroad",
    }  # fmt: skip


def test_places_outside_free_movement_rank_last(offers, profile):
    base = offers["imc_macro_analyst_intern_amsterdam"]
    profile = copy.deepcopy(profile)
    profile.personal.work_authorization = ["EU", "US"]
    new_york = score(at(base, "New York, NY"), profile)
    assert new_york.location.level == "other"
    assert new_york.score < score(at(base, "Amsterdam, Netherlands"), profile).score


def test_real_madrid_internship_outranks_real_amsterdam_internship(offers, profile):
    madrid = score(offers["prophero_strategy_intern_madrid"], profile)
    amsterdam = score(offers["imc_macro_analyst_intern_amsterdam"], profile)
    assert madrid.location.level == "home" and amsterdam.location.level == "abroad"
    assert madrid.score > amsterdam.score + 10


def test_long_place_list_is_not_madrid(offers, profile):
    job = offers["squarepoint_desk_quant_many_places"]
    many = assess_location(job, profile)
    assert many.many and many.tier == "acceptable" and many.level == "country"
    assert "not specific" in many.label()
    short = assess_location(at(job, "London, Madrid"), profile)
    assert not short.many and short.tier == "preferred" and short.level == "home"


# --------------------------------------------------------------- availability (hard)


def test_onsite_internship_starting_during_the_course_is_capped(offers, profile):
    madrid = score(offers["datadog_swe_intern_madrid"], profile)  # on site, starts Feb 2027
    assert madrid.score <= HARD_CAP
    assert any("remote only" in reason for reason in madrid.caps)
    assert "max 40" in madrid.explanation


def test_same_internship_remote_or_in_summer_is_not_capped(offers, profile):
    madrid = offers["datadog_swe_intern_madrid"]
    remote = score(dataclasses.replace(madrid, workplace_type="remote"), profile)
    assert not remote.caps
    summer = score(offers["datadog_swe_intern_paris"], profile)  # June 2027 start
    assert not summer.caps and summer.score > HARD_CAP


# ----------------------------------------------------------------------- contract


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("prima_data_scientist_madrid", "internship"),  # contract not stated, 3+ years asked
        ("imc_graduate_quant_amsterdam", "new-grad"),
        ("fever_strategy_ops_analyst_madrid", "internship"),
        ("squarepoint_desk_quant_many_places", "full-time"),
    ],
)
def test_non_internships_are_capped_when_the_profile_wants_an_internship(
    offers, profile, name, reason
):
    result = score(offers[name], profile)
    assert result.score <= HARD_CAP, result.explanation
    assert any(reason in c for c in result.caps)


def test_full_time_is_fine_if_the_profile_accepts_it(offers, profile):
    profile = copy.deepcopy(profile)
    profile.preferences.contract_types = ["internship", "full_time"]
    result = score(offers["squarepoint_desk_quant_many_places"], profile)
    assert not any("full-time" in c for c in result.caps)


# ------------------------------------------------------------------------ language


@pytest.mark.parametrize(
    ("name", "language"),
    [
        ("n26_italian_fluent_madrid", "Italian"),
        ("databricks_portuguese_speaking_madrid", "Portuguese"),
        ("aircall_french_market_paris", "French"),
        ("raisin_dutch_market_berlin", "Dutch"),
    ],
)
def test_required_language_missing_from_profile_caps_the_score(offers, profile, name, language):
    result = score(offers[name], profile)
    assert result.score <= HARD_CAP
    assert any(f"requires {language}" in c for c in result.caps), result.caps


def test_language_in_profile_at_b2_or_better_lifts_the_cap(offers, profile):
    job = offers["aircall_french_market_paris"]
    assert any("French" in c for c in score(job, profile).caps)  # profile has French B1 only
    profile = copy.deepcopy(profile)
    profile.languages = [Language("French", "C1"), *profile.languages[:2]]
    assert not any("French" in c for c in score(job, profile).caps)


def test_language_cap_ignores_unfilled_template_levels(offers):
    from jobhunt.profile import load_profile
    from tests.conftest import ROOT

    template = load_profile(ROOT / "profile.template.yaml")  # English level is still TODO
    assert not any("English" in c for c in score(offers["datadog_swe_intern_paris"], template).caps)


# -------------------------------------------------------------------------- roles


def test_software_engineering_intern_matches_software_engineer(offers, profile):
    result = score(offers["datadog_swe_intern_paris"], profile)
    assert "role 'software engineer' ✓" in result.explanation
    assert result.components["role"] == 1.0
