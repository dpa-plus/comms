---
name: using-comms
description: Use only when the user explicitly invokes the using-comms skill by name, for example "$using-comms", "using-comms", "use the using-comms skill", or "invoke the using-comms skill". Do not trigger merely because the user mentions comms, says "start comms", "use comms", "with comms", "claim with comms", "check comms", "release comms", or describes coordination work.
---

# Using `comms`

Use this workflow only after the user explicitly invokes `using-comms`.
Bare references to `comms` are not enough.

`comms` coordinates parallel coding sessions through per-session claims,
short notes/findings, and a repo-local docs wiki backed by a per-machine JSONL
event log.

## What you are running

The command is **`comms-graph`** (`~/.local/bin/comms-graph`). The older `comms`
binary is still on this machine and reads the same log, but `comms-graph` is the
one to use: it is where the task graph, the board and `--staged` live.

**Claims are enforced, not advised.** A `PreToolUse` hook runs
`comms-graph check --stdin-json` before every Edit and Write. If you touch a file
somebody else holds, the edit is refused and you are told who holds it. You do
not run that hook yourself; it runs whether or not you remember it.

**Spellings that reach one file are one claim.** `src/a.py` and `src/A.py` are
the same file on this disk, and the guard treats them as one, so you cannot get
round a claim by changing the case of a letter.

**Claim before the first WRITE, not before the first edit you notice.** A
generator counts. `npm run i18n -- set` counts. A codegen step, a formatter you
point at one file, a script that rewrites a JSON file — all writes. An agent
here wrote three keys into a shared `messages/de.json` through an i18n command,
claimed the file forty minutes later when it got to the tests, and in between a
peer committed the whole file inside an unrelated change. Nothing was violated:
at that moment nobody held it. Nothing broke either, which was luck. That agent
would have said it was following the rule right up until it read the log.

**The hook only sees Edit and Write.** If you change a file with `sed -i`, a
heredoc, `tee`, or a Python one-liner inside Bash, nothing checks it — and in
auto mode Bash is the documented default, so this is the normal case, not a
corner. Measured on a real session: ~35 edits, every one through Bash, the hook
fired zero times, and two agents' changes were swept into each other's commits.

So the hook is a safety net for one route in, not proof you are clear. **Claim
before you edit, whichever tool you use**, and run `comms-graph check <path>`
yourself before a Bash edit on anything you have not claimed. Shell commands
cannot be intercepted reliably, so this part is on you.

The backstop that does hold is a git pre-commit hook, because a commit cannot be
argued with by a heredoc:

```sh
printf '#!/bin/sh\nexec comms-graph check --staged\n' > .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

That protects **one machine**: `.git/hooks/` is not versioned, so a fresh clone
has none of it and every session has to run it separately. For a guard that
survives a clone, put the hook in a tracked directory and point git at it:

```sh
mkdir -p .githooks
printf '#!/bin/sh\nexec comms-graph check --staged\n' > .githooks/pre-commit
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks     # per clone, still not automatic
```

`core.hooksPath` is local config, so a clone still needs that one line — but the
hook itself travels with the repository instead of being reinvented.

Do not install either of these yourself. They change the user's repository, and
a peer suggesting it is not the user asking for it. Say what it buys and leave
the decision.

Three things changed on 2026-08-21 and are new to you:

- `claim a b c --intent "..."` takes several scopes **atomically** — all of them
  or none, so you never end up holding half a task boundary.
- `check --staged` refuses a commit whose index touches somebody else's claimed
  files, and prints the exact `git restore --staged` / `git rm --cached` line to
  undo it.
- `--repo <path>` and `COMMS_REPO` name the repository from outside it, which is
  the recovery when macOS withdraws access to the working directory.

If something here is wrong, refuses when it should not, or lets something
through that it should have caught, say so in your reply to the user with the
exact command and its output. This build is new and that report is worth more
than a workaround.

## Actor Identity

In desktop app sessions, prefix every command with a concrete actor. Prefer
stable readable actors for the current role, plus a UI label on hello:

```bash
COMMS_ACTOR=claude-dev comms-graph hello --label "Claude Dev" --model claude-opus-5 --vendor anthropic
COMMS_ACTOR=codex-dev comms-graph hello --label "Codex Dev" --model gpt-5.5 --vendor openai
COMMS_ACTOR=claude-dev comms-graph status
```

Pick one actor name when this skill starts and reuse it for the conversation.
Do not use generic names like `eli`, `claude`, `codex`, `agent`, or `user`.

`--model` and `--vendor` are optional (they fall back to `$COMMS_MODEL` and
`$COMMS_VENDOR`) but say them anyway. comms uses the vendor to record whether a
verification was `independent` — checked by something from a different maker than
the agent that did the work — or `same-family`. "Verified" and "verified by
something with the same blind spots" are different claims, and only the log can
tell them apart later.

## Session Start

```bash
COMMS_ACTOR=claude-dev comms-graph session start "ad-dashboard tracking fixes" --label "Claude Dev"
COMMS_ACTOR=codex-dev comms-graph session join "ad-dashboard tracking fixes" --label "Codex Dev"
COMMS_ACTOR=claude-dev comms-graph status
```

Use `session start "<name>"` when the user asks you to create a named
communication session. Use `session join "<name>"` when the user says another
agent already created one. After joining, claims, notes, findings, and releases
are automatically tagged with that named session so the UI can show separate
logs for simultaneous project windows.

If you start or join a different named session, comms releases your active
claims from the previous session with an audit event before registering you in
the new one. Claims do not follow an actor into a new session.

Mention the chosen actor and joined session name in your reply so the user can
see both.

## Operator UI

The user watches coordination in a local board: `comms-graph ui` (`--port`,
`--host`, `--graph`). It opens on the activity stream — what happened, newest
first, with the events that need somebody bunched at the top — plus a Projects
rail listing every comms store on this machine, the roster, the task graph, and
counts for this repo.

**The board is read-only.** It serves `GET` and nothing else: there are no
buttons that release a claim, end a session or retire an actor, and no mutation
endpoints to call. Coordination is changed only through the CLI, by the agent
that owns the work. That is deliberate — the log is written under a lock through
a fold that enforces the rules, and a dashboard that could write would be a
second writer with none of those guarantees.

If asked to inspect the backend:

```bash
curl -fsS http://127.0.0.1:7878/api/status
```

It returns the whole snapshot: `feed` (recent events, newest first), `claims`,
`findings`, `notes`, `tasks`, `roster`, `alerts`, `projects`, `counts`, and
`root`. There is no `actions` array, because there are no actions.

## Repo Path Recovery

If `comms`, `git`, or Node fails with `repo: getwd: operation not permitted`,
`uv_cwd operation not permitted`, or `fatal: Unable to read current working
directory`, do not assume the repo is broken. On macOS this usually means the
desktop app process lost privacy access to a protected Desktop/Documents/
Downloads path.

Use one of these recovery patterns:

```bash
cd /tmp
COMMS_ACTOR=claude-dev comms-graph --repo /absolute/repo/path status

export COMMS_REPO=/absolute/repo/path
COMMS_ACTOR=claude-dev comms-graph session join "session name" --label "Claude Dev"
```

**Never override `HOME`** (e.g. `HOME=/tmp comms …`). comms stores its event log
under `$HOME/Library/Application Support/comms/`, so changing `HOME` silently
forks every claim/finding/note into a throwaway log that the shared dashboard
and other agents never see — coordination breaks with no error. To escape a
protected working directory use `cd /tmp` (changes the *cwd*, not `HOME`) and
point at the repo with `--repo`/`COMMS_REPO`; leave `HOME` untouched. comms now
prints a warning when its store resolves under a temp dir — if you see it, drop
the `HOME=` and re-run your `hello`/`session join`/`claim`.

Prefer moving long-running/background-service repos to `~/code/<project>` so
agents and launchd jobs avoid macOS protected-folder access problems.

Coordination is changed through the CLI only, by the agent that owns the work.
The board serves GET and has no mutation endpoints, so there is nothing to click
even if asked — see Operator UI above for what `/api/status` returns.

To end one named session from the CLI:

```bash
COMMS_ACTOR=claude-dev comms-graph session end "ad-dashboard tracking fixes" --reason "project window done"
```

## Session Roster Admin

If the user asks you to remove an old/accidental actor from active sessions,
retire it. This appends an audit event, releases that actor's active claims,
and removes it from the live roster without deleting history:

```bash
COMMS_ACTOR=claude-dev comms-graph session retire claude-7e4c --reason "renamed to claude-dev"
```

Leadership is auto-assigned to the first active actor and only gates `--priority`
notes/findings; ignore it unless the user explicitly asks you to set a leader
(`comms-graph session lead`). When asked to remove an actor, do not say "I can't delete
old actors" — `session retire` removes them from the active view while preserving
the append-only audit log.

## Working the Task Graph

**Every request that will change files gets a task, created before the first
claim, and every claim for it carries `--task <slug>`.** Not "when the work is
big enough" — when you are asked to change something. Declare it with
`comms-graph task add <slug> --title "..."`, or `comms-graph plan --from` for a
whole decomposition, then claim with `--task <slug>`.

A question you answer by reading is not a task. Anything you are going to edit
is.

This is a rule, not a suggestion, and it is stated as one deliberately. When it
was phrased as "put it in the graph when work is big enough", agents skipped it:
across 4,356 real claims exactly one carried a task. Nothing was broken — the
instruction was soft, and soft instructions lose to hard ones every time.

**What the tag buys, concretely.** The board lists every task and opens one on a
click, and what it shows there is built from your claims: the files the task
touches, who holds each, and which are already released. That list exists only
because the claims were tagged. Untagged, the task shows "No files are tagged to
this task" and the person watching has to ask you where the work is — which is
the question the tool is supposed to have already answered.

It costs one flag on a command you were running anyway, and it is the only
bookkeeping here that is not derived for you.

A task is what should happen; an edge says one task comes after another and what
the later one consumes from the earlier.

```bash
COMMS_ACTOR=claude-dev comms-graph plan --from plan.json
COMMS_ACTOR=claude-dev comms-graph next
COMMS_ACTOR=claude-dev comms-graph brief auth-api
COMMS_ACTOR=claude-dev comms-graph tasks        # draws the graph to HTML
```

`comms-graph plan` appends the whole decomposition in one write and rejects it entirely
if anything is wrong — an unknown endpoint, a duplicate edge, a dependency cycle.

For one task at a time, or to extend a plan that already exists:

```bash
COMMS_ACTOR=claude-dev comms-graph task add auth-api --title "Session create / refresh / revoke" \
  --size L --check test --ref tracker:PROJ-1234
COMMS_ACTOR=claude-dev comms-graph task edge db-schema auth-api --kind consumes \
  --provides "sessions table; uuid PKs, no cuid"
```

The edge `--kind` is load-bearing, not a label, and there are two of them.
`consumes` means the later task uses something the earlier one produces, so
reworking the earlier one flags it for a recheck. `sequence` is ordering only
and propagates nothing. Say what is consumed in `--provides` — that is the text
`comms-graph brief` hands to whoever picks the next task up.

(`interface` and `artifact` are still accepted and mean `consumes`. They were
two words for one behaviour, which made every edge a choice between synonyms.)

**One task, one agent at a time.** If somebody else is holding ground tagged to
a task, take a different one — or split it so you each have your own piece to be
checked. You are not blocked from working in parallel: you take different files,
which is what the file lock is for. What you cannot do is share a task, because
then one review covers work that is not all there yet.

`comms-graph next` is what you run when you finish something: it offers work waiting to
be verified first (it is finished work holding up everything downstream, and it is
cheap), then unclaimed tasks, then tasks with a free slot. It never offers you
your own work to verify. There is no text dump of the whole graph: `comms-graph
next` is the actionable view, `comms-graph brief <id>` is one task in full, and
`comms-graph tasks` draws the graph to an HTML file for a person to look at.

**Run `comms-graph brief <slug>` before you start a task.** It walks the incoming edges
and gives you what you are building on plus the decisions the
upstream agent recorded. Without it you will re-decide questions that were already
settled, and probably differently.

Tag your claims so the graph tracks itself:

```bash
COMMS_ACTOR=claude-dev comms-graph claim "src/auth/session.ts" --task auth-api --intent "server-side sessions"
```

That is what puts you on the task and what takes you off it when you release. No
separate status to remember.

### Two steps: do it, then somebody else verifies it

```bash
COMMS_ACTOR=claude-dev comms-graph task done auth-api --check test=pass \
  --note "Refresh rotation is single-use: replaying a spent token revokes the family." \
  --note "httpOnly cookie rather than localStorage - the security questionnaire asks about XSS."
```

`done` does **not** close the task, and nothing downstream moves until it is
verified. Write the notes as decisions and their reasons, not a summary of the
diff — the arguable choices are what a reviewer needs and what travels to whoever
picks up the next task.

```bash
COMMS_ACTOR=codex-dev comms-graph task review auth-api --pass
COMMS_ACTOR=codex-dev comms-graph task review auth-api --fail \
  --finding "replaying a spent refresh token still succeeds" \
  --evidence "POST /session/refresh twice with the same token returns 200 both times"
```

**Say how you checked, not just that you did.** A pass that records no method is
a green tick: the next agent builds against that interface on the strength of it
and has no way to tell a real check from a glance. Put the method in the verdict
where your build takes it (`--pass --evidence "ran the suite, 14 pass"`), and in
a note on the task where it does not — the build installed today refuses
`--evidence` on `--pass`, because it pairs that flag with `--finding` for the
failure path.

Rules the tool enforces, so do not plan around them:

- **You cannot verify your own work.** A role suffix does not help; `claude-dev/review`
  is still `claude-dev`.
- **Declared checks must pass** before work can even reach review.
- **A finding must be checkable** — an input that breaks it, a line that contradicts
  the spec, a case the tests miss. The rule on the other side is "if the reason is
  real, fix it, whoever gave it", and an opinion cannot be acted on that way.
- **One verdict.** Do not iterate to agreement with the author; escalate to a third
  agent or to the user instead.

Verify in a **fresh session** wherever you can: read the task, the notes and the
diff, not the author's transcript. If you are spawning a subagent to review, give
it the task and the diff and not your reasoning, keep it read-only, and let it
report its own verdict.

The operator's dashboard shows the same graph: an arrow means the task it points
at comes afterwards, and tasks joined to nothing are drawn apart. Work waiting for
a verifier surfaces as a red **to verify** chip on the top rail, and a plan that
can never finish as a **dependency cycle** chip — both visible without opening
anything. You do not need to look at it. Keep the log honest and it follows.

### Where the real context lives

A task can carry `--ref tracker:PROJ-1234` — a pointer to wherever the real
context lives. comms stores it verbatim and never resolves it; `comms-graph brief`
prints it back and stops there. Reading it is your job: the ticket system that
owns that reference is the one that knows how, and comms taking a dependency on
it would mean auth, a CLI that may be absent, and a public tool hard-wired to
one team's internals.

## How the Task Graph and the Code Map Meet

This is the reason comms sits on graphify at all, so it is worth one paragraph
of how rather than a list of verbs.

**Two graphs, and only one of them is declared.**

- The **task graph** is what people wrote down. An edge exists because somebody
  ran `task edge`, and it means *sequence*: the target comes after the source.
- The **code map** is what the code actually does. `graphify extract` reads the
  repository into files, symbols and the edges between them. Nobody declares any
  of it and it is true whether or not anyone noticed.

**They are joined by your claims.** A claim tagged `--task <slug>` puts a file on
a task. The map already knows how that file reaches other files. So comms can
say which *tasks* meet in the code — derived, not typed — and the board shows it
on a task as **MEETS IN THE CODE**, with the count of places the two share.

That is worth stating plainly because it is the difference between the two
things being connected and merely being installed side by side. Measured on this
machine: eight tasks and **zero** declared edges in a log with thousands of
events. Left to declarations alone the task graph is a list. Left to the code
map alone, nobody knows which work the connections belong to.

**Read them as different kinds of claim.** A declared edge is a judgement about
order and it blocks: the successor waits. A code connection is a fact with no
direction — these two pieces of work touch the same neighbourhood — and it blocks
nothing. It is a reason to go and look, or to talk to whoever holds the other
task, before you find out by collision.

The map runs at roughly a third to a half recall on this kind of codebase, so
**silence from it is weak evidence**. "Meets nothing" means "nothing found", not
"nothing there".

**And some files are not on the map at all**, which is a different thing from low
recall and reads the same. The map is built from code: a JSON catalogue, a
config file, a SQL migration has no symbols and no imports, so it is simply
absent. In this repo the single highest-traffic shared file is
`frontend/messages/de.json` — the one most likely to be edited by two agents at
once — and it can never appear as a shared file. An agent lost work to exactly
that file while its task showed no relevant neighbours. Treat catalogue, config
and migration files as invisible here and coordinate them by claiming, not by
looking for a connection that cannot exist.

**The map is only as fresh as the last extract.** It does not update itself. A
map several commits old reports line numbers that have moved and misses
connections added since, and it does so confidently — which is worse than having
none, because a wrong answer and a right one look identical. The board shows the
map's age and warns past a day. If what you are reading matters, re-run
`graphify extract . --code-only` first.

**Where this is worth reading, and where it is not.** Measured by the agents
using it: for the author of both sides of a connection it is confirmation, not
information — you already know. It earns its place when you are about to touch a
file you did NOT write and cannot know who is downstream of it right now. The
code map gives you the fan-out; the claims give you which of those are in
somebody's open task. Neither half is the feature; the join is.

That is also why `claim` runs the same check at the moment you take ground: that
is when the information is new.

**A task you made to find something out is not work.** Declare it with
`comms-graph task add <slug> --probe` and it stays out of the derived
neighbours. Diagnostics, spikes and reproductions get tasks under the rule
above, and without the flag they sit in the graph afterwards as permanent
neighbours of real work.

**In practice, before you start a task:**

```bash
comms-graph brief <slug>          # what it is, what it consumes, decisions upstream
comms-graph board                 # who holds what, and under which task
graphify explain "<symbol>"       # what your ground connects to in the code
```

If the board says your task meets another one, read that task before you edit —
whoever holds it is working in the same neighbourhood, and that is exactly the
collision claims cannot prevent on files neither of you has claimed yet.

---

## Navigating the Code

If the repo has a graphify map (`graphify-out/graph.json`), it can answer some
questions far more cheaply than reading files — and others far worse than `rg`.
The split is not a matter of taste; it was measured on this codebase.

**Grep to find. graphify to expand. Never graphify to find.**

`rg` locates a symbol. Once you have its name, graphify tells you what connects
to it — which is the part grep cannot do without a manual fan-out.

**The two forms answer different questions and only one of them gives you
callers.** `explain <symbol>` returns what that symbol calls and what contains
it; it does NOT reliably return its callers, including plain same-file calls.
`explain <file>` returns the files that import it, and is complete. So if the
question is "who depends on this", ask for the FILE. Measured against `rg` on a
real change: the file form matched grep exactly, and its per-symbol precision is
what makes it worth running before a signature change — nine files imported the
module, only three imported the symbol being changed.

```bash
graphify explain "<symbol>"                    # its CALLEES and what contains it
graphify explain "path/to/file.ts"             # its IMPORTERS — who depends on it
graphify affected "<node-id>"                  # transitive blast radius
graphify god-nodes                             # the hubs, when you are new here
```

`explain` on a symbol you already know is the tool's real product: on a real
523-file repo it answered "who calls this and what does it call" in 449 tokens
against grep's 667, collapsed twenty identical test callsites to one, and found
a caller in `scripts/` that a `src/`-scoped grep missed.

**Do not use `graphify query`.** It seeds by string similarity and walks two
hops, and two hops from anything in a React or Next.js repo reaches a helper
like `cn()` with 180+ edges, so the neighbourhood explodes. Asked where bank
transactions get matched to rent, it reported 951 nodes, truncated to 77, and
ranked the correct function **72nd** — below `Button`, `DialogTitle` and a
Tailwind classname helper. `rg -n "export (async )?function match" src/`
answered the same question in one line. Every "where is X" question we tried
went the same way.

`affected` needs a node id, not a name. Run `explain <name>` first and copy the
`ID:` it prints.

**Two blind spots, both of which fail silently:**

- **The graph holds names and edges, never code.** It cannot answer "how does
  this work" or "what is the actual rule". It will tell you
  `updateCachedAccessToken` exists; only reading the file tells you the token is
  encrypted at rest behind a refresh lock.
- **Prisma models and SQL migrations are not in the graph at all.** A
  `schema.prisma` with 33 models contributes zero nodes. So `affected` on
  anything data-model-shaped returns *"No affected nodes found"* — which is a
  confident false negative, not an error. Answer those with `rg`.

**The map does not know when it is stale.** There is no timestamp, no commit sha
and no file hashes in it, and nothing warns you. Move a function and it still
reports the old line; DELETE a function and it still reports the function. If
the code has changed since the map was built — and in a long or unattended run
it has — rebuild first:

```bash
graphify update .        # ~5s for 550 files, no LLM, no network
```

Treat a map you did not build yourself this session as a hint, never as truth.

## Claim Before Edits

Before editing a file in a coordinated project:

```bash
COMMS_ACTOR=claude-dev comms-graph claim "frontend/src/lib/aggregate.ts" --intent "fix lead double-counting"
```

Use narrower anchors when practical:

```bash
COMMS_ACTOR=claude-dev comms-graph claim "frontend/src/lib/aggregate.ts#L40-90" --intent "rewrite aggregation loop"
COMMS_ACTOR=claude-dev comms-graph claim "src/auth.ts#validateToken" --intent "tighten JWT expiry check"
```

If the pre-edit hook is installed, this is enforced rather than advisory: an
Edit or Write to a path somebody else holds is stopped before it happens, and
the refusal is written to the log. `comms-graph status` reports the running total as
COLLISIONS PREVENTED — the only number that shows the tool is doing its job.

The hook works out which actor you are from your agent session, recorded when
you ran `comms-graph hello`. You do not need `COMMS_ACTOR` exported into the
environment for it, and it will not mistake your own claim for somebody else's.

Claim several scopes for one task in a single call — each gets its own claim
event under the shared `--intent`, and the batch is all-or-nothing (if any
scope conflicts, nothing is claimed):

```bash
COMMS_ACTOR=claude-dev comms-graph claim "src/auth.ts" "src/routes/login.ts" "src/__tests__/auth.test.ts" --intent "rework auth flow"
```

## Release

Release a claim as soon as you have committed that file's work. A claim is a
lock: every minute you hold it past your last edit is a minute a peer may be
blocked (real sessions have peaked at ~38 files claimed at once). Do not carry
claims across a pause, a context switch, or the end of a session.

```bash
COMMS_ACTOR=claude-dev comms-graph release --latest --result "PR #321 merged"
```

Release several selected claims in one atomic command. Comms resolves every ID
before writing anything, so an unknown or duplicate selection leaves all claims
active:

```bash
COMMS_ACTOR=claude-dev comms-graph release 01JX2Q3Y7W 01JX2Q4A8K --result "committed together"
```

**On a task switch — or when you stop for the day — sweep all your claims** so
nothing is left locked behind an idle or dead session:

```bash
COMMS_ACTOR=claude-dev comms-graph release --all-mine --result "switching to billing fixes"
```

## Guard the Git Index Before Commit

Claims do not prevent Git from staging another actor's files. After staging only
the intended paths, check the index before every commit:

```bash
COMMS_ACTOR=claude-dev comms-graph check --staged
```

Exit 0 means the staged paths are unclaimed or claimed by this actor. Exit 1
lists every peer-owned staged path and prints literal-path recovery commands:
`git restore --staged` normally, or `git rm --cached` before the first commit.
Run them to remove files from the commit without discarding their working-tree
changes, then inspect the staged diff again.

## Findings

```bash
COMMS_ACTOR=claude-dev comms-graph find fix "leads sourced only from tracker overlay" --ref path:frontend/src/lib/aggregate.ts
COMMS_ACTOR=claude-dev comms-graph find decision "tracker is source of truth for leads" --ref doc:lead-counting
COMMS_ACTOR=claude-dev comms-graph find gotcha "META_TOKEN_ENC_KEY is immutable after first deploy" --ref path:src/crypto.ts
COMMS_ACTOR=claude-dev comms-graph find bug "tracker rows duplicated when Meta sync runs more than once per hour"
COMMS_ACTOR=claude-dev comms-graph find ship "v1.4 deployed to develop" --ref pr:#321
```

Category cheat sheet:

- `bug` means an open problem.
- `fix` means a resolved problem.
- `ship` means released or deployed.
- `decision` means an architectural choice, source of truth, or ownership boundary.
- `gotcha` means a persistent trap future agents should remember.

Capture durable knowledge the moment you learn it — findings persist and are what
the next session actually reads:

- Log a `gotcha` the instant something surprises you or wastes your time (a
  sandbox limit, a non-obvious config, a flaky-test trap).
- Log a `decision` whenever you pick an architecture, a source of truth, or an
  ownership boundary, e.g.
  `comms-graph find decision "Codex owns src/**, Claude owns frontend/**"`.

Use `comms-graph note` ONLY for transient, addressed FYIs. If you catch yourself
writing a note that explains how the system works or who owns what, it is a
`decision` or a `gotcha` — log it as a finding so it does not age out of view:

```bash
COMMS_ACTOR=claude-dev comms-graph note "@codex-dev heads-up: Prisma migration lands next session"
```

## Tools Instead of Commands

`comms-graph mcp` serves the same verbs as MCP tools over stdio: `comms_check`,
`comms_claim`, `comms_release`, `comms_status`, `comms_note`, `comms_find`. If
your host has it configured, prefer the tools — they are in front of you every
turn, whereas this skill only loads when somebody types its name.

The tools write the same events to the same log as the CLI, so a session using
tools and a session using commands coordinate with each other without either
knowing which the other used. Each tool takes an `actor` argument, because one
server process may act for several agents.

## Docs

```bash
COMMS_ACTOR=claude-dev comms-graph doc --list
COMMS_ACTOR=claude-dev comms-graph doc tracker-architecture
COMMS_ACTOR=claude-dev comms-graph doc tracker-architecture --edit
```

## Global Lessons

Lessons are curated cross-project operating knowledge for agents. They are
global, not repo-local. Read them when relevant:

```bash
comms-graph lesson --list
comms-graph lesson verify-data-before-ui
```

Only add or edit a lesson when the user explicitly asks or approves a proposed
lesson:

```bash
COMMS_ACTOR=claude-dev comms-graph lesson verify-data-before-ui --edit
```

## Conflict Handling

If `comms-graph claim` exits 1, it is blocked by another actor's active claim. The
`BLOCKED` output tells you whether that claim is **STALE** and what to do.

**A claim goes stale after 1 hour of being idle** — its holder is presumed gone
(crashed, out of quota, or moved on). You may steal a stale claim **directly**,
with no user confirmation and no `--reason` (the staleness is the justification):

```bash
COMMS_ACTOR=claude-dev comms-graph claim "src/foo.ts" --intent "<your intent>" --steal <claim-id>
```

If the blocking claim is **not yet stale** (held < 1h), the holder may still be
working — do **not** steal it. Surface the conflict to the user; only steal with
their confirmation, which still requires a `--reason`:

```bash
COMMS_ACTOR=claude-dev comms-graph claim "src/foo.ts" --intent "<your intent>" --steal <claim-id> --reason "user verified prior session ended"
```

Otherwise choose a different scope, or leave a note:

```bash
COMMS_ACTOR=claude-dev comms-graph note "@claude-3a1f can I take src/foo.ts when you're done?"
```

**A refused edit is the tool working. Do not find another way to make it.** Not a
shell command instead of the editor, not a different actor name, not a subagent
sent to do it for you. Two agents in a supervised run did exactly this — one
scripted the write, one re-exported `COMMS_ACTOR` — and both produced the silent
double-edit the claim existed to prevent. Say what you were blocked on, and stop.

## Failure Modes

Exit codes are not uniform, because two different consumers read them and they
disagree about what the numbers mean.

- `comms-graph claim` refused because somebody else holds the scope: **exit 1**. Show
  the user; do not retry, and do not `--steal` without their say-so.
- `comms-graph check <path>` / `--stdin-json` blocked: **exit 2**. That is the code
  Claude Code's PreToolUse contract treats as "block this edit and show the model
  why"; any other non-zero is read as "the hook itself failed" and the edit goes
  through anyway.
- `comms-graph check --staged` blocked: **exit 1**. That one feeds a git pre-commit
  hook, where any non-zero aborts the commit.
- System error (unreadable log, missing directory): **exit 2**. On the hook path
  that shares a code with "blocked", deliberately — if comms cannot read the log
  it cannot prove the path is clear, so stopping is the safe direction.

## What This Skill Does Not Do

Do not install hooks.
Do not edit `.zshrc`.
Do not start `comms` automatically.
Do not claim files unless the user invoked `using-comms`.

**Before any Edit or Write tool call in an active `using-comms` workflow, claim the file with the selected `COMMS_ACTOR`.** The claim is what stops two agents
editing one file: `comms-graph check` now genuinely blocks the edit when somebody else
holds the path, and the refusal is recorded, so a claim you skip is a collision
nobody can see.

**Before starting multi-step work, declare it as a task or a plan, and pass `--task` on the claims that carry it out.**
