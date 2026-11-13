# Sadoc

<p align="center">
  <img src="assets/sadoc-readme.png" alt="Sadoc, a Harfoot Trailfinder holding his Star-book" width="240" height="240" />
</p>

[![CI](https://github.com/tomusdrw/sadoc/actions/workflows/ci.yml/badge.svg)](https://github.com/tomusdrw/sadoc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A nightly dependency-update agent for your own GitHub repositories.

Sadoc Burrows is the Trailfinder of the Harfoots in *The Rings of Power*. He keeps
the Star-book - the trails and the signs handed down by the Trailfinders before
him - reads the stars, and leads the caravan east from one seasonal haven to the
next. He is the first to notice that something is wrong in the sky, long before
anyone else thinks to look up.

Which is rather the job of a dependency update agent.

It finds outdated dependencies, opens a pull request for each one, fixes whatever
the update breaks, drives CI to green, and answers review comments - then emails you
a reminder if you forget about it.

The agent runs entirely on **your** hardware against a **local** model. It needs a
GitHub token to push, but the model itself never holds one.

Together with [Nori](https://github.com/tomusdrw/nori), named after fellow Harfoot
Elanor "Nori" Brandyfoot, it forms a suite of self-hosted development tools: Sadoc
keeps dependencies up to date, while Nori deploys and manages Docker-based services.

```
scan      find outdated dependencies          (deterministic, no model involved)
run       bump -> verify -> open PR -> fix CI
doctor    check the default branch and fix what is broken
monitor   chase failing CI and review feedback
remind    email about pull requests left unattended
releases  report which repositories owe a release
report    markdown digest
```

---

## Why not Dependabot?

Dependabot opens a pull request per dependency and stops at the first failing
test. On a repository with 24 manifests that is 24 pull requests, none of which
compile. Sadoc instead:

- groups updates into **one pull request per repository** (per tier), so CI runs
  once instead of thirty times;
- works out **how to build the project itself** by reading its CI configuration,
  including any code generation that has to happen first;
- runs the project's **own** test suite locally before pushing, and hands the
  failure output back to the model to fix;
- watches CI afterwards and feeds **failed step logs** back to the model;
- reads **review comments** and implements them.

It can also switch Dependabot off in the repositories it takes care of, so the two
do not compete.

---

## Requirements

| Requirement | Notes |
|---|---|
| `pi` | the agent harness - <https://github.com/earendil-works/pi> |
| `gh` | GitHub CLI, authenticated with `repo` and `workflow` scopes |
| `git` | |
| Python 3.11+ | standard library only, no runtime dependencies |
| `bubblewrap` | `bwrap` - the sandbox the model runs in |
| A model | any OpenAI-compatible endpoint (a local `llama-server` is typical) |

Supported ecosystems: **npm**, **pnpm**, **yarn**, **Cargo**, **pip/poetry/uv**,
**Go modules**, **GitHub Actions**, plus `Dockerfile` base images and
`composer.json`.

### Ecosystems it does not handle

Sadoc knows about a good many more than it can update: Zig, Elixir, Erlang,
Ruby, Maven, Gradle, sbt, CMake, Bazel, Meson, .NET, Swift, Dart, Haskell,
Clojure, Julia, Nim, Crystal, Lua, Deno, vcpkg and Conan.

When a repository uses one of those, you are told - once, by email - rather than
it being passed over in silence. A repository built with a language Sadoc does
not understand would otherwise look exactly like a repository with nothing to
update.

Only a manifest at the top level counts. A `docs/Gemfile` is a side project, not
the repository, and a `Makefile` says how to build rather than what to depend
on.

To add support for one, the ecosystem needs a manifest parser in
`sadoc/manifests.py`, a registry lookup in `sadoc/registry.py`, and an entry in
`sadoc/ecosystems.py`.

---

## Install

```bash
git clone https://github.com/tomusdrw/sadoc.git ~/.local/share/sadoc
mkdir -p ~/.config/sadoc
cp ~/.local/share/sadoc/config.example.toml ~/.config/sadoc/config.toml
$EDITOR ~/.config/sadoc/config.toml
```

Point the config at your model endpoint, then check the scope:

```bash
~/.local/share/sadoc/bin/sadoc scope
```

```
[12:00:00] in scope: 12   excluded: 89
[12:00:00]   IN  you/some-repo  (pushed 2026-10-05)
```

### Secrets

Never put credentials in `config.toml`. Use `secrets.env`:

```bash
cat > ~/.config/sadoc/secrets.env <<'EOF'
RESEND_API_KEY=re_...
EOF
chmod 600 ~/.config/sadoc/secrets.env
```

The config references the **name** of the variable. The systemd unit loads the
file, and the agent sandbox removes those variables from the model's environment,
so the model can never read them.

---

## Running it

```bash
sadoc scan                    # what is outdated?
sadoc scope                   # which repos are in scope, and why
sadoc run                     # the full pipeline
sadoc run --repo my-repo      # just one repository
sadoc --dry-run run           # everything except push and open the PR
sadoc doctor                  # is the default branch green? fix it if not
sadoc monitor                 # chase CI and review feedback
sadoc remind                  # email about unattended pull requests
sadoc releases                # which repositories owe a release
sadoc selfupdate              # update Sadoc from the latest main branch
sadoc status                  # queue and pull request state
```

### Updating itself

Sadoc runs from a git checkout, and updates itself from the latest `main` before
each nightly run. The pull is **fast-forward only** and the working tree must be
clean, so local changes are never thrown away and a divergent history is refused
rather than merged. If the update cannot happen, the run carries on with the
current version.

If something new was fetched, the runner starts again, so the code that runs is
the code that was just fetched. Set `SADOC_SELF_UPDATE=0` to turn it off.

### Nightly

```bash
cp ~/.local/share/sadoc/systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sadoc.timer sadoc-remind.timer
```

`sadoc.timer` runs at 01:00; `sadoc-remind.timer` runs at 09:00 and only sends
reminders.

---

## Tiers

| Tier | Contents | Merge policy |
|---|---|---|
| **A** | patch and minor updates | may auto-merge once CI is green and no review is outstanding |
| **B** | major version updates (breaking) | always needs a human |
| **C** | published security advisories | always needs a human, and is processed first |

Updates are grouped into pull requests by **the files they touch**, not by tier
alone: a major bump and a patch bump to the same `package.json` have to live in
the same pull request or the branches conflict. The group takes the highest tier
present, so a pull request containing a breaking change is never auto-merged.

---

## Safety model

The model never holds a credential that can change anything on GitHub.

- The agent runs inside a **bubblewrap** sandbox: no network (unless enabled),
  the filesystem read-only outside the workspace, `HOME` redirected to a scratch
  directory.
- A policy gate blocks `git push`, `gh pr create`, `gh pr merge`, `sudo`,
  `systemctl`, `docker` and similar commands outright.
- The **orchestrator**, not the model, commits, pushes, opens and merges.
- The model's environment is scrubbed of `GH_TOKEN` and every `*_API_KEY`.
- Tier B and C pull requests are never auto-merged.
- Before pushing, the diff is scanned for references to the tooling; offending
  comments are removed and anything in real code blocks the pull request.

---

## Privacy

Pull requests are written to look like ordinary dependency updates. They contain no
mention of the model, the agent, or this tool. The full internal record - model
name, transcript, verification output - stays in the local SQLite database, and the
pull request carries only a short opaque reference id:

```html
<sub>
ref 8f2a1c3d
</sub>
```

Look a reference id up locally with:

```bash
sqlite3 ~/.local/share/sadoc/state.db \
  "select repo, number, tier, model, branch from pr_details where ref_id='8f2a1c3d'"
```

The emails are a different matter: they are signed, and they say who is writing.

---

## Configuration

See [`config.example.toml`](config.example.toml) for the annotated reference. The
options that matter most:

| Option | Meaning |
|---|---|
| `owners` | which GitHub accounts to sweep (empty = the authenticated user) |
| `scope.include` / `.exclude` | repo allow/deny lists |
| `scope.skip_forks` | skip forks (on by default - vendored forks produce unreviewable diffs) |
| `scope.max_inactive_days` | ignore repositories untouched for a while |
| `schedule.max_prs_per_night` | cap on pull requests per night |
| `merge.auto_merge_tier` | which tiers may merge themselves |
| `sandbox.allow_network` | the model needs network to install packages |
| `sandbox.allow_containers` | build test environments in rootless container images instead of on the machine |
| `sandbox.container_runtime` | which rootless runtime to use (`podman`; Docker is refused) |
| `models.<task>` | a different model per task (bump / migrate / fixci / review) |
| `repos.<name>.verify` | the local verification command for one repository |
| `persona.*` | the name and voice used in emails and comments |
| `disable_dependabot` | switch Dependabot off in the repositories handled here |

### Working out how to build a project

With no `verify` override configured, pi reads project documentation, manifests,
scripts and CI workflows to discover how to install, generate, build and test the
project. No particular script name or CI configuration is required. Setup and
verification run together so environment exports and working-directory changes
carry through. A project that needs `templ generate` before `go build` does not
need a hand-written override.

A recipe usually needs a database, a browser or a message broker. With
`sandbox.allow_containers` on, the agent builds those into a rootless container
image and runs the checks inside it, instead of installing them on the machine
where they would need root and have to be installed again on every run. The
runtime is rootless (`podman` by default); the Docker socket is never used,
because it is root-equivalent. A container may have the repository and the tool
cache and nothing else: no privileged mode, no host namespaces, no extra
capabilities, no host devices, and no host directory outside the workspace.

Before accepting a recipe, Sadoc replays it in the sandbox after resetting its
disposable clone and removing ignored artifacts as well as untracked files. The
recipe, prerequisites, verification output and checks requiring CI/production
access are saved in the local SQLite database under `owner/repository`. Nothing
is added to the target repository. Production checks are recorded separately;
the discovery agent must not deploy or run tests against live production.

Validated recipes are reused across unrelated application-source and test changes.
Rediscovery happens when build inputs change: manifests, lockfiles, toolchain and
CI configuration, build instructions, conventional script/config directories,
and additional files or directories identified during discovery. Added, deleted,
renamed and edited inputs count, as do tracked symlink targets, submodule revisions
and sandbox network-policy changes. Ordinary checks still run before opening a PR.

A dependency *version* is not a change to the build, so it does not throw the
recipe away: the fingerprint masks versions out, and a lockfile's content is
ignored entirely - only whether it is there counts. A new or removed dependency,
a changed script, a changed toolchain pin, a changed CI workflow and any change
to the files the agent declared still force a fresh look. The recipe is always
replayed on the real tree before a pull request is opened, so a recipe that has
gone stale fails loudly instead of passing quietly - and a cached recipe that
fails is thrown away rather than trusted again.

Discovery reports without an input list conservatively track the whole Git tree.
An explicit empty list means the conventional inputs are sufficient; custom
layouts can name files or entire directories. Failures are tried once per complete
repository revision per worker, then retried on the next run. Existing databases
migrate automatically, and older recipe fingerprints are rediscovered once.

Failures distinguish agent errors, malformed reports, unmet prerequisites,
existing project test failures, and recipes that fail clean-checkout replay.
Missing verification is never treated as a passing check. For a manual override,
`[repos."owner/repository"]` with `verify = "..."` takes precedence over the
existing `[repos.repository]` form. An optional `[models.prepare]` selects the
discovery model; otherwise the bump model is used.

No extra setup command is needed to enable discovery: it runs automatically when
Sadoc processes updates. Overrides must include dependency installation and code
generation too, since they replace the entire discovered recipe.

Discovery shell commands have a 15-minute limit; the same failing command may
execute twice before further identical retries are blocked. Complete recipe replay
has a separate 30-minute limit. Discovery retains a bounded local JSON diagnostic
record (recent commands, timings, output tails, stderr, and unfinished tools) in
`repo_setup.diagnostics`, including when the one-hour initial discovery limit expires.
Session-owned containers are removed on shutdown, including forced termination.
Use fully qualified, project-pinned service images and disposable test databases;
only downloads and images should be reused between runs. Browser downloads use
`PLAYWRIGHT_BROWSERS_PATH` in the persistent tool cache; discovery must smoke-test
required browser engines before long suites.

For `agent_failed`, inspect `diagnostics` and the configured pi/model endpoint; for `blocked`, supply
the reported local prerequisites. `baseline_failed` means a check failed before
dependency changes. `validation_failed` means the saved commands could not
reproduce discovery's result. `cleanup_failed` means the disposable checkout or
agent could not be cleaned up. Fix the reported cause and start a new run to retry
a failed discovery; its failed cache entry does not permanently disable a repo.

To inspect the local recipe and diagnostics, run this from the Sadoc checkout
(replace `owner/repository`; pass a config path to `load()` if needed):

```python
import sqlite3
from sadoc.config import load

db = sqlite3.connect(load().state_db.resolve().as_uri() + "?mode=ro", uri=True)
db.row_factory = sqlite3.Row
try:
    row = db.execute("SELECT * FROM repo_setup WHERE repo = ?", ("owner/repository",)).fetchone()
    if row is None:
        print("No discovery recorded for this repository yet.")
    else:
        for key in (
            "status",
            "setup_command",
            "verify_command",
            "prerequisites",
            "ci_only",
            "build_inputs",
            "notes",
            "verify_output",
            "diagnostics",
        ):
            print(f"{key}: {row[key]}")
finally:
    db.close()
```

### The default branch

`sadoc doctor` looks at the latest run on the default branch. If it is red, the
failed logs go to the model, the model works out what broke, and the fix is
offered as a pull request. Transient failures - a network hiccup, a cancelled
job - are left alone, and a repository that already has a Sadoc pull request open
is not diagnosed twice.

### Releases

Sadoc uses publication history, workflow triggers, and publishing scripts to
work out how a repository releases. A manifest or a build step alone is not
evidence that anything is published.

| Evidence | Suggested action |
|---|---|
| A workflow builds or publishes on `release` | Create/publish a GitHub release; let the workflow run its steps |
| A publishing workflow runs on pushed tags | Create and push a tag matching its configured pattern |
| A publishing workflow runs on `workflow_dispatch` | Run that workflow |
| An npm script publishes a package | Run that script |
| Publishing/deployment runs on the default branch | No manual release for that automated destination |
| Previous GitHub releases or registry publications | Recognise that destination even without a publishing workflow |

Registry lookups use actual package names (excluding private packages), and each
package is compared against its own published version. GitHub releases and tags
are compared only when the repository has a single shared version. Tags alone
do not imply GitHub releases; tag-triggered publishing needs a matching workflow.
A documentation deployment does not suppress a library's separate release process.

A version ahead of an established publication is a release candidate. Configured
workflows with no publication history are described, but do **not** trigger a
first-release nudge. Missing history is not proof of an overdue release. This
check follows version history, not a predicted calendar schedule, and does not
infer a release just because commits have accumulated.

An explicit declaration opts into first-release nudges and overrides inference.
Put an actual annotation in the README (outside code examples):

```html
<!-- sadoc: release = npm, github -->
```

or in a `.sadoc.toml`:

```toml
[release]
kind = "crates.io"
command = "cargo publish"
```

### GitHub Actions and the runtimes inside them

Both are dependencies. The actions a workflow uses are found and bumped, with the
pin written the way it was written - `actions/checkout@v4` becomes `@v7`, not
`@v7.0.1`. The runtime a job runs on is found as well: `node-version`,
`python-version` and `go-version` are compared against the newest release. A
workflow that reads the version from a file (`.nvmrc`, `go.mod`) is left alone,
because it is already in step with the project.

---

## How it works

```
          ┌──────────────┐
          │   detect     │  registry APIs + lockfiles, no model
          └──────┬───────┘
                 │  outdated dependencies, split into tiers
          ┌──────▼───────┐
          │  group       │  cluster by the files each update touches
          └──────┬───────┘
                 │
          ┌──────▼───────┐   pi in RPC mode, inside bubblewrap
          │    bump      │   edit manifests, regenerate lockfiles
          └──────┬───────┘
                 │
          ┌──────▼───────┐
          │   verify     │   the project's own test suite, locally
          └──────┬───────┘
                 │ fails ──▶ migrate: the model fixes the code
          ┌──────▼───────┐
          │  push + PR   │   the orchestrator, not the model
          └──────┬───────┘
                 │
          ┌──────▼───────┐   failing step logs go back to the model
          │  watch CI    │──▶ fixci
          └──────┬───────┘
                 │
          ┌──────▼───────┐   review comments go back to the model
          │   monitor    │──▶ review
          └──────┬───────┘
                 │
          ┌──────▼───────┐
          │  auto-merge  │   tier A only, strict conditions
          └──────────────┘
```

The model is only involved where judgement is needed. Version numbers always come
from the registry, and semver arithmetic is done in code.

### Detection is deliberately precise

A manifest states a *constraint*; a lockfile states what is *installed*. Cargo's
`bytes = "1.0"` means `>=1.0, <2.0` - it already permits `1.12.1`, so the only
change needed is a lockfile refresh, not a manifest edit. Sadoc reads lockfiles
and separates the two cases, so it does not rewrite manifests that did not need it.

Other details that matter:

- a minor bump on a `0.x` version is a breaking change (Cargo and npm convention);
- a package can appear several times in `package-lock.json` - the hoisted entry is
  the one that counts;
- a lockfile-only update in a project without a lockfile is a no-op and is skipped;
- Docker tags are matched within their own version series, never across
  distributions.

---

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

The tests cover the parts that must not regress: semver and constraint semantics,
manifest and lockfile parsing, advisory range matching, update grouping, the
privacy scrubber, the scope guard, and the email rendering.

---

## Licence

MIT - see [LICENSE](LICENSE).
