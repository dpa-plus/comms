# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- The dashboard streams history incrementally. `/api/status` and the first frame
  a new client receives still carry the complete log; later Server-Sent Events
  frames carry only rows appended since the previous frame, marked
  `events_delta`, and the page merges them by event ID. Filtering still runs over
  every row, and a push now costs what changed instead of the whole append-only
  log — on a store of 11,000 events that is roughly 4.8 MB per push down to tens
  of kilobytes.
- Session views no longer repeat the events already present in the top-level
  history. `current_session`, `active_comms_sessions[]`, `comms_sessions[]`, and
  their `project_sessions[]` equivalents keep their summary counts and drop their
  `events` arrays, which nothing has read since history became continuous.
- Every snapshot reports `events_total`, so a client whose frame was coalesced
  away can detect that it is short and refetch once rather than silently miss
  audit rows.

### Fixed

- A dashboard rebuild that finishes out of order can no longer hand a newly
  opened tab a staler history than one already broadcast.
- Opening a dashboard tab at the same moment the server publishes can no longer
  wedge that tab's stream before it receives anything.
- A snapshot refetch that loses the race with a live update no longer discards
  the newer rows it arrived alongside, and is no longer treated as having
  reconciled when it could not.
- Running the dashboard scoped to one repository no longer clears the project
  selected in the all-project sidebar.
- The retired per-session log selector's `selectedSessionID` key is removed from
  browser storage instead of lingering.

### Documentation

- `POST /api/claim/release-all` (`release_actor_claims`) is documented in the
  protocol reference; it shipped in 0.2.0 but was missing from the endpoint table
  and the discoverable `actions` example.

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
