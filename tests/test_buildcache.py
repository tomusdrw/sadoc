"""The recipe fingerprint.

The fingerprint answers one question: could the build recipe have changed? A
dependency version bump does not change how a project is built, so it must not
invalidate the recipe - otherwise every pull request Sadoc opens would throw
away the recipe it just proved.
"""

from __future__ import annotations

import json

import pytest

from sadoc import buildcache
from sadoc.gitops import Git

PACKAGE_JSON = {
    "name": "thing",
    "scripts": {"build": "tsc", "test": "vitest run"},
    "dependencies": {"react": "18.3.1", "zod": "3.23.8"},
    "devDependencies": {"typescript": "5.6.3"},
    "engines": {"node": ">=20"},
}

CARGO_TOML = """
[package]
name = "thing"
version = "0.1.0"

[dependencies]
serde = "1.0.204"
tokio = { version = "1.39.2", features = ["full"] }

[dev-dependencies]
tempfile = "3.12.0"
"""

GO_MOD = """module example.com/thing

go 1.22

require (
\tgithub.com/spf13/cobra v1.8.1
\tgithub.com/stretchr/testify v1.9.0
)

require golang.org/x/sys v0.22.0 // indirect
"""

REQUIREMENTS = """flask==3.0.3
requests>=2.32.3,<3
pytest~=8.3.2
"""

WORKFLOW = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
"""


@pytest.fixture
def repo(tmp_path):
    """A git checkout that can be changed and re-fingerprinted."""
    root = tmp_path / "repo"
    root.mkdir()
    git = Git(root)
    git._run("init", "-b", "main")

    def write(files: dict[str, str], message: str = "change") -> None:
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git.stage_all()
        git._run(
            "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message
        )

    git.write = write  # type: ignore[attr-defined]
    return git


def fingerprint(git: Git) -> str:
    return buildcache.fingerprint(git, [], allow_network=True)


def test_a_version_bump_keeps_the_recipe(repo):
    repo.write({"package.json": json.dumps(PACKAGE_JSON)})
    before = fingerprint(repo)

    bumped = dict(PACKAGE_JSON)
    bumped["dependencies"] = {"react": "19.0.0", "zod": "3.24.1"}
    bumped["devDependencies"] = {"typescript": "5.7.2"}
    repo.write({"package.json": json.dumps(bumped)}, "bump")
    assert fingerprint(repo) == before


def test_a_lockfile_keeps_the_recipe(repo):
    repo.write(
        {
            "package.json": json.dumps(PACKAGE_JSON),
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        }
    )
    before = fingerprint(repo)

    repo.write({"pnpm-lock.yaml": "lockfileVersion: '9.0'\npackages:\n  react@19.0.0: {}\n"})
    assert fingerprint(repo) == before


def test_adding_a_dependency_changes_the_recipe(repo):
    repo.write({"package.json": json.dumps(PACKAGE_JSON)})
    before = fingerprint(repo)

    added = dict(PACKAGE_JSON)
    added["dependencies"] = {**PACKAGE_JSON["dependencies"], "zod": "3.23.8", "axios": "1.7.7"}
    repo.write({"package.json": json.dumps(added)}, "add a dependency")
    assert fingerprint(repo) != before


def test_a_new_lockfile_changes_the_recipe(repo):
    repo.write({"package.json": json.dumps(PACKAGE_JSON)})
    before = fingerprint(repo)
    repo.write({"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"})
    assert fingerprint(repo) != before


def test_a_changed_script_changes_the_recipe(repo):
    repo.write({"package.json": json.dumps(PACKAGE_JSON)})
    before = fingerprint(repo)

    changed = dict(PACKAGE_JSON)
    changed["scripts"] = {"build": "tsc -b", "test": "vitest run"}
    repo.write({"package.json": json.dumps(changed)}, "change the build script")
    assert fingerprint(repo) != before


def test_a_changed_toolchain_changes_the_recipe(repo):
    repo.write({"package.json": json.dumps(PACKAGE_JSON)})
    before = fingerprint(repo)

    changed = dict(PACKAGE_JSON)
    changed["engines"] = {"node": ">=22"}
    repo.write({"package.json": json.dumps(changed)}, "change the node version")
    assert fingerprint(repo) != before


def test_a_changed_workflow_changes_the_recipe(repo):
    repo.write({".github/workflows/ci.yml": WORKFLOW})
    before = fingerprint(repo)
    repo.write(
        {".github/workflows/ci.yml": WORKFLOW.replace('node-version: "22"', 'node-version: "24"')}
    )
    assert fingerprint(repo) != before


def test_a_cargo_version_bump_keeps_the_recipe(repo):
    repo.write({"Cargo.toml": CARGO_TOML})
    before = fingerprint(repo)
    repo.write(
        {"Cargo.toml": CARGO_TOML.replace('serde = "1.0.204"', 'serde = "1.0.210"')},
        "bump",
    )
    assert fingerprint(repo) == before


def test_a_cargo_dependency_added_changes_the_recipe(repo):
    repo.write({"Cargo.toml": CARGO_TOML})
    before = fingerprint(repo)
    repo.write(
        {
            "Cargo.toml": CARGO_TOML.replace(
                'serde = "1.0.204"', 'serde = "1.0.204"\nanyhow = "1.0.86"'
            )
        },
        "add",
    )
    assert fingerprint(repo) != before


def test_a_go_version_bump_keeps_the_recipe(repo):
    repo.write({"go.mod": GO_MOD})
    before = fingerprint(repo)
    repo.write(
        {"go.mod": GO_MOD.replace("cobra v1.8.1", "cobra v1.9.1")},
        "bump",
    )
    assert fingerprint(repo) == before


def test_a_go_dependency_added_changes_the_recipe(repo):
    repo.write({"go.mod": GO_MOD})
    before = fingerprint(repo)
    repo.write(
        {
            "go.mod": GO_MOD.replace(
                "cobra v1.8.1", "cobra v1.8.1\n\tgithub.com/spf13/viper v1.19.0"
            )
        },
        "add",
    )
    assert fingerprint(repo) != before


def test_the_go_directive_is_not_masked(repo):
    """The Go version decides how the project is built, so it must still count."""
    repo.write({"go.mod": GO_MOD})
    before = fingerprint(repo)
    repo.write({"go.mod": GO_MOD.replace("go 1.22", "go 1.23")}, "bump go")
    assert fingerprint(repo) != before


def test_a_requirements_bump_keeps_the_recipe(repo):
    repo.write({"requirements.txt": REQUIREMENTS})
    before = fingerprint(repo)
    repo.write({"requirements.txt": REQUIREMENTS.replace("flask==3.0.3", "flask==3.1.0")}, "bump")
    assert fingerprint(repo) == before


def test_a_requirements_line_added_changes_the_recipe(repo):
    repo.write({"requirements.txt": REQUIREMENTS})
    before = fingerprint(repo)
    repo.write({"requirements.txt": REQUIREMENTS + "httpx==0.27.2\n"}, "add")
    assert fingerprint(repo) != before


def test_application_source_does_not_change_the_recipe(repo):
    """Source changes are what the pull request is about, not the build."""
    repo.write({"package.json": json.dumps(PACKAGE_JSON), "src/app.ts": "export const a = 1;\n"})
    before = fingerprint(repo)
    repo.write({"src/app.ts": "export const a = 2;\n"}, "change the source")
    assert fingerprint(repo) == before


def test_the_fingerprint_has_a_version_of_its_own(repo):
    repo.write({"package.json": json.dumps(PACKAGE_JSON)})
    assert fingerprint(repo).startswith("recipe-v3:")


def test_the_sandbox_policy_is_part_of_the_fingerprint(repo):
    """A different environment is a different recipe."""
    repo.write({"package.json": json.dumps(PACKAGE_JSON)})
    before = fingerprint(repo)

    with_containers = buildcache.fingerprint(
        repo, [], allow_network=True, allow_containers=True, container_runtime="podman"
    )
    assert with_containers != before

    other_runtime = buildcache.fingerprint(
        repo, [], allow_network=True, allow_containers=True, container_runtime="nerdctl"
    )
    assert other_runtime != with_containers

    no_network = buildcache.fingerprint(
        repo, [], allow_network=False, allow_containers=True, container_runtime="podman"
    )
    assert no_network != with_containers
