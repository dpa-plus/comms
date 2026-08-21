"""A local dashboard: both graphs, the live claims, and who is here.

WHY A SERVER AND NOT JUST FILES. ``view.py`` and ``taskview.py`` each write a
complete standalone page, which is the right shape for sending somebody a
snapshot. What they cannot do is change when the log changes — and the whole
value of a coordination board is that you leave it open while agents work. This
adds the one thing files cannot have: it notices a write and pushes.

WHY THE TWO GRAPHS ARE IFRAMES. Each is a finished page with its own vis-network
instance, its own layout, and its own interaction state. Inlining them into one
document would mean two vis instances fighting over one global, and — worse —
every push would have to avoid destroying pan, zoom and selection in both of
them. An iframe reloads only when its own data changed, and its internal state
is its own business. The cost is two extra requests; the alternative was
re-implementing both graphs.

BINDS TO LOOPBACK ONLY, and deliberately has no mutating endpoints. The board
shows what the log says; anything that changes the log goes through the CLI,
where the lock and the fold enforce the rules. A dashboard button that could
release somebody else's claim would be a second writer with none of those
guarantees.
"""

from __future__ import annotations

import html
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import log as _log
from . import state as _state
from . import task as _task

#: How often the watcher stats the log. The log is appended under a lock and a
#: human reading a board does not need sub-second latency, so this is chosen to
#: be invisible to a reader while costing about two syscalls a second.
POLL_SECONDS = 0.5

#: How long a holder must be silent before their claim reads as quiet. One hour,
#: matching the Go build's stale rule, and deliberately advisory: quiet is a
#: prompt to look, never a licence for the board to act. Nothing expires on its
#: own — a claim ends only when somebody names it in a release or a steal.
QUIET_AFTER_SECONDS = 3600


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _string(data: Any, key: str) -> str:
    """One field out of an event payload, as a string, whatever is in there.

    Event data is agent-written and only conventionally shaped: a key can be
    missing, null, a number, or a nested object. The board must render it
    either way — this is the surface that must never go blank.
    """
    if not isinstance(data, dict):
        return ""
    value = data.get(key)
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value)


def _snapshot(root: Path, log_file: Path) -> dict:
    """Everything the board shows, as plain JSON.

    Never raises: a board that goes blank because one field could not be read is
    worse than a board reporting that it could not read it.
    """
    try:
        events = _log.read(log_file)
    except Exception as exc:
        return {"error": f"the coordination log could not be read: {exc}",
                "generated": _now_text()}
    st = _state.fold(events)

    # WHEN A CLAIM IS "QUIET", and why it is not simply "old".
    #
    # A claim's timestamp is when the ground was TAKEN, never when it was last
    # touched, so ageing claims out by their own timestamp calls a legitimate
    # three-hour task abandoned. What actually indicates nobody is coming back
    # is the HOLDER going silent. So: quiet when neither the claim nor anything
    # else from its holder has happened for QUIET_AFTER.
    #
    # Computed here and not in the fold. fold() is pure and total and runs in
    # front of every agent edit; a clock inside it would make the same log fold
    # differently at different moments.
    now = datetime.now(timezone.utc)
    last_by_actor: dict[str, datetime] = {}
    for s in st.sessions.values():
        seen = s.last_seen or s.ts
        if seen is not None:
            prev = last_by_actor.get(s.actor)
            if prev is None or seen > prev:
                last_by_actor[s.actor] = seen

    claims = []
    for c in sorted(st.claims.values(), key=lambda c: c.ts, reverse=True):
        heard = c.ts
        seen = last_by_actor.get(c.actor)
        if seen is not None and seen > heard:
            heard = seen
        idle = max(0.0, (now - heard).total_seconds())
        claims.append({
            "id": c.id, "actor": c.actor, "scope": str(c.scope),
            "intent": c.intent, "task": c.task,
            "ts": c.ts.isoformat().replace("+00:00", "Z"),
            "idle_seconds": int(idle),
            "quiet": idle >= QUIET_AFTER_SECONDS,
            # Ground that was taken OFF somebody. Recorded, and shown nowhere.
            "stolen_from": getattr(c, "stolen_from_id", ""),
            "steal_reason": getattr(c, "steal_reason", ""),
        })

    # WHO IS HERE is everyone who has ACTED, not everyone who said hello.
    #
    # A hello is only written when the host reports a session id, which in
    # practice means Claude Code. So an agent on any other harness could claim,
    # release and get refused all day while the panel said "Nobody has said
    # hello in this repo" — a board reporting an empty room to somebody watching
    # four agents work in it.
    #
    # A hello still matters and is still shown: it is what ties an actor to a
    # process, which is what the review gate compares. An actor without one is
    # present but unidentified, and the panel says which.
    acted: dict[str, datetime] = {}
    for ev in events:
        if not ev.actor:
            continue
        prev = acted.get(ev.actor)
        if prev is None or ev.ts > prev:
            acted[ev.actor] = ev.ts

    roster = []
    for actor in sorted(set(acted) | set(st.sessions)):
        s = st.sessions.get(actor)
        last = acted.get(actor)
        if s is not None:
            hello_seen = s.last_seen or s.ts
            if hello_seen is not None and (last is None or hello_seen > last):
                last = hello_seen
        roster.append({
            "actor": actor,
            "label": getattr(s, "label", "") if s else "",
            "vendor": getattr(s, "vendor", "") if s else "",
            "host": getattr(s, "hostname", "") if s else "",
            "identified": s is not None,
            "last_seen": last.isoformat().replace("+00:00", "Z") if last else "",
            "holding": sum(1 for c in st.claims.values() if c.actor == actor),
        })

    tasks = []
    for t in sorted(st.tasks.values(), key=lambda t: t.id):
        tasks.append({
            "id": t.id, "title": t.title, "phase": t.phase,
            "doers": t.doers, "did": t.did, "verified_by": t.verified_by,
            "independence": t.independence,
            "blocked_by": t.blocked_by, "rejections": t.rejections,
            # The facts a person needs to answer "is this stuck, and was it
            # really checked". Every one of these is already in the fold and
            # none of them reached the page: `outstanding` is WHO the task is
            # waiting on, which is the difference between a blocked board and
            # an actionable one, and `verification` is what the reviewer says
            # they ran, which is the difference between a sign-off and a tick.
            "outstanding": list(getattr(t, "outstanding", []) or []),
            "verification": getattr(t, "verification", ""),
            "checks": list(getattr(t, "checks", []) or []),
            "check_results": dict(getattr(t, "check_results", {}) or {}),
            "slots": getattr(t, "slots", 1),
            "ever_verified": bool(getattr(t, "ever_verified", False)),
            "findings": [
                {"what": getattr(f, "what", ""), "where": getattr(f, "where", "")}
                for f in (getattr(t, "findings", []) or [])
            ],
        })

    # The log itself, most recent first, as a readable feed. The board had no
    # answer at all to "what just happened" — the one question somebody who has
    # been away asks first, and the log is nothing but the answer to it.
    feed = []
    for ev in events[-60:][::-1]:
        feed.append({
            "type": ev.type,
            "actor": ev.actor,
            "scope": (ev.scope or [""])[0] if ev.scope else "",
            "task": _string(ev.data, "task"),
            "state": _string(ev.data, "state"),
            "intent": _string(ev.data, "intent"),
            "reason": _string(ev.data, "reason"),
            "result": _string(ev.data, "result"),
            "body": _string(ev.data, "body"),
            "category": _string(ev.data, "category"),
            "steals": _string(ev.data, "steals"),
            "ts": ev.ts.isoformat().replace("+00:00", "Z"),
        })

    # Things that are WRONG with the plan itself, as opposed to slow. Both are
    # computed by the task-graph page and both live inside its side panel,
    # which the board hides to give the drawing room — so a dependency loop, the
    # one state a plan cannot recover from on its own, was invisible on the
    # surface somebody actually watches.
    alerts = []
    cycles = [t["id"] for t in tasks if t["phase"] == _task.PHASE_CYCLE]
    if cycles:
        alerts.append({
            "kind": "cycle",
            "text": f"{len(cycles)} task(s) in a dependency loop: " + ", ".join(cycles[:6]),
            "hint": "Nothing in the loop can ever start. An edge has to go.",
        })
    declared = {t["id"] for t in tasks}
    dangling = sorted({
        e.from_ if e.from_ not in declared else e.to
        for e in (st.task_edges or [])
        if e.from_ not in declared or e.to not in declared
    })
    if dangling:
        alerts.append({
            "kind": "dangling",
            "text": f"{len(dangling)} edge(s) name a task that was never declared: "
                    + ", ".join(dangling[:6]),
            "hint": "Ordering was recorded against something that does not exist.",
        })
    stuck = [t for t in tasks if t["phase"] == _task.PHASE_REVIEW]
    if stuck:
        alerts.append({
            "kind": "review",
            "text": f"{len(stuck)} task(s) finished and waiting for somebody to check them: "
                    + ", ".join(t["id"] for t in stuck[:6]),
            "hint": "Nothing downstream moves until one of these is verified.",
        })
    quiet_claims = [c for c in claims if c["quiet"]]
    if quiet_claims:
        alerts.append({
            "kind": "quiet",
            "text": f"{len(quiet_claims)} claim(s) held by somebody who has gone quiet: "
                    + ", ".join(c["scope"] + " (@" + c["actor"] + ")" for c in quiet_claims[:4]),
            "hint": "This is the one thing worth doing by hand. Free it with the "
                    "command on the claim, or leave it if they are still working.",
        })
    waiting = [t for t in tasks if t["outstanding"] and t["phase"] != _task.PHASE_CLOSED]
    if waiting:
        who = sorted({a for t in waiting for a in t["outstanding"]})
        alerts.append({
            "kind": "outstanding",
            "text": "held up by " + ", ".join("@" + a for a in who[:6])
                    + " on " + ", ".join(t["id"] for t in waiting[:4]),
            "hint": "They hold ground tagged to it and have not submitted.",
        })

    return {
        "root": str(root),
        "generated": _now_text(),
        "alerts": alerts,
        "claims": claims,
        "roster": roster,
        "tasks": tasks,
        "feed": feed,
        "counts": {
            "claims": len(claims),
            "agents": len(roster),
            "tasks": len(tasks),
            "blocked": sum(1 for t in tasks if t["phase"] == _task.PHASE_BLOCKED),
            "review": sum(1 for t in tasks if t["phase"] == _task.PHASE_REVIEW),
            "ready": sum(1 for t in tasks if t["phase"] == _task.PHASE_READY),
            "doing": sum(1 for t in tasks if t["phase"] == _task.PHASE_DOING),
            "closed": sum(1 for t in tasks if t["phase"] == _task.PHASE_CLOSED),
            "cycle": sum(1 for t in tasks if t["phase"] == _task.PHASE_CYCLE),
        },
        # `summary`, not `body`. The field was read by a name Finding does not
        # have, so every finding would have rendered as a category and a blank
        # line — and nothing caught it, because there was no way to write a
        # finding in this build for a test to have one to render.
        "findings": [
            {"actor": f.actor, "category": getattr(f, "category", ""),
             "body": getattr(f, "summary", ""),
             "priority": bool(getattr(f, "priority", False)),
             # kind AND value: a bare "src/auth.ts" and a bare "#321" are both
             # just strings, and which one it is is the useful half.
             "refs": [f"{getattr(r, 'kind', '')}:{getattr(r, 'value', '')}".strip(":")
                      for r in (getattr(f, "refs", []) or [])],
             "ts": f.ts.isoformat().replace("+00:00", "Z")}
            for f in list(st.findings)[-12:][::-1]
        ],
        "notes": [
            {"actor": n.actor, "body": getattr(n, "body", ""),
             "priority": bool(getattr(n, "priority", False)),
             "ts": n.ts.isoformat().replace("+00:00", "Z")}
            for n in list(getattr(st, "notes", []) or [])[-12:][::-1]
        ],
        # Carry the task and the reason too. A refusal recorded against a TASK
        # (a self-review, a failing check) has no scope, so the panel rendered
        # rows saying only "@alice was refused" — the two facts worth having,
        # what and why, were dropped on the way out. It also called those
        # "collisions prevented", which a failed check is not.
        "blocked_events": [
            {"actor": b.actor, "scope": getattr(b, "scope", ""),
             "holder": getattr(b, "holder", ""),
             "task": getattr(b, "task", ""),
             "reason": getattr(b, "reason", ""),
             "ts": b.ts.isoformat().replace("+00:00", "Z")}
            for b in list(st.blocked)[-12:][::-1]
        ],
    }


_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>comms</title>
<style>
  /* One dark surface, three depths: page, panel, row. Everything else is
     borrowed from those three so nothing has to be re-picked per component. */
  :root {
    color-scheme: dark;
    --page: #0f0f1a; --panel: #16162b; --line: #2a2a4e; --hair: #20203a;
    --ink: #e8eaf2; --dim: #8890b0; --code: #cfd8ff; --key: #f0c274;
    --ok: #7ad39a; --wait: #8ab4f8; --idle: #9aa0b4; --bad: #f28b82;
    /* 8pt rhythm. Every gap and pad below is one of these. */
    --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 24px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--page); color: var(--ink);
    font: 13px/1.5 ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif; }
  body { display: grid; grid-template-rows: auto auto minmax(0, 1fr); height: 100dvh; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .num { font-variant-numeric: tabular-nums; }

  header { display: flex; align-items: center; gap: var(--s3); padding: var(--s2) var(--s4);
    border-bottom: 1px solid var(--line); background: var(--panel); flex-wrap: wrap; }
  .brand { font-weight: 700; letter-spacing: .12em; text-transform: uppercase;
    font-size: 12px; color: var(--key); }
  .root { color: var(--dim); font-family: ui-monospace, Menlo, monospace; font-size: 11.5px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .spacer { margin-left: auto; }
  .chips { display: flex; gap: var(--s1); flex-wrap: wrap; }
  .chip { border: 1px solid var(--line); border-radius: 999px; padding: 1px var(--s2);
    font-size: 11.5px; color: var(--code); font-variant-numeric: tabular-nums;
    display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
  .chip .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
  .chip.off { opacity: .42; }
  .live { color: var(--ok); }
  .live.stale { color: var(--bad); }

  /* NEEDS YOU. Always present, so "nothing is wrong" is a statement the reader
     can see rather than an absence they have to infer from a missing band. */
  .needs { padding: var(--s2) var(--s4); border-bottom: 1px solid var(--line);
    background: #191527; display: flex; flex-direction: column; gap: var(--s1); }
  .needs.clear { background: var(--panel); }
  .needs h2 { margin: 0 0 2px; font-size: 10.5px; letter-spacing: .08em;
    text-transform: uppercase; color: var(--dim); font-weight: 600; }
  .alert { display: flex; gap: var(--s2); align-items: baseline; }
  /* A flex child will not shrink below its content unless told to, so the
     sentence ran off the right edge instead of wrapping — and the half that
     went missing was the half that says what to do about it. */
  .alert > span:last-child { min-width: 0; }
  .alert .tag { flex: none; font-size: 10px; letter-spacing: .06em; text-transform: uppercase;
    padding: 1px 6px; border-radius: 3px; border: 1px solid currentColor; }
  .a-cycle .tag, .a-dangling .tag { color: var(--bad); }
  .a-quiet .tag { color: var(--key); }
  .a-review .tag { color: var(--wait); }
  .a-outstanding .tag { color: var(--idle); }
  .alert .hint { color: var(--dim); }
  .allclear { color: var(--ok); display: flex; gap: var(--s2); align-items: baseline; }

  main { display: grid; grid-template-columns: minmax(0, 1fr) 340px; min-height: 0; }
  /* ONE graph at a time, the whole pane. Two of them stacked gave each half a
     screen, which is not enough for either: a task graph shrinks to unreadable
     labels and a code map of a few hundred nodes becomes a smudge. They are
     also answers to different questions, and nobody asks both at once. */
  .pane { position: relative; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
  .tabs { display: flex; gap: var(--s1); padding: var(--s2) var(--s3);
    border-bottom: 1px solid var(--line); background: var(--panel); flex: none; }
  .tab { background: none; border: 1px solid transparent; border-radius: 4px;
    color: var(--dim); font: inherit; font-size: 11px; letter-spacing: .06em;
    text-transform: uppercase; padding: 3px 10px; cursor: pointer; }
  .tab:hover { color: var(--ink); }
  .tab[aria-selected="true"] { color: var(--key); border-color: var(--line); background: #10101f; }
  .tab .sub { text-transform: none; letter-spacing: 0; color: var(--dim); margin-left: 6px;
    font-size: 11px; }
  /* Both stay mounted and full-size. Unmounting or display:none would make
     vis-network re-measure into a zero box on the way back, and remounting
     would throw away the pan and zoom the reader had set. */
  .frames { position: relative; flex: 1 1 auto; min-height: 0; }
  .frame { position: absolute; inset: 0; }
  .frame[hidden] { visibility: hidden; pointer-events: none; }
  .frame > iframe { width: 100%; height: 100%; border: 0; display: block; }
  aside { border-left: 1px solid var(--line); background: var(--panel); overflow-y: auto;
    padding: var(--s3) var(--s4); min-width: 0; }
  .sec { font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase;
    color: var(--dim); margin: var(--s5) 0 var(--s2); font-weight: 600; }
  .sec:first-child { margin-top: 0; }
  .row { padding: var(--s2) 0; border-bottom: 1px solid var(--hair); }
  .row:last-child { border-bottom: 0; }
  .who { color: var(--key); font-family: ui-monospace, Menlo, monospace; }
  .scope { font-family: ui-monospace, Menlo, monospace; word-break: break-all; color: var(--code); }
  .muted { color: var(--dim); }
  .empty { color: var(--dim); padding: var(--s2) 0; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    margin-right: 6px; vertical-align: 1px; flex: none; }
  .err { background: #3a1d1b; border: 1px solid var(--bad); color: #f6b0aa;
    padding: var(--s2) var(--s3); border-radius: 5px; margin-bottom: var(--s3); }
  .flag { font-size: 10px; letter-spacing: .05em; text-transform: uppercase;
    border: 1px solid currentColor; border-radius: 3px; padding: 0 4px; margin-left: 6px; }
  .flag.quiet { color: var(--key); }
  .flag.unknown { color: var(--idle); }
  /* The one action. A command to copy, not a button — the CLI stays the only
     writer, so the board hands over the exact words and gets out of the way. */
  .cmd { display: block; width: 100%; margin-top: 6px; text-align: left;
    background: #10101f; border: 1px solid var(--line); border-radius: 4px;
    color: var(--code); font-family: ui-monospace, Menlo, monospace; font-size: 11px;
    padding: 5px 7px; cursor: pointer; word-break: break-all; }
  .cmd:hover { border-color: var(--key); color: var(--key); }
  .cmd.copied { border-color: var(--ok); color: var(--ok); }
  .feed .row { display: flex; gap: var(--s2); align-items: baseline; }
  .cid { font-size: 10.5px; opacity: .6; }
  .checks { display: flex; flex-wrap: wrap; gap: var(--s1); margin-top: 5px; }
  .check { font-size: 10px; letter-spacing: .04em; border: 1px solid currentColor;
    border-radius: 3px; padding: 0 5px; white-space: nowrap; }
  .check.pass { color: var(--ok); }
  .check.fail { color: var(--bad); }
  .check.none { color: var(--idle); }
  .feed .when { color: var(--dim); font-variant-numeric: tabular-nums; flex: none;
    font-size: 11.5px; min-width: 52px; }
  .feed .what { min-width: 0; }
  .verb { font-size: 10px; letter-spacing: .05em; text-transform: uppercase; }

  @media (max-width: 1100px) {
    body { height: auto; min-height: 100dvh; }
    main { grid-template-columns: minmax(0, 1fr); }
    .pane { height: 68vh; }
    aside { border-left: 0; border-top: 1px solid var(--line); }
  }
</style>
<header>
  <span class="brand">comms</span>
  <span class="root mono" id="root"></span>
  <span class="spacer"></span>
  <span class="chips" id="chips"></span>
  <span class="chip live" id="live">connecting</span>
</header>
<section class="needs clear" id="needs"></section>
<main>
  <div class="pane">
    <div class="tabs" role="tablist">
      <button class="tab" role="tab" id="tab-tasks" aria-selected="true" aria-controls="frame-tasks"
        >Work<span class="sub">what is moving, and what is stuck</span></button>
      <button class="tab" role="tab" id="tab-map" aria-selected="false" aria-controls="frame-map"
        >Code<span class="sub">who is standing where</span></button>
    </div>
    <div class="frames">
      <div class="frame" id="frame-tasks"><iframe id="tasks" src="/tasks.html" title="task graph"></iframe></div>
      <div class="frame" id="frame-map" hidden><iframe id="map" src="/map.html" title="code map"></iframe></div>
    </div>
  </div>
  <aside id="side"></aside>
</main>
<script>
var lastStamp = "";
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"\']/g, function (ch) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "\'": "&#39;" }[ch];
  });
}
var PHASE = {
  ready:   { color: "#7ad39a", word: "ready" },
  doing:   { color: "#f0c274", word: "doing" },
  review:  { color: "#8ab4f8", word: "needs a check" },
  blocked: { color: "#9aa0b4", word: "blocked" },
  closed:  { color: "#4a5568", word: "done" },
  cycle:   { color: "#f28b82", word: "in a loop" }
};
function ago(iso) {
  var t = Date.parse(iso);
  if (!t) return "";
  var s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}
function chip(label, n, color, dim) {
  return '<span class="chip' + (dim ? " off" : "") + '">' +
    (color ? '<span class="dot" style="background:' + color + '"></span>' : "") +
    esc(label) + " " + n + "</span>";
}

function renderNeeds(d) {
  var el = document.getElementById("needs");
  var alerts = d.alerts || [];
  if (!alerts.length) {
    el.className = "needs clear";
    el.innerHTML = '<div class="allclear"><span>&#10003;</span>' +
      "<span>Nothing needs you. No loops, nothing waiting on a check, " +
      "nobody holding ground they have gone quiet on.</span></div>";
    return;
  }
  el.className = "needs";
  var h = "<h2>Needs you</h2>";
  alerts.forEach(function (a) {
    h += '<div class="alert a-' + esc(a.kind) + '"><span class="tag">' +
      esc(a.kind) + '</span><span>' + esc(a.text) +
      ' <span class="hint">' + esc(a.hint || "") + "</span></span></div>";
  });
  el.innerHTML = h;
}

function renderChips(d) {
  var c = d.counts || {};
  var h = "";
  ["cycle", "blocked", "review", "doing", "ready", "closed"].forEach(function (k) {
    var n = c[k] || 0;
    // Zero stays on the rail, dimmed. A count that disappears when it hits zero
    // makes the reader re-scan the row to work out which one is missing.
    h += chip(PHASE[k].word, n, PHASE[k].color, n === 0);
  });
  h += chip("agents", c.agents || 0, "", false);
  document.getElementById("chips").innerHTML = h;
}

function claimRow(x) {
  var h = '<div class="row"><div class="scope">' + esc(x.scope);
  if (x.quiet) h += '<span class="flag quiet">quiet</span>';
  h += "</div>";
  h += '<div class="muted"><span class="who">@' + esc(x.actor) + "</span> " +
       esc(x.intent || "") +
       (x.task ? ' <span class="mono">[' + esc(x.task) + "]</span>" : "") +
       ' <span class="num">' + ago(x.ts) + " ago</span></div>";
  // The id, on every row. It is the one string nobody retypes from memory and
  // the only handle for releasing or stealing the ground it names.
  h += '<div class="muted mono cid">' + esc(x.id) + "</div>";
  if (x.steal_reason) {
    h += '<div class="muted">taken over &mdash; ' + esc(x.steal_reason) + "</div>";
  }
  if (x.quiet) {
    // The exact words, because the id is the one thing nobody can retype from
    // memory and this is the only action a person takes here.
    var cmd = 'comms-graph release ' + x.id + ' --as me --force --reason "why they are gone"';
    h += '<button class="cmd" data-cmd="' + esc(cmd) + '" title="copy">' + esc(cmd) + "</button>";
  }
  return h + "</div>";
}

function taskRow(t) {
  var p = PHASE[t.phase] || { color: "#8890b0", word: t.phase };
  var h = '<div class="row"><span class="dot" style="background:' + p.color + '"></span>' +
    '<span class="scope">' + esc(t.id) + '</span> <span class="muted">' + esc(p.word) + "</span>";
  if (t.title) h += '<div class="muted">' + esc(t.title) + "</div>";
  if (t.outstanding && t.outstanding.length) {
    h += '<div class="muted">waiting on ' +
      t.outstanding.map(function (a) { return '<span class="who">@' + esc(a) + "</span>"; }).join(", ") +
      " to submit</div>";
  } else if (t.blocked_by && t.blocked_by.length) {
    h += '<div class="muted">after ' + esc(t.blocked_by.join(", ")) + "</div>";
  }
  if (t.verified_by) {
    // Three different claims, and they must not read alike: checked by somebody
    // else, checked by somebody with the same blind spots, and signed off by
    // its own author.
    if (t.independence === "self-acknowledged") {
      h += '<div class="muted">by @' + esc(t.did) +
        ', <span style="color:#f0c274">signed off by themselves</span></div>';
    } else if (t.independence === "same-family") {
      h += '<div class="muted">by @' + esc(t.did) + ", checked by @" + esc(t.verified_by) +
        ' <span class="muted">(same model family)</span></div>';
    } else {
      h += '<div class="muted">by @' + esc(t.did) + ", checked by @" + esc(t.verified_by) + "</div>";
    }
    if (t.verification) {
      h += '<div class="muted">&ldquo;' + esc(t.verification) + "&rdquo;</div>";
    }
  } else if (t.did) {
    h += '<div class="muted">done by @' + esc(t.did) + " &mdash; needs somebody else</div>";
  }
  if (t.checks && t.checks.length) {
    // What the doer SAYS ran. Self-reported, so it reads as a claim they made
    // and not a verdict the tool reached — and a declared check with no result
    // is the interesting one, so it says "not reported" rather than vanishing.
    h += '<div class="checks">';
    t.checks.forEach(function (name) {
      var got = (t.check_results || {})[name];
      var ok = String(got).toLowerCase() === "pass";
      var cls = got === undefined ? "none" : (ok ? "pass" : "fail");
      h += '<span class="check ' + cls + '">' + esc(name) + " " +
           (got === undefined ? "not reported" : esc(String(got))) + "</span>";
    });
    h += "</div>";
  }

  if (t.rejections) {
    h += '<div class="muted num">sent back ' + t.rejections + "&times;</div>";
  }
  return h + "</div>";
}

var VERB = {
  claim:      { word: "claimed",  color: "#f0c274" },
  release:    { word: "released", color: "#7ad39a" },
  task:       { word: "declared", color: "#cfd8ff" },
  task_edge:  { word: "ordered",  color: "#cfd8ff" },
  task_state: { word: "moved",    color: "#8ab4f8" },
  finding:    { word: "found",    color: "#7ad39a" },
  note:       { word: "noted",    color: "#8890b0" },
  blocked:    { word: "refused",  color: "#f28b82" },
  hello:      { word: "arrived",  color: "#8890b0" }
};
function feedRow(e) {
  var v = VERB[e.type] || { word: e.type, color: "#8890b0" };
  var what = "";
  if (e.type === "task_state") what = (e.state || "moved") + " " + (e.task || "");
  else if (e.type === "finding") what = (e.category ? e.category + ": " : "") + (e.body || "");
  else if (e.type === "note") what = e.body || "";
  else if (e.type === "blocked") what = e.scope || e.task || "";
  else if (e.type === "release") what = e.result || "";
  else what = e.scope || e.task || e.intent || "";
  var h = '<div class="row"><span class="when num">' + ago(e.ts) + "</span>" +
    '<span class="what"><span class="who">@' + esc(e.actor) + "</span> " +
    '<span class="verb" style="color:' + v.color + '">' + esc(v.word) + "</span> " +
    '<span class="scope">' + esc(what) + "</span>";
  if (e.type === "blocked" && e.reason) h += ' <span class="muted">(' + esc(e.reason) + ")</span>";
  h += "</span></div>";
  return h;
}

function render(d) {
  document.getElementById("root").textContent = d.root || "";
  var side = document.getElementById("side");
  if (d.error) {
    // Blank the counts rather than leaving the last good ones next to an
    // error. A number that is confidently wrong is worse than no number.
    document.getElementById("chips").textContent = "counts unavailable";
    document.getElementById("needs").className = "needs";
    document.getElementById("needs").innerHTML =
      '<div class="alert a-cycle"><span class="tag">error</span><span>' +
      esc(d.error) + "</span></div>";
    side.innerHTML = '<div class="err">' + esc(d.error) + "</div>";
    return;
  }
  renderChips(d);
  renderNeeds(d);

  var h = "";
  h += '<div class="sec">Ground held right now</div>';
  if (!d.claims.length) h += '<div class="empty">Nobody is holding anything.</div>';
  else d.claims.forEach(function (x) { h += claimRow(x); });

  h += '<div class="sec">Work</div>';
  if (!d.tasks.length) h += '<div class="empty">No tasks declared yet.</div>';
  else d.tasks.forEach(function (t) { h += taskRow(t); });

  h += '<div class="sec">Who is here</div>';
  if (!d.roster.length) h += '<div class="empty">Nobody has done anything here yet.</div>';
  else d.roster.forEach(function (a) {
    h += '<div class="row"><span class="who">@' + esc(a.actor) + "</span>" +
      (a.vendor ? ' <span class="muted">' + esc(a.vendor) + "</span>" : "") +
      (a.identified ? "" : '<span class="flag unknown" title="no session on record">unidentified</span>') +
      '<div class="muted num">holding ' + a.holding + " &middot; last seen " + ago(a.last_seen) +
      " ago</div></div>";
  });

  var keep = (d.findings || []).concat(d.notes || []);
  if (keep.length) {
    h += '<div class="sec">Worth knowing</div>';
    // Priority first; both arrays arrive newest-first.
    keep.sort(function (a, b) { return (b.priority ? 1 : 0) - (a.priority ? 1 : 0); });
    keep.slice(0, 8).forEach(function (f) {
      h += '<div class="row">';
      if (f.category) {
        h += '<span class="check ' + (f.category === "bug" ? "fail" : "pass") + '">' +
             esc(f.category) + "</span> ";
      }
      h += esc(f.body || "");
      h += '<div class="muted"><span class="who">@' + esc(f.actor) + "</span> " +
           '<span class="num">' + ago(f.ts) + " ago</span>";
      if (f.refs && f.refs.length) {
        h += ' <span class="mono">' + esc(f.refs.join(" ")) + "</span>";
      }
      h += "</div></div>";
    });
  }

  h += '<div class="sec">Just happened</div>';
  h += '<div class="feed">';
  if (!d.feed || !d.feed.length) h += '<div class="empty">The log is empty.</div>';
  else d.feed.slice(0, 30).forEach(function (e) { h += feedRow(e); });
  h += "</div>";

  side.innerHTML = h;
}

// Copy the release command. Delegated, because the rail is replaced wholesale
// on every push and a listener bound to a row would not survive it.
document.addEventListener("click", function (ev) {
  var b = ev.target.closest && ev.target.closest(".cmd");
  if (!b) return;
  var text = b.getAttribute("data-cmd") || "";
  var done = function () {
    b.classList.add("copied");
    setTimeout(function () { b.classList.remove("copied"); }, 1200);
  };
  // Selecting the text is the fallback for EVERY failure, not just a missing
  // API. writeText also rejects without user activation and on a denied
  // permission, and swallowing that left a button that visibly did nothing —
  // worse than no button, because the reader thinks the command is on their
  // clipboard and pastes whatever was there before.
  var selectIt = function () {
    var sel = window.getSelection();
    var r = document.createRange();
    r.selectNodeContents(b);
    sel.removeAllRanges();
    sel.addRange(r);
    b.classList.add("copied");
    setTimeout(function () { b.classList.remove("copied"); }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, selectIt);
  } else {
    selectIt();
  }
});

// Which graph is showing. Remembered, because a board is left open for hours
// and a reload that silently jumps back to the other one is a small theft.
var PANES = ["tasks", "map"];
function showPane(which) {
  if (PANES.indexOf(which) < 0) which = "tasks";
  PANES.forEach(function (name) {
    var on = name === which;
    document.getElementById("frame-" + name).hidden = !on;
    document.getElementById("tab-" + name).setAttribute("aria-selected", on ? "true" : "false");
  });
  try { localStorage.setItem("comms.pane", which); } catch (e) {}
}
PANES.forEach(function (name) {
  document.getElementById("tab-" + name).addEventListener("click", function () {
    showPane(name);
  });
});
(function () {
  var saved = null;
  try { saved = localStorage.getItem("comms.pane"); } catch (e) {}
  showPane(saved || "tasks");
})();

function refreshFrames() {
  ["map", "tasks"].forEach(function (id) {
    var f = document.getElementById(id);
    // Cache-bust so the browser re-fetches the regenerated page rather than
    // showing the copy it already has.
    f.src = "/" + id + ".html?t=" + Date.now();
  });
}
var live = document.getElementById("live");
var es = new EventSource("/events");
es.onopen = function () { live.textContent = "live"; live.classList.remove("stale"); };
es.onerror = function () { live.textContent = "disconnected"; live.classList.add("stale"); };
es.onmessage = function (ev) {
  var d = JSON.parse(ev.data);
  render(d);
  // Only reload the graphs when the LOG actually moved. They are expensive to
  // redraw and reloading them on every heartbeat would throw away the reader\'s
  // pan and zoom every few seconds.
  if (d.stamp && d.stamp !== lastStamp) {
    if (lastStamp) refreshFrames();
    lastStamp = d.stamp;
  }
};
</script>
"""


#: Appended to each embedded page to give the graph the whole pane.
#:
#: Both pages are complete, standalone documents, and each carries its own
#: right-hand panel — the map has search, node info and a community filter; the
#: task graph has its counts and legend. Standalone that is exactly right. Side
#: by side inside the board, with the board's own rail beside them, it meant
#: THREE columns of chrome saying overlapping things, and the graphs — the
#: reason anybody opens this — were squeezed into what was left.
#:
#: Hidden rather than removed, so both pages keep working on their own and
#: neither generator has to know it is inside a frame.
_EMBED_CSS = (
    "<style>"
    "#comms-panel,#sidebar,#side{display:none!important}"
    # Collapse the TRACK the panel occupied, not just the panel. #wrap is a grid
    # with a fixed second column, so hiding #side left a 290px empty track and
    # the graph stayed short by exactly that much — the picture then read as
    # small and left-biased in a pane that looked like it had room.
    "#wrap{grid-template-columns:minmax(0,1fr)!important}"
    "#graph,#wrap{width:100%!important;flex:1 1 auto!important}"
    "body{overflow:hidden!important}"
    "</style>"
    # Hiding the panel widens the canvas, and an ELEMENT resize fires no window
    # resize event — so vis kept the narrower viewport it had measured at
    # construction and the picture sat small and off-centre in a pane with room
    # to spare. Both pages already re-fit on window resize; this gives them the
    # event they were waiting for, and a ResizeObserver keeps it true afterwards
    # when the browser window changes or a pane is dragged.
    "<script>(function(){"
    "var g=document.getElementById('graph')||document.body;"
    "var fire=function(){window.dispatchEvent(new Event('resize'));"
    "var n=window.commsTaskNetwork||window.network;"
    # setSize BEFORE redraw: vis measured its canvas when the panel was still
    # there, and fit() centres inside the CANVAS, not inside the container. So
    # the picture sat centred in a box narrower than the pane and read as
    # left-biased and small. redraw() alone does not re-measure — setSize is
    # what makes it look again.
    "if(n){try{n.setSize('100%','100%');n.redraw();"
    "if(n.fit){n.fit({animation:false});}}catch(e){}}};"
    "if(window.ResizeObserver){new ResizeObserver(fire).observe(g);}"
    "setTimeout(fire,0);setTimeout(fire,120);setTimeout(fire,400);"
    "})();</script>"
)


class _Handler(BaseHTTPRequestHandler):
    server_version = "comms"

    # Silence the default one-line-per-request logging: this serves a handful of
    # requests a second while somebody watches it, and the noise buries anything
    # that actually matters.
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The reader navigated away mid-response. Not an error worth noise.
            pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        board = self.server.board  # type: ignore[attr-defined]
        if path == "/":
            self._send(board.page().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send(json.dumps(board.snapshot()).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/map.html":
            self._send(board.map_html().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/tasks.html":
            self._send(board.tasks_html().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/events":
            self._stream(board)
        else:
            self._send(b"not found", "text/plain; charset=utf-8", 404)

    def _stream(self, board) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = None
        try:
            while True:
                stamp = board.stamp()
                snap = board.snapshot()
                snap["stamp"] = stamp
                payload = "data: " + json.dumps(snap) + "\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
                last = stamp
                # Wait for the log to move, but wake up periodically anyway so
                # the connection is proven alive and "seen 40s ago" keeps
                # counting up rather than freezing at whatever it said last.
                # A floor, ALWAYS paid, even when the log has already moved
                # again. Without it a busy repository turned this into a tight
                # loop that re-read and re-folded the whole log as fast as the
                # CPU allowed, for every connected browser.
                time.sleep(POLL_SECONDS)
                waited = POLL_SECONDS
                while board.stamp() == last and waited < 10.0:
                    time.sleep(POLL_SECONDS)
                    waited += POLL_SECONDS
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The reader closed the tab. Entirely normal for a stream somebody
            # leaves open all day, and it used to print a traceback on the
            # terminal running the board, which reads as something being wrong.
            self.close_connection = True
            return


class Board:
    """Holds what the pages need and regenerates them only when the log moves."""

    def __init__(self, root: Path, log_file: Path, graph_file: str | None = None) -> None:
        self.root = Path(root)
        self.log_file = Path(log_file)
        self.graph_file = graph_file
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, str]] = {}
        #: One lock per page key, so a slow map build does not hold up the task
        #: frame. Guarded by _lock while being created.
        self._builders: dict[str, threading.Lock] = {}

    def stamp(self) -> str:
        """A cheap value that changes when anything the pages are built from does.

        The log alone was not enough. The map is drawn from graphify-out/graph.json
        as well, so re-running `graphify extract` changed the picture completely
        while the stamp sat still — and the board went on serving the map it had
        cached, with no way for the reader to tell it was looking at the old
        shape of their codebase.
        """
        parts = []
        for path in (self.log_file, self._graph_path()):
            try:
                st = path.stat()
                parts.append(f"{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append("absent")
        return "|".join(parts)

    def _graph_path(self) -> Path:
        if self.graph_file:
            return Path(self.graph_file)
        return self.root / "graphify-out" / "graph.json"

    def snapshot(self) -> dict:
        return _snapshot(self.root, self.log_file)

    def page(self) -> str:
        # Returned verbatim. _PAGE is NOT a format string — it is full of CSS and
        # JavaScript braces, and an earlier version doubled them for a .format()
        # call that never happened, so the doubled braces shipped literally and
        # every rule and script block in the page was invalid.
        return _PAGE

    def _cached(self, key: str, build) -> str:
        """One builder at a time per page, because building is not side-effect free.

        Each build RENDERS TO A FILE and reads it back. Two requests missing the
        cache together — a browser fetching both frames, or a reload landing on
        top of a push — had both builders writing the same path at once, and one
        of them read it half-written: HTTP 200 with a zero-byte body, so the
        frame went blank with nothing in any log to explain it.

        The second caller waits and then finds the first one's result, rather
        than repeating work that was already in flight.
        """
        stamp = self.stamp()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] == stamp:
                return hit[1]
            builder = self._builders.setdefault(key, threading.Lock())
        with builder:
            # Re-check under the build lock: whoever held it may have just
            # produced exactly what we came for.
            with self._lock:
                hit = self._cache.get(key)
                if hit and hit[0] == stamp:
                    return hit[1]
            body = build()
            with self._lock:
                self._cache[key] = (stamp, body)
            return body

    def map_html(self) -> str:
        def build() -> str:
            from . import view as _view

            out = self.root / "graphify-out" / "graph.comms.live.html"
            try:
                res = _view.render(self.root, out, graph_file=self.graph_file,
                                   log_file=self.log_file)
                body = Path(res.output_path).read_text(encoding="utf-8")
                # The map is a complete page with its own "who is here" panel.
                # Standalone that is exactly right; embedded here it sits beside
                # the board's own rail saying the same thing twice, and two
                # panels disagreeing during a refresh would be worse than one.
                # Hidden rather than removed, so the map keeps working on its own
                # and nothing in view.py has to know it is inside a frame.
                return body + _EMBED_CSS
            except Exception as exc:
                return _placeholder(
                    "No code map yet.",
                    "Build one with <code>graphify extract . --code-only</code> "
                    "— claims still record and still block without it.",
                    str(exc))
        return self._cached("map", build)

    def tasks_html(self) -> str:
        def build() -> str:
            from . import taskview as _taskview

            out = self.root / "graphify-out" / "tasks.comms.html"
            try:
                st = _state.fold(_log.read(self.log_file))
                res = _taskview.render(st, out, generated=_now_text())
                return Path(res.output_path).read_text(encoding="utf-8") + _EMBED_CSS
            except Exception as exc:
                return _placeholder("The task graph could not be drawn.", "", str(exc))
        return self._cached("tasks", build)


def _placeholder(title: str, hint: str, detail: str = "") -> str:
    return (
        '<!doctype html><meta charset="utf-8">'
        '<style>html,body{margin:0;height:100%;background:#0f0f1a;color:#8890b0;'
        'display:grid;place-items:center;text-align:center;'
        'font:13px/1.6 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}'
        'code{background:#24243f;padding:1px 5px;border-radius:3px}'
        'div{max-width:52ch;padding:20px}small{opacity:.65}</style>'
        f"<div><strong>{html.escape(title)}</strong><br>{hint}"
        + (f"<br><br><small>{html.escape(detail)}</small>" if detail else "")
        + "</div>"
    )


def serve(root: Path, log_file: Path, host: str = "127.0.0.1", port: int = 7878,
          graph_file: str | None = None) -> ThreadingHTTPServer:
    """Start the board. Returns the server so a caller can shut it down."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    httpd.board = Board(root, log_file, graph_file)  # type: ignore[attr-defined]
    return httpd
