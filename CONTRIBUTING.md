# Contributing to comms

Thanks for your interest in improving `comms`. It is one Python package,
`comms-graph`, that installs two console scripts (`comms-graph` and `comms`)
running the same code.

## Development setup

You need **Python 3.11+**. Everything runs through the `Makefile`:

```bash
make test        # pytest in python/ (creates python/.venv on first run)
make build       # wheel and sdist into python/dist/
make install     # install this checkout like a user would, then run doctor --fix
```

There is no codegen and no database: `comms` reads and writes a per-machine
append-only JSONL log under a file lock. Tests point `HOME` at a temp directory;
never run `install.sh` against your own machine while testing changes to it.

## Before you open a PR

CI runs `python -m pytest -q` on Ubuntu and macOS with Python 3.11 and 3.13, and
a clean wheel install. Run `make test` first.

New behavior needs tests that assert what a caller can observe: exit codes,
output, files on disk. The agent skill has one copy,
`python/comms_graph/data/using-comms/SKILL.md`, and a test checks that every
command it shows is one the CLI dispatches.

## Releases

The version lives in one place, `python/comms_graph/__init__.py`
(`__version__`). To release, bump it, merge to `main`, and push a tag `v<version>`.
The release workflow fails unless the tag matches, then attaches the wheel, the
sdist and `install.sh` to the GitHub release. Users upgrade by rerunning
`install.sh`; `comms-graph doctor` says when an install is stale.

## Branching model

- **`main`** is always-releasable and is what the README links resolve to. Tags (`v*`) are cut from `main` and trigger a release.
- **`develop`** is the integration branch. Open feature branches off `develop`
  and target your PR at `develop`.
- At release time, `develop` is merged to `main` and a tag is pushed.

## Commit messages

Match the existing convention (see `git log --oneline`): a short type prefix
plus an imperative summary:

- `comms: ...` for behavior/features/fixes in the tool
- `security: ...` for security-relevant changes
- `chore: ...` / `docs: ...` for housekeeping and documentation

Please add a bullet to the `## [Unreleased]` section of `CHANGELOG.md` for any
user-visible change.

## Reporting bugs and requesting features

Use the issue templates (the "New issue" button). For **security** issues, do
not open a public issue: follow [`SECURITY.md`](SECURITY.md) instead.

## Code of conduct

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).
