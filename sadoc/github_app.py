"""GitHub App installation credentials, used only by the orchestrator."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Lock

KEY_DIR = Path.home() / ".sadoc" / "github-app"
PRIVATE_KEY = KEY_DIR / "private-key.pem"
KEY_FD_ENV = "SADOC_GITHUB_APP_KEY_FD"
_MAX_KEY_BYTES = 64 * 1024


class GitHubAppError(RuntimeError):
    pass


def _base64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def protect_key_descriptor() -> int | None:
    value = os.environ.get(KEY_FD_ENV)
    if value is None:
        return None
    try:
        fd = int(value)
        if fd < 3:
            raise ValueError
        info = os.fstat(fd)
        mode = fcntl.fcntl(fd, fcntl.F_GETFL)
        if (
            info.st_uid != 0
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or not 0 < info.st_size <= _MAX_KEY_BYTES
            or mode & os.O_ACCMODE != os.O_RDONLY
        ):
            raise ValueError
        # The systemd descriptor is inheritable on entry. No git, gh or pi
        # process should inherit it from the Python orchestrator.
        os.set_inheritable(fd, False)
    except (OSError, ValueError, OverflowError) as exc:
        raise GitHubAppError(
            "expected a read-only descriptor for a root-owned GitHub App key"
        ) from exc
    return fd


class GitHubApp:
    def __init__(self, app_id: int, owner: str, *, require_protected_key: bool = False):
        self._key_fd = protect_key_descriptor()
        if require_protected_key and self._key_fd is None:
            raise GitHubAppError(
                "GitHub App with containers requires a root-owned key opened by systemd"
            )
        self.app_id = app_id
        self.owner = owner
        self._installation: int | None = None
        self._app_slug: str | None = None
        self._token = ""
        self._expires = 0.0
        self._lock = Lock()

    def _sign_with_protected_key(self, signing_input: bytes) -> bytes:
        # Pass PEM on stdin. Only the unsigned JWT is placed in the anonymous
        # file, so the root-owned descriptor never leaves this process.
        assert self._key_fd is not None
        key = os.pread(self._key_fd, _MAX_KEY_BYTES + 1, 0)
        if not 0 < len(key) <= _MAX_KEY_BYTES:
            raise GitHubAppError("invalid GitHub App key size")
        with tempfile.TemporaryFile() as message:
            message.write(signing_input)
            message.flush()
            return subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-sign",
                    "/dev/stdin",
                    f"/proc/self/fd/{message.fileno()}",
                ],
                input=key,
                pass_fds=(message.fileno(),),
                capture_output=True,
                check=True,
                timeout=15,
            ).stdout

    def _jwt(self) -> str:
        now = int(time.time())
        payload = json.dumps({"iss": str(self.app_id), "iat": now - 60, "exp": now + 540})
        signing_input = b".".join(
            (_base64url(b'{"alg":"RS256","typ":"JWT"}'), _base64url(payload.encode()))
        )
        try:
            if self._key_fd is not None:
                signature = self._sign_with_protected_key(signing_input)
            else:
                if KEY_DIR.is_symlink() or PRIVATE_KEY.is_symlink():
                    raise GitHubAppError(
                        "the GitHub App credential directory and key must not be symlinks"
                    )
                if PRIVATE_KEY.stat().st_mode & 0o077:
                    raise GitHubAppError("the GitHub App key must be accessible only to its owner")
                signature = subprocess.run(
                    ["openssl", "dgst", "-sha256", "-sign", str(PRIVATE_KEY)],
                    input=signing_input,
                    capture_output=True,
                    check=True,
                    timeout=15,
                ).stdout
        except (OSError, subprocess.SubprocessError):
            source = "the protected descriptor" if self._key_fd is not None else str(PRIVATE_KEY)
            raise GitHubAppError(f"could not sign with the GitHub App key from {source}") from None
        return (signing_input + b"." + _base64url(signature)).decode("ascii")

    def _request(self, path: str, jwt: str, method: str = "GET") -> dict:
        request = urllib.request.Request(
            "https://api.github.com" + path,
            method=method,
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise GitHubAppError(f"GitHub App {method} {path}: HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise GitHubAppError(f"GitHub App {method} {path}: request failed") from exc

    def bot_login(self) -> str:
        self.token()
        if not self._app_slug:
            raise GitHubAppError("GitHub did not return the app slug for its installation")
        return f"{self._app_slug}[bot]"

    def token(self) -> str:
        with self._lock:
            if self._expires > time.time() + 120:
                return self._token
            jwt = self._jwt()
            if self._installation is None:
                installations = self._request("/app/installations?per_page=100", jwt)
                matches = [
                    installation
                    for installation in installations
                    if installation["account"]["login"].casefold() == self.owner.casefold()
                ]
                if len(matches) != 1:
                    raise GitHubAppError(f"expected one GitHub App installation for {self.owner}")
                self._installation = matches[0]["id"]
                self._app_slug = matches[0].get("app_slug")
            issued = self._request(
                f"/app/installations/{self._installation}/access_tokens", jwt, "POST"
            )
            self._token = issued["token"]
            self._expires = datetime.fromisoformat(issued["expires_at"]).timestamp()
            return self._token
