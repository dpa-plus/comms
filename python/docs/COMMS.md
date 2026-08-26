# Coordination in the map

This fork adds coordination for parallel coding agents to graphify: who is
working on what, and whose work is near yours.

It is deliberately smaller than the idea it started as. This document records
what was tried, what was measured, and what was cut, so the cut parts are not
quietly reintroduced later by someone who only sees the appealing version.

## The idea that failed

The original plan was that the map could **derive order**. If job A changes
something job B leans on, A goes first. Foundation before floor. It is a clean
story and it sounds obviously right.

It was checked against three months of real changes on two projects. It was
right about as often as a coin flip: roughly half: on both. On one project the
real work went the *other* way round most of the time.

So the neat theory is a nice story, not a measured fact. Had it shipped as a
blocker, the board would have confidently told people to wait for no reason, and
they would have stopped believing the board within a day. Losing trust in a
coordination tool is not a small cost: the tool only works if people read what it
says.

**Permanent rule that follows: no arrow the machine draws will ever block
anything on its own.** That is not first-version caution. Order may later be
*suggested*, shown to a person as a yes/no question with the reason attached, and
only a confirmed suggestion may ever hold work up, so that every refusal traces
back to a named human decision.

## What survived, and why

The **contact warning**. You say what you are about to work on and immediately
find out two things:

1. whether anyone else has claimed the same ground, and
2. whether the code you named is connected to code somebody else named.

No order, no blocking, no plan to fill in, no permission to wait for. It pays
back to the person typing at the moment they type, and it needs no calibration to
be honest.

## What it will not do

These are measured limits, not modesty. Each one is stated in the tool's own
output, because a limit the user cannot see is a limit that misleads them.

- **No warning does not mean independent.** Between a third and a half of file
  pairs that really do get changed together are invisible to the map.
- **A warning is a prompt to look, never a verdict.** Of the pairs the map does
  flag, well under half turn out to be things that really get changed together.
- **It needs small, specific claims.** Claim everything you touch and the board
  turns to mush: measured, not guessed. Three places is the cap; a job needing
  more has to be split.
- **A mistyped name finds nothing and looks exactly like a job with no
  connections.** In testing, a quarter of names typed from memory named something
  that does not exist. So a miss is announced out loud, with the reason and a
  list of what the file does contain.
- **The connection only knows what the map last read.** A claim pointing at code
  that changed since then is reported as out of date rather than as truth.
- **It fits projects shaped like the one it was built for.** On a large,
  loosely-connected codebase, narrow claims produce almost no connections at all
 : an empty board. The measuring step exists to detect that and say so rather
  than shipping a board that looks calm because it knows nothing.

## Where the data lives, and why it is split

Two stores, on purpose:

- **The map is derived.** It is thrown away and rebuilt whenever the code
  changes. `graphify-out/graph.json` is rewritten whole-file, and
  `graphify uninstall --purge` deletes the directory it sits in.
- **The log is truth.** Append-only, never rewritten, must never be lost. It
  holds claims, releases, findings and tasks, under the user data directory keyed
  by a hash of the repository root.

Coordination state is therefore written to the log and only *projected* onto the
map. Putting it in `graph.json` would lose it on the next rebuild, and would
lose concurrent writes besides, since that file is replaced wholesale rather than
appended to.
