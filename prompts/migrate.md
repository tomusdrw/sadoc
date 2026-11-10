You are fixing a build or test failure caused by a dependency upgrade.

## Context

The dependency versions in this repository were just updated:

{{UPDATES}}

The project now fails to build or the tests fail. The error output is below.

## Failure output

```
{{ERROR}}
```

## Your task

1. Read the failure output carefully and identify the **root cause** in the project's
   own source code - the dependency change is intended and must not be reverted.
2. Read the relevant source files and the updated dependency's API (check the
   installed source in the registry cache / node_modules / `~/.cargo/registry` if
   the new API is unclear).
3. Make the **minimum** change to the project source that makes it work with the
   new dependency version.
4. Re-run the failing command to verify your fix actually works. Iterate until the
   command passes, or until you are confident the fix is correct.
5. If the failure genuinely cannot be fixed in the project source (for example the
   new version removed an API with no replacement), do **not** hack around it -
   say so clearly in your summary so the pipeline can drop that dependency instead.

## Rules

- Fix the source. Never pin the dependency back to the old version - that
  defeats the whole point and the pipeline will reject it.
- Keep the change minimal and idiomatic for the project. Match the existing
  code style.
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
