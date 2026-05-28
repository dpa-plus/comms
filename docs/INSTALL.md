# Installation & integration

## 1. Install the binary

Requires Go 1.22+.

```bash
go install github.com/dpa-plus/comms/cmd/comms@latest
```

This drops `comms` into `$GOBIN` (typically `~/go/bin/` or `~/.local/bin/`). Make sure that directory is on `$PATH`.

## 2. Manual / desktop-app use

`comms` requires `$COMMS_ACTOR` to be a **per-session** name (e.g. `claude-3a1f`, `codex-9b2c`, `human-eli`). A generic name shared across all your sessions (just `eli` or `claude`) is rejected by default because it breaks the conflict model — `comms check` would treat every other live agent's claim as "held by same actor" and wave through edits.

If you use Claude or Codex from desktop apps and want opt-in coordination,
do not install hooks and do not edit shell startup files. Instead, invoke the
`using-comms` skill explicitly and prefix every command:

```bash
COMMS_ACTOR=claude-20260527-a comms hello
COMMS_ACTOR=claude-20260527-a comms status
COMMS_ACTOR=claude-20260527-a comms claim "src/foo.ts" --intent "fix bug"
COMMS_ACTOR=claude-20260527-a comms release --latest --result "done"
```

Pick one actor per assistant conversation and reuse it until that conversation
ends.

The first active session is shown as the **leader** in the UI. This is not a
manager role and does not add protocol steps. It only lets that actor post
high-priority messages:

```bash
COMMS_ACTOR=claude-20260527-a comms note --priority "Everyone should know: stop touching aggregation until claim clears."
COMMS_ACTOR=claude-20260527-a comms find --priority decision "tracker overlay is the source of truth"
```

Priority notes/findings are pinned above normal notes/findings in status and
the dashboard.

To watch the repo state in a browser:

```bash
comms ui
```

Then open `http://127.0.0.1:7878`. The dashboard refreshes from the JSONL log
every two seconds. The log path is shown in the dashboard header and is
normally:

```text
~/Library/Application Support/comms/<repo-hash>/log.jsonl
```

Claims older than 90 minutes are highlighted as stale. That default fits
short-lived agent work: enough time for a real debugging pass, but short
enough to flag abandoned sessions quickly. To use a different threshold:

```bash
comms ui --stale-after 45m
```

If you start the UI with `COMMS_ACTOR` set, claim rows include a **Clear** or
**Release** button. Fresh claims use **Clear**; stale claims use **Release**.
The button does not delete or edit old JSONL rows. It appends a normal
`release` event with an audit reason, the same way
`comms release <id> --reason "..."` does. Clearing a fresh claim requires an
explicit UI confirmation and is recorded as a release event too.

The UI also includes forms for the rest of the day-to-day workflow:

- **Hello** appends a `hello` event for the UI actor.
- **Check Path** reports whether a file/scope is blocked by someone else's
  active claim.
- **Claim** appends a `claim` event, with optional steal ID + reason.
- **Release Mine** releases your latest claim or all claims held by the UI
  actor.
- **Note** appends a short `note`; priority notes are leader-only.
- **Finding** appends a categorized `finding`; priority findings are
  leader-only.
- **Doc** loads or saves `.comms/docs/<slug>.md`; saving also appends the
  normal `updated doc:<slug>` finding.

If the repo has no events yet, the log table will be empty. To preview the UI
with sample sessions, claims, findings, notes, docs, and raw events:

```bash
comms ui --demo
```

Demo mode is UI-only. It serves deterministic in-memory sample data and never
writes fake events to the real JSONL log.

## 3. Optional shell wrappers

If you launch Claude Code or Codex from a terminal and want automatic actor
injection, add this to `~/.zshrc` (or `~/.bashrc`):

```bash
# Wrap Claude Code to inject a fresh per-session actor every launch.
cc() {
  COMMS_ACTOR="claude-$(uuidgen | head -c 8 | tr A-Z a-z)" command claude "$@"
}

# Same for Codex.
cdx() {
  COMMS_ACTOR="codex-$(uuidgen | head -c 8 | tr A-Z a-z)" command codex "$@"
}

# Your raw shell sessions identify as a human.
export COMMS_ACTOR=human-$USER
```

Reload your shell, then run `cc` (instead of `claude`) and `cdx` (instead of `codex`).

**Why the wrappers and not a single global `COMMS_ACTOR=eli`:** if Claude, Codex, and the human shell all set `COMMS_ACTOR=eli`, `comms` cannot tell who's editing what. Per-session names are mandatory.

If you _really_ need to set a single generic name (for emergencies or one-off ops), set `COMMS_ALLOW_GENERIC_ACTOR=1` alongside it.

## 4. Optional Claude Code hooks

Only install hooks if you want `comms` to run automatically on every Claude
Code session and edit. Edit `~/.claude/settings.json` and merge the `hooks`
block from `examples/settings.json.snippet`:

```json
{
  "hooks": {
    "SessionStart": [
      {"type": "command", "command": "comms hello && comms status --since 24h"}
    ],
    "PreToolUse": [
      {"matcher": "Edit|Write", "type": "command", "command": "comms check --stdin-json"}
    ]
  }
}
```

The SessionStart hook announces the session and shows recent activity. The PreToolUse hook calls `comms check` with the tool input JSON on stdin — if another actor holds the file you're about to edit, the hook exits 1 and Claude sees the conflict report.

## 5. Claude Code skill

Drop `skills/using-comms/SKILL.md` into `~/.claude/skills/using-comms/SKILL.md`.
The bundled skill is intentionally manual: it activates only when you invoke
`using-comms` by name.

## 6. Per-repo AGENTS.md (for Codex + cross-runner consistency)

In each repo you want coordinated, place the contents of `examples/AGENTS.md.template` at the **top** of the repo's `AGENTS.md`. Codex reads `AGENTS.md` at session start and pays attention to top-of-file content.

## 7. Test on a fresh repo

```bash
mkdir -p ~/tmp/test-comms-repo
cd ~/tmp/test-comms-repo
git init && git commit --allow-empty -m init
COMMS_ACTOR=human-test comms hello
# Expect: "@human-test registered. ..." on the first line.

ls .comms/
# Expect: policy.txt, docs/, .gitignore

comms claim "src/test.ts" --intent "first claim"
# Expect: exit 0, prints claim ID.

comms status
# Expect: 1 active claim by @human-test.
```

## What lives where

| Location                                                                 | Contents                                                                                |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `~/.local/bin/comms` (or `~/go/bin/comms`)                               | The binary.                                                                             |
| `~/.claude/skills/using-comms/SKILL.md`                                  | Optional manual Claude Code skill.                                                      |
| `~/.claude/settings.json` (hooks block)                                  | Optional SessionStart + PreToolUse hooks.                                                |
| `~/.zshrc` (cc / cdx functions + export)                                 | Optional terminal-launch actor injection.                                                |
| `<repo>/.comms/policy.txt`                                               | Risky-files list (committed).                                                           |
| `<repo>/.comms/docs/*.md`                                                | The wiki (committed).                                                                   |
| `<repo>/.comms/.gitignore`                                               | Ignores `docs/.*.lock` sidecar files.                                                   |
| `<repo>/AGENTS.md`                                                       | Top-of-file COMMS PROTOCOL block (committed; for Codex).                                |
| `~/Library/Application Support/comms/<repo-hash>/log.jsonl`              | The event log (per machine, NOT in iCloud).                                             |
| `~/Library/Application Support/comms/<repo-hash>/.lock`                  | The per-repo flock target.                                                              |

## Updating

```bash
go install github.com/dpa-plus/comms/cmd/comms@latest
```

There's no migration step — the on-disk format is stable.
