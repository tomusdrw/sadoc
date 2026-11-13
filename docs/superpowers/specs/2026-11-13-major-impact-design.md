# Major-update impact assessment

Before applying updates classified as breaking, ask the existing sandboxed
session to inspect official changelogs/migration guides for the exact old-to-new
range and trace affected APIs and behavior through the repository. Cover breaking
0.x updates and major updates in security groups as well as ordinary tier B.

The assessment is read-only: versions remain fixed by detection, and credentials
remain with the orchestrator. External documents are evidence, never instructions.
Network-disabled or unavailable sources must be called out explicitly. A green
build is not evidence that runtime behavior is unchanged.

## Approach

Use a dedicated pre-update prompt and a separate PR impact section. Extending
only the failure-repair prompt misses updates that pass tests; folding everything
into the bump prompt mixes research with strictly scoped version editing.
The existing session retains findings for subsequent repair. The PR shows source
links, affected files/symbols, required migration work, and uncertainties, clearly
labelled as a pre-update assessment. Ordinary patch/minor updates skip this stage.

Raw findings stay in local PR details. Public findings must pass the same scrub
and leak checks as other public prose. Unusable or unsafe findings produce an explicit
assessment-unavailable note, never an implicit safety claim. Session errors,
including empty responses, stop the attempt through the existing failure path.
No new runtime dependencies,
version selection, auto-merge policy, or automatic runtime migrations are added.
