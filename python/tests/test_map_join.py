"""Joining a claim to the map, and turning that into advice.

The rule both modules are built around: a miss must be LOUD. A name that
resolves to nothing looks exactly like a piece of work with no neighbours —
quiet, and apparently fine — and a quarter of names typed from memory name
something that does not exist. Every test here is ultimately about not printing
an all-clear over a question that was never answered.

The advice itself is deliberately weak, and that is also tested: the map misses
between a third and a half of the file pairs that really change together, so
nothing here may read as a verdict.
"""

from __future__ import annotations

import networkx as nx
import pytest

from comms_graph.contact import MAX_SCOPES, contact, render
from comms_graph.resolve import resolve
from comms_graph.scope import parse as parse_scope

SERVER = "src/api/server.ts"
BILLING = "src/api/billing.ts"


def node(g, nid, label, file, line, **extra):
    g.add_node(nid, label=label, source_file=file, source_location=f"L{line}" if line else None,
               **extra)


@pytest.fixture
def graph():
    """A small, realistic map: a caller, a callee, and a bystander."""
    g = nx.DiGraph()
    node(g, "f:server", "server.ts", SERVER, None, file_type="file")
    node(g, "s:charge", "charge()", SERVER, 20)
    node(g, "s:refund", "refund()", SERVER, 60)
    node(g, "s:helper", "helper()", SERVER, 120)
    node(g, "b:rate", "rate()", BILLING, 10)
    node(g, "b:invoice", "invoice()", BILLING, 40)
    g.add_edge("s:charge", "b:rate", relation="calls", confidence="EXTRACTED")
    g.add_edge("f:server", "s:charge", relation="contains")
    g.add_edge("f:server", "s:refund", relation="contains")
    return g


# ---------------------------------------------------------------------------
# Resolving a claim onto the map
# ---------------------------------------------------------------------------


def test_a_symbol_that_exists_resolves_to_exactly_that_symbol(graph):
    """IF THIS FAILS: symbol-level claims cannot be placed at all, so every piece
    of advice about them is either absent or about the wrong code."""
    res = resolve(graph, parse_scope(f"{SERVER}#charge"))
    assert res.miss_reason is None
    assert [p.node_id for p in res.places] == ["s:charge"]
    assert res.places[0].via == "symbol"


def test_a_function_claimed_the_way_people_type_it_still_resolves(graph):
    """IF THIS FAILS: every ordinary function claim misses, because the map
    labels functions `charge()` while a person writes `path#charge` — nobody
    types the brackets. And a miss reads as "not in the map", which is the exact
    false all-clear this module exists to prevent."""
    assert [p.node_id for p in resolve(graph, parse_scope(f"{SERVER}#charge")).places] == [
        "s:charge"
    ]


def test_a_typo_comes_back_as_a_reason_and_a_suggestion(graph):
    """IF THIS FAILS: a mistyped symbol produces an empty result that the caller
    cannot tell apart from "checked, nothing near it" — so the agent is told it
    is clear to work on a name that does not exist."""
    res = resolve(graph, parse_scope(f"{SERVER}#chrage"))
    assert not res.places
    assert res.miss_reason and "chrage" in res.miss_reason
    assert "charge()" in res.miss_reason  # it says what IS claimable here


def test_a_case_only_miss_says_so_in_those_words(graph):
    """IF THIS FAILS: the user stares at a name that looks identical to the one
    they typed and concludes the tool is broken. Case matters here because the
    blocking layer treats `Charge` and `charge` as different symbols, so the two
    layers must agree — but a human needs telling why."""
    res = resolve(graph, parse_scope(f"{SERVER}#Charge"))
    assert not res.places
    assert "case" in res.miss_reason.lower()


def test_a_path_the_map_never_saw_is_distinguished_from_a_typo(graph, tmp_path):
    """IF THIS FAILS: "you spelled the filename wrong" and "this file exists but
    was never indexed" produce the same message, and the two need opposite
    actions — fix the name, versus rebuild the map. Conflating them is how a typo
    gets mistaken for an isolated file."""
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "new.ts").write_text("export const x = 1\n")

    real_but_unindexed = resolve(graph, parse_scope("src/api/new.ts"), root=tmp_path)
    typo = resolve(graph, parse_scope("src/api/sevrer.ts"), root=tmp_path)

    assert "not in the map" in real_but_unindexed.miss_reason
    assert "spelling" in typo.miss_reason


def test_no_map_at_all_is_a_reason_not_a_silence(graph):
    """IF THIS FAILS: a project that has never been extracted reports "nothing
    near your work" for every claim — an all-clear derived from no data at all."""
    res = resolve(None, parse_scope(f"{SERVER}#charge"))
    assert not res.places
    assert "extract" in res.miss_reason


def test_an_empty_resolution_always_carries_its_reason(graph, tmp_path):
    """IF THIS FAILS: some path through this module returns an empty list with no
    explanation, and every caller downstream is free to read it as "clear". The
    invariant is what lets callers trust `places == []` never to mean silence."""
    for scope in (f"{SERVER}#nope", "src/nope.ts", f"{SERVER}#L1-5", "src/nope.ts#L1-2"):
        res = resolve(graph, parse_scope(scope), root=tmp_path)
        assert not res.places and res.miss_reason, scope
        assert not res  # the __bool__ contract callers actually use


def test_a_line_claim_lands_on_the_symbol_that_spans_those_lines(graph):
    """IF THIS FAILS: line-range claims resolve to nothing (so they are advice
    about nothing) or to everything in the file (so the advice is noise). The map
    only records where a symbol STARTS, so a symbol's span has to be inferred
    from where the next one begins."""
    inside = resolve(graph, parse_scope(f"{SERVER}#L25-30"))
    assert [p.node_id for p in inside.places] == ["s:charge"]

    across = resolve(graph, parse_scope(f"{SERVER}#L55-65"))
    assert {p.node_id for p in across.places} == {"s:charge", "s:refund"}

    assert resolve(graph, parse_scope(f"{SERVER}#L1-5")).places == []


def test_a_node_with_no_line_does_not_answer_a_line_claim(graph):
    """IF THIS FAILS: file-level and prose nodes (which have no location) are
    treated as spanning their whole file, so every line claim matches everything
    in the file and the answer stops meaning anything."""
    res = resolve(graph, parse_scope(f"{SERVER}#L25-30"))
    assert "f:server" not in {p.node_id for p in res.places}


def test_a_whole_file_claim_covers_everything_recorded_in_that_file(graph):
    """IF THIS FAILS: the commonest claim of all — a file — resolves to less than
    the file, so contact advice about it is incomplete without saying so."""
    res = resolve(graph, parse_scope(SERVER))
    assert {p.node_id for p in res.places} == {"f:server", "s:charge", "s:refund", "s:helper"}
    assert all(p.via == "file" for p in res.places)


def test_the_path_a_person_typed_matches_the_path_that_was_indexed(graph):
    """IF THIS FAILS: every claim misses on projects whose scan recorded a
    leading `./`, and the miss reads as "your file is not in the map" — a whole
    project quietly told that none of its files exist."""
    g = nx.DiGraph()
    node(g, "s:charge", "charge()", f"./{SERVER}", 20)
    assert resolve(g, parse_scope(SERVER)).places


# ---------------------------------------------------------------------------
# Contact advice
# ---------------------------------------------------------------------------


def held(graph, actor, scope):
    return (actor, scope, resolve(graph, parse_scope(scope)))


def test_the_same_symbol_claimed_twice_is_reported_as_the_same_ground(graph):
    """IF THIS FAILS: the strongest signal there is — two people naming the one
    symbol — is buried among the weak "nearby" hints, or missed entirely."""
    mine = resolve(graph, parse_scope(f"{SERVER}#charge"))
    report = contact(graph, mine, [held(graph, "bob", f"{SERVER}#charge")])
    assert [(t.other_actor, t.kind) for t in report.touches] == [("bob", "same")]


def test_work_next_door_is_flagged_with_the_connection_that_links_it(graph):
    """IF THIS FAILS: the one thing this feature actually pays back — "the code
    you named leans on code somebody else named" — is either silent or unreadable
    because it cannot say WHY it fired. A `calls` edge read straight out of the
    source deserves more attention than a guessed one, so the relation and its
    confidence have to survive."""
    mine = resolve(graph, parse_scope(f"{SERVER}#charge"))
    report = contact(graph, mine, [held(graph, "bob", f"{BILLING}#rate")])
    (touch,) = report.touches
    assert (touch.other_actor, touch.kind, touch.relation) == ("bob", "near", "calls")
    assert touch.confidence == "EXTRACTED"


def test_whoever_holds_the_callee_is_warned_too(graph):
    """IF THIS FAILS: which of two agents gets the warning depends on which of
    them happened to run the check — and it is the person holding the CALLEE
    (whose change breaks the caller) who is left uninformed. Direction was tested
    and failed to predict anything; nearness is symmetric or it is nothing."""
    from_callee = contact(
        graph,
        resolve(graph, parse_scope(f"{BILLING}#rate")),
        [held(graph, "alice", f"{SERVER}#charge")],
    )
    assert [t.other_actor for t in from_callee.touches] == ["alice"]


def test_an_unrelated_claim_produces_no_touch(graph):
    """IF THIS FAILS: everything is "near" everything, the warning fires on every
    claim, and people stop reading it — which costs the real warnings too."""
    mine = resolve(graph, parse_scope(f"{SERVER}#refund"))
    report = contact(graph, mine, [held(graph, "bob", f"{BILLING}#invoice")])
    assert report.touches == []
    assert report.note  # never silent: "nothing found" is itself an answer


def test_a_structural_edge_does_not_hide_a_real_one(graph):
    """IF THIS FAILS: on a multigraph, a parallel `contains` edge (structural,
    meaningless for contact) masks a `calls` edge between the same two nodes, and
    the pair is reported as no contact at all — a false all-clear produced by the
    map having MORE information, not less."""
    g = nx.MultiDiGraph()
    node(g, "s:charge", "charge()", SERVER, 20)
    node(g, "b:rate", "rate()", BILLING, 10)
    g.add_edge("s:charge", "b:rate", relation="contains")
    g.add_edge("s:charge", "b:rate", relation="calls", confidence="EXTRACTED")

    report = contact(
        g,
        resolve(g, parse_scope(f"{SERVER}#charge")),
        [("bob", f"{BILLING}#rate", resolve(g, parse_scope(f"{BILLING}#rate")))],
    )
    assert [t.relation for t in report.touches] == ["calls"]


def test_containment_alone_does_not_make_two_symbols_neighbours(graph):
    """IF THIS FAILS: every symbol in a file is "near" every other symbol in that
    file, because the file contains them both. That is precisely the mush that
    makes the advice worthless — and it would fire on the most ordinary pair of
    claims there is, two functions in one file."""
    mine = resolve(graph, parse_scope(f"{SERVER}#charge"))
    report = contact(graph, mine, [held(graph, "bob", f"{SERVER}#helper")])
    assert report.touches == []


def test_somebody_elses_unplaceable_claim_is_reported_not_dropped(graph):
    """IF THIS FAILS: the all-clear prints over live work. The commonest reason
    another agent's claim cannot be placed is that the file was created since the
    last extract — which is exactly what a parallel agent has just done, and
    exactly when a collision is most likely."""
    others = [held(graph, "bob", "src/api/brand-new.ts")]
    report = contact(graph, resolve(graph, parse_scope(f"{SERVER}#charge")), others)
    assert any("brand-new.ts" in u and "bob" in u for u in report.unplaced)
    assert "brand-new.ts" in render(report)


def test_a_claim_we_could_not_place_never_reads_as_clear(graph):
    """IF THIS FAILS: your own unplaceable claim comes back as "nobody else is on
    this", which is a statement the tool has no basis for making."""
    mine = resolve(graph, parse_scope("src/typo.ts"))
    report = contact(graph, mine, [held(graph, "bob", f"{SERVER}#charge")])
    assert not report.touches
    assert "could not place" in report.note
    assert report.note in render(report)


def test_no_map_means_only_exact_overlaps_are_known(graph):
    """IF THIS FAILS: a project with no map reports the same reassuring silence
    as a project with a complete one."""
    report = contact(None, resolve(graph, parse_scope(f"{SERVER}#charge")), [])
    assert report.note and "extract" in report.note


def test_an_ordinary_file_claim_is_not_treated_as_too_broad(graph):
    """IF THIS FAILS: the feature bails out on nearly every real invocation. A
    whole-file claim is the natural unit and resolves to one node per symbol, so
    counting resolved NODES against the cap made an ordinary file claim exceed it
    — and the advice was "narrow it to a file, a symbol, or a line range" on a
    claim that already was a file."""
    mine = resolve(graph, parse_scope(SERVER))
    assert len(mine.places) > MAX_SCOPES
    report = contact(graph, mine, [held(graph, "bob", f"{BILLING}#rate")], scope_count=1)
    assert report.touches, report.note


def test_a_job_that_names_too_many_places_says_so_instead_of_guessing(graph):
    """IF THIS FAILS: a job that claims half the repository produces a wall of
    "nearby" lines that nobody can act on, which is the same as saying nothing —
    but without admitting it."""
    mine = resolve(graph, parse_scope(SERVER))
    report = contact(graph, mine, [held(graph, "bob", f"{BILLING}#rate")],
                     scope_count=MAX_SCOPES + 1)
    assert not report.touches
    assert str(MAX_SCOPES) in report.note


def test_nothing_found_is_never_rendered_as_a_guarantee(graph):
    """IF THIS FAILS: the output promises independence it cannot deliver. Between
    a third and a half of file pairs that really do change together are invisible
    to the map, so "no warning" must never be printed as "safe" — people act on
    that wording, and once it is wrong twice they stop reading any of it."""
    mine = resolve(graph, parse_scope(f"{SERVER}#refund"))
    report = contact(graph, mine, [held(graph, "bob", f"{BILLING}#invoice")])
    text = render(report).lower()
    assert text.strip()
    assert "not a guarantee" in text or "misses" in text


def test_a_nearby_hint_is_never_worded_as_a_conflict(graph):
    """IF THIS FAILS: advisory output is mistaken for a blocking decision. The
    ordering experiment behind that idea was measured at about coin-flip accuracy
    and was cut for exactly this reason; wording it as a conflict brings the
    discredited feature back through the output."""
    mine = resolve(graph, parse_scope(f"{SERVER}#charge"))
    report = contact(graph, mine, [held(graph, "bob", f"{BILLING}#rate")])
    text = render(report)
    assert "NEARBY" in text
    assert "worth a look" in text
    assert "CONFLICT" not in text.upper().replace("NOT A CONFLICT", "")


def test_the_strong_case_is_printed_before_the_weak_one(graph):
    """IF THIS FAILS: the one line worth acting on — somebody else holds this
    exact symbol — is buried under advisory neighbours and gets skimmed past."""
    mine = resolve(graph, parse_scope(f"{SERVER}#charge"))
    report = contact(
        graph, mine,
        [held(graph, "bob", f"{BILLING}#rate"), held(graph, "carol", f"{SERVER}#charge")],
    )
    assert [t.kind for t in report.touches] == ["same", "near"]
    text = render(report)
    assert text.index("SAME GROUND") < text.index("NEARBY")
