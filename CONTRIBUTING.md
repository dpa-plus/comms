# Contributing to comms

Thanks for your interest in improving `comms`. It's a small, lean project — a
single Go binary with no runtime dependencies — and contributions that keep it
that way are very welcome.

## Development setup

You need **Go 1.25.10+** (see `go.mod`). Everything runs through the `Makefile`:

```bash
make build      # build ./comms with version info baked in
make test       # go test ./...
make race       # go test -race ./...   (what CI runs)
make fmt        # gofmt -w .
make vet        # go vet ./...
make lint       # golangci-lint run     (config in .golangci.yml)
```

There is no codegen, no external services, and no database — `comms` reads and
writes a per-machine append-only JSONL log under a file lock.

## Before you open a PR

CI mirrors these exactly, so run them first:

```bash
make fmt vet test race lint
```

All of the following must be green:

- `gofmt` reports no files (the CI step fails on any unformatted file)
- `go vet ./...` is clean
- `go test -race ./...` passes
- `golangci-lint run` passes

New behavior needs tests. The project leans on a strong test suite — if you add
or change a command, the reducer, or the dashboard's derived state, add a
`*_test.go` covering it.

## Branching model

- **`main`** is always-releasable and is what `go install ...@latest` and the
  README links resolve to. Tags (`v*`) are cut from `main` and trigger a release.
- **`develop`** is the integration branch. Open feature branches off `develop`
  and target your PR at `develop`.
- At release time, `develop` is merged to `main` and a tag is pushed.

## Commit messages

Match the existing convention (see `git log --oneline`): a short type prefix
plus an imperative summary —

- `comms: ...` for behavior/features/fixes in the tool
- `security: ...` for security-relevant changes
- `chore: ...` / `docs: ...` for housekeeping and documentation

Please add a bullet to the `## [Unreleased]` section of `CHANGELOG.md` for any
user-visible change.

## Reporting bugs and requesting features

Use the issue templates (the "New issue" button). For **security** issues, do
not open a public issue — follow [`SECURITY.md`](SECURITY.md) instead.

## Code of conduct

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).
