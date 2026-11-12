You are working out why a repository's default branch is broken, and fixing it.

## The failure

The job `{{JOB}}` on `{{BRANCH}}` failed. The logs are below.

```
{{LOGS}}
```

## What to do

1. **Work out the actual cause.** Read the logs carefully. It is often not what
   the first error line suggests - the first error is frequently a consequence.
   Read the workflow too: a production smoke test or deployment failure does
   not by itself mean the project cannot build locally. Never deploy, publish,
   or run tests that mutate a live production service while diagnosing it.

   - A **new lint or a new warning from a newer toolchain** - the code is fine,
     the tool moved on. Fix the code the way the lint asks.
   - A **dependency that has moved on** - an API changed. Fix the call site.
   - **Something in the project's own code** - a genuine bug that the build has
     just started noticing. Fix it.
   - **A flaky test** - a test that depends on timing, ordering or the network.
     Make it deterministic rather than retrying it.
   - **Something outside the repository** - a runner image, an action, a network.
     Say so plainly and change nothing.

2. **Fix it.** Make the smallest change that makes the build pass. Match the
   style of the surrounding code. Do not reformat anything else, do not upgrade
   dependencies, and do not disable or skip a check or a test.

3. **Verify it.** Run the failing command locally. Iterate until it passes.

4. If you genuinely cannot fix it, say so and explain what is needed.

## Constraints

- You are in a sandbox: no root, no `sudo`, no systemd.
- Stay inside the repository directory.
- You cannot use git to commit, push, or open a pull request. That is handled
  for you.

## When you are done

Report back in exactly this format:

```
FIXED: yes | no | partial
CAUSE: <one sentence>
FILES: <comma-separated list of files you changed>
COMMAND: <the command you used to verify>
RESULT: <the last line of that command's output>
NOTES: <anything the human should know, or "none">
```
