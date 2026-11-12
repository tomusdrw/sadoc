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
    assert versions["cargo:demo"] == "0.2.0"
    assert versions["npm:demo"] == "1.4.0"


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


def test_readme_examples_are_not_annotations():
    for fence in ("```", "~~~"):
        files = {
            "README.md": f"{fence}html\n<!-- sadoc: release = npm -->\n{fence}\n"
            f'{fence}toml\n[release]\nkind = "cargo"\n{fence}\n'
        }
        assert releases.read_annotations(files) == ([], "")


def test_config_only_reads_release_section_and_takes_precedence():
    files = {
        ".sadoc.toml": '[other]\nkind="npm"\n[release]\nkind="none"',
        "README.md": "<!-- sadoc: release = github -->",
    }
    assert releases.read_annotations(files) == ([releases.NONE], "")


def test_building_python_is_not_publishing():
    files = {".github/workflows/build.yml": "on: [push]\nsteps:\n  - run: python -m build\n"}
    assert releases.detect_kinds(files)[0] == []


def test_comments_and_step_names_are_not_publish_commands():
    files = {
        ".github/workflows/build.yml": "on: [push]\nsteps:\n"
        "  # npm publish\n  - name: cargo publish\n    run: cargo test\n"
    }
    assert releases.detect_kinds(files)[0] == []


def test_pull_request_deploy_is_not_default_branch_deploy():
    files = {
        ".github/workflows/build.yml": "on:\n  pull_request:\nsteps:\n"
        "  - uses: actions/deploy-pages@v4\n"
    }
    assert releases.detect_kinds(files)[0] != [releases.AUTO]


def test_tag_workflow_suggests_a_tag_not_manual_publishing():
    files = {
        ".github/workflows/publish.yml": 'on:\n  push:\n    tags: ["v*"]\n'
        "steps:\n  - run: cargo publish\n"
    }
    kinds, command, _ = releases.detect_kinds(files)
    assert kinds == [releases.CRATES]
    assert "tag" in command
    assert "cargo publish" not in command


def test_package_release_script_is_evidence():
    files = {
        "package.json": '{"name":"lib","version":"1.0.0",'
        '"scripts":{"release":"npm publish --access public"}}'
    }
    kinds, command, source = releases.detect_kinds(files)
    assert kinds == [releases.NPM]
    assert command == "npm run release"
    assert source == "script"


def test_inline_tag_trigger_is_not_automatic_default_branch_publish():
    kinds, command, _ = releases.detect_kinds(
        {
            ".github/workflows/release.yml": 'on: {push: {tags: ["v*"]}}\n'
            "steps:\n  - run: npm publish\n",
        }
    )
    assert kinds == [releases.NPM]
    assert "tag" in command


def test_indented_and_inline_readme_examples_are_not_annotations():
    assert releases.read_annotations(
        {"README.md": "    <!-- sadoc: release = npm -->\n`<!-- sadoc: release = github -->`"}
    ) == ([], "")


def test_workspace_version_does_not_use_dependency_version():
    files = {
        "Cargo.toml": '[workspace.package]\nversion="1.2.0"\n',
        "crates/lib/Cargo.toml": '[package]\nname="lib"\nversion.workspace=true\n'
        '[dependencies.helper]\nversion="9.9.9"',
    }
    assert releases.project_versions(files) == [("cargo:lib", "1.2.0")]


def test_release_script_is_still_detected_alongside_docs_deploy():
    kinds, command, _ = releases.detect_kinds(
        {
            "package.json": '{"scripts":{"release":"npm publish"}}',
            ".github/workflows/docs.yml": "on: [push]\nsteps:\n  - uses: actions/deploy-pages@v4\n",
        }
    )
    assert kinds == [releases.NPM]
    assert command == "npm run release"


def test_release_script_keeps_other_historical_destinations():
    kinds, _, _ = releases.detect_kinds(
        {"package.json": '{"scripts":{"release":"npm publish"}}'},
        published_kinds=(releases.GITHUB,),
    )
    assert releases.GITHUB in kinds
    assert releases.NPM in kinds


def test_branch_and_path_names_are_not_workflow_events():
    files = {
        ".github/workflows/publish.yml": "on:\n  push:\n    branches: [main]\n    paths: [release]\n"
        "steps:\n  - run: npm publish\n"
    }
    assert releases.detect_kinds(files)[0] == [releases.AUTO]
    files[".github/workflows/publish.yml"] = (
        "on: {push: {branches: [main], paths: [release]}}\nsteps:\n  - run: npm publish\n"
    )
    assert releases.detect_kinds(files)[0] == [releases.AUTO]


def test_ignoring_other_branches_still_publishes_from_default_branch():
    files = {
        ".github/workflows/publish.yml": "on:\n  push:\n    branches-ignore: [develop]\n"
        "steps:\n  - run: npm publish\n"
    }
    assert releases.detect_kinds(files)[0] == [releases.AUTO]


def test_excluded_default_branch_is_not_automatic():
    for filters in ("branches-ignore: [main]", 'branches: ["*", "!main"]'):
        files = {
            ".github/workflows/publish.yml": f"on:\n  push:\n    {filters}\n"
            "steps:\n  - run: npm publish\n"
        }
        assert releases.detect_kinds(files)[0] != [releases.AUTO]
