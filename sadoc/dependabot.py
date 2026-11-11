"""Taking over a repository from Dependabot.

Two separate things have to be switched off:

* **Version updates** - no REST API exists for these, they are configured by
  `.github/dependabot.yml`. The documented way to turn them off without losing
  the security updates is `open-pull-requests-limit: 0` on every package ecosystem.
* **Security updates** - `DELETE /repos/{owner}/{repo}/automated-security-fixes`.
  Public repositories always have this on, so the call is allowed to fail.

The file is edited line by line rather than through a YAML library, so its
formatting, comments and key order survive untouched.
"""

from __future__ import annotations

import base64
import re

DEPENDABOT_PATHS = (".github/dependabot.yml", ".github/dependabot.yaml")

_ECOSYSTEM_RE = re.compile(r"^(\s*)-(\s*)package-ecosystem\s*:")
_LIMIT_RE = re.compile(r"^\s*open-pull-requests-limit\s*:")


def disable_version_updates(text: str) -> tuple[str, int]:
    """Set `open-pull-requests-limit: 0` on every package ecosystem.

    Returns the new text and the number of ecosystems that were changed.
    """
    lines = text.splitlines()
    out: list[str] = []
    changed = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        out.append(line)
        match = _ECOSYSTEM_RE.match(line)
        if not match:
            index += 1
            continue
        # The keys of this ecosystem entry are indented deeper than the "-".
        entry_indent = len(match.group(1))
        index += 1
        block: list[str] = []
        while index < len(lines):
            nxt = lines[index]
            stripped = nxt.strip()
            indent = len(nxt) - len(nxt.lstrip())
            if stripped and indent <= entry_indent and not stripped.startswith("#"):
                break
            block.append(nxt)
            index += 1
        limit_at = next((i for i, b in enumerate(block) if _LIMIT_RE.match(b)), None)
        if limit_at is None:
            block.append(f"{' ' * (entry_indent + 2)}open-pull-requests-limit: 0")
            changed += 1
        elif not block[limit_at].split(":", 1)[1].strip().startswith("0"):
            prefix = block[limit_at].split(":", 1)[0]
            comment = ""
            if "#" in block[limit_at]:
                comment = "  #" + block[limit_at].split("#", 1)[1]
            block[limit_at] = f"{prefix}: 0{comment}"
            changed += 1
        out.extend(block)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), changed


def _file_info(gh, owner: str, repo: str, path: str) -> dict | None:
    data = gh.api(f"repos/{owner}/{repo}/contents/{path}")
    return data if isinstance(data, dict) and "content" in data else None


def disable_version_updates_in_repo(gh, owner: str, repo: str, branch: str) -> dict:
    """Edit the Dependabot config so it opens no version-update pull requests.

    The change goes onto its own branch and is offered as a pull request:
    several repositories have a ruleset that forbids direct changes to the
    default branch, and a pull request is how a human would do it too.
    """
    result = {"status": "ok", "changed": 0, "detail": ""}
    path = None
    info = None
    for candidate in DEPENDABOT_PATHS:
        found = _file_info(gh, owner, repo, candidate)
        if found:
            path, info = candidate, found
            break
    if not path or not info:
        result["status"] = "no_config"
        return result

    text = base64.b64decode(info["content"]).decode("utf-8", "replace")
    updated, changed = disable_version_updates(text)
    if not changed:
        result["detail"] = "already disabled"
        return result

    branch_name = "chore/disable-dependabot-version-updates"
    message = (
        "chore: stop opening Dependabot version update pull requests\n\n"
        "The dependency updates for this repository are maintained by Sadoc, which\n"
        "groups them into a single pull request, fixes what the update breaks and\n"
        "drives CI to green. Dependabot security updates are unaffected.\n"
    )
    content = base64.b64encode(updated.encode()).decode()

    # The branch starts at the head of the default branch.
    head = ""
    try:
        ref = gh.api(f"repos/{owner}/{repo}/git/ref/heads/{branch}")
        head = (ref or {}).get("object", {}).get("sha", "")
    except Exception:  # noqa: BLE001
        head = ""
    if not head:
        try:
            head = (gh.api(f"repos/{owner}/{repo}/commits/{branch}") or {}).get("sha", "")
        except Exception:  # noqa: BLE001
            head = ""
    if not head:
        result["status"] = "failed"
        result["detail"] = "could not find the head of the default branch"
        return result

    try:
        gh.api(
            f"repos/{owner}/{repo}/git/refs",
            method="POST",
            ref=f"refs/heads/{branch_name}",
            sha=head,
        )
    except Exception:
        # The reference may already exist; carry on and try the file anyway.
        pass

    try:
        gh.api(
            f"repos/{owner}/{repo}/contents/{path}",
            method="PUT",
            message=message,
            content=content,
            sha=info["sha"],
            branch=branch_name,
        )
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["detail"] = str(exc)[-300:]
        return result

    # Offer it as a pull request.
    try:
        pr = gh.open_pr(
            owner,
            repo,
            "chore: stop opening Dependabot version update pull requests",
            message.split("\n\n", 1)[1],
            branch_name,
            branch,
        )
        result["detail"] = pr.get("url", "")
        result["pr"] = pr.get("url", "")
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"branch {branch_name} (no PR: {str(exc)[:120]})"

    result["changed"] = changed
    return result


def disable_security_updates(gh, owner: str, repo: str) -> dict:
    """Turn off Dependabot security updates, where the repository allows it."""
    code, output = gh.run_status(
        "api", f"repos/{owner}/{repo}/automated-security-fixes", "--method", "DELETE"
    )
    if code == 0:
        return {"status": "ok", "detail": "disabled"}
    if "404" in output:
        return {"status": "always_on", "detail": "public repositories always have it on"}
    if "Vulnerability alerts must be enabled" in output:
        return {"status": "already_off", "detail": "vulnerability alerts were never on"}
    return {"status": "failed", "detail": output[:200]}


def take_over(gh, owner: str, repo: str, branch: str) -> dict:
    """Everything needed to stop Dependabot competing with this tool."""
    return {
        "version_updates": disable_version_updates_in_repo(gh, owner, repo, branch),
        "security_updates": disable_security_updates(gh, owner, repo),
    }
