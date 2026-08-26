"""Unit tests for the coordination core: scope arithmetic, the state fold, and
scope-to-map resolution.

Every test here is about a way coordination can fail SILENTLY. A crash in
comms is cheap: somebody sees a traceback and fixes it. The expensive failures
are the quiet ones: two agents both believing they hold the same symbol, a
phantom claim nobody can release, a typo that reads as "nothing to worry
about". Each test below names the quiet failure it is standing in front of.

Tests that merely restate the implementation are deliberately absent: they make
a rewrite look like a regression, which is worse than no test at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import networkx as nx
import pytest

from comms_graph import resolve as resolve_mod
from comms_graph import state as state_mod
from comms_graph.scope import AnchorKind, Scope, ScopeError, overlaps, parse

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

T0 = "2026-01-01T00:00:00Z"


def ev(ts=T0, id="e1", actor="agent-a", type="note", scope=None, data=None):
    """One raw log line, as fold() sees it coming off the JSONL."""
    out = {"ts": ts, "id": id, "actor": actor, "type": type}
    if scope is not None:
        out["scope"] = scope
    if data is not None:
        out["data"] = data
    return out


def claim_ev(id, actor, scope_str, ts=T0, **data):
    return ev(ts=ts, id=id, actor=actor, type="claim", scope=[scope_str], data=data or {})


def graph_with(*nodes):
    """A tiny map. Each node is (node_id, source_file, source_location, label)."""
    g = nx.DiGraph()
    for nid, src, loc, label in nodes:
        attrs = {"source_file": src, "label": label}
        if loc is not None:
            attrs["source_location"] = loc
        g.add_node(nid, **attrs)
    return g


# ===========================================================================
# scope: the round trip
# ===========================================================================


@pytest.mark.parametrize(
    "raw",
    [
        "src/api/server.ts#handleRequest",           # ordinary
        "src/api/server.ts#L10-40",                  # line range
        "src/api/server.ts",                         # whole file
        "src/**/*.ts#parse",                         # globbed path + symbol
        r"src/we\#ird.ts#Handler",                   # literal '#' in the filename
        r"src/dir\\#Handler",                        # path whose last byte is a backslash
        r"src/dir\\#L1-2",                           # ...same, with a line anchor
        "src/api/server.ts#L1-1 ",                   # trailing space on the anchor
        "./src/api/server.ts#handleRequest",         # non-canonical spelling
    ],
)
def test_scope_round_trips_through_str(raw):
    """WHAT BREAKS: str(scope) is what gets written into the append-only log and
    echoed in conflict messages, and parse() is what reads it back on every
    subsequent claim check. If the pair is not invertible, a symbol claim can
    come back as a WHOLE-file claim (silently widening one agent's territory),
    a line claim can come back as a symbol claim (so it never intersects the
    range it was meant to protect), or it can come back as a ScopeError that
    state.py swallows: dropping a claim whose event lives in the log forever.
    All three end with two agents editing the same code believing they are
    alone."""
    once = parse(raw)
    twice = parse(str(once))
    # Scope equality ignores `raw`, so this compares the territory itself.
    assert twice == once
    assert twice.anchor.kind is once.anchor.kind
    assert twice.path == once.path
    assert twice.anchor.symbol == once.anchor.symbol
    assert (twice.anchor.line_start, twice.anchor.line_end) == (
        once.anchor.line_start,
        once.anchor.line_end,
    )
    # And it is a fixed point from the second pass on: the log is replayed
    # many times, not once, so a form that drifts on each rewrite is not stable.
    assert str(twice) == str(once)


def test_anchored_claim_never_degrades_to_whole_file():
    """WHAT BREAKS: this is the specific direction of round-trip failure that is
    unsafe. A symbol claim that re-parses as a whole-file claim silently grows
    to cover the file; worse, in the escaping bug this guards, the file it grew
    to cover ("src/dir#Handler") does not exist, so it conflicts with nothing
    and both agents hold the real symbol."""
    for raw in (r"src/dir\\#Handler", r"src/we\#ird.ts#Handler", "a/b.ts#L2-3"):
        assert parse(str(parse(raw))).anchor.kind is not AnchorKind.WHOLE


def test_whitespace_padded_line_anchor_is_not_a_symbol():
    """WHAT BREAKS: if " L1-1" parsed as a SYMBOL named "L1-1", its canonical
    form would be "f#L1-1", which parses back as a LINE range. One claim would
    mean two different things depending on how many times it had been through
    the log, and a symbol claim replayed as a line claim protects the wrong
    territory."""
    padded = parse("src/a.ts#L1-1 ")
    assert padded.anchor.kind is AnchorKind.LINE
    assert padded == parse("src/a.ts#L1-1")


def test_symbol_names_that_merely_look_like_ranges_stay_symbols():
    """WHAT BREAKS: real identifiers start with L and contain hyphens
    ("List-impl", "L-value"). Reading them as line ranges would silently
    relocate the claim onto lines nobody meant, and a claim on the wrong lines
    is a claim that fails to protect the thing it named."""
    for name in ("List-impl", "L-value", "Loader-2", "Lx-1"):
        assert parse(f"src/a.ts#{name}").anchor.kind is AnchorKind.SYMBOL


@pytest.mark.parametrize(
    "raw",
    ["/etc/passwd", "../outside.ts", "src/../../outside.ts", "", "src/a.ts#", "src/a.ts#L0-3",
     "src/a.ts#L9-2"],
)
def test_scope_rejects_what_it_cannot_represent(raw):
    """WHAT BREAKS: these must raise at the CLI boundary where a human sees the
    message. Accepting them writes a claim into the permanent log that either
    names territory outside the repo or has no coherent meaning, and once it is
    in an append-only file, nothing can take it back out."""
    with pytest.raises(ScopeError):
        parse(raw)


# ===========================================================================
# scope: overlap is an equivalence-shaped relation
# ===========================================================================

_SCOPE_MATRIX = [
    "src/api/server.ts",
    "src/api/server.ts#handleRequest",
    "src/api/server.ts#parse",
    "src/api/server.ts#L10-40",
    "src/api/server.ts#L41-80",
    "src/api/*.ts",
    "src/**",
    "src/**/*.ts",
    "src/lib/banking",
    "src/lib/banking/webhook.ts",
    "tests/test_api.py",
]


@pytest.mark.parametrize("raw", _SCOPE_MATRIX)
def test_overlap_is_reflexive(raw):
    """WHAT BREAKS: a scope that does not overlap itself is a scope two agents
    can both claim. Reflexivity is the floor the whole exclusion property
    stands on: every other overlap rule is a refinement of "the same claim
    twice is a conflict"."""
    s = parse(raw)
    assert overlaps(s, s)


@pytest.mark.parametrize("a_raw", _SCOPE_MATRIX)
@pytest.mark.parametrize("b_raw", _SCOPE_MATRIX)
def test_overlap_is_symmetric(a_raw, b_raw):
    """WHAT BREAKS: whether a collision is reported would depend on WHICH agent
    asked. Agent A checks its scope against B's held claim and is told "clear";
    B, checking the same pair the other way round, would have been told
    "blocked". Asymmetry means the tool's answer is a function of arrival
    order, and both agents can be cleared onto the same code."""
    a, b = parse(a_raw), parse(b_raw)
    assert overlaps(a, b) == overlaps(b, a)


def test_glob_claim_conflicts_with_a_concrete_file_underneath_it():
    """WHAT BREAKS: an agent claiming a subtree ("I am refactoring src/**")
    while another claims one file inside it is the single most common real
    collision. If the glob does not intersect the file, the subtree claim
    protects nothing at all."""
    subtree = parse("src/**")
    one_file = parse("src/api/server.ts")
    assert overlaps(subtree, one_file)
    assert overlaps(parse("src/api/*.ts"), one_file)
    assert overlaps(parse("src/**/*.ts"), one_file)
    # ...and it must still stop somewhere, or every claim conflicts with every
    # other and agents learn to ignore the warning.
    assert not overlaps(subtree, parse("tests/test_api.py"))


def test_a_directory_claim_covers_what_is_under_it():
    """WHAT BREAKS: "src/lib/banking" is how a person spells "that directory".
    If it only matched a FILE by that exact name, a claim on the directory and a
    claim on src/lib/banking/webhook.ts would not conflict, and two agents would
    both believe they own the webhook."""
    assert overlaps(parse("src/lib/banking"), parse("src/lib/banking/webhook.ts"))


def test_shallow_and_recursive_globs_stay_distinguishable():
    """WHAT BREAKS: if `src/*` were promoted to a subtree the way a literal
    segment is, `src/*` and `src/**` would become synonyms and there would be NO
    spelling left for "just the files directly in src". A user who wants the
    narrow claim would be forced into a wide one, which blocks work that was
    never in contention. The cost is a known missed conflict; write `src/**`
    when you mean the subtree."""
    assert overlaps(parse("src/**"), parse("src/foo/bar.ts"))
    assert not overlaps(parse("src/*"), parse("src/foo/bar.ts"))
    assert overlaps(parse("src/*"), parse("src/bar.ts"))


# ===========================================================================
# scope: anchors
# ===========================================================================


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("f.ts#L10-20", "f.ts#L20-30", True),    # touch at one shared line
        ("f.ts#L10-20", "f.ts#L21-30", False),   # adjacent, disjoint
        ("f.ts#L10-20", "f.ts#L10-20", True),    # identical
        ("f.ts#L10-40", "f.ts#L20-25", True),    # containment
        ("f.ts#L1-1", "f.ts#L1-1", True),        # single shared line
        ("f.ts#L1-1", "f.ts#L2-2", False),       # single lines, adjacent
    ],
)
def test_line_ranges_are_inclusive_intervals(a, b, expected):
    """WHAT BREAKS, in both directions. Treat the ranges as half-open and
    L10-20 / L20-30 come back clear: two agents both edit line 20, and the
    second write silently loses the first. Be too generous instead and L10-20
    conflicts with L21-30, so every claim in a file blocks every other one;
    agents wait for nothing, stop trusting the tool, and turn it off."""
    assert overlaps(parse(a), parse(b)) is expected


def test_line_ranges_in_different_files_never_conflict():
    """WHAT BREAKS: anchors refine a path match, they do not create one. If
    identical line numbers in unrelated files conflicted, every agent working
    near the top of any file would block every other."""
    assert not overlaps(parse("a.ts#L1-10"), parse("b.ts#L1-10"))


def test_a_whole_file_claim_swallows_every_anchor_in_that_file():
    """WHAT BREAKS: claiming a file is how an agent says "I am rewriting this".
    If an existing symbol or line claim inside it did not conflict, the rewrite
    would proceed straight over somebody else's in-flight edit."""
    whole = parse("src/a.ts")
    for anchored in ("src/a.ts#parse", "src/a.ts#L1-2", "src/a.ts#L900-901"):
        assert overlaps(whole, parse(anchored))


def test_a_symbol_and_a_line_range_conflict_pessimistically():
    """WHAT BREAKS: this module has no symbol table: it runs BEFORE the map
    exists, which is the whole point of a claim, so it cannot know whether
    #parse lives inside #L10-40. The only safe answer is "maybe, so treat it as
    a conflict". Answering "no" here trades a rare unnecessary wait for a
    silent concurrent edit, which is the wrong direction to fail in."""
    assert overlaps(parse("src/a.ts#parse"), parse("src/a.ts#L10-40"))


def test_distinct_symbols_in_one_file_do_not_conflict():
    """WHAT BREAKS: symbol-level claims exist so two agents CAN work in one
    file. If distinct symbols conflicted, the anchor would buy nothing over
    claiming the whole file and nobody would use it."""
    assert not overlaps(parse("src/a.ts#parse"), parse("src/a.ts#render"))
    assert overlaps(parse("src/a.ts#parse"), parse("src/a.ts#parse"))


# ===========================================================================
# state.fold: totality
# ===========================================================================

_MALFORMED = [
    pytest.param(None, id="none"),
    pytest.param(42, id="int"),
    pytest.param("not-an-event", id="bare-string"),
    pytest.param([], id="list"),
    pytest.param({}, id="empty-dict"),
    pytest.param(ev(ts=None), id="ts-none"),
    pytest.param(ev(ts=""), id="ts-empty"),
    pytest.param(ev(ts="not a timestamp"), id="ts-garbage"),
    pytest.param(ev(ts="0001-01-01T00:00:00Z"), id="ts-zero"),
    pytest.param(ev(ts="9999-12-31T23:59:59-05:00"), id="ts-overflows-forward"),
    pytest.param(ev(ts="0001-01-01T00:00:00+05:00"), id="ts-overflows-backward"),
    pytest.param(ev(id=""), id="id-empty"),
    pytest.param(ev(id=None), id="id-none"),
    pytest.param(ev(actor=""), id="actor-empty"),
    pytest.param(ev(actor=123), id="actor-not-a-string"),
    pytest.param(ev(type="wormhole"), id="type-unknown"),
    pytest.param(ev(type=None), id="type-none"),
    pytest.param(ev(type="claim", scope="src/a.ts"), id="scope-bare-string"),
    pytest.param(ev(type="claim", scope=[123]), id="scope-non-string-element"),
    pytest.param(ev(type="claim", scope=[]), id="scope-empty-list"),
    pytest.param(ev(type="claim", scope={"a": 1}), id="scope-dict"),
    pytest.param(ev(data=["not", "a", "map"]), id="data-list"),
    pytest.param(ev(data="string"), id="data-string"),
    pytest.param(ev(type="claim", scope=["/etc/passwd"]), id="scope-absolute"),
    pytest.param(ev(type="claim", scope=["../../outside.ts"]), id="scope-escapes-root"),
    pytest.param(ev(type="claim", scope=["src/a.ts#L5-1"]), id="scope-inverted-range"),
    pytest.param(ev(type="release", data={"refs": 7}), id="refs-not-a-list"),
    pytest.param(ev(type="release", data={"refs": [1, None]}), id="refs-non-string"),
    pytest.param(ev(type="finding", data={"refs": ["flat", 3]}), id="finding-refs-flat"),
    pytest.param(ev(type="hello", data={"tier": float("nan")}), id="tier-nan"),
    pytest.param(ev(type="hello", data={"tier": float("inf")}), id="tier-inf"),
    pytest.param(ev(type="hello", data={"leader": "yes"}), id="bool-as-string"),
]


@pytest.mark.parametrize("bad", _MALFORMED)
def test_fold_is_total_on_malformed_events(bad):
    """WHAT BREAKS: fold() runs on the pre-edit hot path in front of EVERY agent
    write. If it raises, one corrupt line in an append-only file that is never
    rewritten permanently disables every agent's ability to edit anything:
    there is no "delete the bad line" recovery, because the log is truth. Total
    means total: drop what cannot be placed, never throw."""
    st = state_mod.fold([bad])
    assert isinstance(st, state_mod.State)


def test_fold_is_total_on_a_self_referential_event():
    """WHAT BREAKS: decoded JSON is arbitrary caller data, and a reducer that
    walks nested values recursively can be sent into unbounded recursion by a
    cycle. RecursionError on the pre-edit path is the same outage as any other
    raise."""
    looped = ev(type="note")
    looped["data"] = looped
    state_mod.fold([looped])


def test_fold_survives_a_corrupt_line_in_the_middle_of_a_good_log():
    """WHAT BREAKS: corruption is not an all-or-nothing event: one bad line
    lands in a log full of good ones. A reducer that gave up at the first bad
    line would throw away every claim written after it, freeing scopes that are
    still actively held."""
    events = [
        claim_ev("c1", "agent-a", "src/a.ts", ts="2026-01-01T00:00:00Z"),
        "total garbage",
        {"ts": "nope", "id": "c2", "actor": "agent-b", "type": "claim", "scope": ["src/b.ts"]},
        claim_ev("c3", "agent-b", "src/b.ts", ts="2026-01-01T00:00:02Z"),
    ]
    st = state_mod.fold(events)
    assert set(st.claims) == {"c1", "c3"}


def test_fold_does_not_mutate_or_consume_its_input():
    """WHAT BREAKS: the same decoded event list is folded repeatedly (once per
    claim check, plus by the reporting commands). A reducer that reordered or
    consumed it would make the second fold disagree with the first, so the
    answer to "is this scope free?" would change without the log changing."""
    events = [
        claim_ev("c2", "agent-b", "src/b.ts", ts="2026-01-01T00:00:02Z"),
        claim_ev("c1", "agent-a", "src/a.ts", ts="2026-01-01T00:00:00Z"),
    ]
    snapshot = [dict(e) for e in events]
    first = state_mod.fold(events)
    second = state_mod.fold(events)
    assert events == snapshot
    assert set(first.claims) == set(second.claims) == {"c1", "c2"}


def test_fold_orders_by_timestamp_not_arrival():
    """WHAT BREAKS: a release must be able to close a claim regardless of the
    order the lines are handed to fold(). If arrival order won, a release read
    before its claim would leave the claim active forever: a scope nobody can
    free."""
    release = ev(ts="2026-01-01T00:00:05Z", id="r1", actor="agent-a", type="release",
                 data={"refs": ["c1"], "result": "done"})
    claim = claim_ev("c1", "agent-a", "src/a.ts", ts="2026-01-01T00:00:00Z")
    assert state_mod.fold([release, claim]).claims == {}
    assert state_mod.fold([claim, release]).claims == {}


# ===========================================================================
# state.fold: a corrupt event must never MATERIALISE a claim
# ===========================================================================

_CORRUPT_CLAIMS = [
    pytest.param(ev(type="claim", id="c1", scope=[123, "src/api/server.ts"]),
                 id="partly-non-string-scope"),
    pytest.param(ev(type="claim", id="c1", scope="src/api/server.ts"),
                 id="scope-is-a-bare-string"),
    pytest.param(ev(type="claim", id="c1", scope={"0": "src/api/server.ts"}),
                 id="scope-is-a-dict"),
    pytest.param(ev(type="claim", id="c1", scope=["/src/api/server.ts"]),
                 id="scope-is-absolute"),
    pytest.param(ev(type="claim", id="c1", scope=["../../../etc/passwd"]),
                 id="scope-escapes-the-repo"),
    pytest.param(ev(type="claim", id="c1", scope=["src/api/server.ts#L9-2"]),
                 id="scope-has-an-inverted-range"),
    pytest.param(ev(type="claim", id="c1", scope=["src/api/server.ts#"]),
                 id="scope-has-an-empty-anchor"),
    pytest.param(ev(type="claim", id="c1", scope=[]), id="scope-is-empty"),
    pytest.param(ev(type="claim", id="c1"), id="scope-is-absent"),
    pytest.param(ev(type="claim", id="c1", scope=["src/a.ts"], data=["oops"]),
                 id="data-is-not-a-map"),
    pytest.param(ev(type="claim", id="", scope=["src/a.ts"]), id="claim-has-no-id"),
    pytest.param(ev(type="claim", id="c1", actor="", scope=["src/a.ts"]),
                 id="claim-has-no-actor"),
]


@pytest.mark.parametrize("corrupt", _CORRUPT_CLAIMS)
def test_a_corrupt_event_never_materialises_an_active_claim(corrupt):
    """WHAT BREAKS: an active claim is a lock, and this one would be a PHANTOM:
    held by no writer, matching a scope nobody typed, and impossible to release
    because no agent knows it exists and no release event references its id. It
    blocks every other agent on that scope for the life of the log. Salvaging
    the readable half of a corrupt line is exactly how the phantom gets made:
    `{"scope":[123,"src/api/server.ts"]}` once folded into a live claim on
    src/api/server.ts. Dropping the whole line is the only safe reading."""
    st = state_mod.fold([corrupt])
    assert st.claims == {}
    assert st.conflicts_for(parse("src/api/server.ts")) == []


def test_a_dropped_event_does_not_refresh_an_actors_heartbeat():
    """WHAT BREAKS: last_seen is the passive heartbeat used to decide whether an
    agent is still alive, and therefore whether its claims may be stolen. A
    malformed line that folds "far enough" to bump last_seen hands a dead agent
    a fresh pulse: its stale claims then look live and can never be reclaimed,
    so the scope stays locked by a process that exited."""
    hello = ev(ts="2026-01-01T00:00:00Z", id="h1", actor="agent-a", type="hello")
    later_but_corrupt = ev(ts="2026-01-01T01:00:00Z", id="x1", actor="agent-a",
                           type="claim", scope="src/a.ts")  # bare string: not an event
    st = state_mod.fold([hello, later_but_corrupt])
    assert st.sessions["agent-a"].last_seen == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_a_good_claim_does_materialise():
    """The control for the tests above. WHAT BREAKS without it: a reducer that
    dropped EVERYTHING would pass every corruption test on this page while
    making the tool a no-op that reports no conflicts, ever."""
    st = state_mod.fold([claim_ev("c1", "agent-a", "src/api/server.ts#handleRequest")])
    assert list(st.claims) == ["c1"]
    assert st.claims["c1"].actor == "agent-a"
    assert st.claims["c1"].scope.anchor.symbol == "handleRequest"


# ===========================================================================
# State.conflicts_for
# ===========================================================================


def two_agents_on_one_file():
    return state_mod.fold([
        claim_ev("mine", "agent-a", "src/api/server.ts", ts="2026-01-01T00:00:00Z"),
        claim_ev("theirs", "agent-b", "src/api/server.ts", ts="2026-01-01T00:00:01Z"),
        claim_ev("elsewhere", "agent-c", "docs/README.md", ts="2026-01-01T00:00:02Z"),
    ])


def test_conflicts_for_excludes_your_own_claims():
    """WHAT BREAKS: an agent re-checks its scope constantly: before each edit,
    and again when narrowing or extending a claim. If its OWN claim came back as
    a conflict it would be permanently blocked by itself, and the only way out
    would be to release the claim it is actively relying on. Self-deadlock,
    100% reproducible, on the happy path."""
    st = two_agents_on_one_file()
    got = st.conflicts_for(parse("src/api/server.ts"), "agent-a")
    assert [c.id for c in got] == ["theirs"]


def test_conflicts_for_still_reports_everyone_elses():
    """WHAT BREAKS: the exclusion above must be exactly "mine", not "the first
    one" or "all of them". An off-by-one that dropped one other holder is a
    collision reported as clear."""
    st = two_agents_on_one_file()
    assert [c.id for c in st.conflicts_for(parse("src/api/server.ts"), "agent-b")] == ["mine"]
    assert {c.id for c in st.conflicts_for(parse("src/api/server.ts"))} == {"mine", "theirs"}


def test_conflicts_for_ignores_unrelated_territory():
    """WHAT BREAKS: if conflicts_for returned claims that do not overlap, every
    claim in the repo would block every other one and agents would stop reading
    the output."""
    st = two_agents_on_one_file()
    assert st.conflicts_for(parse("src/other.ts"), "agent-a") == []


def test_conflicts_for_reports_the_oldest_holder_first():
    """WHAT BREAKS: the first entry is what the CLI shows and what an
    arbitration prompt names as "the holder". Ordering by anything other than
    when the claim was taken means the message points at the wrong agent, and
    the person chasing the conflict messages someone who never held it."""
    st = two_agents_on_one_file()
    got = st.conflicts_for(parse("src/api/server.ts"))
    assert [c.id for c in got] == ["mine", "theirs"]


def test_a_released_claim_stops_conflicting():
    """WHAT BREAKS: this is the other half of exclusion. A claim that survives
    its own release locks the scope forever: the agent that took it has exited,
    and there is no second release to send."""
    st = state_mod.fold([
        claim_ev("c1", "agent-a", "src/api/server.ts", ts="2026-01-01T00:00:00Z"),
        ev(ts="2026-01-01T00:00:05Z", id="r1", actor="agent-a", type="release",
           data={"refs": ["c1"], "result": "done"}),
    ])
    assert st.conflicts_for(parse("src/api/server.ts"), "agent-b") == []


def test_a_steal_leaves_exactly_one_holder():
    """WHAT BREAKS: a steal takes the scope and frees the displaced claim in ONE
    event. If both survived, the scope would report two holders and neither
    agent could tell whose turn it is; if neither did, the stealer would be
    editing under no claim at all."""
    st = state_mod.fold([
        claim_ev("c1", "agent-a", "src/api/server.ts", ts="2026-01-01T00:00:00Z"),
        claim_ev("c2", "agent-b", "src/api/server.ts", ts="2026-01-01T00:00:01Z",
                 steals="c1", steal_reason="agent-a is gone", arbitrator="human"),
    ])
    assert list(st.claims) == ["c2"]
    assert [c.id for c in st.conflicts_for(parse("src/api/server.ts"), "agent-b")] == []


def test_active_claims_by_actor_returns_only_mine_oldest_first():
    """WHAT BREAKS: this is what "release everything I hold" iterates over. Too
    wide and one agent releases another's claims, freeing code that is being
    edited right now. Too narrow and it leaves its own claims behind on exit:
    the stale-lock failure again."""
    st = state_mod.fold([
        claim_ev("a2", "agent-a", "src/b.ts", ts="2026-01-01T00:00:02Z"),
        claim_ev("a1", "agent-a", "src/a.ts", ts="2026-01-01T00:00:00Z"),
        claim_ev("b1", "agent-b", "src/c.ts", ts="2026-01-01T00:00:01Z"),
    ])
    assert [c.id for c in st.active_claims_by_actor("agent-a")] == ["a1", "a2"]
    assert [c.id for c in st.active_claims_by_actor("agent-b")] == ["b1"]
    assert st.active_claims_by_actor("nobody") == []


def test_conflicts_for_uses_scope_overlap_not_string_equality():
    """WHAT BREAKS: claims are typed by different agents in different
    spellings: one claims the subtree, one claims the file, one claims a
    symbol inside it. If the lookup compared strings, only a character-for-
    character rematch would ever be a conflict, and the tool would report clear
    for every real collision it exists to catch."""
    st = state_mod.fold([
        claim_ev("sub", "agent-a", "src/**", ts="2026-01-01T00:00:00Z"),
    ])
    assert [c.id for c in st.conflicts_for(parse("src/api/server.ts#parse"), "agent-b")] == ["sub"]
    assert [c.id for c in st.conflicts_for(parse("./src/api/server.ts"), "agent-b")] == ["sub"]


# ===========================================================================
# resolve: a miss must be loud
# ===========================================================================


@pytest.fixture
def small_map():
    return graph_with(
        ("f:src/api/server.ts", "src/api/server.ts", None, "server.ts"),
        ("s:handleRequest", "src/api/server.ts", "L10", "handleRequest"),
        ("s:parseBody", "src/api/server.ts", "L40", "parseBody"),
        ("s:sendReply", "src/api/server.ts", "L80", "sendReply"),
    )


def test_a_typoed_symbol_gives_a_reason_not_an_empty_list(small_map):
    """WHAT BREAKS: an empty result is indistinguishable from "this code is
    isolated: nobody else is near it", which reads as reassurance. A name typed
    from memory that does not exist would then look like a clean bill of health
    on ground the tool never actually checked. The miss has to say so, and it
    has to say what the file DOES contain so the typo can be fixed on the
    spot."""
    res = resolve_mod.resolve(small_map, parse("src/api/server.ts#handleRequst"))
    assert res.places == []
    assert not res  # falsy, so `if resolution:` callers do not read it as a hit
    assert res.miss_reason
    assert "handleRequst" in res.miss_reason
    assert "handleRequest" in res.miss_reason  # the real name, offered back


def test_a_typoed_path_says_check_the_spelling(tmp_path, small_map):
    """WHAT BREAKS: "not in the map" and "no such file" call for opposite
    actions: rebuild the index vs fix your typo. Conflating them sends the
    agent to re-run extraction over and over against a filename that will never
    appear."""
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "server.ts").write_text("x", encoding="utf-8")
    typo = resolve_mod.resolve(small_map, parse("src/api/sever.ts"), root=tmp_path)
    assert typo.places == []
    assert "spelling" in typo.miss_reason

    # The same file, present on disk but absent from the map, must NOT be
    # reported as a typo: it is a stale or incomplete index.
    (tmp_path / "src" / "api" / "client.ts").write_text("x", encoding="utf-8")
    stale = resolve_mod.resolve(small_map, parse("src/api/client.ts"), root=tmp_path)
    assert stale.places == []
    assert "spelling" not in stale.miss_reason


def test_no_map_at_all_is_a_reason_not_a_silence():
    """WHAT BREAKS: with no index built, EVERY claim resolves to nothing. If
    that were silent, an agent on a fresh checkout would be told "no contact"
    for every scope in the repo and would trust it."""
    res = resolve_mod.resolve(None, parse("src/api/server.ts"))
    assert res.places == []
    assert res.miss_reason


@pytest.mark.parametrize(
    "scope_str",
    [
        "src/api/server.ts#nosuchsymbol",
        "src/api/nosuchfile.ts",
        "src/api/server.ts#L1-9",  # before the first indexed symbol
    ],
)
def test_every_empty_resolution_carries_a_reason(scope_str, small_map):
    """WHAT BREAKS: this is the invariant the two tests above are instances of:
    places==[] and miss_reason==None must be an unreachable combination. The
    moment one miss path forgets to set a reason, that path starts reading as
    "checked, nothing there", which is the failure this whole module exists to
    prevent."""
    res = resolve_mod.resolve(small_map, parse(scope_str))
    assert res.places == [] and res.miss_reason


def test_a_symbol_that_exists_resolves_to_just_that_symbol(small_map):
    """WHAT BREAKS: the control. If a real symbol also missed, every claim would
    be loud and the reason text would become noise people scroll past: at which
    point the genuine misses above are invisible again."""
    res = resolve_mod.resolve(small_map, parse("src/api/server.ts#parseBody"))
    assert res.miss_reason is None
    assert [p.node_id for p in res.places] == ["s:parseBody"]
    assert res.places[0].via == "symbol"


def test_a_line_claim_resolves_through_implied_spans(small_map):
    """WHAT BREAKS: the map records where a symbol STARTS and not where it ends,
    so a span runs to just before the next symbol. If a line claim only matched
    the exact start line, L41-79 (the whole body of parseBody) would resolve to
    nothing and report a miss on code that is plainly indexed, and a miss on
    real ground trains people to ignore misses."""
    inside = resolve_mod.resolve(small_map, parse("src/api/server.ts#L41-79"))
    assert [p.node_id for p in inside.places] == ["s:parseBody"]
    assert inside.places[0].via == "lines"

    spanning = resolve_mod.resolve(small_map, parse("src/api/server.ts#L39-41"))
    assert [p.node_id for p in spanning.places] == ["s:handleRequest", "s:parseBody"]

    # The LAST symbol's span runs to end-of-file, because nothing in the map
    # records where a file stops. WHAT BREAKS otherwise: a claim on lines below
    # the last recorded symbol would report "nothing indexed there" about code
    # that is plainly indexed: a false miss, which costs more than the
    # occasional over-wide span (the span is advisory and blocks nothing).
    past_the_end = resolve_mod.resolve(small_map, parse("src/api/server.ts#L5000-5001"))
    assert [p.node_id for p in past_the_end.places] == ["s:sendReply"]

    # Above the first one there is no such excuse: the region really is unindexed.
    before_the_start = resolve_mod.resolve(small_map, parse("src/api/server.ts#L1-9"))
    assert before_the_start.places == [] and before_the_start.miss_reason


def test_an_unlocated_node_does_not_answer_line_claims(small_map):
    """WHAT BREAKS: file-level and rationale nodes carry no source_location. If
    they were treated as spanning the file, every line-range claim in that file
    would match them, so every line claim would resolve to the same catch-all
    node and the anchor would tell you nothing about where the work actually
    is."""
    res = resolve_mod.resolve(small_map, parse("src/api/server.ts#L10-11"))
    assert "f:src/api/server.ts" not in [p.node_id for p in res.places]
    # ...but a WHOLE-file claim does reach it, which is what makes the
    # exclusion above a targeted rule rather than the node being unreachable.
    whole = resolve_mod.resolve(small_map, parse("src/api/server.ts"))
    assert "f:src/api/server.ts" in [p.node_id for p in whole.places]


def test_resolution_matches_paths_the_way_a_person_typed_them():
    """WHAT BREAKS: the map stores whatever path the scan walked, which may
    carry a leading "./"; a claim is written repo-relative. Comparing raw
    strings makes EVERY claim miss, which surfaces as "not in the map" for a
    fully indexed repo: the loud channel firing constantly, which is how it
    gets tuned out."""
    g = graph_with(("s:parse", "./src/api/server.ts", "L10", "parse"))
    res = resolve_mod.resolve(g, parse("src/api/server.ts#parse"))
    assert res.miss_reason is None
    assert [p.node_id for p in res.places] == ["s:parse"]


def test_resolve_reads_the_anchor_off_the_real_scope_type(small_map):
    """WHAT BREAKS: resolve() reaches into Scope for its anchor. An earlier
    draft looked for attributes this Scope does not have, so EVERY anchored
    claim fell through to the whole-file branch: a wrong answer with no error
    anywhere: a claim on one symbol reported contact with every symbol in the
    file. This pins that an anchored claim and a whole-file claim on the same
    path give different answers."""
    anchored = resolve_mod.resolve(small_map, parse("src/api/server.ts#parseBody"))
    whole = resolve_mod.resolve(small_map, parse("src/api/server.ts"))
    assert len(anchored.places) == 1
    assert len(whole.places) == 4
    assert {p.via for p in whole.places} == {"file"}


def test_scope_is_hashable_and_immutable():
    """WHAT BREAKS: a Scope is carried inside a Claim that was written to an
    append-only log. If it could be mutated after the fact, the in-memory view
    would drift from the line on disk, and a replay would disagree with the
    running process about who holds what."""
    s = parse("src/a.ts#parse")
    assert {s, parse("./src/a.ts#parse")} == {s}  # same territory, one entry
    with pytest.raises(Exception):
        s.path = "src/b.ts"  # type: ignore[misc]
    assert isinstance(s, Scope)


def test_an_over_long_note_says_where_it_would_cut_and_what_to_use(tmp_path, monkeypatch, capsys):
    """IF THIS FAILS: the refusal is true and useless, and people split the note.

    The old message said only "that note is 586 characters; keep it under 400".
    It did not say what to drop and did not say that the thing being written was
    not a note. Three agents reported it, and one did the predictable thing:
    split a handoff into two notes, which then read OUT OF ORDER in the feed,
    because the feed is ordered by time and the second half had been written
    first. Splitting is the option that looks like it works and does not, so it
    is ruled out explicitly.

    The cap itself is not the problem and is not moved. Measured on 469 real
    notes: median 171 characters, p90 194, longest 352. Nothing written normally
    has ever come near it.
    """
    from comms_graph import cli

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("COMMS_ACTOR", "alice")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["note", "x" * 500]) != 0
    err = capsys.readouterr().err
    assert "500 characters, 100 over" in err, "it should say how far over"
    assert "It would end here" in err, "it should show where it cuts"
    # Every route it offers must accept a long body. `doc --edit` hands control
    # to $EDITOR, which an agent cannot drive, so it must not be one of them.
    assert "task done" in err and ".comms/docs/" in err
    assert "--edit" not in err, "offered a route an agent cannot use"
    assert "Do not split" in err, "the failure mode it caused must be named"
