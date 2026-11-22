"""Discover literal toolchain pins and retain their exact edit locations."""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from . import semver, toolchain_releases

_VERSION = re.compile(r"v?(\d+(?:\.\d+){0,2})(?:\.x)?")
_EXACT = r"v?\d+(?:\.\d+){0,2}(?:\.x)?"
_VERSION_FILES = {
    ".node-version": "node",
    ".nvmrc": "node",
    ".python-version": "python",
    ".go-version": "go",
    ".ruby-version": "ruby",
    ".php-version": "php",
    ".java-version": "java",
    ".swift-version": "swift",
    ".dart-version": "dart",
    ".elixir-version": "elixir",
    ".erlang-version": "erlang",
    "rust-toolchain": "rust",
}
_PACKAGE_MANAGERS = ("npm", "yarn", "pnpm", "bun")
_WORKFLOW_KEYS = {
    "node-version": "node",
    "python-version": "python",
    "go-version": "go",
    "java-version": "java",
    "ruby-version": "ruby",
    "php-version": "php",
    "swift-version": "swift",
    "dart-version": "dart",
    "elixir-version": "elixir",
    "otp-version": "erlang",
    "bun-version": "bun",
}
_TOOL_ALIASES = {"nodejs": "node", "golang": "go", "otp": "erlang", "openjdk": "java"}
_IMAGES = {
    "node": "node",
    "python": "python",
    "golang": "go",
    "rust": "rust",
    "ruby": "ruby",
    "php": "php",
    "composer": "composer",
    "swift": "swift",
    "dart": "dart",
    "elixir": "elixir",
    "erlang": "erlang",
    "eclipse-temurin": "java",
    "gradle": "gradle",
    "maven": "maven",
}
_PROJECT_FILES = {"package.json", "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile", "pom.xml"}
_PIN_FILES = _PROJECT_FILES | {
    *_VERSION_FILES,
    ".tool-versions",
    ".yarnrc.yml",
    ".yarnrc.yaml",
    "mise.toml",
    ".mise.toml",
    "rust-toolchain.toml",
    "go.work",
    "devcontainer.json",
    "gradle-wrapper.properties",
    "maven-wrapper.properties",
    ".sadoc-toolchains.toml",
}


@dataclass(frozen=True)
class Pin:
    tool: str
    path: str
    version: str
    start: int
    end: int
    role: str = "install"
    scope: str = "."
    image: str = ""
    variant: str = ""
    constraint: str = ""


def is_build_input(path: str) -> bool:
    base = PurePosixPath(path).name
    return (
        base in _PIN_FILES
        or base.startswith(("Dockerfile", "compose.", "docker-compose."))
        or path.startswith(".github/workflows/")
        and base.endswith((".yml", ".yaml"))
        or base.endswith((".sh", ".bash"))
    )


def _pin(tool: str, path: str, match: re.Match[str], group: str = "version", **fields) -> Pin:
    return Pin(tool, path, match[group], *match.span(group), **fields)


def _version_role(version: str) -> str:
    if _VERSION.fullmatch(version):
        return "install"
    if version in {"stable", "latest", "lts", "lts/*", "node"} or version.startswith("lts/"):
        return "floating"
    if any(c in version for c in "<>=~^*") and "$" not in version:
        return "compatibility"
    return "unresolved"


def _package_pins(path: str, text: str) -> list[Pin]:
    # Check the actual top-level field before editing exact bytes in its original formatting.
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    pin = data.get("packageManager")
    out = []
    if isinstance(pin, str):
        prefix, _, version = pin.partition("@")
        if prefix in _PACKAGE_MANAGERS:
            values = list(re.finditer(r'"packageManager"\s*:\s*"(?P<version>[^"\\]+)"', text))
            if len(values) == 1 and values[0]["version"] == pin:
                match = values[0]
                version_start = match.start("version") + len(prefix) + 1
                raw_version, _, integrity = version.partition("+")
                end = match.end("version")
                out.append(
                    Pin(
                        prefix,
                        path,
                        version,
                        version_start,
                        end,
                        _version_role(raw_version) if not integrity else "unresolved",
                    )
                )
            else:
                out.append(Pin(prefix, path, version, 0, 0, "unresolved"))
    engines = data.get("engines")
    if isinstance(engines, dict):
        for tool in ("node", *_PACKAGE_MANAGERS):
            version = engines.get(tool)
            if isinstance(version, str):
                out.append(Pin(tool, path, version, 0, 0, "compatibility", constraint=version))
    return out


def _image_pin(path: str, match: re.Match[str]) -> Pin | None:
    reference = match["reference"]
    image, separator, tag = reference.rpartition(":")
    if not separator:
        image, tag = reference, ""
    digest = "@" in reference
    if digest:
        image, _, tag = reference.partition("@")
        image = image.partition(":")[0]
    name = image.removeprefix("docker.io/").removeprefix("library/")
    tool = _IMAGES.get(name)
    if not tool:
        return None
    version = re.fullmatch(rf"(?P<version>{_EXACT})(?P<variant>-[\w.-]+)?", tag)
    begin = match.start("reference") + len(image) + 1
    if version:
        return Pin(
            tool,
            path,
            version["version"],
            begin,
            begin + len(version["version"]),
            image=name,
            variant=version["variant"] or "",
        )
    role = (
        "floating" if not tag or tag.split("-")[0] in {"stable", "latest", "lts"} else "unresolved"
    )
    return Pin(tool, path, tag, begin, match.end("reference"), role, image=name)


def _matrix_items(tool: str, path: str, value: str, offset: int) -> list[Pin]:
    items = []
    for match in re.finditer(
        rf"""(?:^|[,\[])[ \t]*["']?(?P<version>{_EXACT})["']?[ \t]*(?=[,\]])""", value
    ):
        start, end = match.span("version")
        items.append(Pin(tool, path, match["version"], offset + start, offset + end, "matrix"))
    if len(items) != value.count(",") + 1:
        return [Pin(tool, path, value, offset, offset + len(value), "unresolved")]
    return items


def _workflow_pins(path: str, text: str) -> list[Pin]:
    out = []
    for key, tool in _WORKFLOW_KEYS.items():
        for match in re.finditer(rf"(?m)^[ \t]*{key}:[ \t]*(?P<version>[^\n#]+)", text):
            raw = match["version"]
            value = raw.strip().strip("\"'")
            position = match.start("version") + raw.index(value)
            selector = re.fullmatch(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}", value)
            if selector:
                axis = re.escape(selector[1])
                axes = list(re.finditer(rf"(?m)^[ \t]*{axis}:[ \t]*(?P<axis>\[[^\n#]*\])", text))
                if len(axes) == 1:
                    out.extend(_matrix_items(tool, path, axes[0]["axis"], axes[0].start("axis")))
                    continue
            if value.startswith("[") and value.endswith("]"):
                out.extend(_matrix_items(tool, path, value, position))
                continue
            if tool == "go" and value.startswith("go"):
                value, position = value[2:], position + 2
            out.append(
                Pin(tool, path, value, position, position + len(value), _version_role(value))
            )
    return list(dict.fromkeys(out))


def _literal_pins(path: str, text: str) -> list[Pin]:
    base = PurePosixPath(path).name
    out = _package_pins(path, text) if base == "package.json" else []
    if base in {".yarnrc.yml", ".yarnrc.yaml"}:
        for match in re.finditer(r"(?m)^yarnPath:[ \t]*(?P<version>[^\n#]+)", text):
            out.append(_pin("yarn", path, match, role="unresolved"))
    if base in _VERSION_FILES:
        match = re.match(r"\s*(?P<version>[^\s#]+)", text)
        if match:
            out.append(
                _pin(_VERSION_FILES[base], path, match, role=_version_role(match["version"]))
            )
    if path.startswith(".github/workflows/"):
        out.extend(_workflow_pins(path, text))
    if base.startswith("Dockerfile"):
        for match in re.finditer(r"(?im)^\s*FROM\s+(?:--platform=\S+\s+)?(?P<reference>\S+)", text):
            pin = _image_pin(path, match)
            if pin:
                out.append(pin)
    elif base.startswith(("compose.", "docker-compose.")) or base == "devcontainer.json":
        for match in re.finditer(r'(?m)^\s*"?image"?\s*:\s*["\']?(?P<reference>[^\s"\',]+)', text):
            pin = _image_pin(path, match)
            if pin:
                out.append(pin)
    if base in {".tool-versions", ".mise.toml", "mise.toml"}:
        pattern = (
            r"(?m)^[ \t]*(?P<tool>[\w-]+)[ \t]+(?P<version>[^\s#]+)"
            if base == ".tool-versions"
            else r'(?m)^[ \t]*(?P<tool>[\w-]+)[ \t]*=[ \t]*"(?P<version>[^"]+)"'
        )
        if base != ".tool-versions":
            tools_section = re.search(r"(?ms)^\[tools\][ \t]*\n(?P<tools>.*?)(?=^\[|\Z)", text)
            offset = tools_section.start("tools") if tools_section else 0
            source = tools_section["tools"] if tools_section else ""
        else:
            offset, source = 0, text
        for match in re.finditer(pattern, source):
            tool = _TOOL_ALIASES.get(match["tool"], match["tool"])
            version = match["version"]
            start, end = match.span("version")
            out.append(
                Pin(tool, path, version, offset + start, offset + end, _version_role(version))
            )
    if base in {"go.mod", "go.work"}:
        for match in re.finditer(r"(?m)^[ \t]*(toolchain[ \t]+go|go[ \t]+)(?P<version>\S+)", text):
            role = (
                _version_role(match["version"])
                if match[1].startswith("toolchain")
                else "compatibility"
            )
            out.append(
                _pin(
                    "go",
                    path,
                    match,
                    role=role,
                    constraint=f">={match['version']}" if role == "compatibility" else "",
                )
            )
    if base == "rust-toolchain.toml":
        match = re.search(r'(?m)^[ \t]*channel[ \t]*=[ \t]*"(?P<version>[^"]+)"', text)
        if match:
            out.append(_pin("rust", path, match, role=_version_role(match["version"])))
    if base in {"Cargo.toml", "pyproject.toml", "Gemfile"}:
        fields = {
            "Cargo.toml": ("rust", r"rust-version[ \t]*="),
            "pyproject.toml": ("python", r"requires-python[ \t]*="),
            "Gemfile": ("ruby", r"ruby[ \t]+"),
        }
        tool, field = fields[base]
        for match in re.finditer(rf'(?m)^[ \t]*{field}[ \t]*["\'](?P<version>[^"\']+)', text):
            role = _version_role(match["version"]) if base == "Gemfile" else "compatibility"
            constraint = ""
            if role == "compatibility":
                constraint = f">={match['version']}" if tool == "rust" else match["version"]
            out.append(_pin(tool, path, match, role=role, constraint=constraint))
    if base in {"gradle-wrapper.properties", "maven-wrapper.properties"}:
        tool = "gradle" if base.startswith("gradle") else "maven"
        for match in re.finditer(
            rf"(?m)^distributionUrl=.*?/{tool}-(?P<version>{_EXACT})(?=-)", text
        ):
            out.append(
                _pin(tool, path, match, role="unresolved" if "Sha256Sum" in text else "install")
            )
    if base.startswith("Dockerfile") or base.endswith((".sh", ".bash", ".yml", ".yaml")):
        for match in re.finditer(
            r"(?m)^[ \t]*(?:RUN[ \t]+)?[^\n#]*?\b(?:"
            r"(?P<yarn>yarn[ \t]+set[ \t]+version[ \t]+)|"
            r"corepack[ \t]+(?:prepare|install[ \t]+--global)[ \t]+(?P<manager>yarn|pnpm|npm)@)"
            r"(?P<version>[^\s\"';&|]+)",
            text,
        ):
            tool = "yarn" if match["yarn"] else match["manager"]
            out.append(_pin(tool, path, match, role=_version_role(match["version"])))
    return out


def _project_roots(files: dict[str, str]) -> set[PurePosixPath]:
    roots = {
        PurePosixPath(path).parent for path in files if PurePosixPath(path).name in _PROJECT_FILES
    }
    for path, text in files.items():
        if PurePosixPath(path).name != "package.json":
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        workspaces = data.get("workspaces", [])
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages", [])
        if not isinstance(workspaces, list):
            continue
        root = PurePosixPath(path).parent
        for member in roots.copy():
            if (
                member != root
                and member.is_relative_to(root)
                and any(
                    isinstance(pattern, str)
                    and fnmatch.fnmatchcase(member.relative_to(root).as_posix(), pattern)
                    for pattern in workspaces
                )
            ):
                roots.remove(member)
    return roots


def inventory(files: dict[str, str]) -> list[Pin]:
    roots = _project_roots(files)
    pins = []
    for path, text in sorted(files.items()):
        parent = PurePosixPath(path).parent
        enclosing = [root for root in roots if parent.is_relative_to(root)]
        scope = max(enclosing, key=lambda root: len(root.parts)).as_posix() if enclosing else "."
        pins.extend(replace(pin, scope=scope) for pin in _literal_pins(path, text))
    scoped_tools: dict[str, set[str]] = {}
    for pin in pins:
        if pin.scope != "." and pin.role == "install":
            scoped_tools.setdefault(pin.tool, set()).add(pin.scope)
    root_tools = {
        pin.tool
        for pin in pins
        if pin.scope == "." and not pin.path.startswith(".github/workflows/")
    }
    resolved = []
    for pin in pins:
        scopes = scoped_tools.get(pin.tool, set())
        if pin.scope == "." and pin.tool not in root_tools and scopes:
            if len(scopes) == 1:
                resolved.append(replace(pin, scope=next(iter(scopes))))
            else:
                resolved.extend(
                    replace(pin, scope=scope, role="unresolved") for scope in sorted(scopes)
                )
        else:
            resolved.append(pin)
    lines: dict[tuple[str, str], set[tuple[int, ...]]] = {}
    matrix: dict[tuple[str, str], list[tuple[int, ...]]] = {}
    for pin in resolved:
        key = (pin.scope, pin.tool)
        if pin.role == "install":
            lines.setdefault(key, set()).add(_release_line(pin))
        elif pin.role == "matrix":
            matrix.setdefault(key, []).append(_release_line(pin))
    for key, options in matrix.items():
        lines.setdefault(key, {max(options)})
    return [
        replace(
            pin,
            role=(
                "install" if _release_line(pin) in lines[(pin.scope, pin.tool)] else "compatibility"
            ),
        )
        if pin.role == "matrix"
        else pin
        for pin in resolved
    ]


def _release_line(pin: Pin) -> tuple[int, ...]:
    version = version_key(pin.version)
    precision = 1 if pin.tool in {"node", "java", "erlang", "npm", "yarn", "pnpm", "bun"} else 2
    return version[:precision]


@dataclass(frozen=True)
class Change:
    pin: Pin
    replacement: str


@dataclass(frozen=True)
class Proposal:
    scope: str
    tool: str
    target: str
    pins: tuple[Pin, ...]
    changes: tuple[Change, ...]
    coordination: tuple[str, ...] = ()


def version_key(version: str) -> tuple[int, int, int]:
    parts = [int(n) for n in version.removeprefix("v").removesuffix(".x").split(".")]
    return tuple((parts + [0, 0])[:3])


def _target_for_pin(pin: Pin, target: str) -> str:
    version = pin.version.removeprefix("v").removesuffix(".x")
    target = ".".join(target.split(".")[: len(version.split("."))])
    prefix = "v" if pin.version.startswith("v") else ""
    suffix = ".x" if pin.version.endswith(".x") else ""
    return prefix + target + suffix


def _compatible(constraint: str, target: str) -> bool:
    def allows(token: str) -> bool:
        token = token.replace(" ", "")
        if token.startswith(("^", "~")) and version_key(target) < version_key(
            re.sub(r"[xX*]", "0", token[1:])
        ):
            return False
        if re.fullmatch(r"\d+(?:\.\d+){0,2}(?:\.[xX*])?", token):
            prefix = token.removesuffix(".x").removesuffix(".X").removesuffix(".*")
            return target == prefix or target.startswith(prefix + ".")
        return semver.allows(token, target, "npm")

    for clause in constraint.split("||"):
        tokens = re.findall(r"(?:<=|>=|[<>=~^])?\s*(?:\d+(?:\.[\dxX*]+){0,2}|\*)", clause)
        remainder = re.sub(r"(?:<=|>=|[<>=~^])?\s*(?:\d+(?:\.[\dxX*]+){0,2}|\*)", "", clause)
        if (
            tokens
            and not remainder.replace(",", "").strip()
            and all(allows(token) for token in tokens)
        ):
            return True
    return False


def _config(files: dict[str, str]) -> dict:
    return tomllib.loads(files.get(".sadoc-toolchains.toml", ""))


def _map_pins(pins: list[Pin], files: dict[str, str], mappings: list[dict]) -> list[Pin]:
    for item in mappings:
        path, tool, pattern = item["path"], item["tool"], item["pattern"]
        matches = list(re.finditer(pattern, files[path], re.M))
        if len(matches) != 1 or not matches[0].groupdict().get("version"):
            raise ValueError(f"{path}: custom pin must identify one named version group")
        match = matches[0]
        start, end = match.span("version")
        version = match["version"]
        role = item.get("role", _version_role(version))
        if role not in {"compatibility", "install", "floating", "unresolved"}:
            raise ValueError(f"{path}: invalid custom pin role")
        if role == "install" and not _VERSION.fullmatch(version):
            raise ValueError(f"{path}: custom install pin must be numeric")
        scope = PurePosixPath(item.get("scope", "."))
        if scope.is_absolute() or ".." in scope.parts:
            raise ValueError(f"{path}: invalid scope")
        pins = [
            pin
            for pin in pins
            if not (pin.path == path and pin.tool == tool and pin.start == start)
        ]
        pins.append(Pin(tool, path, version, start, end, role, scope.as_posix()))
    return pins


def plan(
    files: dict[str, str],
    *,
    resolve=toolchain_releases.latest,
    artifact_exists=toolchain_releases.image_exists,
    lock_paths: tuple[str, ...] = (),
) -> tuple[list[Proposal], list[str]]:
    """Resolve an exact release for each group of pins that move together."""
    try:
        config = _config(files)
        pins = _map_pins(inventory(files), files, config.get("pin", []))
        coordinated = []
        for rule in config.get("coordinate", []):
            scope, tools = rule.get("scope", "."), rule["tools"]
            if (
                not isinstance(scope, str)
                or not isinstance(tools, list)
                or len(tools) < 2
                or not all(isinstance(tool, str) and tool in _VERSION_COMMANDS for tool in tools)
            ):
                raise ValueError("coordinate entries need a scope and at least two known tools")
            coordinated.append((scope, set(tools)))
        release_lines = config.get("release-line", {})
        if not isinstance(release_lines, dict):
            raise ValueError("release-line must be a table")
        if any(
            not isinstance(line, str) or not re.fullmatch(r"\d+(?:\.\d+)?", line)
            for line in release_lines.values()
        ):
            raise ValueError("release lines must use a numeric major or major.minor")
    except (ValueError, KeyError, TypeError, re.error) as exc:
        return [], [f".sadoc-toolchains.toml: {exc}"]
    groups: dict[tuple[str, str], list[Pin]] = {}
    for pin in pins:
        groups.setdefault((pin.scope, pin.tool), []).append(pin)
    proposals = []
    blocked = []
    failed: set[tuple[str, str]] = set()
    for (scope, tool), members in sorted(groups.items()):
        installs = [pin for pin in members if pin.role == "install"]
        if not installs:
            if any(pin.role == "unresolved" for pin in members):
                failed.add((scope, tool))
                blocked.append(f"{tool} in {scope}: unresolved references")
            continue
        unresolved = next((pin for pin in members if pin.role == "unresolved"), None)
        if unresolved:
            failed.add((scope, tool))
            blocked.append(f"{tool} in {scope}: unresolved pin in {unresolved.path}")
            continue
        line = release_lines.get(f"{scope}/{tool}", release_lines.get(tool, ""))
        target = resolve(tool, line)
        target_match = _VERSION.fullmatch(target or "")
        if not target_match:
            failed.add((scope, tool))
            blocked.append(f"{tool} in {scope}: no eligible upstream release")
            continue
        target = target_match[1]
        if line and target != line and not target.startswith(f"{line}."):
            failed.add((scope, tool))
            blocked.append(f"{tool} in {scope}: upstream release {target} is outside {line}")
            continue
        if any(version_key(pin.version) > version_key(target) for pin in installs):
            failed.add((scope, tool))
            blocked.append(f"{tool} in {scope}: selected release {target} would downgrade a pin")
            continue
        incompatible = next(
            (pin for pin in members if pin.constraint and not _compatible(pin.constraint, target)),
            None,
        )
        if incompatible:
            failed.add((scope, tool))
            blocked.append(
                f"{tool} in {scope}: {target} does not satisfy compatibility constraint "
                f"{incompatible.constraint} in {incompatible.path}"
            )
            continue
        changes = tuple(
            Change(pin, replacement)
            for pin in installs
            if (replacement := _target_for_pin(pin, target)) != pin.version
        )
        if not changes:
            continue
        missing = next(
            (
                (pin, tag)
                for pin in installs
                if pin.image
                and not artifact_exists(
                    pin.image, tag := _target_for_pin(pin, target) + pin.variant
                )
            ),
            None,
        )
        if missing:
            failed.add((scope, tool))
            pin, tag = missing
            blocked.append(
                f"{tool} in {scope}: {pin.path} references unavailable {pin.image}:{tag}"
            )
            continue
        coordination = tuple(
            json.dumps([declared_scope, *sorted(tools)])
            for declared_scope, tools in coordinated
            if declared_scope == scope and tool in tools
        )
        proposals.append(Proposal(scope, tool, target, tuple(installs), changes, coordination))
    while True:
        before = len(failed)
        for scope, tools in coordinated:
            keys = {(scope, tool) for tool in tools}
            if keys & failed or not keys.issubset(groups):
                failed.update(keys)
        blocked_paths = {
            pin.path
            for key in failed
            for pin in groups.get(key, [])
            if pin.role in {"install", "unresolved"}
        }
        for scope, tool in failed:
            blocked_paths.update(_related_lockfiles(tool, scope, lock_paths))
        for proposal in proposals:
            edits = {change.pin.path for change in proposal.changes}
            edits.update(_related_lockfiles(proposal.tool, proposal.scope, lock_paths))
            if edits & blocked_paths:
                failed.add((proposal.scope, proposal.tool))
        if len(failed) == before:
            break
    for proposal in proposals:
        if (proposal.scope, proposal.tool) in failed:
            blocked.append(
                f"{proposal.tool} in {proposal.scope}: another pin blocks the coordinated set"
            )
    proposals = [
        proposal for proposal in proposals if (proposal.scope, proposal.tool) not in failed
    ]

    return proposals, blocked


_TOOL_LOCKFILES = {
    "yarn": {"yarn.lock"},
    "npm": {"package-lock.json", "npm-shrinkwrap.json"},
    "pnpm": {"pnpm-lock.yaml"},
    "bun": {"bun.lock", "bun.lockb"},
    "node": {
        "yarn.lock",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
    },
    "go": {"go.sum", "go.work.sum"},
    "rust": {"Cargo.lock"},
    "python": {"poetry.lock", "uv.lock", "Pipfile.lock"},
    "ruby": {"Gemfile.lock"},
    "php": {"composer.lock"},
    "composer": {"composer.lock"},
    "swift": {"Package.resolved"},
    "dart": {"pubspec.lock"},
    "elixir": {"mix.lock"},
    "erlang": {"mix.lock"},
}


def is_lockfile(path: str) -> bool:
    return any(PurePosixPath(path).name in names for names in _TOOL_LOCKFILES.values())


def related_lockfiles(proposal: Proposal, paths: list[str]) -> list[str]:
    return _related_lockfiles(proposal.tool, proposal.scope, paths)


def _related_lockfiles(tool: str, scope: str, paths) -> list[str]:
    names = _TOOL_LOCKFILES.get(tool, set())
    scope_path = PurePosixPath(scope)
    return sorted(
        path
        for path in paths
        if PurePosixPath(path).name in names and PurePosixPath(path).parent == scope_path
    )


def footprint(plan: dict) -> set[str]:
    return {change["pin"]["path"] for change in plan["changes"]} | set(plan["lockfiles"])


def declared_paths(files: dict[str, str]) -> set[str]:
    paths = set()
    for item in _config(files).get("pin", []):
        path = PurePosixPath(item["path"])
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            raise ValueError(f"invalid path: {path}")
        paths.add(path.as_posix())
    return paths


def _checkout_file(root: Path, path: str) -> Path:
    file = root / path
    if file.resolve() != root.resolve() / path or not file.is_file():
        raise ValueError(f"toolchain file is missing or not a regular checkout file: {path}")
    return file


def _read(file: Path) -> str:
    return file.read_bytes().decode("utf-8")


def apply(root: Path, plans: list[dict]) -> None:
    by_path: dict[str, list[dict]] = {}
    for plan in plans:
        for change in plan["changes"]:
            by_path.setdefault(change["pin"]["path"], []).append(change)
    rewritten: dict[Path, str] = {}
    for path, changes in by_path.items():
        file = _checkout_file(root, path)
        text = _read(file)
        previous_start = len(text)
        for change in sorted(changes, key=lambda change: -change["pin"]["start"]):
            pin = change["pin"]
            start, end = pin["start"], pin["end"]
            if end > previous_start or text[start:end] != pin["version"]:
                raise ValueError(f"toolchain pin changed since detection: {path}")
            text = text[:start] + change["replacement"] + text[end:]
            previous_start = start
        rewritten[file] = text
    for file, text in rewritten.items():
        file.write_bytes(text.encode("utf-8"))


def _signature(pin: Pin) -> tuple[str, str, str, str]:
    return pin.path, pin.version, pin.image, pin.variant


def _checkout_pins(root: Path, paths: list[str]) -> list[Pin]:
    files = {path: _read(_checkout_file(root, path)) for path in paths if is_build_input(path)}
    allow = set(paths)
    for path in declared_paths(files):
        if path not in allow:
            raise ValueError(f"toolchain configuration references an untracked file: {path}")
        files[path] = _read(_checkout_file(root, path))
    return _map_pins(inventory(files), files, _config(files).get("pin", []))


def node_recovery_target(root: Path, paths: list[str], manifests: list[str], current: str) -> str:
    """Find a newer Node in the running major that respects the repository's pins."""
    scopes = [PurePosixPath(path).parent for path in manifests]
    line = current.split(".")[0]
    release_lines = {}
    config_path = ".sadoc-toolchains.toml"
    if config_path in paths:
        release_lines = _config({config_path: _read(_checkout_file(root, config_path))}).get(
            "release-line", {}
        )
        if not isinstance(release_lines, dict):
            return ""
    pins = []
    for pin in _checkout_pins(root, paths):
        if pin.tool != "node" or not any(scope.is_relative_to(pin.scope) for scope in scopes):
            continue
        if pin.role == "unresolved":
            return ""
        pins.append(pin)
    scopes.extend(PurePosixPath(pin.scope) for pin in pins)
    declared = [
        release_lines.get(f"{scope}/node", release_lines.get("node", line)) for scope in scopes
    ]
    if any(
        not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)?", value)
        for value in declared
    ):
        return ""
    literals = [
        pin.version.removeprefix("v").removesuffix(".x") for pin in pins if pin.role == "install"
    ]
    for pinned in [*declared, *literals]:
        if pinned == line or line.startswith(pinned + "."):
            continue
        if not pinned.startswith(line + "."):
            return ""
        line = pinned
    target = toolchain_releases.latest("node", line)
    if not target or version_key(target) <= version_key(current):
        return ""
    if any(pin.constraint and not _compatible(pin.constraint, target) for pin in pins):
        return ""
    return target


def protected_pins(pins: list[Pin], plans: list[dict]) -> list[list[str]]:
    selected = {(plan["scope"], plan["tool"]) for plan in plans}
    return sorted(
        [pin.scope, pin.tool, pin.role, *_signature(pin)]
        for pin in pins
        if (pin.scope, pin.tool) not in selected or pin.role != "install"
    )


def protect(root: Path, plans: list[dict], paths: list[str]) -> None:
    plans[0]["protected"] = protected_pins(_checkout_pins(root, paths), plans)


def validate(root: Path, plans: list[dict], paths: list[str], *, proposed: bool = True) -> None:
    pins = _checkout_pins(root, paths)
    if "protected" in plans[0] and plans[0]["protected"] != protected_pins(pins, plans):
        raise ValueError("toolchain change includes pins outside the selected set")
    for plan in plans:
        scope, tool = plan["scope"], plan["tool"]
        actual = [
            pin
            for pin in pins
            if pin.scope == scope and pin.tool == tool and pin.role != "compatibility"
        ]
        expected = [
            _signature(
                replace(pin, version=_target_for_pin(pin, plan["target"])) if proposed else pin
            )
            for raw in plan["pins"]
            if (pin := Pin(**raw))
        ]
        resolved = [_signature(pin) for pin in actual if pin.role == "install"]
        if sorted(resolved) != sorted(expected) or any(pin.role == "unresolved" for pin in actual):
            raise ValueError(
                f"incomplete {tool} toolchain set in {scope}: expected {expected}, found {resolved}"
            )
        for path in plan["lockfiles"]:
            _checkout_file(root, path)


_VERSION_COMMANDS = {
    "bun": "bun --version",
    "composer": "composer --version",
    "dart": "dart --version",
    "elixir": "elixir --version",
    "erlang": """erl -noshell -eval 'io:format("~s~n", [erlang:system_info(otp_release)]), halt().'""",
    "go": "go version",
    "gradle": "./gradlew --version",
    "java": "java -version",
    "maven": "./mvnw --version",
    "node": "node --version",
    "npm": "npm --version",
    "php": "php --version",
    "pnpm": "pnpm --version",
    "python": "python --version",
    "ruby": "ruby --version",
    "rust": "rustc --version",
    "swift": "swift --version",
    "yarn": "yarn --version",
}
_IMMUTABLE_INSTALLS = {
    "bun.lock": "bun install --frozen-lockfile",
    "bun.lockb": "bun install --frozen-lockfile",
    "npm-shrinkwrap.json": "npm ci",
    "package-lock.json": "npm ci",
    "pnpm-lock.yaml": "pnpm install --frozen-lockfile",
    "pnpm-lock.yml": "pnpm install --frozen-lockfile",
    "yarn.lock": "if yarn --version | grep -q '^1\\.'; then yarn install --frozen-lockfile; else yarn install --immutable; fi",
}


def verification_commands(root: Path, plans: Sequence[dict]) -> str:
    """Check the running version in the same environment as the build recipe."""
    scripts = []
    installs = set()
    for plan in plans:
        scope = plan["scope"]
        tool = plan["tool"]
        executable = _VERSION_COMMANDS[tool]
        pins = [Pin(**raw) for raw in plan["pins"] if not raw["image"]]
        if pins:
            precision = max(
                len(pin.version.removeprefix("v").removesuffix(".x").split(".")) for pin in pins
            )
            target = ".".join(plan["target"].split(".")[:precision])
            version = rf"(?<![0-9.])v?{re.escape(target)}(?![0-9{'.-' if precision == 3 else ''}])"
            scope_dir = shlex.quote(str(root / scope))
            diagnostic = shlex.quote(f"{tool} in {scope}: verification must use {target}")
            scripts.append(
                f"(cd {scope_dir} && {executable} 2>&1 | grep -P {shlex.quote(version)}) "
                f"|| {{ echo {diagnostic}; exit 1; }}"
            )
        for path in plan["lockfiles"]:
            lock = PurePosixPath(path)
            if command := _IMMUTABLE_INSTALLS.get(lock.name):
                installs.add((lock.parent.as_posix(), command))
    for scope, install in sorted(installs):
        scripts.append(f"(cd {shlex.quote(str(root / scope))} && {install})")
    return "\n".join(scripts)
