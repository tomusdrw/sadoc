"""Semver parsing, classification and constraint semantics."""

from __future__ import annotations

import pytest

from deps_bot import semver


@pytest.mark.parametrize(
    "text,major,minor,patch",
    [
        ("1.2.3", 1, 2, 3),
        ("v1.2.3", 1, 2, 3),
        ("1.2", 1, 2, 0),
        ("1", 1, 0, 0),
        ("0.4.0", 0, 4, 0),
        ("2024.1.2", 2024, 1, 2),
        (" 1.2.3 ", 1, 2, 3),
    ],
)
def test_parse(text, major, minor, patch):
    v = semver.parse(text)
    assert v is not None
    assert (v.major, v.minor, v.patch) == (major, minor, patch)


@pytest.mark.parametrize("text", ["", "*", "latest", "not-a-version", None])
def test_parse_rejects_non_versions(text):
    assert semver.parse(text) is None


def test_prerelease_is_detected():
    v = semver.parse("1.2.3-rc.1")
    assert v is not None
    assert v.is_prerelease


def test_compare_orders_prerelease_before_release():
    pre = semver.parse("1.2.3-rc.1")
    rel = semver.parse("1.2.3")
    assert semver.compare(pre, rel) == -1
    assert semver.compare(rel, pre) == 1


def test_compare_equal():
    assert semver.compare(semver.parse("1.2.3"), semver.parse("1.2.3")) == 0


@pytest.mark.parametrize(
    "current,latest,expected",
    [
        ("1.2.3", "1.2.4", "patch"),
        ("1.2.3", "1.3.0", "minor"),
        ("1.2.3", "2.0.0", "major"),
        ("1.2.3", "1.2.3", None),
        ("1.2.3", "1.2.2", None),
        ("1.2.3", "2.0.0-rc.1", None),
    ],
)
def test_classify(current, latest, expected):
    assert semver.classify(current, latest) == expected


def test_zero_x_minor_bump_is_breaking():
    """Cargo and npm treat a minor bump on 0.x as a breaking change."""
    assert semver.classify("0.4", "0.6.0") == "major"
    assert semver.classify("0.99.1", "2.1.1") == "major"


def test_bump_version_keeps_precision():
    assert semver.bump_version("1.2", "1.3.0") == "1.3"
    assert semver.bump_version("1.2.3", "2.0.0") == "2.0.0"
    assert semver.bump_version("1", "2.5.1") == "2"


@pytest.mark.parametrize(
    "constraint,ecosystem,kind",
    [
        ("^1.2.3", "npm", "caret"),
        ("~1.2.3", "npm", "tilde"),
        ("1.2.3", "npm", "exact"),
        ("1.0", "crates", "caret"),
        ("=1.0", "crates", "exact"),
        (">=1.0", "npm", "gte"),
        ("*", "npm", "any"),
        (">=1.0, <2.0", "npm", "range"),
    ],
)
def test_constraint_kind(constraint, ecosystem, kind):
    assert semver.constraint_kind(constraint, ecosystem) == kind


def test_cargo_bare_version_is_caret():
    """Cargo's "1.0" means >=1.0, <2.0 - it already permits 1.12.1."""
    assert semver.allows("1.0", "1.12.1", "crates")
    assert not semver.allows("1.0", "2.0.0", "crates")


def test_cargo_zero_x_caret_blocks_minor_bump():
    """Cargo's "0.4" means >=0.4, <0.5."""
    assert semver.allows("0.4", "0.4.9", "crates")
    assert not semver.allows("0.4", "0.6.0", "crates")


def test_npm_bare_version_is_exact():
    assert not semver.allows("1.0", "1.12.1", "npm")
    assert semver.allows("^1.0", "1.12.1", "npm")


def test_python_requirement_allows_anything():
    assert semver.allows(">=2.0", "9.9.9", "pypi")


def test_go_pins_exactly():
    assert not semver.allows("v1.2.3", "v1.2.4", "go")
    assert semver.allows("v1.2.3", "v1.2.3", "go")
