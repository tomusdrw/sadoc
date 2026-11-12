"""Build discovery against real Git checkouts and SQLite; only pi is replaced."""

import sqlite3
import subprocess
from unittest.mock import Mock

import pytest

from sadoc.config import Config
from sadoc.gitops import Git
from sadoc.pipeline import Worker
from sadoc.piworker import PiError, TurnResult
from sadoc.state import State

REPORT = """```
STATUS: ready
SETUP: export BUILD_MARKER=ready && printf generated > artifact
VERIFY: test "$BUILD_MARKER" = ready && test -f artifact
PREREQUISITES: none
CI_ONLY: production smoke test requires the deployed service
NOTES: Check the generated artifact.
```"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git = Git(root)
    git._run("init", "-b", "main")
    (root / ".gitignore").write_text("artifact\n")
    (root / "README.md").write_text("A project with unconventional build steps.\n")
    git.stage_all()
    git._run("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    state = State(tmp_path / "state.db")
    worker = Worker(Config(disable_dependabot=False), Mock(), state)
    worker.gh.default_branch.return_value = "main"
    worker.prepare_clone = lambda *a: git

    class LocalSession:
        report = REPORT
        prompts = []
        commands = []

        def __init__(self, **kwargs):
            self.cwd = kwargs["cwd"]

        def start(self):
            pass

        def prompt(self, prompt, **kwargs):
            self.prompts.append(prompt)
            # Discovery left behind an ignored artifact, which replay must remove.
            (root / "artifact").write_text("from interactive discovery")
            if isinstance(self.report, Exception):
                raise self.report
            return TurnResult(text=self.report, settled=True)

        def bash(self, command, **kwargs):
            self.commands.append(command)
            proc = subprocess.run(command, shell=True, cwd=self.cwd, capture_output=True, text=True)
            return {
                "success": True,
                "data": {
                    "output": proc.stdout + proc.stderr,
                    "exitCode": proc.returncode,
                    "cancelled": False,
                },
            }

        def close(self):
            pass

    monkeypatch.setattr("sadoc.pipeline.PiSession", LocalSession)
    yield worker, git, LocalSession
    state.close()


def test_setup_is_saved_and_replayed_in_the_verification_shell(project):
    worker, git, session = project
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "ok"
    saved = worker.state.setup("alice/repo")
    assert "export BUILD_MARKER" in saved["setup_command"]
    assert "production smoke" in saved["ci_only"]
    assert saved["fingerprint"]
    assert not (git.path / "artifact").exists()
    assert worker.run_verify(git, "alice/repo", session(cwd=git.path))[0]


def test_a_recipe_that_stops_working_is_thrown_away(project):
    """A cached recipe that fails must not be trusted for ever."""
    worker, git, session = project
    worker.ensure_build_setup("alice", "repo", "main", git)
    assert worker.state.setup("alice/repo")["status"] == "ok"

    # A later run replays the cached recipe, and it fails.
    next_run = Worker(worker.cfg, worker.gh, worker.state)
    next_run._invalidate_recipe("alice", "repo", "exit 1\nthe build is broken")
    assert worker.state.setup("alice/repo")["status"] == "stale"
    assert worker.verify_command("alice/repo") is None

    # The next run works it out again rather than failing the same way.
    third = Worker(worker.cfg, worker.gh, worker.state)
    assert third.ensure_build_setup("alice", "repo", "main", git)["status"] == "ok"
    assert len(session.prompts) == 2


def test_a_recipe_worked_out_this_run_is_not_thrown_away(project):
    """A recipe that was just discovered has already recorded its failure."""
    worker, git, session = project
    session.report = REPORT.replace("export BUILD_MARKER=ready", "export BUILD_MARKER=wrong")
    worker.ensure_build_setup("alice", "repo", "main", git)
    worker._invalidate_recipe("alice", "repo", "exit 1")
    assert worker.state.setup("alice/repo")["status"] != "stale"


def test_generated_artifacts_cannot_make_an_incomplete_recipe_pass(project):
    worker, git, session = project
    session.report = REPORT.replace(
        "export BUILD_MARKER=ready && printf generated > artifact", "export BUILD_MARKER=ready"
    )
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "validation_failed"
    assert worker.verify_command("alice/repo") is None
    assert "exit" in result["notes"].lower()
    assert not (git.path / "artifact").exists()


def test_cache_reused_across_workers_but_refreshed_when_tree_changes(project):
    worker, git, session = project
    worker.ensure_build_setup("alice", "repo", "main", git)
    second = Worker(worker.cfg, worker.gh, worker.state)
    assert second.ensure_build_setup("alice", "repo", "main", git)["status"] == "ok"
    assert len(session.prompts) == 1
    (git.path / "README.md").write_text("New build instructions\n")
    git.stage_all()
    git._run("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "change")
    assert second.ensure_build_setup("alice", "repo", "main", git)["status"] == "ok"
    assert len(session.prompts) == 2


def test_cache_does_not_cross_owners_or_trust_legacy_entries(project):
    worker, git, session = project
    worker.state.record_setup("repo", "legacy-command")
    worker.ensure_build_setup("alice", "repo", "main", git)
    worker.ensure_build_setup("bob", "repo", "main", git)
    assert len(session.prompts) == 2
    assert worker.state.setup("alice/repo")["status"] == "ok"
    assert worker.state.setup("bob/repo")["status"] == "ok"


@pytest.mark.parametrize(
    ("report", "status", "detail"),
    [
        (PiError("Connection refused"), "agent_failed", "Connection refused"),
        ("I could not find a command.", "invalid_report", "VERIFY"),
        (
            "STATUS: blocked\nVERIFY: NONE\nPREREQUISITES: libfoo\nNOTES: missing library",
            "blocked",
            "missing library",
        ),
        (
            "STATUS: baseline_failed\nVERIFY: make test\nNOTES: existing test fails",
            "baseline_failed",
            "existing test fails",
        ),
    ],
)
def test_failures_are_actionable_and_not_repeated_for_each_update_group(
    project, report, status, detail
):
    worker, git, session = project
    session.report = report
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == status
    assert detail in result["notes"]
    worker.ensure_build_setup("alice", "repo", "main", git)
    assert len(session.prompts) == 1
    assert worker.verify_command("alice/repo") is None


def test_explicit_override_takes_precedence_without_discovery(project):
    worker, git, session = project
    worker.cfg.repos = {"repo": {"verify": "make test"}, "alice/repo": {"verify": "make check"}}
    assert worker.ensure_build_setup("alice", "repo", "main", git)["status"] == "ok"
    assert worker.verify_command("alice/repo") == "make check"
    assert worker.verify_command("bob/repo") == "make test"
    assert not session.prompts


def test_verification_requires_a_recipe_and_a_sandbox_session(project):
    worker, git, _ = project
    assert worker.run_verify(git, "alice/repo")[0] is False
    worker.cfg.repos = {"repo": {"verify": "true"}}
    assert worker.run_verify(git, "alice/repo")[0] is False


def test_old_database_migrates_without_losing_setup(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute(
            "CREATE TABLE repo_setup (repo TEXT PRIMARY KEY, verify_command TEXT, "
            "prerequisites TEXT, notes TEXT, status TEXT, created_at REAL, updated_at REAL)"
        )
        c.execute("INSERT INTO repo_setup VALUES ('old', 'make test', '', '', 'ok', 1, 1)")
    state = State(path)
    try:
        assert state.setup("old")["verify_command"] == "make test"
        state.record_setup(
            "alice/repo",
            "make test",
            setup_command="make generate",
            fingerprint="tree",
            ci_only="deployment",
            verify_output="passed",
        )
        assert state.setup("alice/repo")["setup_command"] == "make generate"
    finally:
        state.close()


def test_pipeline_stops_before_bump_on_failed_setup(project):
    worker, git, session = project
    session.report = PiError("endpoint unavailable")
    result = worker.process_repo("alice", "repo", "A", [], dry_run=True)
    assert result.status == "failed"
    assert "agent_failed" in result.detail
    assert "endpoint unavailable" in result.detail
    assert git.current_branch() == "main"
    assert len(session.prompts) == 1


def test_pipeline_uses_the_qualified_recipe(project):
    worker, git, session = project
    discover = session.prompt

    def prompt(self, text, **kwargs):
        if "STATUS: ready" in text:
            return discover(self, text, **kwargs)
        # Stand in for a dependency edit, then let the real verification path run.
        (git.path / "README.md").write_text("Dependency update\n")
        return TurnResult(text="NOTES: Updated dependencies", settled=True)

    session.prompt = prompt
    result = worker.process_repo("alice", "repo", "A", [], dry_run=True)
    assert result.status == "skipped"
    assert "would open PR" in result.detail
    assert len(session.commands) == 2  # clean baseline replay, then updated-tree verification
    assert worker.state.setup("alice/repo")["status"] == "ok"


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "error": "shell unavailable"},
        {"success": True, "data": {"exitCode": 0, "cancelled": True}},
        {"success": True, "data": {"output": "no exit code"}},
    ],
)
def test_shell_failures_cannot_validate_a_recipe(project, response):
    worker, git, session = project
    session.bash = lambda *a, **kw: response
    assert worker.ensure_build_setup("alice", "repo", "main", git)["status"] == "validation_failed"


def test_failed_setup_cannot_be_hidden_by_successful_verification(project):
    worker, git, session = project
    session.report = "STATUS: ready\nSETUP: false\nVERIFY: printf should-not-run\nNOTES: test"
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "validation_failed"
    assert "should-not-run" not in result["verify_output"]


def test_replay_uses_original_commit_even_if_discovery_commits_changes(project):
    worker, git, session = project
    initial_sha = git.commit_sha()

    def prompt(self, text, **kwargs):
        (git.path / "shortcut").write_text("not part of the original checkout")
        git.stage_all()
        git._run(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "unexpected discovery edit",
        )
        return TurnResult(text="STATUS: ready\nVERIFY: test -f shortcut", settled=True)

    session.prompt = prompt
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "validation_failed"
    assert git.commit_sha() == initial_sha
    assert not (git.path / "shortcut").exists()


def test_failed_setup_and_list_cannot_be_hidden_by_later_verification(project):
    worker, git, session = project
    session.report = (
        "STATUS: ready\nSETUP: false && printf setup-should-not-run\n"
        "VERIFY: printf verification-ran\nNOTES: test"
    )
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "validation_failed"
    assert "verification-ran" not in result["verify_output"]


def test_discovery_fixture_clones_do_not_survive_clean_replay(project):
    worker, git, session = project

    def prompt(self, text, **kwargs):
        fixture = git.path / "fixtures"
        fixture.mkdir()
        Git(fixture)._run("init")
        (fixture / "data").write_text("fixture data")
        return TurnResult(text="STATUS: ready\nVERIFY: test -f fixtures/data", settled=True)

    session.prompt = prompt
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "validation_failed"
    assert not (git.path / "fixtures").exists()


def test_discovery_submodule_checkout_must_be_part_of_setup(project):
    worker, git, session = project
    source = git.path.parent / "submodule-source"
    source.mkdir()
    submodule = Git(source)
    submodule._run("init")
    (source / "fixture").write_text("data")
    submodule.stage_all()
    submodule._run(
        "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixture"
    )
    git._run("-c", "protocol.file.allow=always", "submodule", "add", str(source), "fixtures")
    git.stage_all()
    git._run(
        "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "submodule"
    )
    git._run("submodule", "deinit", "--force", "--all")

    def prompt(self, text, **kwargs):
        git._run("-c", "protocol.file.allow=always", "submodule", "update", "--init")
        return TurnResult(text="STATUS: ready\nVERIFY: test -f fixtures/fixture", settled=True)

    session.prompt = prompt
    assert worker.ensure_build_setup("alice", "repo", "main", git)["status"] == "validation_failed"


def test_cleanup_failure_preserves_the_agent_error_and_records_it(project, monkeypatch):
    from sadoc.gitops import GitError

    worker, git, session = project
    session.report = PiError("endpoint unavailable")

    def fail_cleanup(*args, **kwargs):
        raise GitError("cannot remove generated files")

    monkeypatch.setattr(git, "clean", fail_cleanup)
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "agent_failed"
    assert "endpoint unavailable" in result["notes"]
    assert "cannot remove generated files" in result["notes"]
    assert worker.state.setup("alice/repo")["status"] == "agent_failed"
    assert worker.verify_command("alice/repo") is None


def test_failed_final_cleanup_cannot_cache_a_successful_recipe(project, monkeypatch):
    from sadoc.gitops import GitError

    worker, git, session = project
    clean = git.clean
    calls = 0

    def fail_final_cleanup(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GitError("cannot reset the checkout")
        clean(*args, **kwargs)

    monkeypatch.setattr(git, "clean", fail_final_cleanup)
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "cleanup_failed"
    assert "cannot reset the checkout" in result["notes"]
    assert worker.state.setup("alice/repo")["status"] == "cleanup_failed"
    assert worker.verify_command("alice/repo") is None


def test_session_close_failure_still_cleans_the_checkout(project):
    worker, git, session = project
    session.report = PiError("endpoint unavailable")

    def fail_close(self):
        raise OSError("cannot close agent process")

    session.close = fail_close
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "agent_failed"
    assert "endpoint unavailable" in result["notes"]
    assert "cannot close agent process" in result["notes"]
    assert not (git.path / "artifact").exists()


def test_new_run_retries_failed_discovery_without_a_source_change(project):
    worker, git, session = project
    session.report = PiError("endpoint unavailable")
    assert worker.ensure_build_setup("alice", "repo", "main", git)["status"] == "agent_failed"
    session.report = REPORT
    next_run = Worker(worker.cfg, worker.gh, worker.state)
    assert next_run.ensure_build_setup("alice", "repo", "main", git)["status"] == "ok"
    assert next_run.verify_command("alice/repo") is not None
    assert len(session.prompts) == 2


def commit_build_inputs(git):
    git.stage_all()
    git._run("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "inputs")


def test_cache_survives_unrelated_source_test_and_doc_changes(project):
    worker, git, session = project
    session.report = REPORT + "\nINPUTS: []"
    first = worker.ensure_build_setup("alice", "repo", "main", git)
    (git.path / "app.py").write_text('print("new source")\n')
    (git.path / "test_app.py").write_text("def test_app(): assert True\n")
    (git.path / "CHANGELOG.md").write_text("New release\n")
    commit_build_inputs(git)
    next_run = Worker(worker.cfg, worker.gh, worker.state)
    cached = next_run.ensure_build_setup("alice", "repo", "main", git)
    assert cached["fingerprint"] == first["fingerprint"]
    assert len(session.prompts) == 1
    assert worker.state.setup("alice/repo")["build_inputs"] == "[]"


@pytest.mark.parametrize(
    "path",
    [
        "package.json",
        "packages/backend/pyproject.toml",
        ".github/workflows/ci.yml",
        ".tool-versions",
        "scripts/check.sh",
        "app/vite.config.ts",
        "crates/core/build.rs",
        "ruff.toml",
        "Makefile.am",
        "cmake/toolchain.cmake",
    ],
)
@pytest.mark.parametrize("change", ["add", "edit", "delete"])
def test_build_config_changes_refresh_cached_recipe(project, path, change):
    worker, git, session = project
    session.report = REPORT + "\nINPUTS: []"
    file = git.path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    if change != "add":
        file.write_text("old configuration\n")
        commit_build_inputs(git)
    worker.ensure_build_setup("alice", "repo", "main", git)
    if change == "delete":
        file.unlink()
    else:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("new configuration\n")
    commit_build_inputs(git)
    assert worker.ensure_build_setup("alice", "repo", "main", git)["status"] == "ok"
    assert len(session.prompts) == 2


@pytest.mark.parametrize("change", ["add", "delete"])
def test_a_lockfile_appearing_or_going_refreshes_cached_recipe(project, change):
    """A lockfile records what was resolved, so its content is not the recipe."""
    worker, git, session = project
    session.report = REPORT + "\nINPUTS: []"
    file = git.path / "pnpm-lock.yaml"
    if change == "delete":
        file.write_text("lockfileVersion: '9.0'\n")
        commit_build_inputs(git)
    worker.ensure_build_setup("alice", "repo", "main", git)
    if change == "delete":
        file.unlink()
    else:
        file.write_text("lockfileVersion: '9.0'\n")
    commit_build_inputs(git)
    assert worker.ensure_build_setup("alice", "repo", "main", git)["status"] == "ok"
    assert len(session.prompts) == 2


def test_a_lockfile_changing_content_keeps_the_cached_recipe(project):
    """Resolving different versions does not change how the project is built."""
    worker, git, session = project
    session.report = REPORT + "\nINPUTS: []"
    file = git.path / "pnpm-lock.yaml"
    file.write_text("lockfileVersion: '9.0'\n")
    commit_build_inputs(git)
    worker.ensure_build_setup("alice", "repo", "main", git)
    file.write_text("lockfileVersion: '9.0'\npackages:\n  react@19.0.0: {}\n")
    commit_build_inputs(git)
    assert worker.ensure_build_setup("alice", "repo", "main", git)["status"] == "ok"
    assert len(session.prompts) == 1


def test_custom_build_inputs_and_new_files_in_watched_directories_refresh(project):
    worker, git, session = project
    (git.path / "automation").mkdir()
    (git.path / "automation" / "driver.py").write_text("old driver")
    (git.path / "recipe.py").write_text("old recipe")
    commit_build_inputs(git)
    session.report = REPORT + '\nINPUTS: ["automation/", "recipe.py"]'
    worker.ensure_build_setup("alice", "repo", "main", git)
    (git.path / "recipe.py").write_text("new recipe")
    commit_build_inputs(git)
    next_run = Worker(worker.cfg, worker.gh, worker.state)
    next_run.ensure_build_setup("alice", "repo", "main", git)
    (git.path / "automation" / "new_step.py").write_text("new step")
    commit_build_inputs(git)
    next_run.ensure_build_setup("alice", "repo", "main", git)
    assert len(session.prompts) == 3


def test_missing_input_list_keeps_conservative_fallback(project):
    worker, git, session = project
    worker.ensure_build_setup("alice", "repo", "main", git)
    (git.path / "unconventional.py").write_text("changed")
    commit_build_inputs(git)
    worker.ensure_build_setup("alice", "repo", "main", git)
    assert len(session.prompts) == 2


@pytest.mark.parametrize(
    "inputs",
    ["not-json", '"file.py"', "[42]", '["../outside"]', '["/absolute"]', '["scripts/*.py"]'],
)
def test_invalid_build_inputs_are_not_cached_as_success(project, inputs):
    worker, git, session = project
    session.report = REPORT + "\nINPUTS: " + inputs
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "invalid_report"
    assert "INPUTS" in result["notes"]
    assert worker.verify_command("alice/repo") is None


def test_network_policy_change_refreshes_recipe(project):
    worker, git, session = project
    session.report = REPORT + "\nINPUTS: []"
    worker.ensure_build_setup("alice", "repo", "main", git)
    worker.cfg.allow_network = False
    worker.ensure_build_setup("alice", "repo", "main", git)
    assert len(session.prompts) == 2


def test_watched_symlink_tracks_its_target(project):
    worker, git, session = project
    (git.path / "driver.py").write_text("old driver")
    (git.path / "build.py").symlink_to("driver.py")
    commit_build_inputs(git)
    session.report = REPORT + '\nINPUTS: ["build.py"]'
    worker.ensure_build_setup("alice", "repo", "main", git)
    (git.path / "driver.py").write_text("new driver")
    commit_build_inputs(git)
    worker.ensure_build_setup("alice", "repo", "main", git)
    assert len(session.prompts) == 2


@pytest.mark.parametrize("inputs", ["[]", '["scripts/check.py"]'])
def test_script_directory_symlinks_track_custom_target_files(project, inputs):
    worker, git, session = project
    (git.path / "automation").mkdir()
    (git.path / "automation" / "check.py").write_text("old script")
    (git.path / "scripts").symlink_to("automation", target_is_directory=True)
    commit_build_inputs(git)
    session.report = REPORT + "\nINPUTS: " + inputs
    worker.ensure_build_setup("alice", "repo", "main", git)
    (git.path / "automation" / "check.py").write_text("new script")
    commit_build_inputs(git)
    worker.ensure_build_setup("alice", "repo", "main", git)
    assert len(session.prompts) == 2


def test_symlink_parent_components_resolve_after_intermediate_symlinks(project):
    worker, git, session = project
    (git.path / "custom" / "nested").mkdir(parents=True)
    (git.path / "custom" / "nested" / "keep").write_text("tracked directory")
    (git.path / "custom" / "driver.py").write_text("old driver")
    (git.path / "alias").symlink_to("custom/nested", target_is_directory=True)
    (git.path / "build.py").symlink_to("alias/../driver.py")
    commit_build_inputs(git)
    session.report = REPORT + '\nINPUTS: ["build.py"]'
    worker.ensure_build_setup("alice", "repo", "main", git)
    (git.path / "custom" / "driver.py").write_text("new driver")
    commit_build_inputs(git)
    worker.ensure_build_setup("alice", "repo", "main", git)
    assert len(session.prompts) == 2


@pytest.mark.parametrize("target", ["build.py", "../external", "/opt/build.py"])
def test_unresolvable_symlinks_use_conservative_cache_invalidation(project, target):
    worker, git, session = project
    (git.path / "build.py").symlink_to(target)
    commit_build_inputs(git)
    session.report = REPORT + '\nINPUTS: ["build.py"]'
    worker.ensure_build_setup("alice", "repo", "main", git)
    (git.path / "app.py").write_text("new source")
    commit_build_inputs(git)
    worker.ensure_build_setup("alice", "repo", "main", git)
    assert len(session.prompts) == 2


def test_a_working_recipe_is_not_blocked_by_listed_toolchain(project):
    """A model may list what the build needs and still have a working recipe.

    PREREQUISITES is for requirements the sandbox could not meet. Treating any
    non-empty field as blocking threw away recipes that had been verified.
    """
    worker, git, session = project
    session.report = (
        "STATUS: ready\n"
        "SETUP: export BUILD_MARKER=ready\n"
        "VERIFY: printf should-not-run\n"
        "PREREQUISITES: Go 1.26.0 and Bun, both already installed in the sandbox\n"
        "NOTES: verified\n"
    )
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "ok"
    assert worker.verify_command("alice/repo") is not None


def test_unmet_prerequisites_still_block_when_the_status_is_not_ready(project):
    worker, git, session = project
    session.report = (
        "STATUS: blocked\n"
        "VERIFY: printf should-not-run\n"
        "PREREQUISITES: libfoo-dev is missing and cannot be installed\n"
        "NOTES: cannot verify\n"
    )
    result = worker.ensure_build_setup("alice", "repo", "main", git)
    assert result["status"] == "blocked"
    assert worker.verify_command("alice/repo") is None
