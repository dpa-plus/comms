"""Turn "roughly where my job lands" into real places in the map.

A claim names a slice of the repository — a file, a line range, or a symbol. The
map knows about symbols. This module is the join, and deliberately the only place
that knows how to cross between the two.

WHY IMPLIED SPANS RATHER THAN REAL ONES. The map records where a symbol STARTS
(``source_location`` is ``L20``) and not where it ends. Recording the end would
mean editing every per-language extractor — there are dozens, they are the part of
the fork most likely to move upstream, and we would be rebasing that change
forever. Instead a symbol's span runs from its own start to just before the next
symbol's start in the same file. Measured on a real 82-file corpus the median gap
between consecutive symbols is 7 lines and the 90th percentile is 49, so the
approximation is close in the common case.

The approximation is honest here because everything downstream of it is ADVISORY.
Nothing this module returns blocks an edit. A slightly wide span produces one
extra "you might be near this" line, which costs a glance; it never wrongly stops
work.

WHY A MISS IS LOUD. A name that resolves to nothing looks exactly like a job with
no connections — quiet, and apparently fine. In testing, a quarter of names typed
from memory named something that does not exist. So an empty resolution carries
its own reason and is never an empty list a caller can mistake for "checked,
nothing there".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_LINE_RE = re.compile(r"^L(\d+)$")

#: Sentinel end for the last symbol in a file. Large enough to cover any real
#: file, finite so comparisons stay ordinary integers.
_EOF_LINE = 1_000_000_000


@dataclass(frozen=True)
class Placed:
    """One spot in the map a claim was found to touch."""

    node_id: str
    label: str
    source_file: str
    start_line: int | None
    #: How the claim reached this node. Kept because anything showing a warning
    #: should be able to say why it fired: "the whole file" is much weaker
    #: evidence than "this exact symbol".
    via: str  # "file" | "symbol" | "lines"


@dataclass
class Resolution:
    """What a scope turned into, including the reason it turned into nothing."""

    places: list[Placed] = field(default_factory=list)
    #: Set when the scope named something the map does not contain. This is the
    #: case that must never be silently read as "no connections".
    miss_reason: str | None = None

    def __bool__(self) -> bool:
        return bool(self.places)


def _node_line(data: dict) -> int | None:
    m = _LINE_RE.match(str(data.get("source_location") or ""))
    return int(m.group(1)) if m else None



def _same_symbol(label: str, want: str) -> bool:
    """Does a map label name the symbol a person typed?

    Case-sensitive, to agree with scope.py — but callable-blind. The map labels
    functions with their call parentheses (``charge()``), while a person claiming
    one writes ``path#charge``: nobody types the brackets. Matching the raw
    strings made every ordinary function claim miss, and a miss reads as "not in
    the map", which is exactly the false all-clear this module exists to prevent.
    """
    if not label:
        return False
    if label == want:
        return True
    strip = lambda s: s[:-2] if s.endswith("()") else s
    return strip(label) == strip(want)

def _norm(p: str) -> str:
    """Compare paths the way a person typed them, not the way they were indexed.

    The map stores whatever path the scan walked, which may carry a leading
    ``./``; a claim is written repo-relative. Comparing raw strings makes every
    claim miss, which then looks like "no connections" — the exact failure this
    module exists to make loud.
    """
    s = str(p or "").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def file_nodes(graph, source_file: str) -> list[tuple[str, dict, int | None]]:
    """Every node recorded against one file, ordered by where it starts."""
    want = _norm(source_file)
    out = []
    for nid, data in graph.nodes(data=True):
        if _norm(data.get("source_file") or "") != want:
            continue
        out.append((nid, data, _node_line(data)))
    out.sort(key=lambda t: (t[2] is None, t[2] or 0))
    return out


def implied_spans(graph, source_file: str) -> list[tuple[str, dict, int, int]]:
    """Symbols in a file with a start and an inferred end.

    One symbol's end is the line before the next begins; the last runs to the end
    of the file. Nodes with no recorded line (file-level and rationale nodes) are
    dropped — they have no span, and treating them as spanning the file would make
    every line-range claim match everything in it.
    """
    located = [(nid, d, ln) for nid, d, ln in file_nodes(graph, source_file) if ln is not None]
    spans: list[tuple[str, dict, int, int]] = []
    for i, (nid, data, start) in enumerate(located):
        nxt = located[i + 1][2] if i + 1 < len(located) else None
        end = (nxt - 1) if nxt is not None and nxt > start else _EOF_LINE
        spans.append((nid, data, start, end))
    return spans


def resolve(graph, scope, root: str | Path | None = None) -> Resolution:
    """Find the places in the map a claim scope touches.

    ``scope`` carries a ``path`` and an optional anchor: ``anchor_kind`` of
    ``"symbol"`` with a name, or ``"lines"`` with a ``(lo, hi)`` pair.
    """
    res = Resolution()
    if graph is None:
        res.miss_reason = "no map has been built for this project yet, run `graphify extract`"
        return res

    path = _norm(getattr(scope, "path", "") or "")
    if not path:
        res.miss_reason = "the claim named no path"
        return res

    in_file = file_nodes(graph, path)
    if not in_file:
        # Distinguish "the map has never seen this file" from "the file exists but
        # holds nothing indexed". They call for different advice, and conflating
        # them is how a typo gets mistaken for an isolated file.
        on_disk = (Path(root) / path).exists() if root else None
        if on_disk is False:
            res.miss_reason = f"no file named {path!r}, check the spelling"
        elif on_disk is True:
            res.miss_reason = (
                f"{path} is not in the map (unsupported language, ignored, or the map is stale)"
            )
        else:
            res.miss_reason = f"{path} is not in the map"
        return res

    # Read the anchor through scope.py's real shape rather than a guessed one.
    # An earlier draft looked for `scope.anchor_kind` and a bare `scope.anchor`,
    # which this Scope does not have — every anchored claim then fell through to
    # the whole-file branch and produced a WRONG answer with no error. Ask the
    # anchor what it is.
    a = getattr(scope, "anchor", None)
    kind = getattr(getattr(a, "kind", None), "value", None)
    anchor_kind = {"symbol": "symbol", "line": "lines"}.get(kind)
    if anchor_kind == "symbol":
        anchor = getattr(a, "symbol", "") or ""
    elif anchor_kind == "lines":
        anchor = (getattr(a, "line_start", 0), getattr(a, "line_end", 0))
    else:
        anchor = None

    if anchor_kind == "symbol" and anchor:
        # Case-SENSITIVE, to agree with scope.py. That module deliberately treats
        # `#Parse` and `#parse` as different anchors, so matching loosely here made
        # the two layers contradict each other about the same pair of claims: the
        # overlap check said they were different symbols while the map quietly
        # resolved them to the same one. In languages where case distinguishes
        # exported from private, they really are different symbols.
        want = str(anchor)
        hits = [
            Placed(nid, str(d.get("label") or nid), path, ln, "symbol")
            for nid, d, ln in in_file
            if _same_symbol(str(d.get("label") or ""), want)
            or _same_symbol(str(d.get("norm_label") or ""), want)
        ]
        if not hits:
            # A case-only near miss is worth naming, because it looks identical.
            close = sorted(
                {
                    lbl
                    for _, d, _ in in_file
                    if (lbl := str(d.get("label") or "")) and _same_symbol(lbl.lower(), want.lower())
                }
            )
            if close:
                res.miss_reason = (
                    f"{path} has no symbol named {anchor!r}, but it does have "
                    f"{close[0]!r}: names are matched exactly, including case"
                )
                return res
        if not hits:
            # Suggest real symbol names only. The map also holds "rationale" nodes
            # whose labels are whole sentences of prose about the code; listing
            # those produced a suggestion line hundreds of characters long that
            # named nothing you could actually claim, which is worse than no
            # suggestion at all. Short labels only, for the same reason.
            names = sorted(
                {
                    lbl
                    for _, d, _ in in_file
                    if (lbl := str(d.get("label") or "")) and len(lbl) <= 60
                    and d.get("file_type") != "rationale"
                }
            )
            shown = ", ".join(names[:6]) + (" …" if len(names) > 6 else "")
            res.miss_reason = (
                f"{path} has no symbol named {anchor!r}."
                + (f" It does have: {shown}" if names else " Nothing in it is claimable by name.")
            )
            return res
        res.places = hits
        return res

    if anchor_kind == "lines" and anchor:
        lo, hi = anchor
        hits = [
            Placed(nid, str(d.get("label") or nid), path, start, "lines")
            for nid, d, start, end in implied_spans(graph, path)
            if start <= hi and end >= lo
        ]
        if not hits:
            res.miss_reason = f"{path} has nothing indexed between lines {lo} and {hi}"
            return res
        res.places = hits
        return res

    res.places = [
        Placed(nid, str(d.get("label") or nid), path, ln, "file") for nid, d, ln in in_file
    ]
    return res


def resolve_all(graph, scopes: Iterable, root: str | Path | None = None):
    """Resolve several scopes, keeping each outcome attached to its scope."""
    return [(s, resolve(graph, s, root)) for s in scopes]
