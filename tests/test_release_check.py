"""Release suggestions need evidence of a real publication process."""

import pytest

from sadoc import registry, release_check, releases


class Repo:
    def __init__(self, files, history=(), tags=()):
        self.files = files
        self.history = list(history)
        self.tags = list(tags)

    def file_text(self, owner, repo, path, ref):
        return self.files[path]

    def repo_tree(self, owner, repo, branch):
        return [{"path": path} for path in self.files]

    def api_paginate(self, endpoint, limit):
        return self.history if endpoint.endswith("/releases") else self.tags


@pytest.fixture(autouse=True)
def registries(monkeypatch):
    for name in ("npm_latest", "crates_io_latest", "pypi_latest"):
        monkeypatch.setattr(registry, name, lambda name: None)


def check(files, history=(), tags=()):
    return release_check.check(Repo(files, history, tags), "owner", "repo", "main")


def test_documentation_examples_do_not_request_a_first_release():
    status = check(
        {
            "pyproject.toml": '[project]\nname="sadoc"\nversion="1.0.0"',
            "README.md": "```html\n<!-- sadoc: release = npm, github -->\n```",
        }
    )
    assert not status.due
    assert not status.kinds


def test_unstarted_release_automation_is_not_overdue():
    status = check(
        {
            "Cargo.toml": '[package]\nname="pvm-decompiler"\nversion="0.3.0"',
            ".github/workflows/publish.yml": "on:\n  release:\n    types: [published]\njobs:\n  publish:\n"
            "    steps:\n      - run: cargo publish\n",
        }
    )
    assert not status.due
    assert status.command == "gh release create"
    assert "cargo publish" not in status.note
    assert "history" in status.note.lower()


def test_github_history_establishes_a_release_process():
    status = check({"VERSION": "1.2.0"}, history=[{"tag_name": "v1.1.0", "draft": False}])
    assert status.due
    assert status.kinds == [releases.GITHUB]
    assert status.published == "1.1.0"


def test_draft_and_prerelease_history_does_not_establish_stable_releases():
    status = check(
        {"VERSION": "1.2.0"},
        history=[
            {"tag_name": "v1.1.0", "draft": True},
            {"tag_name": "v1.2.0-rc.1", "prerelease": True},
        ],
    )
    assert not status.due


def test_tags_alone_do_not_imply_github_releases():
    status = check({"VERSION": "1.2.0"}, tags=[{"name": "v1.1.0"}])
    assert not status.due
    assert releases.GITHUB not in status.kinds


def test_registry_history_uses_actual_package_name(monkeypatch):
    monkeypatch.setattr(
        registry, "npm_latest", lambda name: "1.0.0" if name == "@org/lib" else None
    )
    status = check({"package.json": '{"name":"@org/lib","version":"1.1.0"}'})
    assert status.due
    assert status.kinds == [releases.NPM]
    assert status.published == "1.0.0"


def test_private_package_does_not_inherit_registry_history(monkeypatch):
    monkeypatch.setattr(registry, "npm_latest", lambda name: "1.0.0")
    status = check({"package.json": '{"name":"private-app","version":"2.0.0","private":true}'})
    assert not status.due


def test_packages_are_compared_against_their_own_publications(monkeypatch):
    monkeypatch.setattr(registry, "npm_latest", lambda name: "10.0.0")
    monkeypatch.setattr(registry, "crates_io_latest", lambda name: "0.1.0")
    status = check(
        {
            "package.json": '{"name":"frontend","version":"10.0.0"}',
            "Cargo.toml": '[package]\nname="backend"\nversion="0.2.0"',
        }
    )
    assert status.due


def test_explicit_annotation_can_request_a_first_release():
    status = check({"VERSION": "1.0.0", ".sadoc.toml": '[release]\nkind="github"'})
    assert status.due


def test_explicit_none_overrides_history():
    status = check(
        {"VERSION": "1.2.0", ".sadoc.toml": '[release]\nkind="none"'},
        history=[{"tag_name": "v1.0.0"}],
    )
    assert not status.due
    assert status.kinds == [releases.NONE]


def test_release_triggered_build_suggests_github_release():
    status = check(
        {
            "VERSION": "1.2.0",
            ".github/workflows/build.yml": "on: [release]\njobs:\n  build:\n    steps:\n      - run: make build\n",
        },
        history=[{"tag_name": "v1.1.0"}],
    )
    assert status.due
    assert status.command == "gh release create"


def test_automatic_npm_publish_on_main_needs_no_manual_release(monkeypatch):
    monkeypatch.setattr(registry, "npm_latest", lambda name: "1.0.0")
    status = check(
        {
            "package.json": '{"name":"lib","version":"1.1.0"}',
            ".github/workflows/publish.yml": "on:\n  push:\n    branches: [main]\n"
            "jobs:\n  publish:\n    steps:\n      - run: npm publish\n",
        }
    )
    assert not status.due
    assert status.kinds == [releases.AUTO]


def test_docs_deployment_does_not_hide_a_published_library(monkeypatch):
    monkeypatch.setattr(registry, "crates_io_latest", lambda name: "1.0.0")
    status = check(
        {
            "Cargo.toml": '[package]\nname="lib"\nversion="1.1.0"',
            ".github/workflows/docs.yml": "on: [push]\nsteps:\n  - uses: actions/deploy-pages@v4\n",
        }
    )
    assert status.due
    assert releases.CRATES in status.kinds


def test_tag_build_history_can_establish_cadence_without_github_releases():
    status = check(
        {
            "VERSION": "1.2.0",
            ".github/workflows/publish.yml": 'on:\n  push:\n    tags: ["v*"]\n'
            "steps:\n  - run: cargo publish\n",
        },
        tags=[{"name": "v1.1.0"}],
    )
    assert status.due
    assert "tag" in status.command


def test_failed_history_lookup_is_not_a_first_release_debt():
    class UnavailableRepo(Repo):
        def api_paginate(self, endpoint, limit):
            raise RuntimeError("unavailable")

    status = release_check.check(
        UnavailableRepo(
            {
                "VERSION": "1.2.0",
                ".github/workflows/build.yml": "on: [release]\nsteps:\n  - run: make build\n",
            }
        ),
        "owner",
        "repo",
        "main",
    )
    assert not status.due


def test_pypi_history_is_checked(monkeypatch):
    monkeypatch.setattr(
        registry, "pypi_latest", lambda name: "1.0.0" if name == "library" else None
    )
    status = check({"pyproject.toml": '[project]\nname="library"\nversion="1.1.0"'})
    assert status.due
    assert status.kinds == [releases.PYPI]


def test_unpublished_second_package_does_not_borrow_other_packages_history(monkeypatch):
    monkeypatch.setattr(registry, "npm_latest", lambda name: "1.0.0")
    status = check(
        {
            "package.json": '{"name":"frontend","version":"1.0.0"}',
            "Cargo.toml": '[package]\nname="backend"\nversion="2.0.0"',
        }
    )
    assert not status.due


def test_publish_false_crates_do_not_inherit_registry_history(monkeypatch):
    monkeypatch.setattr(registry, "crates_io_latest", lambda name: "1.0.0")
    status = check({"Cargo.toml": '[package]\nname="private-lib"\nversion="2.0.0"\npublish=false'})
    assert not status.due


def test_legacy_poetry_manifest_can_still_owe_a_release():
    status = check(
        {
            "pyproject.toml": '[tool.poetry]\nname="lib"\nversion="1.2.0"',
            ".github/workflows/publish.yml": "on: [release]\nsteps:\n  - run: poetry publish\n",
        },
        history=[{"tag_name": "v1.1.0"}],
    )
    assert status.due
    assert status.version == "1.2.0"
