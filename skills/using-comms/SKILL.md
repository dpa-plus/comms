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

In desktop app sessions, prefix every command with a concrete actor:

```bash
COMMS_ACTOR=claude-20260527-a comms status
COMMS_ACTOR=codex-20260527-a comms status
```

Pick one actor name when this skill starts and reuse it for the conversation.
Do not use generic names like `eli`, `claude`, `codex`, `agent`, or `user`.

## Session Start

```bash
COMMS_ACTOR=claude-20260527-a comms hello
COMMS_ACTOR=claude-20260527-a comms status
```

Mention the chosen actor in your reply so the user can see it.

## Operator UI

The user may run `COMMS_ACTOR=human-eli comms ui` to watch the whole project
coordination window. The UI has **Start Comms Session** and **End Comms
Session** controls and a **Session Event Log** selector for current vs archived
session logs.

Agents still use the CLI for coordination. Do not click UI controls or call the
UI mutation endpoints unless the user explicitly asks. If asked to inspect the
UI backend, use:

```bash
curl -fsS http://127.0.0.1:7878/api/status
```

The backend advertises `actions`, `current_session.events`, and
`comms_sessions[].events`; those are filtered views over the append-only JSONL
log.

## Claim Before Edits

Before editing a file in a coordinated project:

```bash
COMMS_ACTOR=claude-20260527-a comms claim "frontend/src/lib/aggregate.ts" --intent "fix lead double-counting"
```

Use narrower anchors when practical:

```bash
COMMS_ACTOR=claude-20260527-a comms claim "frontend/src/lib/aggregate.ts#L40-90" --intent "rewrite aggregation loop"
COMMS_ACTOR=claude-20260527-a comms claim "src/auth.ts#validateToken" --intent "tighten JWT expiry check"
```

## Release

```bash
COMMS_ACTOR=claude-20260527-a comms release --latest --result "PR #321 merged"
COMMS_ACTOR=claude-20260527-a comms release --all-mine --result "switching tasks"
```

## Findings

```bash
COMMS_ACTOR=claude-20260527-a comms find fix "leads sourced only from tracker overlay" --ref path:frontend/src/lib/aggregate.ts
COMMS_ACTOR=claude-20260527-a comms find decision "tracker is source of truth for leads" --ref doc:lead-counting
COMMS_ACTOR=claude-20260527-a comms find gotcha "META_TOKEN_ENC_KEY is immutable after first deploy" --ref path:src/crypto.ts
COMMS_ACTOR=claude-20260527-a comms find bug "tracker rows duplicated when Meta sync runs more than once per hour"
COMMS_ACTOR=claude-20260527-a comms find ship "v1.4 deployed to develop" --ref pr:#321
```

Category cheat sheet:

- `bug` means an open problem.
- `fix` means a resolved problem.
- `ship` means released or deployed.
- `decision` means an architectural choice.
- `gotcha` means a persistent trap future agents should remember.

Use `comms note` for short FYIs that are not persistent decisions:

```bash
COMMS_ACTOR=claude-20260527-a comms note "FYI Prisma schema migration coming next session"
```

## Docs

```bash
COMMS_ACTOR=claude-20260527-a comms doc --list
COMMS_ACTOR=claude-20260527-a comms doc tracker-architecture
COMMS_ACTOR=claude-20260527-a comms doc tracker-architecture --edit
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
COMMS_ACTOR=claude-20260527-a comms lesson verify-data-before-ui --edit
```

## Conflict Handling

If `comms claim` exits 1, stop and surface the conflict to the user.
Do not run `--steal` unless the user confirms the prior session is dead.

If the user confirms takeover:

```bash
COMMS_ACTOR=claude-20260527-a comms claim "src/foo.ts" --intent "<your intent>" --steal <claim-id> --reason "user verified prior session ended"
```

If the session is still active, choose another scope or leave a note:

```bash
COMMS_ACTOR=claude-20260527-a comms note "@claude-3a1f can I take src/foo.ts when you're done?"
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
