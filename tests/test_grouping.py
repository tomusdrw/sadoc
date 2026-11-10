"""Grouping updates into pull requests.

Updates are clustered by the files they would touch. Two updates that touch the
same file have to be in the same pull request, otherwise the branches conflict.
"""

from __future__ import annotations

from deps_bot import pipeline
from deps_bot.detect import Update


def make(
    manifest="package.json",
    ecosystem="npm",
    name="pkg",
    current="1.0.0",
    latest="2.0.0",
    bump="major",
):
    return Update(
        repo="owner/repo",
        manifest=manifest,
        ecosystem=ecosystem,
        name=name,
        section="dependencies",
        kind="runtime",
        current=current,
        latest=latest,
        bump_kind=bump,
        constraint=f"^{current}",
    )


def test_updates_to_the_same_manifest_end_up_together():
    updates = [make(name="a"), make(name="b")]
    groups = pipeline.group_updates(updates)
    assert len(groups) == 1
    assert len(groups[0].updates) == 2


def test_different_ecosystems_are_separate_groups():
    updates = [make(ecosystem="npm"), make(manifest="Cargo.toml", ecosystem="crates")]
    groups = pipeline.group_updates(updates)
    assert len(groups) == 2


def test_group_tier_is_the_highest_present():
    updates = [
        make(name="a", bump="minor", latest="1.1.0"),
        make(name="b", bump="major", latest="2.0.0"),
    ]
    groups = pipeline.group_updates(updates)
    assert len(groups) == 1
    assert groups[0].tier == pipeline.TIER_B


def test_security_promotes_a_group_to_tier_c():
    updates = [make(name="a", bump="patch", latest="1.0.1")]
    groups = pipeline.group_updates(updates, security={("package.json", "a"): True})
    assert groups[0].tier == pipeline.TIER_C


def test_groups_are_ordered_most_severe_first():
    updates = [
        make(name="a", bump="minor", latest="1.1.0"),
        make(manifest="Cargo.toml", ecosystem="crates", name="c", bump="major", latest="2.0.0"),
    ]
    groups = pipeline.group_updates(updates)
    assert groups[0].tier == pipeline.TIER_B
    assert groups[1].tier == pipeline.TIER_A


def test_lockfile_is_part_of_the_touch_set():
    """Two npm manifests share one lockfile, so they cannot be separate PRs."""
    updates = [
        make(manifest="package.json", name="a"),
        make(manifest="apps/web/package.json", name="b"),
    ]
    groups = pipeline.group_updates(updates)
    assert len(groups) == 1
    assert "package-lock.json" in groups[0].touch


def test_empty_input():
    assert pipeline.group_updates([]) == []
