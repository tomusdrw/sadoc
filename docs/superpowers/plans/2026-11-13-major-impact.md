# Major-update impact assessment implementation plan

> Execute inline using the executing-plans skill, then request one code review.

**Goal:** Include changelog-based, codebase-specific impact findings for breaking updates.
**Architecture:** One pre-bump prompt in the existing session; a separate sanitized
PR section; raw evidence in existing SQLite PR details.
**Tech stack:** Python standard library, Markdown prompts, pytest.
**Spec:** ../specs/2026-11-13-major-impact-design.md

## Constraints and review focus

- Never let the model choose versions or hold GitHub write credentials.
- Select by each update's breaking classification, including 0.x and security groups.
- Do not claim unavailable evidence proves safety; retain findings even with green tests.
- Keep internal detail out of public text; never publish an unsanitized fallback.
- Preserve ordinary update behavior and the existing verification/migration loop.

## Implementation

- [x] Add integration tests using real Git and SQLite with fake external services:
  assessment precedes bump; only breaking updates are assessed; findings reach the PR
  and local state; patch updates skip it; failures and unsafe/empty reports are explicit.
- [x] Run tests and confirm failure against the current pipeline.
- [x] Add `prompts/impact.md`; integrate with `process_repo` and `pr_body` in
  `sadoc/pipeline.py`; feed findings to migration and preserve migration work in notes.
- [x] Update `prompts/migrate.md` and `docs/design.md` for the added stage.
- [x] Run pytest on Python 3.11–3.13, Ruff checks and formatting, and diff checks.
- [x] Review the final diff and fix actionable findings.

Integration follows the repository's commit, push, and main verification rule.
Validation: 351 tests passed on each supported Python version; Ruff lint and
format checks passed.

## Review decisions

- Accept harmless Markdown whitespace normalization while rejecting raw and
  scrubbed leak markers; a nested-list regression protects source links.
- Retain PiSession's existing error contract: an empty response fails the attempt.
  The test double raises the same PiError instead of simulating an impossible
  successful empty turn. Explicit reports of missing evidence still reach the PR.
