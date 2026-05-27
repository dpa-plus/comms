# comms

Lightweight multi-agent coordination CLI: per-session claims, JSONL event log, `.comms/docs` wiki.

`comms` is the third generation of multi-agent coordination at DPA+. It learned from:
- **`mcp-agent-mail`** (heavy MCP server, severity ladders, 7 identities) — too much ceremony; agents kept forgetting protocol steps.
- **A 1632-line `COMMS.md` append-only markdown** — worked OK but grew unbounded, no targeted reads, agents had to remember to update it, iCloud sync forked the file.

`comms` is the small version. ~9 commands, 5 event types, JSONL log + `flock`, a 80-line Claude Code skill, a 3-line `AGENTS.md` block for Codex.

## Quick start

```bash
# Install
go install github.com/dpa-plus/comms/cmd/comms@latest

# Set up shell wrappers (one-time, in ~/.zshrc)
cc()  { COMMS_ACTOR="claude-$(uuidgen | head -c 8 | tr A-Z a-z)" command claude "$@"; }
cdx() { COMMS_ACTOR="codex-$(uuidgen  | head -c 8 | tr A-Z a-z)" command codex  "$@"; }
export COMMS_ACTOR=human-$USER

# Now use `cc` instead of `claude`, `cdx` instead of `codex`. Each launch
# gets a fresh per-session actor that propagates through the process tree.
```

See `docs/INSTALL.md` for full setup, `docs/PROTOCOL.md` for the event schema, `docs/DESIGN.md` for the why.

## Commands at a glance

```
comms hello [<name>]                          # session entry
comms claim "<scope>" --intent "<text>" [--steal <id> --reason "<text>"]
comms release [<id>|--latest|--all-mine] [--result "<text>"]
comms check <path>                            # PreToolUse hook (also: --stdin-json)
comms status [--json]
comms log [--actor X] [--since 1h] [--scope path] [--type list] [--category cat]
comms note "<≤200-char FYI>"
comms find <bug|fix|ship|decision|gotcha> "<summary>" [--ref kind:value ...]
comms doc --list                              # wiki: list slugs
comms doc <slug>                              # wiki: print
comms doc <slug> --edit                       # wiki: open $EDITOR under sidecar flock
```

## License

Apache-2.0.
