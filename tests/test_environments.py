"""The runtime versions pinned inside CI workflows."""

from __future__ import annotations

from sadoc import environments


def test_finds_pinned_runtimes():
    text = """
jobs:
  build:
    steps:
      - uses: actions/setup-node@v5
        with:
          node-version: 20
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - uses: actions/setup-go@v5
        with:
          go-version: "1.22"
"""
    found = {k: v for k, _e, v in environments.find_environment_versions(text)}
    assert found["node-version"] == "20"
    assert found["python-version"] == "3.11"
    assert found["go-version"] == "1.22"


def test_a_version_file_is_left_alone():
    text = """
      - uses: actions/setup-node@v5
        with:
          node-version-file: .nvmrc
"""
    assert environments.find_environment_versions(text) == []


def test_a_file_variant_anywhere_means_no_bump():
    """If one job reads the version from a file, the workflow manages it."""
    text = """
      - uses: actions/setup-node@v5
        with:
          node-version: 20
      - uses: actions/setup-node@v5
        with:
          node-version-file: .nvmrc
"""
    found = environments.find_environment_versions(text)
    assert [f[0] for f in found] == []


def test_expressions_are_ignored():
    text = """
        with:
          node-version: ${{ matrix.node }}
"""
    assert environments.find_environment_versions(text) == []


def test_x_suffix_is_trimmed():
    text = "        node-version: 20.x\n"
    found = environments.find_environment_versions(text)
    assert found and found[0][2] == "20"


def test_detect_updates(monkeypatch):
    monkeypatch.setattr(environments, "latest_for", lambda eco: "26.1.0")
    updates = environments.detect_updates(
        "o/r", ".github/workflows/ci.yml", "        node-version: 20\n"
    )
    assert len(updates) == 1
    update = updates[0]
    assert update.ecosystem == "node"
    assert update.current == "20"
    assert update.latest == "26.1.0"
    assert update.bump_kind == "major"
    assert update.as_dict()["manifest"] == ".github/workflows/ci.yml"


def test_no_update_when_already_current(monkeypatch):
    monkeypatch.setattr(environments, "latest_for", lambda eco: "20.5.0")
    assert (
        environments.detect_updates(
            "o/r", ".github/workflows/ci.yml", "        node-version: 20.5.0\n"
        )
        == []
    )
