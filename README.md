# comms

Lightweight multi-agent coordination CLI: per-session claims, JSONL event log, `.comms/docs` wiki.

`comms` is the third generation of multi-agent coordination at DPA+. It learned from:
- **`mcp-agent-mail`** (heavy MCP server, severity ladders, 7 identities) — too much ceremony; agents kept forgetting protocol steps.
- **A 1632-line `COMMS.md` append-only markdown** — worked OK but grew unbounded, no targeted reads, agents had to remember to update it, iCloud sync forked the file.

`comms` is the small version. A compact CLI, 5 event types, JSONL log + `flock`, and opt-in Claude/Codex skills.

The first active session is shown as the repo's lightweight **leader**. The
leader has one extra privilege: posting `--priority` notes/findings that pin
to the top of `status` and `ui`. It does not assign work or add ceremony.

## Quick start

```bash
# Install
go install github.com/dpa-plus/comms/cmd/comms@latest

# Manual / desktop-app use: prefix commands with a concrete actor.
COMMS_ACTOR=codex-dev comms hello --label "Codex Dev"
COMMS_ACTOR=codex-dev comms session start "dashboard fixes" --label "Codex Dev"
COMMS_ACTOR=codex-dev comms status
COMMS_ACTOR=codex-dev comms claim "src/foo.ts" --intent "fix bug"
COMMS_ACTOR=codex-dev comms note --priority "Everyone should know: stop editing aggregation until the current claim clears."
```

See `docs/INSTALL.md` for manual and optional automated setup,
`docs/PROTOCOL.md` for the event schema, `docs/DESIGN.md` for the why.

## Commands at a glance

```
comms hello [<name>] [--label "Claude Dev"]   # session entry + friendly UI label
comms session start "<name>" [--label "..."]  # create + join a named comms session
comms session join "<name>" [--label "..."]   # join an existing named comms session
comms session end "<name>" [--reason "..."]   # archive one named session + release its claims
comms claim "<scope>" --intent "<text>" [--steal <id> --reason "<text>"]
comms release [<id>|--latest|--all-mine] [--result "<text>"]
comms session retire <actor> [--reason "..."] # remove actor from active roster; releases its claims
comms session lead [<actor>] [--reason "..."] # make exactly one active actor the leader
comms check <path>                            # PreToolUse hook (also: --stdin-json)
comms status [--json]
comms log [--actor X] [--since 1h] [--scope path] [--type list] [--category cat]
comms note [--priority] "<≤200-char FYI>"
comms find [--priority] <bug|fix|ship|decision|gotcha> "<summary>" [--ref kind:value ...]
comms doc --list                              # wiki: list slugs
comms doc <slug>                              # wiki: print
comms doc <slug> --edit                       # wiki: open $EDITOR under sidecar flock
comms lesson --list                           # global lessons: list slugs
comms lesson <slug>                           # global lessons: print
comms lesson <slug> --edit                    # global lessons: edit under sidecar flock
comms ui [--demo] [--all] [--stale-after 90m] [--addr 127.0.0.1:7878] # local dashboard
```

The dashboard is for watching repo state. Agents still perform normal work via
the CLI/skill. When started with `COMMS_ACTOR`, the UI can start multiple named
sessions, show each session's actors/claims/events separately, and end one
selected named session when that project window is done. Events stay in the
same append-only per-repo JSONL file, but every joined actor's claims, notes,
findings, and releases carry `comms_session_id`/`comms_session_name` metadata so
session data does not mix in the UI or API. If an actor starts or joins a
different named session, comms releases that actor's prior-session claims first;
claims do not follow the actor into the new session. Demo mode remains
read-only.

Use `comms ui --all` for a read-only portfolio dashboard across every repo log
under the comms data directory. Per-repo logs stay separate on disk; the global
view prefixes session names with the project.

Use stable, readable actors for desktop app work, for example `claude-dev` and
`codex-dev`, plus `--label "Claude Dev"` / `--label "Codex Dev"` for UI display.
If an agent accidentally registers a throwaway actor, retire it instead of
editing the log:

```bash
COMMS_ACTOR=claude-dev comms session retire claude-7e4c --reason "renamed to claude-dev"
COMMS_ACTOR=claude-dev comms session lead --reason "user asked Claude Dev to lead"
```

Global lessons are carefully curated cross-project operating knowledge for
agents. They live under the user's comms data directory, not in any one repo.
Project docs remain under `.comms/docs`.

## License

Apache-2.0.
