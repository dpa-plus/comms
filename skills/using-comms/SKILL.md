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
COMMS_ACTOR=claude-dev comms hello --label "Claude Dev"
COMMS_ACTOR=codex-dev comms hello --label "Codex Dev"
COMMS_ACTOR=claude-dev comms status
```

Pick one actor name when this skill starts and reuse it for the conversation.
Do not use generic names like `eli`, `claude`, `codex`, `agent`, or `user`.

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
Completed** feed (from claim-release results), and the per-session event log —
to that project, with live SSE updates the instant any project's log changes.
A project shows as active when it has recent findings/notes/completed work, even
with no named session and all claims released. Scope to one repo with
`comms ui --repo /path`. The launcher sets `COMMS_ACTOR=human-eli` so the
operator can release claims from the dashboard.

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

If the user asks you to become or assign the leader, transfer leadership:

```bash
COMMS_ACTOR=claude-dev comms session lead --reason "user asked Claude Dev to lead"
COMMS_ACTOR=human-eli comms session lead claude-dev --reason "user asked Claude Dev to lead"
```

The leader's only extra privilege is posting priority notes/findings. Do not
say "I can't delete old actors"; say that `session retire` removes them from
active view while preserving the append-only audit log.

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

```bash
COMMS_ACTOR=claude-dev comms release --latest --result "PR #321 merged"
COMMS_ACTOR=claude-dev comms release --all-mine --result "switching tasks"
```

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
- `decision` means an architectural choice.
- `gotcha` means a persistent trap future agents should remember.

Use `comms note` for short FYIs that are not persistent decisions:

```bash
COMMS_ACTOR=claude-dev comms note "FYI Prisma schema migration coming next session"
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

If `comms claim` exits 1, stop and surface the conflict to the user.
Do not run `--steal` unless the user confirms the prior session is dead.

If the user confirms takeover:

```bash
COMMS_ACTOR=claude-dev comms claim "src/foo.ts" --intent "<your intent>" --steal <claim-id> --reason "user verified prior session ended"
```

If the session is still active, choose another scope or leave a note:

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
