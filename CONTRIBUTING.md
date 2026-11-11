# Contributing

Thanks for considering a contribution.

## Getting set up

```bash
git clone https://github.com/tomusdrw/sadoc.git
cd sadoc
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The runtime has **no third-party dependencies** — the standard library only.
`pi`, `gh`, `git` and `bubblewrap` are external tools, not Python packages.

## Before you open a pull request

```bash
pytest
ruff check .
ruff format --check .
```

## What a good contribution looks like

- **A test that fails without your change.** The parts that must not regress
  are the semver and constraint semantics, the manifest and lockfile parsers,
  the advisory range matcher, the update grouping, and the privacy scrubber.
- **A note in `docs/design.md`** if you change something that has a subtle
  reason behind it. That file is the record of what was learned the hard way;
  it is more valuable than most of the code.
- **No new runtime dependencies.** If you need one, argue for it first.

## Scope

This tool updates **your own** repositories. It is not a hosted service and not
a general-purpose renovate replacement. Contributions that widen the scope
(beyond dependency updates) are unlikely to be merged.

## Reporting bugs

Include the repository name, what you expected, what happened, and the relevant
part of the log:

```bash
journalctl --user -u sadoc -n 100
sqlite3 ~/.local/share/sadoc/state.db \
  "select * from events order by ts desc limit 20"
```

If the bug is about the model producing the wrong change, the full transcript is
stored locally and can be attached with the ref id from the pull request:

```bash
sqlite3 ~/.local/share/sadoc/state.db \
  "select transcript from pr_details where ref_id='8f2a1c3d'"
```

## Security

If you find a way to make the model push code, open a pull request, or read a
credential, that is a security bug — please report it privately rather than in
a public issue.
