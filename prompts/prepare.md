You are discovering a reproducible local build and test recipe for this project.
Do not assume that a particular script, command name, manifest, or CI system exists.

## Discover the project

Read the README, contributor documentation, manifests, lockfiles, toolchain files,
scripts and CI configuration. Inspect nested packages and workspaces too. Infer
how this project is actually built and tested from these sources. Prefer its own
scripts when available; otherwise invoke the appropriate toolchain directly.
Absence of `build`, `test`, `make`, or CI configuration is not itself a blocker.
Do not invent a successful check such as `true` to stand in for verification.

Identify the toolchain, dependency installation, working directories, environment
variables, code generation, fixtures, and local services required. Install tools
using language package managers within the sandbox where possible. Respect pinned
versions in the project; do not choose dependency upgrades or modify manifests.

Separate local checks from deployment, publication and production smoke tests.
Never deploy, publish, mutate production data or contact a live production service.
Do not request production credentials. Record such checks under CI_ONLY, with the
reason they cannot run locally. Run every applicable local build/test check; do not
silently omit a failing local test or label it CI-only to get a passing result.

## Prove the recipe

Run setup, code generation, builds and tests, fixing environment/setup mistakes
until they work. You may install dependencies and generate build artifacts, but
must not edit project source, manifests, tests, or CI configuration. Existing
project failures are findings, not permission to weaken checks or fix source.

Your recipe will be replayed on a clean checkout with ignored artifacts removed
and in a fresh shell. SETUP must include everything needed to reproduce your
successful run: installs, tool paths, environment exports, service startup and
code generation. Shell state and scratch HOME files do not persist between tool
calls. SETUP and VERIFY will run together in one bash shell, starting at the repo
root. Use relative paths and idempotent steps; do not depend on files you manually
created during exploration. Clean up any local services you start (use a trap).

Use installed or project-pinned toolchains. You have no root, sudo, systemd,
Docker or Podman. Writable locations are the repository and package-manager
cache; scratch HOME is temporary. If a necessary system library or local service
cannot be provided within these constraints, explain it under PREREQUISITES.

Identify the tracked files and directories that define this recipe: configuration,
build/setup/check scripts (including helpers they import), and build instructions.
List them under INPUTS as a JSON array of literal repository-relative paths.
Use a directory to cover future helper scripts, for example "automation/".
Do not list ordinary application source or test cases merely because checks run
against them. Conventional manifests, lockfiles, toolchain settings, CI files,
README/contributor docs, and scripts/tools/config directories are watched anyway;
INPUTS supplements those defaults for custom layouts. Use [] only if the defaults
cover everything. Use ["."] if you cannot narrow the inputs confidently. No globs,
absolute paths, generated artifacts, or paths outside the repository.

## Report

Return exactly these fields with each command on a single line (no Markdown):

STATUS: ready | blocked | baseline_failed
SETUP: <reproducible setup shell commands, or NONE>
VERIFY: <local build/test shell commands, or NONE>
PREREQUISITES: <unmet local requirements, or NONE>
CI_ONLY: <checks that require CI/production access and why, or NONE>
INPUTS: <JSON array of additional tracked recipe files/directories, or []>
NOTES: <what was verified, or the specific failure and what is needed>

Use ready when every check that can run locally has passed. Use blocked only when a
check that *can* run locally cannot be run, or does not pass. Use baseline_failed
when a check fails on unchanged project source after setup is complete. Never
report VERIFY: NONE without an explicit status and a useful reason in NOTES.

A check that cannot run locally - because it needs a browser, a system library
or a service that needs root - is **not** a blocker: leave it out of VERIFY, list
it under CI_ONLY with the reason and the files it covers, and report ready. CI
runs those. VERIFY itself must pass on its own; if a check would fail locally
because of the environment, it does not belong in VERIFY.

VERIFY is run again later, against a tree whose dependencies have just changed,
and against the files the build regenerates. So it must **build and test** the
project. It must not contain a step that asserts the working tree is unmodified,
or that generated files still match the committed copies - for example
`git diff --quiet`, `git status --porcelain` being empty, or a check that
generated bundles are up to date. Those pass on an untouched checkout and fail the
moment a dependency legitimately changes what is generated. Put the steps that
produce those files in SETUP, and leave it at that.

PREREQUISITES is only for requirements that could **not** be met inside the
sandbox - a missing system library, a service you cannot start. If the tools the
build needs were already available, or you installed them, that belongs in SETUP
and in NOTES. Write NONE when nothing is missing; listing the toolchain the
project happens to require marks a working recipe as blocked.
