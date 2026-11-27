"""Registry lookups and Docker image tag handling.

Docker tags are ambiguous, so the rules are deliberately narrow: only a tag
from the same version series is ever offered.
"""

from __future__ import annotations

import json

from sadoc import registry


def test_action_release_evidence_keeps_fixed_range_and_breaking_boundaries(monkeypatch):
    tags = ["v0.4.0", "v0.3.3", "v0.3.2", "v0.3.2+build", "v0.3.1-rc.1", "v0.3.0"]
    tags += [f"v0.2.{patch}" for patch in range(10, -1, -1)]
    tags += ["v0.1.5", "v0.1.4"]
    payload = [{"tag_name": tag, "body": tag * 500} for tag in tags]
    monkeypatch.setattr(registry, "_fetch", lambda *a, **k: json.dumps(payload).encode())

    evidence = registry.action_release_evidence("owner/example", "v0.1.4", "v0.3.2")

    notes = evidence["releases"]
    assert [note["tag"] for note in notes[:3]] == ["v0.3.2", "v0.2.0", "v0.3.0"]
    assert len(notes) == 8
    assert all(note["excerpted"] for note in notes)
    assert not {note["tag"] for note in notes} & {
        "v0.4.0",
        "v0.3.3",
        "v0.3.2+build",
        "v0.3.1-rc.1",
        "v0.1.4",
    }
    assert evidence["coverage_limits"] == ["Only up to eight in-range releases are included."]


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
