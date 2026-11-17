# Checkout Cleanup Implementation Plan

**Goal:** Reclaim disposable checkouts after every job and recover killed jobs.
**Architecture:** Standard-library workspace context manager with a workdir lock;
all clone-owning pipeline paths use it. CLI recovery runs before model checks.
**Spec:** `docs/superpowers/specs/2026-11-17-checkout-cleanup.md`
**Constraints:** Python >=3.11, no runtime dependencies; preserve sandbox and
credential boundaries; run pytest and ruff; commit and push the finished change.

## Task 1: Workspace lifecycle and integration

- [x] Add regression tests in `tests/test_workspaces.py`: real directory cleanup
  on success/failure/interrupt, partial clones, crash recovery, concurrent locks,
  legacy recognition, unknown paths/symlinks, cleanup failures preserving errors.
- [x] Run tests and observe failures before implementation.
- [x] Add `sadoc/workspaces.py`: `checkout(workdir, owner, repo)` yields a Path;
  `recover(workdir)` cleans abandoned managed and verified legacy checkouts.
- [x] Make `Worker.prepare_clone` a context manager and scope every owning caller;
  adapt existing tests' borrowed checkouts with `nullcontext`.
- [x] Add CLI recovery for run, monitor and doctor before command execution.
- [x] Verify every clone owner, including early exits before sessions start.

## Task 2: Verification and delivery

- [x] Explain reclaimed workspaces and persistent caches in README/design docs.
- [x] Run full pytest and ruff check, inspect the diff and ownership boundaries.
- [x] Review, fix material findings, commit, push, verify the remote commit.

## Review focus

Partial clones without Git metadata; jobs killed before finally; another process
holding the workspace lock; links to directories outside workdir; discovery
borrowing an already-active clone. Each belongs to Task 1's regression tests.


## Validation

Baseline: 467 tests passed. Final implementation: 491 tests passed on Python
3.11, 3.12 and 3.13; Ruff lint and format checks clean. Independent review found
one owned-discovery reporting issue: deleting the directory could replace an
existing discovery report with `clone_failed`. Two failing regression tests
reproduced it; both now pass, retaining the report and marking successful recipes
`cleanup_failed`. The reviewer additionally verified real cross-process lock
contention, SIGKILL recovery and preservation of nested symlink targets.
