# Why comms is built this way

comms keeps coding agents that share a working tree off each other's files. This
page explains the choices behind it, both the ones that have held since the start
and the ones 0.5.0 made after measuring how agents really use the tool.

## Where comms came from

comms replaced two earlier attempts. The first was one shared Markdown file that
agents were asked to keep up to date. It grew without limit, could not be read in
parts, and iCloud forked it whenever two agents wrote at once. The second was a
heavy MCP server with severity levels, inboxes and registration, and agents skipped
its steps. comms keeps what worked in both, an agent identity and an append-only
record, and drops the ceremony.

## The log is the whole system

Every fact comms knows is one line in one file per repository, `log.jsonl`. A claim,
a release, a note and a task change are each an event appended to it. Nothing is
ever edited or deleted. Every view, from `comms status` to the board, replays the log
from the start.

This makes the log the interface. Any tool that reads JSON lines can read it, a
broken view cannot corrupt it, and history and audit cost nothing extra.

**JSON lines, not SQLite.** A JSONL file can be read with `grep`, appended to without
a schema, and repaired by skipping a bad line. SQLite would give indexed queries,
but the file would be opaque, and a sync tool touching its journal can damage it.

**The log lives outside the repository and outside iCloud.** iCloud Drive resolves
two concurrent writes by forking the file, which for an append-only log means two
diverging histories. So the log sits in the per-user data home
(`~/Library/Application Support/comms/<repo-hash>/` on macOS), keyed by a hash of
the repository path. All git worktrees of one repository hash to the same store,
because worktree-per-agent is the most common way to run several agents on one
repository, and separate logs per worktree would coordinate nothing. Notes written
with `comms doc` are the exception: they go in `.comms/docs/` and travel with the
repository through git.

**One lock, held briefly.** Every write takes `flock` on the store's `.lock`, reads
the log, decides, appends and releases. `flock` works on macOS and Linux, and the
kernel releases it when the process exits, even after `kill -9`, so a crashed agent
never leaves a stale lock. Reads take no lock, which keeps the pre-edit check fast.

**A steal is one event.** Taking over a claim could be a release followed by a new
claim, but between the two the file would be free and a third agent could take it.
So a steal is one `claim` event that names the claim it replaces. The log never
shows a moment when nobody or both hold the file.

**Overlap is computed from the strings.** To decide whether `src/**` and
`src/auth.ts` overlap, comms compares the two patterns segment by segment and never
looks at the disk. The file may not exist yet, scanning would be slow on a large
tree, and two machines with different trees would get different answers.

## Why comms reads git

A claim only records what an agent chose to declare. For a long time every guard
asked "is this path claimed by somebody else?" and treated "no" as safe. It is not.
"Nobody declared anything" and "nothing is happening" are different facts, and
several real incidents failed in exactly that direction: a board reporting no
claims over a tree full of changed files, a commit that carried a staged deletion
left by an agent that had gone, and an agent that ran no comms command at all after
its context was compacted and edited heavily anyway.

So comms reads `git status` itself and shows changed files next to claimed files.
It never guesses who made an unclaimed change, because "we do not know who did
this" is the finding. The general rule is to prefer a signal nobody has to remember
to send, and never to show "could not check" as "all clear". The same rule makes
the pre-edit check refuse an edit when it cannot read the log.

## Claims do not expire on a clock

Agents have no reliable sense of time, so a claim that expired after a timeout
would vanish in the middle of a long task. No claim ends by itself. A claim ends
when its holder releases it or someone steals it with a reason. A claim made by an
edit also ends when its session ends or when the same session claims the file by
hand.

The clock is used only to advise. A holder's silence is judged by its last event
and by a marker file the edit hook touches on every edit. When a claim is over an
hour old and its holder has been silent that long, `status` marks it STALE, and an
edit refused by a holder silent for an hour prints the exact `--steal` command. The
decision to take the file stays with an agent or the person watching.

## What changed in 0.5.0, and why

0.5.0 was shaped by replaying how agents used comms: 26,349 tool calls from agent
transcripts, 53,599 logged events and the owner's messages to agents, all from the
author's machine. The numbers below come from that replay.

### One build

comms had a Go binary and a Python build that read the same log. They had drifted
apart in eight behaviors, and agents made only 13 calls to the Go build in the
month before the release. The Python build had no version and no release path, and
a stale install was the owner's most repeated complaint. Keeping two builds meant
fixing every bug twice and still shipping disagreement, so the Go build was
retired. `comms` and `comms-graph` are now the same Python program, with one version
number, installed by `install.sh` from the wheel attached to each release. The log
format did not change, so every existing log still loads.

### Identity comes from the session

Before 0.5.0, an agent had to say who it was on every call, through `COMMS_ACTOR`
or `--as`. In practice the session-start hook that was supposed to name the agent
failed on all 725 recorded runs. 11 of the first 21 hook blocks stopped an agent
from editing a file it had claimed itself, and all 4 recorded cases of an agent
writing a file through the shell to get around a block followed one of those false
blocks. Agents passed the name by hand on 95% of calls, and the owner had to tell
agents to "join and claim" about 35 times.

Now Claude Code tells comms which session is calling, and comms names the session
the first time it sees it, for example `claude-4f9a1c`. `--as` and `COMMS_ACTOR`
still win when given. Every name used inside one session counts as the same owner,
so a session is never blocked by its own claim. Subagents share their parent's
session, which is why the skill asks for `--as <name>` when subagents work in
parallel.

### The first edit claims the file

Claiming before the first edit stayed at about 65% across model generations, and
67% of the subagents that edited files in the busiest project used comms not at all.
Agents that did start using comms handled the rest well. Releasing at the end rose
from 30% to 68%, and tagging claims with a task from 22% to 73%. The weak point was
the first step, so comms now takes it for them. The pre-edit hook claims a free file
the first time a session edits it.

A claim made this way must not hoard the file. It ends when the session ends, and
it gives way to another agent as soon as its file has nothing uncommitted in git.
A claim the agent makes by hand behaves as before.

### Short output

`status` printed a median of 1.5k characters, 9.7k at the 95th percentile, and up
to 19k. `board` reached 40k. 46% of recent calls piped the output through `head` or
`grep`. Long output costs an agent context and hides the one line it needed. Now
`status` and `board` print the caller's view by default: its claims, how many files
each other agent holds, and notes to it. `--all` prints everything, and `--json` is
unchanged.

### Commands that accept what agents type

Agents lost many calls to the command line itself: 279 full usage dumps after a bad
call since 15 October, 48 notes over the length limit, 44 task ids refused as "not
a slug", 49 "no task called X", and 1,574 help lookups. They also guessed verbs that
did not exist, such as `task show`, `task list`, `claims` and `whoami`. Those
guesses now work. A mistake is answered in at most three lines that name the valid
form. Global flags work before or after the verb. Notes take up to 1000 characters,
and task ids up to 64, in any case. An unknown flag is refused, because silently
dropping it, as `claim x --reason y` used to, is worse than an error.

### What was cut

`lesson`, `plan` and `completion` were removed. None of the three was called after
September 2026, and no lesson was ever written. Tests used to write into the real
data home, and 251 of the 258 store folders on the author's machine were test
leftovers. Tests now point `COMMS_HOME` at a temporary folder,
and any repository under the system temp folder keeps its store there.

## Tasks are written for people

A claim says who is on a file. A task says what the work is, and tagging claims
with `--task <id>` is what lets the board answer "which files did this touch". The
`--title` is the one line about a task that a person outside the code reads, so it
is plain English with no file or function names: "Round money the same way
everywhere", not "money-rounding refactor in toCents". A task that asks for review
closes only when a different agent checks it, because a task its author marks
finished and a task somebody verified are different claims.

## The MCP server came back

The MCP server was cut along with the rest of the earlier server's ceremony, and
later brought back. The ceremony was the problem, not the transport. A tool that
sits in an agent's tool list every turn gets used more than a command the agent has
to be told about. `comms mcp` serves six tools that map one to one onto `check`,
`claim`, `release`, `status`, `note` and `find`, with no registration and no inbox.
It speaks JSON-RPC over stdio without an SDK, and every tool takes an `actor`
argument because one server process may act for several agents.

## Still out of scope

- Sync between machines. Each machine has its own logs.
- Windows. comms stores its logs on macOS and Linux only, and refuses to guess a
  third layout.
- Symbol-aware overlap. A symbol anchor such as `src/auth.ts#login` is compared as
  a string, without a language server.
