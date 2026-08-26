"""Where a task's work sits in the code map, and which tasks that connects.

WHY THIS EXISTS. comms already knew two things separately and joined neither:
the log knows which files a task touched, because claims carry ``--task``; the
code map knows how files reach each other. Keeping them apart meant the task
graph could only ever show dependencies somebody had typed in by hand — and
measured on a real store, nobody ever typed one: eight tasks, zero declared
edges, in a log with thousands of events. A graph whose edges depend on
bookkeeping nobody does is a list.

The join makes a task's neighbourhood derivable instead. Two tasks are RELATED
when a file one of them touched reaches a file the other touched, one hop in the
map. Nobody declares that, and it is true whether or not anybody noticed.

What this deliberately does NOT do is treat that as an ordering. A declared edge
says "this comes after that", which is a judgement; a code connection says "these
two pieces of work meet", which is a fact with no direction. Presenting the
second as the first would put made-up sequencing on the board, and the map is
measured at roughly a third to a half recall — silence from it is weak evidence,
so an inferred arrow would be confidently wrong a lot of the time.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised by the absence path in tests
    import networkx as nx
except Exception:  # pragma: no cover
    nx = None

from .resolve import resolve
from .scope import parse as parse_scope

#: One hop. Two would connect almost everything to almost everything on a real
#: codebase — the map is dense enough that a two-hop neighbourhood of a handful
#: of files covers most of a module, which says nothing anybody can act on.
_HOPS = 1


def _nodes_for(graph, scopes: list[str], root) -> set[str]:
    """Every graph node the given scopes land on."""
    found: set[str] = set()
    for text in scopes:
        try:
            res = resolve(graph, parse_scope(text), root)
        except Exception:
            continue
        if getattr(res, "miss_reason", None):
            continue
        for place in getattr(res, "places", []) or []:
            found.add(place.node_id)
    return found


def _files_of(graph, node_ids) -> set[str]:
    """The distinct source files a set of graph nodes belongs to.

    Counting NODES instead of files is what made the ranking useless. A hub
    module contributes one node per exported symbol, so two tasks that both
    touched `auftraege/schema.ts` scored 30 while a genuinely specific overlap of
    two components scored 4 — and 30 sorted first. "You both touched the schema"
    is true of nearly everything on that backend and carries no information; the
    4 was the collision that actually cost somebody an afternoon.
    """
    out = set()
    for nid in node_ids:
        data = graph.nodes.get(nid) or {}
        out.add(str(data.get("source_file") or data.get("path") or nid))
    return out


def link(graph, tasks: dict[str, Any], task_files: dict[str, list[str]], root,
         task_actors: dict[str, set[str]] | None = None) -> dict[str, dict]:
    """For each task: what its files reach, and which tasks that puts it next to.

    Returns ``{task_id: {"touches": int, "related": [{"task", "via", "shared"}]}}``.
    Absent map, absent answer — an empty dict rather than a guess, because "no
    connections" and "no map" are different claims and only one of them is
    evidence.
    """
    if graph is None or nx is None or not tasks:
        return {}

    own: dict[str, set[str]] = {}
    for tid in tasks:
        scopes = task_files.get(tid) or []
        if scopes:
            own[tid] = _nodes_for(graph, scopes, root)

    near: dict[str, set[str]] = {}
    for tid, ids in own.items():
        reach: set[str] = set()
        for nid in ids:
            if nid not in graph:
                continue
            for _ in range(_HOPS):
                for nb in nx.all_neighbors(graph, nid):
                    reach.add(nb)
        near[tid] = reach - ids

    out: dict[str, dict] = {}
    for tid, ids in own.items():
        related = []
        for other, other_ids in own.items():
            if other == tid or not other_ids:
                continue
            # Their code, inside my reach — or mine inside theirs. Either way the
            # two pieces of work meet, and which direction the import happens to
            # point is not a fact about who should go first.
            # Both ends of the meeting. Their files inside my reach, and mine
            # inside theirs — the union names every place the two touch, which
            # is what "shared places" means on the board. Taking one direction
            # only would make the count depend on which way an import happens to
            # point, and would report a different number to each of the two
            # agents involved.
            shared = (near.get(tid, set()) & other_ids) | (near.get(other, set()) & ids)
            if not shared:
                continue
            places = _files_of(graph, shared)
            mine = (task_actors or {}).get(tid) or set()
            theirs = (task_actors or {}).get(other) or set()
            related.append({
                "task": other,
                # Distinct FILES, which is what "places" means to a reader.
                "shared": len(places),
                "via": sorted(places)[:3],
                # Meeting your own earlier work is a mirror, not a warning. The
                # point of this is finding the person you have not talked to, and
                # on a task whose whole list was its author's own prior tasks the
                # useful rows were pushed off the bottom.
                "same_actor": bool(mine and theirs and mine <= theirs or
                                   theirs and mine and theirs <= mine),
            })
        # Somebody else's work first, then by how specific the overlap is.
        related.sort(key=lambda r: (r["same_actor"], -r["shared"], r["task"]))
        out[tid] = {"touches": len(near.get(tid, set())), "related": related}
    return out


def files_from_log(events, string_of) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    """Which files each task touched, and who touched them, out of the log.

    Read from claim EVENTS rather than from live claims, so a file stays on the
    task after it is released — a task that forgets its files the moment the work
    finishes answers "what did this touch" with "nothing".

    One implementation, used by both the board and `brief`. Two would drift, and
    the first version of `brief` referred to a helper that did not exist at all:
    wrapped in a try/except it printed nothing and said nothing, which is the
    worst of the three possible behaviours.
    """
    files: dict[str, list[str]] = {}
    actors: dict[str, set[str]] = {}
    for ev in events:
        if getattr(ev, "type", "") != "claim" or not getattr(ev, "scope", None):
            continue
        tag = string_of(ev.data, "task")
        if not tag:
            continue
        scope = ev.scope[0]
        if scope not in files.setdefault(tag, []):
            files[tag].append(scope)
        actors.setdefault(tag, set()).add(ev.actor)
    return files, actors
