# Coordinated Toolchain Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement tasks with tests.

**Goal:** Open one verified PR for all pins in each coordinated toolchain set.

**Architecture:** Inventory pins with typed parsers and resolve targets using publisher data. Persist each set with its identity, edit footprint, and expected pins so verification and PR maintenance can enforce the same contract.

**Tech Stack:** Python 3.11+, standard library, SQLite, GitHub CLI, pytest, Ruff.

**Spec:** [Coordinated toolchain updates](../specs/2026-11-19-coordinated-toolchain-updates-design.md)

## Global constraints

One PR per coordinated set. Reuse Sadoc's sandbox, PR lifecycle, and repository scope. Version selection and artifact resolution are deterministic. Toolchain proposals require human review, and verification preserves immutable installs.

## Review focus

- Distinct project scopes selecting the same target never share a branch or block each other.
- An unresolved recognized pin blocks the entire affected set and cannot fall through to an independent dependency PR.
- A coarse pin already selecting the intended line does not generate a no-op update.
- A failed toolchain proposal does not hold up a security fix verified on the current toolchain.
- PR maintenance uses the persisted original pin plan and rejects changed or missing pins.

## Task 1. Discover pins and resolve releases

- [ ] In `tests/test_toolchains.py`, cover inventory of the same tool across workspace manifests, CI, Docker and a version file, role separation, ambiguity, and registry failure.
- [ ] Run `pytest tests/test_toolchains.py` and confirm the tests fail without the inventory.
- [ ] Implement `sadoc/toolchains.py` with a typed pin record and parsers for conventional pins, plus explicit repository mappings. Reuse `environments.py` and `registry.py` adapters where appropriate. Cover Node and package managers, Python, Go, Rust, JVM tools, Ruby, PHP and Composer, Swift, Dart, Erlang and Elixir.
- [ ] Resolve stable or LTS targets from publisher metadata, honoring release-line scope and exact artifact availability. Add tests for unavailable artifacts, same-line floaters and compatibility floors.
- [ ] Run `pytest tests/test_toolchains.py tests/test_environments.py`.

## Task 2. Plan coordinated sets and reserve files

- [ ] Add failing tests for one set spanning multiple manifests, distinct installations of the same tool, shared edit files, and a blocked set that cannot fall through to dependency detection.
- [ ] Wire inventory through `detect.py` and reuse `group_updates` for file overlap. Give toolchain sets stable scope keys. Use the persisted footprint to reserve files per open PR in `cli.py` instead of gating the entire repository on a toolchain PR.
- [ ] Preserve existing dependency behavior for legacy records. Test priority and deferral for overlapping dependency and security work.
- [ ] Run `pytest tests/test_toolchains.py tests/test_grouping.py tests/test_detect.py`.

## Task 3. Apply, verify, and preserve the set

- [ ] Add an icod2-shaped fixture and failing process tests for multiple Yarn pins, Docker bootstrap, lockfile regeneration, and refusal to push incomplete changes.
- [ ] Apply deterministic pin edits in `pipeline.py` and discover the build recipe against a disposable proposed snapshot. Extend `buildcache.py` to track toolchain pin inputs. Run scoped model-assisted migration and artifact regeneration with the exact targets, then reparse and verify every planned pin before a push.
- [ ] Persist the set identity, footprint and plan in SQLite. Use the plan for monitor, CI repairs, merge conflicts, and review edits. Block further writes for an invalid plan and hold toolchain PRs for human review regardless of tier.
- [ ] Run focused pipeline tests and all of `pytest`, `ruff check .`, shell checks, and the CLI smoke test. Open a Sadoc PR against the current `main` and link it.
