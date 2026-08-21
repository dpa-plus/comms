"""Folding the log into "who holds what right now".

Two properties carry everything else, and both are about failure:

  TOTAL — `fold` runs on the pre-edit hot path, in front of every agent write.
  If it can raise, one bad line in an append-only log stops every agent in the
  repository from editing anything, permanently. So garbage is dropped, never
  thrown.

  PURE and ORDER-INDEPENDENT — the same events must fold to the same world
  whatever order they arrive in, because logs get concatenated, replayed and
  read while other processes append to them.

The rest is claim lifecycle: a claim that outlives its release blocks everyone
forever, and a claim that dies early lets two agents onto one function.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from comms_graph.scope import parse as parse_scope
from comms_graph.state import fold

UTC = timezone.utc
T0 = datetime(2026, 5, 22, 14, 30, tzinfo=UTC)


def at(seconds: int) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def event(id, type, actor="claude-3a1f", secs=0, scope=None, **data):
    ev = {"ts": at(secs), "id": id, "actor": actor, "type": type}
    if scope is not None:
        ev["scope"] = scope
    if data:
        ev["data"] = data
    return ev


def claim(id, scope, actor="claude-3a1f", secs=0, **data):
    return event(id, "claim", actor=actor, secs=secs, scope=[scope], **data)


def release(id, refs, actor="claude-3a1f", secs=0, **data):
    return event(id, "release", actor=actor, secs=secs, refs=refs, **data)


# ---------------------------------------------------------------------------
# TOTAL: the reducer may never raise
# ---------------------------------------------------------------------------

SELF_REFERENTIAL = {"ts": at(1), "id": "S", "actor": "a", "type": "note", "data": {}}
SELF_REFERENTIAL["data"]["self"] = SELF_REFERENTIAL["data"]

GARBAGE = [
    None,
    42,
    "not an event",
    [],
    {},
    {"ts": at(0)},                                                    # nothing else
    {"ts": "not-a-date", "id": "A", "actor": "a", "type": "note"},
    {"ts": None, "id": "A", "actor": "a", "type": "note"},
    {"ts": at(0), "id": "", "actor": "a", "type": "note"},
    {"ts": at(0), "id": "A", "actor": "", "type": "note"},
    {"ts": at(0), "id": "A", "actor": "a", "type": "unknown-to-this-build"},
    {"ts": at(0), "id": "A", "actor": "a", "type": "note", "data": "a string"},
    {"ts": at(0), "id": "A", "actor": "a", "type": "claim", "scope": "src/a.ts"},
    {"ts": "9999-12-31T23:59:59-05:00", "id": "A", "actor": "a", "type": "note"},
    {"ts": "0001-01-01T00:00:00+05:00", "id": "A", "actor": "a", "type": "note"},
    {"ts": "0001-01-01T00:00:00Z", "id": "A", "actor": "a", "type": "note"},
    SELF_REFERENTIAL,
]


@pytest.mark.parametrize("junk", GARBAGE, ids=[str(i) for i in range(len(GARBAGE))])
def test_no_single_event_can_take_the_reducer_down(junk):
    """IF THIS FAILS: one malformed or hostile line in the log disables every
    agent's ability to edit the repository, because the pre-edit check folds the
    log before every write. Dropping the line costs one event; raising costs the
    whole tool. (The loud "this log is damaged" story belongs to the reader,
    which the caller runs first.)"""
    good = claim("KEEP", "src/api/server.ts", secs=5)
    state = fold([good, junk])
    assert "KEEP" in state.claims


def test_a_scope_array_with_a_number_in_it_does_not_become_a_live_claim():
    """IF THIS FAILS: corruption materialises as authority. A line whose scope is
    `[123, "src/api/server.ts"]` was written by nothing we know, yet salvaging
    the string half hands out an ACTIVE claim on src/api/server.ts that blocks
    every other agent on ground no writer ever claimed — and no release exists
    that can free it."""
    state = fold([{"ts": at(0), "id": "X", "actor": "a", "type": "claim",
                   "scope": [123, "src/api/server.ts"]}])
    assert state.claims == {}


def test_an_event_that_was_dropped_does_not_prove_its_author_is_alive():
    """IF THIS FAILS: a corrupt line refreshes an actor's heartbeat, so a dead
    agent looks recently active — which is exactly what suppresses a legitimate
    steal of the ground it is still holding. Liveness must come only from events
    we actually understood."""
    events = [
        event("H", "hello", actor="ghost", secs=0),
        {"ts": at(600), "id": "BAD", "actor": "ghost", "type": "note", "data": ["not", "a", "map"]},
    ]
    session = fold(events).sessions["ghost"]
    assert session.last_seen == T0


def test_a_claim_whose_scope_will_not_parse_is_dropped_whole():
    """IF THIS FAILS: a claim is half-applied — it exists in the board with no
    usable territory, so it can never be matched by a conflict check and never
    be found by the release that was meant to close it."""
    assert fold([claim("X", "/etc/passwd")]).claims == {}
    assert fold([claim("X", "src/\x1b[31m.ts")]).claims == {}


# ---------------------------------------------------------------------------
# PURE and ORDER-INDEPENDENT
# ---------------------------------------------------------------------------


def test_the_same_events_in_any_order_give_the_same_world():
    """IF THIS FAILS: two agents reading one log disagree about who holds what,
    because their reads interleaved differently with somebody's appends. The
    board would then be a function of arrival order rather than of the log."""
    events = [
        event("H1", "hello", actor="alice", secs=0),
        claim("C1", "src/api/server.ts", actor="alice", secs=10, intent="charge"),
        event("H2", "hello", actor="bob", secs=20),
        claim("C2", "src/ui/App.tsx", actor="bob", secs=30),
        event("F1", "finding", actor="bob", secs=40, summary="n+1"),
        release("R1", ["C1"], actor="alice", secs=50, result="done"),
        claim("C3", "src/api/server.ts#refund", actor="bob", secs=60),
    ]
    baseline = fold(events)
    rng = random.Random(20260522)
    for _ in range(12):
        shuffled = events[:]
        rng.shuffle(shuffled)
        got = fold(shuffled)
        assert sorted(got.claims) == sorted(baseline.claims)
        assert sorted(got.sessions) == sorted(baseline.sessions)
        assert [f.id for f in got.findings] == [f.id for f in baseline.findings]
        assert [r.id for r in got.releases] == [r.id for r in baseline.releases]


def test_folding_does_not_consume_or_edit_the_caller_s_events():
    """IF THIS FAILS: the caller's copy of the log is mutated underneath it, so
    the second fold in one command (check, then re-check after appending) sees a
    different world than the first — and any caller that passes a generator gets
    an empty board on its second use."""
    events = [claim("C1", "src/a.ts", secs=1), release("R1", ["C1"], secs=2)]
    snapshot = [dict(e) for e in events]
    fold(events)
    fold(events)
    assert events == snapshot


# ---------------------------------------------------------------------------
# Claim lifecycle
# ---------------------------------------------------------------------------


def test_a_release_frees_its_claim():
    """IF THIS FAILS: nothing is ever given back and the repository seizes up
    after the first few claims."""
    state = fold([claim("C1", "src/a.ts", secs=1), release("R1", ["C1"], secs=2, result="done")])
    assert state.claims == {}
    assert [r.id for r in state.releases] == ["R1"]


def test_a_release_still_frees_its_claim_when_the_clock_stepped_backwards():
    """IF THIS FAILS: an NTP correction between two commands — or two machines
    a second apart — seats the release before the claim it closes. The claim then
    folds afterwards and stays ACTIVE FOREVER, blocking every other agent on a
    scope the log plainly says was released, with no command that can free it."""
    state = fold([
        claim("C1", "src/a.ts", secs=10),
        release("R1", ["C1"], secs=1),  # timestamped BEFORE the claim it closes
    ])
    assert state.claims == {}


def test_a_steal_leaves_exactly_one_holder():
    """IF THIS FAILS: an arbitrated steal leaves either two holders (both agents
    edit) or none (the ground is unowned while someone is working on it). The
    take and the displacement are one event precisely so no window exists where
    the world is in between."""
    state = fold([
        claim("C1", "src/a.ts", actor="alice", secs=1),
        claim("C2", "src/a.ts", actor="bob", secs=2, steals="C1", steal_reason="stalled"),
    ])
    assert [c.actor for c in state.claims.values()] == ["bob"]
    assert state.claims["C2"].stolen_from_id == "C1"


def test_the_string_false_does_not_end_the_session():
    """IF THIS FAILS: a badly-encoded field — `"comms_session_end": "false"`,
    which any JSON-by-string-formatting writer can produce — wipes every active
    claim and every session in the repository. Truthiness is not a protocol."""
    state = fold([
        claim("C1", "src/a.ts", secs=1),
        release("R1", [], secs=2, comms_session_end="false"),
    ])
    assert "C1" in state.claims


def test_ending_a_session_hands_every_piece_of_ground_back():
    """IF THIS FAILS: the next session starts blocked by claims belonging to
    agents that no longer exist — and nobody has the claim ids to release them,
    so the repository stays locked until somebody deletes the store by hand.
    What was FOUND has to survive the same wipe: findings are the durable output
    of a session, while claims are only its scaffolding."""
    state = fold([
        event("H", "hello", actor="alice", secs=0),
        claim("C1", "src/a.ts", actor="alice", secs=1),
        event("F", "finding", actor="alice", secs=2, summary="n+1 in charge()"),
        release("R1", ["C1"], actor="alice", secs=3,
                comms_session_end=True, reason="wrapped up"),
    ])
    assert state.claims == {}
    assert state.sessions == {}
    assert [f.id for f in state.findings] == ["F"]
    (ended,) = state.ended_comms_sessions
    assert ended.reason == "wrapped up"
    assert (ended.claim_count, ended.finding_count) == (1, 1)
    assert ended.actors == ["alice"]


def test_housekeeping_releases_stay_out_of_the_finished_work_feed():
    """IF THIS FAILS: "recently completed" fills up with session admin — retires,
    leader transfers, session ends — and the one thing an agent reads to find out
    what its colleagues actually finished becomes noise."""
    state = fold([
        claim("C1", "src/a.ts", secs=1),
        release("R1", ["C1"], secs=2, session_retire=True, retired_actor="claude-3a1f"),
    ])
    assert state.releases == []
    assert state.claims == {}


def test_a_refusal_is_recorded_so_the_tool_can_prove_it_did_something():
    """IF THIS FAILS: a prevented collision leaves no trace — which is how a real
    store of thousands of claims could honestly report that it had never stopped
    anything, and how the whole feature looks like pure overhead to whoever pays
    for it."""
    state = fold([{
        "ts": at(3), "id": "B1", "actor": "bob", "type": "blocked",
        # the refused scope travels in `data`, because the claim was never made
        "data": {"scope": "src/a.ts", "holder": "alice",
                 "holder_scope": "src/a.ts", "intent": "refactor"},
    }])
    (blocked,) = state.blocked
    assert (blocked.actor, blocked.holder, blocked.scope) == ("bob", "alice", "src/a.ts")


# ---------------------------------------------------------------------------
# Asking the board a question
# ---------------------------------------------------------------------------


def board():
    return fold([
        claim("C1", "src/api/server.ts", actor="alice", secs=10),
        claim("C2", "src/ui/App.tsx", actor="bob", secs=20),
        claim("C3", "src/api/**", actor="carol", secs=30),
    ])


def test_you_are_never_blocked_by_your_own_claim():
    """IF THIS FAILS: an agent that re-checks or widens its own claim blocks
    itself and cannot continue its own work."""
    mine_only = fold([claim("C1", "src/api/server.ts", actor="alice", secs=10)])
    assert mine_only.conflicts_for(parse_scope("src/api/server.ts"), "alice") == []
    # ...and holding one scope does not hide somebody else's overlapping one.
    assert {c.actor for c in board().conflicts_for(
        parse_scope("src/api/server.ts"), "alice")} == {"carol"}


def test_everybody_else_s_overlap_is_reported_even_when_it_is_not_identical():
    """IF THIS FAILS: only exact string matches are caught, so the glob claim
    (`src/api/**`) and the file claim under it are declared independent — the
    coarse claim that was meant to protect an area protects nothing."""
    hits = board().conflicts_for(parse_scope("src/api/server.ts#charge"), "dave")
    assert {c.actor for c in hits} == {"alice", "carol"}


def test_unrelated_ground_is_not_a_conflict():
    """IF THIS FAILS: everything blocks everything, agents wait on each other for
    no reason, and the first thing anyone learns is how to bypass the check."""
    assert board().conflicts_for(parse_scope("docs/README.md"), "dave") == []


def test_the_oldest_holder_is_named_first():
    """IF THIS FAILS: the refusal points at whoever happens to come out of a hash
    map first instead of at the person who has been holding the ground longest —
    which is the person you actually have to go and talk to."""
    hits = board().conflicts_for(parse_scope("src/api/server.ts"), "dave")
    assert [c.actor for c in hits] == ["alice", "carol"]


def test_an_empty_actor_means_everybody():
    """IF THIS FAILS: a caller that has no actor identity yet (a hook, a status
    read) is told the board is empty and reports an all-clear."""
    assert len(board().conflicts_for(parse_scope("src/api/server.ts"), "")) == 2


def test_your_own_claims_come_back_oldest_first():
    """IF THIS FAILS: `release --all`-style flows and any "what am I holding"
    display order themselves nondeterministically, so an agent cannot reason
    about which of its claims is the stale one."""
    state = fold([
        claim("C1", "src/a.ts", actor="alice", secs=30),
        claim("C2", "src/b.ts", actor="alice", secs=10),
        claim("C3", "src/c.ts", actor="bob", secs=20),
    ])
    assert [c.id for c in state.active_claims_by_actor("alice")] == ["C2", "C1"]


def test_an_ambiguous_claim_id_prefix_is_refused_rather_than_guessed():
    """IF THIS FAILS: an agent typing a short claim id releases or steals a
    DIFFERENT claim than the one it meant — silently, because both ids start the
    same way. Refusing costs one retry with more characters."""
    state = fold([
        claim("01ABCDEF", "src/a.ts", secs=1),
        claim("01ABCXYZ", "src/b.ts", secs=2),
    ])
    assert state.claim_by_id("01ABC") is None
    assert state.claim_by_id("01ABCD").id == "01ABCDEF"
    assert state.claim_by_id("01ABCDEF").id == "01ABCDEF"


def test_a_steal_folded_before_the_claim_it_displaces_does_not_resurrect_it():
    """IF THIS FAILS: two agents hold the same file, the board shows both, and
    the pre-edit hook lets both edit.

    A steal and the claim it displaces are two events, and fold sorts by
    timestamp — so a clock step between two machines (or two processes) can put
    the steal FIRST. It then pops nothing, because the claim it is displacing
    has not folded yet. The record that should have suppressed that claim on
    arrival must therefore survive, which is why displaced ids are remembered
    rather than discarded.
    """
    from datetime import datetime, timedelta, timezone

    from comms_graph import log as clog
    from comms_graph import state as cstate

    t0 = datetime(2026, 5, 22, 14, 30, tzinfo=timezone.utc)

    def ev(secs, actor, data, eid):
        ts = t0 + timedelta(seconds=secs)
        return clog.Event(ts=ts, id=eid, actor=actor, type="claim",
                          scope=["src/a.py"], data=data)

    alice = ev(10, "alice", {"intent": "mine"}, "CLAIM-ALICE")
    # The steal carries an EARLIER timestamp than the claim it displaces.
    steal = ev(5, "bob", {"intent": "taking it", "steals": "CLAIM-ALICE"}, "CLAIM-BOB")

    st = cstate.fold([alice, steal])
    holders = sorted(c.actor for c in st.claims.values())
    assert holders == ["bob"], f"one file, one holder — got {holders}"


def test_a_steal_in_the_ordinary_order_still_displaces():
    from datetime import datetime, timedelta, timezone

    from comms_graph import log as clog
    from comms_graph import state as cstate

    t0 = datetime(2026, 5, 22, 14, 30, tzinfo=timezone.utc)

    def ev(secs, actor, data, eid):
        ts = t0 + timedelta(seconds=secs)
        return clog.Event(ts=ts, id=eid, actor=actor, type="claim",
                          scope=["src/a.py"], data=data)

    st = cstate.fold([
        ev(10, "alice", {"intent": "mine"}, "CLAIM-ALICE"),
        ev(20, "bob", {"intent": "taking it", "steals": "CLAIM-ALICE"}, "CLAIM-BOB"),
    ])
    assert sorted(c.actor for c in st.claims.values()) == ["bob"]


def test_folding_many_named_session_ends_stays_linear():
    """IF THIS FAILS: the fold goes quadratic on a busy store, and it runs in
    front of every agent tool call.

    Measured before the fix: 8,000 named session ends over 8,000 claims took
    8 seconds. The cost was not the obvious loop — a profile put 3.2s of 4.7s in
    a single sorted() that ran on EVERY end over every actor ever seen, and was
    then discarded for named ends. Guessing at the hot spot fixed nothing twice;
    the profiler found it in one pass.
    """
    import time
    from datetime import datetime, timedelta, timezone

    from comms_graph import log as clog
    from comms_graph import state as cstate

    t0 = datetime(2026, 5, 22, 14, 30, tzinfo=timezone.utc)

    def ev(i, typ, actor, data, scope=None, eid=None):
        ts = t0 + timedelta(seconds=i)
        return clog.Event(ts=ts, id=eid or clog.new_id(ts), actor=actor,
                          type=typ, scope=scope, data=data)

    n = 3000
    evs = [ev(i, "hello", "a%d" % i, {"comms_session_id": "S%05d" % i})
           for i in range(n)]
    evs += [ev(n + i, "claim", "a%d" % i,
               {"intent": "x", "comms_session_id": "S%05d" % i},
               scope=["f%d.py" % i], eid="C%05d" % i) for i in range(n)]
    evs += [ev(2 * n + i, "release", "a%d" % i,
               {"refs": [], "comms_session_end": True,
                "comms_session_id": "S%05d" % i}) for i in range(n)]

    started = time.monotonic()
    st = cstate.fold(evs)
    elapsed = time.monotonic() - started

    # Correctness first: every named session really did close.
    assert st.claims == {}
    assert st.sessions == {}
    # Then the budget. Generous — this is about catching a return to quadratic,
    # not about pinning a number to one machine. Quadratic put 3,000 at ~0.7s
    # and 8,000 at 8s; linear puts 3,000 well under a tenth of that.
    assert elapsed < 2.0, f"folding {n} session ends took {elapsed:.2f}s"
