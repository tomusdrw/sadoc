from sadoc import toolchains


def test_yarn_inventory_spans_workspace_and_bootstrap():
    files = {
        "package.json": '{"packageManager": "yarn@4.9.2", "workspaces": ["be"]}',
        "be/package.json": '{"packageManager": "yarn@4.9.2"}',
        "be/Dockerfile": "FROM node:24-bookworm-slim\nRUN corepack enable && yarn set version 4.9.2\n",
    }
    pins = [pin for pin in toolchains.inventory(files) if pin.tool == "yarn"]
    assert {(pin.path, pin.version, pin.scope) for pin in pins} == {
        ("package.json", "4.9.2", "."),
        ("be/package.json", "4.9.2", "."),
        ("be/Dockerfile", "4.9.2", "."),
    }
    assert all(pin.role == "install" for pin in pins)


def test_node_compatibility_floor_and_matrix_lane_keep_their_roles():
    pins = toolchains.inventory(
        {
            ".node-version": "22.12.0\n",
            "package.json": '{"engines": {"node": ">=20"}}',
            ".github/workflows/test.yml": "matrix:\n  node-version: [20, 22]\n",
            "Dockerfile": "FROM node:22-bookworm-slim\n",
        }
    )
    assert {(pin.path, pin.version, pin.role) for pin in pins} == {
        (".node-version", "22.12.0", "install"),
        ("package.json", ">=20", "compatibility"),
        (".github/workflows/test.yml", "20", "compatibility"),
        (".github/workflows/test.yml", "22", "install"),
        ("Dockerfile", "22", "install"),
    }


def test_independent_projects_have_distinct_scopes():
    files = {
        "first/package.json": '{"packageManager":"pnpm@9.0.0"}',
        "second/package.json": '{"packageManager":"pnpm@9.0.0"}',
    }
    pins = toolchains.inventory(files)
    assert {(pin.tool, pin.scope) for pin in pins} == {("pnpm", "first"), ("pnpm", "second")}


def test_unresolved_docker_pin_is_recorded():
    [pin] = toolchains.inventory({"Dockerfile": "FROM node:${NODE_VERSION}-slim\n"})
    assert (pin.tool, pin.role, pin.version) == ("node", "unresolved", "${NODE_VERSION}-slim")


def test_plans_use_one_exact_target_and_skip_unchanged_coarse_pin():
    files = {
        ".node-version": "22.12.0\n",
        "Dockerfile": "FROM node:22-bookworm-slim\n",
        ".github/workflows/test.yml": "node-version: 22\n",
    }
    proposals, blocked = toolchains.plan(
        files, resolve=lambda tool, line: "22.13.0", artifact_exists=lambda *_: True
    )
    [proposal] = proposals
    assert not blocked
    assert proposal.target == "22.13.0"
    assert {change.pin.path: change.replacement for change in proposal.changes} == {
        ".node-version": "22.13.0"
    }


def test_missing_container_artifact_blocks_the_whole_set():
    proposals, blocked = toolchains.plan(
        {".node-version": "22.12.0\n", "Dockerfile": "FROM node:22-bookworm-slim\n"},
        resolve=lambda tool, line: "24.1.2",
        artifact_exists=lambda image, tag: False,
    )
    assert not proposals
    assert "node:24-bookworm-slim" in blocked[0]


def test_unresolved_reference_blocks_the_whole_set():
    proposals, blocked = toolchains.plan(
        {
            ".node-version": "22.12.0\n",
            "Dockerfile": "FROM node:${NODE_VERSION}-slim\n",
        },
        resolve=lambda tool, line: "24.1.2",
        artifact_exists=lambda *_: True,
    )
    assert not proposals
    assert "Dockerfile" in blocked[0]


def test_configured_release_line_is_used():
    proposals, blocked = toolchains.plan(
        {
            ".node-version": "22.12.0\n",
            ".sadoc-toolchains.toml": '[release-line]\nnode = "22"\n',
        },
        resolve=lambda tool, line: {"22": "22.13.0"}[line],
        artifact_exists=lambda *_: True,
    )
    assert not blocked
    assert proposals[0].target == "22.13.0"


def test_scan_produces_one_yarn_plan_with_all_paths(monkeypatch):
    from unittest.mock import Mock

    from sadoc import detect
    from sadoc.pipeline import group_updates

    files = {
        "package.json": '{"packageManager": "yarn@4.9.2", "workspaces": ["be"]}',
        "be/package.json": '{"packageManager": "yarn@4.9.2"}',
        "be/Dockerfile": "FROM node:24-slim\nRUN corepack enable && yarn set version 4.9.2\n",
        "yarn.lock": "",
    }
    gh = Mock()
    gh.repo_tree.return_value = [{"type": "blob", "path": path} for path in files]
    gh.file_text.side_effect = lambda owner, repo, path, ref: files[path]
    original_plan = toolchains.plan

    def plan(files, **kwargs):
        return original_plan(
            files, resolve=lambda tool, line: {"yarn": "4.18.1", "node": "24.1.0"}[tool], **kwargs
        )

    monkeypatch.setattr(toolchains, "plan", plan)
    [group] = group_updates(detect.detect_repo(gh, "owner", "repo", "main"))
    assert group.toolchain
    assert group.touch == {"package.json", "be/package.json", "be/Dockerfile", "yarn.lock"}
    assert [(u.name, u.latest) for u in group.updates] == [("yarn", "4.18.1")]


def test_apply_and_verify_reject_a_partially_updated_set(tmp_path):
    from dataclasses import asdict

    files = {
        "package.json": '{"packageManager": "yarn@4.9.2", "workspaces": ["be"]}',
        "be/package.json": '{"packageManager": "yarn@4.9.2"}',
        "be/Dockerfile": "RUN corepack enable && yarn set version 4.9.2\n",
    }
    for path, text in files.items():
        destination = tmp_path / path
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(text)
    [proposal], blocked = toolchains.plan(files, resolve=lambda *_: "4.18.1")
    assert not blocked
    plans = [{**asdict(proposal), "lockfiles": []}]
    toolchains.apply(tmp_path, plans)
    toolchains.validate(tmp_path, plans, list(files))
    assert "yarn@4.18.1" in (tmp_path / "be/package.json").read_text()
    assert "yarn set version 4.18.1" in (tmp_path / "be/Dockerfile").read_text()
    (tmp_path / "be/package.json").write_text(files["be/package.json"])

    import pytest

    with pytest.raises(ValueError, match="Yarn|yarn"):
        toolchains.validate(tmp_path, plans, list(files))


def test_real_matrix_axis_is_updated_in_unison():
    files = {
        ".nvmrc": "20\n",
        ".github/workflows/ci.yml": (
            "matrix:\n  node: [18, 20]\nwith:\n  node-version: ${{ matrix.node }}\n"
        ),
    }
    [proposal], blocked = toolchains.plan(files, resolve=lambda *_: "24.1.0")
    assert not blocked
    assert {(change.pin.path, change.pin.version) for change in proposal.changes} == {
        (".nvmrc", "20"),
        (".github/workflows/ci.yml", "20"),
    }


def test_incompatible_engine_range_blocks_a_release():
    proposals, blocked = toolchains.plan(
        {".nvmrc": "18\n", "package.json": '{"engines":{"node":"<20"}}'},
        resolve=lambda *_: "24.1.0",
    )
    assert not proposals
    assert "compatibility" in blocked[0]


def test_related_lockfiles_include_files_without_dependency_parsers():
    from unittest.mock import Mock

    from sadoc.detect import find_manifest_paths

    gh = Mock()
    gh.repo_tree.return_value = [
        {"type": "blob", "path": path} for path in ("go.sum", "go.work.sum", "bun.lockb")
    ]
    _, _, lockfiles = find_manifest_paths(gh, "o", "r", "main")
    assert lockfiles == ["bun.lockb", "go.sum", "go.work.sum"]


def test_detected_compatibility_lanes_are_protected(tmp_path):
    from dataclasses import asdict

    import pytest

    files = {
        ".nvmrc": "20\n",
        ".ruby-version": "3.2.0\n",
        ".github/workflows/ci.yml": "matrix:\n  node-version: [18, 20]\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    proposals, _ = toolchains.plan(
        files, resolve=lambda tool, line: {"node": "24.1.0", "ruby": "3.2.0"}[tool]
    )
    plans = [{**asdict(proposals[0]), "lockfiles": []}]
    toolchains.protect(tmp_path, plans, list(files))
    toolchains.apply(tmp_path, plans)
    toolchains.validate(tmp_path, plans, list(files))
    (tmp_path / ".ruby-version").write_text("3.3.0\n")
    with pytest.raises(ValueError, match="outside the selected set"):
        toolchains.validate(tmp_path, plans, list(files))
    (tmp_path / ".ruby-version").write_text("3.2.0\n")
    (tmp_path / ".github/workflows/ci.yml").write_text("matrix:\n  node-version: [24]\n")
    with pytest.raises(ValueError, match="outside the selected set"):
        toolchains.validate(tmp_path, plans, list(files))


def test_explicitly_coordinated_tools_block_together():
    files = {
        ".node-version": "22\n",
        "Dockerfile": "FROM node:22-slim\n",
        "package.json": '{"packageManager":"yarn@4.9.2"}',
        ".sadoc-toolchains.toml": '[[coordinate]]\ntools = ["node", "yarn"]\n',
    }
    proposals, blocked = toolchains.plan(
        files,
        resolve=lambda tool, line: {"node": "24.1.0", "yarn": "4.18.1"}[tool],
        artifact_exists=lambda *_: False,
    )
    assert not proposals
    assert any("coordinated set" in reason for reason in blocked)


def test_independent_scopes_each_get_a_group(monkeypatch):
    from unittest.mock import Mock

    from sadoc import detect
    from sadoc.pipeline import group_updates

    files = {
        "first/package.json": '{"packageManager":"pnpm@9.0.0"}',
        "second/package.json": '{"packageManager":"pnpm@9.0.0"}',
    }
    gh = Mock()
    gh.repo_tree.return_value = [{"type": "blob", "path": path} for path in files]
    gh.file_text.side_effect = lambda owner, repo, path, ref: files[path]
    original_plan = toolchains.plan
    monkeypatch.setattr(
        toolchains,
        "plan",
        lambda files, **kw: original_plan(files, resolve=lambda *_: "10.0.0", **kw),
    )
    groups = group_updates(detect.detect_repo(gh, "o", "r", "main"))
    assert len(groups) == 2
    assert len({group.identity for group in groups}) == 2
    assert [group.touch for group in groups] == [{"first/package.json"}, {"second/package.json"}]


def test_verification_rejects_wrong_runtime(tmp_path, monkeypatch):
    import os
    import subprocess
    from dataclasses import asdict

    [proposal], _ = toolchains.plan({".node-version": "22.0.1\n"}, resolve=lambda *_: "24.1.2")
    command = toolchains.verification_commands(tmp_path, [{**asdict(proposal), "lockfiles": []}])
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\necho v22.0.1\n")
    node.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    args = ["bash", "-e", "-o", "pipefail", "-c", command]
    assert subprocess.run(args, capture_output=True, check=False).returncode == 1
    node.write_text("#!/bin/sh\necho v24.1.2\n")
    assert subprocess.run(args, capture_output=True, check=False).returncode == 0


def test_vendored_yarn_path_blocks_package_manager_only_change():
    proposals, blocked = toolchains.plan(
        {
            "package.json": '{"packageManager":"yarn@4.9.2"}',
            ".yarnrc.yml": "yarnPath: .yarn/releases/yarn-4.9.2.cjs\n",
        },
        resolve=lambda *_: "4.18.1",
    )
    assert not proposals
    assert ".yarnrc.yml" in blocked[0]
