# Discovery Timeout Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans; implement inline.

**Goal:** Make discovery failures bounded, diagnosable, and repeatable.
**Architecture:** PiSession captures bounded diagnostics; SQLite persists them.
The sandbox bounds shell execution, while the orchestrator owns container cleanup.
**Tech Stack:** Python standard library, TypeScript pi extension, pytest/Bun.
**Spec:** docs/superpowers/specs/2026-11-13-discovery-timeout-design.md

## Global Constraints
- No runtime dependencies or credentials in the model environment.
- Keep clean-checkout replay, the existing sandbox and container policy.
- Discovery deadline 3600s, shell limit 900s, RPC recipe limit 1800s.
- At most two executions of an identical failing discovery command.
- Diagnostics remain local; incomplete reports never pass.

## Review Focus
- Timeout with an active tool must retain command and partial output.
- Large streaming output must not erase useful diagnostic events or grow unbounded.
- Existing databases must migrate without losing recipes.
- Forced pi termination must clean only session-owned containers.
- Shorter model timeouts must still work; successful commands must remain repeatable.

### Task 1: Diagnostics
Files: sadoc/piworker.py, sadoc/pipeline.py, sadoc/state.py,
tests/test_piworker.py, tests/test_build_setup.py.
- [ ] Add failing tests for active-command timeout context, bounded output, migrated
  and persisted diagnostics on discovery failures and successes.
- [ ] Implement timestamped diagnostic JSON, store in repo_setup.diagnostics in
  discovery's finally block, and explain inspection in README.
- [ ] Run pytest tests/test_piworker.py tests/test_build_setup.py.

### Task 2: Execution and cleanup
Files: extensions/sandbox.ts, sadoc/piworker.py, tests/test_container_policy.py,
tests/test_piworker.py, tests/sandbox_policy.ts.
- [ ] Add regression tests for command timeout, failed-command retry limit,
  distinct session sockets, container tagging and cleanup after forced exit.
- [ ] Implement limits on both tool and RPC paths, bounded helper execution,
  per-session helper locations, and orchestrator-owned labeled-container cleanup.
- [ ] Run pytest tests/test_container_policy.py tests/test_piworker.py.

### Task 3: Recipe guidance and verification
Files: prompts/prepare.md, README.md, docs/design.md.
- [ ] Correct cache/database/image guidance, readiness checks, retries, cleanup and
  contradictory instructions about which checks belong in CI.
- [ ] Attempt discovery on the disposable website-creator clone with notifications
  disabled; inspect recorded diagnostics and report any remaining blockers.
- [ ] Run pytest and ruff check .; review the final diff and commit.
- [ ] Push to origin/main without force; confirm remote HEAD and local HEAD agree.
