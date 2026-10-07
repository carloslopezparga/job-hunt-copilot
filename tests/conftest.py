from __future__ import annotations

from pathlib import Path

import pytest

from jobhunt.profile import Profile, load_profile

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def root() -> Path:
    return ROOT


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def profile() -> Profile:
    return load_profile(ROOT / "profile.example.yaml")
