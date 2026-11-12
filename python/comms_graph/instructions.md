### Parallel agents

Another agent may be editing this repository right now. Claim ground before you
edit it, and release it when you are done:

```
comms-graph claim <path>[#symbol|#L20-48] --as <your-name> --intent "..."
comms-graph release <path> --as <your-name>
comms-graph board
```

Read the reply. Its parts are not the same kind of thing, and treating them
alike is the main way this gets misused:

- **CLAIM CONFLICT** blocks, and exits 1. This is exact, read straight from the
  log: somebody holds overlapping ground and nothing was recorded for you.
  Narrow your claim, or agree with them who takes it.
- **SAME GROUND / NEARBY** is advisory. A prompt to look before you start: never
  a conflict, never a reason to wait, and never an instruction about what order
  to work in.
- **NOT ON THE MAP** means the name you typed matched nothing in the code map.
  Your claim still stands; check the spelling, or rebuild the map with
  `graphify extract . --code-only`.

If a claim is blocking you and its holder is plainly gone: no events for hours,
a session that died: you may take it:

```
comms-graph claim <path> --as <your-name> --steal <claim-id> --reason "..."
```

It needs the exact id and a reason, both recorded under your name. Use it when
somebody left, not when somebody is slow: a claim held by an agent that is still
working is a conversation, not an obstacle.

No warning is not proof you are working alone. The map misses roughly a third of
the file pairs that really do get changed together, and of the pairs it does
flag, well under half turn out to matter. Claim anyway.

If an edit of yours is refused, that refusal is the tool working. Do not reach
for another way to make the same edit: a different tool, a shell command, a
changed actor name. Say what you were blocked on and stop.

### Notes

To tell the other agents something, leave a note. Start it with @name to
address one agent.

```
comms-graph note "schema migration lands next session" --as <you>
```

Exit status everywhere: **0** recorded, **1** the answer is no, **2** it did not
work.
