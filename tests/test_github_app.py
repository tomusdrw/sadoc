from sadoc import config


def test_github_app_requires_app_id(tmp_path):
    settings = tmp_path / "config.toml"
    settings.write_text('owner = "tomusdrw"\n[github]\nmode = "app"\napp_id = 5901719\n')
    assert config.load(settings).github_app_id == 5901719
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
    authorization = env["GIT_CONFIG_VALUE_2"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(authorization) == b"x-access-token:app-token"


def test_app_mode_rejects_host_container_helper(tmp_path):
    import pytest

    settings = tmp_path / "config.toml"
    settings.write_text(
        'owner = "tomusdrw"\n[github]\nmode = "app"\napp_id = 5901719\n'
        "[sandbox]\nallow_containers = true\n"
    )
    with pytest.raises(config.ConfigError, match="allow_containers=false"):
        config.load(settings)
