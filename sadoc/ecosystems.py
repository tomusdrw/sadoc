"""The ecosystems Sadoc knows about.

Every repository is checked against this catalogue, whether or not Sadoc can help
with it. A repository built with a language Sadoc does not handle is worth
saying out loud - otherwise it is simply skipped, and the absence of a pull
request looks like "nothing to do" rather than "not supported".

The catalogue is deliberately generous about recognising things. It is more
useful to say "I do not handle Zig yet" than to say nothing about a repository
that clearly has dependencies in it.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass


@dataclass(frozen=True)
class Ecosystem:
    name: str
    label: str
    manifests: tuple[str, ...] = ()
    globs: tuple[str, ...] = ()
    supported: bool = False
    note: str = ""


# Ecosystems Sadoc can update.
SUPPORTED = (
    Ecosystem(
        "npm", "npm", ("package.json",), supported=True, note="package.json and the npm registry"
    ),
    Ecosystem("pnpm", "pnpm", ("pnpm-lock.yaml", "pnpm-workspace.yaml"), supported=True),
    Ecosystem("yarn", "yarn", ("yarn.lock",), supported=True),
    Ecosystem("cargo", "Cargo", ("Cargo.toml",), supported=True, note="Cargo.toml and crates.io"),
    Ecosystem("pip", "pip", ("requirements.txt", "requirements-dev.txt"), supported=True),
    Ecosystem("poetry", "Poetry", ("poetry.lock",), supported=True),
    Ecosystem("uv", "uv", ("uv.lock",), supported=True),
    Ecosystem(
        "go", "Go modules", ("go.mod",), supported=True, note="go.mod and the Go module proxy"
    ),
    Ecosystem("composer", "Composer", ("composer.json",), supported=True),
    Ecosystem(
        "github-actions",
        "GitHub Actions",
        (),
        globs=(".github/workflows/*.yml", ".github/workflows/*.yaml"),
        supported=True,
    ),
    Ecosystem("docker", "Docker", ("Dockerfile",), globs=("**/Dockerfile",), supported=True),
)

# Ecosystems Sadoc recognises but cannot update yet.
UNSUPPORTED = (
    Ecosystem("zig", "Zig", ("build.zig", "build.zig.zon"), note="the Zig package manager"),
    Ecosystem("elixir", "Elixir", ("mix.exs", "mix.lock")),
    Ecosystem("erlang", "Erlang", ("rebar.config", "rebar.lock")),
    Ecosystem("ruby", "Ruby", ("Gemfile", "Gemfile.lock", ".gemspec")),
    Ecosystem("maven", "Maven", ("pom.xml",)),
    Ecosystem(
        "gradle",
        "Gradle",
        ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
    ),
    Ecosystem("sbt", "sbt", ("build.sbt",)),
    Ecosystem("cmake", "CMake", ("CMakeLists.txt",)),
    Ecosystem("bazel", "Bazel", ("WORKSPACE", "MODULE.bazel", "BUILD.bazel")),
    Ecosystem("meson", "Meson", ("meson.build",)),
    Ecosystem(
        "dotnet",
        ".NET",
        (),
        globs=("*.csproj", "*.fsproj", "*.vbproj", "*.sln", "Directory.Build.props"),
    ),
    Ecosystem("swift", "Swift", ("Package.swift", "Package.resolved")),
    Ecosystem("dart", "Dart", ("pubspec.yaml", "pubspec.lock")),
    Ecosystem("haskell", "Haskell", ("stack.yaml", "cabal.project"), globs=("*.cabal",)),
    Ecosystem("clojure", "Clojure", ("deps.edn", "project.clj")),
    Ecosystem("julia", "Julia", ("Project.toml", "Manifest.toml")),
    Ecosystem("nim", "Nim", (), globs=("*.nimble",)),
    Ecosystem("crystal", "Crystal", ("shard.yml", "shard.lock")),
    Ecosystem("lua", "Lua", (), globs=("*.rockspec",)),
    Ecosystem("deno", "Deno", ("deno.json", "deno.jsonc")),
    Ecosystem("vcpkg", "vcpkg", ("vcpkg.json",)),
    Ecosystem("conan", "Conan", ("conanfile.txt", "conanfile.py")),
)

ALL = SUPPORTED + UNSUPPORTED


@dataclass
class EcosystemHit:
    ecosystem: Ecosystem
    manifest: str

    @property
    def supported(self) -> bool:
        return self.ecosystem.supported


def _matches(path: str, ecosystem: Ecosystem) -> str | None:
    """The manifest that made a path match an ecosystem, if any."""
    base = path.rsplit("/", 1)[-1]
    if base in ecosystem.manifests:
        return path
    for pattern in ecosystem.globs:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(base, pattern):
            return path
    return None


def detect(paths: list[str]) -> list[EcosystemHit]:
    """Which ecosystems a repository uses.

    Only the most specific match per ecosystem is kept, and a supported
    ecosystem wins over an unsupported one that matches the same file.
    """
    hits: dict[str, EcosystemHit] = {}
    for path in paths:
        for ecosystem in ALL:
            manifest = _matches(path, ecosystem)
            if not manifest:
                continue
            existing = hits.get(ecosystem.name)
            if existing is None or (not ecosystem.supported and existing.supported):
                hits[ecosystem.name] = EcosystemHit(ecosystem, manifest)
    return sorted(hits.values(), key=lambda h: (h.supported, h.ecosystem.name))


def unsupported(paths: list[str]) -> list[EcosystemHit]:
    """The ecosystems in a repository that Sadoc cannot update.

    Only a manifest at the top level counts. A `docs/Gemfile` or an
    `examples/build.gradle` is a side project, and saying "I cannot look after
    this repository" about it would be noise.
    """
    root = [p for p in paths if "/" not in p]
    return [hit for hit in detect(root) if not hit.supported]


def describe(hits: list[EcosystemHit]) -> str:
    """A sentence about what was found and what is not handled."""
    if not hits:
        return ""
    unknown = [h for h in hits if not h.supported]
    if not unknown:
        return ""
    names = ", ".join(sorted({h.ecosystem.label for h in unknown}))
    manifests = sorted({h.manifest for h in unknown})
    listed = ", ".join(f"`{m}`" for m in manifests)
    return f"I found {names} here ({listed}), and I do not know how to update it yet."
