"""Working out what a release means for a repository."""

from __future__ import annotations

from sadoc import releases


def test_reads_an_annotation_from_the_readme():
    files = {"README.md": "<!-- sadoc: release = crates.io -->\n\n# Demo\n"}
    kinds, command = releases.read_annotations(files)
    assert kinds == [releases.CRATES]
    assert command == ""


def test_reads_several_kinds():
    files = {"README.md": "<!-- sadoc: release = npm, github -->\n"}
    kinds, _ = releases.read_annotations(files)
    assert kinds == [releases.NPM, releases.GITHUB]


def test_reads_a_config_file():
    files = {".sadoc.toml": '[release]\nkind = "npm"\ncommand = "npm publish"\n'}
    kinds, command = releases.read_annotations(files)
    assert kinds == [releases.NPM]
    assert command == "npm publish"


def test_detects_crates_from_a_workflow():
    files = {
        ".github/workflows/release.yml": "jobs:\n  p:\n    steps:\n      - run: cargo publish\n",
    }
    kinds, _command, source = releases.detect_kinds(files)
    assert releases.CRATES in kinds
    assert source == "workflow"


def test_detects_npm_from_a_workflow():
    files = {
        ".github/workflows/publish.yml": "steps:\n  - run: npm publish\n",
    }
    kinds, _command, _source = releases.detect_kinds(files)
    assert releases.NPM in kinds


def test_detects_a_github_release():
    files = {
        ".github/workflows/release.yml": "steps:\n      - uses: softprops/action-gh-release@v2\n",
    }
    kinds, _command, _source = releases.detect_kinds(files)
    assert releases.GITHUB in kinds


def test_a_repository_that_only_deploys_is_auto():
    files = {
        ".github/workflows/deploy.yml": "on:\n  push:\n    branches: [main]\n"
        "steps:\n  - uses: actions/deploy-pages@v4\n",
    }
    kinds, _command, source = releases.detect_kinds(files)
    assert kinds == [releases.AUTO]
    assert source == "workflow"


def test_an_annotation_beats_the_workflows():
    files = {
        "README.md": "<!-- sadoc: release = none -->\n",
        ".github/workflows/release.yml": "steps:\n  - run: cargo publish\n",
    }
    kinds, _command, source = releases.detect_kinds(files)
    assert kinds == [releases.NONE]
    assert source == "annotation"


def test_project_versions_of_several_packages():
    files = {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.2.0"\n',
        "package.json": '{"name": "demo", "version": "1.4.0"}',
    }
    versions = dict(releases.project_versions(files))
    assert versions["cargo"] == "0.2.0"
    assert versions["npm"] == "1.4.0"


def test_project_versions_of_workspace_crates():
    files = {
        "Cargo.toml": '[workspace]\nmembers = ["crates/a"]\n',
        "crates/a/Cargo.toml": '[package]\nname = "a"\nversion = "0.3.1"\n',
    }
    versions = dict(releases.project_versions(files))
    assert versions["cargo:a"] == "0.3.1"


def test_release_due():
    assert releases.release_due("1.1.0", "1.0.0")
    assert not releases.release_due("1.0.0", "1.0.0")
    assert not releases.release_due("0.9.0", "1.0.0")
    # Never published, and not a placeholder: a first release is due.
    assert releases.release_due("1.0.0", "")
    assert releases.release_due("0.1.0", "")


def test_a_placeholder_version_is_not_due():
    assert not releases.release_due("0.0.0", "")
