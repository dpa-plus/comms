<div align="center">

<img src="assets/logo.svg" alt="comms logo" width="128" height="128">

# comms

**Coordinate parallel coding agents through one shared, append-only log.**

[![CI](https://github.com/dpa-plus/comms/actions/workflows/ci.yml/badge.svg)](https://github.com/dpa-plus/comms/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/dpa-plus/comms?include_prereleases&logo=github&color=0f766e)](https://github.com/dpa-plus/comms/releases)
[![Go Reference](https://pkg.go.dev/badge/github.com/dpa-plus/comms.svg)](https://pkg.go.dev/github.com/dpa-plus/comms)
[![License](https://img.shields.io/badge/license-MIT-0f766e)](LICENSE)
[![Go](https://img.shields.io/badge/Go-1.25+-0f766e?logo=go&logoColor=white)](go.mod)
[![No daemon](https://img.shields.io/badge/runtime-a%20file%20on%20disk-0f766e)](#how-it-actually-runs-on-your-machine)
[![No daemon](https://img.shields.io/badge/no-daemon%20%C2%B7%20no%20server-0f766e)](#how-it-actually-runs-on-your-machine)

</div>

> **comms is a tiny command-line tool that lets several AI coding agents — and you — work in the same repository at the same time without stepping on each other.**

<p align="center">
  <img src="assets/dashboard.png" alt="The comms dashboard: team roster, active claims, findings, notes, and one continuous persistent history." width="900">
</p>

<p align="center"><em>The board (<code>comms-graph ui</code>): what just happened, who is holding which files right now, the tasks with the files each one touches, and every project on the machine down the left. Everything on it is read out of the append-only log.</em></p>

---

## The problem it solves

Modern coding often means running **more than one AI agent on the same codebase at once** — say Claude in one window and Codex in another, plus you. That's powerful, but they're effectively three people editing the same project with no shared awareness:

- 🥊 **They collide.** Two agents edit the same file and overwrite each other's work.
- 🔁 **They repeat each other.** One agent fixes a bug the other already fixed.
- 🧠 **They forget context.** A decision ("the tracker is the source of truth for leads") lives in one agent's head and is lost to the others.
- 🌫️ **You can't see what's happening.** Who's working on what, right now?

These are the classic problems of people working in parallel — and the classic answer is to **write things down in one shared place.** comms is that shared place, built for agents: small enough that they actually use it, with a live view so *you* can watch.

> This is the **third generation** of multi-agent coordination at DPA+. The first was a single 1,632-line `COMMS.md` markdown file — it worked, but grew without bound, had no targeted reads, relied on agents remembering to update it, and iCloud sync kept forking the file. The second was `mcp-agent-mail` — a heavy MCP server with severity ladders and seven identities; too much ceremony, and agents kept forgetting the protocol. **comms is the small version that learned from both.**

---

## What comms gives you

| Primitive | What it's for | Example |
|---|---|---|
| **Claim** | "I'm working on this file — hands off." | `comms claim "src/auth.ts" --intent "fix JWT expiry"` |
| **Finding** | A durable fact: a bug, a fix, a decision, a gotcha, a release. | `comms find decision "tracker is source of truth for leads"` |
| **Note** | A short, throwaway heads-up. | `comms note "FYI: schema migration coming next"` |
| **Session** | A named work window that groups claims/events and can be archived. | `comms session start "dashboard fixes"` |
| **Doc** | A small per-repo wiki under `.comms/docs`. | `comms doc tracker-architecture` |
| **Lesson** | Cross-project knowledge that outlives any one repo. | `comms lesson verify-data-before-ui` |

Before an agent edits a file it **claims** it. Before it forgets a decision it records a **finding**. Another agent (or you) runs `comms status` and instantly sees the whole picture. The first active participant becomes a lightweight **leader** whose only extra power is pinning `--priority` notes to the top.

---

## How it actually runs on your machine

The most important thing to understand: **comms is not a server.** There's no daemon, no background process, no network service, no database to install. It's a command-line tool on your `PATH` that appends to a file.

There are two builds of it and they share one log, so you can run either: the Go binary (`comms`), and the Python build (`comms-graph`), which adds the task graph and the code map. See [Two implementations, one log](#two-implementations-one-log).

**Everything is files.** State lives in two places:

```
your-repo/.comms/                         ← committed to git (shared design)
  ├─ policy.txt                            ← optional rules (which paths need claims)
  └─ docs/                                 ← the per-repo wiki

~/Library/Application Support/comms/<repo-hash>/   ← per-machine, NOT in git
  ├─ log.jsonl                             ← the append-only event log (the heart)
  └─ .lock                                 ← a file lock that serializes writes
```

> The per-machine log lives outside iCloud on purpose — iCloud Drive forks files that two processes append to at the same time, which would corrupt the log.

**Every command is the same tiny dance.** When an agent runs `comms claim …`:

1. The binary **starts**, figures out which repo you're in, and finds that repo's `log.jsonl`.
2. It grabs the **file lock** (so two agents can't write at the same instant).
3. It **appends one line** — a JSON event — to the end of the log.
4. It **releases the lock and exits.**

That's it. A `comms` command is a short-lived program that opens a file, appends a line, and quits. Reading state (`comms status`) just replays the log — no lock needed.

The dashboard reads those same files into **one continuous persistent history**.
Selecting a project filters the timeline without changing or hiding its stored
events, and inactive, unended, and archived sessions remain visible. Project
and session names are retained on every row as context, not separate log stores.

```mermaid
flowchart LR
    A["Claude<br/>(comms claim …)"] -- append --> L[("log.jsonl<br/>append-only<br/>+ file lock")]
    B["Codex<br/>(comms find …)"] -- append --> L
    C["You<br/>(comms note …)"] -- append --> L
    L -- replay/read --> D["comms status"]
    L -- watched live --> U["comms ui<br/>(dashboard)"]
```

---

## How the agents actually communicate

Here's the key idea: **the agents never talk to each other directly.** There's no chat, no messages flying between them, no network connection. They communicate the way a team uses a shared whiteboard:

- **Agent A writes to the board.** `comms claim "aggregate.ts"` appends a *claim* event to the log.
- **Agent B reads the board.** `comms status` replays the log and sees A's claim — so B knows to work elsewhere.
- **Conflicts are caught by reading, not messaging.** If B tries to claim a file that overlaps A's claim, comms sees the overlap in the log and tells B to back off.
- **Stale claims can be taken over.** A claim goes **stale after 1 hour idle** — its holder is presumed gone. The `BLOCKED` message says so, and B can steal a stale claim directly (`comms claim … --steal <id>`, no `--reason`). A still-active claim (held < 1h) requires the user's confirmation and a `--reason` to steal.

The log is the single source of truth. It's **append-only**, so history is never rewritten — you can always see exactly who did what and when. This "shared ledger" model is what makes coordination reliable without any of the agents needing to know the others exist. They only need to know about **the log**.

The dashboard (`comms ui`) is simply a **live read-only view** of that same log.

---

## The live dashboard

```bash
COMMS_ACTOR=human-you comms-graph ui   # http://127.0.0.1:7878 — every project, one tab
```

It opens on **what just happened**, newest first, with the things that need
somebody pulled to the top. Around it: who is holding which files right now, the
roster of who is actually here, the tasks with the files each one touches, and
every project on this machine down the left side.

**It reads. It does not write** — with exactly one exception. The log is appended
under a lock, through a fold that enforces the rules, and a dashboard writing
around either would be a second writer with none of those guarantees. So the only
button that changes anything is **Release**, which frees a claim somebody else is
holding, and it goes through the same lock and appends the same event the CLI
does. It asks for a reason and refuses without one, because the release is
recorded under your name permanently and "who freed this and why" is the only
question anybody asks afterwards.

> **Start it with `COMMS_ACTOR` set** (e.g. `human-you`). Without an actor the
> board refuses to release at all: a release with no author is worse than no
> release, because the ground is gone and the log cannot say who took it.

It is **unified by default**: one window for every comms project on the machine. The **Projects** rail lists them and clicking one scopes the whole view. It lists real projects only — a store whose directory has been deleted, or which lives in a temp folder, is not a project, and before that filter existed two real projects sat among 213 that were not.

The **Roster** shows who is here, meaning the last hour, plus anyone holding a claim whatever their age — a stale claim is the one thing on the board that needs a person, and hiding its holder would hide the only name that can free it. Agents take a fresh name each session, so everyone who has ever said hello is a much longer and much less useful list; it is one click away.

Run it **once** and watch every repo. Agents never open anything — they write to their logs, which this board already sees.

The **work graph** shows the tasks in the selected project: an arrow means the
task it points at comes afterwards, and tasks joined to nothing sit apart from
the rest, because they are. Finished work compresses to a dashed outline so the
board shrinks as the project progresses; work waiting for a verifier is the
loudest thing on it, because it is finished *and* holding up everything
downstream. Layout is computed on the server, so an arrow can only point
rightward — there is no geometry to get wrong in the browser.

It updates by **push, not polling.** A file watcher inside `comms ui` is notified by the operating system the instant any project's `log.jsonl` changes; it rebuilds the snapshot once and streams it to every open browser tab over [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events). So when any agent anywhere appends an event, the right project lights up in the sidebar **immediately**, and your laptop isn't burning cycles re-reading logs on a timer.

Every snapshot carries the server's **front-end build fingerprint**, and the page remembers the one it loaded with. So when you replace the binary and restart `comms ui`, every open tab notices the new build on the next push and **reloads itself** to the new dashboard — no more stale UI lingering after an upgrade.

It **opens your browser automatically** when run interactively (`--no-open` to suppress). On macOS you can also double-click a **Comms Dashboard** launcher instead of using the terminal. The header shows the active **session name** (the name agents use, e.g. `acme-build`) next to the repo.

**Which dashboard the flags belong to.** The push/reload behaviour and the flags above are the **Go** build's
`comms ui`, which is still here and still works:

- `comms ui --repo /path/to/repo` — scope to a single repo (no sidebar).
- `comms ui --demo` — explore with sample data (read-only; writes nothing real).

The Python board is `comms-graph ui [--port 7878] [--host 127.0.0.1] [--graph <graph.json>]`. It has no
`--demo` and no `--repo`; scope it by running it from the repo, or name one with the global
`comms-graph --repo <path> ui`. Both read the same log, so it does not matter much which is open.

### Run the dashboard as a login service (macOS)

So the dashboard is always up — survives reboots, and is restarted automatically if it ever exits — install it as a per-user `launchd` agent. The template sets `COMMS_ACTOR` (so the operator buttons work — see the note above); change it from `operator` to your own name first:

```bash
# Set your operator name (and point at your binary if it is not Homebrew, `which comms`):
#   sed -i '' "s#<string>operator</string>#<string>human-you</string>#" contrib/launchd/plus.dpa.comms-ui.plist
install -m644 contrib/launchd/plus.dpa.comms-ui.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/plus.dpa.comms-ui.plist
```

After installing a new binary, restart the service to pick it up (open tabs then auto-reload, see above):

```bash
launchctl kickstart -k "gui/$(id -u)/plus.dpa.comms-ui"
```

To remove it: `launchctl bootout "gui/$(id -u)/plus.dpa.comms-ui"` then delete the plist.

---

## Quick start

```bash
# Install (single binary, nothing else)
go install github.com/dpa-plus/comms/cmd/comms@latest

# In desktop-app / manual use, prefix commands with a concrete actor name:
COMMS_ACTOR=codex-dev comms hello --label "Codex Dev"
COMMS_ACTOR=codex-dev comms session start "dashboard fixes" --label "Codex Dev"
COMMS_ACTOR=codex-dev comms status
COMMS_ACTOR=codex-dev comms claim "src/foo.ts" --intent "fix bug"
COMMS_ACTOR=codex-dev comms note --priority "Stop editing aggregation until my claim clears."
```

If a desktop app loses macOS access to its working directory, run `comms` from a safe directory and point it at the repo explicitly:

```bash
COMMS_ACTOR=claude-dev comms --repo /Users/you/code/my-project status
# or for one shell:
export COMMS_REPO=/Users/you/code/my-project
COMMS_ACTOR=claude-dev comms status
```

See [`docs/INSTALL.md`](docs/INSTALL.md) for manual + optional automated (hook/skill) setup, [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the event schema, and [`docs/DESIGN.md`](docs/DESIGN.md) for the *why*.

---

## Teach your agents to use it

comms ships a **skill** — [`skills/using-comms/SKILL.md`](skills/using-comms/SKILL.md) — that teaches an AI agent the protocol: when to `claim`, what to record as a `finding`, how to coordinate, and how to recover. There's one for **Claude** and one for **Codex** — the same file works for both (they share the skill format), so install it for whichever agents you run:

```bash
cp -r skills/using-comms ~/.claude/skills/    # Claude
cp -r skills/using-comms ~/.codex/skills/     # Codex
```

The agent then follows it whenever you say **`using-comms`**.

---

## Commands at a glance

```
comms hello [<name>] [--label "Claude Dev"] [--model claude-opus-5 --vendor anthropic]  # session entry; model/vendor let a verification say whether it was independent
comms session start "<name>" [--label "..."]  # create + join a named comms session
comms session join "<name>" [--label "..."]   # join an existing named comms session
comms session end "<name>" [--reason "..."]   # archive one named session + release its claims
comms claim "<scope>" ["<scope>" ...] --intent "<text>" [--steal <id> [--reason "..."]]  # steal a stale (>1h) claim freely; --reason needed for an active one
comms release [<id> ...|--latest|--all-mine] [--result "<text>"]  # selected IDs release atomically
comms session retire <actor> [--reason "..."] # remove actor from active roster; releases its claims
comms session lead [<actor>] [--reason "..."] # make exactly one active actor the leader
comms plan --from plan.json                   # a whole decomposition in one write, or nothing
comms task add <slug> --title "<instruction>" [--size S|M|L] [--slots N] [--check test] [--ref tracker:PROJ-1234]
comms task edge <from> <to> --kind interface|artifact|sequence --provides "<what the later one consumes>"
comms task done <slug> --check test=pass --note "<a decision you made>"   # finished, NOT closed
comms task review <slug> --pass | --fail --finding "<what>" --evidence "<how to check>"
comms task show                               # the graph, grouped by what you can do about it
comms next                                    # what you could pick up right now
comms brief <slug>                            # what you inherit before starting: interface + upstream decisions
comms claim "<scope>" --task <slug> --intent "..."   # tagging a claim is what puts you on a task
comms check <path>                            # PreToolUse hook (also: --stdin-json); exits 2 to block
comms mcp                                     # serve the same verbs as MCP tools over stdio
comms check --staged                         # pre-commit guard: block peer-claimed staged paths
comms status [--json]
comms log [--actor X] [--since 1h] [--scope path] [--type list] [--category cat]
comms note [--priority] "<=200-char FYI>"
comms find [--priority] <bug|fix|ship|decision|gotcha> "<summary>" [--ref kind:value ...]
comms doc --list | comms doc <slug> | comms doc <slug> --edit
comms lesson --list | comms lesson <slug> | comms lesson <slug> --edit
comms ui [--repo <path>] [--demo] [--no-open] [--stale-after 1h] [--addr 127.0.0.1:7878]  # unified by default

Global flags:
  --repo /absolute/repo/path                  # bypass cwd/git discovery
```

Use stable, readable actors for desktop-app work (e.g. `claude-dev`, `codex-dev`) plus `--label "Claude Dev"` for the UI. If an agent registers a throwaway actor, **retire** it instead of editing the log:

```bash
COMMS_ACTOR=claude-dev comms session retire claude-7e4c --reason "renamed to claude-dev"
COMMS_ACTOR=claude-dev comms session lead --reason "let Claude Dev lead"
```

---

## Upgrading (and what happens to a running session)

Because `comms` is just a binary that runs fresh on every command, upgrading is painless and **never disturbs an in-flight session**:

- **The session lives in the log file, not in the binary.** Claims, findings, and notes are on disk. Replacing the binary doesn't touch them.
- **CLI commands pick up the new version instantly** — the *next* `comms …` an agent runs uses the new binary. No restart, no re-join.
- **Only the dashboard's *process* needs a nudge.** `comms ui` is the one long-running process; it holds the old binary until you restart it. But once you do, the browser doesn't: every open tab sees the new build fingerprint on the next push and **reloads itself** (see [The live dashboard](#the-live-dashboard)). Restarting loses nothing — it just re-reads the same log.

```bash
go install github.com/dpa-plus/comms/cmd/comms@latest   # agents use it on their next command
# then restart the one long-running dashboard process; open tabs auto-reload:
launchctl kickstart -k "gui/$(id -u)/plus.dpa.comms-ui"  # if installed as a login service
# (otherwise: stop your `comms ui` and run it again)
```

---

## Design notes

- **uuid-free, dependency-light.** The core is the Go standard library plus a CLI framework, a ULID generator, and a file watcher.
- **Append-only + `flock`.** Writes are serialized by a per-repo file lock; the log is never rewritten, so history and audit are free.
- **Recoverable by design.** Blank lines are skipped, a torn final line is ignored, duplicate event IDs are dropped — a half-written line never breaks a read.
- **Opt-in, not enforced.** comms suggests and records; it doesn't block your editor. A `PreToolUse` hook (`comms check`) can warn before an agent touches a claimed path, and `comms check --staged` can stop a commit when its Git index contains another actor's claimed files.

More in [`docs/DESIGN.md`](docs/DESIGN.md) and [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

---

## What the graph adds

The log answers *who is touching what*. It cannot answer *what is this work, where
does it live, and is any of it connected* — those are questions about the code and
the plan, not about the last five minutes. Two graphs answer them, and both are
built from things the agents were already doing.

**The task graph is built out of claims.** An agent declares a task, then claims
files with `--task <id>`. That flag is the only bookkeeping in the whole tool that
is not derived for you, and it is one word on a command you were running anyway.
In return the board can open a task and show what it is, whether it is finished,
whether somebody *other than the author* checked it, and the exact files it
touched — including the ones already released, because a task that forgets its
files when the work ends answers "what did this change?" with "nothing".

Tasks connect to each other too. An edge says one task comes after another and
what the later one consumes from the earlier, so `comms-graph brief <task>` hands
the next agent the decisions the previous one made instead of letting it re-decide
them differently.

**The code map is built by [graphify](https://pypi.org/project/graphifyy/).** It
reads the repository into a graph of files, symbols and the edges between them, so
a claim can also report what sits *next to* the ground you just took — the callers,
the importers, the tests. Run it once per repo:

```bash
graphify extract . --code-only     # local AST, no API key
```

Two honest limits, both measured rather than assumed. The map misses between a
third and a half of the file pairs that really do change together, so **silence
from it is weak evidence, not a clear signal**. And of the pairs it does flag,
well under half turn out to matter — it is a prompt to look, never a verdict.
Anything it has not indexed (SQL migrations, for instance, without the optional
parser) reports "no connections" when the truth is "not looked at".

The two graphs meet on the board: a task knows its files, and the code map knows
what those files touch.

---

## Two implementations, one log

This repository holds two builds of comms, and they are not a port and its
original — they run side by side against **the same store**, read each other's
events, and block each other's claims.

- **Go** — everything outside `python/`, installed as the `comms` binary. This is
  what the shipped `PreToolUse` hook runs.
- **Python** — [`python/`](python/README.md), installed as `comms-graph`. Answers
  every verb the Go build does, plus `board` and `tasks`: a task graph with a
  review gate, and a board for the person watching. Built on
  [graphify](https://pypi.org/project/graphifyy/), so a claim can also report
  what sits *next to* the code you took.

Because the log is the interface, moving a hook from one to the other is a
one-line change and reversible — the events already interleave. Both are covered
by [CI](.github/workflows/ci.yml).

---

## License

[MIT](LICENSE).
