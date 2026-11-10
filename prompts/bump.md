You are updating dependency versions in a checked-out repository.

## Task

Apply exactly the dependency updates listed below. Do nothing else. Do not
reformat, refactor, or "improve" anything that is not part of these updates.

## Updates to apply

{{UPDATES}}

## Manifest locations

{{MANIFESTS}}

## Rules

1. Edit **every** manifest listed in the updates table, in the section named in the
   table. Preserve the existing formatting, ordering and comment style of the file.
2. Do not change any dependency that is not in the table.
3. Do not upgrade a dependency past the version in the table. If the table says
   `1.2.3 -> 1.3.0`, the manifest must end up at exactly `1.3.0`.
4. Preserve the constraint style already used in the file:
   - if the manifest pins an exact version (`"react": "18.3.1"`), write the new exact
     version;
   - if it uses a caret/tilde range (`"react": "^18.3.1"`), keep the operator and
     replace only the version;
   - for Cargo, keep the operator (`"1.2"` stays `"1.3"`, `"=1.2"` stays `"=1.3"`).
5. If a manifest is a workspace root with a `[workspace.dependencies]` table, update
   the entry there **and** check whether member crates inherit it. Where a member
   pins its own version, update that too.
6. Regenerate lockfiles so the manifests and lockfiles stay in sync:
   - `package-lock.json` / `pnpm-lock.yaml` / `yarn.lock` -> run the matching install
     (`npm install --package-lock-only`, `pnpm install --lockfile-only`, ...)
   - `Cargo.lock` -> run `cargo update -p <crate> --precise <version>` for each crate,
     or `cargo update` if that fails
   - `poetry.lock` / `uv.lock` -> `poetry lock` / `uv lock`
   - `go.sum` -> `go mod tidy`
   Only regenerate the lockfile if the project actually has one committed.
7. If a tool needed for step 6 is not installed, skip that lockfile and say so in
   your summary rather than failing.

## Constraints

- You cannot use git to commit, push, or create pull requests. That is handled for you.
- Stay inside the repository directory.

## When you are done

Report back in exactly this format:

```
APPLIED: <count>
MANIFESTS: <comma-separated list of manifest paths you changed>
LOCKFILES: <comma-separated list of lockfiles you regenerated, or "none">
SKIPPED: <comma-separated list of "name: reason" for anything you did not apply>
NOTES: <one line: anything surprising, e.g. a dependency that had to be skipped>
```
