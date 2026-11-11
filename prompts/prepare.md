You are working out how to build and test a checked-out project.

The goal is a **single shell command** that builds the project and runs its
tests, and that works on a plain Linux machine with the usual toolchains
installed.

## What to do

1. **Read the CI configuration first.** Look at `.github/workflows/`, and any
   other CI files (`Makefile`, `justfile`, `Taskfile`, `.gitlab-ci.yml`,
   `noxfile.py`, `tox.ini`, `Taskfile.yml`, `package.json` scripts, `Cargo.toml`,
   `go.mod`, `pyproject.toml`). The CI already knows the exact commands, the
   order to run them in, and any code generation that has to happen first.
   Mirror what CI does.

2. **Work out the prerequisites.** CI often installs things before building:
   a code generator (`templ generate`, `sqlx generate`, `protoc`, `prisma
   generate`, `openapi-generator`), a toolchain (`rustup target add`, `go
   install`, `pip install -e .[dev]`), or a system package
   (`apt-get install libfoo-dev`).

   - Things you can install with a **language package manager** (cargo, npm,
     pnpm, bun, pip, uv, go) - install them.
   - A **system package** needs root, which you do not have. If one is
     genuinely required, note it clearly - do not try to work around it.

3. **Run the commands and iterate.** Actually execute them until you have a
   command that works end to end: the build succeeds and the tests pass. Fix
   environment problems as you go - generate the code, install the missing
   tool, export the right environment variable.

4. **Do not modify the project.** You are working out how to build it, not
   changing it. Do not edit source files, do not pin versions, do not "fix"
   anything that is broken in the project itself. If the project's own tests
   fail on a clean checkout, that is a finding to report, not something to
   fix. The only things you may create are generated files that the build
   itself produces.

5. **Prefer the project's own commands** over ad-hoc ones. If CI runs
   `npm run build && npm test`, that is the command to report. If CI runs
   `make test`, use that.

## Constraints

- You are in a sandbox: no root, no `sudo`, no systemd, and the only writable
  place is this repository plus a scratch cache directory.
- Keep the command to a single line that can be run with `bash -c`.
- Do not include the checkout, `cd`, or any environment setup that happens
  before it - those are handled for you. Only the commands that run *inside*
  the project directory.

## When you are done

Report back in exactly this format:

```
VERIFY: <one shell command, or NONE>
SETUP: <any steps that must run before it, as part of the same command, or none>
PREREQUISITES: <system packages needed that you could not install, or none>
NOTES: <anything worth knowing - what the command does, how long it takes, whether the tests pass>
```

If the project genuinely cannot be built or tested here, report `VERIFY: NONE`
and explain why in `NOTES`.
