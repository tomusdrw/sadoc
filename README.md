# deps-bot

[![CI](https://github.com/tomusdrw/deps-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/tomusdrw/deps-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A nightly dependency-update agent for your own GitHub repositories.

It finds outdated dependencies, opens a pull request for each one, fixes whatever
the update breaks, drives CI to green, and answers review comments — then emails
you a reminder if you forget about it.

The agent runs entirely on **your** hardware against a **local** model. It needs a
GitHub token to push, but the model itself never holds one.

```
scan      find outdated dependencies          (deterministic, no model involved)
run       bump -> verify -> open PR -> fix CI
monitor   chase failing CI and review feedback
remind    email about pull requests left unattended
report    markdown digest
```

---

## Why not Dependabot?

Dependabot opens a pull request per dependency and stops at the first failing
test. On a repository with 24 manifests that is 24 pull requests, none of which
compile. deps-bot instead:

- groups updates into **one pull request per repository** (per tier), so CI runs
  once instead of thirty times;
- runs the project's **own** test suite locally before pushing, and hands the
  failure output back to the model to fix;
- watches CI afterwards and feeds **failed step logs** back to the model;
- reads **review comments** and implements them.

It is also quiet: pull requests look like ordinary dependency updates. See
[Privacy](#privacy) below.

---

## Requirements

| Requirement | Notes |
|---|---|
| `pi` | the agent harness — <https://github.com/earendil-works/pi> |
| `gh` | GitHub CLI, authenticated with `repo` and `workflow` scopes |
| `git` | |
| Python 3.11+ | standard library only, no runtime dependencies |
| `bubblewrap` | `bwrap` — the sandbox the model runs in |
| A model | any OpenAI-compatible endpoint (a local `llama-server` is typical) |

Supported ecosystems: **npm**, **pnpm**, **yarn**, **Cargo**, **pip/poetry/uv**,
**Go modules**, **GitHub Actions**, plus `Dockerfile` base images and
`composer.json`.

---

## Install

```bash
git clone https://github.com/tomusdrw/deps-bot.git ~/.local/share/deps-bot
mkdir -p ~/.config/deps-bot
cp ~/.local/share/deps-bot/config.example.toml ~/.config/deps-bot/config.toml
$EDITOR ~/.config/deps-bot/config.toml
```

Point the config at your model endpoint, then check the scope:

```bash
~/.local/share/deps-bot/bin/deps-bot scope
```

```
[12:00:00] in scope: 12   excluded: 89
[12:00:00]   IN  you/some-repo  (pushed 2026-10-05)
```

### Secrets

Never put credentials in `config.toml`. Use `secrets.env`:

```bash
cat > ~/.config/deps-bot/secrets.env <<'EOF'
RESEND_API_KEY=re_...
EOF
chmod 600 ~/.config/deps-bot/secrets.env
```

The config references the **name** of the variable. The systemd unit loads the
file, and the agent sandbox removes those variables from the model's environment,
so the model can never read them.

---

## Running it

```bash
deps-bot scan                    # what is outdated?
deps-bot scope                   # which repos are in scope, and why
deps-bot run                     # the full pipeline
deps-bot run --repo my-repo      # just one repository
deps-bot --dry-run run           # everything except push and open the PR
deps-bot monitor                 # chase CI and review feedback
deps-bot remind                  # email about unattended pull requests
deps-bot status                  # queue and pull request state
```

### Nightly

```bash
cp ~/.local/share/deps-bot/systemd/deps-bot.service ~/.config/systemd/user/
cp ~/.local/share/deps-bot/systemd/deps-bot.timer    ~/.config/systemd/user/
cp ~/.local/share/deps-bot/systemd/deps-bot-remind.service ~/.config/systemd/user/
cp ~/.local/share/deps-bot/systemd/deps-bot-remind.timer    ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now deps-bot.timer deps-bot-remind.timer
```

`deps-bot.timer` runs at 01:00; `deps-bot-remind.timer` runs at 09:00 and only
sends reminders.

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

Pull requests are written to look like ordinary dependency updates. They contain
no mention of the model, the agent, or this tool. The full internal record — model
name, transcript, verification output — stays in the local SQLite database, and
the pull request carries only a short opaque reference id:

```html
<sub>
ref 8f2a1c3d
</sub>
```

Look a reference id up locally with:

```bash
sqlite3 ~/.local/share/deps-bot/state.db \
  "select repo, number, tier, model, branch from pr_details where ref_id='8f2a1c3d'"
```

---

## Configuration

See [`config.example.toml`](config.example.toml) for the annotated reference. The
options that matter most:

| Option | Meaning |
|---|---|
| `owners` | which GitHub accounts to sweep (empty = the authenticated user) |
| `scope.skip_forks` | skip forks (on by default — vendored forks produce unreviewable diffs) |
| `scope.max_inactive_days` | ignore repositories nobody has touched for a while |
| `schedule.max_prs_per_night` | cap on pull requests per night |
| `merge.auto_merge_tier` | which tiers may merge themselves |
| `sandbox.allow_network` | the model needs network to install packages |
| `models.<task>` | a different model per task (bump / migrate / fixci / review) |
| `repos.<name>.verify` | the local verification command for one repository |

### Per-repository verification

The verification command is the gate that runs *before* a pull request is opened.
If it is not configured, CI is the only gate.

```toml
[repos."my-repo"]
verify = "npm ci --no-audit && npm run typecheck && npm test"
```

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
`bytes = "1.0"` means `>=1.0, <2.0` — it already permits `1.12.1`, so the only
change needed is a lockfile refresh, not a manifest edit. deps-bot reads lockfiles
and separates the two cases, so it does not rewrite manifests that did not need it.

Other details that matter:

- a minor bump on a `0.x` version is a breaking change (Cargo and npm convention);
- a package can appear several times in `package-lock.json` — the hoisted entry is
  the one that counts;
- a lockfile-only update in a project without a lockfile is a no-op and is skipped.

---

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

The tests cover the parts that must not regress: semver and constraint semantics,
manifest and lockfile parsing, advisory range matching, update grouping, and the
privacy scrubber.

---

## Licence

MIT — see [LICENSE](LICENSE).
