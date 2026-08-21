"""The task graph: what work exists, what order it must happen in, was it checked.

Port of comms' ``internal/state/task.go``. Kept in its own module for the same
reason the Go does: the coordination half (who holds which file right now) and
the work half (what is there to do) answer different questions, and folding them
in one place made both harder to read.

THREE IDEAS DO ALL THE WORK HERE.

**Phase is derived, never authored.** No event says "this task is blocked". A
task's phase falls out of the graph and the live claims every time the log is
folded, so it cannot drift from reality and nobody has to remember to update it.
The same goes for who is working on something: ``doers`` comes from ACTIVE claims
tagged with the task, so releasing a file empties it for free.

**A different agent has to verify.** A task unblocks its successors when it is
VERIFIED, not when it is done. Self-review measurably fails and fresh-context
review by another agent measurably works, so review sits on the critical path
rather than beside it. ``base_actor`` strips a ``/suffix`` so ``claude-dev/review``
is recognised as ``claude-dev`` and cannot sign off its own work.

**A cycle must be survivable.** ``fold`` returns no error and runs on the
pre-edit hot path, in front of every tool call an agent makes. So the reachability
pass is Kahn's algorithm — iterative, no recursion, terminating on any input —
and a task inside or downstream of a cycle resolves deterministically to the
``cycle`` phase. It is never a hang, never a crash, and never a stack overflow on
somebody's laptop because two tasks were declared to follow each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import unicodedata
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------
# Phases — derived, never written into an event
# --------------------------------------------------------------------------

#: Nothing upstream is outstanding and nobody has picked it up.
PHASE_READY = "ready"
#: Somebody holds a claim tagged with this task.
PHASE_DOING = "doing"
#: Work is submitted and waiting for SOMEBODY ELSE to check it.
PHASE_REVIEW = "review"
#: Verified. Only now do its successors stop being blocked.
PHASE_CLOSED = "closed"
#: At least one predecessor is not verified yet.
PHASE_BLOCKED = "blocked"
#: In, or downstream of, a dependency cycle. Reported rather than hung on.
PHASE_CYCLE = "cycle"

#: B consumes an interface or an artifact from A. Reworking A flags B for
#: recheck, because what B was built against may have moved.
EDGE_INTERFACE = "interface"
EDGE_ARTIFACT = "artifact"
#: Ordering only. Reworking A does NOT touch B. This distinction is what makes a
#: rejection precise instead of invalidating everything downstream of it.
EDGE_SEQUENCE = "sequence"

_EDGE_KINDS = frozenset({EDGE_INTERFACE, EDGE_ARTIFACT, EDGE_SEQUENCE})


@dataclass
class TaskFinding:
    """Something a reviewer found. Travels with the task through its rework."""

    what: str = ""
    where: str = ""


@dataclass
class TaskEdge:
    """A directed dependency: ``to`` comes after ``from_``."""

    from_: str
    to: str
    kind: str = EDGE_SEQUENCE
    #: What ``to`` consumes from ``from_`` — an interface, a schema, a file's
    #: public surface. This is what reaches whoever picks up ``to``, and it is
    #: why a rejection can be precise about who else needs to look again.
    provides: str = ""
    ts: datetime | None = None


@dataclass
class Task:
    """One piece of work. ``phase`` and ``doers`` are derived on every fold."""

    id: str
    title: str = ""
    #: S, M or L. Advisory sizing, not a schedule.
    size: str = ""
    #: Check names that must all pass before it can be marked done.
    checks: list[str] = field(default_factory=list)
    #: Opaque external reference, e.g. "tracker:PROJ-1234". comms stores it and
    #: never executes anything with it.
    ref: str = ""

    ts: datetime | None = None
    updated_at: datetime | None = None

    #: Derived from ACTIVE claims tagged to this task, so it empties itself when
    #: an agent releases. Nobody has to remember to update it.
    doers: list[str] = field(default_factory=list)

    #: The actor whose implementation is awaiting review, or whose work was
    #: verified. Cleared by a rejection — that is the rework edge.
    did: str = ""
    #: EVERYONE who has submitted work on this task, not just the most recent.
    #: `did` is overwritten by each submission, so with only that field a task
    #: worked by alice and then by bob remembered bob — and alice, who had also
    #: worked on it, was then a "different agent" and could sign it off.
    submitters: list[str] = field(default_factory=list)
    #: Everyone who has ever held a claim tagged to this task, whether or not
    #: they submitted. `submitters` only knows who pressed `done`; a second
    #: agent recruited onto a multi-slot task and still mid-work has written
    #: half of it and is not a reviewer. Persistent on purpose — releasing the
    #: file ends your turn on the task, it does not unwrite what you wrote.
    workers: list[str] = field(default_factory=list)
    #: What the doer REPORTED for each declared check, name -> result. The fold
    #: has always read these to decide whether `done` is allowed and then thrown
    #: them away, so "was this actually tested" was decidable at the moment of
    #: submission and unanswerable one line later. Replaced whole on each
    #: submission: a new round reports its own results.
    check_results: dict[str, str] = field(default_factory=dict)
    #: Decisions written while working. What a verifier reads, and what reaches
    #: whoever picks up a successor.
    notes: list[str] = field(default_factory=list)
    verified_by: str = ""
    #: True once a verification has ever been accepted, and never cleared by a
    #: later rejection. It is what separates "this was signed off and has since
    #: been reworked" from "this was never signed off at all" — two situations
    #: with the same verified_by ("") and opposite consequences downstream.
    ever_verified: bool = False
    #: "independent" when the verifier's vendor differs from the doer's,
    #: "same-family" otherwise. Verified and verified-by-something-with-the-same-
    #: blind-spots are different claims and should not read alike.
    independence: str = ""
    #: How the verifier says they checked it. Written by `task review --pass
    #: --evidence`. Kept apart from `notes` on purpose: "the author decided X"
    #: and "somebody else checked Y" are different claims, and a reader who
    #: cannot tell them apart has lost the only thing the review gate produces.
    #: Cleared by a rejection, with the verification it describes.
    verification: str = ""
    rejections: int = 0
    findings: list[TaskFinding] = field(default_factory=list)
    last_activity: datetime | None = None

    phase: str = PHASE_READY
    blocked_by: list[str] = field(default_factory=list)


@dataclass
class RefusedTransition:
    """A state change the fold would not make, and why.

    Kept rather than dropped: a refusal is the moment the rule did its job, and
    a rule that enforces itself in silence looks exactly like a rule nobody
    wrote. This is the same reasoning as the ``blocked`` event for claims.
    """

    ts: datetime | None
    task: str
    actor: str
    phase: str
    reason: str


# --------------------------------------------------------------------------
# Reducers
# --------------------------------------------------------------------------


def base_actor(actor: str) -> str:
    """The actor without its role suffix: ``claude-dev/review`` -> ``claude-dev``.

    A LEADING slash is not a role suffix and must not be treated as one. Naively
    splitting made ``/alice`` and ``/bob`` both reduce to the empty string, which
    broke the gate in BOTH directions at once: two genuinely different agents
    were refused as self-review, while ``/claude-dev`` reviewing ``claude-dev``
    sailed through because "" != "claude-dev". An empty base is never a useful
    answer, so fall back to the whole name.
    """
    head = actor.split("/", 1)[0] if "/" in actor else actor
    return head or actor



# Cyrillic and Greek letters drawn the same as Latin ones. Folded to the Latin
# letter for COMPARISON only — nothing here is ever stored or shown, and the log
# keeps exactly the bytes that were written. Uppercase forms included because
# casefold runs after this, not before.
_LATIN_LOOKALIKES = str.maketrans({
    # Cyrillic
    "\u0430": "a", "\u0410": "A", "\u0435": "e", "\u0415": "E",
    "\u043e": "o", "\u041e": "O", "\u0440": "p", "\u0420": "P",
    "\u0441": "c", "\u0421": "C", "\u0445": "x", "\u0425": "X",
    "\u0443": "y", "\u0423": "Y", "\u0456": "i", "\u0406": "I",
    "\u0458": "j", "\u0408": "J", "\u04bb": "h", "\u041d": "H",
    "\u0412": "B", "\u041a": "K", "\u041c": "M", "\u0422": "T",
    "\u0405": "S", "\u0455": "s",
    # Greek
    "\u03b1": "a", "\u0391": "A", "\u03bf": "o", "\u039f": "O",
    "\u03c1": "p", "\u03a1": "P", "\u03b5": "e", "\u0395": "E",
    "\u03c5": "u", "\u03a5": "Y", "\u03ba": "k", "\u039a": "K",
    "\u03bd": "v", "\u039d": "N", "\u03c4": "t", "\u03a4": "T",
    "\u0392": "B", "\u0397": "H", "\u0399": "I", "\u039c": "M",
    "\u03a7": "X", "\u0396": "Z",
})

def normalise_actor(actor: str) -> str:
    """An actor name reduced to what it LOOKS like, for comparison only.

    Never stored or displayed — the log keeps exactly what was written.

    Closes the cheap disguises: a different case, a zero-width space, a
    non-breaking space, a compatibility character, and the Cyrillic and Greek
    letters that are drawn identically to Latin ones.

    That last group is here because a real attempt found it: with no host
    session id there is no hello, so :func:`same_agent` has only the name to go
    on, and ``nоra`` with a Cyrillic "о" signed off work ``nora`` had written —
    the board, the brief and the successor's unblock all asserting an
    independent review, under a name nobody could tell apart on screen.

    This is a fixed table of the letters that actually collide in a Latin name,
    not a full confusables database. It closes the realistic disguise and does
    not pretend to close every possible one — which is why this stays a filter
    and the session comparison in :func:`same_agent` is the real gate.
    """
    text = unicodedata.normalize("NFKC", actor or "")
    # Cf is the format category: zero-width space, joiners, direction marks.
    # Cc is control characters. Neither is visible, so neither may distinguish
    # one actor from another.
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cc"))
    text = text.translate(_LATIN_LOOKALIKES)
    return text.strip().casefold()


def same_agent(a: str, b: str, sessions: Mapping[str, Any] | None = None) -> bool:
    """Are these two actor names the same agent?

    THE GATE THIS PROTECTS. A task closes, and its successors unblock, only when
    somebody OTHER than the doer verifies it. Self-review measurably fails, so
    the entire value of "verified" rests on this answer being right.

    Comparing the raw names was not enough, and the ways past it were not exotic:
    ``CLAUDE-DEV`` reviewing ``claude-dev`` worked. So did a zero-width space, a
    U+2010 hyphen, and a Cyrillic "a" — each printing a line indistinguishable
    from a real review, because the difference is invisible on screen.

    TWO LAYERS, because neither alone is enough:

    * Normalised names catch the cheap disguises. They cannot catch a homoglyph.
    * The AGENT SESSION catches everything, because it ignores names entirely.
      Every actor writes a hello carrying the host agent's session id, and two
      names from one process share it. The log already recorded this — the
      pre-edit hook has used it for identity since it was written — the review
      gate simply never looked. That is what makes a homoglyph pointless: you
      may call yourself anything, but you are still the same process.

    A missing or empty session id proves nothing either way, so it only ever
    adds a match; it never overrides the name comparison to say "different".
    """
    if normalise_actor(base_actor(a)) == normalise_actor(base_actor(b)):
        return True
    if not sessions:
        return False
    sa = sessions.get(a)
    sb = sessions.get(b)
    ses_a = (getattr(sa, "agent_session", "") or "").strip() if sa else ""
    ses_b = (getattr(sb, "agent_session", "") or "").strip() if sb else ""
    return bool(ses_a) and ses_a == ses_b


def _failed_checks(required: Iterable[str], data: Mapping[str, Any]) -> list[str]:
    """Required checks whose reported result is not a pass.

    Anything that is not the string "pass" counts as a failure, including a
    check that was simply not reported. Silence is not a pass — the commonest
    way for a gate like this to rot is for the thing it gates on to quietly stop
    running, and a missing result should read exactly like a failing one.
    """
    required = list(required)
    if not required:
        return []
    results = data.get("checks")
    if not isinstance(results, Mapping):
        results = {}
    failed = []
    for name in required:
        value = results.get(name)
        if not isinstance(value, str) or value.strip().lower() != "pass":
            failed.append(name)
    return failed


def apply_task(tasks: dict[str, Task], ev: Any, string_of, int_of, ref_list) -> None:
    """Upsert a task node. Whole-record replace, field by field.

    Only non-empty fields overwrite, so a later event that names a task without
    restating its checks does not silently drop the gate on it.
    """
    task_id = string_of(ev.data, "task")
    if not task_id:
        return
    t = tasks.get(task_id)
    if t is None:
        t = Task(id=task_id, ts=ev.ts)
        tasks[task_id] = t
    title = string_of(ev.data, "title")
    if title:
        t.title = title
    size = string_of(ev.data, "size")
    if size:
        t.size = size.upper()
    checks = ref_list(ev.data, "checks")
    if checks:
        added = [c for c in checks if c not in t.checks]
        t.checks = checks
        if added and t.did and not t.verified_by:
            # A NEW required check invalidates a submission made before it
            # existed. The gate's contract is "every declared check passed when
            # this was submitted", and a check added afterwards was never
            # reported — so leaving the submission standing let a task close
            # with a required check that had never run. Declaring the checks
            # late would otherwise be a way around the gate.
            #
            # Only genuinely NEW names do this; re-stating a task with the same
            # checks changes nothing, or editing a title would bounce work.
            t.did = ""
            t.notes.append(
                "resubmit: " + ", ".join(added)
                + (" was" if len(added) == 1 else " were")
                + " required after this was submitted, so it has not been reported"
            )
    ref = string_of(ev.data, "ref")
    if ref:
        t.ref = ref
    t.updated_at = ev.ts
    t.last_activity = ev.ts


def apply_task_edge(edges: list[TaskEdge], ev: Any, string_of,
                    index: dict[tuple[str, str], int] | None = None) -> None:
    """Upsert a dependency edge.

    A self-edge is dropped rather than recorded: it is always a mistake, and
    keeping it would put the task permanently in its own cycle.
    """
    from_ = string_of(ev.data, "from")
    to = string_of(ev.data, "to")
    if not from_ or not to or from_ == to:
        return
    kind = string_of(ev.data, "kind").lower()
    if kind not in _EDGE_KINDS:
        # An unrecognised kind becomes the weakest one. Guessing "interface"
        # would invent a rework dependency nobody declared.
        kind = EDGE_SEQUENCE
        if not string_of(ev.data, "kind"):
            # NAMED NOTHING, which is different from naming something wrong.
            # A later event that only adds a `provides` note must not erase the
            # kind already recorded: since the kind became load-bearing, that
            # downgraded interface to sequence and flipped a task a rejection
            # had reopened straight back to closed, with nobody re-reviewing it.
            # Same rule apply_task uses for checks — only non-empty fields win.
            for existing in edges:
                if existing.from_ == from_ and existing.to == to:
                    kind = existing.kind
                    break
    edge = TaskEdge(from_=from_, to=to, kind=kind,
                    provides=string_of(ev.data, "provides"), ts=ev.ts)
    # Upsert through an index rather than rescanning. The linear scan made the
    # fold quadratic in edge count, and this runs in front of every agent tool
    # call: 6,000 edges took 0.26s, 32,000 took the best part of ten seconds,
    # all of it spent re-walking edges to find out the new one is not among them.
    if index is not None:
        seen = index.get((from_, to))
        if seen is not None:
            edges[seen] = edge
        else:
            index[(from_, to)] = len(edges)
            edges.append(edge)
        return
    for i, existing in enumerate(edges):
        if existing.from_ == from_ and existing.to == to:
            edges[i] = edge
            return
    edges.append(edge)


def _authored_by(task: "Task", actor: str, sessions=None) -> str | None:
    """The name this actor previously submitted work under, if any.

    Checks EVERY submitter rather than just the most recent. Two agents taking
    turns on one task — alice submits, bob submits — left `did` holding only
    bob, so alice read as a different agent and could sign off work she had
    written half of. Review means somebody who did not build it looked at it;
    "did not build the last revision of it" is not the same claim.
    """
    for prior in (([task.did] if task.did else [])
                  + list(task.submitters) + list(task.workers)):
        if prior and same_agent(actor, prior, sessions):
            return prior
    return None


def apply_task_state(
    tasks: dict[str, Task],
    refused: list[RefusedTransition],
    ev: Any,
    string_of,
    ref_list,
    independence_of,
    sessions: Mapping[str, Any] | None = None,
) -> None:
    """A lifecycle transition, with every rule that guards it.

    All three refusals live here rather than in the command layer, because a
    rule enforced only at the CLI is a rule that a second writer — another
    implementation, a repaired log, a script — does not have to obey. The fold
    is the one place every reader agrees on.
    """
    task_id = string_of(ev.data, "task")
    t = tasks.get(task_id)
    if t is None:
        # A transition for a task nobody declared. Not a refusal worth recording
        # against a task that does not exist; the event is simply inert.
        return

    def refuse(reason: str) -> None:
        refused.append(RefusedTransition(
            ts=ev.ts, task=task_id, actor=ev.actor,
            phase=string_of(ev.data, "state"), reason=reason,
        ))

    t.last_activity = ev.ts
    state = string_of(ev.data, "state").lower()

    if state == "done":
        failed = _failed_checks(t.checks, ev.data)
        if failed:
            refuse("required checks did not pass: " + ", ".join(failed))
            return
        t.did = ev.actor
        reported = ev.data.get("checks") if isinstance(ev.data, dict) else None
        t.check_results = ({
            str(k): str(v) for k, v in reported.items()
            if isinstance(k, str) and isinstance(v, (str, int, float, bool))
        } if isinstance(reported, dict) else {})
        if not any(same_agent(ev.actor, prior) for prior in t.submitters):
            t.submitters.append(ev.actor)
        t.verified_by = ""
        # Clear the independence with the verification it describes. It used to
        # survive a resubmission, so after a rework `brief` on a successor said
        # "signed off by @ themselves" — with a bare @ where the name had been —
        # and the drawing painted the amber dashed self-signed node for a task
        # sitting in review with no verifier at all. A label outliving the thing
        # it labels is worse than no label.
        t.independence = ""
        t.verification = ""
        notes = ref_list(ev.data, "notes")
        if notes:
            t.notes.extend(notes)

    elif state == "verified":
        if not t.did:
            refuse("nothing is awaiting review on this task")
            return
        author = _authored_by(t, ev.actor, sessions)
        acknowledged = bool(ev.data.get("acknowledged_self_review"))
        if author is not None and not acknowledged:
            refuse(
                (
                    # The bar can come from having SUBMITTED it or from still
                    # holding ground on it, and those need different sentences.
                    # "cannot verify work done by @bob" told bob, who had not
                    # submitted anything, that he had done work he had not.
                    f"self-review: {ev.actor} is working on this task"
                    if not any(same_agent(ev.actor, sub, sessions)
                               for sub in ([t.did] if t.did else []) + list(t.submitters))
                    else f"self-review: {ev.actor} cannot verify work done by {author}"
                )
                + ("; everyone who submitted this is barred, so it needs "
                   "somebody who has not worked on it"
                   if len(t.submitters) > 1 else "")
            )
            return
        t.verified_by = ev.actor
        t.ever_verified = True
        if author is not None:
            # An ESCAPE, not a loophole. Once two agents have both submitted a
            # task, neither may verify it and only a third party can move it —
            # which on a two-agent run is nobody, so the task and everything
            # after it stopped forever. Refusing to record anything is not
            # safety, it is a stall.
            #
            # So it may be recorded, and it is recorded as WHAT IT IS. This
            # never reads as an independent review: the board shows
            # "self-acknowledged", the value is not "independent" or
            # "same-family", and nothing downstream can mistake the two.
            # Somebody chose to sign off their own work and their name is on it.
            t.independence = "self-acknowledged"
        else:
            t.independence = independence_of(ev.actor, t.did)
        # The CLI has always written this; nothing read it. Only the `done`
        # branch extended notes, so a verifier's "how I checked it" was
        # appended to the log and then dropped by the fold — present in the
        # bytes, absent from every surface. For a tool whose whole output is
        # independent verification, that was the one fact worth keeping.
        checked = ref_list(ev.data, "notes")
        if checked:
            t.verification = checked[-1]
        t.findings = []

    elif state == "rejected":
        if not t.did:
            refuse("nothing is awaiting review on this task")
            return
        # NO authorship check here, deliberately. Self-APPROVAL is the failure
        # this gate exists for; self-rejection is somebody saying their own work
        # is not ready, which is the outcome we want to be easy.
        #
        # Barring it created a deadlock: once alice and bob had both submitted,
        # neither could verify (correct — they wrote it) and neither could
        # reject either, so the task and everything downstream of it was stuck
        # until a third party appeared. On a two-agent run there is no third
        # party. A rejection returns the task to being worked on and costs
        # nobody anything; refusing one buys no safety at all.
        # The rework edge. The graph is NOT redrawn — the task simply goes back
        # to being worked on, and the findings travel with it so whoever picks
        # it up reads why it came back.
        t.did = ""
        t.verified_by = ""
        t.verification = ""
        # And the label describing that sign-off. Clearing this on resubmission
        # but not on rejection left the identical bug reachable by the other
        # route: `brief` on a SUCCESSOR read "signed off by @ themselves" —
        # a bare @ where the name had been — about a predecessor the same
        # command had just called `ready`, and the drawing painted it amber
        # and dashed. Two surfaces read `independence` alone; both believed it.
        t.independence = ""
        t.rejections += 1
        t.findings = _findings_from(ev.data)


def _findings_from(data: Mapping[str, Any]) -> list[TaskFinding]:
    raw = data.get("findings")
    if not isinstance(raw, list):
        return []
    out: list[TaskFinding] = []
    for item in raw:
        if isinstance(item, Mapping):
            out.append(TaskFinding(
                what=str(item.get("what") or ""),
                where=str(item.get("where") or ""),
            ))
        elif isinstance(item, str) and item.strip():
            out.append(TaskFinding(what=item.strip()))
    return out


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def derive_phases(
    tasks: dict[str, Task],
    edges: list[TaskEdge],
    claims: Iterable[Any],
    sessions: Mapping[str, Any] | None = None,
) -> None:
    """Compute every task's phase from the graph and the live claims.

    Runs after the main fold, on every fold, so nothing here may raise and
    nothing may fail to terminate — this is on the path of every pre-edit check.

    ``sessions`` matters: the review gate answers "is this the same agent?" with
    them, and this used to answer it without. One process under two names was
    then one agent to the gate and two here, which held a cleanly verified task
    open and blocked its successor while reporting a reason that was false.
    """
    if not tasks:
        return

    # Doers come from ACTIVE claims, so releasing a file empties them for free.
    for t in tasks.values():
        t.doers = []
        t.blocked_by = []
    for c in claims:
        task_id = getattr(c, "task", "") or ""
        t = tasks.get(task_id)
        if not task_id or t is None:
            continue
        if c.actor not in t.doers:
            t.doers.append(c.actor)
        if c.ts is not None and (t.last_activity is None or c.ts > t.last_activity):
            t.last_activity = c.ts
    for t in tasks.values():
        t.doers.sort()

    # Kahn's algorithm. Whatever cannot be peeled off is in, or downstream of, a
    # cycle. Iterative on purpose: recursion here would put a stack depth equal
    # to the longest dependency chain in front of every agent tool call, and
    # would crash rather than report on a cyclic log.
    indegree: dict[str, int] = {tid: 0 for tid in tasks}
    outgoing: dict[str, list[str]] = {tid: [] for tid in tasks}
    for e in edges:
        if e.from_ not in tasks or e.to not in tasks:
            # An edge naming a task nobody declared is noise, not a cycle. It
            # must not make its endpoint unsettleable.
            continue
        indegree[e.to] += 1
        outgoing[e.from_].append(e.to)

    queue = [tid for tid in sorted(tasks) if indegree[tid] == 0]
    settled: set[str] = set()
    while queue:
        tid = queue.pop(0)
        settled.add(tid)
        for nxt in outgoing[tid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    # Predecessors, grouped once rather than rescanned per task: a task graph is
    # small, but this runs on the hot path and O(tasks x edges) is avoidable.
    # Kept WITH their kind, because the kind decides whether losing a
    # predecessor's verification reopens work that was already finished.
    predecessors: dict[str, list[tuple[str, str]]] = {tid: [] for tid in tasks}
    for e in edges:
        if e.from_ in tasks and e.to in tasks:
            predecessors[e.to].append((e.from_, e.kind))

    # Who is still writing a task: holds ground tagged to it and has not
    # submitted. Computed for every task up front because the blocked_by pass
    for t in tasks.values():
        # Compute this FIRST and for every task, not only for ones nobody has
        # touched. Testing verified_by ahead of it let a task that was never
        # startable be marked done, verified, and reported CLOSED while its own
        # predecessor sat at ready — and `next` then offered its successors as
        # startable, so unbuilt work unblocked more unbuilt work.
        #
        # DONE is not enough for a predecessor: a successor waits for VERIFIED.
        # That is the whole point of putting review on the critical path.
        # Which unverified predecessors actually hold this task back depends on
        # whether it is finished yet — and that is what the edge KIND is for.
        #
        # Not started or in flight: EVERY predecessor blocks. Ordering is
        # ordering, and "b comes after a" means what it says.
        #
        # Already verified: only the ones it CONSUMES from can reopen it. If a
        # was reworked and b was built against a's interface, what b was built
        # against has moved and b has to be looked at again. If the edge was
        # only ordering, a's rework says nothing about b — reopening it would
        # invalidate finished, reviewed work on no evidence, and after a couple
        # of those nobody believes the board.
        #
        # Without this the kind field was recorded and then ignored, so every
        # edge behaved like an interface edge.
        blocked_by = sorted({
            dep for dep, kind in predecessors[t.id]
            if not tasks[dep].verified_by
            and (
                # Not finished yet: every predecessor blocks. Ordering is
                # ordering, whatever the kind.
                not t.verified_by
                # Finished, and it consumes from this predecessor: what it was
                # built against has moved, so look again.
                or kind in (EDGE_INTERFACE, EDGE_ARTIFACT)
                # Finished, ordering-only edge — but this predecessor has NEVER
                # been verified, so the successor was never legitimately
                # startable and its sign-off jumped the queue. That is a
                # different situation from a rework, and only ever_verified
                # tells them apart: both leave verified_by empty.
                or not tasks[dep].ever_verified
            )
        })
        t.blocked_by = blocked_by

        if t.id not in settled:
            t.phase = PHASE_CYCLE
        elif blocked_by:
            # Even a verification does not close it. A verification granted
            # while the ground underneath was still moving is not evidence
            # about the finished thing, and reporting it as closed would hide
            # that from everyone downstream.
            t.phase = PHASE_BLOCKED
        elif t.verified_by:
            t.phase = PHASE_CLOSED
        elif t.did:
            t.phase = PHASE_REVIEW
        elif t.doers:
            t.phase = PHASE_DOING
        else:
            t.phase = PHASE_READY


# --------------------------------------------------------------------------
# Reading the graph
# --------------------------------------------------------------------------


def ready_tasks(tasks: dict[str, Task]) -> list[Task]:
    """Tasks nobody has picked up, most recently touched first.

    READY only. A task with a doer belongs to that doer until they submit or
    let go of it — one task, one agent, which is the rule that removed a whole
    family of bugs. Every severity-1 the task graph ever had came from two
    agents sharing one: a review that covered half the work, a gate you could
    opt out of by never submitting, and a file release that silently closed the
    task somebody else was still writing.
    """
    out = [t for t in tasks.values() if t.phase == PHASE_READY]
    out.sort(key=lambda t: (t.last_activity or datetime.min.replace(tzinfo=None)), reverse=True)
    return out


def awaiting_review(tasks: dict[str, Task], actor: str,
                    sessions: Mapping[str, Any] | None = None) -> list[Task]:
    """Tasks this actor may verify — that is, anything they did not do."""
    # _authored_by, not same_agent(actor, t.did): the GATE bars everyone who
    # submitted, so checking only the most recent submitter made the board
    # recommend a review the CLI was guaranteed to refuse. The reader half and
    # the enforcing half have to ask the same question.
    return sorted(
        (t for t in tasks.values()
         if t.phase == PHASE_REVIEW and _authored_by(t, actor, sessions) is None),
        key=lambda t: t.id,
    )


def incoming(edges: list[TaskEdge], task_id: str) -> list[TaskEdge]:
    """Edges into a task — what it depends on, and what it consumes from each."""
    return sorted((e for e in edges if e.to == task_id), key=lambda e: e.from_)


def would_cycle(edges: list[TaskEdge], from_: str, to: str) -> bool:
    """Would adding ``from_ -> to`` close a loop?

    Asked BEFORE writing, so a whole plan can be refused before a single byte
    reaches the log. The fold survives a cycle either way, but a graph that
    reports "cycle" is a graph nobody can use, and it is far kinder to refuse
    the edge that would create one than to accept it and describe the wreckage.
    """
    if from_ == to:
        return True
    # Can we already get from `to` back to `from_`? If so, the new edge closes it.
    adjacency: dict[str, list[str]] = {}
    for e in edges:
        adjacency.setdefault(e.from_, []).append(e.to)
    seen = {to}
    stack = [to]
    while stack:
        node = stack.pop()
        if node == from_:
            return True
        for nxt in adjacency.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False
