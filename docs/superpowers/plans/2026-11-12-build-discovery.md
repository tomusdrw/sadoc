# Reliable project discovery implementation plan

**Goal:** Discover reproducible project setup and verification, cache recipes across
unrelated source changes, and report actionable failures.

**Architecture:** Preserve deterministic dependency selection and orchestrator-owned
GitHub writes. Store recipes and diagnostics in SQLite under owner/repository.
Replay discovery on a clean disposable checkout before accepting a recipe.
Fingerprint conventional build inputs plus declared custom files/directories.

**Tech stack:** Python 3.11+ standard library, SQLite, pi RPC, existing shell isolation.

## Requirements and decisions

- No runtime dependencies or GitHub credentials for pi.
- SETUP and VERIFY run in the same shell; failed setup must prevent verification,
  including failed AND-lists that bash errexit alone does not catch.
- Replay starts from the original commit without ignored artifacts, nested fixture
  clones or initialized submodules. Cleanup errors cannot turn a failed operation
  into success or hide its original error.
- pi must settle with a complete, nonempty final response. Rejected requests,
  provider errors, truncation, timeouts and empty final replies fail explicitly.
- Doctor prompts must receive the job name, branch and actual failure logs.
- Production/deployment checks remain CI-only coverage limitations. A local test
  failure must never be hidden by labeling it CI-only.
- Successful recipes use versioned fingerprints of tracked paths, modes and object
  IDs, plus network policy. Conventional inputs include manifests, locks, toolchain
  settings, CI, build instructions and script/config directories.
- INPUTS declares additional literal relative files/directories. Missing declarations
  retain whole-tree invalidation; invalid declarations fail closed. Follow tracked
  symlink targets and include submodule pointers. Additions and deletions count.
- Failures are persisted and memoized per whole revision within a worker; new runs
  retry them. Explicit verify overrides still take precedence.
- Old databases migrate automatically. Legacy short-name/old-version recipes are
  rediscovered. The target repository does not receive configuration changes.

## Implementation and verification

- Protocol and doctor regressions cover failed, rejected, empty, truncated and
  timed-out turns, retry recovery, final-message authority and prompt rendering.
- Build integration tests use real temporary Git repositories and SQLite, replacing
  only pi/network execution. They exercise clean replay, setup shell state, failure
  classification, cleanup, owner isolation, migration and cached-recipe execution.
- Cache tests cover source/test-only reuse; added/edited/deleted build inputs;
  custom files/directories; symlinks; missing/invalid declarations; network policy.
- A no-model smoke test exercises the installed pi RPC shell boundary when pi and
  bubblewrap are available. A live local-model fixture discovery also passed.
- User requested a rebase onto latest main and a pull request. The rebase completed
  without conflicts onto 3c01d67. The nightly installation is not modified directly.

Maintainer invariants and operator instructions are in docs/design.md §11, README.md
and config.example.toml; those documents are authoritative for current behavior.
