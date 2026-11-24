"""GitHub App installation credentials, used only by the orchestrator."""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Lock

KEY_DIR = Path.home() / ".sadoc" / "github-app"
PRIVATE_KEY = KEY_DIR / "private-key.pem"


class GitHubAppError(RuntimeError):
    pass


def _base64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


class GitHubApp:
    def __init__(self, app_id: int, owner: str):
        self.app_id = app_id
        self.owner = owner
        self._installation: int | None = None
        self._token = ""
        self._expires = 0.0
        self._lock = Lock()

    def _jwt(self) -> str:
        now = int(time.time())
        payload = json.dumps({"iss": str(self.app_id), "iat": now - 60, "exp": now + 540})
        signing_input = b".".join(
            (_base64url(b'{"alg":"RS256","typ":"JWT"}'), _base64url(payload.encode()))
        )
        try:
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
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitHubAppError(
                f"could not sign with the GitHub App key at {PRIVATE_KEY}"
            ) from exc
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
            issued = self._request(
                f"/app/installations/{self._installation}/access_tokens", jwt, "POST"
            )
            self._token = issued["token"]
            self._expires = datetime.fromisoformat(issued["expires_at"]).timestamp()
            return self._token
