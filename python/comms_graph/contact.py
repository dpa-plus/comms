"""The contact warning: who else is standing near the ground you just claimed.

This is the one thing here that measurement supports, and it is deliberately much
weaker than the feature it replaced.

WHAT WAS TRIED AND REJECTED. The original idea was that the map could derive
ORDER: if job A changes something job B leans on, A goes first. It was checked
against three months of real changes on two projects and was right about as often
as a coin flip: on one project the real work went the other way round most of the
time. Shipping that as a blocker would have had the board confidently telling
people to wait for no reason, and they would have stopped believing it within a
day. So there is no ordering here, and no blocking. See ``docs/COMMS.md``.

WHAT IS LEFT, AND WHY IT IS WORTH HAVING. You say what you are about to work on
and immediately find out (a) whether anyone else is standing on the same ground
and (b) whether the code you named is connected to code somebody else named. That
pays back to the person typing, at the moment they type, with no plan to fill in
and no permission to wait for.

TWO HONEST LIMITS, BOTH MEASURED, BOTH STATED IN THE OUTPUT:

  * Between a third and a half of file pairs that really do get changed together
    are invisible here. **No warning does not mean independent**, and nothing in
    this module may imply that it does.
  * Of the pairs the map does flag, well under half turn out to be things that
    really get changed together. So this is a prompt to look, never a verdict.

Both of those are why the wording is "worth a look" and never "conflict", and why
this returns advice rather than a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import networkx as nx

#: One hop only. Two hops was tested and turns a busy shared utility into a mesh
#: where everything touches everything, which is the same as saying nothing.
_HOPS = 1

#: Relations worth walking. These are the ones that mean "this code leans on that
#: code". Deliberately excludes ``rationale_for`` and ``contains``: the first is
#: commentary, the second is structural (a file "contains" its symbols) and would
#: make every symbol in a file contact every other one.
_RELATIONS = frozenset(
    {"calls", "indirect_call", "references", "imports", "imports_from", "uses",
     "inherits", "extends", "implements", "dynamic_import", "re_exports", "method"}
)

#: A job naming more than this many SCOPES stops producing contact advice.
#: Measured: claim everything you touch and the board turns to mush.
#:
#: This counts scopes a person named, NOT the graph nodes they resolve to. An
#: earlier version counted resolved nodes, which meant an ordinary whole-file
#: claim: the natural unit: resolved to one node per symbol, blew past the cap,
#: and got told to "narrow it (a file, a symbol, or a line range)". Telling
#: somebody to narrow a file claim to a file made the feature bail out on nearly
#: every real invocation.
MAX_SCOPES = 3


@dataclass(frozen=True)
class Touch:
    """One reason to look at somebody else's work."""

    other_actor: str
    other_scope: str
    #: "same" when both claims land on the very same node: the strong case.
    #: "near" when they are one connection apart: the weak, advisory case.
    kind: str
    my_label: str
    their_label: str
    relation: str = ""
    #: EXTRACTED means the connection was read straight out of the source;
    #: INFERRED means graphify worked it out. Shown because a guessed connection
    #: deserves less of your attention than one that is literally written down.
    confidence: str = ""


@dataclass
class ContactReport:
    touches: list[Touch] = field(default_factory=list)
    #: Set when no advice could be produced, with the reason. Never left empty
    #: alongside an empty touch list: silence and "nothing found" must be
    #: distinguishable.
    note: str | None = None
    #: Other people's claims the map could not place. Reported, never dropped:
    #: an unplaceable claim is usually a brand-new file, which is precisely when
    #: two agents are most likely to collide.
    unplaced: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.touches)


def _edge_relation(graph, a: str, b: str) -> tuple[str, str]:
    """Relation and confidence between two nodes, in whichever direction exists.

    The walk is UNDIRECTED on purpose. Direction is what the ordering experiment
    needed and direction is what failed to predict anything; for "are these two
    pieces of work near each other" it does not matter who points at whom.
    """
    best = ("", "")
    for x, y in ((a, b), (b, a)):
        if not graph.has_edge(x, y):
            continue
        data = graph.get_edge_data(x, y) or {}
        # A MultiGraph hands back {key: payload}. Taking the first payload let a
        # structural `contains` edge hide a parallel `calls` edge and report the
        # pair as no contact at all, so consider every parallel edge and keep the
        # most informative one.
        payloads = (
            list(data.values())
            if data and "relation" not in data and all(isinstance(v, dict) for v in data.values())
            else [data]
        )
        for pl in payloads:
            rel = str((pl or {}).get("relation") or "")
            conf = str((pl or {}).get("confidence") or "")
            if rel in _RELATIONS:
                return rel, conf
            if rel and not best[0]:
                best = (rel, conf)
    return best


def contact(
    graph,
    mine,
    others: Iterable[tuple[str, str, object]],
    *,
    scope_count: int = 1,
) -> ContactReport:
    """Advice about whose work is near yours.

    ``mine`` is a Resolution for the scope just claimed. ``others`` is an iterable
    of ``(actor, scope_string, Resolution)`` for every claim somebody else holds.
    ``scope_count`` is how many scopes this one job named: see MAX_SCOPES.
    """
    report = ContactReport()

    if graph is None:
        report.note = "no map for this project yet, so only exact overlaps are known. Run `graphify extract`"
        return report
    if getattr(mine, "miss_reason", None):
        # Loud, not silent. A name that matched nothing must never read as "clear".
        report.note = f"could not place your claim on the map: {mine.miss_reason}"
        return report
    if not mine.places:
        report.note = "your claim matched nothing in the map"
        return report
    if scope_count > MAX_SCOPES:
        report.note = (
            f"this job names {scope_count} separate scopes, more than the {MAX_SCOPES} this can "
            f"usefully reason about: split the job, or claim fewer places at once"
        )
        return report

    my_ids = {p.node_id: p for p in mine.places}
    # Neighbours of everything I named, one hop, along leaning relations only.
    my_near: dict[str, tuple[str, str, str]] = {}
    for nid, place in my_ids.items():
        if nid not in graph:
            continue
        # all_neighbors, not neighbors: on a directed graph `neighbors` yields
        # successors only, so whoever holds the CALLEE: the one whose change
        # breaks the caller: would be told "nobody else is on this", while the
        # caller's holder saw the touch. Who got warned depended on which of them
        # ran the check, which is the opposite of what "undirected" promises.
        for nb in nx.all_neighbors(graph, nid):
            if nb in my_ids:
                continue
            rel, conf = _edge_relation(graph, nid, nb)
            if rel and rel not in _RELATIONS:
                continue
            my_near.setdefault(nb, (place.label, rel, conf))

    unplaced: list[str] = []
    for actor, scope_str, res in others:
        if not res or getattr(res, "miss_reason", None):
            # Somebody else's claim the map could not place is NOT nothing. The
            # commonest case is a file created since the last extract: exactly
            # what a parallel agent does, and silently skipping it meant the
            # all-clear printed over live work. The loud-miss rule has to apply
            # to other people's claims too, not only your own.
            unplaced.append(f"@{actor} holds {scope_str}, which is not in the map")
            continue
        for p in res.places:
            if p.node_id in my_ids:
                # Only call it the same thing when both sides named it directly.
                # Two line ranges that do not overlap can still land inside one
                # symbol's INFERRED span, and reporting that as "claimed this
                # exact thing" is false on its face, and contradicts the
                # blocking layer, which correctly does not block it.
                exact = my_ids[p.node_id].via != "lines" and p.via != "lines"
                report.touches.append(
                    Touch(actor, scope_str, "same" if exact else "near",
                          my_ids[p.node_id].label, p.label,
                          "" if exact else "same region")
                )
            elif p.node_id in my_near:
                my_label, rel, conf = my_near[p.node_id]
                report.touches.append(
                    Touch(actor, scope_str, "near", my_label, p.label, rel, conf)
                )

    # Strong cases first; a shared node is worth acting on, a neighbour is worth a
    # glance, and mixing them buries the one that matters.
    report.touches.sort(key=lambda t: (t.kind != "same", t.other_actor, t.their_label))
    report.unplaced = unplaced
    if not report.touches:
        report.note = (
            "nobody else is on this or next to it, though the map misses roughly a third of "
            "real couplings, so this is not a guarantee"
        )
    return report


def render(report: ContactReport) -> str:
    """One block of text for an agent or a person. Advice, never a verdict."""
    lines: list[str] = []
    if report.note and not report.touches:
        # Unplaced holders FIRST, and they cancel the all-clear.
        #
        # This used to print "nobody else is on this or next to it" and then,
        # underneath, the two lines admitting that two of the live claims were
        # never actually checked. A reader who stops at the verdict, which is
        # what a verdict is for: walks away believing the ground is clear when
        # nobody established that. Worse, unplaceable claims are usually on
        # brand-new files, which is exactly when two agents are most likely to
        # be standing on the same spot.
        #
        # So the admission goes above the summary, and the summary stops
        # claiming something it does not know.
        for u in report.unplaced:
            lines.append(f"  NOT ON THE MAP: {u}; rebuild with `graphify extract` to see it")
        if report.unplaced:
            lines.append(
                "  Cannot say whether anyone is near you: "
                f"{len(report.unplaced)} live claim(s) above could not be placed on the map."
            )
        else:
            lines.append(f"  {report.note}")
        return "\n".join(lines)
    same = [t for t in report.touches if t.kind == "same"]
    near = [t for t in report.touches if t.kind == "near"]
    if same:
        lines.append("  SAME GROUND: somebody else has claimed this exact thing:")
        for t in same:
            lines.append(f"    @{t.other_actor} holds {t.other_scope}  ({t.their_label})")
    if near:
        lines.append("  NEARBY: worth a look before you start, not a conflict:")
        for t in near:
            via = f" [{t.relation}]" if t.relation else ""
            conf = f" {t.confidence.lower()}" if t.confidence else ""
            lines.append(
                f"    {t.my_label} -{via}{conf}-> {t.their_label}, held by @{t.other_actor} ({t.other_scope})"
            )
        lines.append("    (the map flags more pairs than really change together; and it misses some)")
    for u in report.unplaced:
        lines.append(f"  NOT ON THE MAP: {u}; rebuild with `graphify extract` to see it")
    return "\n".join(lines)
