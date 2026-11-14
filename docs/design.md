# Design notes

Background and rationale for the parts of sadoc that are not obvious from the
code. Most of this document exists because something was learned the hard way.

---

## 1. Why the model does not have the keys

The obvious design is to give the agent a GitHub token and let it do everything.
The design used here is different:

```
pi (has the model)          →  pi extension  →  bubblewrap
   no GitHub credentials         blocks push        no network
   no cloud credentials          blocks gh pr       read-only FS
                                          │
                                          ▼
orchestrator (has the credentials)     commits, pushes, opens PRs, merges
```

The model can only edit files and run commands inside a sandboxed checkout. The
process that holds the credentials is a plain Python program with no model in the
loop. A confused model cannot push, cannot open a pull request, and cannot merge
anything.

That is the single most important design decision in the project.

---

## 2. Talking to `pi`

`pi` runs as a long-lived subprocess speaking JSONL on stdin/stdout. One instance
is kept per repository for the whole pipeline (bump → migrate → fix CI → answer
reviews) so the model keeps its context and does not have to re-read the repository
at every stage.

Four things about this integration are not in the documentation:

1. **stdin must stay open.** Closing it is pi's documented orderly-shutdown
   signal. `printf '...' | pi --mode rpc` therefore exits before the model has
   even answered. The client has to hold stdin open for the whole session.

2. **Do not re-register the `bash` tool.** A pi extension can create its own
   tool definition, but registering it under the name `bash` *replaces* the
   built-in and the agent ends up unable to execute anything. The working
   approach is to hook `pi.on("tool_call")` and **mutate `event.input.command`
   in place** — pi then executes the mutated string.

3. **The tool-call event shape varies.** `tool_execution_end` may carry a bare
   string, a dict whose `structuredContent` is a dict, or a dict whose
   `structuredContent` is a string. The client has to handle all three.

4. **The model id does not have to match the server.** Any string works as long
   as the endpoint is the only model it serves.

---

## 3. The sandbox

`extensions/sandbox.ts` is a pi extension that wraps every shell command in
`bubblewrap`. The argument order is load-bearing, and getting it wrong fails
*silently* rather than loudly:

```
--ro-bind / /            the filesystem, read-only
--dev /dev               must come AFTER --ro-bind, or /dev/null is read-only
--proc /proc
--tmpfs /tmp             replaces /tmp entirely
--dir <scratch>          the writable HOME
--bind <workspace>       must come AFTER --tmpfs /tmp, or the repo vanishes
--chdir <workspace>      otherwise bubblewrap falls back to $HOME
--unshare-net            no network
```

Two more traps:

- `/bin/sh` is bash in POSIX mode on some systems, where sourcing a missing file
  **aborts the whole script**. Use `bash -c` and guard every `source`.
- The script passed to `sh -c` must be quoted. Unquoted, the *outer* shell splits
  it on `;` and `||` and runs parts of the command **outside** the sandbox.

### Test environments belong in containers

A build recipe usually needs a database, a browser, a message broker. Installing
those onto the machine is slow, needs root, and leaves the machine dirty - and
the next run has to do it all again. So the agent is told to build a container
image for anything that needs a service, and to run the checks inside it.

The runtime is **rootless** (`podman`), and the Docker socket is never used:
the socket is `root:docker`, so reaching the daemon is reaching root. The
container rules live in `sadoc/policy.py` and are handed to the extension in
the environment, so there is one list of them rather than one in Python and one
in TypeScript. A container may not:

- run privileged, or with extra capabilities;
- join the host network, pid, ipc, uts or user namespace;
- use an unconfined seccomp profile, or a host device;
- mount the runtime socket, or any host directory outside the workspace.

The runtime is allowed in *both* the `tool_call` path and the `user_bash` path.
That matters: if the model could not run the runtime while it was working out
how the project builds, it could never discover a containerised recipe in the
first place.

### The runtime runs outside bubblewrap

bubblewrap empties the capability bounding set. `newuidmap` carries its
capability as a file capability, and a file capability is masked by the
bounding set, so inside the sandbox it cannot work:

```
newuidmap: Could not set caps
Error: cannot set up namespace using "/usr/bin/newuidmap": exit status 1
```

Without it, podman falls back to a single id mapping, where every uid in the
image except one is unmapped. An image that runs as its own user - a database,
nearly all of them - cannot start.

So the runtime runs **outside** the sandbox. The sandbox sees a shim in place
of the runtime; the shim hands the request over a unix socket to a helper in
the pi process, and the helper applies the same policy before running
anything. The rest of the recipe stays inside bubblewrap, so the write
protection is unchanged: only the runtime itself is outside it.

The helper is the pi process, so it never holds a credential the model could
reach, and it runs the runtime with the user's own environment and a normal
user namespace.

Images and containers live under the persistent tool cache, so the image is
built once and reused.

Shell commands also redirect `XDG_DATA_HOME`, `PNPM_HOME`, pnpm's store directory,
and `COREPACK_HOME` into the persistent tool cache after loading profiles.
Changing `HOME` alone is insufficient: inherited package-manager paths can still
point at the read-only host home. Installs must populate writable cached storage
and reuse it across commands without granting write access to the user's store.

---

## 4. Detection

Version numbers come from the registry; semver arithmetic is done in code. The
model is never asked what the latest version of something is.

The subtle part is that a **manifest states a constraint** and a **lockfile states
what is installed**, and those are different things:

| Ecosystem | Manifest | Meaning |
|---|---|---|
| Cargo | `bytes = "1.0"` | `>=1.0, <2.0` — already permits `1.12.1` |
| npm | `"react": "^18.3.1"` | `>=18.3.1, <19` |
| npm | `"react": "18.3.1"` | exactly `18.3.1` |
| Go | `require x v1.2.3` | exactly `v1.2.3` |
| Python | `flask>=2.0` | `>=2.0` |

So the detection reads lockfiles and separates two cases:

- the constraint already permits the newest release → **lockfile refresh only**;
- the constraint excludes it → **the manifest has to change**.

Without this the tool rewrites manifests that did not need it, which is both noise
and a (small) risk.

Other details:

- a minor bump on a `0.x` version is breaking (Cargo and npm convention), so it
  is tier B, not tier A;
- a package can appear several times in `package-lock.json`; the hoisted (shallowest)
  entry is the one that counts;
- a lockfile-only update in a project that has no lockfile is a no-op, because the
  package manager would already resolve to the newest compatible version.

---

## 5. Grouping updates into pull requests

Splitting purely by tier produces branches that conflict: a major bump and a
patch bump to the same `package.json` cannot live in two pull requests.

Updates are therefore clustered by **the files they would touch** (the manifest
plus the lockfile its ecosystem implies), using union-find. A cluster becomes one
pull request, and its tier is the highest tier among its members. That keeps the
tiering meaningful — a pull request with a breaking change is never auto-merged —
without producing conflicting branches.

---

## 6. The green loop

```
assess breaking updates → bump → verify locally → (fail) migrate → verify → push → watch CI
                                                   → (fail) fixci → watch CI
                                                   → green → maybe auto-merge
```

Running the test suite **locally** first is what makes this affordable: it is a
fast loop that does not consume CI minutes, and it gives the model a precise error
instead of a CI log.

Before editing a breaking update, the same sandboxed session inspects official
changelogs and migration guides for the exact current-to-target range, including
intermediate breaking releases, then traces affected usages in the repository.
This also applies to breaking 0.x updates and major updates in security groups.
Ordinary patch/minor updates skip the assessment. It is read-only; version choices,
credentials, and auto-merge policy remain with the orchestrator.
During assessment the extension exposes only reading/search and sandboxed shell
tools, mounts the checkout (including `.git`) read-only, and refuses container
helper requests. Shell commands may use temporary scratch space for evidence.
The next bump turn restores normal editing access. A post-assessment HEAD and
dirty-file check still fails closed, recording the changed paths and commits
locally if the invariant is violated.

The PR includes a separate **Major-version impact (before changes)** section with
source links, affected files/symbols, migration needs, and validation gaps. This is
retained even when tests pass. Subsequent migration fixes are described separately
in the work summary. Missing sources or incomplete coverage must be explicit;
unusable or unsafe public findings produce an assessment-unavailable notice.
Session errors, including an empty response, stop the attempt before editing. Raw findings remain in local
PR details and logs. Network-disabled runs use local evidence and disclose gaps.

When CI still fails, the failed step logs are fetched with `gh run view
--log-failed` and handed to the model, which can see the real compiler output.
The same session is reused, so the model already knows what it changed.

---

## 7. Privacy

Public repositories should not advertise the tooling that maintains them. The
pull request therefore contains:

- a table of the updated packages;
- an optional short note explaining what had to change in the project's own code;
- for breaking updates, a sanitized changelog-based impact assessment;
- a small `<sub>ref 8f2a1c3d</sub>` reference id.

Everything else — which model, the transcript, the verification output, the
tier — is written to the local SQLite database and looked up by that id.

Two layers keep it that way:

1. a dedicated prompt that asks for a reviewer-facing note and explicitly
   forbids mentioning any tool, model or automation;
2. a scrubber that removes such mentions from the prose, and a diff scanner that
   finds them in files the model edited. Comment lines are removed automatically;
   anything in real code blocks the pull request and asks for a human.

The scrubber never damages technical punctuation — `vi.fn()` has to survive it.

---

## 8. Notifications

GitHub does not email you about activity on your own pull requests, so the bot
does it instead. Two backends:

- **Resend** over HTTP — the API is one POST, no dependencies;
- **SMTP** directly, via `smtplib` — no local mail server required, just an
  account and an app password.

Secrets live in an environment variable referenced by name in the config, never
in the config itself. The systemd unit loads them with `EnvironmentFile=`, and the
sandbox scrubs them from the model's environment.

Every alert has a dedupe key, so a reminder is sent at most once a day per pull
request rather than on every run.

---

## 9. GitHub API traps

- **`gh api` silently converts GET to POST** as soon as a field is added. Every
  paginated read has to pass `--method GET` explicitly, or it becomes a POST and
  returns a confusing 422.
- **`--raw-field` sends strings.** GraphQL rejects `"1"` for an `Int` variable,
  so numbers must use `--field`, which types the value.
- **Query parameters must not be duplicated.** An endpoint that already carries
  `?per_page=100` should not be given `per_page` again.

---

## 10. Advisory lookups

The advisory database is queried per package with `affects=`. Indexing a page of
the advisory list is not enough: the list is newest-first, so the roughly 1000
most recent advisories cover recent packages and miss old ones entirely.

The vulnerable version range uses a comparison-list form (`>= 1.0, <= 1.9`), so
the matcher evaluates every comparison rather than only the first.

---

## 11. Project discovery and recipe replay

Initial discovery has a one-hour deadline, 15-minute shell limits and at most two
executions of an identical failing shell command. RPC recipe replay retains its
30-minute deadline. GNU `timeout` bounds sandbox execution and the external
container helper, so a hung child cannot keep a command waiting indefinitely.
Each session has its own helper socket and an orchestrator-owned container label.
On close, even after forced pi termination, the orchestrator removes only that
session's labeled containers and anonymous volumes. Image downloads remain cached;
test databases must use fresh disposable state and the project's service versions.
`repo_setup.diagnostics` retains bounded tool timings/output and active commands
on success or failure, locally only. It is separate from user-facing recipe notes.

`Worker.ensure_build_setup` runs before the dependency-editing session. Pi reads
`prompts/prepare.md`, explores the checkout, and reports setup and verification
commands plus unmet prerequisites and checks requiring CI. This is discovery of
the project's existing process, not permission to pick dependency versions,
repair source, skip failing local tests, or run production operations.

SETUP and VERIFY are shell programs, not prose: their newlines, indentation and
continuations are preserved. Optional fenced bash blocks protect heredocs whose
contents resemble report headers; use a longer fence when the content includes
backticks. Prose fields continue to fold continuation lines into spaces.

Cleanup must precede the final structured reply. A settled nonempty reply with
missing/invalid fields gets one formatting-only recovery, shared across initial
discovery and recipe repair, with a 120-second timeout. The sandbox extension
removes active tools for this turn and rejects residual calls, so formatting cannot
restart exploration or cleanup. The next ordinary prompt restores the prior tool
set. Only the recovered settled reply is parsed; earlier assistant messages never
substitute for a final report. Another malformed reply, provider error, or timeout
remains a failure. A recovered ready report still requires clean replay.

Playwright projects have an executable container path via
`node "$SADOC_PLAYWRIGHT_RUNNER" [--network NAME] [--postgres-env NAME] -- COMMAND...`.
After a locked dependency install, the helper resolves the installed Playwright
version (rejecting conflicting package versions) and runs its matching official
Ubuntu image through the existing Podman shim. It mounts only workspace/cache,
uses the caller's uid in a rootless user namespace, enables Chromium sandboxing
for its preflight, and uses the image's `/ms-playwright` browsers. The requested
suite retains its own browser launch configuration. All three browser engines must
launch/render/close before the requested suite runs. Optional PostgreSQL preflight
uses the project's `pg` package and an explicit test URL to perform `SELECT 1`
from the same container as the tests. Suite exit codes propagate unchanged.
The shim checks mount sources directly from argv so workspace/cache paths with
spaces or literal quotes retain their boundaries; outside mounts remain refused.

Database containers and tests can share a disposable private network. Where
rootless DNS services are unavailable, create it with `--disable-dns` and inspect
the database's address. Never confuse a host-published port with the database's
internal port. Recipes own network cleanup as well as database cleanup. Image
and runtime errors remain blocking; do not copy mismatched browser libraries
onto the host or weaken security settings. The image's Node version must also
satisfy the target project's toolchain requirements.

The recipe is evidence only after independent replay. Discovery saves the
original commit, resets to it, removes ignored artifacts and nested fixture
clones, and deinitializes submodules before executing the commands. Final cleanup
restores that same commit again. `Git.clean(include_ignored=True)` is deliberately
destructive and must only be used on the orchestrator's disposable clone. Normal
cleanup retains its less aggressive behavior. Errors during final cleanup are
recorded alongside the primary error, and prevent a successful recipe from being
used; they must not erase the agent's actual failure or bypass SQLite recording.

Before replay, the combined script is checked with `bash -n` inside the sandbox.
Syntax diagnostics, including unterminated-heredoc warnings that Bash reports
with a zero exit code, prevent execution. This does not detect semantic errors
such as invalid export arguments or wrong script names; real replay still decides
whether the recipe works.

A failed syntax check or replay gets at most one repair in the discovery session,
with the exact failed commands and diagnostic output. Reset to the original tree
before requesting repair and again before validating its complete replacement
report. The repair must not change source or weaken checks. Explicit blocked or
baseline-failed reports stop immediately. `verify_output` retains both attempts,
including the first failure if repair produces an invalid report or an agent error.
Only successful replay and cleanup make the replacement eligible for caching.

SETUP and VERIFY run in one bash shell so exports and directory changes carry
through. Failures in setup must stop verification, including failed `&&` lists
that `bash -e` alone does not catch. Both model tool calls and orchestrator RPC
`bash` commands use bubblewrap, through `tool_call` and `user_bash` respectively.
Never restore the old unsandboxed subprocess fallback. RPC cancellation, absent
exit codes, and rejected commands are failures, regardless of partial output.

`PiSession.prompt` accepts only a settled, nonempty final assistant message with
stop reason `stop`. Earlier tool-call text and failed retries cannot substitute
for that report. Pi may recover internally from a provider error, so wait for the
settled final result rather than rejecting the first failed attempt. Truncation,
empty replies and timeouts must remain actionable errors, not `VERIFY: NONE`.

`repo_setup` stores recipes under `owner/repository`. `buildcache.py` computes a
`recipe-v4` fingerprint from committed input paths, file modes and content, plus
the sandbox network policy. It always includes conventional manifests,
lockfiles, toolchain/hidden configuration, CI, README/contributor documentation,
shell scripts, and scripts/tools/config directories. Enumerating the current tree
also detects new matching files, deletions, renames and changed submodule pointers.
Tracked symlink targets are resolved through Git, never through the host filesystem.

The fingerprint answers one question: *could the build recipe have changed?* A
dependency **version** does not change how a project is built - it changes what
is installed - so the versions are masked out before hashing. A lockfile records
what was resolved, not how to build, so its content is masked out entirely and
only its presence counts. Without this, every pull request Sadoc opens would
invalidate the recipe it had just proved, and the next night would have to work
out how to build the project from scratch: 52 minutes for `website-creator`, and
nothing else that night.

What still invalidates a recipe: a new or removed dependency, a changed script,
a changed toolchain pin (`engines`, the Go directive, a workflow's
`node-version`), a changed CI workflow, a new or removed lockfile, and any
change to the files the agent declared in `INPUTS`.

Masking is never silent. The recipe is always replayed on the real tree before
a pull request is opened, so a recipe that has gone stale fails loudly. A
cached recipe that fails is marked `stale` and thrown away, so a repository
cannot fail the same way for ever.

The discovery report's `INPUTS` field supplements these defaults with a JSON array
of literal relative files/directories for custom scripts and their helpers. The
normalized list is stored as `build_inputs`. `[]` requests just the defaults;
`["."]` explicitly watches everything. A missing declaration (empty database field)
follows the whole tree conservatively. Invalid declarations fail discovery; corrupt
stored declarations trigger rediscovery. Application-source/test changes that do
not touch a recipe input reuse the recipe, while actual checks still run on the
updated checkout. This is a recipe cache, not a cache of passing test results.

Increment the recipe version when changing the contract in a way that invalidates
previous validation. Schema additions belong in `State.MIGRATIONS` so existing
installations retain their history. Old fingerprints and short-name entries are
rediscovered once; short names do not establish ownership. Runtime tool binaries
and production services are not fingerprinted: every update still executes the
recipe, and environment failures remain failures rather than cached success.

Failures are persisted for diagnosis and memoized for the worker's current run,
so multiple update groups do not repeatedly rediscover the same failure. A new
worker retries failures even with the same tree. Successful cached recipes are
used across runs. During dependency editing, verification deliberately uses the
recipe established on the default branch; it must not rediscover a weaker recipe
from the edited branch just because its tree changed.

Explicit `verify` overrides bypass discovery but still run in the sandbox.
Owner-qualified config keys win over legacy short-name keys. Automatic discovery
and its diagnostics remain local; a recipe's CI-only coverage is not a claim that
those checks passed.

Version 4 invalidates recipes validated before lossless shell parsing and syntax
checks: the previous parser could flatten commands or swallow verification into
a heredoc and incorrectly cache success. Those recipes must be rediscovered once.

Regression coverage lives in `tests/test_build_setup.py`, with real temporary Git
repositories and SQLite but a substituted pi process. `tests/test_piworker.py`
exercises RPC events and, when pi/bwrap are installed, the actual RPC shell sandbox
without a model call. The normal GitHub test matrix does not install pi, so that
smoke test is skipped there. An actual model-driven discovery on a disposable
fixture is an additional manual integration check, not a deterministic unit test.
