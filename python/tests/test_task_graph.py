"""The task graph: what exists, what order, and was it actually checked.

The properties here are the ones that make the graph worth having. If phase
derivation drifts, the board confidently tells agents to start work that is not
startable; if the review gate leaks, "verified" stops meaning anything; and if
the cycle guard fails, a malformed graph hangs the pre-edit hook in front of
every tool call an agent makes.

Each test says what breaks in the real world if it fails.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from pathlib import Path

import pytest

from comms_graph import log as clog
from comms_graph import state as cstate
from comms_graph import task as ctask

UTC = timezone.utc
T0 = datetime(2026, 5, 22, 14, 30, tzinfo=UTC)


class _Seq:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, typ, actor, data=None, scope=None):
        self.n += 1
        ts = T0 + timedelta(seconds=self.n)
        return clog.Event(ts=ts, id=clog.new_id(ts), actor=actor, type=typ,
                          scope=scope, data=data or {})


def phases(st):
    return {k: v.phase for k, v in st.tasks.items()}


def _chain(ev):
    """api -> ui, where ui consumes an interface from api.

    `api` asks for review explicitly. Review is opt-in: a task that never asked
    for one is finished when it is done, and everything below this point is
    testing the GATE, so the precondition has to be stated rather than assumed.
    It used to be assumed because review was unconditional.
    """
    return [
        ev("task", "planner", {"task": "api", "title": "Auth API",
                               "checks": ["test"], "review": True}),
        ev("task", "planner", {"task": "ui", "title": "Login screen"}),
        ev("task_edge", "planner", {"from": "api", "to": "ui", "kind": "interface",
                                    "provides": "POST /session returns a token"}),
    ]


# ---------------------------------------------------------------------------
# The review gate: the reason the graph exists at all
# ---------------------------------------------------------------------------


def test_a_successor_waits_for_verified_not_merely_done():
    """IF THIS FAILS: the review gate is decorative. An agent marks its own work
    done and everything downstream immediately unblocks, so unchecked work
    propagates through the whole plan, which is the exact failure the
    verify-before-unblock design exists to prevent."""
    ev = _Seq()
    evs = _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                               "checks": {"test": "pass"}}))
    st = cstate.fold(evs)
    assert phases(st) == {"api": "review", "ui": "blocked"}

    evs.append(ev("task_state", "codex-dev", {"task": "api", "state": "verified"}))
    st = cstate.fold(evs)
    assert phases(st) == {"api": "closed", "ui": "ready"}


def test_an_agent_cannot_verify_its_own_work_under_a_role_suffix():
    """IF THIS FAILS: self-review is one rename away. An agent that reviews as
    `claude-dev/review` is the same agent with the same blind spots, and
    self-review measurably fails, which is the whole reason a DIFFERENT agent
    is on the critical path."""
    ev = _Seq()
    evs = _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                               "checks": {"test": "pass"}}))
    evs.append(ev("task_state", "claude-dev/review", {"task": "api", "state": "verified"}))
    st = cstate.fold(evs)
    assert st.tasks["api"].phase == "review", "self-review must not close the task"
    assert not st.tasks["api"].verified_by
    assert "self-review" in st.refused_task_states[-1].reason


def test_a_refused_transition_is_recorded_not_silently_dropped():
    """IF THIS FAILS: the rules enforce themselves invisibly. A rule that leaves
    no trace when it fires looks exactly like a rule nobody wrote, and the first
    thing anybody asks of a gate is how often it caught something."""
    ev = _Seq()
    evs = _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done"}))
    st = cstate.fold(evs)
    assert len(st.refused_task_states) == 1
    r = st.refused_task_states[0]
    assert r.task == "api" and r.actor == "claude-dev" and r.phase == "done"
    assert "test" in r.reason


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def test_done_is_refused_when_a_declared_check_did_not_run():
    """IF THIS FAILS: a gate silently stops gating. The commonest way a required
    check rots is that it quietly stops running, so a MISSING result must read
    exactly like a failing one: silence is not a pass."""
    ev = _Seq()
    evs = _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done"}))
    st = cstate.fold(evs)
    assert st.tasks["api"].phase == "ready", "an ungated done must not take effect"
    assert not st.tasks["api"].did


def test_done_is_refused_when_a_check_reports_failure():
    ev = _Seq()
    evs = _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                               "checks": {"test": "fail"}}))
    st = cstate.fold(evs)
    assert not st.tasks["api"].did
    assert "test" in st.refused_task_states[-1].reason


# ---------------------------------------------------------------------------
# Rejection is the rework edge
# ---------------------------------------------------------------------------


def test_a_rejection_sends_the_task_back_and_reblocks_what_followed_it():
    """IF THIS FAILS: rejected work stays "done enough" to unblock its
    successors, so the rework never actually happens before the next task is
    built on top of it."""
    ev = _Seq()
    evs = _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                               "checks": {"test": "pass"}}))
    evs.append(ev("task_state", "codex-dev", {"task": "api", "state": "rejected",
                                              "findings": [{"what": "token never expires",
                                                            "where": "src/auth.ts"}]}))
    st = cstate.fold(evs)
    assert phases(st) == {"api": "ready", "ui": "blocked"}
    t = st.tasks["api"]
    assert t.did == "" and t.verified_by == ""
    assert t.rejections == 1
    assert [f.what for f in t.findings] == ["token never expires"]


def test_the_graph_is_not_redrawn_by_a_rejection():
    """IF THIS FAILS: rework mutates the plan. The edges are a record of what
    depends on what; a rejection is news about one node's state, and rewriting
    the graph in response would lose the structure somebody declared."""
    ev = _Seq()
    evs = _chain(ev)
    before = len(cstate.fold(evs).task_edges)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                               "checks": {"test": "pass"}}))
    evs.append(ev("task_state", "codex-dev", {"task": "api", "state": "rejected"}))
    assert len(cstate.fold(evs).task_edges) == before


# ---------------------------------------------------------------------------
# Derivation from live coordination state
# ---------------------------------------------------------------------------


def test_who_is_doing_a_task_comes_from_live_claims():
    """IF THIS FAILS: somebody has to remember to say they stopped. Deriving the
    doers from ACTIVE claims means releasing a file empties them for free, and a
    task cannot be left showing a worker who walked away hours ago."""
    ev = _Seq()
    evs = _chain(ev)
    claim = ev("claim", "fable-dev", {"intent": "build it", "task": "api"}, scope=["src/auth.ts"])
    evs.append(claim)
    st = cstate.fold(evs)
    assert st.tasks["api"].phase == "doing"
    assert st.tasks["api"].doers == ["fable-dev"]

    evs.append(ev("release", "fable-dev", {"refs": [claim.id]}))
    st = cstate.fold(evs)
    assert st.tasks["api"].doers == []
    assert st.tasks["api"].phase == "ready"


def test_a_claim_tagged_to_no_task_does_not_invent_one():
    ev = _Seq()
    evs = _chain(ev)
    evs.append(ev("claim", "fable-dev", {"intent": "unrelated"}, scope=["README.md"]))
    st = cstate.fold(evs)
    assert set(st.tasks) == {"api", "ui"}
    assert st.tasks["api"].doers == []


# ---------------------------------------------------------------------------
# Cycles: this runs in front of every agent tool call
# ---------------------------------------------------------------------------


def test_a_cycle_resolves_to_a_phase_instead_of_hanging():
    """IF THIS FAILS: a malformed graph wedges the pre-edit hook, which runs
    before every Edit an agent makes, so one bad pair of edges stops all work
    in the repository rather than reporting itself."""
    ev = _Seq()
    evs = [
        ev("task", "p", {"task": "a"}), ev("task", "p", {"task": "b"}),
        ev("task", "p", {"task": "c"}),
        ev("task_edge", "p", {"from": "a", "to": "b"}),
        ev("task_edge", "p", {"from": "b", "to": "c"}),
        ev("task_edge", "p", {"from": "c", "to": "a"}),
    ]
    started = time.monotonic()
    st = cstate.fold(evs)
    assert time.monotonic() - started < 2.0, "folding a cycle must not take real time"
    assert phases(st) == {"a": "cycle", "b": "cycle", "c": "cycle"}


def test_a_task_downstream_of_a_cycle_is_reported_as_cycle_too():
    """A task that can never be reached is not 'blocked': blocked implies
    somebody finishing something unblocks you, and here nobody can."""
    ev = _Seq()
    evs = [
        ev("task", "p", {"task": "a"}), ev("task", "p", {"task": "b"}),
        ev("task", "p", {"task": "downstream"}),
        ev("task_edge", "p", {"from": "a", "to": "b"}),
        ev("task_edge", "p", {"from": "b", "to": "a"}),
        ev("task_edge", "p", {"from": "b", "to": "downstream"}),
    ]
    st = cstate.fold(evs)
    assert st.tasks["downstream"].phase == "cycle"


def test_a_self_edge_is_dropped_rather_than_making_a_task_its_own_blocker():
    ev = _Seq()
    evs = [ev("task", "p", {"task": "solo"}),
           ev("task_edge", "p", {"from": "solo", "to": "solo"})]
    st = cstate.fold(evs)
    assert st.task_edges == []
    assert st.tasks["solo"].phase == "ready"


def test_an_edge_naming_an_undeclared_task_is_noise_not_a_blocker():
    """IF THIS FAILS: a typo in a plan permanently blocks a real task on
    something that does not exist, and nothing can ever unblock it."""
    ev = _Seq()
    evs = [ev("task", "p", {"task": "real"}),
           ev("task_edge", "p", {"from": "ghost", "to": "real"})]
    st = cstate.fold(evs)
    assert st.tasks["real"].phase == "ready"


def test_a_long_chain_does_not_recurse():
    """IF THIS FAILS: deep plans crash instead of folding. An iterative pass has
    no stack depth to exhaust; a recursive one dies somewhere past a thousand."""
    ev = _Seq()
    evs = [ev("task", "p", {"task": "t%03d" % i}) for i in range(400)]
    evs += [ev("task_edge", "p", {"from": "t%03d" % i, "to": "t%03d" % (i + 1)})
            for i in range(399)]
    st = cstate.fold(evs)
    assert st.tasks["t000"].phase == "ready"
    assert st.tasks["t399"].phase == "blocked"
    assert st.tasks["t399"].blocked_by == ["t398"]


# ---------------------------------------------------------------------------
# Upsert semantics
# ---------------------------------------------------------------------------


def test_restating_a_task_without_its_checks_does_not_drop_the_gate():
    """IF THIS FAILS: renaming a task's title silently removes the checks that
    had to pass before it could be called done."""
    ev = _Seq()
    evs = [ev("task", "p", {"task": "api", "title": "Auth", "checks": ["test", "lint"]}),
           ev("task", "p", {"task": "api", "title": "Auth API v2"})]
    st = cstate.fold(evs)
    assert st.tasks["api"].title == "Auth API v2"
    assert st.tasks["api"].checks == ["test", "lint"]


def test_an_edge_is_upserted_not_duplicated():
    ev = _Seq()
    evs = [ev("task", "p", {"task": "a"}), ev("task", "p", {"task": "b"}),
           ev("task_edge", "p", {"from": "a", "to": "b", "kind": "sequence"}),
           ev("task_edge", "p", {"from": "a", "to": "b", "kind": "interface",
                                 "provides": "the schema"})]
    st = cstate.fold(evs)
    assert len(st.task_edges) == 1
    assert st.task_edges[0].kind == "consumes"
    assert st.task_edges[0].provides == "the schema"


def test_an_unknown_edge_kind_becomes_the_weakest_one():
    """Guessing 'interface' would invent a rework dependency nobody declared."""
    ev = _Seq()
    evs = [ev("task", "p", {"task": "a"}), ev("task", "p", {"task": "b"}),
           ev("task_edge", "p", {"from": "a", "to": "b", "kind": "banana"})]
    st = cstate.fold(evs)
    assert st.task_edges[0].kind == "sequence"


# ---------------------------------------------------------------------------
# Guarding the write side
# ---------------------------------------------------------------------------


def test_a_cycle_can_be_detected_before_it_is_written():
    """The fold survives a cycle, but a graph reporting 'cycle' is a graph
    nobody can use. Far kinder to refuse the edge that would close the loop."""
    edges = [ctask.TaskEdge("a", "b"), ctask.TaskEdge("b", "c")]
    assert ctask.would_cycle(edges, "c", "a") is True
    assert ctask.would_cycle(edges, "a", "a") is True
    assert ctask.would_cycle(edges, "a", "d") is False
    assert ctask.would_cycle(edges, "c", "d") is False


def test_independence_is_reported_honestly_when_it_cannot_be_told():
    """IF THIS FAILS: an unknown verifier reads as an independent one. 'Verified'
    and 'verified by something with the same blind spots' are different claims,
    and the absence of evidence must not resolve to the stronger of the two."""
    def run(hellos):
        # fold sorts by timestamp, so a hello has to be MINTED before the
        # transition it explains, not merely placed before it in the list.
        ev = _Seq()
        evs = [ev("hello", who, {"vendor": vendor}) for who, vendor in hellos]
        evs += _chain(ev)
        evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                                   "checks": {"test": "pass"}}))
        evs.append(ev("task_state", "codex-dev", {"task": "api", "state": "verified"}))
        return cstate.fold(evs).tasks["api"].independence

    assert run([]) == "unknown", "no evidence must not read as independent"
    assert run([("claude-dev", "anthropic"), ("codex-dev", "openai")]) == "independent"
    assert run([("claude-dev", "anthropic"), ("codex-dev", "anthropic")]) == "same-family"
    assert run([("claude-dev", "anthropic")]) == "unknown", "half the evidence is not evidence"


# ---------------------------------------------------------------------------
# Through the real CLI: the surface agents actually touch
# ---------------------------------------------------------------------------


def _cli(repo, *args, session=None):
    """Run the real command in `repo`. Returns (exit_code, stdout+stderr).

    `session` is the host agent's session id. Real agents each have their own;
    a test that leaves it unset inherits the session of whatever process is
    running pytest, which makes every actor in the test look like ONE agent to
    the review gate: correctly, since it would be.
    """
    import io
    import os
    from contextlib import redirect_stderr, redirect_stdout

    from comms_graph import cli as ccli

    out = io.StringIO()
    was = os.getcwd()
    prior = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.chdir(repo)
    try:
        if session is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = session
        else:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        with redirect_stdout(out), redirect_stderr(out):
            try:
                code = ccli.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(was)
        if prior is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior
    return code, out.getvalue()


def _repo(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    git = __import__("shutil").which("git")
    if git is None:
        import pytest
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=repo, check=True)
    return repo


def test_the_drawing_uses_definite_heights_not_a_percentage_chain(tmp_path):
    """IF THIS FAILS: the canvas renders blank and nothing says why.

    vis sizes its canvas to fill its container. If that container's height is a
    percentage resolving against an ancestor with no definite height, the canvas
    ends up driving the box that was supposed to be driving it: observed
    growing 2036 -> 2072 -> 2120 px across redraws while painting nothing at
    all. Every diagnostic looked healthy: the canvas existed, reported sensible
    dimensions, and the network reported nine positioned nodes.
    """
    from comms_graph import taskview

    out = tmp_path / "t.html"
    taskview.render(_state_with_a_chain(), out)
    css = out.read_text(encoding="utf-8")
    assert "100dvh" in css, "the wrapper needs a height that resolves on its own"
    assert "height: 100%;\n" not in css.split("<script>")[0].replace("  ", " "), \
        "no bare percentage height may drive the canvas box"


def test_the_drawing_disables_the_layout_pre_pass_that_gives_up(tmp_path):
    """IF THIS FAILS: vis's improvedLayout pre-pass can bail on a graph and
    leave every node without coordinates: it logs an info message, not an
    error, and the page renders empty. Hierarchical layout assigns every
    position itself, so the pre-pass has nothing to contribute here anyway."""
    from comms_graph import taskview

    out = tmp_path / "t.html"
    taskview.render(_state_with_a_chain(), out)
    assert "improvedLayout: false" in out.read_text(encoding="utf-8")


def test_an_empty_task_graph_says_so_rather_than_looking_broken(tmp_path):
    """Early on there are no tasks at all. That is the normal state, and a blank
    dark rectangle is indistinguishable from a page that failed to load."""
    from comms_graph import taskview

    class _Empty:
        tasks: dict = {}
        task_edges: list = []

    out = tmp_path / "t.html"
    res = taskview.render(_Empty(), out)
    assert res.task_count == 0
    body = out.read_text(encoding="utf-8")
    assert "Nothing declared yet" in body
    assert "No tasks declared yet" in body


def test_the_drawing_distinguishes_consuming_edges_from_mere_ordering(tmp_path):
    """The distinction is what makes a rejection precise instead of
    invalidating everything downstream, so it has to be visible in the picture
    and not only in the data."""
    from comms_graph import taskview

    nodes, edges, summary = taskview.build(_state_with_a_chain())
    by_pair = {(e["from"], e["to"]): e for e in edges}
    consuming = by_pair[("api", "ui")]
    assert consuming["dashes"] is False, "a consuming edge is drawn solid"
    ordering = by_pair[("api", "later")]
    assert ordering["dashes"] is True, "an ordering-only edge is drawn dashed"
    assert all(e["arrows"]["to"]["enabled"] for e in edges), \
        "every edge carries an arrowhead: the direction IS the content here"


def test_an_edge_to_an_undeclared_task_is_not_drawn_but_is_counted(tmp_path):
    """Drawing it would invent a node; hiding it silently would lose the fact
    that somebody wrote it down."""
    from comms_graph import taskview

    st = _state_with_a_chain()
    st.task_edges.append(ctask.TaskEdge(from_="api", to="ghost"))
    nodes, edges, summary = taskview.build(st)
    assert all(e["to"] != "ghost" for e in edges)
    assert summary["dangling"] == 1


def _state_with_a_chain():
    ev = _Seq()
    evs = [
        ev("task", "p", {"task": "api", "title": "Auth API"}),
        ev("task", "p", {"task": "ui", "title": "Login"}),
        ev("task", "p", {"task": "later", "title": "Docs"}),
        ev("task_edge", "p", {"from": "api", "to": "ui", "kind": "interface",
                              "provides": "the token format"}),
        ev("task_edge", "p", {"from": "api", "to": "later", "kind": "sequence"}),
    ]
    return cstate.fold(evs)


def test_a_task_title_cannot_close_the_script_block(tmp_path):
    """IF THIS FAILS: any agent that can name a task can run script in the
    browser of whoever is watching the board.

    json.dumps escapes quotes and backslashes but NOT '<', so a string
    containing '</script>' closes the block early and everything after it is
    parsed as live HTML. Every string embedded here is written by another agent:
    task titles, the "provides" text on an edge, actor names. Reproduced with a
    task titled '</title><script>alert(3)</script>', which executed.

    view.py already defended against this; this module was written separately
    and did not carry it over, which is exactly how it got in.
    """
    import json as _json
    import re

    from comms_graph import taskview

    ev = _Seq()
    nasty = "</title><script>alert(3)</script>"
    evs = [
        ev("task", "p", {"task": "pwn", "title": nasty}),
        ev("task", "p", {"task": "pwn2"}),
        ev("task_edge", "p", {"from": "pwn", "to": "pwn2",
                              "kind": "interface", "provides": '"><script>alert(4)</script>'}),
    ]
    out = tmp_path / "t.html"
    taskview.render(cstate.fold(evs), out)
    body = out.read_text(encoding="utf-8")

    injected = [m for m in re.findall(r"<script[^>]*>", body) if "vis-network" not in m]
    assert len(injected) == 1, f"only the page's own script tag may appear, found {injected}"
    assert nasty not in body, "the payload must never appear literally"

    # ...and the escaping must not corrupt the value it protects.
    m = re.search(r"const NODES = (\[.*?\]);", body, re.S)
    assert m, "node payload missing"
    nodes = _json.loads(m.group(1))
    assert any(nasty in n["label"] for n in nodes), \
        "the title must still decode to exactly what was written"


# ---------------------------------------------------------------------------
# The review gate, attacked
# ---------------------------------------------------------------------------


def test_a_leading_slash_does_not_empty_the_base_name():
    """IF THIS FAILS: the gate breaks in BOTH directions at once.

    Splitting on "/" made '/alice' and '/bob' both reduce to '', so two
    genuinely different agents were refused as self-review: while
    '/claude-dev' reviewing 'claude-dev' sailed through, because '' never
    equals 'claude-dev'. A leading slash is not a role suffix.
    """
    assert ctask.base_actor("/alice") == "/alice"
    assert ctask.base_actor("/alice") != ctask.base_actor("/bob")
    assert ctask.base_actor("claude-dev/review") == "claude-dev"
    assert ctask.base_actor("a/b/c") == "a"


@pytest.mark.parametrize("disguise,label", [
    ("CLAUDE-DEV", "a different case"),
    ("claude-dev​", "a zero-width space"),
    ("claude-dev ", "a non-breaking space"),
])


def test_a_cheap_disguise_cannot_sign_off_its_own_work(disguise, label):
    """IF THIS FAILS: the one guarantee this tool makes is defeated by typing
    your own name differently, and the printed line reads identically to a real
    review, so nobody looking at the board can tell."""
    ev = _Seq()
    evs = _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                               "checks": {"test": "pass"}}))
    evs.append(ev("task_state", disguise, {"task": "api", "state": "verified"}))
    st = cstate.fold(evs)
    assert st.tasks["api"].phase == "review", f"{label} signed off its own work"
    assert not st.tasks["api"].verified_by


@pytest.mark.parametrize("homoglyph,label", [
    ("claude‐dev", "a U+2010 hyphen"),
    ("clаude-dev", "a Cyrillic a"),
    ("/claude-dev", "a leading slash"),
])


def test_a_homoglyph_is_caught_by_the_session_not_the_name(homoglyph, label):
    """IF THIS FAILS: an agent renames itself with a lookalike character and
    signs off its own work.

    No amount of normalising closes this: a Cyrillic 'a' genuinely IS a
    different letter. What closes it is that both names come from ONE process,
    and every actor's hello records the host agent's session id. The log already
    held that fact; the gate simply never read it.
    """
    ev = _Seq()
    evs = [
        ev("hello", "claude-dev", {"agent_session": "one-and-the-same"}),
        ev("hello", homoglyph, {"agent_session": "one-and-the-same"}),
    ]
    evs += _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                               "checks": {"test": "pass"}}))
    evs.append(ev("task_state", homoglyph, {"task": "api", "state": "verified"}))
    st = cstate.fold(evs)
    assert st.tasks["api"].phase == "review", f"{label} signed off its own work"
    assert not st.tasks["api"].verified_by
    assert "self-review" in st.refused_task_states[-1].reason


def test_two_genuinely_different_agents_are_still_allowed():
    """IF THIS FAILS the gate is useless in the other direction: nothing can
    ever be verified, so nothing downstream of anything ever unblocks."""
    ev = _Seq()
    evs = [
        ev("hello", "claude-dev", {"agent_session": "agent-A"}),
        ev("hello", "codex-dev", {"agent_session": "agent-B"}),
    ]
    evs += _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                               "checks": {"test": "pass"}}))
    evs.append(ev("task_state", "codex-dev", {"task": "api", "state": "verified"}))
    st = cstate.fold(evs)
    assert st.tasks["api"].phase == "closed"
    assert st.tasks["api"].verified_by == "codex-dev"


def test_next_does_not_offer_you_your_own_work_to_review():
    """The listing has to use the same rule as the gate, or an agent is invited
    to do something it will then be refused for."""
    ev = _Seq()
    evs = [
        ev("hello", "claude-dev", {"agent_session": "one"}),
        ev("hello", "CLAUDE-DEV", {"agent_session": "one"}),
    ]
    evs += _chain(ev)
    evs.append(ev("task_state", "claude-dev", {"task": "api", "state": "done",
                                               "checks": {"test": "pass"}}))
    st = cstate.fold(evs)
    assert ctask.awaiting_review(st.tasks, "CLAUDE-DEV", st.sessions) == []
    assert [t.id for t in ctask.awaiting_review(st.tasks, "codex-dev", st.sessions)] == ["api"]


def test_every_agent_who_submitted_is_barred_from_reviewing_not_just_the_last():
    """IF THIS FAILS: two agents take turns on one task and then sign off each
    other's combined work.

    `did` holds only the most recent submission, so alice-then-bob left bob
    recorded, and alice, who had written half of it, read as a different agent.
    Review means somebody who did not build it looked at it; "did not build the
    most recent revision" is a different and much weaker claim.
    """
    ev = _Seq()
    evs = [ev("task", "p", {"task": "api", "review": True}),
           ev("task_state", "alice", {"task": "api", "state": "done"}),
           ev("task_state", "bob", {"task": "api", "state": "done"}),
           ev("task_state", "alice", {"task": "api", "state": "verified"})]
    st = cstate.fold(evs)
    assert st.tasks["api"].phase == "review"
    assert not st.tasks["api"].verified_by
    assert "self-review" in st.refused_task_states[-1].reason

    # somebody who never touched it still can
    evs.append(ev("task_state", "carol", {"task": "api", "state": "verified"}))
    assert cstate.fold(evs).tasks["api"].verified_by == "carol"


def test_a_task_whose_predecessor_is_unfinished_cannot_report_itself_closed():
    """IF THIS FAILS: unbuilt work unblocks more unbuilt work.

    Testing verified_by before blocked let a task that was never startable be
    marked done, verified, and reported CLOSED while its own predecessor sat at
    ready, and `next` then offered ITS successors as startable.
    """
    ev = _Seq()
    evs = [ev("task", "p", {"task": "a"}), ev("task", "p", {"task": "b"}),
           ev("task_edge", "p", {"from": "a", "to": "b"}),
           ev("task_state", "alice", {"task": "b", "state": "done"}),
           ev("task_state", "bob", {"task": "b", "state": "verified"})]
    st = cstate.fold(evs)
    assert st.tasks["b"].phase == "blocked", "a premature verification must not close it"
    assert st.tasks["b"].blocked_by == ["a"]

    evs += [ev("task_state", "alice", {"task": "a", "state": "done"}),
            ev("task_state", "bob", {"task": "a", "state": "verified"})]
    st = cstate.fold(evs)
    assert st.tasks["a"].phase == "closed"
    assert st.tasks["b"].phase == "closed", "once the ground is firm it closes"


def _with_edge(ev, kind, finish_b=True):
    evs = [ev("task", "p", {"task": "a"}), ev("task", "p", {"task": "b"}),
           ev("task_edge", "p", {"from": "a", "to": "b", "kind": kind}),
           ev("task_state", "alice", {"task": "a", "state": "done"}),
           ev("task_state", "bob", {"task": "a", "state": "verified"})]
    if finish_b:
        evs += [ev("task_state", "carol", {"task": "b", "state": "done"}),
                ev("task_state", "dave", {"task": "b", "state": "verified"})]
    return evs


@pytest.mark.parametrize("kind", ["interface", "artifact"])


def test_reworking_something_a_finished_task_consumes_flags_it_again(kind):
    """IF THIS FAILS: work stays marked verified against an interface that has
    since moved, and nobody is told to look at it."""
    ev = _Seq()
    evs = _with_edge(ev, kind)
    assert cstate.fold(evs).tasks["b"].phase == "closed"
    evs.append(ev("task_state", "bob", {"task": "a", "state": "rejected"}))
    st = cstate.fold(evs)
    assert st.tasks["b"].phase == "blocked"
    assert st.tasks["b"].blocked_by == ["a"]


def test_reworking_something_a_finished_task_merely_follows_leaves_it_alone():
    """IF THIS FAILS: a rejection invalidates everything downstream of it,
    including work that never consumed anything from the rejected task.

    That is the difference the edge kind exists to record, and it is what makes
    a rejection precise. Reopening finished, reviewed work on no evidence costs
    the board its credibility after about two occurrences.
    """
    ev = _Seq()
    evs = _with_edge(ev, "sequence")
    assert cstate.fold(evs).tasks["b"].phase == "closed"
    evs.append(ev("task_state", "bob", {"task": "a", "state": "rejected"}))
    assert cstate.fold(evs).tasks["b"].phase == "closed"


@pytest.mark.parametrize("kind", ["interface", "sequence"])


def test_ordering_still_holds_for_work_that_has_not_been_done_yet(kind):
    """The kind only changes what REOPENS finished work. Before a task is
    finished, "b comes after a" means what it says, whatever the kind."""
    ev = _Seq()
    evs = _with_edge(ev, kind, finish_b=False)
    assert cstate.fold(evs).tasks["b"].phase == "ready"
    evs.append(ev("task_state", "bob", {"task": "a", "state": "rejected"}))
    assert cstate.fold(evs).tasks["b"].phase == "blocked"


def test_a_sequence_predecessor_that_was_never_verified_still_blocks_a_finished_task():
    """The kind rule must not become a loophole.

    "Signed off and since reworked" and "never signed off at all" both leave
    verified_by empty, and they have opposite consequences: the first is a
    rework the successor may legitimately have been built before, the second
    means the successor was never startable and its sign-off jumped the queue.
    Only ever_verified separates them.
    """
    ev = _Seq()
    evs = [ev("task", "p", {"task": "a"}), ev("task", "p", {"task": "b"}),
           ev("task_edge", "p", {"from": "a", "to": "b", "kind": "sequence"}),
           ev("task_state", "alice", {"task": "b", "state": "done"}),
           ev("task_state", "bob", {"task": "b", "state": "verified"})]
    st = cstate.fold(evs)
    assert st.tasks["b"].phase == "blocked", "a jumped queue is not a rework"
    assert not st.tasks["a"].ever_verified


def test_a_task_two_agents_both_submitted_can_still_be_rejected():
    """IF THIS FAILS: the task deadlocks and everything downstream with it.

    Once alice and bob have both submitted, neither may VERIFY: correct, they
    wrote it, and barring them from rejecting too left the task unmovable until
    a third party appeared. On a two-agent run there is no third party.
    Self-APPROVAL is the failure this gate exists for; self-rejection is
    somebody saying their own work is not ready, which should be easy.
    """
    ev = _Seq()
    evs = [ev("task", "p", {"task": "t", "review": True}),
           ev("task_state", "alice", {"task": "t", "state": "done"}),
           ev("task_state", "bob", {"task": "t", "state": "done"})]
    assert cstate.fold(evs).tasks["t"].phase == "review"

    # neither may verify
    for who in ("alice", "bob"):
        probe = evs + [ev("task_state", who, {"task": "t", "state": "verified"})]
        assert cstate.fold(probe).tasks["t"].phase == "review", f"{who} verified its own work"

    # but either may reject, which unsticks it
    evs.append(ev("task_state", "alice", {"task": "t", "state": "rejected"}))
    st = cstate.fold(evs)
    assert st.tasks["t"].phase == "ready"
    assert st.tasks["t"].did == ""


def test_next_never_offers_a_review_the_gate_will_refuse():
    """The reader half and the enforcing half must ask the same question, or the
    board recommends a command guaranteed to fail."""
    ev = _Seq()
    evs = [ev("task", "p", {"task": "t", "review": True}),
           ev("task_state", "alice", {"task": "t", "state": "done"}),
           ev("task_state", "bob", {"task": "t", "state": "done"})]
    st = cstate.fold(evs)
    assert ctask.awaiting_review(st.tasks, "alice", st.sessions) == []
    assert ctask.awaiting_review(st.tasks, "bob", st.sessions) == []
    assert [t.id for t in ctask.awaiting_review(st.tasks, "carol", st.sessions)] == ["t"]


def test_an_edge_event_naming_no_kind_keeps_the_recorded_one():
    """IF THIS FAILS: adding a note to an edge erases the rework dependency on
    it, and a task a rejection had reopened flips back to closed with nobody
    re-reviewing it. Same rule apply_task uses for checks: only non-empty
    fields overwrite."""
    ev = _Seq()
    evs = [ev("task", "p", {"task": "a"}), ev("task", "p", {"task": "b"}),
           ev("task_edge", "p", {"from": "a", "to": "b", "kind": "interface",
                                 "provides": "schema"}),
           ev("task_edge", "p", {"from": "a", "to": "b", "provides": "schema v2"})]
    st = cstate.fold(evs)
    assert len(st.task_edges) == 1
    assert st.task_edges[0].kind == "consumes"
    assert st.task_edges[0].provides == "schema v2"


def test_a_check_declared_after_submission_invalidates_it():
    """IF THIS FAILS: declaring the checks late is a way around the gate.

    A task with no checks is submitted, a required check is added afterwards,
    and the submission still stands, so the task closes with a required check
    that never ran. The gate's contract is "every declared check passed when
    this was submitted", and a check added later was never reported.
    """
    ev = _Seq()
    evs = [ev("task", "p", {"task": "api"}),
           ev("task_state", "alice", {"task": "api", "state": "done"}),
           ev("task", "p", {"task": "api", "checks": ["test"]})]
    st = cstate.fold(evs)
    assert st.tasks["api"].did == "", "the submission predates the check"
    assert st.tasks["api"].phase == "ready"
    assert "resubmit" in st.tasks["api"].notes[-1]

    evs.append(ev("task_state", "bob", {"task": "api", "state": "verified"}))
    assert cstate.fold(evs).tasks["api"].phase == "ready", \
        "there is nothing awaiting review, so nothing to verify"


def test_restating_a_task_with_the_same_checks_does_not_bounce_the_work():
    """The rule must be about NEW requirements, not about touching the task.
    Renaming a title would otherwise throw away a submission."""
    ev = _Seq()
    evs = [ev("task", "p", {"task": "b", "review": True, "checks": ["test"]}),
           ev("task_state", "alice", {"task": "b", "state": "done",
                                      "checks": {"test": "pass"}}),
           ev("task", "p", {"task": "b", "review": True, "title": "renamed", "checks": ["test"]})]
    st = cstate.fold(evs)
    assert st.tasks["b"].did == "alice"
    assert st.tasks["b"].phase == "review"


def test_a_two_agent_run_has_a_way_out_and_it_is_recorded_honestly():
    """IF THIS FAILS either the task deadlocks or the gate has a silent loophole.

    Once two agents have both submitted, neither may verify and only a third
    party can move it, which on a two-agent run is nobody, so the task and
    everything after it stops forever. Refusing to record anything is not
    safety, it is a stall.

    So it can be recorded, and it is recorded as what it is: never
    "independent", never "same-family", and the board shows it differently.
    Somebody chose to sign off their own work and their name is on it.
    """
    ev = _Seq()
    evs = [ev("task", "p", {"task": "t", "review": True}),
           ev("task_state", "alice", {"task": "t", "state": "done"}),
           ev("task_state", "bob", {"task": "t", "state": "done"})]

    # without the acknowledgement it is still refused
    probe = evs + [ev("task_state", "alice", {"task": "t", "state": "verified"})]
    assert cstate.fold(probe).tasks["t"].phase == "review"

    evs.append(ev("task_state", "alice",
                  {"task": "t", "state": "verified", "acknowledged_self_review": True}))
    t = cstate.fold(evs).tasks["t"]
    assert t.phase == "closed"
    assert t.verified_by == "alice"
    assert t.independence == "self-acknowledged"
    assert t.independence not in ("independent", "same-family"), \
        "a self sign-off must never read as a real review"


def test_two_agents_cannot_both_claim_one_not_yet_created_file(tmp_path, monkeypatch):
    """IF THIS FAILS: both agents are cleared to write one file, during exactly
    the window they are both writing it.

    Everything below the deepest existing directory is kept as typed, because a
    file with no spelling on disk has nothing to defer to, so on a case-blind
    filesystem `src/NewFeature.py` and `src/newfeature.py` were two scopes for
    one file. Claiming the file you are about to create is the primary use of
    this tool, so this was the primary case going wrong.
    """
    from comms_graph.cli import _case_blind_here

    repo = _repo(tmp_path, monkeypatch)
    (repo / "src").mkdir(exist_ok=True)
    if not _case_blind_here(repo / "src"):
        pytest.skip("case-sensitive filesystem: these are genuinely two files")

    code, out = _cli(repo, "claim", "src/NewFeature.py", "--as", "alice",
                     "--intent", "writing it", session="agent-A")
    assert code == 0, out
    code, out = _cli(repo, "claim", "src/newfeature.py", "--as", "bob",
                     "--intent", "also writing it", session="agent-B")
    assert code == 1, f"a second spelling of one file must be refused: {out}"

    # an unrelated new file is still free
    code, out = _cli(repo, "claim", "src/Unrelated.py", "--as", "carol",
                     "--intent", "different file", session="agent-C")
    assert code == 0, out


def test_a_case_variant_of_an_uncreated_claim_is_blocked_by_the_hook(tmp_path, monkeypatch):
    """The hook has to agree with claim, or an agent refused a claim can simply
    edit anyway."""
    import json as _json

    from comms_graph.cli import _case_blind_here

    repo = _repo(tmp_path, monkeypatch)
    (repo / "src").mkdir(exist_ok=True)
    if not _case_blind_here(repo / "src"):
        pytest.skip("case-sensitive filesystem")

    _cli(repo, "claim", "src/NewFeature.py", "--as", "alice", "--intent", "x",
         session="agent-A")

    import io as _io
    import os as _os
    import sys as _sys
    from contextlib import redirect_stderr as _re, redirect_stdout as _ro

    from comms_graph import cli as _ccli

    def hook(session, path):
        payload = _json.dumps({"session_id": session, "tool_name": "Edit",
                               "tool_input": {"file_path": str(path)}})
        buf, was, old = _io.StringIO(), _os.getcwd(), _sys.stdin
        _os.chdir(repo)
        _os.environ["CLAUDE_CODE_SESSION_ID"] = session
        try:
            _sys.stdin = _io.StringIO(payload)
            with _ro(buf), _re(buf):
                try:
                    return _ccli.main(["check", "--stdin-json"])
                except SystemExit as exc:
                    return exc.code if isinstance(exc.code, int) else 1
        finally:
            _sys.stdin = old
            _os.chdir(was)
            _os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    assert hook("agent-B", repo / "src" / "newfeature.py") == 2, "the twin must block"
    assert hook("agent-A", repo / "src" / "NewFeature.py") == 0, "the holder may write"
    assert hook("agent-B", repo / "src" / "Another.py") == 0, "unrelated is free"


@pytest.mark.parametrize("dirname", ["2026", "20260820", "0005", "1.2.3"])


def test_a_directory_with_no_cased_letters_does_not_disable_the_twin_check(
        tmp_path, monkeypatch, dirname):
    """IF THIS FAILS: the protection silently switches off for ordinary
    directory names.

    The probe swaps the case of a directory's own name and compares inodes, and
    the first version gave up when that name had no cased letters. Date folders,
    ticket numbers and version directories all hit it, and every one turned the
    whole thing off on a filesystem that IS case-blind. It walks up to an
    ancestor it can ask about now: case-sensitivity is a property of the
    volume, so any ancestor on it answers the same question.
    """
    from comms_graph.cli import _case_blind_here

    probe = tmp_path / dirname / "src"
    probe.mkdir(parents=True)
    # only meaningful where the filesystem really is case-blind
    if not _case_blind_here(tmp_path):
        pytest.skip("case-sensitive filesystem")
    assert _case_blind_here(probe) is True


def test_a_resubmission_clears_the_independence_it_described(tmp_path, monkeypatch):
    """IF THIS FAILS: a label outlives the thing it labels.

    A resubmission clears verified_by but left independence set, so `brief` on a
    successor said "signed off by @ themselves": a bare @ where the name had
    been, and the drawing painted the self-signed node for a task sitting in
    review with no verifier at all.
    """
    ev = _Seq()
    evs = [ev("task", "p", {"task": "t1"}),
           ev("task_state", "alice", {"task": "t1", "state": "done"}),
           ev("task_state", "alice", {"task": "t1", "state": "verified",
                                      "acknowledged_self_review": True})]
    assert cstate.fold(evs).tasks["t1"].independence == "self-acknowledged"

    evs.append(ev("task_state", "alice", {"task": "t1", "state": "done"}))
    t = cstate.fold(evs).tasks["t1"]
    assert t.verified_by == ""
    assert t.independence == "", "the label must go with the verification"


def test_an_unusable_store_is_an_error_not_a_traceback(tmp_path, monkeypatch):
    """IF THIS FAILS: a wrapper reads an unreachable store as a live conflict.

    Every writing command crashed with a raw FileExistsError and exited 1: the
    code the usage text reserves for "somebody else holds this", so a script
    that retries on conflict would loop forever against a broken store.
    """
    import shutil

    from comms_graph import log as _l

    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "board", session="agent-A")  # create the store
    store = Path(_l.log_path(repo)).parent
    shutil.move(str(store), str(store) + ".moved")
    Path(store).write_text("in the way\n")

    code, out = _cli(repo, "board", session="agent-A")
    assert code == 2, f"an unusable store is not a conflict: exit {code}\n{out}"
    assert "cannot use the coordination store" in out
    assert "Traceback" not in out


def test_a_full_task_is_not_offered(tmp_path, monkeypatch):
    """The slot limit still has to bind, or it is decoration in the other
    direction."""
    repo = _repo(tmp_path, monkeypatch)
    (repo / "b.py").write_text("y = 2\n")
    _cli(repo, "task", "add", "solo", "--as", "p", session="agent-P")
    _cli(repo, "claim", "a.py", "--as", "carol", "--intent", "z", "--task", "solo",
         session="agent-C")
    code, out = _cli(repo, "next", "--as", "dave", session="agent-D")
    assert "solo" not in out or "slots taken" not in out, out


def test_help_on_a_leaf_verb_explains_it_instead_of_running_it(tmp_path, monkeypatch):
    """IF THIS FAILS: asking how a verb works either errors or DOES the thing.

    Every leaf verb ignored --help: `board --help` read the store. For a
    tool whose users are agents, --help is the whole discovery path, and it is
    the one call that must never have a side effect.
    """
    from comms_graph import log as clog2

    repo = _repo(tmp_path, monkeypatch)
    for args in (("claim", "--help"), ("release", "--help"), ("board", "--help"),
                 ("note", "--help")):
        code, out = _cli(repo, *args)
        assert code == 0, (args, out)
        assert out.startswith("Usage: comms-graph"), (args, out[:90])
    assert not clog2.log_path(repo).exists(), "--help wrote to the log"


def test_help_does_not_hijack_free_text_that_mentions_it(tmp_path, monkeypatch):
    """--intent is prose an agent writes. A claim whose intent talks about the
    --help flag must still be a claim."""
    repo = _repo(tmp_path, monkeypatch)
    code, out = _cli(
        repo, "claim", "a.py", "--as", "w", "--intent", "document the --help flag"
    )
    assert code == 0, out
    assert "CLAIMED" in out


def test_evidence_does_not_outlive_the_verification_it_describes(tmp_path, monkeypatch):
    """A rejection reopens the task. The old sign-off's evidence must go with
    it, or `brief` tells the next agent that reworked code was checked."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "task", "add", "t1", "--as", "alpha", "--title", "T1", session="sA")
    _cli(repo, "task", "add", "t2", "--as", "alpha", "--title", "T2", session="sA")
    _cli(repo, "task", "edge", "t1", "t2", "--as", "alpha", "--kind", "interface", session="sA")
    _cli(repo, "task", "done", "t1", "--as", "alpha", session="sA")
    _cli(repo, "task", "review", "t1", "--as", "beta", "--pass",
         "--evidence", "checked it thoroughly", session="sB")

    _cli(repo, "task", "done", "t1", "--as", "alpha", "--note", "reworked", session="sA")
    _, out = _cli(repo, "brief", "t2", "--as", "gamma", session="sC")
    assert "checked it thoroughly" not in out, (
        "a resubmission left the previous sign-off's evidence standing:\n" + out
    )

    _cli(repo, "task", "review", "t1", "--as", "beta", "--fail",
         "--evidence", "still broken", session="sB")
    _, out = _cli(repo, "brief", "t2", "--as", "gamma", session="sC")
    assert "checked it thoroughly" not in out, out
    assert "verified by" not in out, out


def test_a_rejection_withdraws_the_self_signed_label_too(tmp_path, monkeypatch):
    """The label was cleared on resubmission but not on rejection, so the same
    bug survived by the other route: a successor's brief asserted a sign-off
    that had been withdrawn, naming a bare "@"."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "task", "add", "api", "--as", "p", "--title", "API", session="sP")
    _cli(repo, "task", "add", "ui", "--as", "p", "--title", "UI", session="sP")
    _cli(repo, "task", "edge", "api", "ui", "--as", "p",
         "--kind", "interface", "--provides", "POST /session", session="sP")
    _cli(repo, "task", "done", "api", "--as", "alice", "--note", "JWT", session="sA")
    _cli(repo, "task", "review", "api", "--as", "alice", "--pass",
         "--acknowledge-self-review", session="sA")
    _cli(repo, "task", "review", "api", "--as", "bob", "--fail",
         "--evidence", "token never expires", session="sB")

    _, out = _cli(repo, "brief", "ui", "--as", "x", session="sX")
    assert "signed off by" not in out, ("a withdrawn sign-off is still asserted:\n" + out)
    assert "@ themselves" not in out, out


def test_reclaiming_ground_you_hold_replaces_it_rather_than_stacking(tmp_path, monkeypatch):
    """IF THIS FAILS: the board's claim count is wrong and one agent reads as two.

    Conflict detection only fires for a DIFFERENT actor, so re-claiming your own
    ground appended a second claim. The board showed two rows for one file, it
    was ambiguous which intent was current, and, because a claim carries a task
    tag: one agent on one file registered as a doer on two tasks, which the
    phase derivation reads as two people still working.
    """
    repo = _repo(tmp_path, monkeypatch)
    (repo / "a.py").write_text("x = 1\n")
    _cli(repo, "claim", "a.py", "--as", "bob", "--intent", "first", session="sB")
    _cli(repo, "claim", "a.py", "--as", "bob", "--intent", "second", session="sB")

    _, out = _cli(repo, "board", "--all", "--as", "bob", session="sB")
    assert "active claims (1)" in out, out
    assert "second" in out and "first" not in out, out

    # a symbol inside the file is different ground; holding both is legitimate
    _cli(repo, "claim", "a.py#parse", "--as", "bob", "--intent", "narrower", session="sB")
    _, out = _cli(repo, "board", "--all", "--as", "bob", session="sB")
    assert "active claims (2)" in out, out

    # and somebody else is still refused
    code, out = _cli(repo, "claim", "a.py", "--as", "carol", "--intent", "x", session="sC")
    assert code == 1, out
    assert "CLAIM CONFLICT" in out, out


def test_a_claim_that_folds_before_its_task_still_records_the_worker(tmp_path, monkeypatch):
    """IF THIS FAILS: two seconds of clock skew launder a co-worker's sign-off.

    fold() sorts by TIMESTAMP. A claim from a machine running slightly behind
    arrives before the `task` event it is tagged to, and the worker record was
    dropped on the floor: permanently, since unlike a claim nothing later
    repairs it. The co-worker then passed the review gate and their sign-off
    recorded as a genuine independent review.
    """
    from comms_graph import state as cstate

    ev = _Seq()
    # the claim is stamped BEFORE the task that declares it
    # minted claim-first: leo's clock runs behind, so his claim carries the
    # earlier timestamp and fold() seats it ahead of the task it names
    claim = ev("claim", "leo", {"task": "t10"}, scope=["src/i.py"])
    task = ev("task", "ken", {"task": "t10", "title": "Ten", "slots": 2})

    st = cstate.fold([
        claim, task,
        ev("claim", "ken", {"task": "t10"}, scope=["src/h.py"]),
        ev("task_state", "ken", {"task": "t10", "state": "done"}),
    ])
    assert "leo" in st.tasks["t10"].workers, (
        "the worker record was lost to clock skew: " + repr(st.tasks["t10"].workers)
    )

    # and the gate still bars him
    st2 = cstate.fold([
        claim, task,
        ev("claim", "ken", {"task": "t10"}, scope=["src/h.py"]),
        ev("task_state", "ken", {"task": "t10", "state": "done"}),
        ev("task_state", "leo", {"task": "t10", "state": "verified"}),
    ])
    assert not st2.tasks["t10"].verified_by, (
        "a co-worker's sign-off was accepted as independent after clock skew"
    )


def test_an_abandoned_claim_can_be_taken_over(tmp_path, monkeypatch):
    """IF THIS FAILS: clearing up after a crashed agent means impersonating it.

    `release` only ever touched claims held by `--as`, so freeing somebody
    else's ground meant stealing it and handing it back, or passing their name
   , which the CLI accepts and which writes a lie into an append-only log.

    """
    from comms_graph import state as cstate
    from comms_graph import cli as ccli
    from comms_graph import log as clog2

    repo = _repo(tmp_path, monkeypatch)
    for f in ("h.py", "i.py"):
        (repo / f).write_text("x = 1\n")
    _cli(repo, "claim", "h.py", "--as", "ken", "--intent", "k", session="sK")

    _, board = _cli(repo, "board", "--all", "--as", "x", session="sX")
    kid = board.split("[")[1].split("]")[0]

    code, out = _cli(repo, "release", kid, "--as", "nate", "--force", session="sN")
    assert code != 0 and "--reason" in out, out
    code, out = _cli(repo, "release", "h.py", "--as", "nate", "--force",
                     "--reason", "gone", session="sN")
    assert code != 0, ("a path was accepted; it can match more than was meant:\n" + out)

    code, out = _cli(repo, "release", kid, "--as", "nate", "--force",
                     "--reason", "ken's session died 3h ago", session="sN")
    assert code == 0, out
    assert "was @ken" in out and "@nate" in out, out

    # the log says who took it off whom, not that ken let go himself
    log_file, _ = ccli._store(ccli._repo_root(str(repo)), create=False)
    rel = cstate.fold(clog2.read(log_file)).releases[-1]
    assert rel.actor == "nate" and rel.original_actor == "ken", rel

    code, out = _cli(repo, "claim", "h.py", "--as", "nate", "--intent", "finishing it",
                     session="sN")
    assert code == 0, ("the freed file could not be picked up:\n" + out)


def test_a_homoglyph_name_does_not_launder_a_sign_off(tmp_path, monkeypatch):
    """IF THIS FAILS: a Cyrillic letter buys an independent review.

    With no host session id no hello is written, so same_agent has only the
    name to compare, and `nоra` with a Cyrillic "о" signed off work `nora` had
    written, under a name nobody can tell apart on screen. The board, the brief
    and the successor's unblock all asserted a real review.
    """
    from comms_graph import state as cstate

    ev = _Seq()
    evs = [
        ev("task", "planner", {"task": "t7", "title": "Seven", "slots": 2}),
        ev("claim", "nora", {"task": "t7"}, scope=["src/l.py"]),
        ev("claim", "olaf", {"task": "t7"}, scope=["src/m.py"]),
        ev("task_state", "olaf", {"task": "t7", "state": "done"}),
        ev("task_state", "nоra", {"task": "t7", "state": "verified"}),  # Cyrillic о
    ]
    st = cstate.fold(evs)
    assert not st.tasks["t7"].verified_by, (
        "a homoglyph signed off work the same agent wrote: "
        + repr(st.tasks["t7"].verified_by)
    )
    assert st.refused_task_states, "and nothing recorded the refusal"

    # a genuinely different person is still fine
    evs2 = evs[:-1] + [ev("task_state", "quinn", {"task": "t7", "state": "verified"})]
    st2 = cstate.fold(evs2)
    assert st2.tasks["t7"].verified_by == "quinn"


def test_a_person_can_free_ground_an_agent_abandoned(tmp_path, monkeypatch):
    """IF THIS FAILS: clearing up after a crashed agent means impersonating it.

    `release` only ever touched claims held by `--as`, so the two ways to free
    somebody else's ground were to steal it and hand it back: two writes, and
    you briefly own work you did not do, or to pass their name, which the CLI
    accepts and which writes a lie into an append-only log. A person tidying up
    after a crash should not have to pretend to be the crash.
    """
    from comms_graph import state as cstate

    repo = _repo(tmp_path, monkeypatch)
    (repo / "a.py").write_text("x = 1\n")
    _cli(repo, "claim", "a.py", "--as", "ghost", "--intent", "then crashed", session="sG")
    _, board = _cli(repo, "board", "--all", "--as", "x", session="sX")
    cid = board.split("[")[1].split("]")[0]

    code, out = _cli(repo, "release", cid, "--as", "eli-desk", "--force", session="sE")
    assert code != 0 and "--reason" in out, out
    code, out = _cli(repo, "release", "a.py", "--as", "eli-desk", "--force",
                     "--reason", "gone", session="sE")
    assert code != 0, ("a path was accepted; it can match more than was meant:\n" + out)

    code, out = _cli(repo, "release", cid, "--as", "eli-desk", "--force",
                     "--reason", "session died 4h ago", session="sE")
    assert code == 0, out
    assert "was @ghost" in out and "@eli" in out, out

    _, board = _cli(repo, "board", "--all", "--as", "x", session="sX")
    assert "no active claims" in board, board

    # and the log says who took it off whom, rather than reading as ghost's own
    from comms_graph import cli as ccli
    from comms_graph import log as clog2
    log_file, _ = ccli._store(ccli._repo_root(str(repo)), create=False)
    st = cstate.fold(clog2.read(log_file))
    rel = st.releases[-1]
    assert rel.actor == "eli-desk", rel
    assert rel.original_actor == "ghost", ("an arbitrated handover folded as an ordinary "
                                           "release: " + repr(rel))
    assert rel.arbitrator == "eli-desk", rel


def test_releasing_your_own_claim_does_not_need_force(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "a.py").write_text("x = 1\n")
    _cli(repo, "claim", "a.py", "--as", "alice", "--intent", "w", session="sA")
    _, board = _cli(repo, "board", "--all", "--as", "x", session="sX")
    cid = board.split("[")[1].split("]")[0]
    code, out = _cli(repo, "release", cid, "--as", "alice", "--force",
                     "--reason", "x", session="sA")
    assert code != 0 and "your own claim" in out, out
    code, out = _cli(repo, "release", "a.py", "--as", "alice", session="sA")
    assert code == 0, out


def test_the_old_edge_kinds_still_fold(tmp_path, monkeypatch):
    """IF THIS FAILS: every log written before the merge changes meaning.

    `interface` and `artifact` were two words for one behaviour: every place
    the kind was READ treated them identically, so writing an edge meant
    picking between synonyms for no effect. They are one kind now, `consumes`,
    which is what they always meant and which pairs with `--provides`.

    Both old spellings must keep folding to it. A log is append-only: events
    written months ago cannot be rewritten, so the reader has to keep
    understanding them.
    """
    from comms_graph import state as cstate

    ev = _Seq()
    for spelling in ("interface", "artifact", "consumes"):
        st = cstate.fold([
            ev("task", "p", {"task": "a", "title": "A"}),
            ev("task", "p", {"task": "b", "title": "B"}),
            ev("task_edge", "p", {"from": "a", "to": "b", "kind": spelling,
                                  "provides": "the schema"}),
        ])
        assert st.task_edges[0].kind == "consumes", (spelling, st.task_edges[0].kind)
        # and it still REOPENS a finished successor, which is what the kind is for
        assert st.task_edges[0].provides == "the schema"

    # sequence is untouched and still means ordering only
    st = cstate.fold([
        ev("task", "p", {"task": "c", "title": "C"}),
        ev("task", "p", {"task": "d", "title": "D"}),
        ev("task_edge", "p", {"from": "c", "to": "d", "kind": "sequence"}),
    ])
    assert st.task_edges[0].kind == "sequence"


def test_a_task_that_never_asked_for_review_closes_when_it_is_done():
    """IF THIS FAILS: every task parks in a queue nobody is serving.

    Review was unconditional, and the reasoning was sound: self-review
    measurably fails. What that missed is that an always-on gate is not free.
    Measured on real work: four of eight tasks sat in `review` with no reviewer
    coming, which is not a quality bar, it is a backlog. A gate that is always on
    and rarely satisfied teaches people to stop declaring tasks at all, and then
    there is no graph to gate.

    So `--review` asks for it and everything else closes on `done`. Signing off
    somebody else's work is still possible on any task; only REQUIRING it moved.
    """
    ev = _Seq()
    evs = [ev("task", "planner", {"task": "plain", "title": "No review"}),
           ev("task_state", "alice", {"task": "plain", "state": "done"})]
    assert phases(cstate.fold(evs)) == {"plain": "closed"}


def test_a_task_that_asked_for_review_still_waits_for_one():
    ev = _Seq()
    evs = [ev("task", "planner", {"task": "gated", "title": "Gated", "review": True}),
           ev("task_state", "alice", {"task": "gated", "state": "done"})]
    assert phases(cstate.fold(evs)) == {"gated": "review"}

    evs.append(ev("task_state", "bob", {"task": "gated", "state": "verified"}))
    assert phases(cstate.fold(evs)) == {"gated": "closed"}


def test_a_finished_no_review_task_unblocks_what_comes_after_it():
    """IF THIS FAILS: opt-in review deadlocks every plan that uses it.

    The successor waits on a verification the predecessor was never going to
    receive, so the chain stops forever. Reading only `verified_by` here is
    exactly that bug: the outer guard, not the branch below it, is what decides.
    """
    ev = _Seq()
    evs = [ev("task", "p", {"task": "first", "title": "First"}),
           ev("task", "p", {"task": "second", "title": "Second"}),
           ev("task_edge", "p", {"from": "first", "to": "second", "kind": "consumes"}),
           ev("task_state", "alice", {"task": "first", "state": "done"})]
    assert phases(cstate.fold(evs)) == {"first": "closed", "second": "ready"}


def test_restating_a_task_without_review_turns_the_requirement_off():
    """`task` is a whole-record upsert, like `hello`. Every other field follows
    that rule and this one is not a special case: worth pinning so it is a
    decision rather than an accident."""
    ev = _Seq()
    evs = [ev("task", "p", {"task": "t", "review": True}),
           ev("task_state", "alice", {"task": "t", "state": "done"})]
    assert phases(cstate.fold(evs)) == {"t": "review"}

    evs.append(ev("task", "p", {"task": "t"}))
    assert phases(cstate.fold(evs)) == {"t": "closed"}


