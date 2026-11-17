# Disposable checkout cleanup

Sadoc currently retains each `work/owner__repo` clone until the next visit to
that repository, including dependency installs and build output. Reclaim it
when its job ends, even on early returns, failed cloning, or exceptions.

Use a context-managed clone for discovery, updates, doctor, conflict repair,
and CI/review monitoring. Discovery supplied with a checkout borrows it.
Create new clones under the reserved `work/.sadoc-checkouts/` directory.
A workdir-wide advisory lock protects creation, recovery and deletion; an
already active job makes a competing checkout/recovery fail clearly.

Recover abandoned managed checkouts before new work and at startup of run,
monitor and doctor, even when no repositories need work. Migrate old leftovers
only when the direct child name and ordinary `.git/config` identify the exact
GitHub clone Sadoc would have created. Never follow symlinks or delete worktrees,
unrelated directories, caches, reports, state or credentials. Surface cleanup
errors and preserve the primary error when both work and cleanup fail.

## Approaches

- Cleanup only on the next visit leaves inactive repositories indefinitely.
- An age-based sweep can delete a long-running build and delays reclamation.
- Scoped cleanup plus locked recovery handles both normal completion and killed
  processes without a retention timer; this is the selected implementation.

Persistent toolchains, package caches and container images remain reusable and
can still grow. This change prevents checkout accumulation, not total-disk quotas
or automatic eviction of caches or logs. Document the distinction.
