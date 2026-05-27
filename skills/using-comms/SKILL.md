---
name: using-comms
description: Multi-agent coordination via the `comms` CLI. Use BEFORE any Edit/Write tool call. Triggers — "before I edit", "claim this", "what is X working on", "release my claim", "found a bug", "decided to use", "session ended", "is this file claimed".
---

# Using `comms`

`comms` coordinates parallel coding sessions (Claude, Codex, human shells) via per-session exclusive claims and a JSONL event log. Use it whenever you're working in a repo that contains `.comms/`.

## The 3-rule contract

1. **Claim before edit.** Before any `Edit` or `Write` tool call on a file not already claimed by you this session, run `comms claim "<path>[#anchor]" --intent "<reason>"`. The PreToolUse hook does this for you automatically — but you can claim explicitly when you know in advance what you'll touch.
2. **Release when done.** When you finish a task (PR merged, fix committed, decision recorded), run `comms release --result "<outcome>"` or `comms release --latest`.
3. **Surface conflicts.** If a claim or check exits 1, the stderr names the other actor + the exact next-command. Show that to the user; never `--steal` without their go-ahead.

## Examples

### Claim a single file

```bash
comms claim "frontend/src/lib/aggregate.ts" --intent "fix lead double-counting"
```

### Claim with a line-range anchor (lighter conflict surface)

```bash
comms claim "frontend/src/lib/aggregate.ts#L40-90" --intent "rewrite the for-loop"
```

### Claim with a symbol anchor

```bash
comms claim "src/auth.ts#validateToken" --intent "tighten the JWT exp check"
```

### Release

```bash
comms release --latest --result "PR #321 merged"
```

### Record a finding (5 categories)

```bash
comms find fix "leads sourced only from tracker overlay" --ref path:frontend/src/lib/aggregate.ts --ref commit:cece752
comms find decision "tracker is source of truth for leads" --ref doc:lead-counting
comms find gotcha "META_TOKEN_ENC_KEY is immutable after first deploy" --ref path:backend/src/crypto.ts
comms find bug "Tracker rows duplicated when Meta sync runs >1×/hour" --ref path:backend/src/meta-sync.ts
comms find ship "v1.4 deployed to develop" --ref pr:#321
```

Category cheat sheet:

- `bug` — open problem, needs fixing
- `fix` — problem just resolved
- `ship` — now in production / released
- `decision` — architectural choice worth remembering
- `gotcha` — non-obvious trap; persistent reminder for future agents

Rule of thumb: ephemeral chatter = `note`, persistent-gotcha = `gotcha`, design-choice = `decision`.

### Quick FYI (no decision required)

```bash
comms note "FYI Prisma schema migration coming next session"
```

### Docs (the wiki)

```bash
comms doc --list                              # see all slugs
comms doc tracker-architecture                # print one
comms doc tracker-architecture --edit         # open $EDITOR under sidecar flock
```

## Conflict-handling protocol

When `comms claim` exits 1, stderr looks like:

```
BLOCKED: src/foo.ts is claimed.
  Holder:  @claude-3a1f
  Claim:   01HZK4...
  Intent:  "fix lead double-counting"
  Since:   2026-05-22T09:14:00Z (5h 12m ago)

Surface this to the user. Ask whether @claude-3a1f's session is still active.

If user confirms the prior session ended:
  comms claim "src/foo.ts" --intent "<your-intent>" --steal 01HZK4 --reason "user verified prior session ended"

If session is still active:
  Choose a different scope, or `comms note "@claude-3a1f can I take this when you're done?"`
```

Your job:

1. **Stop. Don't `--steal` reflexively.** Read the holder + intent + since.
2. **Ask the user**: "Another session (@claude-3a1f, claimed 5h ago to 'fix lead double-counting') holds this scope. Is that session still active?"
3. **If user says it's dead**, run the literal `--steal` command from the stderr. Use a `--reason` that quotes the user's confirmation.
4. **If user says it's alive**, pick a different scope OR `comms note "@claude-3a1f can I take this when you're done?"` and switch to another task.

## Failure modes

- **Exit 1 (blocked)** = block the edit, surface to user.
- **Exit 2 (system error: broken log, missing dir, etc.)** = log a warning to the user but do NOT block. A broken `comms` should not brick all editing.

## Hard rule

**Before any Edit or Write tool call on a file not already claimed by you this session, run `comms claim`.**
