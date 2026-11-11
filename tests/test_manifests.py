"""Manifest and lockfile parsing."""

from __future__ import annotations

from sadoc import lockfiles, manifests


def test_parse_package_json():
    deps = manifests.parse_package_json(
        '{"dependencies": {"react": "^18.3.1"}, "devDependencies": {"vite": "5.4.0"}}',
        "package.json",
    )
    by_name = {d.name: d for d in deps}
    assert by_name["react"].constraint == "^18.3.1"
    assert by_name["react"].kind == "runtime"
    assert by_name["vite"].kind == "dev"


def test_parse_package_json_survives_garbage():
    assert manifests.parse_package_json("{not json", "package.json") == []


def test_parse_cargo_toml():
    deps = manifests.parse_cargo_toml(
        """
[package]
name = "demo"
version = "0.1.0"

[dependencies]
serde = "1.0"
anyhow = { version = "1.0.90" }

[dev-dependencies]
criterion = "0.5"
""",
        "Cargo.toml",
    )
    by_name = {d.name: d for d in deps}
    assert by_name["serde"].constraint == "1.0"
    assert by_name["anyhow"].constraint == "1.0.90"
    assert by_name["criterion"].kind == "dev"


def test_cargo_skips_path_and_workspace_deps():
    deps = manifests.parse_cargo_toml(
        """
[dependencies]
local = { path = "../local" }
inherited = { workspace = true }
serde = "1.0"
""",
        "Cargo.toml",
    )
    assert [d.name for d in deps] == ["serde"]


def test_cargo_workspace_dependencies():
    deps = manifests.parse_cargo_toml(
        """
[workspace]
members = ["a"]

[workspace.dependencies]
rand = "0.8"
""",
        "Cargo.toml",
    )
    assert any(d.name == "rand" and d.section == "workspace.dependencies" for d in deps)


def test_parse_requirements_txt():
    deps = manifests.parse_requirements_txt(
        """
# a comment
flask==2.3.0
requests>=2.28
uvicorn[standard]==0.23.0
-r other.txt
""",
        "requirements.txt",
    )
    by_name = {d.name: d for d in deps}
    assert by_name["flask"].current == "2.3.0"
    assert by_name["requests"].constraint == ">=2.28"
    assert by_name["uvicorn"].extras == ["standard"]
    assert "other" not in by_name


def test_parse_pyproject_pep508():
    deps = manifests.parse_pyproject_toml(
        """
[project]
dependencies = ["flask>=2.3", "requests[security]==2.31.0"]
""",
        "pyproject.toml",
    )
    by_name = {d.name: d for d in deps}
    assert by_name["flask"].constraint == ">=2.3"
    assert by_name["requests"].extras == ["security"]
    assert by_name["requests"].current == "2.31.0"


def test_parse_go_mod():
    deps = manifests.parse_go_mod(
        """
module example.com/demo

require (
    github.com/pkg/errors v0.9.1
    golang.org/x/sys v0.15.0 // indirect
)

require github.com/spf13/cobra v1.8.0
""",
        "go.mod",
    )
    names = {d.name for d in deps}
    assert "github.com/pkg/errors" in names
    assert "github.com/spf13/cobra" in names
    assert "golang.org/x/sys" in names


def test_parse_workflow_actions():
    deps = manifests.parse_workflow(
        """
jobs:
  build:
    steps:
      - uses: actions/checkout@v6
      - uses: dtolnay/rust-toolchain@stable
      - uses: ./.github/actions/local
""",
        ".github/workflows/ci.yml",
    )
    by_name = {d.name: d for d in deps}
    assert by_name["actions/checkout"].current == "v6"
    assert by_name["dtolnay/rust-toolchain"].kind == "action"
    # local actions have no version
    assert not any(d.name.startswith(".") for d in deps)


def test_parse_dockerfile():
    deps = manifests.parse_dockerfile(
        "FROM node:20-alpine\nRUN npm ci\nFROM golang:1.22 AS build\n", "Dockerfile"
    )
    by_name = {d.name: d for d in deps}
    # Official images are namespaced under "library/" so the registry lookup
    # resolves to the right repository.
    assert by_name["library/node"].current == "20-alpine"
    assert by_name["library/golang"].current == "1.22"


def test_parse_dockerfile_keeps_namespaced_images():
    deps = manifests.parse_dockerfile("FROM ghcr.io/org/thing:1.2.3\n", "Dockerfile")
    assert deps[0].name == "ghcr.io/org/thing"
    assert deps[0].current == "1.2.3"


def test_parse_cargo_lock():
    locked = lockfiles.parse_cargo_lock(
        '[[package]]\nname = "serde"\nversion = "1.0.210"\n\n'
        '[[package]]\nname = "anyhow"\nversion = "1.0.90"\n'
    )
    assert locked["serde"] == "1.0.210"
    assert locked["anyhow"] == "1.0.90"


def test_package_lock_prefers_hoisted_version():
    """A package can appear several times; the hoisted entry wins."""
    locked = lockfiles.parse_package_lock(
        """
{
  "packages": {
    "": {"name": "root"},
    "node_modules/left-pad": {"version": "1.3.0"},
    "node_modules/a/node_modules/left-pad": {"version": "0.0.1"}
  }
}
"""
    )
    assert locked["left-pad"] == "1.3.0"


def test_parse_pnpm_lock():
    locked = lockfiles.parse_pnpm_lock(
        "packages:\n"
        "  /react@18.3.1:\n"
        "    resolution: {}\n"
        "  /@scope/pkg@2.0.0:\n"
        "    resolution: {}\n"
    )
    assert locked["react"] == "18.3.1"
    assert locked["@scope/pkg"] == "2.0.0"


def test_is_lockfile():
    assert lockfiles.is_lockfile("Cargo.lock")
    assert lockfiles.is_lockfile("apps/web/package-lock.json")
    assert not lockfiles.is_lockfile("package.json")
