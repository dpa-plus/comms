# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/dpa-plus/comms/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dpa-plus/comms/releases/tag/v0.1.0
