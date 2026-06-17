# Installation & integration

## 1. Install the binary

Requires Go 1.25+.

```bash
go install github.com/dpa-plus/comms/cmd/comms@latest
```

This drops `comms` into `$GOBIN` (typically `~/go/bin/` or `~/.local/bin/`). Make sure that directory is on `$PATH`.

## 2. Manual / desktop-app use

`comms` requires `$COMMS_ACTOR` to be a concrete actor name. For desktop-app
work, prefer stable readable actors per live assistant role, such as
`claude-dev`, `codex-dev`, and `human-eli`. A generic name shared across all
your sessions (just `eli` or `claude`) is rejected by default because it breaks
the conflict model — `comms check` would treat every other live agent's claim
as "held by same actor" and wave through edits.

If you use Claude or Codex from desktop apps and want opt-in coordination,
do not install hooks and do not edit shell startup files. Instead, invoke the
`using-comms` skill explicitly and prefix every command:

```bash
COMMS_ACTOR=claude-dev comms hello --label "Claude Dev"
COMMS_ACTOR=claude-dev comms status
COMMS_ACTOR=claude-dev comms claim "src/foo.ts" --intent "fix bug"
COMMS_ACTOR=claude-dev comms release --latest --result "done"
```

Pick one actor per assistant conversation and reuse it until that conversation
ends.

If an agent accidentally registers a throwaway actor name, do not edit the
JSONL log. Append an audit event that retires it from the active roster:

```bash
COMMS_ACTOR=claude-dev comms session retire claude-7e4c --reason "renamed to claude-dev"
```

Retiring an actor removes it from **Active Sessions** and releases any active
claims it still held. The old historical rows remain in the append-only log and
in archived session analysis.

The first active session is shown as the **leader** in the UI. This is not a
manager role and does not add protocol steps. It only lets that actor post
high-priority messages:

```bash
COMMS_ACTOR=claude-dev comms note --priority "Everyone should know: stop touching aggregation until claim clears."
COMMS_ACTOR=claude-dev comms find --priority decision "tracker overlay is the source of truth"
```

Priority notes/findings are pinned above normal notes/findings in status and
the dashboard.

To make a different active actor the leader:

```bash
COMMS_ACTOR=human-eli comms session lead claude-dev --reason "user asked Claude Dev to lead"
```

To watch the repo state in a browser:

```bash
comms ui
```

Then open `http://127.0.0.1:7878`. The dashboard streams live updates over
Server-Sent Events: a file watcher in the `comms ui` process detects each change
to the JSONL log and pushes a fresh snapshot to the browser instantly, so there
is no polling. The log path is shown in the dashboard header and is normally:

```text
~/Library/Application Support/comms/<repo-hash>/log.jsonl
```

`comms ui` is **unified by default**: a single dashboard across every project
that has a comms log. It scans `~/Library/Application Support/comms/*/repo-path.txt`
and `log.jsonl` and lists each project in a sidebar. Scope to one repo with
`comms ui --repo /path/to/repo`.

### macOS Desktop/Document permission recovery

If Claude/Codex reports `repo: getwd: operation not permitted`,
`uv_cwd operation not permitted`, or `fatal: Unable to read current working
directory` while the repo lives under Desktop, Documents, or Downloads, the repo
is usually not broken. The app process has lost macOS privacy access to that
protected folder.

Preferred fix for long-running/background projects: move the repo outside the
protected folder, for example:

```bash
mkdir -p ~/code
mv ~/Desktop/Projects/my-project ~/code/my-project
```

Then reopen the agent in `/Users/you/code/my-project`.

Short-term recovery: run `comms` from a readable directory and point it at the
repo explicitly:

```bash
cd /tmp
COMMS_ACTOR=claude-dev comms --repo /Users/you/code/my-project status

export COMMS_REPO=/Users/you/code/my-project
COMMS_ACTOR=claude-dev comms session join "my-project" --label "Claude Dev"
```

`--repo` bypasses cwd and git discovery by walking the supplied path upward to
`.git`. If the app has no read access to that absolute path at all, grant Full
Disk Access to the app and restart it, or move the repo to `~/code`.

Claims idle longer than 1 hour are highlighted as stale (and become stealable
without confirmation). That default fits short-lived agent work: enough time for
a real debugging pass, but short enough to flag abandoned sessions quickly. To
use a different display threshold:

```bash
comms ui --stale-after 45m
```

The UI is intentionally not a replacement for the CLI. Agents still use
`comms session start`, `comms session join`, `claim`, `release`, `note`,
`find`, and `doc` themselves. The UI is for watching the repo, starting named
communication windows, switching between their logs, and ending the selected
named session when you are done with it.

If you start the UI with `COMMS_ACTOR` set, the header includes **Start Comms
Session** and **End Comms Session** buttons. **Start Comms Session** asks for a
name and appends a normal `hello` event with `comms_session_start=true`,
`comms_session_id`, and `comms_session_name`. Agents can join the same named
session with:

```bash
COMMS_ACTOR=claude-dev comms session join "dashboard fixes" --label "Claude Dev"
COMMS_ACTOR=codex-dev comms session join "dashboard fixes" --label "Codex Dev"
```

An agent can also create the named session directly:

```bash
COMMS_ACTOR=claude-dev comms session start "dashboard fixes" --label "Claude Dev"
```

Use **End Comms Session** when the selected named work window is over. It
appends one normal `release` event with `comms_session_end=true`, releases
claims tagged to that named session, clears actors joined to that named session,
and adds a **Comms Session Archive** summary for later analysis. It does not
delete old JSONL rows.

Every named session keeps its own `comms_session_id` in the events it owns, so
multiple sessions can be active in the same repo without their claims, notes,
findings, or event logs mixing. Later analysis can read the JSONL log directly
and reconstruct the full history, while the UI shows compact counts: actors,
events, claims, findings, notes, released refs, end time, and reason.

One actor can only be in one named session at a time. If that actor starts or
joins another named session, comms first releases that actor's active claims
from the old session with an audit event, then registers the actor in the new
session.

The **Session Event Log** selector shows logs per communication session. It is
not a separate file per session; it is a filtered view over the same append-only
`log.jsonl`, so the audit trail stays complete while the UI avoids mixing old
sessions into the current one.

Global lessons live outside any single project:

```text
~/Library/Application Support/comms/global/lessons/*.md
```

Use them for carefully curated cross-project agent operating knowledge:

```bash
comms lesson --list
comms lesson verify-data-before-ui
COMMS_ACTOR=human-eli comms lesson verify-data-before-ui --edit
```

Project docs (`comms doc`) explain this repo. Global lessons (`comms lesson`)
explain durable patterns that should apply across repos. Add lessons rarely:
only when the user explicitly asks or approves a leader's proposed lesson.

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
