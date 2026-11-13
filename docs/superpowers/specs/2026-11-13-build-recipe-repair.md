# Build recipe parsing and repair

Implement the proposal approved in the conversation: preserve shell newlines,
validate syntax, allow one repair after failed clean replay, and rediscover the
two failed recipes. No dependency or source changes in target repositories.

Keep the existing report format and prose folding; preserve SETUP and VERIFY
whitespace, with optional fenced shell blocks for unambiguous content. This is
smaller than a JSON protocol migration and supports existing single-line reports.
Update the prompt to demonstrate multiline commands rather than requiring one
line. Add regression coverage for exports, conditionals, continuations, heredocs,
and failed setup.

Syntax-check the combined script through the sandbox before execution. Syntax
checks cannot catch invalid export arguments or nonexistent commands, so clean
replay remains authoritative. On failure, reset the disposable checkout and give
the discovery session the exact recipe and failure output for one repair. Require
a complete replacement report; revalidate its status and input declarations, then
clean and replay again. Preserve failure evidence if repair fails or errors out.
Do not retry explicit blocked/baseline-failed reports or loosen checks to pass.

All commands remain sandboxed, setup and verification share a shell, and final
cleanup restores the original commit. Failed records already retry on a fresh
worker; no database deletion or migration is needed. Bump the recipe fingerprint
to v4 so previously cached false successes are also rediscovered once.
