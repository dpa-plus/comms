# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-08-20

A minor bump rather than a patch: this adds four event types and five commands,
and it changes what `comms check` returns when it blocks. Anything reading that
exit code needs to know.

### Added

- `comms mcp` serves the coordination verbs as MCP tools over stdio, so claiming,
  checking and releasing sit in an agent's tool list every turn instead of being
  commands somebody has to remember to run. Six verbs mapping one-to-one onto
  existing commands: no registration, no inbox, no severity ladder. JSON-RPC with
  no SDK, so the dependency list is unchanged. `docs/DESIGN.md` cut an MCP server
  originally and now records why that was reversed.
- Every tool takes an `actor`, and `Open` accepts a per-request actor override.
  `COMMS_ACTOR` is per process; one MCP server may act for several agents over one
  connection.
- A `blocked` event records a claim comms refused. It is the only evidence the
  tool prevents anything, and `comms status` reports it as COLLISIONS PREVENTED.
- A task graph. `task`, `task_edge` and `task_state` events describe what work
  exists, the order it must happen in, and how far along it is. State is computed
  by replaying the log: nothing writes "ready" or "blocked" into an event.
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
- `comms next`: work waiting to be verified first, and never work you did
  yourself. `comms task show` groups the graph by what can be done about it.
- `comms claim --task <slug>` ties a file claim to a task, so who is working on
  what is derived from work agents already do rather than a second bookkeeping
  step. Releasing the file takes the agent off the task.
- `comms hello --model/--vendor` (or `$COMMS_MODEL` / `$COMMS_VENDOR`). A
  verification then records whether it was `independent` or `same-family`:
  "verified" and "verified by something with the same blind spots" are different
  claims.
- `comms status` gains a TASKS section and a `tasks` array in `--json`.
- The dashboard gains a **Work graph** panel. Layout is computed server-side and
  shipped as coordinates, so an arrow can only ever point rightward and tasks
  joined to nothing are laid out apart from the rest rather than sharing a row
  or column that would imply a relationship. Finished work compresses; work
  waiting for a verifier is the loudest thing on the board. `/api/status` carries
  it as `task_board`, per project.

### Changed

- The top of the dashboard is one 46px rail. It was 163px: a header carrying two
  filesystem paths, and under it a summary band that was 78% empty background:
  312px of tiles stretched across 1425. The paths moved into the title of the
  project name that already identifies the checkout, and the counts moved into
  the headings of the two panels that render the things being counted.
- Stale claims, work waiting to be verified, and dependency cycles are now filled
  red chips on that rail: the only filled shapes on it. All three exist at all
  times and only toggle hidden, and the alert wash is an inset shadow rather than
  a border, so an alarm appearing never moves the page by a pixel. In the
  all-projects view they sum across projects, because a task graph belongs to one
  repository and the merged snapshot carries no board of its own.
- Active claims are grouped by whose work it is and why. One agent editing eight
  files for one reason used to print that reason eight times and squeeze the
  paths, the part you are scanning for, into a scrollbar; now the intent and
  the directory every path shares are printed once, and each file is one line.
- The dashboard's summary tiles only count what the page does not already show
  in full. Findings, notes and the session archive are rendered a few hundred
  pixels away, so tiles for them were chrome; `stale`, `to verify` and
  `dependency cycle` appear only when they are not zero.
- Recent findings, notes and completed work share one scrolling column instead of
  three stacked boxes with three scrollbars, so a sentence is no longer cut in
  half by the panel boundary above it.
- The roster prints an agent's handle once, on the metadata line, instead of
  beside the label where a narrow column broke it mid-hyphen.
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
  log: on a store of 11,000 events that is roughly 4.8 MB per push down to tens
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

### Fixed

- The pre-edit hook never blocked an edit. Claude Code's PreToolUse contract
  treats exit 2 as "block this call" and every other non-zero code as "the hook
  failed"; `comms check` exited 1 on a conflict from the day it shipped, so the
  conflict report went to stderr and was discarded. It was also inverted: comms
  uses exit 2 for its own system errors, so a broken log blocked edits while a
  real collision did not. Only the PreToolUse path changes: `check --staged`
  feeds git, where any non-zero aborts and 1 is the documented value.
- Stealing a stale claim measured how long ago the claim was filed rather than
  whether the agent holding it had gone quiet, so a live agent that had held a
  file for an hour had its work taken. It now reads the holder's last activity,
  which every event already records.
- A finding filed without an explicit path ref is anchored to the claim its
  author holds. Findings only ever resurface through their path refs, so an
  unanchored one was written and never read again; on the real store 720 of 1,501
  findings had no path ref and 415 of those were written while their author held
  an open claim.

- The history panel sized its grid row to its content, so a log of 11,000 events
  made the page 52,000 pixels tall with a scrollbar thumb a few pixels high and
  the panels you actually watch stranded at the top of it. The row is bounded and
  the panel scrolls itself, as the rest of the layout already did.

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
  a pure replay (`state.Fold`): no daemon, no polling, no database.
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
