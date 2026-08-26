"""Fold the comms event log into the current state of the world.

Port of comms' internal/state/state.go (the coordination half: the task graph
lands separately). Given the events, it answers, which claims are live, who is
still around, what was found, and which claims were refused.

Two properties are load-bearing and everything below is arranged around them:

  * PURE. No file IO, no clock reads, no mutation of the caller's events. The
    same events in any order fold to the same State, so the projection onto the
    map is reproducible and the fold can be replayed, cached or compared.
  * TOTAL. `fold` never raises, whatever it is handed. It runs on the pre-edit
    hot path in front of every agent write; a reducer that can throw turns one
    corrupt line in an append-only log into an agent that can no longer edit
    anything. Malformed events are dropped, exactly as the Go reducer drops
    them, and the loud "the log is corrupt" story stays with the log reader.

Everything here reads the event stream; nothing here writes it. The log is
truth, this is only the view of it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import task as _task
from .scope import Scope
from .log import KNOWN_TYPES as _LOG_TYPES

KNOWN_TYPES = frozenset(_LOG_TYPES)
from .scope import overlaps as scopes_overlap
from .scope import parse as parse_scope

__all__ = [
    "Blocked",
    "Claim",
    "EndedCommsSession",
    "Finding",
    "Note",
    "Ref",
    "Release",
    "Session",
    "State",
    "fold",
]


# The types this build understands. An event of any other type is dropped, the
# way Go's Decode rejects it, but the task types stay in the set even though
# the task graph is not folded yet, because a dropped event is also a lost
# heartbeat and a lost entry in the session window counts.
# Imported, never re-listed. log.py keeps the single ordered tuple precisely
# because the Go port had two hand-maintained whitelists in two packages and
# adding a type meant remembering both, and this module had quietly become the
# second one. When the two drift, the reader accepts events the reducer silently
# drops, and a dropped event is a lost heartbeat that can suppress a legitimate
# steal.
_UNUSED_LOCAL_TYPES = frozenset(
    {
        "hello",
        "claim",
        "release",
        "note",
        "finding",
        "blocked",
        "task",
        "task_edge",
        "task_state",
    }
)

# Go rejects a zero time.Time, which is what an absent `ts` unmarshals to. The
# same string reaching us from a Go writer means the same thing: no timestamp.
_ZERO_TIME = datetime(1, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    """An active exclusive claim on a scope, keyed in State by its event ID."""

    id: str
    ts: datetime
    actor: str
    scope: Scope
    intent: str = ""
    task: str = ""
    session_id: str = ""
    session_name: str = ""
    # Set when this claim displaced another one (an arbitrated steal).
    stolen_from_id: str = ""
    steal_reason: str = ""
    arbitrator: str = ""


@dataclass
class Session:
    """The most recent hello per actor, plus that actor's passive heartbeat."""

    actor: str
    ts: datetime
    label: str = ""
    base_name: str = ""
    hostname: str = ""
    tty: str = ""
    leader: bool = False
    # last_seen is the timestamp of this actor's most recent event of ANY type,
    # not just its hello. Every command an agent runs proves it is still alive,
    # so liveness is judged from real activity instead of a one-shot greeting.
    # Always >= ts.
    #
    # It is actor-global on purpose, not session-scoped: "is this agent's
    # process alive?" is a property of the process, not of whichever named
    # coordination window its events happened to land in.
    last_seen: datetime | None = None
    agent_session: str = ""
    session_id: str = ""
    session_name: str = ""
    # Model identity, best effort. Recorded so a later reader can say whether a
    # verification was genuinely independent or came from something with the
    # same blind spots. Absent on any hello written before comms 0.3.0.
    model: str = ""
    vendor: str = ""
    tier: int = 0


@dataclass
class Ref:
    """A `kind:value` pair attached to a finding."""

    kind: str
    value: str


@dataclass
class Finding:
    id: str
    ts: datetime
    actor: str
    category: str = ""
    summary: str = ""
    refs: list[Ref] = field(default_factory=list)
    session_id: str = ""
    session_name: str = ""


@dataclass
class Note:
    id: str
    ts: datetime
    actor: str
    body: str = ""
    session_id: str = ""
    session_name: str = ""


@dataclass
class Release:
    """A completed claim release: the outcome plus the scopes it freed."""

    id: str
    ts: datetime
    actor: str
    result: str = ""
    scopes: list[str] = field(default_factory=list)
    #: Set when somebody OTHER than the holder ended the claim. Both are
    #: written by `release --force` and both were dropped on the way in, so an
    #: arbitrated handover folded as an ordinary release and the board could not
    #: tell "she finished" from "somebody took it off her".
    original_actor: str = ""
    arbitrator: str = ""
    session_id: str = ""
    session_name: str = ""


@dataclass
class Blocked:
    """One refused claim: who wanted what, and who already held it.

    This is the only event that is evidence the tool did its job. Without it a
    prevented collision leaves no trace at all, which is how a log of thousands
    of claims can honestly report that it never prevented anything.
    """

    id: str
    ts: datetime
    actor: str
    scope: str = ""
    intent: str = ""
    holder: str = ""
    holder_scope: str = ""
    #: What the actor was trying to do ("verified", "rejected") when a TASK
    #: transition was refused, and every holder rather than only the first.
    #: Both are written and neither was read, so a refusal panel could say who
    #: was stopped but not what they were stopped from doing.
    attempted: str = ""
    holders: list[str] = field(default_factory=list)
    #: A refusal against a TASK rather than a scope: a self-review, a failing
    #: check: has no scope at all. Without these two the board could only say
    #: "@alice was refused", dropping the only facts worth having.
    task: str = ""
    reason: str = ""
    session_id: str = ""
    session_name: str = ""


@dataclass
class EndedCommsSession:
    """An archived coordination window, closed by a session-end release."""

    id: str
    ts_started: datetime | None
    ts_ended: datetime
    session_id: str = ""
    name: str = ""
    ended_by: str = ""
    reason: str = ""
    actors: list[str] = field(default_factory=list)
    released_refs: list[str] = field(default_factory=list)
    event_count: int = 0
    claim_count: int = 0
    finding_count: int = 0
    note_count: int = 0


@dataclass
class State:
    """The materialized view of the event log."""

    # ACTIVE claims only, keyed by claim event ID. Released and stolen claims
    # are gone from the map, not flagged in it.
    claims: dict[str, Claim] = field(default_factory=dict)
    # Most recent hello per actor.
    sessions: dict[str, Session] = field(default_factory=dict)
    # Chronological feeds. Callers filter by `since`.
    findings: list[Finding] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)
    ended_comms_sessions: list[EndedCommsSession] = field(default_factory=list)
    # The work half. Keyed by slug because a task has a lifecycle (the claims
    # precedent); edges are a list because an edge is an append-only fact.
    tasks: dict[str, "_task.Task"] = field(default_factory=dict)
    task_edges: list["_task.TaskEdge"] = field(default_factory=list)
    # Transitions the fold would not make. Kept, not dropped: a refusal is the
    # moment the rule did its job, and a rule that enforces itself in silence
    # looks exactly like a rule nobody wrote.
    refused_task_states: list["_task.RefusedTransition"] = field(default_factory=list)

    def independence_of(self, verifier: str, doer: str) -> str:
        """Whether a verification came from something with different blind spots.

        Read from each actor's hello. "Verified" and "verified by another
        instance of the same model" are different claims and must not read
        alike; when we cannot tell, say so rather than implying the stronger one.
        """
        vs = self.sessions.get(verifier)
        ds = self.sessions.get(doer)
        v = (vs.vendor if vs else "") or ""
        d = (ds.vendor if ds else "") or ""
        if not v or not d:
            return "unknown"
        return "independent" if v != d else "same-family"

    def conflicts_for(self, scope: Scope, actor: str = "") -> list[Claim]:
        """Active claims overlapping `scope` that are held by somebody else.

        Takes an already-parsed scope, like the Go does, so that the "this scope
        string is not valid" error belongs to the caller who can report it.
        Swallowing a parse failure here would answer "no conflicts" for an
        unparseable scope, which is the one wrong direction to fail in.

        An empty `actor` means "any other", since every claim has an actor.
        """
        out = [
            claim
            for claim in self.claims.values()
            if claim.actor != actor and scopes_overlap(claim.scope, scope)
        ]
        out.sort(key=lambda c: c.ts)
        return out

    def active_claims_by_actor(self, actor: str) -> list[Claim]:
        """Every active claim held by `actor`, oldest first."""
        out = [claim for claim in self.claims.values() if claim.actor == actor]
        out.sort(key=lambda c: c.ts)
        return out

    def claim_by_id(self, claim_id: str) -> Claim | None:
        """Look up an active claim, accepting an unambiguous ID prefix.

        Agents copy claim IDs by hand, so a prefix is the shape they actually
        type. An ambiguous prefix returns None rather than a guess.
        """
        if not claim_id:
            return None
        exact = self.claims.get(claim_id)
        if exact is not None:
            return exact
        match: Claim | None = None
        for claim in self.claims.values():
            if claim.id.startswith(claim_id):
                if match is not None:
                    return None
                match = claim
        return match

    def latest_claim_by_actor(self, actor: str) -> Claim | None:
        """The most recently opened active claim owned by `actor`."""
        latest: Claim | None = None
        for claim in self.claims.values():
            if claim.actor != actor:
                continue
            if latest is None or claim.ts > latest.ts:
                latest = claim
        return latest


# ---------------------------------------------------------------------------
# The reducer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Event:
    """A validated event. Building one is this module's only trust boundary."""

    ts: datetime
    id: str
    actor: str
    type: str
    scope: tuple[str, ...]
    data: Mapping[str, Any]


@dataclass
class _Window:
    """Running tallies for one named coordination window."""

    start: datetime
    actors: set[str] = field(default_factory=set)
    events: int = 0
    claims: int = 0
    findings: int = 0
    notes: int = 0


def fold(events: Iterable[Any]) -> State:
    """Replay events in chronological order and return the resulting State.

    `events` may be decoded JSON objects straight off the log or objects with
    matching attributes; either way nothing in it is modified.

    Ordering policy: timestamp order, with a STABLE sort so events sharing a
    timestamp keep their append order. The log is append-only under a per-repo
    lock, so append order already IS causal order; sorting only re-seats a line
    written out of wall-clock order. We deliberately do NOT sort by event ID:
    same-millisecond ULIDs are not guaranteed to sort causally, which would
    silently reorder a claim against its own steal or release.
    """
    ordered = [ev for ev in (_validate(raw) for raw in events) if ev is not None]
    ordered.sort(key=lambda ev: ev.ts)

    # A release closes its claim WHICHEVER ORDER the two fold in.
    #
    # The sort is by timestamp so that concatenated logs merge sensibly, but a
    # clock step backwards between two commands: an NTP correction is enough:
    # can seat a release before the claim it closes. The release then popped
    # nothing, the claim folded afterwards, and it stayed active FOREVER while the
    # exact-conflict layer blocked every other agent on a scope the log plainly
    # says was released. Remembering which ids a release named makes the pair
    # order-independent, which fixes that without giving up the sort.
    released_ids: set[str] = set()
    # Claims displaced by a steal. Kept separately from released_ids because a
    # steal and a release are different facts: a release is the holder letting
    # go, a steal is somebody taking it off them, and only the second one has to
    # survive being folded BEFORE the claim it displaces.
    # Workers seen before their task folded; see the claim branch below.
    pending_workers: dict[str, list[str]] = {}
    stolen_ids: set[str] = set()
    # (from, to) -> position in state.task_edges, so an edge upsert is a lookup
    # rather than a walk. Local to the fold: it is scaffolding for building the
    # list, not state anybody reads afterwards.
    task_edge_index: dict[tuple[str, str], int] = {}
    # session_id -> claim ids opened under it. Ending a NAMED session has to
    # drop that session's claims, and doing it by scanning every open claim made
    # the fold quadratic: 8,000 session ends over 8,000 claims took 8s, on the
    # path in front of every agent tool call. The index is treated as a HINT:
    # every id is re-checked against the real claim before anything is deleted:
    # so a drifted entry can only cost a wasted lookup, never a wrong deletion.
    claims_by_session: dict[str, set[str]] = {}
    # Same shape, same reason, for the roster: ending a named session also drops
    # the actors registered under it, and that scan is the larger half of the
    # cost: one pass over every actor, per end.
    actors_by_session: dict[str, set[str]] = {}

    state = State()

    # The unnamed window: everything since the last global comms-session end.
    window_start: datetime | None = None
    window_actors: set[str] = set()
    window_events = window_claims = window_findings = window_notes = 0
    named_windows: dict[str, _Window] = {}

    # last_seen[actor] = that actor's most recent event of ANY type. Because
    # `ordered` ascends, the final write per actor is the maximum, so no
    # comparison is needed here: that is the passive heartbeat.
    last_seen: dict[str, datetime] = {}

    for ev in ordered:
        if window_start is None:
            window_start = ev.ts
        window_events += 1
        window_actors.add(ev.actor)
        last_seen[ev.actor] = ev.ts
        if ev.type == "claim":
            window_claims += 1
        elif ev.type == "finding":
            window_findings += 1
        elif ev.type == "note":
            window_notes += 1

        session_id = _string_of(ev.data, "comms_session_id")
        named: _Window | None = None
        if session_id:
            named = named_windows.get(session_id)
            if named is None:
                named = _Window(start=ev.ts)
                named_windows[session_id] = named
            named.events += 1
            named.actors.add(ev.actor)
            if ev.type == "claim":
                named.claims += 1
            elif ev.type == "finding":
                named.findings += 1
            elif ev.type == "note":
                named.notes += 1

        if ev.type == "hello":
            _sid = _string_of(ev.data, "comms_session_id")
            if _sid:
                actors_by_session.setdefault(_sid, set()).add(ev.actor)
            state.sessions[ev.actor] = Session(
                actor=ev.actor,
                ts=ev.ts,
                label=_string_of(ev.data, "label"),
                base_name=_string_of(ev.data, "base_name"),
                hostname=_string_of(ev.data, "hostname"),
                tty=_string_of(ev.data, "tty"),
                leader=_bool_of(ev.data, "leader"),
                agent_session=_string_of(ev.data, "agent_session"),
                session_id=session_id,
                session_name=_string_of(ev.data, "comms_session_name"),
                model=_string_of(ev.data, "model"),
                vendor=_string_of(ev.data, "vendor"),
                tier=_int_of(ev.data, "tier"),
            )

        elif ev.type == "claim":
            claim = _claim_from_event(ev)
            if claim is None:
                continue
            # An atomic steal: taking the scope and freeing the displaced claim
            # are one event, so no window exists where both or neither is held.
            if claim.stolen_from_id:
                state.claims.pop(claim.stolen_from_id, None)
                # Remember it, do NOT forget it. This line used to discard the
                # displaced id, and that resurrected the claim it was meant to
                # end: a clock step can put the steal's timestamp BEFORE the
                # claim it displaces, so the steal folds first, pops nothing
                # (the claim is not there yet), and clears the very record that
                # would have suppressed it. The displaced claim then folds
                # normally and both agents hold the same file: with the board
                # showing two holders and the pre-edit hook letting both edit.
                stolen_ids.add(claim.stolen_from_id)
            if claim.id in released_ids or claim.id in stolen_ids:
                # Its own release or the steal that took it already folded,
                # ahead of it, because a clock step put an earlier timestamp on
                # the later command. Activating it now would strand it
                # permanently, or hand out a second copy of held ground.
                continue
            state.claims[claim.id] = claim
            # Taking ground for a task makes you one of its authors, and that
            # is permanent. Recorded HERE, in log order, so the review gate
            # sees it on the very next event: `doers` is derived after the
            # fold has finished, far too late for a rule the fold enforces.
            if claim.task:
                _t = state.tasks.get(claim.task)
                if _t is not None:
                    if claim.actor not in _t.workers:
                        _t.workers.append(claim.actor)
                else:
                    # The task has not folded yet. fold() sorts by TIMESTAMP, so
                    # a claim from a machine whose clock runs two seconds behind
                    # arrives before the `task` event that declares what it is
                    # tagged to. Dropping it here lost the author record
                    # PERMANENTLY: unlike a claim, nothing later repairs it:
                    # and the co-worker's sign-off then recorded as a genuine
                    # independent review. The steal and release paths in this
                    # same loop already carry sets forward for exactly this
                    # reason; this one had no equivalent.
                    pending_workers.setdefault(claim.task, []).append(claim.actor)
            if claim.session_id:
                claims_by_session.setdefault(claim.session_id, set()).add(claim.id)

        elif ev.type == "release":
            refs = _ref_list(ev.data, "refs")
            released_scopes: list[str] = []
            for ref in refs:
                held = state.claims.pop(ref, None)
                # Remember it either way. If the claim has not folded yet, this is
                # what stops it being activated when it does.
                released_ids.add(ref)
                if held is not None:
                    released_scopes.append(str(held.scope))

            # Session lifecycle releases: retire, leader transfer, session end
            # carry refs too, because they sweep up the claims they close. But
            # they are coordination admin, not finished work, so they stay out
            # of the "recently completed" feed.
            housekeeping = (
                _bool_of(ev.data, "session_retire")
                or _bool_of(ev.data, "leader_transfer")
                or _bool_of(ev.data, "comms_session_end")
            )
            if refs and not housekeeping:
                state.releases.append(
                    Release(
                        id=ev.id,
                        ts=ev.ts,
                        actor=ev.actor,
                        result=_string_of(ev.data, "result"),
                        scopes=released_scopes,
                        original_actor=_string_of(ev.data, "original_actor"),
                        arbitrator=_string_of(ev.data, "arbitrator"),
                        session_id=session_id,
                        session_name=_string_of(ev.data, "comms_session_name"),
                    )
                )

            if _bool_of(ev.data, "session_retire"):
                state.sessions.pop(_string_of(ev.data, "retired_actor"), None)

            if _bool_of(ev.data, "leader_transfer"):
                for session in state.sessions.values():
                    session.leader = False
                new_leader = state.sessions.get(_string_of(ev.data, "leader_actor"))
                if new_leader is not None:
                    new_leader.leader = True

            if _bool_of(ev.data, "comms_session_end"):
                reason = _string_of(ev.data, "reason") or _string_of(ev.data, "result")
                # Sort ONLY the set actually used. This used to sort the global
                # window's actors first and then throw that away for a named
                # end: one sort of every actor ever seen, per end, which made
                # the fold quadratic: 8,000 named ends took 8 seconds on the
                # path in front of every agent tool call. The profile put 3.2s
                # of 4.7s in this one call.
                if session_id and named is not None:
                    started_at = named.start
                    actors = _sorted_actors(named.actors)
                    counts = (named.events, named.claims, named.findings, named.notes)
                else:
                    started_at = window_start
                    actors = _sorted_actors(window_actors)
                    counts = (window_events, window_claims, window_findings, window_notes)
                state.ended_comms_sessions.append(
                    EndedCommsSession(
                        id=ev.id,
                        session_id=session_id,
                        name=_string_of(ev.data, "comms_session_name"),
                        ts_started=started_at,
                        ts_ended=ev.ts,
                        ended_by=ev.actor,
                        reason=reason,
                        actors=actors,
                        released_refs=list(refs),
                        event_count=counts[0],
                        claim_count=counts[1],
                        finding_count=counts[2],
                        note_count=counts[3],
                    )
                )
                if not session_id:
                    # A global end archives everything and starts over.
                    state.claims = {}
                    state.sessions = {}
                    named_windows = {}
                    window_start = None
                    window_actors = set()
                    window_events = window_claims = window_findings = window_notes = 0
                    # last_seen is window-scoped like window_actors: every
                    # surviving actor has to say hello again, so keeping its old
                    # heartbeat would let a pre-wipe timestamp resurrect a
                    # session that no longer exists.
                    last_seen = {}
                else:
                    # A named end closes only its own window; the global one
                    # carries on.
                    for claim_id in claims_by_session.pop(session_id, set()):
                        # Re-check rather than trust: the claim may have been
                        # released or stolen since, and the index is only a hint.
                        existing = state.claims.get(claim_id)
                        if existing is not None and existing.session_id == session_id:
                            del state.claims[claim_id]
                    for actor in actors_by_session.pop(session_id, set()):
                        existing = state.sessions.get(actor)
                        if existing is not None and existing.session_id == session_id:
                            del state.sessions[actor]
                    named_windows.pop(session_id, None)

        elif ev.type == "blocked":
            state.blocked.append(
                Blocked(
                    id=ev.id,
                    ts=ev.ts,
                    actor=ev.actor,
                    scope=_string_of(ev.data, "scope"),
                    intent=_string_of(ev.data, "intent"),
                    holder=_string_of(ev.data, "holder"),
                    holder_scope=_string_of(ev.data, "holder_scope"),
                    attempted=_string_of(ev.data, "attempted"),
                    holders=[str(h) for h in (ev.data.get("holders") or [])
                             if isinstance(h, (str, int))] if isinstance(ev.data, dict) else [],
                    task=_string_of(ev.data, "task"),
                    reason=_string_of(ev.data, "reason"),
                    session_id=session_id,
                    session_name=_string_of(ev.data, "comms_session_name"),
                )
            )

        elif ev.type == "finding":
            state.findings.append(
                Finding(
                    id=ev.id,
                    ts=ev.ts,
                    actor=ev.actor,
                    category=_string_of(ev.data, "category"),
                    summary=_string_of(ev.data, "summary"),
                    refs=_parse_refs(ev.data),
                    session_id=session_id,
                    session_name=_string_of(ev.data, "comms_session_name"),
                )
            )

        elif ev.type == "note":
            state.notes.append(
                Note(
                    id=ev.id,
                    ts=ev.ts,
                    actor=ev.actor,
                    body=_string_of(ev.data, "body"),
                    session_id=session_id,
                    session_name=_string_of(ev.data, "comms_session_name"),
                )
            )

        elif ev.type == "task":
            _task.apply_task(state.tasks, ev, _string_of, _int_of, _ref_list)
            waiting = pending_workers.pop(_string_of(ev.data, "task"), None)
            if waiting:
                _t = state.tasks.get(_string_of(ev.data, "task"))
                if _t is not None:
                    for who in waiting:
                        if who not in _t.workers:
                            _t.workers.append(who)

        elif ev.type == "task_edge":
            _task.apply_task_edge(state.task_edges, ev, _string_of, task_edge_index)

        elif ev.type == "task_state":
            _task.apply_task_state(
                state.tasks, state.refused_task_states, ev,
                _string_of, _ref_list, state.independence_of,
                state.sessions,
            )

    # Stamp the heartbeat onto whichever sessions survived. A hello is itself
    # one of the actor's events, so last_seen can only be >= ts; the guard is
    # there for a log where it somehow is not.
    for session in state.sessions.values():
        seen = last_seen.get(session.actor)
        session.last_seen = seen if seen is not None and seen > session.ts else session.ts

    # Last, because it reads the finished claim map and the finished sessions:
    # who is doing a task comes from live claims, and whether a verification was
    # independent comes from the verifier's hello.
    _task.derive_phases(state.tasks, state.task_edges, state.claims.values(),
                        state.sessions)

    return state


def _claim_from_event(ev: _Event) -> Claim | None:
    """Build a Claim, or None if the event cannot support one."""
    if not ev.scope:
        return None
    try:
        parsed = parse_scope(ev.scope[0])
    except Exception:
        # A scope that will not parse cannot be honoured or displayed, so the
        # claim is dropped rather than half-applied. Broad on purpose: the
        # reducer must survive whatever scope.py decides to raise, and it is not
        # the place that gets to declare the log corrupt.
        return None
    return Claim(
        id=ev.id,
        ts=ev.ts,
        actor=ev.actor,
        scope=parsed,
        intent=_string_of(ev.data, "intent"),
        task=_string_of(ev.data, "task"),
        session_id=_string_of(ev.data, "comms_session_id"),
        session_name=_string_of(ev.data, "comms_session_name"),
        stolen_from_id=_string_of(ev.data, "steals"),
        steal_reason=_string_of(ev.data, "steal_reason"),
        arbitrator=_string_of(ev.data, "arbitrator"),
    )


def _sorted_actors(actors: set[str]) -> list[str]:
    return sorted(actor for actor in actors if actor)


# ---------------------------------------------------------------------------
# The trust boundary
#
# Everything below turns whatever the caller handed us into something the
# reducer can rely on. Nothing here raises and nothing here recurses: no
# helper walks a nested value, so a self-referential JSON object (data["d"] is
# data) is read one level deep and cannot loop.
# ---------------------------------------------------------------------------


def _validate(raw: Any) -> _Event | None:
    """Return a validated _Event, or None to drop this one.

    Mirrors the checks Go performs when decoding a log line: an event without an
    ID, an actor, a known type or a timestamp is not something the reducer can
    place, so it never reaches the fold. Doing it here means `fold` can be fed
    raw JSON objects without a decode step in front of it.
    """
    ts = _timestamp(_field(raw, "ts"))
    if ts is None:
        return None
    event_id = _field(raw, "id")
    actor = _field(raw, "actor")
    event_type = _field(raw, "type")
    if not isinstance(event_id, str) or not event_id:
        return None
    if not isinstance(actor, str) or not actor:
        return None
    if not isinstance(event_type, str) or event_type not in KNOWN_TYPES:  # from log.py
        return None

    raw_scope = _field(raw, "scope")
    scope: tuple[str, ...] = ()
    if raw_scope is not None:
        # `scope` is []string in Go and `list[str] | None` in log.py, and both
        # reject the whole line when it is anything else. Same reasoning as the
        # non-Mapping `data` below: a scope of the wrong shape is not an event
        # with some elements missing, it is a line that does not conform to the
        # event shape at all.
        #
        # WHAT WENT WRONG ONCE: this used to FILTER: `tuple(s for s in
        # raw_scope if isinstance(s, str))`, which is strictly more permissive
        # than either sibling and let corruption materialise as state:
        #   * {"scope":[123,"src/api/server.ts"]} dropped the 123 and folded an
        #     ACTIVE claim on src/api/server.ts, blocking every other agent on a
        #     scope no writer ever wrote.
        #   * {"scope":"src/api/server.ts"} (a bare string, not an array) is not
        #     a list at all, so it fell through to the empty tuple and the event
        #     still folded far enough to bump the actor's last_seen: handing a
        #     dead agent a fresh heartbeat and suppressing a legitimate steal.
        # Neither line is salvageable, so neither line is salvaged.
        if not isinstance(raw_scope, (list, tuple)):
            return None
        if not all(isinstance(s, str) for s in raw_scope):
            return None
        scope = tuple(raw_scope)

    data = _field(raw, "data")
    if data is None:
        # `data` is omitempty on the way out and nullable on the way in; absent
        # simply means the event carries no type-specific fields.
        data = {}
    elif not isinstance(data, Mapping):
        # A `data` that is a list or a scalar is not an event with an empty bag,
        # it is a line that does not conform to the event shape at all. Go's
        # unmarshal into map[string]interface{} rejects the whole line. Folding
        # it as if its fields were merely missing would invent an empty note or
        # a scopeless release out of corruption.
        return None

    return _Event(ts=ts, id=event_id, actor=actor, type=event_type, scope=scope, data=data)


def _field(raw: Any, key: str) -> Any:
    """Read one top-level field from a decoded JSON object or an event object.

    The log yields dicts and an event type may yield objects; the field names
    are the same either way, so accepting both costs one branch and saves the
    caller a conversion pass on the hot path.
    """
    if isinstance(raw, Mapping):
        return raw.get(key)
    return getattr(raw, key, None)


def _timestamp(value: Any) -> datetime | None:
    """Normalize an event timestamp to an aware UTC datetime, or None.

    Every timestamp in the log is RFC3339 with a trailing Z. Accepting that Z is
    exactly why the floor is Python 3.11: `fromisoformat` refuses it before
    that, so there is no fallback parser here on purpose.

    A naive value is read as UTC rather than rejected, because the sort in
    `fold` uses a single key: mixing aware and naive datetimes in one comparison
    raises TypeError, and the reducer is not allowed to raise.
    """
    if isinstance(value, datetime):
        stamped = value
    elif isinstance(value, str) and value:
        try:
            stamped = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    else:
        try:
            stamped = stamped.astimezone(timezone.utc)
        except (OverflowError, OSError):
            # WHAT WENT WRONG ONCE, so it is not reintroduced: the try above
            # only wrapped fromisoformat and only caught ValueError, but the
            # shift to UTC is a SECOND, independent failure point. A timestamp
            # sitting within its own UTC offset of datetime.min/datetime.max
            # moves out of Python's representable range when the offset is
            # applied, and datetime signals that with OverflowError, which is
            # not a ValueError, so it escaped the guard and took `fold` down.
            # On the pre-edit hot path that is one corrupt log line disabling
            # every agent write, exactly the failure the TOTAL property exists
            # to prevent. Repro: {"ts":"9999-12-31T23:59:59-05:00"} shifts
            # forward past year 9999; {"ts":"0001-01-01T00:00:00+05:00"} shifts
            # backward past year 1. (OSError joins it only to match the same
            # guard in log.py's _to_utc; on an aware datetime the shift is pure
            # arithmetic and OverflowError is what it actually raises.)
            #
            # There is no Go behaviour to copy here: Go's time.Time spans a far
            # wider range and accepts both instants happily, so this is a limit
            # of the port, not of the format. Dropping is the right direction
            # to fail in: the alternative, clamping to datetime.min/max, would
            # let a corrupt line masquerade as a real instant, and a claim
            # pinned at datetime.max sorts last forever, wins every
            # latest_claim_by_actor and never ages out.
            return None
    # Go's zero time.Time is what an absent `ts` unmarshals to, and Go rejects
    # it. The same instant arriving as a string means the same thing.
    if stamped == _ZERO_TIME:
        return None
    return stamped


def _string_of(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _bool_of(data: Mapping[str, Any], key: str) -> bool:
    # Only a real JSON bool counts, never a truthy value: Go type-asserts, and a
    # release whose "comms_session_end" is the string "false" must not wipe the
    # world.
    return data.get(key) is True


def _int_of(data: Mapping[str, Any], key: str) -> int:
    """Read a JSON number as an int, the way Go's reducer does.

    Go unmarshals every JSON number as float64 and truncates toward zero, while
    Python's json gives int for integers and float for the rest. Normalizing
    here: the one place numbers enter: is what keeps the two agreeing. A bool
    is an int subclass in Python but is not a number to Go's type switch, so it
    is excluded; NaN and the infinities are excluded because int() raises on
    them and this function may not.
    """
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return int(value)


def _ref_list(data: Mapping[str, Any], key: str) -> list[str]:
    """Read `refs` as a list of claim IDs, tolerating the single-string form."""
    value = data.get(key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _parse_refs(data: Mapping[str, Any]) -> list[Ref]:
    """Read a finding's `refs`: a list of `{kind, value}` objects."""
    value = data.get("refs")
    if not isinstance(value, (list, tuple)):
        return []
    return [
        Ref(kind=_string_of(item, "kind"), value=_string_of(item, "value"))
        for item in value
        if isinstance(item, Mapping)
    ]
