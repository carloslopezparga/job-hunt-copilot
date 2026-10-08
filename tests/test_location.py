"""Location parsing and tiers.

Without network access in the development environment these cases are written from
the location formats the user reported seeing in real Greenhouse/Lever/Ashby data
("Madrid", "Hybrid - Madrid", "Remote - EU", "Spain/España", multi-location strings
separated by ';' or ','). Run `jobhunt locations` on a real tracker to list the
actual strings and add any that are misclassified here.
"""

from __future__ import annotations

import pytest

from jobhunt.location import assess, authorized_countries, matches, parse_places
from jobhunt.models import Job

PREFERRED = ["Madrid"]
ACCEPTABLE = ["Spain", "Remote - Spain", "Remote - EU", "Remote - Europe", "EU"]


def tier(location: str, workplace: str = "unknown", auth=("EU",), locations=()) -> str:
    job = Job("greenhouse", "1", "Co", "co", "Intern", "u", location=location,
              locations=list(locations), workplace_type=workplace)  # fmt: skip
    return assess(job, PREFERRED, ACCEPTABLE, [], list(auth)).tier


@pytest.mark.parametrize(
    "location, expected",
    [
        # 1) Madrid
        ("Madrid", "preferred"),
        ("Madrid, Spain", "preferred"),
        ("Madrid, Comunidad de Madrid, España", "preferred"),
        ("Hybrid - Madrid", "preferred"),
        ("Madrid (Hybrid)", "preferred"),
        ("Madrid Office", "preferred"),
        # 2) rest of Spain, remote from Spain/EU
        ("Barcelona", "acceptable"),
        ("Barcelona, Catalonia, Spain", "acceptable"),
        ("Spain", "acceptable"),
        ("España", "acceptable"),
        ("Spain/España", "acceptable"),
        ("Remote - Spain", "acceptable"),
        ("Remote, Spain", "acceptable"),
        ("Remote - EU", "acceptable"),
        ("Remote (Europe)", "acceptable"),
        ("Remote - Europe", "acceptable"),
        # 3) EU without visa
        ("Amsterdam, Netherlands", "acceptable"),
        ("Paris, France", "acceptable"),
        ("Berlin", "acceptable"),
        ("Dublin, Ireland", "acceptable"),
        ("Lisboa, Portugal", "acceptable"),
        # 4) rest of the world without visa problems
        ("Zurich, Switzerland", "other"),
        ("EMEA", "other"),
        ("Remote", "other"),
        ("Anywhere", "other"),
        # visa needed for an EU-only candidate
        ("London, United Kingdom", "visa"),
        ("London, UK", "visa"),
        ("New York, NY", "visa"),
        ("San Francisco, CA", "visa"),
        ("Cambridge, MA", "visa"),
        ("Remote - US", "visa"),
        ("Remote (United States)", "visa"),
        ("US-Remote", "visa"),
        ("Toronto, ON, CA", "visa"),
        ("Singapore", "visa"),
        ("Tel Aviv, IL", "visa"),
        # nothing usable
        ("", "unknown"),
        ("Springfield", "unknown"),
    ],
)
def test_tiers(location, expected):
    assert tier(location) == expected


@pytest.mark.parametrize(
    "location, expected",
    [
        ("New York, NY; London, UK; Madrid, Spain", "preferred"),
        ("San Francisco, CA | Barcelona, Spain", "acceptable"),
        ("London, UK; Remote - EU", "acceptable"),
        ("Madrid, London", "preferred"),
        ("Lisbon or Madrid", "preferred"),
        ("New York, NY; London, UK", "visa"),
        ("Spain, Portugal", "acceptable"),
    ],
)
def test_multi_location_strings_use_the_best_place(location, expected):
    assert tier(location) == expected


def test_extra_locations_list_is_considered():
    assert tier("New York, NY", locations=["New York, NY", "Madrid, Spain"]) == "preferred"


def test_remote_flag_from_ats_applies_to_the_place():
    job = Job("ashby", "1", "Co", "co", "Intern", "u", location="Spain", workplace_type="remote")
    result = assess(job, PREFERRED, ACCEPTABLE, [], ["EU"])
    assert result.tier == "acceptable" and result.remote


def test_authorization_changes_visa_outcome():
    assert tier("London, UK", auth=("EU", "UK")) == "other"
    assert tier("New York, NY", auth=("EU", "US")) == "other"
    assert authorized_countries(["EU"]) >= {"ES", "FR", "NL", "CH", "NO"}
    assert "GB" not in authorized_countries(["EU"])


def test_avoid_list():
    job = Job("x", "1", "Co", "co", "T", "u", location="Barcelona, Spain")
    assert assess(job, PREFERRED, ACCEPTABLE, ["Barcelona"], ["EU"]).tier == "avoided"


@pytest.mark.parametrize(
    "place, pattern, expected",
    [
        ("Hybrid - Madrid", "Madrid", True),
        ("Remote - Spain", "Madrid", False),
        ("Remote - Spain", "Spain", True),
        ("Remote - Spain", "Remote - EU", True),
        ("Barcelona", "Remote - Spain", False),
        ("Amsterdam", "EU", True),
        ("Zurich", "EU", False),
        ("Zurich", "Europe", True),
        ("Remote", "Remote", True),
        ("Remote - US", "Remote", False),
        ("Sant Cugat del Vallès", "Barcelona", False),
        ("Tres Cantos", "Spain", True),
        ("Somewhere Odd", "somewhere odd", True),  # unknown names fall back to text match
    ],
)
def test_matches(place, pattern, expected):
    assert matches(parse_places([place])[0], pattern) is expected


def test_parse_places_dedupes_and_flags():
    places = parse_places(["Madrid, Spain", "Madrid, Spain; Remote - EU", ""])
    assert len(places) == 2
    assert places[0].country == "ES" and "madrid" in places[0].cities
    assert places[1].remote and places[1].region == "eu"
