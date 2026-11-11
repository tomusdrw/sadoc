"""Docker image tag handling.

Docker tags are ambiguous, so the rules are deliberately narrow: only a tag
from the same version series is ever offered.
"""

from __future__ import annotations

import json

from deps_bot import registry


def _fake_tags(monkeypatch, names: list[str]) -> None:
    """Make the registry return exactly these tags."""
    payload = {"results": [{"name": name} for name in names]}
    monkeypatch.setattr(registry, "_fetch", lambda *a, **k: json.dumps(payload).encode())


def test_tag_parts():
    assert registry._tag_parts("3.21.10") == (3, 21, 10)
    assert registry._tag_parts("20-bullseye-slim") == (20, 0, 0)
    assert registry._tag_parts("1.22") == (1, 22, 0)
    assert registry._tag_parts("latest") is None
    assert registry._tag_parts("jammy-20260322.1") is None


def test_docker_latest_needs_a_current_tag():
    """Without a current tag there is no series to stay within."""
    assert registry.docker_latest("library/node") is None


def test_docker_latest_returns_none_when_the_series_has_no_successor(monkeypatch):
    _fake_tags(monkeypatch, ["3.21"])
    assert registry.docker_latest("library/alpine", "3.21") is None


def test_docker_latest_stays_in_the_series(monkeypatch):
    _fake_tags(
        monkeypatch,
        [
            "3.21.10",
            "3.21.9",
            "3.24.4",  # a different minor - not an update
            "20260805",  # a date tag
            "latest",
        ],
    )
    assert registry.docker_latest("library/alpine", "3.21") == "3.21.10"


def test_docker_latest_never_downgrades(monkeypatch):
    _fake_tags(monkeypatch, ["26.6.0", "26.6.6"])
    assert registry.docker_latest("library/node", "26.6.6") is None


def test_docker_latest_keeps_the_suffix(monkeypatch):
    """A different base distribution is not an update."""
    _fake_tags(
        monkeypatch,
        [
            "20.20-bullseye-slim",
            "20.20-bookworm-slim",
            "24.20-bullseye-slim",
        ],
    )
    assert registry.docker_latest("library/node", "20-bullseye-slim") == "20.20-bullseye-slim"


def test_docker_latest_accepts_a_minor_for_a_major_only_tag(monkeypatch):
    _fake_tags(monkeypatch, ["20.20-bullseye-slim", "22.20-bullseye-slim"])
    assert registry.docker_latest("library/node", "20-bullseye-slim") == "20.20-bullseye-slim"
