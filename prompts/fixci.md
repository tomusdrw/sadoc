You are fixing a failing GitHub Actions CI run on a pull request.

## Context

This pull request updates dependencies:

{{UPDATES}}

The CI run failed. The failed step logs are below.

## Failed CI logs

```
{{ERROR}}
```

## Your task

1. Work out which check failed and why, from the logs.
2. Distinguish two very different causes:
   - **The dependency upgrade broke the project** -> fix the project source, as in
     the migration task: minimal, idiomatic changes, re-run the failing command
     locally to verify.
   - **The CI environment itself is the problem** (a flaky network fetch, a missing
     tool, a runner image change, a timeout) -> do not hack the source code to work
     around it. Instead adjust the workflow in `.github/workflows/` minimally and
     correctly, and say clearly in your summary that you changed CI configuration.
3. Verify locally by running the failing command if you can.
4. If the failure is unrelated to the dependency update (it was already broken on
   the base branch), say so clearly - the pipeline will then close the PR instead
   of churning.

## Rules

- Fix the cause, not the symptom. Never delete or skip a failing test or check.
- Never weaken a CI check to make it pass.
- Keep changes minimal.
- You cannot use git to commit or push. That is handled for you.

## When you are done

Report back in exactly this format:

```
FIXED: yes | no | partial
CAUSE: <one sentence>
FILES: <comma-separated list of files you changed>
CHANGED_CI_CONFIG: yes | no
VERIFIED: <the command you ran and its result, or "not verified locally">
NOTES: <anything the human should know, or "none">
```
