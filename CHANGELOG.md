# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-11-11

comms is one build now, and it no longer depends on an agent remembering to
introduce itself or to claim first. This release was shaped by replaying how
agents used the tool: 26,349 calls from their transcripts and 53,599 logged
events. Upgrading has one step, `sh install.sh`, described in
`docs/INSTALL.md`.

### Changed

- **One build.** The Go binary is retired. `comms` and `comms-graph` are the
  same Python program, installed by `install.sh` from the wheel attached to
  each release. The log format is unchanged, so every existing log still loads.
  Agents made 13 calls to the Go build in the month before this release, and
  the two builds had drifted apart in eight behaviors.
- **Identity comes from the session.** An agent no longer needs `hello`,
  `COMMS_ACTOR` or `--as`. Its session gets a name on first use, and later
  calls resolve to it. `--as` still wins when given, and `hello --as <role>`
  renames the session. Every name tied to one session counts as the same
  owner, so a session is never blocked by its own claim. Before this, the
  session-start hook failed on every recorded run and half of the early hook
  blocks were an agent blocked by itself.
- **The first edit claims the file.** `hook pre-edit` records a claim the first
  time a session edits a free file through Edit, Write or NotebookEdit. Claims
  made this way are released at session end, when the same session claims the
  file by hand, or when someone else wants the file and it has nothing
  uncommitted. Claiming before the first edit had stayed at about 65% across
  model generations.
- `status` and `board` print a short view by default: your claims, who else
  holds how many files, notes to you. `--all` prints everything and `--json`
  is unchanged.
- A claim is marked STALE only when it is old and its holder has also been
  silent for the threshold. A block on a silent holder prints the exact
  command that takes the file over.
- The skill ships inside the package and is 112 lines, down from 831. It loads
  when the session briefing says comms is on, and it covers what agents still
  have to do themselves: claim before a shell write, name subagents, release,
  stop at a refusal.

### Added

- `comms hook session-start`, `hook pre-edit` and `hook session-end` for
  Claude Code. Session start prints a briefing of at most 900 characters in a
  repository that uses comms and nothing anywhere else.
- `comms doctor` checks the install: both commands on `PATH` at this version,
  the three hooks, the skill copies for Claude and Codex, the board service,
  the data home and the commit guard. Each failing check prints the command
  that fixes it, and `doctor --fix` applies what it can.
- `install.sh`, a real version (`comms version`, `--version`), and a release
  workflow that refuses a tag that differs from the package version.
- Commands accept what agents type: global flags before the verb, `task list`,
  `task show`, `claims`, `whoami`, `findings`, several paths on `check` and
  `release`, task ids up to 64 characters in any case, and notes up to 1000
  characters. A mistake is answered in a few lines that name the valid form.
  `<verb> --help` prints that verb with every flag it takes.
- `COMMS_HOME` moves the data home. A repository under a temp directory keeps
  its store under that temp directory.
- Graph-first work view with readable task names, owners and holds. Open work is
  shown first; completed work, search, zoom, keyboard selection and a list
  alternative are available without leaving the board.
- Distinct declared dependency arrows and focused, undirected code connections,
  with explicit link expansion and missing-map/hidden-completed explanations.
- Task-first dashboard with current owners, held scopes, recent results and a
  responsive Team panel. Project, task and agent histories load older events
  without dropping the current view.
- Inline single-hold release confirmation naming the holder and scope, requiring
  an operator and reason, and preserving failed attempts for correction.

### Fixed

- `release <path>` for a path the caller does not hold now fails instead of
  printing success.
- An unknown flag is refused. `claim x --reason y` used to drop the flag and
  exit 0.
- `check --staged` no longer leaks a git usage error when the coordination
  root is not a git repository.
- Tests can no longer write the real data home. 251 of the 258 store folders
  on the author's machine were test leftovers.
- Live graph updates preserve search text, keyboard focus, selection and scroll.
- Multi-word task search retains spaces and leaves no unrelated task selected
  when there are no matches.
- Dashboard connection status now distinguishes a loaded snapshot from an open
  connection, retains prior data on invalid updates and labels delayed streams.
- Indexed literal release scopes to avoid repeatedly scanning all release
  history for every dirty file; wildcard matching and newest-release attribution
  remain unchanged.
- History retains legacy release references, all affected holders and both ends
  of task dependencies. Resubmitted work no longer inherits an old checked label.

### Removed

- The Go build and its release archives.
- `lesson`, `plan` and `completion`. None was called after September 2026, and
  no lesson was ever written.

## [0.4.0] - 2026-11-11

A minor bump, and one with an install step: the dashboard is now served by the
Python build. `comms ui` still works, but it hands over to `comms-graph ui`, so
anyone running the dashboard needs the `comms-graph` package installed alongside
this release. Without it, `comms ui` says how to install it and exits.

The Python build is new in this release. Entries that name it describe how it
behaves now; the fixes made to it while it was being built are not listed
separately.

### Added

- A Python build of comms in `python/`, installed as its own package with the
  console script `comms-graph`. It is the same coordination model as the Go
  binary, built on graphify's code graph, so a claim can warn that the ground
  you are taking is coupled to ground somebody else holds. The two builds share
  one store and read each other's events, so they can run side by side. The
  `comms` name stays with the Go binary.
- `comms-graph` answers every verb `comms` does, including `session`, `doc`,
  `status`, `log`, `hello`, `lesson`, `mcp`, `version` and `completion
  bash|zsh`, plus `board` and `tasks`, which the Go build has no equivalent for.
  Every event it writes is stamped with the actor's session, as the Go build
  does, so `session end` releases its claims and the archive counts add up. It
  also takes a global `--repo` flag or `COMMS_REPO`, and claims several scopes
  atomically: every scope is validated before anything is written.
- `comms-graph guard install [--chain]`, `guard status` and `guard uninstall`
  wire `check --staged` into git as a pre-commit hook, so the commit check runs
  without anybody remembering to type it. The hook is written where git actually
  looks (`git rev-parse --git-path hooks`, which covers `core.hooksPath` and
  worktrees), carries the absolute path of the binary that installed it because
  hooks do not inherit a shell's `PATH`, and is made executable. It refuses to
  overwrite a hook comms did not write; `--chain` moves that hook aside, runs it
  first and keeps its veto, and `uninstall` puts it back. If the guard cannot
  run, the commit is refused and the message names the command that removes the
  hook. `board` and the dashboard say when a repo has a coordination log and no
  guard.
- `comms-graph` reads `git status`, so undeclared work no longer reads as
  safety. Each changed path is attributed from the log: held by a live claim,
  released by somebody, or claimed by nobody. It never guesses an author.
  `board` shows the working tree beside the claims and no longer prints "no
  active claims in this repo" on its own when the tree is dirty; the dashboard
  lists files changed on disk that nobody holds, including files somebody
  released and left uncommitted; and `check --staged` names staged paths nobody
  claimed and refuses a staged deletion of another actor's ground. That deletion
  is the only unattributed change it blocks.
- `comms-graph check --staged` handles an amend. The index says nothing about
  HEAD, and an amend rewrites HEAD, so on its own an empty index would wave
  through an amend of a commit a peer had made seconds earlier. The check now
  looks at the commit being amended and refuses when it touches ground somebody
  else holds, naming that commit and saying there is nothing of yours to
  unstage. Amending your own commit, or amending in a repo nobody coordinates
  in, still works.
- In `comms-graph`, a note that names somebody reaches them. A note containing
  `@their-actor` is printed to that actor on their next command of any kind,
  once. The name must match exactly, so a note to `@claude` does not reach
  `@claude-dev`, while a role suffix still receives (`claude-dev/review` is
  `claude-dev`). The commit guard restates any such note under two hours old,
  because a commit is the last point at which taking a fix along is free.
  Delivery never changes an exit code and stays silent on `check --stdin-json`
  and `--json`.
- The board can release a claim: the one write the server does, through the same
  lock and the same event `release --force` appends. It refuses without an
  actor, without a reason, or on anything but an exact claim id. Work in
  progress is grouped by agent, quietest first, with one **Free all N** button
  per agent.
- The board draws the code map and the task graph for the selected project. Only
  tasks joined by an edge are drawn as a graph; tasks nothing waits on are cards
  underneath, carrying the title, the state in words and the id. A task opens on
  a click and shows its declared checks against their results and the files
  tagged to it, with who holds each.
- `comms-graph task add --review` asks for a review; `task add --probe` marks a
  task made only to reproduce something, so it is not counted as a neighbour of
  real work.
- Claim rows say how long the claim has been held (`for 12m`). The roster shows
  agents active in the last hour plus anyone holding a claim, with the rest one
  click away.
- CI runs the Python suite on Linux and macOS under Python 3.11 and 3.12, and
  installs the built wheel into a clean environment to catch packaging and
  entry-point breaks.

### Changed

- `comms ui` is a launcher. It finds `comms-graph`, translates the flags and
  replaces its own process rather than spawning a child, so Ctrl-C, the exit
  status and any supervisor still reach the process serving the page. `--addr
  host:port` is split into the two flags `comms-graph` takes; `--demo`, `--all`,
  `--open` and `--stale-after` are accepted and ignored rather than rejected;
  `comms --repo X ui` still means what it says. The launchd template is updated
  to match.
- In the Python build review is opt-in: a task without `--review` closes on
  `done`, and closed-without-review unblocks what comes after it just as
  verified does. One task has one agent: claiming ground for a task somebody
  else is holding is refused. `interface` and `artifact` edges are one kind,
  `consumes`, against `sequence`; both old spellings still fold to it, so
  existing logs read unchanged.
- `--size` on `task add` is described as a rough sense of scale, not a gate.
  Nothing ever refused a claim on an L task.
- `brief` returns a task's `--ref` as given. It special-cased one tracker's
  scheme and printed the command that resolves it, which contradicted the
  protocol; examples now use a generic `tracker:PROJ-1234`.
- `check --staged` in the Python build exits 1 on a conflict, 2 when it cannot
  find out, lists every blocked path, and prints a recovery command that runs:
  `git rm --cached -f` before the first commit and `git restore --staged` after,
  with a literal pathspec so a glob character cannot unstage somebody else's
  files. It identifies the committer from the session's hello, so an agent's own
  claims no longer block its own commit inside a git hook. `@name` and `name`
  are the same actor on every verb.
- In `comms-graph`, a claim no longer prints three lines about the missing code
  map when nobody else holds anything; when somebody does and the map cannot
  answer, that is one line. One `claim a b c` renders as one row in `log` and in
  the dashboard feed instead of one row per path. The log still records an event
  per scope and `log --json` is unchanged.
- In `comms-graph`, an over-long note says how far over it is, where it would
  cut, which verbs take a long body, and hands back a version that fits, cut at
  a word. `check` given several paths says how many it got and prints the loop.
  The positional form of `task add` warns, after writing and never blocking,
  when a title reads as technical.
- In `comms-graph brief`, "your own earlier work" means the caller's, not the
  task owner's, and the caller's own tasks are demoted below a stranger's rather
  than dropped.
- The board is written for somebody who does not read code: the feed leads with
  the reason rather than the path, file lists under each agent and the list of
  changed files fold behind their counts, the Work panel shows a task's phase as
  a bar so the title gets the row, titles wrap to two lines, and closed work
  folds behind one "N closed" line. The projects rail hides deleted and
  throwaway stores and says how many it set aside.
- Em dashes are gone from every string and document a person reads, in both
  builds.
- The README is cut to what a first-time reader needs, with Quick start near the
  top. Dashboard reference and the long-form design notes moved to
  `docs/DASHBOARD.md` and `docs/DESIGN.md`.
- The `using-comms` skill says what to do when an edit is refused (do not route
  around the hook), to record how a review was checked, to write task titles in
  plain English for people, and, when blocked, to hand the holder the change in
  a note.

### Fixed

- `comms check` resolved the repository from the process's working directory
  rather than the file, so the pre-edit hook failed to find a repository on
  every edit whenever the session started outside one, including for files
  plainly inside one. It now walks up from the file to the nearest directory
  that exists. A path that is in no repository at all is allowed, since there is
  nothing to coordinate; a repository that can be named but not read still
  blocks.

### Removed

- The Go dashboard: `ui.go`, its Server-Sent Events stream and its Work graph
  panel, 7,133 lines in all, and the `fsnotify` dependency with it. Two
  dashboards read the same log and drew different pictures, and every
  improvement had to be built twice. `comms ui` now launches the Python board.

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

[Unreleased]: https://github.com/dpa-plus/comms/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/dpa-plus/comms/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dpa-plus/comms/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/dpa-plus/comms/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/dpa-plus/comms/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dpa-plus/comms/releases/tag/v0.1.0
