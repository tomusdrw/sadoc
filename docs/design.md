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
bump → verify locally → (fail) migrate → verify → push → watch CI
                                                   → (fail) fixci → watch CI
                                                   → green → maybe auto-merge
```

Running the test suite **locally** first is what makes this affordable: it is a
fast loop that does not consume CI minutes, and it gives the model a precise error
instead of a CI log.

When CI still fails, the failed step logs are fetched with `gh run view
--log-failed` and handed to the model, which can see the real compiler output.
The same session is reused, so the model already knows what it changed.

---

## 7. Privacy

Public repositories should not advertise the tooling that maintains them. The
pull request therefore contains:

- a table of the updated packages;
- an optional short note explaining what had to change in the project's own code;
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
