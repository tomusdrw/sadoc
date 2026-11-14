"""The container policy.

A container is only useful if it cannot reach further than the workspace. A
rootless container already runs as the user's own uid; these rules stop it
reaching the host namespaces, the runtime socket, or the machine.

The rules live in `sadoc.policy` and are handed to the sandbox extension in
the environment, so there is one list of them.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from sadoc import policy


def violation(command: str) -> str | None:
    """The reason a container command is not allowed, or None if it is fine."""
    return policy.violation(command)


def test_a_plain_container_run_is_allowed():
    assert violation('podman run --rm -v "$PWD:/w" -w /w postgres:17 pg_ctl start') is None


def test_a_volume_inside_the_workspace_is_allowed():
    assert violation("podman run --rm -v .:/w -w /w node:22 npm test") is None


def test_privileged_is_not_allowed():
    assert violation("podman run --privileged -v /:/host alpine") is not None


def test_the_host_network_is_not_allowed():
    assert violation("podman run --network=host alpine") is not None
    assert violation("podman run --net host alpine") is not None


def test_the_host_namespaces_are_not_allowed():
    for flag in ("--pid=host", "--ipc=host", "--uts=host", "--userns=host"):
        assert violation(f"podman run {flag} alpine") is not None


def test_extra_capabilities_are_not_allowed():
    assert violation("podman run --cap-add=SYS_ADMIN alpine") is not None
    assert violation("podman run --cap-add ALL alpine") is not None


def test_an_unconfined_seccomp_profile_is_not_allowed():
    assert violation("podman run --security-opt seccomp=unconfined alpine") is not None


def test_a_host_device_is_not_allowed():
    assert violation("podman run --device /dev/sda alpine") is not None


def test_the_runtime_socket_is_not_allowed():
    assert violation("podman run -v /run/user/1000/podman/podman.sock:/x alpine") is not None
    assert violation("docker run -v /var/run/docker.sock:/x alpine") is not None


def test_host_directories_are_not_allowed():
    assert violation("podman run -v /etc:/etc alpine") is not None
    assert violation("podman run -v /:/host alpine") is not None
    assert violation("podman run --mount source=/etc,target=/etc alpine") is not None


def test_the_allowed_runtime_is_the_only_one_that_may_be_used():
    assert policy.runtime_violation("podman run alpine", "podman") is None
    assert policy.runtime_violation("docker run alpine", "podman") == "docker"


def test_docker_is_never_allowed():
    """The Docker socket is root-equivalent, so the daemon is never reached."""
    assert policy.runtime_violation("docker run alpine", None) == "docker"
    assert policy.runtime_violation("docker run alpine", "docker") == "docker"


def test_the_rules_travel_to_the_extension_as_json():
    """The extension reads the rules from the environment, so they travel."""
    payload = json.loads(policy.policy_json())
    assert payload["container"]
    assert payload["runtimes"]
    for pattern, label in payload["container"]:
        assert isinstance(pattern, str)
        assert isinstance(label, str) and label
        re.compile(pattern, re.I)
    for name, pattern, may_enable in payload["runtimes"]:
        assert isinstance(name, str) and name
        assert isinstance(pattern, str)
        assert isinstance(may_enable, bool)
        re.compile(pattern, re.I)
    # Docker may never be enabled.
    assert [r for r in payload["runtimes"] if r[0] == "docker"][0][2] is False


def test_the_extension_has_no_rules_of_its_own():
    """One list of rules: the Python's, not the Python's and the extension's."""
    source = (Path(__file__).resolve().parent.parent / "extensions" / "sandbox.ts").read_text()
    assert "SADOC_CONTAINER_POLICY" in source
    assert "--privileged" not in source
    assert "--cap-add" not in source


def test_the_rules_are_only_handed_over_when_containers_are_allowed():
    from sadoc.piworker import PiModel, PiSession

    spec = PiModel(name="x", base_url="http://127.0.0.1:1/v1", model="x", context=1024)
    off = PiSession(cwd="/tmp", model=spec, agent_dir="/tmp/agent")
    assert "SADOC_CONTAINER_POLICY" not in off._build_env()

    on = PiSession(cwd="/tmp", model=spec, agent_dir="/tmp/agent", allow_containers=True)
    env = on._build_env()
    assert env["SADOC_ALLOW_CONTAINERS"] == "1"
    assert env["SADOC_CONTAINER_RUNTIME"] == "podman"
    assert json.loads(env["SADOC_CONTAINER_POLICY"]) == json.loads(policy.policy_json())


def test_docker_cannot_be_configured_as_the_runtime(tmp_path):
    from sadoc.config import ConfigError, load

    path = tmp_path / "config.toml"
    path.write_text('[sandbox]\nallow_containers = true\ncontainer_runtime = "docker"\n')
    try:
        load(path)
    except ConfigError as exc:
        assert "docker" in str(exc)
    else:
        raise AssertionError("docker should not be accepted as the container runtime")


def test_the_runtime_is_read_from_the_sandbox_section(tmp_path):
    from sadoc.config import load

    path = tmp_path / "config.toml"
    path.write_text('[sandbox]\nallow_containers = true\ncontainer_runtime = "podman"\n')
    cfg = load(path)
    assert cfg.allow_containers is True
    assert cfg.container_runtime == "podman"


def test_the_extension_enforces_the_same_rules(tmp_path):
    """Run the real extension against the real rules, if bun is around."""
    import shutil
    import subprocess

    bun = shutil.which("bun")
    if not bun:
        import pytest

        pytest.skip("bun is not installed")

    root = Path(__file__).resolve().parent.parent
    stub = tmp_path / "node_modules" / "@earendil-works" / "pi-coding-agent"
    stub.mkdir(parents=True)
    (stub / "package.json").write_text(
        '{"name": "@earendil-works/pi-coding-agent", "version": "0.0.0",'
        ' "type": "module", "main": "index.js"}'
    )
    (stub / "index.js").write_text(
        "import { spawnSync } from 'node:child_process';\n"
        "export function createLocalBashOperations() { return { exec: async (command, cwd) => {"
        " const p = spawnSync('bash', ['-c', command], {cwd, encoding: 'utf8', timeout: 5000});"
        " return { output: p.stdout + p.stderr, exitCode: p.status }; } }; }\n"
    )
    (tmp_path / "sandbox.ts").write_text((root / "extensions" / "sandbox.ts").read_text())
    (tmp_path / "run.ts").write_text((root / "tests" / "sandbox_policy.ts").read_text())
    (tmp_path / "cache").mkdir()

    result = subprocess.run(
        [bun, "run", "run.ts"],
        cwd=tmp_path,
        env={
            **os.environ,
            "SADOC_CONTAINER_POLICY": policy.policy_json(),
            "SADOC_WORKSPACE": str(tmp_path),
            "SADOC_DISCOVERY": "1",
            "SADOC_ALLOW_CONTAINERS": "0",
            "SADOC_CACHE_DIR": str(tmp_path / "cache"),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "behave as expected" in result.stdout


def test_the_container_helper_reaches_the_runtime_and_applies_the_policy(tmp_path):
    """The sandbox reaches the runtime, and the runtime stays inside the policy."""
    import shutil
    import subprocess

    bun = shutil.which("bun")
    if not bun:
        import pytest

        pytest.skip("bun is not installed")
    runtime = os.environ.get("SADOC_CONTAINER_RUNTIME", "podman")
    if not shutil.which(runtime):
        import pytest

        pytest.skip(f"{runtime} is not installed")

    root = Path(__file__).resolve().parent.parent
    stub = tmp_path / "node_modules" / "@earendil-works" / "pi-coding-agent"
    stub.mkdir(parents=True)
    (stub / "package.json").write_text(
        '{"name": "@earendil-works/pi-coding-agent", "version": "0.0.0",'
        ' "type": "module", "main": "index.js"}'
    )
    (stub / "index.js").write_text(
        "export function createLocalBashOperations() { return { exec: async () =>"
        " ({ output: '', exitCode: 0 }) }; }\n"
    )
    (tmp_path / "sandbox.ts").write_text((root / "extensions" / "sandbox.ts").read_text())
    (tmp_path / "run.ts").write_text((root / "tests" / "container_helper.ts").read_text())
    (tmp_path / "repo").mkdir()

    result = subprocess.run(
        [bun, "run", "run.ts"],
        cwd=tmp_path,
        env={
            **os.environ,
            "SADOC_CONTAINER_POLICY": policy.policy_json(),
            "SADOC_WORKSPACE": str(tmp_path / "repo"),
            "SADOC_CACHE_DIR": str(tmp_path / "cache"),
            "SADOC_ALLOW_CONTAINERS": "1",
            "SADOC_ALLOW_NET": "1",
            "SADOC_CONTAINER_RUNTIME": runtime,
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "container helper checks pass" in result.stdout


def test_helper_tags_containers_and_bounds_runtime_calls(tmp_path):
    import shutil
    import subprocess
    import tempfile

    import pytest

    bun = shutil.which("bun")
    if not bun or not shutil.which("bwrap"):
        pytest.skip("bun and bubblewrap required")
    root = Path(__file__).resolve().parent.parent
    # Unix socket paths must fit sockaddr_un, even with pytest's long test names.
    with tempfile.TemporaryDirectory(prefix="sadoc-helper-") as directory:
        work = Path(directory)
        stub = work / "node_modules/@earendil-works/pi-coding-agent"
        stub.mkdir(parents=True)
        (stub / "package.json").write_text('{"type":"module","main":"index.js"}')
        (stub / "index.js").write_text("export function createLocalBashOperations() {}")
        (work / "sandbox.ts").write_text((root / "extensions/sandbox.ts").read_text())
        (work / "run.ts").write_text((root / "tests/container_lifecycle.ts").read_text())
        runtime = work / "podman"
        runtime.write_text(
            "#!/usr/bin/python\nimport json,os,sys,time\n"
            "assert os.getpgrp() == int(os.environ['FAKE_EXPECTED_PGID'])\n"
            'with open(os.environ["FAKE_RUNTIME_LOG"], "a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
            'if sys.argv[1] == "wait": time.sleep(30)\n'
        )
        runtime.chmod(0o755)
        workspace = work / "repo with spaces 'quote"
        workspace.mkdir()
        result = subprocess.run(
            [bun, "run", "run.ts"],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=20,
            env={
                **os.environ,
                "PATH": f"{work}:" + os.environ["PATH"],
                "SADOC_CONTAINER_RUNTIME": "podman",
                "SADOC_SESSION_ID": "test-session",
                "SADOC_CONTAINER_POLICY": policy.policy_json(),
                "SADOC_ALLOW_CONTAINERS": "1",
                "SADOC_CACHE_DIR": str(work / "cache"),
                "SADOC_WORKSPACE": str(workspace),
                "FAKE_RUNTIME_LOG": str(work / "runtime.log"),
                "FAKE_EXPECTED_PGID": str(os.getpgrp()),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_container_ownership_label_cannot_be_overridden():
    assert violation("podman run --label io.sadoc.session=someone-else example.invalid/image")


def test_docker_registry_is_not_the_docker_runtime():
    assert (
        policy.runtime_violation("podman run --rm docker.io/library/postgres:17-alpine", "podman")
        is None
    )
    assert policy.runtime_violation("/usr/bin/docker run alpine", "podman") == "docker"
