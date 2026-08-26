# Design notes

## Why this shape

`comms` is the third generation of multi-agent coordination at DPA+. The first two failed in different ways; this design is the synthesis.

### What we kept

- **Concept of agent identity** (from `mcp-agent-mail`). Without it, conflict detection is impossible.
- **Markdown docs for shared knowledge** (from the `COMMS.md` era). Letting agents write to a wiki turned out to be valuable.
- **Append-only log** (from both). Easy to reason about, easy to recover, easy to grep.

### What we cut

- **Severity ladders, threaded inboxes, registration tokens, deputies**: `mcp-agent-mail` had all of these. They added ceremony that agents kept skipping.
- **An MCP server**: cut for the same reason, and later *reversed*. See below.
- **A single unbounded markdown file**: `COMMS.md` grew to 1632 lines, had no targeted reads, and iCloud forked it on concurrent writes.
- **Heartbeats and TTLs**: agents don't have a sense of time, so claims would expire mid-session. We replaced this with "user arbitrates dead sessions" (see below).

### What we cut and then brought back

**The MCP server.** It was cut with the rest of `mcp-agent-mail`'s apparatus, on
the grounds that it was ceremony agents skipped. The ceremony judgement was
right. The transport judgement was wrong, and the log says so: across 4,356 real
claims exactly **one** carried a task. Not because the task graph was broken:
because the instruction to use it lived in a skill that forbids auto-triggering
and only loads when a human types its name. A CLI an agent has to be told to run
loses to a tool sitting in its tool list every turn.

`comms mcp` is therefore the same protocol with none of what was actually
objectionable: six verbs mapping one-to-one onto existing commands, no
registration, no inbox, no severity ladder. It speaks JSON-RPC over stdio with no
SDK, because the subset a tool server needs is small and stable and comms would
rather own 300 lines than carry a dependency.

One thing had to change underneath it. `COMMS_ACTOR` is per process, and an MCP
server is one long-lived process that may act for several agents, so every tool
takes an `actor` argument and `Open` accepts a per-request override. The
environment variable remains the default.

## Key decisions

### Per-session actors, not per-user actors

If Claude, Codex, and the human shell all run as `$COMMS_ACTOR=eli`, `comms check` treats every other live agent's claim as "held by same actor" and waves through edits. The whole conflict system collapses.

So:

- `$COMMS_ACTOR` MUST be a concrete per-session identifier: `claude-3a1f`, `codex-9b2c`, `human-eli`.
- Generic names (`eli`, `claude`, `codex`, `agent`, `user`, `$USER`) are rejected by default. Override with `COMMS_ALLOW_GENERIC_ACTOR=1` for emergencies.
- We use shell wrappers (`cc`, `cdx`) that inject `COMMS_ACTOR=claude-<random>` per launch. The env var inherits through the whole process tree.
- Human shells use `COMMS_ACTOR=human-eli` directly: the human's identity doesn't rotate.

The hook approach (`SessionStart` does `export COMMS_ACTOR=...`) doesn't work because each hook fires in an isolated subshell: the export doesn't propagate to other tool subprocesses.

### No TTL, no heartbeat, no daemon

Agents don't have a clock: they can't send heartbeats reliably. Adding a timeout means an agent's claim expires mid-session, surprising it.

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
- Released by the kernel on FD close: including `kill -9`. No stale-lock cleanup needed.
- Simple Go API via `golang.org/x/sys/unix.Flock`.

The lock is per-repo (the log directory's `.lock` file). Every mutating command acquires-then-appends-then-releases.

Read-only `comms check` deliberately skips the lock: it reads the log without writing, so blocking on a long-running `claim` would defeat the point of being fast in the PreToolUse hook.

### JSONL, not SQLite

JSONL is grep-friendly, append-only-friendly, and easy to recover from corruption (skip a bad line, keep going). SQLite would give us indexed queries but at the cost of a more opaque on-disk format and a fork-bomb risk if iCloud touches the journal file.

A `comms compact` operation (future) can rotate or summarize old events without changing the on-disk shape.

### Log lives OUTSIDE iCloud, docs live INSIDE the repo

iCloud Drive forks files that are written concurrently: we got `log 2.jsonl`-style filename collisions in the `COMMS.md` era. To avoid that, the JSONL log lives at `~/Library/Application Support/comms/<repo-hash>/log.jsonl` (per-machine, outside iCloud).

But docs (`.comms/docs/*.md`) are committed and need to travel with the repo via git. They're rarely concurrent-written (only `comms doc --edit` writes them, and that's flocked).

### Segment-aware glob intersection (no FS expansion)

When two scopes are claimed, we need to know if they could match the same path. We can't FS-expand because:

- The files might not exist yet (someone's about to create them).
- It would be slow at scale.
- It would diverge across machines if the trees aren't identical.

So we compute glob ∩ glob purely as a string operation. `src/**` overlaps `src/foo.ts` (yes), `src/**` does NOT overlap `srcs/foo.ts` (different first segment).

### `comms doc` has three forms only

`--list`, `<slug>`, `<slug> --edit`. The `--edit` form takes a sidecar flock so two editors can't clobber each other. No `--diff`, `--history`, `--delete`, `--rename` in MVP: docs are plain Markdown in git, so use `git log .comms/docs/` for history and `git rm` for deletion.

## What's deliberately out of scope

- Cross-project view (per-repo is the design).
- Cross-machine sync (Eli works on one Mac for now).
- LSP integration for symbol anchors (string equality is good enough).
- Web UI (CLI + grep + `git log` are sufficient).
- Replacing this design is cheap if it doesn't work: the on-disk shape is JSONL + Markdown, both of which any tool can read.

---

## Where comms came from

This is the third generation of multi-agent coordination at DPA+. The first was
a single 1,632-line `COMMS.md` markdown file. It worked, but grew without bound,
had no targeted reads, relied on agents remembering to update it, and iCloud
sync kept forking the file. The second was `mcp-agent-mail`, a heavy MCP server
with severity ladders and seven identities: too much ceremony, and agents kept
forgetting the protocol. comms is the small version that learned from both.

## Why comms reads git, and not only its own log

Every guard here used to ask one question, *is this path claimed by somebody
else*, and report "no" as safety. It is not. "Nobody has declared anything" and
"nothing is happening" are different facts. Four measured incidents, all failing
in the reassuring direction:

1. The board printed "no active claims in this repo" while `git status` showed
   fifteen changed files. Four of those paths appear zero times in the log.
2. `check --staged` passed a commit carrying a staged deletion left behind by a
   departed agent. The check was true on its own terms, the file was unclaimed,
   and the deletion shipped.
3. An agent committed a translation file while another agent **held the claim on
   it**, and seventeen of the holder's in-flight keys shipped inside somebody
   else's commit.
4. One agent ran zero comms commands after a context compaction and edited
   heavily anyway. In its own words: *a rule that only exists as prose decays
   exactly this way.*

Two of those look identical and are not. (2) was a **detection** failure: the
check asked the wrong question, and reading `git status` fixes it. (3) was not.
`check --staged` would have refused it by name, with the holder's claim id in the
message. That is an **enforcement** failure, and no amount of better checking
closes it, which is why `guard install` exists.

The rule that falls out, and it generalises past this tool: prefer the signal
nobody has to remember to emit, and never render "we could not establish
anything" as "all clear".

## What the task tag buys

A claim says who is on a file. A task says what the work is, and tagging claims
to it with `--task <slug>` is what lets the board answer "which files did this
touch" and "whose work sits next to mine". It costs one flag on a command you
were running anyway, and it is the only bookkeeping in the tool that is not
derived for you.

When it was phrased as a suggestion, agents skipped it: across 4,356 real claims
exactly one carried a task. It is a rule in the skill now, and stated as one.

## Task titles are written for people

`--title` is the only line about a task that somebody outside the code ever
reads. It is plain English, in English, with no file names, function names or
jargon, and the technical detail goes in the notes, the checks and the files.
"Round money the same way everywhere", not "money-rounding refactor in toCents".
