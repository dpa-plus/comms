"""Draw the task graph: what work exists, what is stuck, and on what.

DELIBERATELY NOT THE SAME PICTURE AS THE CODE MAP, and the difference is the
point. ``view.py`` draws the code map with rings and NO arrows, because deriving
"do A before B" from code edges measured about as reliable as a coin flip, and a
picture implying order would be the most persuasive possible way to ship that
coin flip as a fact.

Here the order is DECLARED. An agent wrote down that B comes after A. That is a
statement somebody made, not something inferred from imports — so arrows are
honest here, and they are the whole content of the picture.

The one question this has to answer at a glance is **what is blocked, and on
what**. Everything else is secondary: layout is left-to-right by depth so an
arrow can only ever point one way, and a reader can follow a chain without
tracing which end of a line is which.

WHY VIS-NETWORK'S HIERARCHICAL LAYOUT rather than a hand-rolled one: it is
already the dependency the code map uses, so this adds nothing to install, and
its layered assignment with ``sortMethod: 'directed'`` is the same Sugiyama-style
algorithm we would otherwise be writing. It is also deterministic, which matters
because this view is regenerated whenever the log changes and a graph that
reshuffles on every regeneration is one nobody can keep their place in.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import task as _task

DEFAULT_FILENAME = "tasks.comms.html"

#: Phase colours. Chosen so the two that demand action — something you could
#: start, and something waiting on YOU — are the ones that carry saturation,
#: while closed work recedes rather than competing for attention.
_PHASE_STYLE = {
    _task.PHASE_READY:   ("#7ad39a", "#1c3327", "startable now"),
    _task.PHASE_DOING:   ("#f0c274", "#3a2a12", "somebody is on it"),
    _task.PHASE_REVIEW:  ("#8ab4f8", "#1b2b45", "waiting to be verified"),
    _task.PHASE_BLOCKED: ("#9aa0b4", "#22222f", "waiting on something unverified"),
    _task.PHASE_CLOSED:  ("#4a5568", "#191922", "verified and closed"),
    _task.PHASE_CYCLE:   ("#f28b82", "#3a1d1b", "in a dependency loop: unreachable"),
}


@dataclass
class TaskViewResult:
    output_path: Path
    task_count: int
    edge_count: int
    blocked: int
    cycles: int


def _state_words(t: Any) -> str:
    """Where this task stands, in words a non-engineer can read.

    Shared by the boxes in the graph and the cards below it, so a task cannot
    describe itself one way in the picture and another way in the list.
    """
    if t.phase == "doing" and t.doers:
        return "@" + ", @".join(t.doers)
    if t.phase == "review":
        return "waiting to be checked"
    if t.phase == "closed":
        return ("checked by @" + t.verified_by) if t.verified_by else "done"
    if t.phase == "blocked":
        return "waiting on " + ", ".join(t.blocked_by[:2]) if t.blocked_by else "blocked"
    if t.phase == "cycle":
        return "in a dependency loop"
    return "nobody on it"


def _node_label(t: Any) -> str:
    """What the box says, and it is read at a glance or not at all.

    It used to be the slug with a truncated title under it, which answered
    neither question somebody actually has in front of a task graph: what is
    this, and is anyone on it. Two boxes reading "jagd-runde-zwei / Elf Befunde
    der zweiten Browserja…" are indistinguishable at a glance.

    So: the TITLE first, because that is what the task is. Then the state in
    words. Then who is holding it, since "someone is on this" and "this is
    sitting there" are the two states worth telling apart from across a room.
    """
    title = (t.title or "").strip() or t.id
    if len(title) > 52:
        # Cut at a space, not mid-word: "messages we cannot tru…" reads as a
        # rendering fault, "messages we cannot…" reads as an abbreviation.
        head = title[:52].rsplit(" ", 1)[0]
        title = (head or title[:51]) + "…"

    state = _state_words(t)
    if len(state) > 38:
        state = state[:37] + "…"

    return f"{title}\n{state}\n{t.id}"


def _tooltip(t: Any, edges: list) -> str:
    lines = [f"{t.id}: {t.phase}"]
    if t.title:
        lines.append(t.title)
    if t.doers:
        lines.append("doing: " + ", ".join("@" + d for d in t.doers))
    if t.did:
        lines.append(f"awaiting review of @{t.did}'s work"
                     if getattr(t, "needs_review", False)
                     else f"finished by @{t.did}")
    if t.verified_by:
        lines.append(f"verified by @{t.verified_by} ({t.independence or 'unknown'})")
        # What they say they ran. A person scanning the board for a task to
        # trust needs this more than the fact of a sign-off — the fact is a
        # colour, the method is the reason to believe it.
        if getattr(t, "verification", ""):
            lines.append(f"  checked by: {t.verification}")
    if t.blocked_by:
        lines.append("waiting on: " + ", ".join(t.blocked_by))
    if t.checks:
        lines.append("checks: " + ", ".join(t.checks))
    if t.rejections:
        lines.append(f"sent back {t.rejections}x")
    for e in edges:
        if e.to == t.id and e.provides:
            lines.append(f"consumes from {e.from_}: {e.provides}")
    return "\n".join(lines)


def build(state: Any) -> tuple[list[dict], list[dict], dict]:
    """Nodes, edges and a summary, ready to hand to the renderer."""
    tasks = getattr(state, "tasks", {}) or {}
    edges = list(getattr(state, "task_edges", []) or [])

    nodes = []
    for tid in sorted(tasks):
        t = tasks[tid]
        border, fill, _ = _PHASE_STYLE.get(t.phase, _PHASE_STYLE[_task.PHASE_READY])
        # A task its own author signed off is NOT a verified task, and drawing
        # it identically to one made the picture the last place still saying so.
        # A reader looking at the shape, the legend or the counts saw "verified";
        # only hovering the right node told them otherwise. Dashed border, and
        # the label carries it, so it is visible without interaction.
        self_signed = t.independence == "self-acknowledged"
        label = _node_label(t) + ("\nself-signed" if self_signed else "")
        nodes.append({
            "id": t.id,
            "label": label,
            "title": _tooltip(t, edges),
            "phase": t.phase,
            "color": {"background": fill, "border": ("#f0c274" if self_signed else border),
                      "highlight": {"background": fill, "border": "#ffffff"}},
            "shapeProperties": {"borderDashes": [6, 4]} if self_signed else {},
            "borderWidth": 3 if (self_signed or t.phase in (_task.PHASE_REVIEW, _task.PHASE_CYCLE)) else 2,
            "font": {"color": "#e8eaf2", "size": 13, "face": "ui-monospace, SFMono-Regular, Menlo, monospace"},
        })

    out_edges = []
    for e in edges:
        if e.from_ not in tasks or e.to not in tasks:
            # An edge to a task nobody declared is noise. Drawing it would
            # invent a node, and hiding it silently would lose the fact that
            # somebody wrote it down, so it is reported in the summary instead.
            continue
        consumes = e.kind == _task.EDGE_CONSUMES
        out_edges.append({
            "from": e.from_,
            "to": e.to,
            # Solid means "B consumes something from A", so reworking A puts B
            # back in question. Dashed means ordering only. That distinction is
            # what makes a rejection precise instead of invalidating everything
            # downstream, and it should be visible without reading a legend.
            "dashes": not consumes,
            "color": {"color": "#8a93ad" if consumes else "#4d5468",
                      "highlight": "#ffffff"},
            "width": 2 if consumes else 1,
            "title": (e.provides or e.kind),
            "arrows": {"to": {"enabled": True, "scaleFactor": 0.65}},
        })

    dangling = sum(1 for e in edges if e.from_ not in tasks or e.to not in tasks)
    summary = {
        "total": len(tasks),
        "blocked": sum(1 for t in tasks.values() if t.phase == _task.PHASE_BLOCKED),
        "cycles": sum(1 for t in tasks.values() if t.phase == _task.PHASE_CYCLE),
        "review": sum(1 for t in tasks.values() if t.phase == _task.PHASE_REVIEW),
        "ready": sum(1 for t in tasks.values() if t.phase == _task.PHASE_READY),
        "doing": sum(1 for t in tasks.values() if t.phase == _task.PHASE_DOING),
        "closed": sum(1 for t in tasks.values() if t.phase == _task.PHASE_CLOSED),
        "dangling": dangling,
    }
    return nodes, out_edges, summary


def _json_for_script(obj) -> str:
    r"""JSON safe to embed inside a <script> block.

    json.dumps escapes quotes and backslashes; it does NOT escape ``<``. So a
    string containing ``</script>`` closes the block early and everything after
    it is parsed as HTML — which is live script, from a value somebody else
    wrote. Every string in here is agent-controlled: task titles, the "provides"
    text on an edge, actor names. Reproduced with a task titled
    ``</title><script>alert(3)</script>``, which executed.

    view.py had this right already (``.replace("</", "<\\/")``); this module was
    written separately and did not carry the defence over. Escaping the three
    characters as \u sequences is stricter than escaping ``</`` alone and cannot
    change the decoded value.
    """
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def _headline(summary: dict) -> str:
    if not summary["total"]:
        return "No tasks declared yet."
    bits = []
    if summary["ready"]:
        bits.append(f"{summary['ready']} startable")
    if summary["doing"]:
        bits.append(f"{summary['doing']} being worked on")
    if summary["review"]:
        bits.append(f"{summary['review']} waiting to be verified")
    if summary["blocked"]:
        bits.append(f"{summary['blocked']} blocked")
    if summary["closed"]:
        bits.append(f"{summary['closed']} closed")
    return f"{summary['total']} task(s): " + ", ".join(bits) if bits else f"{summary['total']} task(s)."


_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>comms - task graph</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  html, body {{ margin: 0; background: #0f0f1a; color: #e8eaf2;
    font: 13px/1.5 ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif; }}
  /* Viewport units, not percentages. A percentage height only resolves against a
     parent with a definite height, and vis sizes its canvas to fill this box :
     so a chain of 100% heights let the canvas drive the container that was
     supposed to be driving IT. The canvas grew on every redraw (2036 -> 2072 ->
     2120 px) and painted nothing at all. 100dvh is definite on its own. */
  #wrap {{ display: grid; grid-template-columns: minmax(0, 1fr) 290px;
    height: 100dvh; max-height: 100dvh; }}
  /* The left column STACKS: the drawn graph on top, the tasks that join to
     nothing as cards underneath. See the comment above the split in render() :
     feeding unconnected tasks to a hierarchical layout is what produced the
     single illegible column this replaces. */
  #gcol {{ display: flex; flex-direction: column; min-width: 0; min-height: 0; }}
  #graph {{ flex: 1 1 auto; min-width: 0; min-height: 0; overflow: hidden; }}
  #graph.gone {{ display: none; }}
  #loose {{ flex: 0 1 auto; overflow-y: auto; border-top: 1px solid #2a2a4e;
    background: #12121f; padding: 12px 16px 16px; max-height: 55%; }}
  /* Nothing is drawn above it, so it takes the pane instead of hugging a border
     that has no picture on the other side. */
  #graph.gone + #loose {{ border-top: none; flex: 1 1 auto; max-height: none; }}
  .lgrid {{ display: grid; gap: 10px;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }}
  .lcard {{ border: 1px solid #2a2a4e; border-left-width: 3px; border-radius: 5px;
    background: #191927; padding: 9px 11px; min-width: 0; }}
  .lcard .lt {{ font-weight: 600; overflow-wrap: anywhere; }}
  .lcard .ls {{ color: #8890b0; font-size: 11.5px; margin-top: 3px; }}
  .lcard .lid {{ color: #5d6486; font-size: 10.5px; margin-top: 5px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }}
  .lhead {{ font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
    color: #8890b0; margin: 0 0 9px; }}
  #side {{ border-left: 1px solid #2a2a4e; background: #16162b; overflow-y: auto; padding: 14px 16px; }}
  h1 {{ font-size: 13px; margin: 0 0 2px; letter-spacing: .04em; text-transform: uppercase; color: #cfd8ff; }}
  .headline {{ font-weight: 600; margin-bottom: 12px; }}
  .sec {{ font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: #8890b0;
    margin: 16px 0 6px; }}
  .row {{ display: flex; gap: 8px; align-items: baseline; padding: 3px 0; }}
  .dot {{ width: 9px; height: 9px; border-radius: 50%; flex: none; transform: translateY(1px); }}
  .slug {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .muted {{ color: #8890b0; }}
  .note {{ color: #8890b0; font-size: 11.5px; margin-top: 4px; }}
  .blocked-on {{ color: #f0c274; font-family: ui-monospace, Menlo, monospace; }}
  .empty {{ color: #8890b0; padding: 24px 0; }}
  /* Over the canvas, not inside the panel beside it. The board hides that panel
     to give the picture the whole pane, which is right: but it was taking the
     one line that explains what you are looking at with it, so a column of
     unconnected boxes arrived with no way to know why. */
  .warn {{ background: #3a1d1b; border: 1px solid #f28b82; color: #f6b0aa;
    padding: 8px 10px; border-radius: 5px; margin: 10px 0; }}
  .warn.info {{ background: #1b2338; border-color: #3d4c74; color: #b7c4e6; }}
  /* A SIBLING of #graph, not a child. vis-network replaces the contents of its
     container on construction, so anything inside #graph is destroyed the
     moment the picture is drawn: the notice was in the HTML, absent from the
     DOM, and invisible for exactly that reason. */
  #wrap > .warn {{ position: absolute; z-index: 5; left: 16px; top: 12px;
    margin: 0; max-width: 720px; font-size: 12.5px; line-height: 1.45;
    box-shadow: 0 6px 24px rgba(0,0,0,.45); }}
  #wrap {{ position: relative; }}
  code {{ background: #24243f; padding: 1px 5px; border-radius: 3px;
    font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; }}
</style>
<div id="wrap">
  <div id="gcol"><div id="graph"></div>{loose_block}</div>
  {warning}
  <div id="side">
    <h1>Task graph</h1>
    <div class="headline">{headline}</div>
    {blocked_block}
    {review_block}
    <div class="sec">Phases</div>
    {legend}
    <div class="sec">Edges</div>
    <div class="row"><span style="flex:none;width:26px;border-top:2px solid #8a93ad"></span>
      <span class="muted">consumes: reworking the source puts the target back in question</span></div>
    <div class="row"><span style="flex:none;width:26px;border-top:1px dashed #4d5468"></span>
      <span class="muted">ordering only: reworking the source does not touch the target</span></div>
    <div class="note">An arrow means somebody DECLARED that the target comes after the
      source. Unlike the code map, this order was written down, not inferred: which is
      why this picture has arrows and that one does not.</div>
    <div class="note">A task unblocks what follows it when it is <strong>verified</strong>
      by somebody other than whoever did it, not when it is marked done.</div>
    <div class="note">Generated {generated}.</div>
  </div>
</div>
<script>
// Only the tasks an edge actually touches. Everything else is a card in
// #loose: a hierarchical layout has nothing to say about a node with no
// edges, and asking it anyway is what drew them as one unreadable column.
const NODES = {nodes_json};
const EDGES = {edges_json};
const container = document.getElementById('graph');
if (NODES.length === 0) {{
  container.classList.add('gone');
}} else {{
  const network = new vis.Network(container,
    {{ nodes: new vis.DataSet(NODES), edges: new vis.DataSet(EDGES) }},
    {{
      layout: {{
        // improvedLayout is a Kamada-Kawai pre-positioning pass that runs before
        // the real layout. On this graph it gave up: "could not be positioned by
        // this version of the improved layout algorithm", and left every node
        // without coordinates, so the canvas rendered completely empty with no
        // error. It is pointless here regardless: hierarchical layout assigns
        // every position itself, so the pre-pass has nothing to contribute.
        improvedLayout: false,
        // Left-to-right by depth, so an arrow can only ever point one way and a
        // chain can be followed without working out which end is which.
        // sortMethod 'directed' is what makes the layering respect the edges.
        hierarchical: {{ enabled: true, direction: 'LR', sortMethod: 'directed',
                         levelSeparation: 250, nodeSpacing: 145, treeSpacing: 190 }},
      }},
      // Physics off entirely: hierarchical layout is deterministic, and letting
      // a simulation settle afterwards would move nodes between regenerations.
      physics: false,
      interaction: {{ hover: true, tooltipDelay: 120, zoomSpeed: 0.35, navigationButtons: false }},
      nodes: {{ shape: 'box', margin: 11, widthConstraint: {{ maximum: 230 }},
                shapeProperties: {{ borderRadius: 4 }} }},
      edges: {{ smooth: {{ type: 'cubicBezier', forceDirection: 'horizontal', roundness: 0.45 }} }},
    }});
  // Draw once the grid has actually resolved a size for this cell.
  //
  // vis sizes its canvas at construction. Inside a CSS grid the cell's height is
  // not final at the moment the script runs, so the canvas was created against a
  // zero-height box and then never redrawn: the page looked completely empty
  // with no error, and the canvas element itself reported the right dimensions
  // afterwards, which made it look like a data problem rather than a timing one.
  // vis-network's fit() zooms OUT to fit a large graph but will not zoom IN
  // past scale 1, so a handful of nodes rendered postage-stamp-sized in the
  // middle of an empty canvas, labels and all. Its maxZoomLevel option does not
  // lift that here, so the scale is computed from the node box instead. 2.2 is
  // where the box borders start to look heavy.
  const MAX_IN = 2.2;
  const settle = () => {{
    network.redraw();
    network.fit({{ animation: false }});
    if (network.getScale() >= MAX_IN) return;
    const pos = network.getPositions();
    const ids = Object.keys(pos);
    if (!ids.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const id of ids) {{
      const p = pos[id];
      if (p.x < x0) x0 = p.x; if (p.x > x1) x1 = p.x;
      if (p.y < y0) y0 = p.y; if (p.y > y1) y1 = p.y;
    }}
    // getPositions returns centres, not boxes, so pad by roughly one node plus
    // breathing room: without it the outermost boxes are clipped by the edge.
    const w = (x1 - x0) + 300, h = (y1 - y0) + 170;
    const cw = container.clientWidth, ch = container.clientHeight;
    if (!cw || !ch) return;
    const want = Math.min(cw / w, ch / h, MAX_IN);
    if (want > network.getScale()) {{
      network.moveTo({{ scale: want, position: {{ x: (x0 + x1) / 2, y: (y0 + y1) / 2 }},
                       animation: false }});
    }}
  }};
  // Synchronously first. requestAnimationFrame alone was not enough: a browser
  // throttles rAF when the page is not actively painting: a background tab, an
  // embedded preview pane: so the callback never ran and the canvas stayed
  // blank with no error anywhere. A straight call always happens; the timeout
  // afterwards catches the case where the grid had not resolved its height yet.
  settle();
  setTimeout(settle, 0);
  setTimeout(settle, 250);
  // Exposed so the page can be interrogated from a console when it misbehaves.
  window.commsTaskNetwork = network;
  try {{
    const p = network.getPositions();
    console.log('comms: task graph drew ' + Object.keys(p).length + ' node(s), scale '
                + network.getScale());
  }} catch (e) {{ console.log('comms: task graph could not report positions: ' + e); }}
  // And again whenever the window changes, or the picture keeps the old viewport.
  let resizeTimer = null;
  window.addEventListener('resize', () => {{
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(settle, 120);
  }});
  // A window 'resize' never fires for the case that actually breaks this: the
  // flex box resolving its height AFTER the timeouts above have run. settle()
  // then read clientHeight 0, bailed out, and left the graph at fit()'s scale
  // with no error: reproduced in headless Chrome, where the whole three-call
  // sequence lands before layout. ResizeObserver watches the box itself, so the
  // picture is re-fitted the moment it actually has a size to fit into.
  if (window.ResizeObserver) {{
    let last = 0;
    new ResizeObserver(() => {{
      const now = container.clientWidth * 100000 + container.clientHeight;
      if (now === last) return;   // observers also fire on our own redraw
      last = now;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(settle, 60);
    }}).observe(container);
  }}
}}
</script>
"""


def render(state: Any, output_path: str | Path, generated: str = "") -> TaskViewResult:
    """Write the task graph to ``output_path``."""
    nodes, edges, summary = build(state)
    tasks = getattr(state, "tasks", {}) or {}

    # A hierarchical layout has nothing to say about a node with no edges: every
    # one of them lands on level 0, so they stack into a single column, and
    # fit() then shrinks that tall thin column until the labels are gone. That
    # is exactly what the board was showing — 17 empty boxes and no way to read
    # any of them.
    #
    # So only the connected part is drawn. Tasks that join to nothing are a
    # LIST, because that is what they are, and a list can be read.
    joined = {e["from"] for e in edges} | {e["to"] for e in edges}
    drawn = [n for n in nodes if n["id"] in joined]
    loose = [tasks[n["id"]] for n in nodes if n["id"] not in joined and n["id"] in tasks]

    if loose:
        cards = []
        for t in sorted(loose, key=lambda t: (t.phase != "doing", t.phase != "ready", t.id)):
            border, fill, _ = _PHASE_STYLE.get(t.phase, _PHASE_STYLE[_task.PHASE_READY])
            cards.append(
                f'<div class="lcard" style="border-left-color:{border};background:{fill}"'
                f' title="{html.escape(_tooltip(t, []))}">'
                f'<div class="lt">{html.escape((t.title or "").strip() or t.id)}</div>'
                f'<div class="ls">{html.escape(_state_words(t))}</div>'
                f'<div class="lid">{html.escape(t.id)}</div></div>'
            )
        head = (f"On their own: {len(loose)} task(s) nothing waits on"
                if drawn else f"{len(loose)} task(s), none connected to another")
        loose_block = (f'<div id="loose"><div class="lhead">{head}</div>'
                       f'<div class="lgrid">{"".join(cards)}</div></div>')
    elif not nodes:
        loose_block = ('<div id="loose"><div class="lhead">Nothing declared yet</div>'
                       '<div class="muted">An agent adds one with '
                       '<code>comms-graph task add &lt;id&gt; --title "what it is, '
                       'in plain words"</code>.</div></div>')
    else:
        loose_block = ""

    legend_rows = []
    for phase, (border, fill, meaning) in _PHASE_STYLE.items():
        if not any(n["phase"] == phase for n in nodes) and summary["total"]:
            continue
        legend_rows.append(
            f'<div class="row"><span class="dot" style="background:{fill};'
            f'box-shadow:0 0 0 2px {border}"></span>'
            f'<span><span class="slug">{phase}</span> '
            f'<span class="muted">: {html.escape(meaning)}</span></span></div>'
        )

    blocked = sorted((t for t in tasks.values() if t.phase == _task.PHASE_BLOCKED),
                     key=lambda t: t.id)
    blocked_block = ""
    if blocked:
        rows = "".join(
            f'<div class="row"><span class="slug">{html.escape(t.id)}</span>'
            f'<span class="muted">waits on</span>'
            f'<span class="blocked-on">{html.escape(", ".join(t.blocked_by))}</span></div>'
            for t in blocked
        )
        blocked_block = f'<div class="sec">Blocked, and on what</div>{rows}'

    in_review = sorted((t for t in tasks.values() if t.phase == _task.PHASE_REVIEW),
                       key=lambda t: t.id)
    review_block = ""
    if in_review:
        rows = "".join(
            f'<div class="row"><span class="slug">{html.escape(t.id)}</span>'
            f'<span class="muted">done by @{html.escape(t.did)}: needs somebody else</span></div>'
            for t in in_review
        )
        review_block = (f'<div class="sec">Waiting on review</div>{rows}'
                        '<div class="note">Each of these blocks everything after it.</div>')

    # A picture of nodes with no edges is not a graph, it is a list drawn
    # badly — and it invites the reader to look for connections that are not
    # there. Measured on the real store: 8 tasks, 0 task_edge events, ever. Say
    # so, and say what would change it, rather than presenting a column of boxes
    # as though the layout meant something.
    # Nothing is broken when no edges exist, so this is stated, not alarmed
    # about. It used to be red, over an empty canvas, next to a column of boxes
    # nobody could read — three separate ways of implying a fault that was not
    # there.
    if not edges and nodes:
        warning = ('<div class="warn info">Nobody has said any of these tasks waits on '
                   'another, so there is no graph to draw yet. They are listed below.<br>'
                   'An agent connects two with '
                   '<code>comms-graph task edge &lt;first&gt; &lt;second&gt; '
                   '--kind consumes --provides "what the second one uses"</code>.</div>')
    else:
        warning = ""
    if summary["cycles"]:
        warning = (f'<div class="warn">{summary["cycles"]} task(s) are in a dependency loop '
                   "and can never start. Nothing downstream of them can either.</div>")
    if summary["dangling"]:
        warning += (f'<div class="warn">{summary["dangling"]} edge(s) name a task that was '
                    "never declared. They are not drawn, and they do not block anything.</div>")

    page = _PAGE.format(
        headline=html.escape(_headline(summary)),
        warning=warning,
        blocked_block=blocked_block,
        review_block=review_block,
        legend="".join(legend_rows) or '<div class="empty">Nothing to show yet.</div>',
        nodes_json=_json_for_script(drawn),
        loose_block=loose_block,
        edges_json=_json_for_script(edges),
        generated=html.escape(generated or "now"),
    )
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return TaskViewResult(output_path=out, task_count=summary["total"],
                          edge_count=len(edges), blocked=summary["blocked"],
                          cycles=summary["cycles"])
