# Build Recipe Repair Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve executable discovery reports and recover once from bad recipes.

**Architecture:** Retain KEY/value reports, preserving shell fields and folding prose.
Syntax-check in the existing sandbox; allow one complete replacement report after
failed replay. Keep clean-checkout validation and failure history authoritative.

**Tech Stack:** Python standard library, Bash, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-11-13-build-recipe-repair.md`

## Global Constraints

- No runtime dependencies or model-held GitHub credentials.
- All recipe execution uses PiSession's sandbox.
- Reset only disposable clones, always to the original commit.
- Commit and push to origin/main without overwriting concurrent work.

## Review Focus

- Shell whitespace and header-looking heredoc content must survive parsing.
- Invalid VERIFY syntax must not execute SETUP side effects.
- Repair must not depend on artifacts or commits from earlier attempts.
- Failed repair must retain diagnostics and never cache success.
- Existing single-line reports, prose, and per-run failure memoization must work.

## Task 1: Preserve shell reports

**Files:** `sadoc/pipeline.py`, `prompts/prepare.md`, `tests/test_build_setup.py`.
**Interface:** `_parse_report(report, *, shell_fields=False)` preserves SETUP/VERIFY
only when requested; `_report_value` optionally preserves whitespace.

- [ ] Add replay tests for multiline exports, conditionals, continuations, heredocs,
  comments, and failing setup; keep prose folding tested independently.
- [ ] Run targeted tests and observe the current parser fail.
- [ ] Implement lossless shell fields and fenced blocks; update the prompt.
- [ ] Run `uv run pytest tests/test_build_setup.py` and require success.

## Task 2: Validate and repair once

**Files:** `sadoc/pipeline.py`, `sadoc/buildcache.py`, `tests/test_build_setup.py`,
`tests/test_buildcache.py`, `docs/design.md`.
**Interface:** discovery syntax-checks before execution, then requests at most one
replacement report using the same session. Each attempt uses a fresh result and
the original checkout; `verify_output` retains all replay diagnostics.

- [ ] Add tests for successful repair, two failed attempts, syntax-only rejection,
  clean repair context/replay, invalid or blocked replacement, and repair errors.
- [ ] Run new tests and observe failures before implementation.
- [ ] Implement the bounded loop and diagnostic preservation; document the contract.
- [ ] Invalidate previously validated recipes with fingerprint v4; test rediscovery
  of a v3 record with otherwise identical inputs.
- [ ] Run the full pytest suite and `ruff check .`; inspect the complete diff.
- [ ] Commit, rebase if necessary, push origin/main, and verify remote equality.
- [ ] Attempt discovery-only reruns of the two repositories if the model is available;
  record outcomes without opening dependency-update PRs.
