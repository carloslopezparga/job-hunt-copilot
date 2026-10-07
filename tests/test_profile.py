from __future__ import annotations

import copy

import pytest
import yaml

from jobhunt.profile import ProfileError, load_profile, localize, parse_profile


def test_example_profile_loads(profile):
    assert profile.personal.full_name == "Alex Example Martín"
    assert profile.personal.first_name == "Alex"
    assert len(profile.experience) == 2
    assert profile.languages[1].certificate.startswith("Cambridge")
    assert profile.preferences.availability[0].remote_only is True
    assert profile.placeholders() == []


def test_template_parses_but_reports_placeholders(root):
    template = load_profile(root / "profile.template.yaml")
    holes = template.placeholders()
    assert "personal.full_name" in holes
    assert "summaries.default.en" in holes


def test_localize_falls_back_to_english():
    assert localize({"en": "hello", "es": "hola"}, "es") == "hola"
    assert localize({"en": "hello"}, "es") == "hello"
    assert localize("plain", "es") == "plain"
    assert localize(None, "en") == ""


def test_missing_profile_file(tmp_path):
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "profile.yaml")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d["personal"].pop("email"), "personal.email"),
        (lambda d: d.update(education=[]), "education"),
        (lambda d: d["preferences"].update(sectors=["crypto"]), "unknown sector"),
        (lambda d: d["preferences"]["availability"][0].update(end="2020-01-01"), "end is before"),
        (lambda d: d["scoring"]["weights"].update(role=-1), "non-negative"),
    ],
)
def test_invalid_profiles_are_rejected(root, mutate, message):
    data = yaml.safe_load((root / "profile.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    mutate(data)
    with pytest.raises(ProfileError, match=message):
        parse_profile(data)
