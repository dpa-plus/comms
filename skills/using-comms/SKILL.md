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

## Actor Identity

In desktop app sessions, prefix every command with a concrete actor. Prefer
stable readable actors for the current role, plus a UI label on hello:

```bash
COMMS_ACTOR=claude-dev comms hello --label "Claude Dev" --model claude-opus-5 --vendor anthropic
COMMS_ACTOR=codex-dev comms hello --label "Codex Dev" --model gpt-5.5 --vendor openai
COMMS_ACTOR=claude-dev comms status
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
COMMS_ACTOR=claude-dev comms session start "ad-dashboard tracking fixes" --label "Claude Dev"
COMMS_ACTOR=codex-dev comms session join "ad-dashboard tracking fixes" --label "Codex Dev"
COMMS_ACTOR=claude-dev comms status
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

The user watches coordination in a local dashboard. Easiest: **double-click
"Comms Dashboard" on the Desktop** (a launcher that starts the dashboard and
opens the browser). From a terminal it's just `comms ui`, which auto-opens the
browser when run interactively (`--no-open` to suppress, `--open` to force).

`comms ui` is **unified by default**: one tab with a left **Projects sidebar**
listing every comms project on this machine. Selecting a project scopes the
whole view — roster, active claims, recent findings/notes, a **Recently
Completed** feed (from claim-release results), the **Work graph**, and the
per-session event log —
to that project, with live SSE updates the instant any project's log changes.
A project shows as active when it has recent findings/notes/completed work, even
with no named session and all claims released. The header shows the active
**session name** (the name agents use, e.g. `acme-build`) as a pill next
to the repo, so the UI matches what agents call the work. Scope to one repo with
`comms ui --repo /path`. The launcher sets `COMMS_ACTOR=human-eli` so the
operator can release claims from the dashboard.

The top is a single rail: the project name (its tooltip carries the repo path,
hash and log path), the active session name, and — only when they are not zero —
red chips for **stale** claims, work **to verify**, and **dependency cycle**.
Those chips are the whole alarm surface: if one is lit, something is waiting on an
agent, and in the all-projects view they sum across every project. Counts sit in
the heading of the panel that owns them (Team Roster, Active Claims) rather than
in a summary row. Active claims are grouped by who holds them and why, with the
intent and the directory every path shares printed once, so the list you scan is
the files.

The UI has **Start/End Comms Session** controls and a **Session Event Log**
selector. Start/end-session are currently enabled in single-repo mode; claim
**release works in the unified view too** (routed to the owning repo). The Docs
and Global Lessons panels were removed from the dashboard — `comms doc` /
`comms lesson` remain CLI-only.

## Repo Path Recovery

If `comms`, `git`, or Node fails with `repo: getwd: operation not permitted`,
`uv_cwd operation not permitted`, or `fatal: Unable to read current working
directory`, do not assume the repo is broken. On macOS this usually means the
desktop app process lost privacy access to a protected Desktop/Documents/
Downloads path.

Use one of these recovery patterns:

```bash
cd /tmp
COMMS_ACTOR=claude-dev comms --repo /absolute/repo/path status

export COMMS_REPO=/absolute/repo/path
COMMS_ACTOR=claude-dev comms session join "session name" --label "Claude Dev"
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

Agents still use the CLI for coordination. Do not click UI controls or call the
UI mutation endpoints unless the user explicitly asks. If asked to inspect the
UI backend, use:

```bash
curl -fsS http://127.0.0.1:7878/api/status
```

The backend advertises `actions`, including `start_comms_session`,
`end_comms_session`, `release_claim`, `retire_session_actor`, and
`transfer_leader`. It also returns `active_comms_sessions[].events`,
`current_session.events`, and `comms_sessions[].events`; those are filtered
views over the append-only JSONL log.

To end one named session from the CLI:

```bash
COMMS_ACTOR=claude-dev comms session end "ad-dashboard tracking fixes" --reason "project window done"
```

## Session Roster Admin

If the user asks you to remove an old/accidental actor from active sessions,
retire it. This appends an audit event, releases that actor's active claims,
and removes it from the live roster without deleting history:

```bash
COMMS_ACTOR=claude-dev comms session retire claude-7e4c --reason "renamed to claude-dev"
```

Leadership is auto-assigned to the first active actor and only gates `--priority`
notes/findings; ignore it unless the user explicitly asks you to set a leader
(`comms session lead`). When asked to remove an actor, do not say "I can't delete
old actors" — `session retire` removes them from the active view while preserving
the append-only audit log.

## Working the Task Graph

When work is big enough to have parts, put it in the graph. A task is what should
happen; an edge says one task comes after another and what the later one consumes
from the earlier.

```bash
COMMS_ACTOR=claude-dev comms plan --from plan.json
COMMS_ACTOR=claude-dev comms next
COMMS_ACTOR=claude-dev comms brief auth-api
COMMS_ACTOR=claude-dev comms task show
```

`comms plan` appends the whole decomposition in one write and rejects it entirely
if anything is wrong — an unknown endpoint, a duplicate edge, a dependency cycle.

For one task at a time, or to extend a plan that already exists:

```bash
COMMS_ACTOR=claude-dev comms task add auth-api --title "Session create / refresh / revoke" \
  --size L --slots 2 --check test --ref omni:AUF-2291
COMMS_ACTOR=claude-dev comms task edge db-schema auth-api --kind artifact \
  --provides "sessions table; uuid PKs, no cuid"
```

The edge `--kind` is load-bearing, not a label. `interface` and `artifact` mean
the later task consumes something, so reworking the earlier one flags it for a
recheck; `sequence` is ordering only and propagates nothing. Say what is consumed
in `--provides` — that is the text `comms brief` hands to whoever picks the next
task up.

`comms next` is what you run when you finish something: it offers work waiting to
be verified first (it is finished work holding up everything downstream, and it is
cheap), then unclaimed tasks, then tasks with a free slot. It never offers you
your own work to verify. `comms task show` prints the whole graph grouped by what
you can do about it.

**Run `comms brief <slug>` before you start a task.** It walks the incoming edges
and gives you the interface or artifact you are building on plus the decisions the
upstream agent recorded. Without it you will re-decide questions that were already
settled, and probably differently.

Tag your claims so the graph tracks itself:

```bash
COMMS_ACTOR=claude-dev comms claim "src/auth/session.ts" --task auth-api --intent "server-side sessions"
```

That is what puts you on the task and what takes you off it when you release. No
separate status to remember.

### Two steps: do it, then somebody else verifies it

```bash
COMMS_ACTOR=claude-dev comms task done auth-api --check test=pass \
  --note "Refresh rotation is single-use: replaying a spent token revokes the family." \
  --note "httpOnly cookie rather than localStorage - the security questionnaire asks about XSS."
```

`done` does **not** close the task, and nothing downstream moves until it is
verified. Write the notes as decisions and their reasons, not a summary of the
diff — the arguable choices are what a reviewer needs and what travels to whoever
picks up the next task.

```bash
COMMS_ACTOR=codex-dev comms task review auth-api --pass
COMMS_ACTOR=codex-dev comms task review auth-api --fail \
  --finding "replaying a spent refresh token still succeeds" \
  --evidence "POST /session/refresh twice with the same token returns 200 both times"
```

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

A task can carry `--ref omni:AUF-2291`. comms stores the reference and never
resolves it; `comms brief` prints the command (`omni context AUF-2291`). Run it
when you need the customer background — that is Omni's data, not comms'.

## Claim Before Edits

Before editing a file in a coordinated project:

```bash
COMMS_ACTOR=claude-dev comms claim "frontend/src/lib/aggregate.ts" --intent "fix lead double-counting"
```

Use narrower anchors when practical:

```bash
COMMS_ACTOR=claude-dev comms claim "frontend/src/lib/aggregate.ts#L40-90" --intent "rewrite aggregation loop"
COMMS_ACTOR=claude-dev comms claim "src/auth.ts#validateToken" --intent "tighten JWT expiry check"
```

Claim several scopes for one task in a single call — each gets its own claim
event under the shared `--intent`, and the batch is all-or-nothing (if any
scope conflicts, nothing is claimed):

```bash
COMMS_ACTOR=claude-dev comms claim "src/auth.ts" "src/routes/login.ts" "src/__tests__/auth.test.ts" --intent "rework auth flow"
```

## Release

Release a claim as soon as you have committed that file's work. A claim is a
lock: every minute you hold it past your last edit is a minute a peer may be
blocked (real sessions have peaked at ~38 files claimed at once). Do not carry
claims across a pause, a context switch, or the end of a session.

```bash
COMMS_ACTOR=claude-dev comms release --latest --result "PR #321 merged"
```

Release several selected claims in one atomic command. Comms resolves every ID
before writing anything, so an unknown or duplicate selection leaves all claims
active:

```bash
COMMS_ACTOR=claude-dev comms release 01JX2Q3Y7W 01JX2Q4A8K --result "committed together"
```

**On a task switch — or when you stop for the day — sweep all your claims** so
nothing is left locked behind an idle or dead session:

```bash
COMMS_ACTOR=claude-dev comms release --all-mine --result "switching to billing fixes"
```

## Guard the Git Index Before Commit

Claims do not prevent Git from staging another actor's files. After staging only
the intended paths, check the index before every commit:

```bash
COMMS_ACTOR=claude-dev comms check --staged
```

Exit 0 means the staged paths are unclaimed or claimed by this actor. Exit 1
lists every peer-owned staged path and prints literal-path recovery commands:
`git restore --staged` normally, or `git rm --cached` before the first commit.
Run them to remove files from the commit without discarding their working-tree
changes, then inspect the staged diff again.

## Findings

```bash
COMMS_ACTOR=claude-dev comms find fix "leads sourced only from tracker overlay" --ref path:frontend/src/lib/aggregate.ts
COMMS_ACTOR=claude-dev comms find decision "tracker is source of truth for leads" --ref doc:lead-counting
COMMS_ACTOR=claude-dev comms find gotcha "META_TOKEN_ENC_KEY is immutable after first deploy" --ref path:src/crypto.ts
COMMS_ACTOR=claude-dev comms find bug "tracker rows duplicated when Meta sync runs more than once per hour"
COMMS_ACTOR=claude-dev comms find ship "v1.4 deployed to develop" --ref pr:#321
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
  `comms find decision "Codex owns src/**, Claude owns frontend/**"`.

Use `comms note` ONLY for transient, addressed FYIs. If you catch yourself
writing a note that explains how the system works or who owns what, it is a
`decision` or a `gotcha` — log it as a finding so it does not age out of view:

```bash
COMMS_ACTOR=claude-dev comms note "@codex-dev heads-up: Prisma migration lands next session"
```

## Docs

```bash
COMMS_ACTOR=claude-dev comms doc --list
COMMS_ACTOR=claude-dev comms doc tracker-architecture
COMMS_ACTOR=claude-dev comms doc tracker-architecture --edit
```

## Global Lessons

Lessons are curated cross-project operating knowledge for agents. They are
global, not repo-local. Read them when relevant:

```bash
comms lesson --list
comms lesson verify-data-before-ui
```

Only add or edit a lesson when the user explicitly asks or approves a proposed
lesson:

```bash
COMMS_ACTOR=claude-dev comms lesson verify-data-before-ui --edit
```

## Conflict Handling

If `comms claim` exits 1, it is blocked by another actor's active claim. The
`BLOCKED` output tells you whether that claim is **STALE** and what to do.

**A claim goes stale after 1 hour of being idle** — its holder is presumed gone
(crashed, out of quota, or moved on). You may steal a stale claim **directly**,
with no user confirmation and no `--reason` (the staleness is the justification):

```bash
COMMS_ACTOR=claude-dev comms claim "src/foo.ts" --intent "<your intent>" --steal <claim-id>
```

If the blocking claim is **not yet stale** (held < 1h), the holder may still be
working — do **not** steal it. Surface the conflict to the user; only steal with
their confirmation, which still requires a `--reason`:

```bash
COMMS_ACTOR=claude-dev comms claim "src/foo.ts" --intent "<your intent>" --steal <claim-id> --reason "user verified prior session ended"
```

Otherwise choose a different scope, or leave a note:

```bash
COMMS_ACTOR=claude-dev comms note "@claude-3a1f can I take src/foo.ts when you're done?"
```

## Failure Modes

- Exit 1 means blocked by another actor or a policy rule; show the user.
- Exit 2 means system error; warn the user and continue only if they approve.

## What This Skill Does Not Do

Do not install hooks.
Do not edit `.zshrc`.
Do not start `comms` automatically.
Do not claim files unless the user invoked `using-comms`.

**Before any Edit or Write tool call in an active `using-comms` workflow, claim the file with the selected `COMMS_ACTOR`.**
