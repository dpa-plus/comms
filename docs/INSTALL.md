# Installation & integration

## 1. Install the binary

Requires Go 1.22+.

```bash
go install github.com/dpa-plus/comms/cmd/comms@latest
```

This drops `comms` into `$GOBIN` (typically `~/go/bin/` or `~/.local/bin/`). Make sure that directory is on `$PATH`.

## 2. Set up per-session actor identities

`comms` requires `$COMMS_ACTOR` to be a **per-session** name (e.g. `claude-3a1f`, `codex-9b2c`, `human-eli`). A generic name shared across all your sessions (just `eli` or `claude`) is rejected by default because it breaks the conflict model — `comms check` would treat every other live agent's claim as "held by same actor" and wave through edits.

Add this to `~/.zshrc` (or `~/.bashrc`):

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

## 3. Claude Code hooks

Edit `~/.claude/settings.json`. Merge the `hooks` block from `examples/settings.json.snippet`:

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

## 4. Claude Code skill (recommended)

Drop `skills/using-comms/SKILL.md` into `~/.claude/skills/using-comms/SKILL.md`. The skill gives Claude the 3-rule contract + conflict-handling protocol + category cheat sheet.

## 5. Per-repo AGENTS.md (for Codex + cross-runner consistency)

In each repo you want coordinated, place the contents of `examples/AGENTS.md.template` at the **top** of the repo's `AGENTS.md`. Codex reads `AGENTS.md` at session start and pays attention to top-of-file content.

## 6. Test on a fresh repo

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
| `~/.claude/skills/using-comms/SKILL.md`                                  | Claude Code agent contract.                                                             |
| `~/.claude/settings.json` (hooks block)                                  | SessionStart + PreToolUse hooks.                                                        |
| `~/.zshrc` (cc / cdx functions + export)                                 | Per-session actor injection.                                                            |
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
