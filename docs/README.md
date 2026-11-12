# comms documentation

- [`README.md`](../README.md): what comms is, how to install it, and the commands.
- [`INSTALL.md`](INSTALL.md): install, `comms doctor`, the commit guard, Codex setup, upgrade and uninstall.
- [`DASHBOARD.md`](DASHBOARD.md): the board, running it at login, and what an upgrade does to it.
- [`DESIGN.md`](DESIGN.md): why comms is built this way, and what changed in 0.5.0.
- [`PROTOCOL.md`](PROTOCOL.md): the log format, identity, storage, the hook contract and the board's HTTP API.

The agent skill ships inside the package at
[`python/comms_graph/data/using-comms/SKILL.md`](../python/comms_graph/data/using-comms/SKILL.md).
`comms doctor --fix` copies it for Claude Code and Codex. The per-repository
instructions for Codex are in [`examples/AGENTS.md.template`](../examples/AGENTS.md.template).
