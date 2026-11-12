# Protocol reference

This page describes the log comms writes, how it identifies callers, where it
stores data, the hook contract and the board's HTTP API.

## Event log format

The log is a JSON lines file with one event per line. comms only ever appends to it.

```jsonl
{"ts":"2026-11-11T19:26:31.62865Z","id":"01M7616Z6CAF9MA0X3EY6XNVGG","actor":"claude-4f9a1c","type":"hello","data":{"agent_session":"4f9a1c22-aaaa","hostname":"Mac","model":"claude-opus","vendor":"anthropic"}}
{"ts":"2026-11-11T19:26:31.866268Z","id":"01M7616ZDT10X69FD8VV7NFP7G","actor":"claude-4f9a1c","type":"claim","scope":["src/new.ts"],"data":{"auto":true,"intent":"auto: edited"}}
{"ts":"2026-11-11T19:26:44.650329Z","id":"01M7617BXACE3J2MJ2V3AT2T3Z","actor":"claude-billing","type":"note","data":{"body":"@claude-4f9a1c META_TOKEN_ENC_KEY cannot change after the first deploy"}}
{"ts":"2026-11-11T19:26:32.868603Z","id":"01M76170D44EDZ229ZRBK5XA56","actor":"claude-4f9a1c","type":"release","data":{"refs":["01M7616ZDT10X69FD8VV7NFP7G"],"result":"session ended"}}
```

| Field | Type | Notes |
|---|---|---|
| `ts` | string | RFC 3339 in UTC. |
| `id` | string | ULID: 26 characters, time-ordered. |
| `actor` | string | The name of the caller. See [Identity](#identity). |
| `type` | string | `hello`, `claim`, `release`, `note` or `blocked`. Old logs also hold the [retired types](#retired-types). |
| `scope` | array | Set on `claim` only. |
| `data` | object | Fields per type, below. Every field is optional unless noted. Readers ignore fields they do not know. |

Any event can also carry `comms_session_id` and `comms_session_name` in `data`
when its actor is in a named work window. See [Named work windows](#named-work-windows).

## Event types

### `hello`

Ties an actor to a host agent session and records who it is.

```json
{"data": {"agent_session": "4f9a1c22-aaaa", "label": "Billing fixes", "vendor": "anthropic", "model": "claude-opus", "hostname": "Mac"}}
```

| Field | Notes |
|---|---|
| `agent_session` | The host's session id (`CLAUDE_CODE_SESSION_ID`, or `session_id` in a hook payload). Absent when there is none, for example under Codex. |
| `label` | A display name. From `--label` or `COMMS_LABEL`. |
| `vendor` | Who made the model, for example `anthropic` or `openai`. From `--vendor` or `COMMS_VENDOR`. |
| `model` | From `--model`, `COMMS_MODEL`, or the session-start payload. |
| `hostname` | The machine name. |
| `leader`, `base_name`, `tty` | Read for display. Written by older builds. |

comms writes a hello on its own the first time a session uses a name. That happens at session
start, on the first auto-claim, or on the first write under an auto-name.

### `claim`

```json
{"scope": ["src/auth.ts"], "data": {"intent": "rework the session cookie"}}
```

| Field | Notes |
|---|---|
| `intent` | One line on what the holder is about to do. |
| `agent_session` | The host session that made the claim. Absent on claims written before 0.5.0 and on calls made outside an agent session. |
| `auto` | `true` when the pre-edit hook made the claim on a first edit. Its intent is `auto: edited`. |
| `agent_id`, `agent_type` | On an auto-claim, copied from the hook payload when Claude Code sends them, which it does for a subagent. |
| `steals` | The id of the claim this one takes over. |
| `steal_reason` | Required with `steals`. |
| `arbitrator` | Who approved the steal, when given. |

A claim with several scopes in one call is written as one event per scope in one
append. Either all of them are recorded or none.

A steal is a single event. The fold ends the claim named in `steals` at the steal's
timestamp, so there is no moment when neither or both claims are active.

### `release`

```json
{"data": {"refs": ["01M7617ME73GTMTCYEYVYKKMJ4"], "result": "committed 4f2a9c1"}}
```

| Field | Notes |
|---|---|
| `refs` | Required. The claim ids this event ends. |
| `result` | How it went. comms writes these values itself: `session ended` (the session-end hook), `auto-claim: file is clean in git` (an auto-claim gave way), `auto-claim: superseded by a claim from the same session`, and `superseded by a new claim on the same ground`. |
| `reason` | Why, on a forced release or a roster change. |
| `original_actor` | The holder, when someone else ends the claim. |
| `arbitrator` | Who ended it, when that is not the holder. |
| `freed_from`, `via` | Written by the board's **Release** button. `via` is `board`. |

Roster changes are `release` events too:

- `session_retire: true` with `retired_actor` removes an actor from the active
  roster and ends the claims in `refs`.
- `leader_transfer: true` with `leader_actor` moves the leader mark to one actor.
- `comms_session_end: true` with `comms_session_id` ends a named work window and the
  claims in it. Without a `comms_session_id`, an older form, it ends every active claim.

### `note`

```json
{"data": {"body": "@claude-billing I need src/invoice.ts for the rounding fix, tell me when it is free"}}
```

`body` holds at most 1000 characters. A note that names an actor with `@name` is
shown to that actor once, after its next comms command. A session briefing also
lists notes addressed to the session from the last 24 hours.

### `blocked`

Records a claim that comms turned down.

```json
{"data": {"scope": "src/auth.ts", "intent": "take it", "holder": "claude-billing", "holder_scope": "src/auth.ts"}}
```

### Retired types

`finding`, `task`, `task_edge` and `task_state` were written by comms 0.5 and
earlier. They are read from old logs and skipped, and never written. The fold
ignores them and the `task` field on an old claim, so they change no claim and no
session. `comms log --type finding` or `--type task` still lists them.

## Identity

Every verb resolves the actor in this order:

1. `--as <name>`.
2. `COMMS_ACTOR`.
3. The session's own name in this repository's log, when `CLAUDE_CODE_SESSION_ID`
   is set. See [Session families](#session-families).
4. An auto-name, `<prefix>-<first 6 characters of the session id>`, lengthened
   when another session already uses that name. The prefix is
   `claude` when `COMMS_VENDOR` is `anthropic` or unset, `codex` when it is
   `openai`, and the vendor itself otherwise. comms records a hello the first time
   the auto-name writes.
5. Otherwise a write fails with a one-line error. A read runs without an actor.

The hooks ignore a generic `COMMS_ACTOR` and use the session's own name instead.

A leading `@` is dropped. A write under a generic name is refused unless
`COMMS_ALLOW_GENERIC_ACTOR=1`. The generic names include `claude`, `codex`,
`agent`, `user` and `me`.

### Session families

A session family is every actor whose hello carries the same `agent_session`.

The session's own name, used in step 3 above, is the first actor the session was
tied to. A later `hello --as` adds a name to the family and does not change it, so a
subagent cannot rename its parent.

Ownership depends on the path:

- **The edit check** (`hook pre-edit`, `check <path>`). A claim is the caller's when
  its `agent_session` equals the caller's session. A claim written without one takes
  the session its holder was tied to at that moment. If there was none, the claim is
  the caller's when its holder is in the caller's family. Two sessions that share
  a name therefore still block each other.
- **The command line** (`claim`, `release`, `status`). A claim is the caller's when
  it is held under the caller's name. A claim under another name conflicts, even
  from the same session, so parallel subagents with their own names keep off each
  other's files. The one exception is the session's own auto-claim, which a hand
  claim replaces.

### Liveness markers

The pre-edit and session-start hooks touch `<store>/alive/<agent session>` on every
call. Only the file's modification time changes, and nothing is appended to the log.
A holder's last sign of life is the later of its last event and the markers of its
sessions. `status` marks a claim STALE, and its holder LIKELY DEAD, when the claim
is older than the stale threshold (1 hour, set with `--stale-after`) and its holder
has been silent that long. A refused edit prints the exact `--steal` command once
the holder has been silent for an hour.

## Scope grammar

```
scope  := path ('#' anchor)?
path   := POSIX path relative to the repository root, optionally with * or **
anchor := L<n>-<m>       a line range, inclusive, 1 <= n <= m
        | <symbol>       an opaque symbol name
```

Write `\#` for a literal `#` in a file name.

Before storage and comparison, a scope is normalized: an absolute path or one that
leaves the repository is refused, backslashes become slashes, `.` and `..` segments
are resolved, and a leading `./` is dropped.

Two scopes overlap when both of these hold:

1. Their paths could match a common path. The comparison works segment by segment
   on the strings: `**` matches zero or more segments and `*` matches one. The disk
   is never read.
2. Their anchors overlap. Two line ranges overlap when they intersect. Two symbols
   overlap when they are equal. A line range and a symbol always overlap. A scope
   with no anchor overlaps every anchor.

## Storage

| Path | Contents |
|---|---|
| `<data home>/comms/<repo-hash>/log.jsonl` | The log. |
| `<data home>/comms/<repo-hash>/.lock` | The `flock` target for every write. |
| `<data home>/comms/<repo-hash>/repo-path.txt` | The repository path this store belongs to. |
| `<data home>/comms/<repo-hash>/alive/` | Liveness markers, one per agent session. |
| `<data home>/comms/<repo-hash>/seen/` | Which addressed notes each actor has been shown. |
| `<repo>/.comms/docs/*.md` | Pages written with `comms doc`. Committed with the repository. |

The data home is the first of these that applies:

1. `COMMS_HOME`, when it is an absolute path.
2. `~/Library/Application Support` on macOS.
3. `XDG_DATA_HOME`, when absolute, else `~/.local/share`, on Linux and the BSDs.

Any other platform is an error.

`<repo-hash>` is the first 12 hex characters of the SHA-256 of the coordination
root, with symbolic links resolved. The coordination root is the repository root,
or the main repository's root for a git worktree, so all worktrees share one store.
Moving or renaming a repository gives it a new store and leaves the old one behind.

A repository whose root is under the system temp folder or `/tmp` keeps its store
in `<temp>/comms-stores/<repo-hash>/` instead of the data home.

The repository root comes from `--root`, then a global `--repo`, then `COMMS_REPO`,
then the nearest directory above the current one that contains `.git`. An explicit
path is used as given and the current directory is never read.

## Concurrency

Every write takes an exclusive `flock` on the store's `.lock`, reads the log, checks
the rules, appends and releases. The kernel releases the lock when the process
exits, including on `kill -9`. Before appending, a writer cuts off an unterminated
last line left by a writer that crashed, so the new event does not fuse with it.
Reads take no lock.

## Hooks

Each hook reads Claude Code's hook JSON on stdin.

| Hook | Claude Code event | Exit codes |
|---|---|---|
| `hook session-start` | `SessionStart` | Always 0. Prints a briefing of at most 900 characters in a repository with a store, and nothing elsewhere. |
| `hook pre-edit` | `PreToolUse` for `Edit\|Write\|NotebookEdit` | 2 blocks the edit and the reason goes to stderr. 0 allows it. |
| `hook session-end` | `SessionEnd` | Always 0. Releases the auto-claims this session made, in every repository it edited, with result `session ended`. |

`hook pre-edit` reads `tool_input.file_path` or `tool_input.notebook_path`. It
exits 2 when:

- another session holds an overlapping claim and that claim does not give way,
- the payload is empty or not JSON, or
- the log exists but cannot be reached or read.

It exits 0 when the file is outside a repository, when the repository has no store,
and when the payload names no file. On an allowed first edit of a file the session
does not hold yet, it appends an auto-claim, taking the lock for at most one second.
It checks again under the lock: if another session claimed the file in the meantime,
the edit is blocked. If the lock cannot be taken or the write fails, the hook reads
the log once more without the lock: a claim another session recorded in the meantime
blocks, and otherwise the edit is allowed.

An auto-claim that blocks someone gives way when it is at least five minutes old and
its file is tracked with no change in `git status --porcelain`, in every worktree of
the repository. A worktree that cannot be asked keeps the claim. The release is
recorded with result `auto-claim: file is clean in git`.

`check --stdin-json` is the older name for `hook pre-edit` without the auto-claim,
with the same exit codes.

## CLI exit codes

| Code | Meaning |
|---|---|
| 0 | Done. |
| 1 | Refused: a claim conflict, a commit that `check --staged` would block, or a named scope with nothing to release. |
| 2 | A usage error, or an edit that `check` or `hook pre-edit` blocks. |

## Board HTTP API

`comms ui` serves the board on `127.0.0.1:7878` by default (`--host`, `--port`). Every
GET takes an optional `store=<repo-hash>` to pick a project. Without it, the board
shows the repository it was started in.

| Endpoint | Method | Returns |
|---|---|---|
| `/` | GET | The board page. |
| `/api/status` | GET | One project's snapshot as JSON. |
| `/events` | GET | Server-Sent Events. Each frame is a full snapshot plus a `stamp`. |
| `/api/history` | GET | One page of history: `{"events": [...], "next_before": "<id>"}`. Takes `actor`, `task` and `before` (an event id). At most 60 rows per page, newest first. |
| `/map.html` | GET | The code map page. |
| `/tasks.html` | GET | The task graph page. |
| `/api/release` | POST | Ends one claim held by someone else. |

`/api/status` has these top-level fields: `version` (the installed comms version),
`root`, `store_key`, `generated`, `board_actor`, `frontend_build`, `projects`,
`projects_hidden`, `claims`, `roster`, `tasks`, `task_edges`, `findings`, `notes`,
`blocked_events`, `feed`, `counts`, `dirty`, `guard`, `alerts`,
`code_map_available` and `map_age_seconds`. A snapshot that cannot read the log
returns only `error` and `generated`.

`/events` checks for a change every half second and sends a frame when the log or
the code map changes, or after 10 seconds without a change.

`/api/release` takes a JSON body:

```json
{"id": "01M7616MYDDPCQK2VWX32PYDPC", "reason": "holder gone since yesterday", "store": "8fa6cffde3bb", "actor": "human-you"}
```

`actor` defaults to the board's `COMMS_ACTOR`. The answer is 200 with
`{"ok": true, "released": ..., "was": ..., "scope": ...}`, or an error object with
403 (no actor), 400 (no id, no reason, or the caller's own claim) or 404 (no active
claim with that id). Any other POST path returns 404.

## Reading a damaged log

| Input | Behavior |
|---|---|
| Missing file | An empty log. |
| Blank line | Skipped. |
| Unterminated last line | Skipped with one warning on stderr. |
| Unknown event `type` | Skipped. One warning per read gives the count. |
| Duplicate event id | The first copy wins. Later copies are dropped. |
| Malformed JSON before the end, invalid UTF-8, a missing or zone-less `ts`, or a line over 1 MiB | The read fails with an error that names the line. The pre-edit hook then blocks edits in that repository. |
