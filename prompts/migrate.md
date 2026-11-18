You are fixing a build or test failure caused by a dependency upgrade.

## Context

The dependency versions in this repository were just updated:

{{UPDATES}}

The project now fails to build or the tests fail. The error output is below.

## Pre-update impact assessment

{{IMPACT}}

Use these findings as context for the repair. Confirm them against the current
code and failure output; they describe risks before the edits, not completed work.

## Failure output

```
{{ERROR}}
```

## Your task

1. Read the failure output and identify the first blocker. If the install fails
   because the committed lockfile is stale (for example, Yarn YN0028), regenerate
   it with the project's package manager and rerun the original check before
   editing source. The intended dependency versions and checks must be preserved.
2. Read the relevant source files and the updated dependency's API (check the
   installed source in the registry cache / node_modules / `~/.cargo/registry` if
   the new API is unclear).
3. Make the **minimum** change to the project source that makes it work with the
   new dependency version.
4. Re-run the failing command to verify your fix. For additional failures, check
   which ones the dependency update introduced before making broader edits. If
   repeated attempts produce the same failure and you have no new evidence to
   act on, stop and report the exact remaining blocker as `no` or `partial`.
5. If the failure genuinely cannot be fixed in the project source (for example the
   new version removed an API with no replacement), do **not** hack around it -
   say so clearly in your summary so the pipeline can drop that dependency instead.

## Rules

- Fix the lockfile or source as the failure requires. Never pin the dependency
  back to the old version - that
  defeats the whole point and the pipeline will reject it.
- Keep the change minimal and idiomatic for the project. Match the existing
  code style.
- Give diagnostic probes an explicit bash tool timeout of 60 seconds or less.
  Allow longer for the project's actual build and test commands as needed.
- Do not touch tests to make them pass unless the test itself relies on the
  changed API (in which case update the test to the new API).
- Do not disable, skip, or weaken a test.
- Do not add new dependencies unless there is genuinely no other way.
- You cannot use git to commit or push. That is handled for you.

## When you are done

Report back in exactly this format:

```
FIXED: yes | no | partial
ROOT_CAUSE: <one sentence>
FILES: <comma-separated list of files you changed>
COMMAND: <the command you used to verify>
RESULT: <the last line of that command's output>
NOTES: <anything the human should know, or "none">
```
