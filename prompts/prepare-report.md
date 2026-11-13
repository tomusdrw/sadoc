Your last settled reply was not a usable structured discovery report. Reformat
the existing findings into a complete replacement report. All tools are disabled
for this turn. Do not repeat exploration, builds, tests or cleanup. Do not refer
to "the report above"; only this new final reply will be parsed.

Return these uppercase fields, with SETUP and VERIFY in fenced bash blocks when
needed. Keep all fields outside those blocks:

STATUS: ready | blocked | baseline_failed
SETUP: <the reproducible setup already established, or NONE>
VERIFY: <the actual local build/test commands already established, or NONE>
PREREQUISITES: <unmet local requirements, or NONE>
CI_ONLY: <checks genuinely requiring CI/production access, or NONE>
INPUTS: <JSON array of tracked recipe input paths, or ["."] if uncertain>
NOTES: <what was verified and any unresolved failures>

Preserve failures and missing prerequisites. Never invent a successful command,
drop applicable checks, or move a local test to CI_ONLY to obtain ready. If you
cannot reconstruct a complete recipe from existing evidence, report blocked and
explain why. A ready report will still be independently replayed on a clean tree.
