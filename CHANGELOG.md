# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0]

First public release. Named after Sadoc Burrows, the Trailfinder of the Harfoots
in *The Rings of Power* - he reads the signs and the stars, and is the first to
notice when something is wrong in the sky.

### Added

- **Detection** across npm, pnpm, yarn, Cargo, pip/poetry/uv, Go modules,
  GitHub Actions, `Dockerfile` base images and `composer.json`. Version numbers
  come from the registries; semver arithmetic is done in code.
- **Lockfile awareness.** A manifest states a constraint and a lockfile states
  what is installed, so detection separates "the manifest has to change" from
  "only the lockfile needs refreshing".
- **The green loop.** Bump the manifests, run the project's own test suite
  locally, fix what breaks, commit, push, open a pull request, then watch CI and
  feed failing step logs back to the model.
- **Review handling.** Review comments are fetched, handed to the model, and the
  resulting changes are pushed; review threads are resolved through the GraphQL
  API.
- **Tiers.** Patch and minor updates may auto-merge; major updates and security
  advisories always need a human. Pull requests are grouped by the files they
  touch so tiers never produce conflicting branches.
- **A sandboxed agent.** Every shell command the model runs goes through
  bubblewrap: no network by default, a read-only filesystem outside the
  workspace, and a policy gate that blocks `git push`, `gh pr create`,
  `gh pr merge` and similar commands. The orchestrator, not the model, owns all
  remote state.
- **Privacy.** Pull requests contain no mention of the tooling. The full record
  is kept in a local SQLite database and the pull request carries only a short
  reference id. A scrubber and a diff scanner enforce this.
- **Notifications.** Email (via Resend or plain SMTP) for new pull requests,
  unrecoverable CI failures, review comments, auto-merges, and reminders about
  pull requests left unattended.
- **A nightly runner** with a wall-clock window, a pull request budget, a
  persistent queue in SQLite, and systemd units for both the nightly run and the
  morning reminders.
- **A report** of what is outdated, what is open, and what needs attention.
- **Build discovery.** With no verification command configured, the agent reads the
  CI configuration and works out the build and test commands itself, including any
  code generation step, and remembers the result.
- **A scope guard.** Manifest edits that are not one of the requested dependencies
  are reverted, so a language or toolchain directive is never changed by accident.
- **Dependabot handover.** Dependabot version updates can be switched off in the
  repositories this tool handles, so the two do not compete.
- **Unsupported ecosystems are reported.** A repository built with Zig, Elixir,
  Gradle or any of two dozen other languages is now said out loud, by email,
  rather than passed over in silence.
- **Self-update.** Sadoc pulls the latest `main` before each nightly run. The
  pull is fast-forward only and refuses to touch a dirty working tree. If
  something new was fetched, the runner starts again so the new code is what
  runs.
- **A voice.** Emails and GitHub comments are signed and written in Sadoc's voice;
  pull request descriptions carry a single closing line and nothing else.
- **GitHub Actions and the runtimes inside them.** The actions a workflow uses are
  found and bumped, and so is the runtime a job runs on - `node-version`,
  `python-version`, `go-version`.
- **The doctor.** The latest run on the default branch is examined; if it is red,
  the failed logs go to the model and the fix is offered as a pull request.
- **Releases.** The release mechanism of each repository is worked out - crates.io,
  npm, PyPI, a GitHub release, or nothing because it deploys from its default
  branch - and you are nudged when the version moves past what has been published.
- **Dependabot handover.** Dependabot version updates can be switched off in the
  repositories this tool handles, so the two do not compete.
- **Unsupported ecosystems are reported.** A repository built with Zig, Elixir,
  Gradle or any of two dozen other languages is now said out loud, by email,
  rather than passed over in silence.
- **Self-update.** Sadoc pulls the latest `main` before each nightly run. The
  pull is fast-forward only and refuses to touch a dirty working tree. If
  something new was fetched, the runner starts again so the new code is what
  runs.

### Notes

- No runtime dependencies: the standard library only.
- Requires `pi`, `gh`, `git`, `bubblewrap` and an OpenAI-compatible model
  endpoint.
