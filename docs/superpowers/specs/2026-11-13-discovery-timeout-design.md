# Build discovery timeouts

The failed website-creator run repeatedly tried an unresolved image name, reused
incompatible PostgreSQL data, and spent most of an hour in unrecorded tool calls.
The user authorized all fixes proposed in the investigation.

Keep the one-hour initial discovery budget and independent clean-checkout replay. Bound
individual discovery shell commands to 15 minutes and identical failed commands
to two executions. Persist a bounded, timestamped local diagnostic record with
commands, output tails, errors and the active command. Never accept partial reports.
RPC verification keeps its 30-minute budget. No new runtime dependencies or model
credentials. Diagnostics stay local and out of published PR text.

Use fully qualified project-pinned service images, fresh per-run test data and
bounded readiness checks. Cache downloads/images, not mutable databases. Give
each pi session a separate helper socket and label its containers; the orchestrator
removes only those containers on close, including timeout/forced shutdown. Keep
the runtime outside bubblewrap and preserve the shared policy.

Approaches: increasing the timeout hides setup failures; a repo override bypasses
learning without repairing the cause. Implement general limits, diagnostics and
repeatable setup guidance first. A real discovery run determines remaining
repository-specific blockers; do not weaken tests to manufacture success.
