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
- **SAME GROUND / NEARBY** is advisory. A prompt to look before you start — never
  a conflict, never a reason to wait, and never an instruction about what order
  to work in.
- **NOT ON THE MAP** means the name you typed matched nothing in the code map.
  Your claim still stands; check the spelling, or rebuild the map with
  `graphify extract . --code-only`.

If a claim is blocking you and its holder is plainly gone — no events for hours,
a session that died — you may take it:

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
for another way to make the same edit — a different tool, a shell command, a
changed actor name. Say what you were blocked on and stop.

### What you learned

When you settle something the next agent would otherwise re-decide, or hit a trap
they would fall into, write it down. It is the only part of your reasoning that
outlives your session.

```
comms-graph find decision "uuid PKs everywhere, never cuid" --as <you> --ref path:src/db.ts
comms-graph find gotcha "the encryption key is immutable after first deploy" --as <you>
comms-graph note "schema migration lands next session" --as <you>
```

`bug` is open, `fix` is closed, `ship` is out, `decision` is why, `gotcha` is what
will bite. A note is the throwaway version — say it and forget it. A finding is
the one somebody reads six weeks from now, so write the reason, not the summary.

### Work that is planned

If this project uses the task graph, these are the verbs. Ask first, so you pick
up something that is actually startable:

```
comms-graph next --as <your-name>       what you could start right now
comms-graph brief <task-id>             what it is, and what came before it
```

`brief` is the one worth running before you write anything. For every task yours
comes after, it prints what yours CONSUMES from it and the decisions recorded
while it was built. That is how the reasoning behind the last piece reaches you,
and there is nowhere else it is written down.

Tag your claim with the task so the board knows who is on what — it is derived
from live claims, so releasing the file clears it for you:

```
comms-graph claim <path> --as <your-name> --task <task-id> --intent "..."
```

When you finish, submit it, reporting every check the task declared:

```
comms-graph task done <task-id> --as <your-name> --check test=pass \
    --note "what you decided and why"
```

Write the note. It is what `brief` hands to whoever picks up the next task, and
a decision you leave in your own head is one the next agent has to guess at.

**A task does not unblock what follows it until somebody ELSE verifies it.**
That is the whole point, so:

```
comms-graph task review <task-id> --as <your-name> --pass|--fail --evidence "..."
```

Write the evidence on a pass too, not only on a fail. It is the only record of
HOW the thing was checked, and `brief` hands it to whoever builds on the task
next. Without it they read "closed" and cannot tell a real check from a glance.

- You cannot verify work you submitted. Not under a different name, not under a
  role suffix — the check is on the process, not the spelling.
- You CAN reject your own work. Saying "this is not ready" needs no second
  opinion.
- If `next` offers you something to review, clear it before starting new work.
  Anything waiting on review is blocking every task after it.
- If two agents have both worked a task, NEITHER can pass it and only somebody
  who has not touched it can. If there genuinely is nobody else, add
  `--acknowledge-self-review`: it records the sign-off as yours, permanently,
  and the board shows it as "signed off by themselves" rather than verified.
  Reach for it last, not first.

Exit status everywhere: **0** recorded, **1** the answer is no, **2** it did not
work.
