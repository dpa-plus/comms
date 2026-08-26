"""Tasks meeting each other through the code map.

This is the join comms exists on top of graphify for. The log knows which files
a task touched, because claims carry `--task`; the map knows how files reach
each other. Apart, the task graph can only show edges somebody typed — and
measured on a real store, nobody ever typed one: eight tasks, zero task_edge
events, in a log with thousands of events.
"""

from __future__ import annotations

import pytest

from comms_graph import taskcode

nx = pytest.importorskip("networkx")


class _T:
    def __init__(self, tid):
        self.id = tid


def _graph():
    """a.py — b.py directly; hub.py touched by a.py and by d.py; c.py alone."""
    g = nx.Graph()
    for n in ("a.py", "b.py", "c.py", "d.py", "hub.py"):
        g.add_node(n, path=n)
    g.add_edge("a.py", "b.py")      # a task on a.py reaches a file a task on b.py owns
    g.add_edge("a.py", "hub.py")    # both reach the hub, neither owns it
    g.add_edge("d.py", "hub.py")
    return g


def _link(monkeypatch, files):
    # Resolution is graphify's job and has its own tests; here the question is
    # the JOIN, so scope->node is stubbed to keep the two failures apart.
    monkeypatch.setattr(taskcode, "_nodes_for",
                        lambda graph, scopes, root: {s for s in scopes if s in graph})
    tasks = {t: _T(t) for t in files}
    return taskcode.link(_graph(), tasks, files, root=None)


def test_a_task_whose_reach_contains_your_file_is_related(monkeypatch):
    """IF THIS FAILS: the graph shows only what somebody typed, which is nothing.

    Neither task declares anything about the other. They are related because a
    file one touched reaches a file the OTHER TOUCHED — a fact about the code,
    true whether or not anybody noticed.
    """
    out = _link(monkeypatch, {"task-a": ["a.py"], "task-b": ["b.py"]})
    assert [r["task"] for r in out["task-a"]["related"]] == ["task-b"]
    assert [r["task"] for r in out["task-b"]["related"]] == ["task-a"]
    # 2, not 1: the meeting has two ends and both are named. a.py is in
    # task-b's reach and b.py is in task-a's, so the places where these two
    # pieces of work touch are a.py and b.py. "shared" counts places, not edges.
    assert out["task-a"]["related"][0]["shared"] == 2


def test_merely_sharing_a_hub_is_not_a_relation(monkeypatch):
    """IF THIS FAILS: every task is related to every task.

    Both of these reach hub.py and neither owns it. Counting that as a meeting
    is the tempting generalisation and it is the one that destroys the feature:
    on a real codebase a handful of hub files are reachable from nearly
    everything, so the answer becomes "all of them" and stops carrying
    information. The rule is deliberately the narrower one — your files, in my
    reach — because a signal that fires constantly is the same as no signal.
    """
    out = _link(monkeypatch, {"task-a": ["a.py"], "task-d": ["d.py"]})
    assert [r["task"] for r in out["task-a"]["related"]] == []
    assert out["task-d"]["related"] == []


def test_a_task_on_unconnected_code_meets_nothing(monkeypatch):
    """"Meets nothing" has to be reachable, or every task looks connected."""
    out = _link(monkeypatch, {"task-a": ["a.py"], "task-c": ["c.py"]})
    assert out["task-c"]["related"] == []


def test_relations_are_symmetric_whichever_way_the_import_points(monkeypatch):
    """IF THIS FAILS: who gets told depends on which file happened to import the
    other, so one of the two agents is warned and the other is not.

    The same bug the claim-time contact check already had: `neighbors` on a
    directed graph yields successors only, so the holder of the callee — whose
    change breaks the caller — heard nothing.
    """
    out = _link(monkeypatch, {"task-a": ["a.py"], "task-b": ["b.py"]})
    a_sees = {r["task"] for r in out["task-a"]["related"]}
    b_sees = {r["task"] for r in out["task-b"]["related"]}
    assert a_sees == {"task-b"} and b_sees == {"task-a"}


def test_no_map_means_no_answer_rather_than_a_guess():
    """"No connections" and "no map" are different claims and only one of them
    is evidence. Returning an empty relation set for a missing map would report
    the second as the first."""
    assert taskcode.link(None, {"t": _T("t")}, {"t": ["a.py"]}, root=None) == {}


def test_a_task_with_no_tagged_files_is_simply_absent(monkeypatch):
    """An untagged task cannot be placed on the map at all. It must not claim to
    meet nothing — it is not that it has no neighbours, it is that nobody said
    where it lives."""
    out = _link(monkeypatch, {"task-a": ["a.py"], "orphan": []})
    assert "orphan" not in out


def test_your_own_is_relative_to_who_is_asking(monkeypatch):
    """IF THIS FAILS: reading a peer's task hides the rows you needed.

    "Your own earlier work" means "skip, you already know this". The first
    version compared the two TASKS' owners rather than asking who was calling —
    a different question that looks right only on your own task. Read a peer's
    brief and every one of THEIR tasks was marked yours, so the label hid what
    was worth reading and left your own tasks unlabelled at the top as though
    they were the strangers. That inverts the feature rather than annoying you.
    """
    monkeypatch.setattr(taskcode, "_nodes_for",
                        lambda graph, scopes, root: {s for s in scopes if s in graph})
    files = {"mine": ["a.py"], "theirs": ["b.py"]}
    actors = {"mine": {"alice"}, "theirs": {"bob"}}
    tasks = {t: _T(t) for t in files}

    # Alice asking: bob's task is not hers.
    out = taskcode.link(_graph(), tasks, files, None, actors, caller="alice")
    assert out["mine"]["related"][0]["same_actor"] is False

    # Bob asking about alice's task: his own work IS his.
    out = taskcode.link(_graph(), tasks, files, None, actors, caller="bob")
    assert out["mine"]["related"][0]["same_actor"] is True


def test_with_no_caller_nothing_is_labelled(monkeypatch):
    """Saying nothing is correct; guessing is what went wrong the first time."""
    monkeypatch.setattr(taskcode, "_nodes_for",
                        lambda graph, scopes, root: {s for s in scopes if s in graph})
    files = {"mine": ["a.py"], "theirs": ["b.py"]}
    out = taskcode.link(_graph(), {t: _T(t) for t in files}, files, None,
                        {"mine": {"alice"}, "theirs": {"bob"}}, caller="")
    assert all(r["same_actor"] is False for r in out["mine"]["related"])


def test_a_probe_task_is_not_a_neighbour(monkeypatch):
    """IF THIS FAILS: testing the tool pollutes the graph it produces.

    An agent made a task purely to reproduce a bug in comms — claim a file,
    append a line, stage, revert, release, zero real changes — and it then sat in
    the graph permanently as a three-file neighbour of that agent's real work,
    outranking a genuine one-file neighbour. That is a direct cost of the rule
    that every file-changing request gets a task, and it compounds: the harder
    people test, the more of it accumulates.

    A probe stays a task. It is claimed and auditable. It is simply not evidence
    about where the real work meets.
    """
    monkeypatch.setattr(taskcode, "_nodes_for",
                        lambda graph, scopes, root: {s for s in scopes if s in graph})

    class _P(_T):
        probe = True

    files = {"real": ["a.py"], "spike": ["b.py"]}
    tasks = {"real": _T("real"), "spike": _P("spike")}
    out = taskcode.link(_graph(), tasks, files, None)
    assert out["real"]["related"] == [], "a probe was counted as a neighbour"
    assert "spike" not in out, "a probe should not get a neighbourhood of its own"


def test_your_own_work_is_demoted_but_never_dropped(monkeypatch):
    """IF THIS FAILS: the row that makes this feature pay off disappears.

    Demoting the caller's own tasks and then taking the top N deleted them. The
    row deleted was precisely the one an agent reported as the win: it found a
    peer's unclaimed task through a file shared with its OWN earlier task.

    It also made discovery depend on which end you opened. `brief theirs` would
    list yours, `brief yours` would not list theirs — and the end you naturally
    open when investigating somebody else's work was the blind one.
    """
    monkeypatch.setattr(taskcode, "_nodes_for",
                        lambda graph, scopes, root: {s for s in scopes if s in graph})
    files = {"mine": ["a.py"], "theirs": ["b.py"]}
    actors = {"mine": {"alice"}, "theirs": {"bob"}}
    out = taskcode.link(_graph(), {t: _T(t) for t in files}, files, None,
                        actors, caller="bob")

    rel = out["mine"]["related"]
    assert [r["task"] for r in rel] == ["theirs"], "bob's own task vanished"
    assert rel[0]["same_actor"] is True, "it should be labelled, not removed"
