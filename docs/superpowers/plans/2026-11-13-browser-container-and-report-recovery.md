# Browser container and report recovery implementation plan

Approved scope: run browser checks in a version-matched rootless Playwright
container and recover a malformed final discovery report once without rerunning
exploration. Keep the existing sandbox and clean-replay requirements.

1. Add a reusable `extensions/playwright-runner.mjs` exposed through
   `SADOC_PLAYWRIGHT_RUNNER`. Resolve the installed Playwright package version,
   derive the official image tag, mount only workspace/cache, run as the caller,
   and smoke-test Chromium/Firefox/WebKit before executing the requested command.
   Optional named-network and PostgreSQL environment arguments support the same
   database connectivity checks the test process will use. Fail closed on missing
   packages, unsupported versions, failed browser launches, or failed SQL queries.
   Test command construction and actual preflight execution with fixture packages;
   then exercise the helper through the real sandbox and rootless runtime.
2. Add one short report-format recovery per discovery, shared by initial discovery
   and recipe repair. Validate the replacement exactly like the original, retain
   earlier replay diagnostics, and never accept previous assistant messages.
   Disable model tools for this turn via the sandbox extension, also rejecting
   residual tool calls. Restore tools for the next ordinary turn. Test recovery,
   exhaustion, errors, clean replay, and tool restoration.
3. Explain executable usage and finalization ordering in the discovery prompt and
   design documentation. Run pytest on supported Python versions, Ruff and the
   extension tests; obtain independent review, fix findings, commit and push main.

Use test-driven development: observe each regression fail before implementing it.
No project source/test edits or dependency upgrades in target repositories; no
host library installation, privilege relaxation, or credentials in the model.
