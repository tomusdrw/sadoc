"""Security advisory range matching."""

from __future__ import annotations

from deps_bot import advisories


def test_version_in_comparison_range():
    assert advisories.version_in_range("4.17.21", ">= 4.0.0, <= 4.17.23")
    assert not advisories.version_in_range("4.18.0", ">= 4.0.0, <= 4.17.23")


def test_version_in_exact_range():
    assert advisories.version_in_range("4.17.23", "= 4.17.23")
    assert not advisories.version_in_range("4.17.22", "= 4.17.23")


def test_version_in_single_comparison():
    assert advisories.version_in_range("1.2.3", "< 1.2.4")
    assert not advisories.version_in_range("1.2.4", "< 1.2.4")


def test_unparsable_version_is_not_vulnerable():
    assert not advisories.version_in_range("not-a-version", "< 1.2.4")


def test_empty_range_is_not_vulnerable():
    assert not advisories.version_in_range("1.0.0", "")


def test_is_vulnerable_picks_the_matching_advisory():
    advisories_list = [
        {"ghsa": "GHSA-a", "range": ">= 2.0.0, < 2.5.0"},
        {"ghsa": "GHSA-b", "range": "< 1.9.0"},
    ]
    assert advisories.is_vulnerable("2.1.0", advisories_list)["ghsa"] == "GHSA-a"
    assert advisories.is_vulnerable("1.8.0", advisories_list)["ghsa"] == "GHSA-b"
    assert advisories.is_vulnerable("3.0.0", advisories_list) is None


def test_ecosystem_mapping():
    assert advisories.ECOSYSTEM_MAP["pip"] == "pypi"
    assert advisories.ECOSYSTEM_MAP["crates"] == "crates"
