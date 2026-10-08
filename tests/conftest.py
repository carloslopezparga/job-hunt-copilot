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


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Tests must never reach a real server (job boards, Telegram...); loopback is fine."""
    import socket

    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"real network access is disabled in tests ({host})")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)
