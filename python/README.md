# comms — coordination for parallel coding agents

This is a fork of [graphify](README.md). Graphify turns a folder of code into a
queryable knowledge graph. This fork adds one thing on top of it: **a way for
several agents working in the same checkout at the same time to find out who is
already on the ground they are about to edit, and whose work sits next to
theirs.**

That is the whole feature. It is smaller than it sounds, on purpose, and the
next section says exactly how much smaller — read it before you decide whether
this is useful to you.

---

## Read this first: what it will not do

These are measured limits, not modesty. They are printed in the tool's own
output too, because a limit you cannot see is a limit that misleads you. The
measurements behind them are in [docs/COMMS.md](docs/COMMS.md).

**It will never tell you who goes first.** The original idea was that the graph
could derive order — if job A changes something job B leans on, A goes first.
That was checked against three months of real changes on two projects. It was
right about as often as a coin flip on both, and on one project the real work
went the *other* way round most of the time. It was cut. No arrow this tool
draws will ever block anything on its own. Order is not printed, not hinted at,
not computed.

**No warning does not mean you are working alone.** Between a third and a half
of the file pairs that really do get changed together are invisible to the map.
Silence is not evidence.

**A warning is a prompt to look, never a verdict.** Of the pairs the map does
flag, well under half turn out to be things that really get changed together.
Expect to look and shrug, often.

**It needs small, specific claims.** Claim everything you touch and the board
turns to mush — measured, not guessed. Three named scopes is the cap; a job
needing more has to be split.

**A mistyped name finds nothing, and that looks exactly like code nothing else
touches.** In testing, a quarter of names typed from memory named something that
does not exist. So a miss is announced out loud, with the reason and a list of
what the file actually contains — it is never rendered as "no connections".

**The connection only knows what the map last read.** A claim pointing at code
that changed since the last `graphify extract` is reported as out of date, not
as truth.

**It fits projects shaped like the one it was built for.** On a large,
loosely-connected codebase, narrow claims produce almost no connections at all —
an empty board that looks calm because it knows nothing.

If what you wanted was a scheduler, a dependency-ordered plan, or a gate that
stops the wrong agent from starting, this is not that, and deliberately so.

---

## Try it

One command, in any git repo:

```
graphify comms claim src/parser.py --as agent-a --intent "rewrite tokenizer"
```

That is the whole interface for the common case. `--as` is required (two agents
sharing one name cannot detect a conflict between them; set `COMMS_ACTOR` to
avoid typing it). You get the contact answer immediately, in the same breath as
the claim.

For the *nearby* half of the answer to work, the repo needs a map:

```
graphify extract . --code-only    # builds graphify-out/graph.json
```

`--code-only` is what comms needs and it runs entirely locally — no API key.
Without it, `extract` also tries to read prose files for semantic meaning, so a
repo containing so much as a README fails with "no LLM API key found" and writes
no graph at all.

Without one, claims still record and still block conflicts — you just get told
plainly that no contact check was possible.

Full surface:

```
graphify comms claim <scope> --as <actor> [--intent "..."]
graphify comms release <scope|claim-id> --as <actor> [--result "..."]
graphify comms board [--as <actor>]
```

`<scope>` is a path, optionally anchored: `src/foo.py`, `src/foo.py#parse`, or
`src/foo.py#L20-48`.

Exit status: `0` recorded, `1` blocked by somebody else's claim, `2` bad usage.

---

## What the reply means

There are four replies and they are four different kinds of thing. Mixing them
up is the main way a coordination tool becomes noise, so they are kept visibly
apart. All output below is real, from a two-file demo repo.

### 1. Nothing near you — which is not the same as nothing there

```
CLAIMED src/parser.py by @agent-a  (id 01M0FETQ5AFGADGWZK4Q6SRPQF)
  nobody else is on this or next to it — though the map misses roughly a third
  of real couplings, so this is not a guarantee
```

The caveat is part of the message, not a footnote. Silence from the map is weak
evidence, and the message says so every single time.

### 2. Nearby — advisory, and it never blocks

```
CLAIMED src/report.py by @agent-b  (id 01M0FETQ88P1D1KTXE5N0TD1TM)
  NEARBY — worth a look before you start, not a conflict:
    report.py — [imports] extracted→ parse(), held by @agent-a (src/parser.py)
    report.py — [imports_from] extracted→ parser.py, held by @agent-a (src/parser.py)
    (the map flags more pairs than really change together; and it misses some)
```

This is the contact warning. It says: somebody else's claim is connected to
yours in the graph, here is the edge, here is who holds it. Exit status is
still `0`. Nothing is held up. It is not an ordering, and there is no permission
to wait for — go and look, or go and talk to them, or carry on.

### 3. Claim conflict — this one is exact, and it does block

```
CLAIM CONFLICT — not recorded. Somebody else already holds this ground.
  you asked for: src/parser.py
  @agent-a holds src/parser.py since 2026-08-20T11:27:47.370446Z  "rewrite tokenizer"
  This one is exact, not a guess: it is read out of the log.
  Claim something narrower, or agree with them who takes it.
```

Exit `1`, and nothing was recorded. This is read straight out of the log, not
inferred from the graph, so it carries none of the uncertainty above. That is
why it is allowed to block and the contact warning is not.

### 4. Not on the map — announced, never silent

```
CLAIMED src/parser.py#tokenise by @agent-d  (id 01M0FEYR8GNXXRAKYKNF0GG78A)
  NOT ON THE MAP — the claim is recorded, but no contact check was possible:
    src/parser.py has no symbol named 'tokenise'. It does have: parse(), parser.py, tokenize()
  Check the name: a typo looks exactly like code nothing else touches.
```

A typo (`tokenise` for `tokenize`) and a genuinely isolated symbol produce
identical silence, so the miss is stated with its reason and the real contents
of the file. The claim itself still stands: the log is about paths, and the path
is true whether or not the map has indexed it.

The board is just the ledger:

```
active claims (2):
  src/parser.py  @agent-a  2026-08-20T11:27:47.370446Z  [01M0FETQ5AFGADGWZK4Q6SRPQF]  "rewrite tokenizer"
  src/report.py  @agent-b  2026-08-20T11:27:47.463871Z  [01M0FETQ88P1D1KTXE5N0TD1TM]  "new output format"
```

---

## Known holes, found and not yet closed

Written down because a limit you know about is survivable and one you do not is
not. Each was reproduced; none is fixed as of this writing.

**The uncreated-file check is narrower than it looks.** Two agents claiming one
not-yet-created file under two spellings are caught when the filesystem is
case-blind — but the probe only asks about CASE. APFS is also
normalization-insensitive in its case-SENSITIVE variant, so an NFC and an NFD
spelling of one new accented filename are both accepted there. And the check
covers only the window between claiming and creating: once the file exists both
spellings resolve to its real name, which is correct, but an agent that claimed
one spelling and created the other locks itself out of its own file.

**The session id is self-asserted, so the review gate stops carelessness rather
than a determined rename.** `same_agent` compares the agent session recorded in
each hello, and that value comes from `CLAUDE_CODE_SESSION_ID` — read from the
environment of the very process being checked. One process can therefore submit
as `alice`, set a different session id, review as `reviewer-2`, and the sign-off
is recorded as `independent`. A hyphenated alias (`alice-review`) also passes,
because only a `/role` suffix is stripped. What the gate reliably catches is an
agent that reviews its own work without meaning to; it cannot catch one that
sets out to.

Homoglyphs used to be part of that hole and are no longer: `normalise_actor`
folds the Cyrillic and Greek letters drawn identically to Latin ones, so `nоra`
and `nora` are one agent to the gate. It is a fixed table of the letters that
actually collide in a Latin name, not a full confusables database — it closes
the disguise somebody would reach for, not every one that exists.

## Two hooks, and only one of them blocks

This matters more than it looks, and it is easy to assume wrongly.

`graphify install` wires **`graphify hook-guard edit`**. That guard is
**advisory by design**: when your edit lands inside somebody else's claim it
adds a note to the model's context saying so — its own message ends "NOT
blocking" — and the edit proceeds. It is a nudge, and it is deliberately a
nudge; it fails open on an unreadable log, an odd filename, a missing store.

**`graphify comms check --stdin-json`** is the one that stops an edit. It exits
2, which is the only exit code Claude Code treats as "block this tool call and
show stderr to the model"; every other non-zero code means "the hook errored"
and the edit goes through anyway. It fails CLOSED: anything it cannot establish
answers 2, because a coordination tool that cannot read its log has not
established that the file is free.

If you want claims enforced rather than mentioned, wire the second one yourself:

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Edit|Write",
    "hooks": [ { "type": "command",
                 "command": "graphify comms check --stdin-json" } ] } ] } }
```

Both now spell paths the way the filesystem does, so neither is fooled by
`src/A.py` versus `src/a.py` on a case-insensitive volume. That is worth saying
because it was not true until recently, and the two were fixed at different
times: every correctness fix landed on `comms check` first, and `hook-guard`
inherited none of them until it was pointed at the same canonicaliser.

## The work graph — what exists, in what order, and was it checked

Claims answer *who is touching this file right now*. They say nothing about what
work there is, what has to happen first, or whether anything was ever checked. A
second layer does that, and it is optional: claims record and block with or
without it.

```
graphify comms plan --from plan.json --as planner   a whole graph, atomically
graphify comms next --as <you>                      what you could start now
graphify comms brief <task-id>                      what it is, and what came before
graphify comms task done <id> --as <you> --check test=pass --note "..."
graphify comms task review <id> --as <you> --pass|--fail --evidence "..."
```

Three rules carry the design, and each is there because the alternative was
measured and failed.

**A task unblocks what follows it when it is VERIFIED, not when it is done.**
Self-review measurably fails; fresh-context review by a different agent
measurably works. So review sits on the critical path rather than beside it, and
you cannot sign off your own work — not under a different name, not under a role
suffix. The check is on the process, not the spelling. You *can* reject your own
work: saying "this is not ready" needs no second opinion, and refusing that
deadlocked any task two agents had both touched.

If two agents have both worked on one task, neither may pass it — correct, they
both wrote it — and only somebody who has not touched it can. On a two-agent
setup that is nobody, so there is an escape:
`task review <id> --pass --acknowledge-self-review`. It records the sign-off as
what it is: the independence field reads `self-acknowledged`, never
`independent`, and the board says "signed off by themselves". Refusing to record
anything was not safety, it was a stall the first disagreement hits.

**`brief` is the verb that earns its keep.** For every task yours comes after it
prints what yours consumes and the decisions recorded upstream:

```
login-ui  Login screen
  phase: blocked · BLOCKED until these are VERIFIED: auth-api
  comes after:
    auth-api (review) [interface] — provides: POST /session returns a JWT
        decided: token is a JWT, 15 minute expiry
```

Without it the first agent's reasoning stays on the first agent's node, which is
the ordinary way a decomposed plan produces pieces that do not fit together.

**The edge kind decides what a rejection costs.** `interface` and `artifact` mean
the successor consumes something, so reworking the predecessor reopens it.
`sequence` is ordering only, and a rejection does not touch work already finished
behind it. Without that distinction one rejection invalidates everything
downstream, and after about two of those nobody believes the board.

Phases are **derived on every fold**, never written down: `ready`, `doing`
(somebody holds a claim tagged to it), `review`, `blocked`, `closed`, and `cycle`
for a task in or behind a dependency loop. Tag a claim with `--task <id>` and the
doer list follows your claims, so releasing the file clears it for you.

A cycle is reported, never hung on. The fold runs in front of every agent tool
call, so reachability is iterative and terminates on any input — and `plan`
refuses an edge that would close a loop before writing a single byte.

## For agents, not just humans

`graphify install` writes a short "Parallel agents" block into the always-on
instruction files (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, …) telling the agent to
claim before it edits, and — importantly — how to read the two halves of the
reply differently.

Over MCP (`graphify-mcp`) the same surface is exposed as four tools:
`comms_claim`, `comms_check`, `comms_release`, `comms_status`.

---

## Where the data lives

Two stores, and the split is load-bearing:

- **The map is derived.** `graphify-out/graph.json` is thrown away and rebuilt
  whenever the code changes, rewritten whole-file, and deleted outright by
  `graphify uninstall --purge`.
- **The log is truth.** Append-only, never rewritten, must never be lost. It
  holds claims, releases, findings and tasks, under the user data directory keyed
  by a hash of the repository root.

Coordination state is written to the log and only *projected* onto the map.
Putting it in `graph.json` would lose it on the next rebuild, and would lose
concurrent writes besides.

The root is the **git root**, not the working directory, so two agents running
from different subdirectories of one checkout share one log rather than getting
two private ones that each report success while coordinating nothing.

---

## Development

Run the fork's own tests:

```
pytest tests/comms tests/test_comms_lock.py \
       tests/test_comms_always_on.py tests/test_comms_edit_hook.py
```

CI for this layer is [.github/workflows/comms.yml](.github/workflows/comms.yml):
the comms tests on Linux and macOS across Python 3.11 and 3.12, plus a packaging
job that builds the wheel and sdist and asserts `graphify/comms/` is actually
inside them. That last job exists because the subpackage was missing from the
explicit `[tool.setuptools] packages` list in `pyproject.toml` — the wheel built
clean, installed clean, and then raised `ModuleNotFoundError` on the first
`graphify comms` command. Unit tests cannot catch that, because they import from
the source tree where the package is always present.

The upstream graphify suite (`.github/workflows/ci.yml`) still has to pass. If
you touch anything under `graphify/always_on/`, those files are **generated** —
edit the fragments in `tools/skillgen/fragments/always-on/` and re-run
`python -m tools.skillgen`, or the `skillgen-check` job will fail on drift.
