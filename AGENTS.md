# AGENTS.md

Guidance for coding agents working on this repository.

## What this is

`sadoc` is a nightly dependency-update agent for a user's own GitHub
repositories. It detects outdated dependencies deterministically, uses a local
model to apply the updates and fix what breaks, opens pull requests, drives CI
to green, and answers review comments.

## Layout

```
sadoc/          the package
  cli.py           argument parsing and the subcommands
  config.py        config.toml -> Config
  state.py         SQLite persistence
  detect.py        scope resolution and detection orchestration
  manifests.py     manifest parsers, one per ecosystem
  lockfiles.py     lockfile parsers
  registry.py      version lookups against the registries
  advisories.py    the GitHub Advisory Database
  semver.py        version parsing and constraint semantics
  pipeline.py      the green loop, the CI loop, the review loop, auto-merge
  piworker.py      the pi RPC client
  gitops.py        git wrapper
  gh.py            gh CLI wrapper (REST + GraphQL)
  notify.py        email backends
  alerts.py        what gets emailed and when
  sanitize.py      the privacy scrubber
  report.py        the markdown digest
bin/               entry points
extensions/        the pi extension that sandboxes the model
prompts/           the prompts sent to the model
tests/             pytest suite
docs/design.md     why things are the way they are
```

## Rules

1. **The model never holds a credential that can change anything on GitHub.**
   The orchestrator owns `git push`, `gh pr create` and `gh pr merge`. Do not
   add credentials to the model's environment or to `PiSession`.

2. **Never let the model pick a version.** Versions come from `registry.py` and
   semver arithmetic is done in `semver.py`. The model only edits files.

3. **The project has no runtime dependencies.** The standard library only. Do
   not add an import of a third-party package to `sadoc/` — `pyyaml` was
   removed for exactly this reason.

4. **Keep pull requests free of internal detail.** Any text that will be
   published goes through `sanitize.scrub_text`, and `sanitize.contains_leak`
   is checked before it is used. `tests/test_sanitize.py` guards this.

5. **A manifest states a constraint; a lockfile states what is installed.**
   They are different things, and confusing them causes the tool to rewrite
   manifests that did not need it. See `docs/design.md` §4.

6. **`gh api` converts GET to POST when a field is added.** Always pass
   `--method` explicitly. This is in `gh.py` already; do not remove it.

7. **Group pull requests by the files they touch**, not by tier alone. See
   `pipeline.group_updates` and `tests/test_grouping.py`.

8. **Tests are the contract.** `pytest` must pass on every supported Python
   version. `ruff check .` must be clean.

## Working on the code

```bash
pip install -e '.[dev]'
pytest
ruff check .
ruff format .
```

Run the CLI against a real repository only when you have `gh` authenticated and
the local model running. `sadoc --dry-run run` is the safe way to try the
pipeline end to end.

## Things that look wrong but are not

- `piworker.py` holds stdin open on purpose. Closing it is pi's shutdown signal.
- `extensions/sandbox.ts` does not re-register the `bash` tool. It mutates the
  command in a `tool_call` hook instead. Re-registering it breaks execution.
- The bubblewrap argument order in `extensions/sandbox.ts` is load-bearing.
- `semver.classify` treats a minor bump on a `0.x` version as breaking.

`docs/design.md` explains each of these.
