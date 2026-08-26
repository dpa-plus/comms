"""Put the people onto the code map.

``graphify`` already draws the map. This module draws the map **exactly as it is
drawn today** and then adds one layer on top of it: where somebody currently is.

WHAT THE MARKS MEAN, AND WHAT THEY DO NOT MEAN. A solid ring means *somebody is
standing here*. A faint ring means *this is one connection away from somewhere
somebody is standing*. That is the whole vocabulary. There is no "blocked", no
"go first", no "wait for", no number that ranks one piece of work above another.

That restriction is not politeness, it is the measured result. Deriving ORDER
from the map — A changes something B leans on, therefore A first — was checked
against three months of real changes on two projects and was right about as often
as a coin flip; on one project the real work went the other way round most of the
time. See ``docs/COMMS.md``. A picture is far more persuasive than a line of
text, so a picture that implied order would be the most effective way possible to
ship a coin flip as a fact. Hence: rings, never arrows the reader could read as
sequence, and never a verdict.

HOW IT STAYS THE SAME PICTURE. It calls the real exporter
(:func:`graphify.exporters.html.to_html`) and then appends one ``<style>`` and one
``<script>`` before ``</body>``. Nothing in the base document is edited. Community
colouring, physics, the legend, the learning overlay, the vis-network options and
the CDN ``<script>`` tag are whatever the exporter produced — including its
warts, which are deliberately inherited rather than forked (see NETWORK below).
If the exporter changes, this changes with it.

NETWORK. The exporter loads vis-network from unpkg.com with an SRI hash, and that
tag is inherited unchanged, so **the produced file is not offline-capable: the
drawing needs the network the first time a browser opens it.** Verified, not
assumed — with the library unreachable the map area is a blank dark rectangle.

What is NOT inherited is failing silently. The exporter's script declares
``network`` and ``nodesDS`` as top-level ``const``s; when the library is missing
that script throws before initialising them, and they stay in the temporal dead
zone where even ``typeof network`` raises. A naive overlay is killed by the same
ReferenceError and vanishes with the picture. Everything here reaches those
bindings through a guarded accessor instead, so with no network you still get the
roster — who holds what, and why — plus a visible line saying the map did not
draw. A blank page beside a silent panel would read as "nothing is going on",
which is the one thing this must never say by accident.

THE EMPTY CASES ARE REAL ANSWERS, NOT ERRORS. "No map has been built yet" and "a
map, but nobody is holding anything" are both ordinary, common states. Each gets a
page that says which one it is. A page that silently renders nothing would look
exactly like a page saying "all clear", and "all clear" is the one thing this must
never say by accident.
"""

from __future__ import annotations

import html as _html
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from comms_graph import contact as _contact
from comms_graph import log as _log
from comms_graph import resolve as _resolve
from comms_graph import state as _state

#: Default name of the rendered file. Deliberately NOT ``graph.html``: the plain
#: map is a build artifact that `graphify extract` rewrites, and clobbering it
#: with a coordination view would make the coordination layer vanish on the next
#: rebuild while looking like it was still there.
DEFAULT_FILENAME = "graph.comms.html"

#: How many of a claim's places get their own text caption. Past this, the claim
#: is marked on every node it covers but LABELLED once, so a whole-file claim
#: does not stamp its owner's name onto forty rings.
#:
#: This is a legibility rule about text, and nothing else. It used to also
#: suppress the "nearby" ring — see MAX_NEAR below for why that was wrong.
MAX_LABELLED_PLACES = 3

#: Cap on how many faint rings ONE claim may draw. A whole-file claim in a busy
#: module legitimately touches a lot of code, and drawing every neighbour turns
#: the picture to mush.
#:
#: WHAT WAS WRONG BEFORE. This module suppressed the nearby ring entirely for any
#: claim covering more than three PLACES — three graph nodes. A whole-file claim
#: is the natural unit and resolves to one node per symbol, so every ordinary
#: claim blew past three instantly: the four claims in a real run covered 24, 37,
#: 11 and 9 places and every one of them printed "nearby not drawn". The faint
#: ring never appeared for anything, so half the picture's vocabulary was dead
#: while the legend still explained it. contact.py had the identical bug and was
#: fixed by counting SCOPES rather than resolved nodes; the constant was renamed
#: there, and the ``getattr(_contact, "MAX_PLACES", 3)`` here silently fell back
#: to the default instead of failing, which is how it survived the fix.
#:
#: Truncating is the honest version of the same instinct: draw the neighbours,
#: stop at a readable number, and SAY that it stopped.
MAX_NEAR = 60

_HELD_COLOR = "#ffffff"
_NEAR_COLOR = "rgba(255,255,255,0.32)"


# --------------------------------------------------------------------------
# What we found
# --------------------------------------------------------------------------


@dataclass
class HeldNode:
    """One node in the map that somebody is currently standing on."""

    node_id: str
    label: str
    actor: str
    scope: str
    why: str = ""
    via: str = ""  # "file" | "symbol" | "lines" — how the claim reached it
    #: Whether the who/why tag is drawn beside this ring. A whole-file claim
    #: lands on every symbol in the file, and repeating one person's name fifty
    #: times over a tight cluster is a smear, not information — so a big claim
    #: rings every node it holds and captions exactly one of them.
    tag: bool = True
    #: For that one captioned node: how many other places the same claim holds.
    also: int = 0


@dataclass
class NearNode:
    """One node one connection away from somewhere somebody is standing."""

    node_id: str
    label: str
    actor: str
    from_label: str
    relation: str = ""
    confidence: str = ""


@dataclass
class ClaimRow:
    """One held claim, as the sidebar lists it."""

    actor: str
    scope: str
    why: str = ""
    place_count: int = 0
    node_ids: list[str] = field(default_factory=list)
    #: Set when the claim names something the map does not contain. Shown out
    #: loud: a claim that matched nothing looks identical to a quiet one.
    miss_reason: str | None = None
    #: Set when the claimed file changed on disk after the map was built.
    stale_note: str | None = None
    #: Set when "nearby" was not computed for this claim, and why.
    near_note: str | None = None


@dataclass
class ViewData:
    """Everything the overlay needs, already reduced to plain data."""

    held: dict[str, HeldNode] = field(default_factory=dict)
    near: dict[str, NearNode] = field(default_factory=dict)
    claims: list[ClaimRow] = field(default_factory=list)
    #: Honest state of the whole picture, always set, always shown.
    headline: str = ""
    notes: list[str] = field(default_factory=list)
    generated: str = ""
    #: True when the log could not be READ. An empty claim list then means "we
    #: do not know", not "nobody" — and those two must never render alike,
    #: because one of them is the most reassuring sentence this tool prints.
    blind: bool = False


@dataclass
class ViewResult:
    output_path: Path
    #: "overlay" (map + marks), "quiet" (map, nobody holding), "no-map".
    kind: str = "overlay"
    data: ViewData = field(default_factory=ViewData)


# --------------------------------------------------------------------------
# Loading the same inputs the rest of graphify loads
# --------------------------------------------------------------------------


def graph_path(root: Path, explicit: str | None = None) -> Path:
    """Where the map lives. Same rule the comms CLI uses."""
    if explicit:
        return Path(explicit).expanduser()
    try:
        from graphify.paths import GRAPHIFY_OUT as out
    except Exception:  # pragma: no cover - paths module is always present
        out = "graphify-out"
    name = Path(out)
    return (name if name.is_absolute() else root / name) / "graph.json"


class _MapUnreadable(Exception):
    """graph.json is there and cannot be parsed. Distinct from it being absent."""


def load_graph(path: Path):
    """The map as the exporter wants it: DIRECTED, straight off graph.json.

    ``contact.py`` gets an undirected view instead, because direction is exactly
    what the abandoned ordering experiment needed and exactly what failed to
    predict anything. The picture keeps direction only because the picture has
    always drawn arrowheads for ``calls``; those arrowheads are the *code's*
    direction, not a running order, and nothing here adds a new one.
    """
    if not Path(path).is_file():
        return None
    try:
        from graphify.affected import load_graph as _lg

        return _lg(Path(path))
    except Exception as exc:
        # A map that exists and will not load is NOT the same as no map, and
        # saying "no map has been built yet — run graphify extract" sends
        # somebody to re-run a command that already produced the broken file.
        # Recorded here and reported by the caller.
        raise _MapUnreadable(str(exc)) from exc


def communities_of(graph) -> tuple[dict[int, list[str]], dict[int, str]]:
    """Rebuild the community split and its labels from the graph itself.

    Same reconstruction ``serve.py`` does. Reading them back off the nodes rather
    than re-clustering is what guarantees the colours match ``graph.html``
    exactly — re-running the clustering could legitimately land on a different
    (equally valid) split and repaint the whole map.
    """
    communities: dict[int, list[str]] = {}
    labels: dict[int, str] = {}
    for node_id, data in graph.nodes(data=True):
        cid = data.get("community")
        if cid is None:
            continue
        cid = int(cid)
        communities.setdefault(cid, []).append(node_id)
        name = data.get("community_name")
        if name and cid not in labels:
            labels[cid] = str(name)
    return communities, labels


class _LogUnreadable(Exception):
    """The log is there and will not parse. Distinct from there being no log."""


def read_state(root: Path, log_file: str | Path | None = None) -> _state.State:
    """Fold the append-only log into current state. A missing log is empty state."""
    path = Path(log_file) if log_file else _log.log_path(root)
    try:
        events = _log.read(path)
    except FileNotFoundError:
        # Nobody has coordinated in this repo yet. Genuinely empty state, and
        # "nobody is holding anything" is a true statement about it.
        return _state.State()
    except Exception as exc:
        # A log that will NOT PARSE is a different thing entirely, and returning
        # empty state made the picture assert its most reassuring sentence —
        # "Nobody is holding anything right now" — out of data it had just
        # failed to read. An empty log and an unreadable one must never render
        # the same way.
        raise _LogUnreadable(str(exc)) from exc
    return _state.fold(events)


# --------------------------------------------------------------------------
# Turning claims into marks
# --------------------------------------------------------------------------


def _why(claim: Any) -> str:
    return str(getattr(claim, "intent", "") or getattr(claim, "task", "") or "")


def _stale_note(root: Path, map_path: Path, rel_path: str) -> str | None:
    """Say so when the claimed file moved on after the map was read.

    COMMS.md: a claim pointing at code that changed since the map was built is
    reported as out of date rather than as truth. This is the cheapest honest
    version of that — file mtime against graph.json mtime.
    """
    try:
        map_mtime = map_path.stat().st_mtime
        f = (root / rel_path)
        if not f.is_file():
            return None
        if f.stat().st_mtime > map_mtime:
            return "this file changed after the map was built: the marks on it may be out of date"
    except OSError:
        return None
    return None


def collect(graph, state, root: Path, map_path: Path) -> ViewData:
    """Reduce (map, log state) to the marks and the sidebar rows."""
    data = ViewData(generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    claims = list(getattr(state, "claims", {}).values())
    # Newest first, so the panel reads as "what is happening now".
    claims.sort(key=lambda c: getattr(c, "ts", None) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True)

    if graph is None:
        # No map is not the same as nobody working. The claims live in the log,
        # which does not need the map at all, and an early return here once made
        # the no-map page print "nobody is holding anything" under a heading that
        # promised what the log knows — the single most dangerous sentence this
        # tool can get wrong, because it reads as all-clear.
        for claim in claims:
            scope = getattr(claim, "scope", None)
            data.claims.append(
                ClaimRow(
                    actor=str(getattr(claim, "actor", "") or "?"),
                    scope=str(scope) if scope is not None else "",
                    why=_why(claim),
                )
            )
        data.headline = "No map has been built for this project yet."
        return data

    undirected = graph
    try:
        undirected = graph.to_undirected(as_view=True)
    except Exception:
        pass

    relations = getattr(_contact, "_RELATIONS", frozenset())
    edge_relation = getattr(_contact, "_edge_relation", None)

    for claim in claims:
        scope = getattr(claim, "scope", None)
        actor = str(getattr(claim, "actor", "") or "?")
        scope_str = str(scope) if scope is not None else ""
        row = ClaimRow(actor=actor, scope=scope_str, why=_why(claim))

        res = _resolve.resolve(graph, scope, root) if scope is not None else None
        if res is None or getattr(res, "miss_reason", None):
            row.miss_reason = getattr(res, "miss_reason", "the claim named no scope")
            data.claims.append(row)
            continue

        row.place_count = len(res.places)
        row.node_ids = [p.node_id for p in res.places]
        row.stale_note = _stale_note(root, map_path, getattr(scope, "path", "") or "")

        # One caption per big claim: the first place not already spoken for, so
        # the caption does not land on a node another claim already names.
        wide = len(res.places) > MAX_LABELLED_PLACES
        caption_id = None
        if wide:
            free = [p.node_id for p in res.places if p.node_id not in data.held]
            caption_id = free[0] if free else res.places[0].node_id

        for p in res.places:
            # First claim on a node wins the label; a second one is still shown
            # in the panel, so nothing is hidden — only the ring text is one name.
            data.held.setdefault(
                p.node_id,
                HeldNode(
                    p.node_id, p.label, actor, scope_str, row.why, p.via,
                    tag=(not wide) or p.node_id == caption_id,
                    also=(len(res.places) - 1) if (wide and p.node_id == caption_id) else 0,
                ),
            )


        drawn_near = 0
        truncated = False
        for p in res.places:
            if p.node_id not in undirected:
                continue
            for nb in undirected.neighbors(p.node_id):
                if drawn_near >= MAX_NEAR:
                    truncated = True
                    break
                if nb == p.node_id:
                    continue
                rel = conf = ""
                if edge_relation is not None:
                    rel, conf = edge_relation(undirected, p.node_id, nb)
                if rel and relations and rel not in relations:
                    continue
                nb_label = str((undirected.nodes[nb] or {}).get("label") or nb)
                if nb not in data.near:
                    data.near[nb] = NearNode(nb, nb_label, actor, p.label, rel, conf)
                    drawn_near += 1
            if drawn_near >= MAX_NEAR:
                truncated = True
                break
        if truncated:
            row.near_note = (
                f"only the first {MAX_NEAR} neighbours are ringed for this claim; "
                f"it covers {len(res.places)} places and reaches further than the picture can show"
            )

        data.claims.append(row)

    # A node somebody is standing on is not also "nearby".
    for nid in list(data.near):
        if nid in data.held:
            del data.near[nid]

    # Count only people the picture can actually SHOW. Counting every claimant
    # made the headline say "1 place(s) held by 3 people" when two of the three
    # had claimed files the map has never seen — so the sentence promised a
    # picture of three agents and drew one, and the reader had no way to tell
    # which. The unplaced ones are not dropped; they are counted separately and
    # listed underneath, because an unplaceable claim is usually a brand-new
    # file, which is exactly when two agents are most likely to collide.
    shown = sorted({h.actor for h in data.held.values()})
    missed = [c for c in data.claims if c.miss_reason]
    if not data.claims:
        data.headline = "Nobody is holding anything right now."
    elif not data.held:
        data.headline = (
            f"{len(data.claims)} claim(s) held, none of which could be placed on this map."
        )
    else:
        data.headline = (
            f"{len(data.held)} place(s) held by {len(shown)} "
            f"{'person' if len(shown) == 1 else 'people'}; "
            f"{len(data.near)} more within one connection."
        )
        if missed:
            data.headline += (
                f" {len(missed)} further claim(s) could not be placed and are not "
                "drawn: listed below."
            )

    data.notes = [
        "A solid ring means somebody is standing here. A faint ring means one connection away.",
        "Neither is a verdict and neither implies an order: the map cannot tell you what to do first.",
        "The map misses roughly a third of the file pairs that really do change together, so an unmarked node is not a guarantee that it is free.",
        "Of the pairs the map does flag, well under half really change together. A faint ring is a prompt to look.",
    ]
    return data


# --------------------------------------------------------------------------
# The overlay itself
# --------------------------------------------------------------------------


def _js(obj) -> str:
    """JSON safe to sit inside a <script> element.

    Escaping only ``</`` is not enough, and the gap is reachable from ordinary
    agent input: an intent containing ``<!--<script`` opens an HTML comment that
    swallows the rest of the element, so the coordination overlay silently
    vanished while the map itself still drew. Nothing errored — the claims were
    simply not there, which is the worst possible way for this to fail.

    ``<`` is escaped wholesale instead. Inside a JSON string ``\u003c`` is the
    same character to any parser, and no sequence the HTML tokeniser cares about
    can survive: not ``</script``, not ``<!--``, not ``<![CDATA[``.
    ``\u2028``/``\u2029`` go too — they are line terminators in JavaScript but
    not in JSON, so a raw one ends the statement early.
    """
    text = json.dumps(obj, ensure_ascii=False)
    return (text.replace("<", "\\u003c")
                .replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))


def _payload(data: ViewData) -> dict:
    return {
        "held": [
            {"id": h.node_id, "label": h.label, "actor": h.actor, "scope": h.scope,
             "why": h.why, "via": h.via, "tag": h.tag, "also": h.also}
            for h in data.held.values()
        ],
        "near": [
            {"id": n.node_id, "label": n.label, "actor": n.actor, "from": n.from_label,
             "relation": n.relation, "confidence": n.confidence}
            for n in data.near.values()
        ],
        "claims": [
            {"actor": c.actor, "scope": c.scope, "why": c.why, "places": c.place_count,
             "ids": c.node_ids, "miss": c.miss_reason, "stale": c.stale_note,
             "near_note": c.near_note}
            for c in data.claims
        ],
        "headline": data.headline,
        "notes": data.notes,
        "blind": data.blind,
        "generated": data.generated,
    }


_OVERLAY_STYLE = """<style>
  /* Coordination overlay. Additive only: no rule here restyles a base element. */
  /* Capped and scrollable: a whole-file claim can list fifty places, and an
     uncapped panel would push the community legend off the bottom of the
     sidebar: quietly removing part of the picture that was already there. */
  #comms-panel { padding: 12px 14px; border-bottom: 1px solid #2a2a4e; background: #16162b;
                 max-height: 42vh; overflow-y: auto; flex-shrink: 0; }
  #comms-panel h3 { font-size: 13px; color: #aaa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
  #comms-headline { font-size: 12px; color: #e0e0e0; line-height: 1.5; margin-bottom: 10px; }
  .comms-claim { border-left: 3px solid rgba(255,255,255,0.85); padding: 4px 0 4px 8px; margin: 6px 0; font-size: 12px; line-height: 1.45; }
  .comms-claim.miss { border-left-color: #f59e0b; }
  .comms-actor { color: #ffffff; font-weight: 600; }
  .comms-scope { color: #9fb6d4; word-break: break-all; }
  .comms-why { color: #aaa; font-style: italic; }
  .comms-sub { color: #f59e0b; font-size: 11px; }
  .comms-quiet { color: #f0d264; font-size: 11px; }
  .comms-node { display: inline-block; margin: 2px 4px 0 0; padding: 1px 6px; border-radius: 3px;
                background: #24243f; color: #cfd8ff; cursor: pointer; font-size: 11px; }
  .comms-node:hover { background: #34345e; }
  .comms-more { display: inline-block; margin-top: 3px; color: #6d6d85; font-size: 11px; }
  #comms-key { margin-top: 10px; font-size: 11px; color: #888; line-height: 1.5; }
  #comms-key div { margin: 3px 0; }
  #comms-key .k { display: inline-block; width: 13px; height: 13px; border-radius: 50%;
                  margin-right: 6px; vertical-align: -2px; background: transparent; }
  #comms-limits { margin-top: 8px; font-size: 10.5px; color: #6d6d85; line-height: 1.5; }
  #comms-limits li { margin-left: 14px; }
  #comms-offline { margin-bottom: 10px; padding: 6px 8px; border-radius: 4px;
                   background: #3a2a12; color: #f0c274; font-size: 11px; line-height: 1.45; }
</style>"""


def _overlay_script(data: ViewData) -> str:
    payload = _js(_payload(data))
    return f"""<script>
// ---------------------------------------------------------------------------
// Coordination overlay. Runs AFTER the base script, touches nothing it built.
// Marks are drawn in an afterDrawing pass so every node keeps the exact colour,
// size, border and font the exporter gave it: the picture underneath is the
// picture graphify draws today.
// ---------------------------------------------------------------------------
const COMMS = {payload};
const COMMS_HELD = new Map(COMMS.held.map(h => [h.id, h]));
const COMMS_NEAR = new Map(COMMS.near.map(n => [n.id, n]));

function commsEsc(s) {{
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}
// The map also holds "rationale" nodes whose label is a whole sentence of prose.
// A file claim picks those up like any other node; printed in full they turn the
// panel into an essay, so the chip shows a head and the hover shows the rest.
function commsShort(s) {{
  s = String(s === null || s === undefined ? '' : s);
  return s.length > 34 ? s.slice(0, 33) + '…' : s;
}}

// --- reaching the base script safely ---------------------------------------
// `network` and `nodesDS` are top-level `const`s in the exporter's script. If
// that script threw before initialising them: which is exactly what happens
// when the CDN drawing library did not load: they exist but are in the
// temporal dead zone, and even `typeof network` throws a ReferenceError. Every
// read of them therefore goes through here, so a failed library takes the
// PICTURE down without also taking down the roster, which needs no library at
// all and is the half of this page somebody can still act on.
function commsNet() {{ try {{ return network; }} catch (e) {{ return null; }} }}
function commsDS()  {{ try {{ return nodesDS; }} catch (e) {{ return null; }} }}
const COMMS_DREW = typeof window.vis !== 'undefined' && !!commsNet();

// --- marks -----------------------------------------------------------------
// A ring, never an arrow. An arrow between two people's work would read as
// sequence, and sequence derived from this map was measured at coin-flip
// accuracy and cut (docs/COMMS.md).
//
// afterDrawing hands over a ctx already transformed into NETWORK coordinates,
// so a line width or a font size written here is in world units and shrinks
// with the zoom: at a fitted view of a few hundred nodes the scale is about
// 0.2, which turned a 3-unit ring into two thirds of a screen pixel and an
// 11-unit label into unreadable fuzz. Every stroke and every glyph below is
// therefore divided by the current scale so the marks stay the same size on
// screen at any zoom, while the ring RADIUS stays in world units because the
// node it circles is in world units too.
function commsRadius(id) {{
  const ds = commsDS();
  const n = ds ? ds.get(id) : null;
  const s = (n && typeof n.size === 'number') ? n.size : 12;
  return s + 6;
}}
function commsVisible(id) {{
  const ds = commsDS();
  const n = ds ? ds.get(id) : null;
  return !!n && n.hidden !== true;
}}

//: Below this zoom the who/why tags are drawn on top of each other and read as
//: a smear. Rings still show, so "somebody is here" survives at every zoom; only
//: the name waits until you are close enough to read it.
const COMMS_TAG_ZOOM = 0.45;

const COMMS_NETWORK = commsNet();
if (COMMS_NETWORK) {{
  const network = COMMS_NETWORK;  // shadows the outer const; safe to read here
  network.on('afterDrawing', function (ctx) {{
    const z = network.getScale() || 1;
    ctx.save();
    // Faint first, so a solid ring is never drawn under a faint one.
    COMMS_NEAR.forEach((n, id) => {{
      if (!commsVisible(id)) return;
      const p = network.getPositions([id])[id];
      if (!p) return;
      ctx.beginPath();
      ctx.arc(p.x, p.y, commsRadius(id), 0, Math.PI * 2);
      ctx.strokeStyle = '{_NEAR_COLOR}';
      ctx.lineWidth = 2 / z;
      ctx.setLineDash([4 / z, 5 / z]);
      ctx.stroke();
    }});
    ctx.setLineDash([]);
    COMMS_HELD.forEach((h, id) => {{
      if (!commsVisible(id)) return;
      const p = network.getPositions([id])[id];
      if (!p) return;
      const r = commsRadius(id);
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.strokeStyle = '{_HELD_COLOR}';
      ctx.lineWidth = 3 / z;
      ctx.stroke();
      // Who, and why, written next to the mark. The label is the point: a mark
      // nobody can attribute is just noise on the map.
      if (z < COMMS_TAG_ZOOM || h.tag === false) return;
      const tag = '@' + h.actor + (h.why ? ': ' + h.why : '')
                + (h.also ? ' (+' + h.also + ' more here)' : '');
      ctx.font = 'bold ' + (11 / z) + 'px -apple-system, BlinkMacSystemFont, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      const w = ctx.measureText(tag).width;
      ctx.fillStyle = 'rgba(12,12,24,0.86)';
      ctx.fillRect(p.x - w / 2 - 4 / z, p.y - r - 18 / z, w + 8 / z, 15 / z);
      ctx.fillStyle = '{_HELD_COLOR}';
      ctx.fillText(tag, p.x, p.y - r - 5 / z);
    }});
    ctx.restore();
  }});
  // vis only repaints on its own events; a zoom done through the API still
  // fires one, but a redraw forced here keeps the marks honest if the page is
  // driven from the console.
  network.on('zoom', () => network.redraw());

  // Slow the wheel down. vis-network's default zoomSpeed of 1 moves roughly a
  // factor of 1.2 per wheel notch, and a trackpad sends notches in bursts: two
  // flicks and a few-hundred-node map has gone from fitted to a single dot or
  // to one node filling the screen, with no sense of having travelled. At 0.35
  // a deliberate scroll still crosses the whole range in about a second, but a
  // stray one costs nothing.
  //
  // Set here rather than in graphify's own exporter: this is the coordination
  // view's call to make, and the fork does not edit upstream artifacts.
  network.setOptions({{ interaction: {{ zoomSpeed: 0.35 }} }});
}}

// --- hover text ------------------------------------------------------------
// Appended to the existing title, never replacing it.
const COMMS_DS = commsDS();
if (COMMS_DS) {{
  const nodesDS = COMMS_DS;  // shadows the outer const; safe to read here
  const updates = [];
  COMMS_HELD.forEach((h, id) => {{
    const n = nodesDS.get(id);
    if (!n) return;
    updates.push({{ id: id, title: (n.title || '') + '\\nSomebody is here: @' + h.actor +
      ' holds ' + h.scope + (h.why ? ': ' + h.why : '') }});
  }});
  COMMS_NEAR.forEach((nb, id) => {{
    const n = nodesDS.get(id);
    if (!n) return;
    updates.push({{ id: id, title: (n.title || '') + '\\nOne connection from work @' + nb.actor +
      ' is doing' + (nb.relation ? ' [' + nb.relation + ']' : '') + ': worth a look, not a verdict' }});
  }});
  if (updates.length) nodesDS.update(updates);
}}

// --- sidebar ---------------------------------------------------------------
(function () {{
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const panel = document.createElement('div');
  panel.id = 'comms-panel';

  // A whole-file claim resolves to every symbol in the file. Listing all of
  // them buries the other claims under one person's wall of chips, so the list
  // is cut and the cut is stated: never silently truncated into looking small.
  const CHIP_CAP = 8;
  let rows = '';
  COMMS.claims.forEach(c => {{
    const ids = c.ids || [];
    let chips = ids.slice(0, CHIP_CAP).map(id =>
      `<span class="comms-node" data-comms-node="${{commsEsc(id)}}" title="${{commsEsc(
        (COMMS_HELD.get(id) || {{}}).label || id)}}">${{commsEsc(
        commsShort((COMMS_HELD.get(id) || {{}}).label || id))}}</span>`).join('');
    if (ids.length > CHIP_CAP) {{
      chips += `<span class="comms-more">and ${{ids.length - CHIP_CAP}} more, all marked on the map</span>`;
    }}
    rows += `<div class="comms-claim${{c.miss ? ' miss' : ''}}">
      <span class="comms-actor">@${{commsEsc(c.actor)}}</span>
      <span class="comms-scope">${{commsEsc(c.scope)}}</span>
      ${{c.why ? `<div class="comms-why">${{commsEsc(c.why)}}</div>` : ''}}
      ${{c.miss ? `<div class="comms-sub">not on the map: ${{commsEsc(c.miss)}}</div>` : ''}}
      ${{c.stale ? `<div class="comms-sub">${{commsEsc(c.stale)}}</div>` : ''}}
      ${{c.near_note ? `<div class="comms-sub">${{commsEsc(c.near_note)}}</div>` : ''}}
      ${{chips}}
    </div>`;
  }});
  if (!COMMS.claims.length) {{
    rows = COMMS.blind
      ? '<div class="comms-quiet">The log could not be read, so this cannot say who is '
        + 'where. An empty list here is the absence of an answer, not the answer '
        + '"nobody": every claim may still be held.</div>'
      : '<div class="comms-quiet">Nobody is holding anything right now. '
        + 'That is what the log says, not a statement about whether the code is free.</div>';
  }}

  // Said out loud, not swallowed: if the picture is not there, a panel that
  // just sat quietly next to a black rectangle would read as "the map says
  // nothing is going on" rather than "the map did not load".
  const offline = COMMS_DREW ? '' :
    '<div id="comms-offline">The drawing library did not load, so the map is not '
    + 'drawn on this page and no marks are placed. The list below is still exactly '
    + 'what the log holds. The library is fetched from unpkg.com the first time the '
    + 'page is opened.</div>';

  panel.innerHTML = `<h3>Who is here</h3>
    ${{offline}}
    <div id="comms-headline">${{commsEsc(COMMS.headline)}}</div>
    ${{rows}}
    ${{COMMS_DREW ? `<div id="comms-key">
      <div><span class="k" style="border:3px solid {_HELD_COLOR};"></span>somebody is standing here</div>
      <div><span class="k" style="border:2px dashed rgba(255,255,255,0.5);"></span>one connection away</div>
    </div>` : ''}}
    <div id="comms-limits"><ul>
      ${{COMMS.notes.map(n => `<li>${{commsEsc(n)}}</li>`).join('')}}
    </ul><div style="margin-top:6px">read ${{commsEsc(COMMS.generated)}}</div></div>`;

  const anchor = document.getElementById('info-panel') || sidebar.firstChild;
  sidebar.insertBefore(panel, anchor);

  // Delegated, like the base script's neighbour links: the id goes in a data
  // attribute and is read back verbatim, so a node id carrying a quote cannot
  // break out into an event handler.
  panel.addEventListener('click', e => {{
    const el = e.target.closest('[data-comms-node]');
    if (!el) return;
    const id = el.getAttribute('data-comms-node');
    const net = commsNet();
    if (!net) return;
    try {{
      net.focus(id, {{ scale: 1.4, animation: true }});
      net.selectNodes([id]);
      if (typeof showInfo === 'function') showInfo(id);
    }} catch (err) {{ /* node filtered out of the render */ }}
  }});
}})();
</script>"""


# --------------------------------------------------------------------------
# The no-map page
# --------------------------------------------------------------------------


def _standalone_page(data: ViewData, map_path: Path) -> str:
    """A real answer for "there is no map yet".

    Self-contained and needs no network: there is no graph to draw, so there is
    no drawing library to fetch. It still lists who holds what, because that half
    of coordination comes from the log and does not need the map at all.
    """
    rows = []
    for c in data.claims:
        rows.append(
            "<div class='claim'><span class='actor'>@{a}</span> "
            "<span class='scope'>{s}</span>{w}</div>".format(
                a=_html.escape(c.actor),
                s=_html.escape(c.scope),
                w=("<div class='why'>" + _html.escape(c.why) + "</div>") if c.why else "",
            )
        )
    body = "".join(rows) or (
        # Two DIFFERENT empty states, and the second one used to borrow the
        # first's sentence. This page was missed when the interactive one was
        # fixed: with an unreadable log it printed the most reassuring line the
        # tool has — "Nobody is holding anything right now. That is what the log
        # says" — attributing it to a log it had just failed to parse, while the
        # board rail beside it showed the parse error. The page contradicted
        # itself and the reassuring half was the false one.
        "<div class='quiet'>The coordination log could not be read, so this page "
        "cannot say who is where. An empty list here is the absence of an answer, "
        "not the answer &quot;nobody&quot;: every claim may still be held.</div>"
        if data.blind else
        "<div class='quiet'>Nobody is holding anything right now. That is what the log "
        "says, not a statement about whether the code is free.</div>"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>comms-graph - no map yet</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0f0f1a; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; padding: 48px; line-height: 1.6; }}
  main {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin-bottom: 6px; }}
  p.lead {{ color: #9aa4c0; margin-bottom: 24px; font-size: 14px; }}
  h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: #888; margin: 28px 0 10px; }}
  .claim {{ border-left: 3px solid #ffffff; padding: 6px 0 6px 10px; margin: 8px 0; font-size: 13px; }}
  .actor {{ font-weight: 600; }}
  .scope {{ color: #9fb6d4; word-break: break-all; }}
  .why {{ color: #999; font-style: italic; font-size: 12px; }}
  .quiet {{ color: #f0d264; font-size: 13px; }}
  code {{ background: #1a1a2e; padding: 2px 6px; border-radius: 4px; color: #cfd8ff; }}
  ul {{ margin-left: 18px; color: #6d6d85; font-size: 12px; }}
  footer {{ margin-top: 32px; color: #55556e; font-size: 11px; }}
</style>
</head>
<body>
<main>
  <h1>No map has been built for this project yet.</h1>
  <p class="lead">Nothing was found at <code>{_html.escape(str(map_path))}</code>.
     Run <code>graphify extract</code> (or <code>graphify update &lt;path&gt;</code>) and read this again.
     The picture needs a map to draw on.</p>
  <h2>What the log does know</h2>
  {body}
  <h2>What this page is not telling you</h2>
  <ul>
    <li>Without a map, only exact overlaps are visible: nothing here knows what is connected to what.</li>
    <li>A short list is not a statement that the rest of the code is free.</li>
    <li>Nothing here implies an order. There is no "first" and no "blocked".</li>
  </ul>
  <footer>read {_html.escape(data.generated)}</footer>
</main>
</body>
</html>"""


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _viz_limit() -> int:
    try:
        from graphify.exporters.html import _viz_node_limit

        return _viz_node_limit()
    except Exception:
        return 5_000


def render(
    root: str | Path,
    output_path: str | Path | None = None,
    *,
    graph_file: str | None = None,
    log_file: str | Path | None = None,
    state: Any | None = None,
) -> ViewResult:
    """Write the map with the people on it. Returns where it went and what it is."""
    root = Path(root).expanduser().resolve()
    map_path = graph_path(root, graph_file)
    out = Path(output_path) if output_path else (map_path.parent / DEFAULT_FILENAME)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Both of these fail in two ways that must not read alike: absent, which is
    # an ordinary state with a next step, and present-but-broken, which is a
    # problem with a file that already exists.
    map_error = ""
    try:
        graph = load_graph(map_path)
    except _MapUnreadable as exc:
        graph, map_error = None, str(exc)

    log_error = ""
    if state is not None:
        st = state
    else:
        try:
            st = read_state(root, log_file)
        except _LogUnreadable as exc:
            st, log_error = _state.State(), str(exc)

    data = collect(graph, st, root, map_path)

    if log_error:
        # Overwrite whatever collect() concluded. It concluded it from empty
        # state, and empty state here is the absence of an answer, not the
        # answer "nobody".
        data.held.clear()
        data.near.clear()
        data.claims = []
        data.blind = True
        data.headline = (
            "The coordination log could not be read, so this picture cannot say who is "
            "where. It is NOT saying the code is free."
        )
        data.notes = [
            f"The log is at {_log.log_path(root) if log_file is None else log_file} "
            f"and did not parse: {log_error}",
            "Every claim may still be held. Nothing here is evidence either way.",
        ]

    if graph is None:
        if map_error:
            data.headline = (
                "There is a map file, and it could not be read, so this is not a "
                "picture of your code, and it is not evidence that anything is free."
            )
            data.notes = [
                f"{map_path} exists but did not parse: {map_error}",
                "Rebuilding it with `graphify extract` will overwrite it.",
            ] + (data.notes if log_error else [])
        out.write_text(_standalone_page(data, map_path), encoding="utf-8")
        return ViewResult(out, "unreadable-map" if map_error else "no-map", data)

    from graphify.exporters.html import to_html

    communities, labels = communities_of(graph)

    # The exporter silently swaps to an aggregated community-level meta-graph
    # above its node cap, and in that render the node ids ARE community ids —
    # every mark would land on the wrong thing, or on nothing. Say so instead.
    if graph.number_of_nodes() > _viz_limit():
        data.held.clear()
        data.near.clear()
        data.headline = (
            f"This map has {graph.number_of_nodes()} nodes, above the "
            f"{_viz_limit()} the picture can draw one-by-one, so it is drawn grouped. "
            "Individual marks are not placed on a grouped picture: the claims below are "
            "still exactly what the log holds."
        )
        for c in data.claims:
            c.node_ids = []

    to_html(graph, communities, str(out), community_labels=labels or None,
            node_limit=_viz_limit() if graph.number_of_nodes() > _viz_limit() else None)

    if not out.is_file():
        # to_html declines to write in one case (a single community in the
        # aggregated path). A missing file must not be reported as a picture.
        out.write_text(_standalone_page(data, map_path), encoding="utf-8")
        return ViewResult(out, "no-map", data)

    html_text = out.read_text(encoding="utf-8")
    overlay = _OVERLAY_STYLE + "\n" + _overlay_script(data)
    if "</body>" in html_text:
        html_text = html_text.replace("</body>", overlay + "\n</body>", 1)
    else:  # pragma: no cover - the exporter always emits </body>
        html_text += overlay
    out.write_text(html_text, encoding="utf-8")

    return ViewResult(out, "overlay" if data.held else "quiet", data)


def main(argv: Iterable[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print(
            "Usage: python -m graphify.comms.view [--root DIR] [--graph graph.json]\n"
            "                                     [--log FILE] [--out FILE]\n\n"
            "Draws the graphify map with current claims marked on it.\n"
            "A mark means somebody is there, or that something is nearby. Never an order."
        )
        return 0
    flags: dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a[2:].split("=", 1)
                flags[k] = v
            elif i + 1 < len(args):
                flags[a[2:]] = args[i + 1]
                i += 1
        i += 1

    res = render(
        flags.get("root", "."),
        flags.get("out"),
        graph_file=flags.get("graph"),
        log_file=flags.get("log"),
    )
    print(f"{res.kind}: {res.output_path}")
    print(f"  {res.data.headline}")
    for c in res.data.claims:
        extra = f"  [{c.miss_reason}]" if c.miss_reason else f"  ({c.place_count} place(s))"
        print(f"  @{c.actor} {c.scope}{extra}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
