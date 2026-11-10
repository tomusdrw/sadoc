# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0]

First public release.

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

### Notes

- No runtime dependencies: the standard library only.
- Requires `pi`, `gh`, `git`, `bubblewrap` and an OpenAI-compatible model
  endpoint.
