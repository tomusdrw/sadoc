"""Release and artifact lookups for coordinated toolchain updates."""

from __future__ import annotations

import json
import re
import tomllib
from datetime import date
from urllib.error import URLError
from urllib.parse import quote

from . import registry

_NODE_INDEX = "https://nodejs.org/dist/index.json"
_NODE_SCHEDULE = "https://raw.githubusercontent.com/nodejs/Release/main/schedule.json"
_TAG_REPOS = {
    "bun": "oven-sh/bun",
    "composer": "composer/composer",
    "elixir": "elixir-lang/elixir",
    "erlang": "erlang/otp",
    "maven": "apache/maven",
    "php": "php/php-src",
    "python": "python/cpython",
    "ruby": "ruby/ruby",
    "rust": "rust-lang/rust",
    "swift": "swiftlang/swift",
}
_NPM_TOOLS = {"npm": "npm", "yarn": "@yarnpkg/cli-dist", "pnpm": "pnpm"}
_STABLE_VERSION = re.compile(r"\d+(?:\.\d+){0,2}")


def _json(url: str):
    return json.loads(registry._fetch(url))


def _versions(tool: str, line: str) -> list[str]:
    if tool == "node":
        schedule = _json(_NODE_SCHEDULE)
        return [
            release["version"].lstrip("v")
            for release in _json(_NODE_INDEX)
            if release["lts"]
            and schedule.get(release["version"].split(".")[0], {}).get("end", "")
            >= date.today().isoformat()
        ]
    if tool == "go":
        return [
            release["version"].removeprefix("go")
            for release in _json("https://go.dev/dl/?mode=json&include=all")
            if release["stable"]
        ]
    if tool == "rust" and not line:
        manifest = tomllib.loads(
            registry._fetch("https://static.rust-lang.org/dist/channel-rust-stable.toml").decode()
        )
        return [manifest["pkg"]["rust"]["version"].split()[0]]
    if tool == "java":
        feed = "https://api.adoptium.net/v3/info"
        releases = _json(f"{feed}/available_releases")
        feature = int(line.split(".")[0]) if line else releases["most_recent_lts"]
        versions = _json(f"{feed}/release_versions?version=%5B{feature},{feature + 1}%29")
        return [release["semver"].split("+")[0] for release in versions["versions"]]
    if tool == "gradle":
        feed = _json("https://services.gradle.org/versions/all")
        return [release["version"] for release in feed if not release.get("snapshot")]
    if tool == "dart":
        version = _json(
            "https://storage.googleapis.com/dart-archive/channels/stable/release/latest/VERSION"
        )
        return [version["version"]]
    if tool in _NPM_TOOLS:
        name = quote(_NPM_TOOLS[tool], safe="")
        feed = _json(f"https://registry.npmjs.org/{name}")
        return list(feed["versions"]) if line else [feed["dist-tags"]["latest"]]
    if tool in _TAG_REPOS:
        tags = _json(f"https://api.github.com/repos/{_TAG_REPOS[tool]}/tags?per_page=100")
        prefixes = {
            "bun": "bun-v",
            "erlang": "OTP-",
            "maven": "maven-",
            "php": "php-",
            "swift": "swift-",
        }
        prefix = prefixes.get(tool, "v")
        return [
            tag["name"].removeprefix(prefix).removesuffix("-RELEASE").replace("_", ".")
            for tag in tags
        ]
    return []


def latest(tool: str, line: str = "") -> str | None:
    cache_key = f"{tool}:{line}"
    cached = registry._cache_get("toolchain", cache_key)
    if cached:
        return cached
    try:
        versions = [
            version
            for version in _versions(tool, line)
            if _STABLE_VERSION.fullmatch(version)
            and (not line or version == line or version.startswith(f"{line}."))
        ]
    except (URLError, OSError, ValueError, KeyError, TypeError):
        return None
    target = max(versions, default=None, key=lambda version: tuple(map(int, version.split("."))))
    if target:
        registry._cache_put("toolchain", cache_key, target)
    return target


def image_exists(image: str, tag: str) -> bool:
    cache_key = f"{image}:{tag}"
    if registry._cache_get("toolchain-image", cache_key):
        return True
    try:
        data = _json(
            f"https://hub.docker.com/v2/repositories/library/{image}/tags/{quote(tag, safe='')}"
        )
    except (URLError, OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("name") != tag or not data.get("images"):
        return False
    registry._cache_put("toolchain-image", cache_key, True)
    return True
