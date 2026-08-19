# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A task graph. `task`, `task_edge` and `task_state` events describe what work
  exists, the order it must happen in, and how far along it is. State is computed
  by replaying the log — nothing writes "ready" or "blocked" into an event.
- Every task is two steps: an agent does it, then a **different** agent verifies
  it. A task unblocks what comes after it only once it has been **verified**, not
  when it is merely finished, which puts review on the critical path rather than
  at the end.
- The reducer refuses transitions rather than trusting the writer, and keeps the
  refusal: a self-review is rejected (including one wearing a role suffix, so
  `claude-dev/review` cannot verify `claude-dev`), and work whose declared checks
  did not pass cannot reach review.
- Edges carry what the later task **consumes** from the earlier one, not just the
  ordering. `interface` and `artifact` edges propagate a rework to their
  successors; `sequence` edges do not. `comms brief <slug>` walks the incoming
  edges and hands an agent the interface it depends on plus the decisions the
  upstream agent recorded while building it.
- `comms plan --from` appends a whole decomposition atomically, validating slugs,
  sizes, unknown endpoints, duplicate edges and dependency cycles first. A cyclic
  plan writes nothing.
- `comms next` — work waiting to be verified first, and never work you did
  yourself. `comms task show` groups the graph by what can be done about it.
- `comms claim --task <slug>` ties a file claim to a task, so who is working on
  what is derived from work agents already do rather than a second bookkeeping
  step. Releasing the file takes the agent off the task.
- `comms hello --model/--vendor` (or `$COMMS_MODEL` / `$COMMS_VENDOR`). A
  verification then records whether it was `independent` or `same-family`:
  "verified" and "verified by something with the same blind spots" are different
  claims.
- `comms status` gains a TASKS section and a `tasks` array in `--json`.

### Changed

- Readers now skip log entries whose event type they do not recognize, warning
  once per read with the count, instead of refusing to read the file. Writers
  still refuse to emit an unknown type.

  This is what makes it possible to add an event type at all. Previously one
  unrecognized line aborted the whole read, so a single event written by a newer
  `comms` made `status`, `log`, `claim`, `note` and the `check` pre-edit hook all
  fail on that repository. Install this release everywhere before any newer
  version writes a new type.
- `comms log --type` validation and its flag help now read the known type list
  from one place, so the two can no longer drift apart.
- `comms log` prints a plain row for an event type it cannot render, rather than
  silently omitting it.

## [0.2.1] - 2026-08-19

### Changed

- The dashboard now presents one continuous, newest-first History across all
  valid repositories. Project and session identities remain labels and filters
  instead of separate event-log views.
- History renders progressively in 500-row chunks while filtering against the
  complete event set, keeping large local stores responsive.
- Minimum and release Go toolchains are pinned to patched versions 1.25.13 and
  1.26.6 following standard-library advisories GO-2026-6090, GO-2026-6089, and
  GO-2026-5972.

### Fixed

- Inactive or unended named sessions no longer disappear from the dashboard
  after their actors and claims age out of the active window.
- Switching projects can no longer retain a stale archived-session selection or
  banner from another repository.

## [0.2.0] - 2026-08-13

### Added

- `comms check --staged` pre-commit guard, which reports every staged path
  claimed by another actor and prints shell-safe commands to unstage those paths
  without discarding working-tree changes.
- `comms release <id> <id> ...` for atomically releasing several selected
  claims after every supplied ID has been resolved and validated.

### Changed

- Staged-path recovery now remains executable from an external working
  directory, preserves unusual Git filename bytes, treats claim globs
  one-sidedly, and safely unstages changed files before an initial commit.
- Minimum Go version raised to 1.25.11; Go modules and GitHub Actions updated.

## [0.1.0] - 2026-06-17

First public release.

### Added

- Coordination primitives: `claim` / `release` exclusive per-session file
  claims, `find` (bug/fix/ship/decision/gotcha) and `note` findings, `session`
  start/join/end/retire/lead, and a per-repo `doc` wiki plus global `lesson`s.
- Append-only JSONL event log per repo with a per-repo `flock`; current state is
  a pure replay (`state.Fold`) — no daemon, no polling, no database.
- `comms check` PreToolUse hook that warns before editing a path another actor
  has claimed.
- Unified live web dashboard (`comms ui`): one view across every repo, pushed
  over Server-Sent Events from a file watcher (no polling), with a roster,
  active claims, findings/notes, and per-session event logs. The dashboard
  auto-reloads open tabs when the server is upgraded.
- Liveness surfacing: silent claim-holders and retired-but-still-claiming
  actors stay visible and one-click releasable; stale claims (idle > 1h) are
  flagged and stealable without confirmation.
- `comms version` / `--version` with build metadata injected at release time.
- A `using-comms` skill for Claude/Codex agents and a launchd login-service
  template (`contrib/launchd/`) for running the dashboard on macOS.

[Unreleased]: https://github.com/dpa-plus/comms/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/dpa-plus/comms/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/dpa-plus/comms/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dpa-plus/comms/releases/tag/v0.1.0
