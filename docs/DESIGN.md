# Design notes

## Why this shape

`comms` is the third generation of multi-agent coordination at DPA+. The first two failed in different ways; this design is the synthesis.

### What we kept

- **Concept of agent identity** (from `mcp-agent-mail`). Without it, conflict detection is impossible.
- **Markdown docs for shared knowledge** (from the `COMMS.md` era). Letting agents write to a wiki turned out to be valuable.
- **Append-only log** (from both). Easy to reason about, easy to recover, easy to grep.

### What we cut

- **Severity ladders, threaded inboxes, MCP server, registration tokens, deputies** — `mcp-agent-mail` had all of these. They added ceremony that agents kept skipping.
- **A single unbounded markdown file** — `COMMS.md` grew to 1632 lines, had no targeted reads, and iCloud forked it on concurrent writes.
- **Heartbeats and TTLs** — agents don't have a sense of time; claims would expire mid-session. We replaced this with "user arbitrates dead sessions" — see below.

## Key decisions

### Per-session actors, not per-user actors

If Claude, Codex, and the human shell all run as `$COMMS_ACTOR=eli`, `comms check` treats every other live agent's claim as "held by same actor" and waves through edits. The whole conflict system collapses.

So:

- `$COMMS_ACTOR` MUST be a concrete per-session identifier: `claude-3a1f`, `codex-9b2c`, `human-eli`.
- Generic names (`eli`, `claude`, `codex`, `agent`, `user`, `$USER`) are rejected by default. Override with `COMMS_ALLOW_GENERIC_ACTOR=1` for emergencies.
- We use shell wrappers (`cc`, `cdx`) that inject `COMMS_ACTOR=claude-<random>` per launch. The env var inherits through the whole process tree.
- Human shells use `COMMS_ACTOR=human-eli` directly — the human's identity doesn't rotate.

The hook approach (`SessionStart` does `export COMMS_ACTOR=...`) doesn't work because each hook fires in an isolated subshell — the export doesn't propagate to other tool subprocesses.

### No TTL, no heartbeat, no daemon

Agents don't have a clock — they can't send heartbeats reliably. Adding a timeout means an agent's claim expires mid-session, surprising it.

Instead:

- Claims are open-ended until explicitly released.
- When an agent hits a conflict, it surfaces to the user. The user verifies whether the other session is alive.
- If dead, the user authorizes `comms claim --steal <id> --reason "user verified prior session ended"`.

No background process, no cron, no clock-based expiry.

### Atomic steal as a single event

A two-event steal (release + claim) leaves a window where both claims are inactive. We encode it as **one** `claim` event with `data.steals=<old-id>` and `data.steal_reason="..."`. The reducer treats `steals` as "the referenced claim becomes inactive at THIS event's timestamp".

One log line, atomic, no race window.

### `flock(2)` for serialization

We need exactly-one-winner semantics for concurrent `claim` invocations. `flock(2)` is:

- POSIX-portable (works on macOS + Linux).
- Released by the kernel on FD close — including `kill -9`. No stale-lock cleanup needed.
- Simple Go API via `golang.org/x/sys/unix.Flock`.

The lock is per-repo (the log directory's `.lock` file). Every mutating command acquires-then-appends-then-releases.

Read-only `comms check` deliberately skips the lock — it reads the log without writing, so blocking on a long-running `claim` would defeat the point of being fast in the PreToolUse hook.

### JSONL, not SQLite

JSONL is grep-friendly, append-only-friendly, and easy to recover from corruption (skip a bad line, keep going). SQLite would give us indexed queries but at the cost of a more opaque on-disk format and a fork-bomb risk if iCloud touches the journal file.

A `comms compact` operation (future) can rotate or summarize old events without changing the on-disk shape.

### Log lives OUTSIDE iCloud, docs live INSIDE the repo

iCloud Drive forks files that are written concurrently — we got `log 2.jsonl`-style filename collisions in the `COMMS.md` era. To avoid that, the JSONL log lives at `~/Library/Application Support/comms/<repo-hash>/log.jsonl` (per-machine, outside iCloud).

But docs (`.comms/docs/*.md`) are committed and need to travel with the repo via git. They're rarely concurrent-written (only `comms doc --edit` writes them, and that's flocked).

### Segment-aware glob intersection (no FS expansion)

When two scopes are claimed, we need to know if they could match the same path. We can't FS-expand because:

- The files might not exist yet (someone's about to create them).
- It would be slow at scale.
- It would diverge across machines if the trees aren't identical.

So we compute glob ∩ glob purely as a string operation. `src/**` overlaps `src/foo.ts` (yes), `src/**` does NOT overlap `srcs/foo.ts` (different first segment).

### `comms doc` has three forms only

`--list`, `<slug>`, `<slug> --edit`. The `--edit` form takes a sidecar flock so two editors can't clobber each other. No `--diff`, `--history`, `--delete`, `--rename` in MVP — docs are plain Markdown in git, so use `git log .comms/docs/` for history and `git rm` for deletion.

## What's deliberately out of scope

- Cross-project view (per-repo is the design).
- Cross-machine sync (Eli works on one Mac for now).
- LSP integration for symbol anchors (string equality is good enough).
- Web UI (CLI + grep + `git log` are sufficient).
- Replacing this design is cheap if it doesn't work — the on-disk shape is JSONL + Markdown, both of which any tool can read.
