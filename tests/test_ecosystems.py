"""Recognising the ecosystems Sadoc does and does not handle."""

from __future__ import annotations

from sadoc import ecosystems


def test_finds_supported_ecosystems():
    paths = ["package.json", "Cargo.toml", "go.mod", ".github/workflows/ci.yml"]
    found = {h.ecosystem.name for h in ecosystems.detect(paths)}
    assert "npm" in found
    assert "cargo" in found
    assert "go" in found
    assert "github-actions" in found


def test_finds_unsupported_ecosystems():
    paths = ["build.zig", "build.zig.zon", "src/main.zig"]
    hits = ecosystems.unsupported(paths)
    assert [h.ecosystem.name for h in hits] == ["zig"]
    assert hits[0].manifest == "build.zig"


def test_a_manifest_deeper_in_the_tree_is_not_reported():
    """A docs/Gemfile is a side project, not the repository."""
    paths = ["docs/Gemfile", "package.json"]
    assert ecosystems.unsupported(paths) == []


def test_a_build_system_is_not_a_dependency_manifest():
    """A Makefile says how to build, not what to depend on."""
    assert ecosystems.unsupported(["Makefile"]) == []


def test_a_specification_is_not_a_dependency_manifest():
    assert ecosystems.unsupported(["api-reference/openapi.json"]) == []


def test_a_manifest_at_the_root_is_reported():
    hits = ecosystems.unsupported(["build.gradle"])
    assert [h.ecosystem.name for h in hits] == ["gradle"]


def test_describe_says_what_was_found():
    hits = ecosystems.unsupported(["build.zig"])
    text = ecosystems.describe(hits)
    assert "Zig" in text
    assert "build.zig" in text


def test_describe_is_empty_when_everything_is_supported():
    assert ecosystems.describe(ecosystems.unsupported(["package.json"])) == ""
