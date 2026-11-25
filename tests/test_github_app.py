from sadoc import config


def test_github_app_requires_app_id(tmp_path):
    settings = tmp_path / "config.toml"
    settings.write_text(
        'owner = "tomusdrw"\n[github]\nmode = "app"\napp_id = 5901719\n'
        "[sandbox]\nallow_containers = true\n"
    )
    cfg = config.load(settings)
    assert cfg.github_app_id == 5901719
    assert cfg.allow_containers
    assert config.Config().github_app_id is None


def test_app_token_refreshes_before_expiry(monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from sadoc.github_app import GitHubApp

    now = 1000
    monkeypatch.setattr("sadoc.github_app.time.time", lambda: now)
    app = GitHubApp(5901719, "tomusdrw")
    monkeypatch.setattr(app, "_jwt", lambda: "jwt")
    expires_at = datetime.fromtimestamp(now + 3600, UTC).isoformat()
    request = Mock(
        side_effect=[
            [{"id": 11, "account": {"login": "tomusdrw"}}],
            {"token": "first", "expires_at": expires_at},
            {"token": "second", "expires_at": expires_at},
        ]
    )
    monkeypatch.setattr(app, "_request", request)
    assert app.token() == app.token() == "first"
    assert request.call_count == 2
    now += 3500
    assert app.token() == "second"
    assert request.call_args.args == ("/app/installations/11/access_tokens", "jwt", "POST")


def test_app_auth_is_scoped_to_gh_subprocess(monkeypatch):
    import os
    from subprocess import CompletedProcess
    from unittest.mock import Mock

    from sadoc.gh import Gh

    app = Mock()
    app.token.return_value = "app-token"
    monkeypatch.setenv("GH_TOKEN", "personal-token")
    monkeypatch.setenv("GH_DEBUG", "api")
    run = Mock(return_value=CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr("sadoc.gh.subprocess.run", run)
    Gh(app=app).api("installation/repositories")
    assert run.call_args.kwargs["env"]["GH_TOKEN"] == "app-token"
    assert "GH_DEBUG" not in run.call_args.kwargs["env"]
    assert os.environ["GH_TOKEN"] == "personal-token"


def test_app_push_auth_only_reaches_the_git_network_command(monkeypatch, tmp_path):
    import base64
    from subprocess import CompletedProcess
    from unittest.mock import Mock

    from sadoc.gitops import Git

    run = Mock(return_value=CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("sadoc.gitops.subprocess.run", run)
    git = Git(tmp_path, github_token=lambda: "app-token")
    git.status()
    assert "env" not in run.call_args.kwargs
    git.push("topic")
    env = run.call_args.kwargs["env"]
    assert env["GIT_CONFIG_KEY_2"] == "http.https://github.com/.extraHeader"
    assert env["GIT_CONFIG_KEY_3"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_3"] == "/dev/null"
    authorization = env["GIT_CONFIG_VALUE_2"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(authorization) == b"x-access-token:app-token"


def test_protected_key_is_required_for_app_with_containers(monkeypatch):
    import pytest

    from sadoc.github_app import GitHubApp, GitHubAppError

    monkeypatch.delenv("SADOC_GITHUB_APP_KEY_FD", raising=False)
    with pytest.raises(GitHubAppError, match="root-owned"):
        GitHubApp(5901719, "tomusdrw", require_protected_key=True)


def test_protected_descriptor_stays_in_orchestrator(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    from sadoc.github_app import KEY_FD_ENV, GitHubApp

    key = tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-out", str(key)],
        capture_output=True,
        check=True,
    )
    key.chmod(0o600)
    with key.open("rb") as source:
        fd = source.fileno()
        real_fstat = os.fstat

        def fstat(candidate):
            info = real_fstat(candidate)
            if candidate != fd:
                return info
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode, st_size=info.st_size)

        os.set_inheritable(fd, True)
        monkeypatch.setenv(KEY_FD_ENV, str(fd))
        with monkeypatch.context() as patch:
            patch.setattr("sadoc.github_app.os.fstat", fstat)
            app = GitHubApp(5901719, "tomusdrw", require_protected_key=True)
        assert not os.get_inheritable(fd)
        assert source.tell() == 0
        assert len(app._jwt().split(".")) == 3
        assert len(app._jwt().split(".")) == 3
        assert source.tell() == 0
        subprocess.run(
            [sys.executable, "-c", f"import os; assert not os.path.exists('/proc/self/fd/{fd}')"],
            close_fds=False,
            check=True,
        )


def test_git_uses_separate_metadata_from_the_checkout_copy(tmp_path):
    import subprocess

    from sadoc.gitops import Git

    source = tmp_path / "source"
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    git = Git(tmp_path / "checkout", git_dir=tmp_path / "private")
    git.clone(source.as_uri(), "main")
    original = git.commit_sha()
    metadata = git.path / ".git"
    assert metadata.is_dir()
    visible = subprocess.check_output(["git", "-C", str(git.path), "rev-parse", "HEAD"], text=True)
    assert visible.strip() == original
    (metadata / "HEAD").write_text("invalid\n")
    assert git.commit_sha() == original
    assert git.status() == ""


def test_app_commit_identity_uses_the_bot_user_id(monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from sadoc.gh import Gh
    from sadoc.github_app import GitHubApp

    app = GitHubApp(5901719, "tomusdrw")
    monkeypatch.setattr(app, "_jwt", lambda: "jwt")
    monkeypatch.setattr(
        app,
        "_request",
        Mock(
            side_effect=[
                [{"id": 11, "app_slug": "sadoc-test", "account": {"login": "tomusdrw"}}],
                {"token": "token", "expires_at": datetime(2099, 1, 1, tzinfo=UTC).isoformat()},
            ]
        ),
    )
    gh = Gh(app=app)
    api = Mock(return_value={"login": "sadoc-test[bot]", "id": 12345})
    monkeypatch.setattr(gh, "api", api)
    expected = ("sadoc-test[bot]", "12345+sadoc-test[bot]@users.noreply.github.com")
    assert gh.app_commit_identity == gh.app_commit_identity == expected
    api.assert_called_once_with("users/sadoc-test%5Bbot%5D")


def test_commit_identity_overrides_local_config_and_environment(tmp_path, monkeypatch):
    from sadoc.gitops import Git

    for variable in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(variable, "Personal")
    for variable in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(variable, "personal@example.com")
    name = "sadoc-test[bot]"
    email = "12345+sadoc-test[bot]@users.noreply.github.com"
    git = Git(tmp_path, commit_identity=lambda: (name, email))
    git._run("init")
    git._run("config", "user.name", "Local User")
    git._run("config", "user.email", "local@example.com")
    git._run("config", "commit.gpgsign", "true")
    (tmp_path / "dependency.txt").write_text("updated\n")

    git.commit("Update dependency")

    assert git._run("log", "-1", "--format=%an%n%ae%n%cn%n%ce").splitlines() == [
        name,
        email,
        name,
        email,
    ]
    assert git._run("config", "user.email").strip() == "local@example.com"
