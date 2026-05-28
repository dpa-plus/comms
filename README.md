# comms

Lightweight multi-agent coordination CLI: per-session claims, JSONL event log, `.comms/docs` wiki.

`comms` is the third generation of multi-agent coordination at DPA+. It learned from:
- **`mcp-agent-mail`** (heavy MCP server, severity ladders, 7 identities) — too much ceremony; agents kept forgetting protocol steps.
- **A 1632-line `COMMS.md` append-only markdown** — worked OK but grew unbounded, no targeted reads, agents had to remember to update it, iCloud sync forked the file.

`comms` is the small version. 10 commands, 5 event types, JSONL log + `flock`, a 80-line Claude Code skill, a 3-line `AGENTS.md` block for Codex.

The first active session is shown as the repo's lightweight **leader**. The
leader has one extra privilege: posting `--priority` notes/findings that pin
to the top of `status` and `ui`. It does not assign work or add ceremony.

## Quick start

```bash
# Install
go install github.com/dpa-plus/comms/cmd/comms@latest

# Manual / desktop-app use: prefix commands with a concrete per-session actor.
COMMS_ACTOR=codex-20260527-a comms hello
COMMS_ACTOR=codex-20260527-a comms status
COMMS_ACTOR=codex-20260527-a comms claim "src/foo.ts" --intent "fix bug"
COMMS_ACTOR=codex-20260527-a comms note --priority "Everyone should know: stop editing aggregation until the current claim clears."
```

See `docs/INSTALL.md` for manual and optional automated setup,
`docs/PROTOCOL.md` for the event schema, `docs/DESIGN.md` for the why.

## Commands at a glance

```
comms hello [<name>]                          # session entry
comms claim "<scope>" --intent "<text>" [--steal <id> --reason "<text>"]
comms release [<id>|--latest|--all-mine] [--result "<text>"]
comms check <path>                            # PreToolUse hook (also: --stdin-json)
comms status [--json]
comms log [--actor X] [--since 1h] [--scope path] [--type list] [--category cat]
comms note [--priority] "<≤200-char FYI>"
comms find [--priority] <bug|fix|ship|decision|gotcha> "<summary>" [--ref kind:value ...]
comms doc --list                              # wiki: list slugs
comms doc <slug>                              # wiki: print
comms doc <slug> --edit                       # wiki: open $EDITOR under sidecar flock
comms ui [--demo] [--stale-after 90m] [--addr 127.0.0.1:7878] # local dashboard
```

The dashboard shows status/log data and, when started with `COMMS_ACTOR`, can
append the same normal events as the CLI: hello, check, claim, release, note,
finding, and doc updates. Demo mode remains read-only.

## License

Apache-2.0.
