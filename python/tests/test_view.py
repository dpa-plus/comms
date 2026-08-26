"""Tests for graphify.comms.view.

Kept in its own directory rather than in ``tests/comms/`` because that tree is
being edited by other people right now.

The assertions that matter most here are the NEGATIVE ones: the picture must not
contain a word that reads as an order or a verdict. That is not style policing:
docs/COMMS.md records that order derived from this map was measured at coin-flip
accuracy and cut, and a picture is the most persuasive place to reintroduce it by
accident.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import networkx as nx
import pytest

from comms_graph import log as _log
from comms_graph import view as _view


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _graph_json(tmp_path: Path) -> Path:
    """A tiny map with two files, real source_locations and one call edge."""
    G = nx.DiGraph()
    G.add_node("a_alpha", label="alpha()", source_file="pkg/a.py",
               source_location="L10", community=0, community_name="a.py",
               file_type="code")
    G.add_node("a_beta", label="beta()", source_file="pkg/a.py",
               source_location="L40", community=0, community_name="a.py",
               file_type="code")
    G.add_node("b_gamma", label="gamma()", source_file="pkg/b.py",
               source_location="L5", community=1, community_name="b.py",
               file_type="code")
    G.add_edge("a_alpha", "b_gamma", relation="calls", confidence="EXTRACTED",
               _src="a_alpha", _tgt="b_gamma")
    data = nx.node_link_data(G, edges="links")
    p = tmp_path / "graphify-out" / "graph.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "a.py").write_text("# a\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("# b\n", encoding="utf-8")
    return p


def _log_with(tmp_path: Path, rows) -> Path:
    p = tmp_path / "comms.log"
    now = datetime.now(timezone.utc)
    for i, (actor, scope, intent) in enumerate(rows):
        _log.append(p, _log.Event(ts=now - timedelta(minutes=10 - i),
                                  id=_log.new_id(), actor=actor, type="claim",
                                  scope=[scope], data={"intent": intent}))
    if not rows:
        p.write_text("", encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# the picture
# --------------------------------------------------------------------------


def test_marks_land_on_the_claimed_node(tmp_path):
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/a.py#alpha()", "rename it")])
    res = _view.render(tmp_path, tmp_path / "out.html", graph_file=str(g), log_file=lg)

    assert res.kind == "overlay"
    assert set(res.data.held) == {"a_alpha"}
    assert res.data.held["a_alpha"].actor == "ann"
    assert res.data.held["a_alpha"].why == "rename it"
    # one call away, along a relation contact.py counts as worth walking
    assert set(res.data.near) == {"b_gamma"}


def test_a_held_node_is_never_also_nearby(tmp_path):
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/a.py#alpha()", ""),
                              ("bo", "pkg/b.py#gamma()", "")])
    res = _view.render(tmp_path, tmp_path / "out.html", graph_file=str(g), log_file=lg)
    assert set(res.data.held) == {"a_alpha", "b_gamma"}
    assert res.data.near == {}


def test_base_document_is_untouched_apart_from_the_appended_block(tmp_path):
    """The picture underneath must be the picture graphify draws today."""
    from graphify.exporters.html import to_html

    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/a.py#alpha()", "x")])
    out = tmp_path / "same-name.html"
    graph = _view.load_graph(g)
    communities, labels = _view.communities_of(graph)
    to_html(graph, communities, str(out), community_labels=labels or None)
    base = out.read_text(encoding="utf-8")

    _view.render(tmp_path, out, graph_file=str(g), log_file=lg)
    overlaid = out.read_text(encoding="utf-8")

    head, sep, tail = base.partition("</body>")
    assert sep, "exporter always emits </body>"
    assert overlaid.startswith(head)          # nothing before </body> changed
    assert overlaid.endswith(sep + tail)      # nothing after </body> changed
    assert "comms-panel" in overlaid[len(head):]


#: Phrases that DENY an order or a verdict. They contain the very words the
#: guard below hunts for, so they are removed before it looks: otherwise the
#: only way to pass the test would be to delete the disclaimers, which is the
#: opposite of what it is for.
_DISCLAIMERS = re.compile(
    r"(not a verdict|never a verdict|neither is a verdict|"
    r"neither implies an order|nothing here implies an order|"
    r"no arrow the machine draws)",
    re.I,
)

_FORBIDDEN = re.compile(
    r"\b(blocked|blocking|must wait|wait for|go first|goes first|do this first|"
    r"depends on you|conflict detected|verdict|priority order|"
    r"implies an order|in this order)\b",
    re.I,
)


@pytest.mark.parametrize("rows", [
    [("ann", "pkg/a.py#alpha()", "rename it")],
    [("ann", "pkg/a.py", "sweep"), ("bo", "pkg/b.py#gamma()", "fix")],
    [],
])
def test_the_page_never_implies_an_order_or_a_verdict(tmp_path, rows):
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, rows)
    out = tmp_path / "out.html"
    _view.render(tmp_path, out, graph_file=str(g), log_file=lg)
    text = out.read_text(encoding="utf-8")
    # Only the block this module added; the base document is not ours to police.
    added = text.partition("<style>\n  /* Coordination overlay")[2]
    hits = _FORBIDDEN.findall(_DISCLAIMERS.sub("", added))
    assert not hits, f"overlay says {hits!r}, which reads as an order or a verdict"


def test_the_page_states_its_limits(tmp_path):
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/a.py#alpha()", "x")])
    out = tmp_path / "out.html"
    _view.render(tmp_path, out, graph_file=str(g), log_file=lg)
    text = out.read_text(encoding="utf-8")
    assert "not a guarantee" in text
    assert "prompt to look" in text
    assert "neither implies an order" in text


# --------------------------------------------------------------------------
# the honest empty cases
# --------------------------------------------------------------------------


def test_no_map_still_lists_who_holds_what(tmp_path):
    """The regression that matters: no map is not the same as nobody working."""
    lg = _log_with(tmp_path, [("ann", "pkg/a.py#alpha()", "rename it")])
    out = tmp_path / "out.html"
    res = _view.render(tmp_path, out, graph_file=str(tmp_path / "missing.json"),
                       log_file=lg)
    assert res.kind == "no-map"
    text = out.read_text(encoding="utf-8")
    assert "No map has been built" in text
    assert "ann" in text and "pkg/a.py#alpha()" in text
    assert "Nobody is holding anything" not in text


def test_no_map_and_no_claims_says_both(tmp_path):
    lg = _log_with(tmp_path, [])
    out = tmp_path / "out.html"
    res = _view.render(tmp_path, out, graph_file=str(tmp_path / "missing.json"),
                       log_file=lg)
    assert res.kind == "no-map"
    text = out.read_text(encoding="utf-8")
    assert "No map has been built" in text
    assert "Nobody is holding anything right now" in text
    assert "not a statement about whether the code is free" in text


def test_map_but_nobody_holding_still_draws_the_map(tmp_path):
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [])
    out = tmp_path / "out.html"
    res = _view.render(tmp_path, out, graph_file=str(g), log_file=lg)
    assert res.kind == "quiet"
    text = out.read_text(encoding="utf-8")
    assert "vis-network" in text                    # the map is really there
    assert "Nobody is holding anything right now" in text
    assert '"held": []' in text or '"held":[]' in text


# --------------------------------------------------------------------------
# the loud misses
# --------------------------------------------------------------------------


def test_a_claim_the_map_cannot_place_is_shown_with_its_reason(tmp_path):
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/ghost.py#nope", "typed from memory")])
    out = tmp_path / "out.html"
    res = _view.render(tmp_path, out, graph_file=str(g), log_file=lg)
    row = res.data.claims[0]
    assert row.miss_reason and "ghost.py" in row.miss_reason
    assert row.node_ids == []
    text = out.read_text(encoding="utf-8")
    assert "not on the map" in text


def test_a_wide_claim_marks_everything_but_captions_once(tmp_path):
    """A claim covering many nodes is marked on all of them and NAMED once.

    That is a rule about text: forty rings each stamped with the same owner's
    name is unreadable. It is not a reason to withhold anything.
    """
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/a.py", "sweep")])
    old = _view.MAX_LABELLED_PLACES
    _view.MAX_LABELLED_PLACES = 1
    try:
        res = _view.render(tmp_path, tmp_path / "out.html", graph_file=str(g),
                           log_file=lg)
    finally:
        _view.MAX_LABELLED_PLACES = old

    assert set(res.data.held) == {"a_alpha", "a_beta"}
    captioned = [h for h in res.data.held.values() if h.tag]
    assert len(captioned) == 1
    assert captioned[0].also == 1


def test_a_whole_file_claim_still_gets_its_nearby_ring(tmp_path):
    """IF THIS FAILS: the faint ring is dead again and half the legend lies.

    This module used to withhold the nearby ring from any claim covering more
    than three PLACES: three graph nodes. A whole-file claim is the natural
    unit and resolves to one node per symbol, so every ordinary claim tripped it
    at once: four claims in a real run covered 24, 37, 11 and 9 places and every
    one printed "nearby not drawn", while the legend below the picture went on
    explaining what a faint ring meant. contact.py had the same bug, fixed by
    counting scopes instead of nodes; this copy survived because it read the
    renamed constant through getattr with a default.
    """
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/a.py", "sweep")])
    res = _view.render(tmp_path, tmp_path / "out.html", graph_file=str(g), log_file=lg)
    assert res.data.near, "a whole-file claim must still ring its neighbours"
    assert not res.data.claims[0].near_note, "nothing was truncated, so say nothing"


def test_too_many_neighbours_are_truncated_out_loud(tmp_path):
    """IF THIS FAILS: a claim in a busy module either floods the picture or
    quietly drops neighbours. Stopping is fine; stopping in silence is not."""
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/a.py", "sweep")])
    old = _view.MAX_NEAR
    _view.MAX_NEAR = 1
    try:
        res = _view.render(tmp_path, tmp_path / "out.html", graph_file=str(g),
                           log_file=lg)
    finally:
        _view.MAX_NEAR = old
    assert len(res.data.near) <= 1
    assert "only the first 1 neighbours" in (res.data.claims[0].near_note or "")


def test_stale_map_is_reported_not_hidden(tmp_path):
    import os
    import time

    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/a.py#alpha()", "x")])
    future = time.time() + 60
    os.utime(tmp_path / "pkg" / "a.py", (future, future))
    res = _view.render(tmp_path, tmp_path / "out.html", graph_file=str(g), log_file=lg)
    assert res.data.claims[0].stale_note
    assert "out of date" in res.data.claims[0].stale_note


# --------------------------------------------------------------------------
# injection
# --------------------------------------------------------------------------


def test_a_hostile_intent_cannot_break_out_of_the_script(tmp_path):
    g = _graph_json(tmp_path)
    nasty = '</script><img src=x onerror=alert(1)>'
    lg = _log_with(tmp_path, [("ann", "pkg/a.py#alpha()", nasty)])
    out = tmp_path / "out.html"
    _view.render(tmp_path, out, graph_file=str(g), log_file=lg)
    text = out.read_text(encoding="utf-8")
    added = text.partition("<style>\n  /* Coordination overlay")[2]
    assert "</script><img" not in added
    # Assert the PROPERTY, not the spelling: no raw "<" from agent text may
    # reach the script element. The escape used to be "<\\/" and is now
    # "\\u003c", which is stronger: it also closes "<!--" and "<![CDATA[",
    # neither of which the old form touched.
    assert "\\u003c/script>" in added or "<\\/script>" in added


def test_agent_text_cannot_delete_the_coordination_overlay(tmp_path):
    """IF THIS FAILS: an intent containing certain characters makes every claim
    silently vanish from the map while the map itself still draws.

    The payload sits inside a <script> element. Escaping only `</` left
    `<!--<script` open an HTML comment that swallowed the rest of the element,
    so the overlay disappeared with nothing erroring: the worst way for this to
    fail, because the page looks fine and simply reports nobody is anywhere.
    """
    import json

    from comms_graph.view import _js

    for hostile in ("</script><b>x", "<!--<script", "<![CDATA[", "a b"):
        out = _js({"why": hostile})
        assert "<" not in out, f"{hostile!r} left a raw < in the payload"
        assert " " not in out and " " not in out
        assert json.loads(out) == {"why": hostile}, "escaping must not change the value"


def test_a_log_that_will_not_parse_does_not_render_as_nobody_is_here(tmp_path):
    """IF THIS FAILS: the picture prints its most reassuring sentence out of
    data it just failed to read.

    An absent log and an unreadable one had the same result: empty state: so
    the map said "Nobody is holding anything right now" for both. One of those
    is true and the other is the absence of an answer, and an agent reading the
    second as the first edits ground somebody is standing on.
    """
    import json

    from comms_graph import view as _v

    (tmp_path / "graphify-out").mkdir()
    (tmp_path / "graphify-out" / "graph.json").write_text(json.dumps({
        "nodes": [{"id": "n1", "label": "a()", "source_file": "a.py",
                   "source_location": "L1"}],
        "links": [],
    }))
    broken = tmp_path / "log.jsonl"
    broken.write_text('{"not a valid event": true}\n')

    res = _v.render(tmp_path, tmp_path / "out.html", log_file=broken)
    assert res.data.blind is True
    assert "could not be read" in res.data.headline
    assert "NOT saying the code is free" in res.data.headline
    assert '"blind": true' in (tmp_path / "out.html").read_text(encoding="utf-8")


def test_a_genuinely_empty_log_still_reads_as_empty(tmp_path):
    """The distinction must not swallow the ordinary case: a repo nobody has
    claimed in yet really is empty, and saying so is correct."""
    import json

    from comms_graph import view as _v

    (tmp_path / "graphify-out").mkdir()
    (tmp_path / "graphify-out" / "graph.json").write_text(json.dumps({
        "nodes": [{"id": "n1", "label": "a()", "source_file": "a.py",
                   "source_location": "L1"}],
        "links": [],
    }))
    empty = tmp_path / "log.jsonl"
    empty.write_text("")
    res = _v.render(tmp_path, tmp_path / "out.html", log_file=empty)
    assert res.data.blind is False
    assert "Nobody is holding anything" in res.data.headline


def test_a_map_that_exists_and_will_not_load_says_so(tmp_path):
    """IF THIS FAILS: a corrupt graph.json is reported as "no map has been built
    yet, run graphify extract", which sends somebody to re-run the command that
    produced the broken file, and reads as an ordinary first-run state."""
    from comms_graph import view as _v

    (tmp_path / "graphify-out").mkdir()
    (tmp_path / "graphify-out" / "graph.json").write_text("{ this is not json")
    res = _v.render(tmp_path, tmp_path / "out.html")
    assert res.kind == "unreadable-map"
    assert "could not be read" in res.data.headline
    assert "No map has been built" not in res.data.headline


def test_the_headline_counts_only_the_people_it_can_draw(tmp_path):
    """IF THIS FAILS: the sentence promises a picture of N agents and draws
    fewer, with no way for the reader to tell which.

    Counting every claimant made it say "1 place(s) held by 3 people" when two
    of the three had claimed files the map has never seen. The unplaced claims
    are not dropped: they are counted separately and listed, because an
    unplaceable claim is usually a brand-new file, which is exactly when two
    agents are most likely to collide.
    """
    g = _graph_json(tmp_path)
    lg = _log_with(tmp_path, [("ann", "pkg/a.py#alpha()", "real"),
                              ("bo", "pkg/ghost.py", "not in the map"),
                              ("cy", "pkg/absent.py", "also not")])
    res = _view.render(tmp_path, tmp_path / "out.html", graph_file=str(g), log_file=lg)
    assert "held by 1 person" in res.data.headline
    assert "2 further claim(s) could not be placed" in res.data.headline
    # and they are still visible, not swallowed
    assert sum(1 for c in res.data.claims if c.miss_reason) == 2


def test_the_no_map_page_also_stops_answering_from_an_unreadable_log(tmp_path):
    """IF THIS FAILS: the page contradicts itself and the reassuring half is the
    false one.

    The interactive template was fixed for this; the no-map page was missed. It
    printed "Nobody is holding anything right now. That is what the log says",
    attributing the most reassuring sentence the tool has to a log it had just
    failed to parse, while the board rail beside it showed the parse error.
    """
    from comms_graph import view as _v

    broken = tmp_path / "log.jsonl"
    broken.write_text('{"not a valid event": true}\n')
    out = tmp_path / "out.html"
    res = _v.render(tmp_path, out, graph_file=str(tmp_path / "missing.json"),
                    log_file=broken)
    assert res.data.blind is True
    text = out.read_text(encoding="utf-8")
    assert "Nobody is holding anything" not in text
    assert "cannot say who is where" in text


def test_the_no_map_page_still_reads_as_empty_when_it_really_is(tmp_path):
    from comms_graph import view as _v

    empty = tmp_path / "log.jsonl"
    empty.write_text("")
    out = tmp_path / "out.html"
    res = _v.render(tmp_path, out, graph_file=str(tmp_path / "missing.json"),
                    log_file=empty)
    assert res.data.blind is False
    assert "Nobody is holding anything" in out.read_text(encoding="utf-8")
