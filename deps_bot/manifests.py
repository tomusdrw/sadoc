"""Parse dependency manifests into a uniform shape.

Every parser returns a list of `Dep` records:
    name     - the package name as the ecosystem knows it
    current  - the version currently pinned (best effort)
    constraint- the raw constraint from the manifest
    kind     - "runtime" | "dev" | "optional" | "build" | "peer" | "action"
    section  - the manifest key it came from (e.g. "dependencies", "[tool.poetry.dependencies]")
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Dep:
    manifest: str
    name: str
    current: str | None = None
    constraint: str | None = None
    kind: str = "runtime"
    section: str = ""
    extras: list[str] = field(default_factory=list)
    source: str = ""  # for actions: "owner/repo"


# --------------------------------------------------------------------- npm
def parse_package_json(text: str, manifest: str) -> list[Dep]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    out: list[Dep] = []
    sections = {
        "dependencies": "runtime",
        "devDependencies": "dev",
        "peerDependencies": "peer",
        "optionalDependencies": "optional",
    }
    for section, kind in sections.items():
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, constraint in block.items():
            out.append(
                Dep(
                    manifest=manifest,
                    name=str(name),
                    constraint=str(constraint),
                    kind=kind,
                    section=section,
                )
            )
    return out


# -------------------------------------------------------------------- rust
def _cargo_dep_table(table: dict, kind: str, manifest: str, section: str) -> list[Dep]:
    out: list[Dep] = []
    for name, spec in (table or {}).items():
        if isinstance(spec, str):
            constraint, current = spec, spec
        elif isinstance(spec, dict):
            constraint = str(spec.get("version", "")) or None
            current = constraint
            if spec.get("workspace") or spec.get("path") or spec.get("git"):
                # Workspace / path / git deps have no registry version to bump.
                continue
        else:
            continue
        out.append(
            Dep(
                manifest=manifest,
                name=str(name),
                current=current,
                constraint=constraint,
                kind=kind,
                section=section,
            )
        )
    return out


def parse_cargo_toml(text: str, manifest: str) -> list[Dep]:
    try:
        data = tomllib.loads(text)
    except Exception:
        return []
    out: list[Dep] = []
    # In Cargo.toml the dependency tables are top level, siblings of [package].
    out += _cargo_dep_table(data.get("dependencies", {}), "runtime", manifest, "dependencies")
    out += _cargo_dep_table(data.get("dev-dependencies", {}), "dev", manifest, "dev-dependencies")
    out += _cargo_dep_table(
        data.get("build-dependencies", {}), "build", manifest, "build-dependencies"
    )
    # A crate's own [package] table may also carry them (unusual but legal).
    pkg = data.get("package", {}) or {}
    out += _cargo_dep_table(pkg.get("dependencies", {}), "runtime", manifest, "dependencies")
    out += _cargo_dep_table(pkg.get("dev-dependencies", {}), "dev", manifest, "dev-dependencies")
    out += _cargo_dep_table(
        pkg.get("build-dependencies", {}), "build", manifest, "build-dependencies"
    )

    # Workspace root: [workspace.dependencies] plus member crates.
    ws = data.get("workspace", {})
    out += _cargo_dep_table(
        ws.get("dependencies", {}), "runtime", manifest, "workspace.dependencies"
    )
    out += _cargo_dep_table(
        ws.get("dev-dependencies", {}), "dev", manifest, "workspace.dev-dependencies"
    )
    for member in ws.get("members", []) or []:
        if isinstance(member, dict):
            for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                out += _cargo_dep_table(member.get(key, {}), "dev", manifest, f"members.{key}")
    return out


# ------------------------------------------------------------------ python
_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*(?P<extras>\[[^\]]*\])?\s*(?P<op>===|==|>=|<=|~=|!=|>|<)?\s*(?P<version>[^\s;#]+)?"
)


def parse_requirements_txt(text: str, manifest: str) -> list[Dep]:
    out: list[Dep] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-r", "-e", "--", "-c")):
            continue
        m = _REQ_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        op = m.group("op")
        version = m.group("version")
        extras = m.group("extras")
        constraint = f"{op or ''}{version or ''}".strip() or None
        out.append(
            Dep(
                manifest=manifest,
                name=name,
                current=version if op == "==" else None,
                constraint=constraint,
                kind="runtime",
                section="requirements",
                extras=[e.strip() for e in extras.strip("[]").split(",")] if extras else [],
            )
        )
    return out


def parse_pyproject_toml(text: str, manifest: str) -> list[Dep]:
    try:
        data = tomllib.loads(text)
    except Exception:
        return []
    out: list[Dep] = []
    project = data.get("project", {})
    for raw in project.get("dependencies", []) or []:
        dep = _parse_pep508(str(raw), manifest, "project.dependencies", "runtime")
        if dep:
            out.append(dep)
    for extra, reqs in (project.get("optional-dependencies", {}) or {}).items():
        for raw in reqs or []:
            dep = _parse_pep508(
                str(raw), manifest, f"project.optional-dependencies.{extra}", "optional"
            )
            if dep:
                out.append(dep)
    for group, reqs in (project.get("dependency-groups", {}) or {}).items():
        for raw in reqs or []:
            if isinstance(raw, str):
                dep = _parse_pep508(raw, manifest, f"project.dependency-groups.{group}", "dev")
                if dep:
                    out.append(dep)
    poetry = ((data.get("tool", {}) or {}).get("poetry", {}) or {}).get("dependencies", {}) or {}
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue
        constraint = spec if isinstance(spec, str) else (spec or {}).get("version")
        out.append(
            Dep(
                manifest=manifest,
                name=str(name),
                current=constraint,
                constraint=constraint,
                kind="runtime",
                section="tool.poetry.dependencies",
            )
        )
    return out


def _parse_pep508(raw: str, manifest: str, section: str, kind: str) -> Dep | None:
    raw = raw.strip()
    m = re.match(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?P<extras>\[[^\]]*\])?\s*(?P<rest>.*)$", raw)
    if not m:
        return None
    name = m.group("name")
    rest = (m.group("rest") or "").strip()
    constraint = None
    current = None
    if rest:
        constraint = rest
        if rest.startswith("=="):
            current = rest[2:].strip()
    extras = []
    if m.group("extras"):
        extras = [e.strip() for e in m.group("extras").strip("[]").split(",") if e.strip()]
    return Dep(
        manifest=manifest,
        name=name,
        current=current,
        constraint=constraint,
        kind=kind,
        section=section,
        extras=extras,
    )


# ---------------------------------------------------------------------- go
_GO_REQUIRE_LINE = re.compile(r"^\s*([^\s/][^\s]*)\s+(v[^\s]+)")


def parse_go_mod(text: str, manifest: str) -> list[Dep]:
    out: list[Dep] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block:
            if line == ")":
                in_block = False
                continue
            m = _GO_REQUIRE_LINE.match(line)
            if m:
                out.append(
                    Dep(
                        manifest=manifest,
                        name=m.group(1),
                        current=m.group(2),
                        constraint=m.group(2),
                        kind="runtime",
                        section="require",
                    )
                )
            continue
        if line.startswith("require "):
            m = _GO_REQUIRE_LINE.match(line[len("require ") :])
            if m:
                out.append(
                    Dep(
                        manifest=manifest,
                        name=m.group(1),
                        current=m.group(2),
                        constraint=m.group(2),
                        kind="runtime",
                        section="require",
                    )
                )
    return out


# ------------------------------------------------------------- gh actions
_USES_RE = re.compile(r"uses:\s*['\"]?([A-Za-z0-9_.\-/]+)@([^\s'\"#]+)")


def parse_workflow(text: str, manifest: str) -> list[Dep]:
    """Find `uses:` pins in a GitHub Actions workflow."""
    out: list[Dep] = []
    seen = set()
    for m in _USES_RE.finditer(text):
        ref, version = m.group(1), m.group(2)
        if ref.startswith("docker://") or "/" not in ref:
            continue
        key = (ref, version)
        if key in seen:
            continue
        seen.add(key)
        # Local actions (./path) have no version.
        if version.startswith("./"):
            continue
        out.append(
            Dep(
                manifest=manifest,
                name=ref,
                current=version,
                constraint=version,
                kind="action",
                section="uses",
                source=ref,
            )
        )
    return out


# ----------------------------------------------------------------- docker
_FROM_RE = re.compile(r"^\s*FROM\s+([^\s]+)", re.IGNORECASE | re.MULTILINE)


def parse_dockerfile(text: str, manifest: str) -> list[Dep]:
    out: list[Dep] = []
    for m in _FROM_RE.finditer(text):
        image = m.group(1)
        if "@sha256:" in image:
            continue
        name, _, tag = image.rpartition(":")
        if not name:
            # No tag at all, e.g. "FROM ubuntu"
            name, tag = image, "latest"
        if "/" not in name:
            # An official image: "node" lives under "library/node".
            name = f"library/{name}"
        out.append(
            Dep(
                manifest=manifest,
                name=name,
                current=tag or "latest",
                constraint=tag or "latest",
                kind="runtime",
                section="FROM",
            )
        )
    return out


# ------------------------------------------------------------------ entry
PARSERS = {
    "package.json": parse_package_json,
    "Cargo.toml": parse_cargo_toml,
    "requirements.txt": parse_requirements_txt,
    "pyproject.toml": parse_pyproject_toml,
    "go.mod": parse_go_mod,
    "Dockerfile": parse_dockerfile,
}


def parse_manifest(path: str, text: str) -> list[Dep]:
    """Dispatch on the manifest basename."""
    base = Path(path).name
    if base in PARSERS:
        return PARSERS[base](text, path)
    if base.startswith("requirements") and base.endswith(".txt"):
        return parse_requirements_txt(text, path)
    if base.startswith("constraints") and base.endswith(".txt"):
        return parse_requirements_txt(text, path)
    return []
