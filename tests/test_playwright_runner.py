"""Run the real browser helper with fixture packages and a local runtime boundary."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "extensions/playwright-runner.mjs"


@pytest.fixture
def browser_repo(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node is needed for the Playwright helper")
    repo = tmp_path / "repo with spaces"
    package = repo / "node_modules/playwright"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": "1.63.0", "main": "index.cjs"}))
    (package / "index.cjs").write_text("""
const fs = require('node:fs');
exports.chromium = exports.firefox = exports.webkit = null;
for (const name of ['chromium', 'firefox', 'webkit']) {
  exports[name] = {launch: async options => {
    fs.appendFileSync('events', name + ':' + JSON.stringify(options) + '\\n');
    if (fs.existsSync('fail-browser') && name === 'webkit') throw Error('missing WebKit library');
    return {newPage: async () => ({setContent: async () => {}, title: async () => 'sadoc'}),
      close: async () => fs.appendFileSync('events', name + ':closed\\n')};
  }};
}
""")
    pg = repo / "node_modules/pg"
    pg.mkdir()
    (pg / "index.js").write_text("""
const fs = require('node:fs');
exports.Client = class {
  constructor(options) { if (options.connectionString !== 'postgres://local/test') throw Error('bad DB URL'); }
  async connect() { fs.appendFileSync('events', 'db:connect\\n'); }
  async query(sql) {
    if (sql !== 'SELECT 1') throw Error('expected real query');
    if (fs.existsSync('fail-db')) throw Error('connection refused');
    fs.appendFileSync('events', 'db:query\\n');
  }
  async end() { fs.appendFileSync('events', 'db:closed\\n'); }
};
""")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runtime = bin_dir / "podman"
    runtime.write_text("""#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
Path('runtime.json').write_text(json.dumps(args))
image = next(i for i, a in enumerate(args) if a.startswith('mcr.microsoft.com/'))
env = {'PATH': os.environ['PATH']}
for i, a in enumerate(args[:image]):
    if a == '--env':
        k, v = args[i+1].split('=', 1)
        env[k] = v
sys.exit(subprocess.run(args[image+1:], env=env).returncode)
""")
    runtime.chmod(0o755)
    cache = tmp_path / "cache"
    cache.mkdir()
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "SADOC_SANDBOX": "1",
        "SADOC_CONTAINER_RUNTIME": "podman",
        "SADOC_WORKSPACE": str(repo),
        "SADOC_TOOL_CACHE": str(cache),
        "PLAYWRIGHT_BROWSERS_PATH": "/wrong/host/browsers",
        "GH_TOKEN": "never-forward",
        "TEST_DATABASE_URL": "postgres://local/test",
    }

    def run(*args):
        return subprocess.run(
            ["node", str(RUNNER), *args], cwd=repo, env=env, text=True, capture_output=True
        )

    return repo, env, run


def test_runner_pins_image_and_checks_browsers_and_database_before_tests(browser_repo):
    repo, _, run = browser_repo
    proc = run(
        "--network",
        "test-net",
        "--postgres-env",
        "TEST_DATABASE_URL",
        "--",
        "bash",
        "-c",
        "echo suite >> events",
    )
    assert proc.returncode == 0, proc.stderr
    argv = json.loads((repo / "runtime.json").read_text())
    assert "mcr.microsoft.com/playwright:v1.63.0-noble" in argv
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in argv
    assert "--userns=keep-id" in argv
    assert "test-net" in argv
    assert not any("never-forward" in a or "/wrong/host" in a for a in argv)
    events = (repo / "events").read_text()
    assert '"chromiumSandbox":true' in events
    assert events.index("webkit:closed") < events.index("db:query") < events.index("suite")


@pytest.mark.parametrize(
    ("marker", "error"),
    [("fail-browser", "missing WebKit library"), ("fail-db", "connection refused")],
)
def test_failed_preflight_prevents_suite(browser_repo, marker, error):
    repo, _, run = browser_repo
    (repo / marker).touch()
    proc = run("--postgres-env", "TEST_DATABASE_URL", "--", "bash", "-c", "echo suite >> events")
    assert proc.returncode != 0
    assert error in proc.stderr
    assert "suite" not in (repo / "events").read_text()


def test_runner_preserves_suite_failure(browser_repo):
    _, _, run = browser_repo
    assert run("--", "bash", "-c", "exit 23").returncode == 23


@pytest.mark.parametrize("version", ["^1.63.0", "1.63.0-beta", "1.63.0; true"])
def test_runner_rejects_unpinned_or_unsupported_versions(browser_repo, version):
    repo, _, run = browser_repo
    (repo / "node_modules/playwright/package.json").write_text(json.dumps({"version": version}))
    assert run("--", "true").returncode != 0
    assert not (repo / "runtime.json").exists()


def test_runner_requires_the_sandbox_and_enabled_runtime(browser_repo):
    repo, env, run = browser_repo
    env.pop("SADOC_CONTAINER_RUNTIME")
    assert run("--", "true").returncode != 0
    assert not (repo / "runtime.json").exists()


def test_runner_resolves_playwright_test_when_playwright_is_not_hoisted(browser_repo):
    repo, _, run = browser_repo
    package = repo / "node_modules/@playwright/test"
    package.parent.mkdir()
    (repo / "node_modules/playwright").rename(package)
    assert run("--", "true").returncode == 0


def test_runner_rejects_conflicting_installed_browser_versions(browser_repo):
    repo, _, run = browser_repo
    package = repo / "node_modules/@playwright/test"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"version":"1.60.0"}')
    proc = run("--", "true")
    assert proc.returncode != 0
    assert "different versions" in proc.stderr
    assert not (repo / "runtime.json").exists()


def test_missing_database_variable_does_not_start_a_container(browser_repo):
    repo, env, run = browser_repo
    env.pop("TEST_DATABASE_URL")
    proc = run("--postgres-env", "TEST_DATABASE_URL", "--", "true")
    assert proc.returncode != 0
    assert "database variable" in proc.stderr
    assert not (repo / "runtime.json").exists()
