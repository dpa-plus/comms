# Protocol reference

## Event log format

The log is JSONL — one event per line, append-only.

```jsonl
{"ts":"2026-05-22T14:30:00Z","id":"01HZ...","actor":"claude-3a1f","type":"hello","data":{"base_name":"claude","hostname":"dev-macbook","tty":"/dev/ttys003"}}
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
| `type`  | string | One of: `hello`, `claim`, `release`, `note`, `finding`.            |
| `scope` | array  | Optional. Only set on `claim` (and informational on `release`).    |
| `data`  | object | Type-specific bag; all keys optional unless noted below.           |

## Event types

### `hello`
```json
{"data": {"base_name": "claude", "hostname": "dev-macbook", "tty": "/dev/ttys003"}}
```
Best-effort metadata; all fields may be empty.

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

If `git rev-parse` fails (not a git repo, no git installed), pass `--repo-id <hash>` explicitly to override. Two repos that resolve to the same path get the same hash; renaming or moving a repo creates a new hash and orphans the old log.

## Concurrency

Every mutating command acquires an exclusive `flock(2)` on `<logdir>/.lock` before reading the log + appending. The lock releases when the process exits — including `kill -9`. We never spawn child processes while holding the lock (since the child would inherit the FD and deadlock).

## Recovery rules for `comms` reading the log

| Input                                  | Behavior                                                                  |
| -------------------------------------- | ------------------------------------------------------------------------- |
| Missing file                           | Treat as empty log; no error.                                             |
| Blank lines (zero bytes or whitespace) | Silently skipped.                                                         |
| Trailing unterminated final line       | Stderr warning, skipped; subsequent reads succeed.                        |
| Malformed JSON before EOF              | Exit 2 (`ErrCorrupt`). Pre-EOF corruption is treated as unrecoverable.    |
| Invalid UTF-8                          | Exit 2.                                                                   |
| Line > 1 MiB                           | Exit 2 (defensive ceiling).                                               |
| Duplicate event ID                     | First occurrence wins; later duplicates dropped silently (iCloud canary). |
