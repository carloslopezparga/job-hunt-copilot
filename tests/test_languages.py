from __future__ import annotations

import pytest

from jobhunt.languages import missing_languages, required_languages, spoken_languages
from jobhunt.textutils import find_roles


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Anti Financial Crime Analyst – Italian Fluent", ["Italian"]),
        ("Solutions Architect (Portuguese speaking)", ["Portuguese"]),
        ("Sales Intern, French Market (6 months)", ["French"]),
        ("Event Producer (Turkish speaker)", ["Turkish"]),
        ("You are fluent in German and English", ["German", "English"]),
        ("Bilingual French/German", ["French", "German"]),
        ("- Native Spanish and fluent English is a must", ["Spanish", "English"]),
        ("Se requiere nivel alto de francés", ["French"]),
        ("Dominio del italiano.", ["Italian"]),
        ("Italian-speaking clients", ["Italian"]),
    ],
)
def test_requirements_are_detected(text, expected):
    assert required_languages(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Italian is a plus",
        "Knowledge of Portuguese would be a nice to have",
        "Native speakers of French are preferred. German a bonus.",
        "Se valora el francés",
        "We are a cloud native company based in the French Quarter of the story",
        "Our team speaks many languages",
        "",
    ],
)
def test_nice_to_haves_and_noise_are_not_requirements(text):
    assert required_languages(text) == []


def test_profile_languages_need_b2_or_better(profile):
    assert spoken_languages(profile) == {"Spanish", "English"}  # French is only B1
    assert missing_languages("Intern - French Market", "", profile) == ["French"]
    assert missing_languages("Intern", "Fluent English required. Spanish native.", profile) == []


@pytest.mark.parametrize(
    ("title", "role", "match"),
    [
        ("Software Engineering Intern", "software engineer", True),
        ("Software Engineers wanted", "software engineer", True),
        ("Data Science Intern", "data scientist", True),
        ("Quantitative Researcher", "quant", True),
        ("Trading Intern", "trader", True),
        ("Investment Banker Summer Analyst", "investment banking", True),
        ("Machine Learning Engineer", "machine learning", True),
        ("Marketing Intern", "analyst", False),
        ("Hardware Engineer Intern", "software engineer", False),
        ("Engine Room Technician", "software engineer", False),
    ],
)
def test_roles_match_by_root_and_variants(title, role, match):
    assert bool(find_roles(title, [role])) is match
