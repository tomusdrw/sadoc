The repository already has the exact toolchain version changes applied. Use
the pinned toolchains to refresh the listed lockfiles when their package
manager requires it. Preserve the existing dependency constraints and
resolutions. Keep immutable installation in the verification recipe.

Toolchain targets

{{TARGETS}}

Lockfiles you may refresh

{{LOCKFILES}}

Do not edit any other files, change the chosen versions, stage files, or make
commits. A lockfile that stays valid under the new toolchain may stay unchanged.
If you cannot complete the refresh, report the exact failing command and error.

When using mise, run the refresh with `mise exec` and the exact planned tool
versions. For example, use `MISE_LOCKFILE=0 mise exec node@"$NODE_VERSION" -- ...`
with the planned Node version and the package manager's refresh command. Avoid
`mise use`, which creates or rewrites project configuration such as `mise.toml`.

For Yarn 4, regenerate with the pinned Yarn from the lockfile directory using
`YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install --mode=update-lockfile`.
Apply the override only to this command. If Yarn automatically rewrites
`.yarnrc.yml` during the migration, restore that file to its original contents
so no unrelated configuration changes are included.
