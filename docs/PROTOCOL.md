# Protocol reference

## Event log format

The log is JSONL — one event per line, append-only.

```jsonl
{"ts":"2026-05-22T14:30:00Z","id":"01HZ...","actor":"claude-dev","type":"hello","data":{"base_name":"claude","label":"Claude Dev","hostname":"dev-macbook","tty":"/dev/ttys003"}}
{"ts":"2026-05-22T14:32:01Z","id":"01HZ...","actor":"claude-3a1f","type":"claim","scope":["src/foo.ts#bar"],"data":{"intent":"fix N+1"}}
{"ts":"2026-05-22T14:45:00Z","id":"01HZ...","actor":"claude-3a1f","type":"finding","data":{"category":"fix","summary":"N+1 resolved","refs":[{"kind":"path","value":"src/foo.ts"},{"kind":"commit","value":"abc1234"}]}}
{"ts":"2026-05-22T14:46:00Z","id":"01HZ...","actor":"claude-3a1f","type":"release","data":{"refs":["01HZ..."],"result":"PR #321 merged"}}
```

Common fields:

| Field   | Type   | Notes                                                              |
| ------- | ------ | ------------------------------------------------------------------ |
| `ts`    | string | RFC3339 UTC, always normalized to UTC regardless of caller TZ.     |
| `id`    | string | ULID (26 chars, time-prefixed, monotonic).                         |
| `actor` | string | Validated COMMS_ACTOR. Per-session — never per-user.               |
| `type`  | string | One of: `hello`, `claim`, `release`, `note`, `finding`, `task`, `task_edge`, `task_state`. Readers skip types they do not know; writers never emit one. |
| `scope` | array  | Optional. Only set on `claim` (and informational on `release`).    |
| `data`  | object | Type-specific bag; all keys optional unless noted below.           |

## Event types

### `hello`
```json
{"data": {"base_name": "claude", "label": "Claude Dev", "hostname": "dev-macbook", "tty": "/dev/ttys003"}}
```
Best-effort metadata; all fields may be empty. `label` is a friendly display
name for status/UI only. The stable identity remains `actor`.

Named comms sessions are created/joined by appending normal `hello` events with
extra metadata:

```json
{"data": {"base_name": "claude", "label": "Claude Dev", "hostname": "MacBook-Pro.local", "comms_session_start": true, "comms_session_id": "01HZ...", "comms_session_name": "dashboard fixes"}}
```

```json
{"data": {"base_name": "codex", "label": "Codex Dev", "hostname": "MacBook-Pro.local", "comms_session_join": true, "comms_session_id": "01HZ...", "comms_session_name": "dashboard fixes"}}
```

After an actor joins a named session, that actor's `claim`, `release`, `note`,
and `finding` events are stamped with the same `comms_session_id` and
`comms_session_name`. Untagged legacy events are still supported and are shown
as a legacy/current window by the UI.

An actor may be active in only one named session at a time. When the same actor
starts or joins a different named session, comms first appends a release/retire
audit event for that actor's prior-session claims, then appends the new `hello`.
This keeps old claims and old session membership from following the actor into
the new session.

### `claim`
```json
{
  "scope": ["src/foo.ts#bar"],
  "data": {"intent": "fix N+1"}
}
```
On an arbitrated steal, additional fields appear:
```json
{
  "scope": ["src/foo.ts"],
  "data": {
    "intent": "alice gone",
    "steals": "01HZK4...",
    "steal_reason": "alice's session ended per eli",
    "arbitrator": "eli"
  }
}
```
The reducer interprets `steals` as "the referenced claim becomes inactive at THIS event's timestamp". Single atomic event — no separate release record.

### `release`
```json
{"data": {"refs": ["<claim-id>"], "result": "PR #321 merged"}}
```
Arbitrated release (a different actor closing someone else's claim) MUST include `reason`:
```json
{
  "actor": "bob",
  "data": {
    "refs": ["<their-id>"],
    "reason": "session ended",
    "original_actor": "alice",
    "arbitrator": "eli"
  }
}
```

The UI's **End Comms Session** button appends a normal `release` event with
session-boundary metadata. For a named session it releases only claims tagged
with that `comms_session_id` and removes only actors currently joined to it:

```json
{
  "data": {
    "refs": ["<claim-id-in-that-session>"],
    "comms_session_end": true,
    "comms_session_id": "01HZ...",
    "comms_session_name": "dashboard fixes",
    "ended_actors": ["claude-3a1f", "codex-9b2c"],
    "reason": "project work session done"
  }
}
```

For old untagged sessions, `comms_session_end=true` without a
`comms_session_id` keeps the legacy behavior: all active claims are released and
all active sessions are cleared. The physical `log.jsonl` remains one
append-only file either way.

Session roster admin is also encoded as normal `release` events. Retiring an
actor removes it from active sessions and releases any claim refs listed, but
does not delete historical rows:

```json
{
  "actor": "claude-dev",
  "data": {
    "refs": ["<claim-held-by-old-actor>"],
    "session_retire": true,
    "retired_actor": "claude-7e4c",
    "reason": "renamed to claude-dev"
  }
}
```

Leader transfer is append-only too. The reducer clears every active session's
leader flag, then sets exactly one target actor:

```json
{
  "actor": "human-eli",
  "data": {
    "leader_transfer": true,
    "leader_actor": "claude-dev",
    "reason": "user asked Claude Dev to lead"
  }
}
```

### `note`
```json
{"data": {"body": "FYI iCloud delete-loop on charts/"}}
```
Body is ≤200 Unicode runes (scalar values).

### `finding`
```json
{
  "data": {
    "category": "fix",
    "summary": "leads sourced only from tracker overlay",
    "refs": [
      {"kind": "path", "value": "frontend/src/lib/aggregate.ts"},
      {"kind": "commit", "value": "cece752"}
    ]
  }
}
```
`category` is one of: `bug`, `fix`, `ship`, `decision`, `gotcha`.

`refs[].kind` is free-form but conventional: `path`, `commit`, `pr`, `issue`, `doc`, `url`.

### `task`

Declares a node of the work graph, or restates one that exists. A restatement
edits the description only — re-titling verified work does not reopen it.

```json
{"data": {"task": "auth-api", "title": "Build session create / refresh / revoke",
          "size": "L", "slots": 2, "checks": ["test", "types"], "ref": "tracker:PROJ-1234"}}
```

| Field    | Notes |
| -------- | ----- |
| `task`   | Slug: 2-32 chars, `[a-z][a-z0-9-]*`. Chosen to be SAID — quoted between agents, typed into a claim. Deliberately not a ULID: on a real store 99.5% of ULIDs share their 6-char short prefix. |
| `title`  | Written as an instruction, not a label. Required on first declaration. |
| `size`   | `S`, `M` or `L`. |
| `slots`  | How many agents may work it at once. Default 1. |
| `checks` | Names that must be reported passing before the task can be marked done. |
| `ref`    | Opaque reference to wherever the real context lives, e.g. `tracker:PROJ-1234`. comms stores it and never resolves it. |

### `task_edge`

Records that `to` comes after `from`, and what `to` consumes from `from`.

```json
{"data": {"from": "auth-api", "to": "login-ui", "kind": "interface",
          "provides": "POST /session, POST /session/refresh - httpOnly cookie, no bearer token"}}
```

`kind` is one of `interface` (B calls a surface A provides), `artifact` (B uses a
file or schema A produced) or `sequence` (ordering only, nothing flows).
Unrecognized kinds degrade to `sequence`.

The distinction decides what a rejection costs: reworking a task forces a
recheck of successors that CONSUME from it, and leaves successors that merely
follow it alone. `provides` is what travels to whoever picks up `to`.

Edges are their own events so any agent can add one at the moment it discovers
the dependency — the only moment it is actually known. An edge whose endpoints
are not both declared is ignored by the reducer; `comms plan` and `comms task
edge` reject it up front.

### `task_state`

Moves a task along its two steps.

```json
{"data": {"task": "auth-api", "state": "done",
          "checks": {"test": "pass", "types": "pass"},
          "notes": ["Refresh rotation is single-use: replaying a spent token revokes the family."]}}
```

```json
{"data": {"task": "auth-api", "state": "rejected", "findings": [
  {"claim": "revoke will table-scan", "evidence": "EXPLAIN shows Seq Scan on refresh_tokens"}]}}
```

`state` is `done`, `verified` or `rejected`. The reducer REFUSES a transition
rather than trusting the writer, and keeps the refusal:

- `done` — refused unless every declared check is reported `pass`.
- `verified` / `rejected` — refused when the actor is the agent who did the work.
  A role suffix does not help: `claude-dev/review` is recognized as `claude-dev`.
- `rejected` clears the doer and returns the task to open work. The graph is not
  redrawn; the findings travel with it.

`notes` are the handover: the decisions the doer made and why. They are what a
verifier reads, and what `comms brief` passes along the edges to whoever picks up
the tasks that come after.

### Derived task state

Nothing below is ever written into an event. All of it is computed by replaying
the log, so the picture cannot contradict the data:

| Phase    | Meaning |
| -------- | ------- |
| `ready`  | Every dependency is verified and nobody has claimed it. |
| `doing`  | At least one agent holds a file claim tagged to this task. |
| `review` | Finished, waiting for a second pair of eyes. |
| `closed` | Verified. **Only now does it unblock its successors.** |
| `blocked`| At least one dependency is not verified yet. |
| `cycle`  | Reachable from itself. Surfaced rather than hidden; the reducer still terminates. |

A task unblocks what comes after it when it is VERIFIED, not when it is merely
finished. That is what keeps review on the critical path.

Who is on a task is read off live claims tagged with `data.task`, so releasing a
file takes the agent off the task with no separate bookkeeping step.

A verification also records whether it was `independent` (the verifier's `vendor`
differs from the doer's, taken from their `hello` events) or `same-family`.
"Verified" and "verified by something with the same blind spots" are different
claims and the protocol does not blur them.

## Scope grammar

```
scope  := path ('#' anchor)?
path   := POSIX path, optionally globbed with * or **
anchor := L<n>-<m>          (line range, inclusive, n ≤ m, both ≥ 1)
        | <symbol-name>      (NFC-normalized opaque identifier)
```

Use `\#` to escape a literal `#` in a filename.

### Path normalization

Before storage or comparison, scopes are normalized:

1. Reject absolute paths (`/etc/passwd`).
2. Reject paths that normalize outside the repo root (`../escape`).
3. Convert backslashes to forward slashes.
4. `filepath.Clean` to collapse `.` and `..`.
5. Strip leading `./`.

Canonical form is POSIX, repo-relative, no `.` or `..` segments.

### Overlap detection

Two scopes overlap if and only if BOTH:

1. Their **path globs** could match a common path — segment-aware string intersection. `**` matches zero or more segments; `*` matches exactly one.
2. Their **anchors** overlap, per:
   - Both line ranges → numeric intersection (closed intervals).
   - Both symbols → case-sensitive equality.
   - Mixed (line + symbol) → pessimistic overlap.
   - Either whole-file (no `#` anchor) → always overlap.

The path overlap is computed purely as a string operation — `comms` never globs against the real filesystem.

## Repo identity

`<repo-hash>` = first 12 hex chars of `sha256(filepath.EvalSymlinks(git rev-parse --show-toplevel))`.

If cwd discovery fails, pass `--repo /absolute/repo/path` or set
`COMMS_REPO=/absolute/repo/path`. Explicit repo paths are resolved by walking up
to `.git` without calling `os.Getwd()` or spawning `git`, so they keep working
when a desktop app process hits a macOS TCC/getcwd failure on a protected
Desktop/Documents/Downloads path. Two repos that resolve to the same path get
the same hash; renaming or moving a repo creates a new hash and orphans the old
log.

## Concurrency

Every mutating command acquires an exclusive `flock(2)` on `<logdir>/.lock` before reading the log + appending. The lock releases when the process exits — including `kill -9`. We never spawn child processes while holding the lock (since the child would inherit the FD and deadlock).

## UI API

`comms ui` serves a read-mostly local dashboard over HTTP. The backend exposes:

By default `comms ui` serves a unified view across all repo log directories under
the comms data directory (scope to one with `--repo`). It does not merge files on
disk; it prefixes project/session labels in the response.

| Endpoint                   | Method | Purpose                                                           |
| -------------------------- | ------ | ----------------------------------------------------------------- |
| `/api/status`              | GET    | Current project snapshot, active state, archives, continuous event history, and action metadata. |
| `/api/comms-session/start` | POST   | Body `name`; append a `hello` event with `comms_session_start=true` and a new named session ID. Requires `COMMS_ACTOR`. |
| `/api/comms-session/end`   | POST   | Body `session_id` or `name`; append a `release` event with `comms_session_end=true` for that named session. Requires `COMMS_ACTOR`. |
| `/api/claim/release`       | POST   | Append a normal `release` event for one active claim. Body: `claim_id`, optional `result`/`reason`. Requires `COMMS_ACTOR`. |
| `/api/claim/release-all`   | POST   | Release every active claim held by one actor: one `release` event per claim, appended as a single batch under one lock hold. Body: `actor`, optional `repo_hash`/`result`/`reason`. Requires `COMMS_ACTOR`. |
| `/api/session/retire`      | POST   | Append a `release` event with `session_retire=true`; releases that actor's claims. Requires `COMMS_ACTOR`. |
| `/api/session/lead`        | POST   | Append a `release` event with `leader_transfer=true`. Requires `COMMS_ACTOR`. |

`/api/status` includes an `actions` array so agents or UI clients can discover
what the backend currently allows:

```json
{
  "actions": [
    {"id": "start_comms_session", "label": "Start Comms Session", "method": "POST", "path": "/api/comms-session/start", "enabled": true},
    {"id": "end_comms_session", "label": "End Comms Session", "method": "POST", "path": "/api/comms-session/end", "enabled": false, "reason": "no active comms session to end"},
    {"id": "release_claim", "label": "Release Claim", "method": "POST", "path": "/api/claim/release", "enabled": true},
    {"id": "release_actor_claims", "label": "Release All Claims", "method": "POST", "path": "/api/claim/release-all", "enabled": true},
    {"id": "retire_session_actor", "label": "Retire Session Actor", "method": "POST", "path": "/api/session/retire", "enabled": true},
    {"id": "transfer_leader", "label": "Transfer Leader", "method": "POST", "path": "/api/session/lead", "enabled": true},
    {"id": "select_session_log", "label": "View Continuous History", "enabled": true}
  ]
}
```

Top-level `events` is the canonical dashboard history. In single-repo mode it
contains every event from that repository's append-only JSONL, including events
from inactive, unended, archived, and legacy sessions. In unified mode it merges
all valid repository histories newest-first. Every row includes `repo_hash`,
`repo_name`, `session_id`, and `session_name` when available, so filtering never
loses repository-qualified identity.

Session views (`current_session`, `active_comms_sessions[]`, `comms_sessions[]`,
and their per-project equivalents under `project_sessions[]`) carry their
summary counts — `event_count`, `claim_count`, `finding_count`, `note_count` —
but no longer carry an `events` array. Since History became one continuous list,
a per-session copy was a duplicate of rows already present at the top level and
roughly a fifth of the payload. Selecting a project or typing in the event filter
only filters the canonical top-level array; no JSONL file is merged, rewritten,
or truncated.

### Incremental history over SSE

`/api/status` — and the first frame a newly connected `/api/events` client
receives — always carry the complete history. Every later SSE frame carries only
the rows appended since the previous frame and sets `events_delta: true`; the
client merges them by event `id` into the log it already holds, so its filter
still runs over every row. This keeps a push proportional to what changed rather
than to the size of the whole append-only log.

| Field           | Type | Notes                                                                              |
| --------------- | ---- | ---------------------------------------------------------------------------------- |
| `events_total`  | int  | Total history rows the snapshot covers, including rows a delta frame trimmed.      |
| `events_delta`  | bool | Present and `true` only on a trimmed frame. Absent means `events` is complete.     |

The delta boundary is inclusive: rows sharing the watermark timestamp are
repeated rather than risked, since the client merges by `id`. Frame delivery
coalesces — a client that has not drained the previous frame has it replaced —
so `events_total` is the recovery mechanism: a client holding fewer rows than
`events_total` refetches `/api/status` once and is whole again.

`/api/status` also includes `task_board`: the work graph for the selected
project, already laid out. Nodes carry `x`/`y`/`w`/`h` and edges carry a `d` path
string, so the page draws rather than computes. It is populated per project —
the all-projects view has none, because a dependency between two repositories is
not something comms models. `too_large` is set instead of a layout once a graph
passes the point where a picture stops being comprehension.

`/api/status` also includes `lessons`, the list of global lesson slugs loaded
from the user's comms data directory.

## Global lessons

`comms lesson` is the global counterpart to project-local `comms doc`.

| Command                      | Purpose                                      |
| ---------------------------- | -------------------------------------------- |
| `comms lesson --list`        | List curated global lesson slugs.            |
| `comms lesson <slug>`        | Print one lesson.                            |
| `comms lesson <slug> --edit` | Edit/create one lesson under a sidecar flock. |

Storage:

```text
~/Library/Application Support/comms/global/lessons/*.md
```

Lessons are not JSONL events and are not tied to a repo hash. They are
cross-project operating knowledge for agents, so they should be added rarely:
only when the user explicitly asks or approves a leader's proposed lesson.

## Recovery rules for `comms` reading the log

| Input                                  | Behavior                                                                  |
| -------------------------------------- | ------------------------------------------------------------------------- |
| Missing file                           | Treat as empty log; no error.                                             |
| Blank lines (zero bytes or whitespace) | Silently skipped.                                                         |
| Trailing unterminated final line       | Stderr warning, skipped; subsequent reads succeed.                        |
| Unrecognized event `type`              | Skipped; one stderr warning per read naming the count. A newer `comms` wrote it. |
| Malformed JSON before EOF              | Exit 2 (`ErrCorrupt`). Pre-EOF corruption is treated as unrecoverable.    |
| Invalid UTF-8                          | Exit 2.                                                                   |
| Line > 1 MiB                           | Exit 2 (defensive ceiling).                                               |
| Duplicate event ID                     | First occurrence wins; later duplicates dropped silently (iCloud canary). |
