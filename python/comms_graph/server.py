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
    # From EVERY event the actor wrote, not from their hello. A hello only
    # exists when the host reports a session id, which in practice means Claude
    # Code — so an agent on any other harness had no `last_seen` at all, and
    # its claim went quiet an hour after it was taken no matter how hard that
    # agent was working. The board then contradicted itself on one screen
    # ("last seen 10 seconds ago" beside "idle 3 hours") and its one
    # recommended manual action was to take live ground off a working agent.
    #
    # The roster was switched to this rule and this computation was not. They
    # read from the same map now, so they cannot disagree again.
    last_by_actor: dict[str, datetime] = {}
    for ev in events:
        if not ev.actor:
            continue
        prev = last_by_actor.get(ev.actor)
        if prev is None or ev.ts > prev:
            last_by_actor[ev.actor] = ev.ts

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
    acted = last_by_actor

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
            # none of them reached the page. `verification` is what the
            # reviewer says they ran, which is the difference between a
            # sign-off and a tick.
            "verification": getattr(t, "verification", ""),
            "checks": list(getattr(t, "checks", []) or []),
            "check_results": dict(getattr(t, "check_results", {}) or {}),
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
            # What the doer SAID ran, on the event that said it. The task
            # carries the latest set; the stream is where "they submitted, and
            # here is what they claim they checked" actually belongs.
            "checks": ({str(k): str(v) for k, v in ev.data["checks"].items()
                        if isinstance(k, str)}
                       if isinstance(ev.data, dict) and isinstance(ev.data.get("checks"), dict)
                       else {}),
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

    # Every project on this machine, so the board can be a place you watch
    # rather than a page you open per repo. Filesystem only — folding 180 logs
    # to draw a sidebar would cost seconds and the sidebar only needs a name
    # and a recency.
    here = _log.repo_hash(root)
    projects = []
    for st_info in _log.known_stores():
        idle = max(0.0, now.timestamp() - st_info["modified"])
        projects.append({
            "key": st_info["key"],
            "name": st_info["name"] or "",
            "root": st_info["root"],
            "exists": st_info["exists"],
            "bytes": st_info["bytes"],
            "idle_seconds": int(idle),
            "current": st_info["key"] == here,
            # Three bands, because a flat list of 180 is not a sidebar. Live
            # in the last hour, quiet today, everything else is history.
            "band": ("active" if idle < 3600
                     else "quiet" if idle < 86400
                     else "low"),
        })

    return {
        "root": str(root),
        "generated": _now_text(),
        "alerts": alerts,
        "projects": projects,
        "store_key": here,
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
             # kind AND value: a bare "src/auth.ts" and a bare "#321" are both
             # just strings, and which one it is is the useful half.
             "refs": [f"{getattr(r, 'kind', '')}:{getattr(r, 'value', '')}".strip(":")
                      for r in (getattr(f, "refs", []) or [])],
             "ts": f.ts.isoformat().replace("+00:00", "Z")}
            for f in list(st.findings)[-12:][::-1]
        ],
        "notes": [
            {"actor": n.actor, "body": getattr(n, "body", ""),
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>comms</title>
<style>

/* ============================================================
   comms / activity-first
   One live stream is the product. Everything else is a rail.
   Palette: warm paper + warm charcoal. Colour is SEMANTIC only:
     blue  = current / in progress   (also the brand accent)
     amber = needs a human           (findings, awaiting-review)
     red   = blocked / stale / error
     green = done
     grey  = inert information       (notes, ready, archive)
   ============================================================ */

/* ---- LIGHT (default) ---- */
:root {
  color-scheme: light;

  --bg:          #efece5;
  --surface:     #fbfaf7;
  --surface-2:   #f3f1ea;
  --surface-3:   #e8e4da;
  --line:        #ddd8cc;
  --line-2:      #c7c0b0;
  --line-hair:   #e6e2d8;

  --ink:         #1b1a17;
  --ink-2:       #4b473f;
  --ink-3:       #7a7368;
  --ink-4:       #9a9284;

  --accent:      #1550c4;
  --accent-2:    #0f3f9e;
  --accent-wash: #e2e9fb;
  --accent-line: #b9cbf4;
  --on-accent:   #ffffff;

  --amber:       #a35a06;
  --amber-wash:  #fbeedb;
  --amber-line:  #ecd3a8;

  --red:         #b32218;
  --red-wash:    #fae5e2;
  --red-line:    #efc3bd;

  --green:       #17683f;
  --green-wash:  #dfeee4;
  --green-line:  #b7d9c4;

  --shadow-1: 0 1px 0 rgba(27,26,23,.04);
  --shadow-2: 0 1px 2px rgba(27,26,23,.06), 0 8px 20px rgba(27,26,23,.05);
  --ring: 0 0 0 2px var(--bg), 0 0 0 4px var(--accent);

  /* 8pt scale + one half step */
  --sp-h: 4px;
  --sp-1: 8px;
  --sp-2: 16px;
  --sp-3: 24px;
  --sp-4: 32px;
  --sp-6: 48px;

  --shell-max: 1560px;
  --rail-l: 244px;
  --rail-r: 328px;

  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

/* ---- DARK ---- */
:root[data-theme="dark"] {
  color-scheme: dark;

  --bg:          #131211;
  --surface:     #1a1917;
  --surface-2:   #211f1d;
  --surface-3:   #2a2724;
  --line:        #2e2b27;
  --line-2:      #443f39;
  --line-hair:   #262320;

  --ink:         #f2efe9;
  --ink-2:       #cbc5bb;
  --ink-3:       #968f84;
  --ink-4:       #756e64;

  --accent:      #6ea2ff;
  --accent-2:    #9dc0ff;
  --accent-wash: #16233c;
  --accent-line: #2c4573;
  --on-accent:   #0c1526;

  --amber:       #f0ab48;
  --amber-wash:  #2e2213;
  --amber-line:  #4d3a1c;

  --red:         #ff7d6e;
  --red-wash:    #2f1815;
  --red-line:    #55261f;

  --green:       #55c98a;
  --green-wash:  #12281c;
  --green-line:  #23472f;

  --shadow-1: 0 1px 0 rgba(0,0,0,.3);
  --shadow-2: 0 1px 2px rgba(0,0,0,.45), 0 10px 26px rgba(0,0,0,.35);
  --ring: 0 0 0 2px var(--bg), 0 0 0 4px var(--accent);
}

/* system-preference fallback when the operator has not chosen */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]):not([data-theme="dark"]) {
    color-scheme: dark;
    --bg:#131211; --surface:#1a1917; --surface-2:#211f1d; --surface-3:#2a2724;
    --line:#2e2b27; --line-2:#443f39; --line-hair:#262320;
    --ink:#f2efe9; --ink-2:#cbc5bb; --ink-3:#968f84; --ink-4:#756e64;
    --accent:#6ea2ff; --accent-2:#9dc0ff; --accent-wash:#16233c; --accent-line:#2c4573; --on-accent:#0c1526;
    --amber:#f0ab48; --amber-wash:#2e2213; --amber-line:#4d3a1c;
    --red:#ff7d6e; --red-wash:#2f1815; --red-line:#55261f;
    --green:#55c98a; --green-wash:#12281c; --green-line:#23472f;
    --shadow-1:0 1px 0 rgba(0,0,0,.3);
    --shadow-2:0 1px 2px rgba(0,0,0,.45), 0 10px 26px rgba(0,0,0,.35);
  }
}

* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  overflow: hidden;
}
.num { font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1; }
.mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
[hidden] { display: none !important; }
::selection { background: var(--accent-wash); }

/* scrollbars: thin, quiet, present */
.scroll { overflow-y: auto; overflow-x: hidden; scrollbar-width: thin; scrollbar-color: var(--line-2) transparent; }
.scroll::-webkit-scrollbar { width: 10px; height: 10px; }
.scroll::-webkit-scrollbar-thumb { background: var(--line-2); border: 3px solid transparent; background-clip: content-box; border-radius: 99px; }
.scroll::-webkit-scrollbar-track { background: transparent; }

/* ---------- top rail ---------- */
.topbar {
  height: 44px;
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  padding: 0 var(--sp-3);
  background: var(--surface);
  border-bottom: 1px solid var(--line);
  position: relative;
  z-index: 20;
}
.brand {
  font-size: 13px; font-weight: 700; letter-spacing: -0.01em;
  display: flex; align-items: center; gap: 7px;
  padding-right: var(--sp-1);
}
.brand .mark {
  width: 14px; height: 14px; border-radius: 2px;
  background: var(--accent);
  position: relative; flex: none;
}
.brand .mark::after {
  content: ""; position: absolute; left: 3px; top: 3px; width: 8px; height: 8px;
  border-radius: 1px; background: var(--surface);
}
.sep { width: 1px; height: 18px; background: var(--line); flex: none; }

.livedot {
  width: 7px; height: 7px; border-radius: 99px; background: var(--green); flex: none;
  box-shadow: 0 0 0 0 var(--green);
  animation: beat 2.4s ease-out infinite;
}
@keyframes beat {
  0% { box-shadow: 0 0 0 0 rgba(85,201,138,.45); }
  70% { box-shadow: 0 0 0 6px rgba(85,201,138,0); }
  100% { box-shadow: 0 0 0 0 rgba(85,201,138,0); }
}
.livedot.off { background: var(--ink-4); animation: none; }
.live {
  display: flex; align-items: center; gap: 7px;
  font-size: 11px; color: var(--ink-3); white-space: nowrap;
}
.grow { flex: 1 1 auto; min-width: var(--sp-2); }

.alarm {
  display: inline-flex; align-items: center; gap: 6px;
  height: 22px; padding: 0 8px 0 7px;
  border-radius: 3px;
  background: var(--red-wash); color: var(--red);
  border: 1px solid var(--red-line);
  font-size: 11px; font-weight: 650; letter-spacing: .02em;
  white-space: nowrap;
}
.alarm.warn { background: var(--amber-wash); color: var(--amber); border-color: var(--amber-line); }
.alarm b { font-weight: 700; }

button, .btn {
  font: inherit; font-size: 12px; font-weight: 550;
  color: var(--ink-2);
  background: var(--surface);
  border: 1px solid var(--line-2);
  border-radius: 4px;
  height: 26px; padding: 0 9px;
  cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  transition: background .12s ease, color .12s ease, border-color .12s ease;
}
button:hover { background: var(--surface-2); color: var(--ink); }
button:focus-visible { outline: none; box-shadow: var(--ring); }
button.ghost { border-color: transparent; background: transparent; }
button.ghost:hover { background: var(--surface-2); border-color: var(--line); }
button.icon { width: 26px; padding: 0; justify-content: center; }
button.danger:hover { color: var(--red); border-color: var(--red-line); background: var(--red-wash); }

.seg {
  display: inline-flex; border: 1px solid var(--line-2); border-radius: 4px; overflow: hidden; height: 26px;
}
.seg button {
  border: 0; border-radius: 0; height: 24px; background: transparent;
  border-left: 1px solid var(--line); font-size: 11px; padding: 0 9px;
}
.seg button:first-child { border-left: 0; }
.seg button[aria-pressed="true"] { background: var(--accent-wash); color: var(--accent); font-weight: 650; }
.seg button:focus-visible { box-shadow: inset 0 0 0 2px var(--accent); }

/* ---------- shell ---------- */
.shell {
  height: calc(100vh - 44px);
  max-width: var(--shell-max);
  margin: 0 auto;
  padding: var(--sp-2) var(--sp-3) var(--sp-2);
  display: grid;
  grid-template-columns: minmax(0, var(--rail-l)) minmax(0, 1fr) minmax(0, var(--rail-r));
  gap: var(--sp-2);
  min-height: 0;
}
@media (max-width: 1180px) {
  .shell { grid-template-columns: minmax(0, 220px) minmax(0, 1fr); }
  .rail-right { display: none; }
}

.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 6px;
  box-shadow: var(--shadow-1);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.card-hd {
  flex: none;
  display: flex; align-items: center; gap: var(--sp-1);
  padding: 0 var(--sp-1) 0 var(--sp-2);
  height: 34px;
  border-bottom: 1px solid var(--line-hair);
}
.card-hd h2 {
  margin: 0; font-size: 10.5px; font-weight: 700;
  letter-spacing: .09em; text-transform: uppercase; color: var(--ink-3);
  white-space: nowrap;
}
.card-hd .count {
  font-size: 11px; color: var(--ink-4);
  padding: 1px 5px; border-radius: 3px; background: var(--surface-2);
}
.card-bd { flex: 1 1 auto; min-height: 0; }
.card-ft {
  flex: none; border-top: 1px solid var(--line-hair);
  padding: var(--sp-1) var(--sp-2);
  font-size: 11px; color: var(--ink-4);
}

/* ---------- left rail: projects ---------- */
.rail-left { display: flex; flex-direction: column; min-height: 0; }
.rail-left .card { flex: 1 1 auto; }

.search {
  position: relative; margin: var(--sp-1) var(--sp-1) var(--sp-h);
}
.search input {
  width: 100%; height: 28px;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--ink);
  font: inherit; font-size: 12px;
  padding: 0 var(--sp-1) 0 26px;
}
.search input::placeholder { color: var(--ink-4); }
.search input:focus { outline: none; border-color: var(--accent-line); background: var(--surface); box-shadow: inset 0 0 0 1px var(--accent-line); }
.search .gl {
  position: absolute; left: 8px; top: 50%; transform: translateY(-50%);
  width: 11px; height: 11px; border: 1.5px solid var(--ink-4); border-radius: 99px;
  pointer-events: none;
}
.search .gl::after {
  content: ""; position: absolute; right: -4px; bottom: -3px;
  width: 5px; height: 1.5px; background: var(--ink-4); transform: rotate(45deg);
}

.plist { padding: 0 var(--sp-h) var(--sp-1); }
.grp {
  display: flex; align-items: center; gap: 6px;
  padding: var(--sp-2) var(--sp-1) var(--sp-h);
  font-size: 10px; font-weight: 700; letter-spacing: .09em; text-transform: uppercase;
  color: var(--ink-4);
  cursor: default; user-select: none;
}
.grp.click { cursor: pointer; }
.grp.click:hover { color: var(--ink-2); }
.grp .caret {
  width: 0; height: 0; border-left: 4px solid currentColor;
  border-top: 3.5px solid transparent; border-bottom: 3.5px solid transparent;
  transition: transform .12s ease; transform-origin: 25% 50%;
}
.grp[data-open="1"] .caret { transform: rotate(90deg); }
.grp .n { margin-left: auto; letter-spacing: 0; font-weight: 600; }

.prow {
  display: grid;
  grid-template-columns: 3px minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--sp-1);
  padding: 5px var(--sp-1) 5px 0;
  border-radius: 4px;
  cursor: pointer;
  color: var(--ink-2);
}
.prow:hover { background: var(--surface-2); }
.prow .bar { height: 18px; border-radius: 0 2px 2px 0; background: transparent; }
.prow[aria-selected="true"] { background: var(--accent-wash); color: var(--ink); }
.prow[aria-selected="true"] .bar { background: var(--accent); }
.prow[aria-selected="true"] .pname { font-weight: 650; }
.pname {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 12.5px;
}
.pname.hash { font-family: var(--mono); font-size: 11.5px; color: var(--ink-3); letter-spacing: -0.01em; }
.prow[aria-selected="true"] .pname.hash { color: var(--ink-2); }
.psub { font-size: 10.5px; color: var(--ink-4); }
.pmeta { display: flex; align-items: center; gap: 6px; flex: none; }
.pcount { font-size: 10.5px; color: var(--ink-4); }
.pdot { width: 5px; height: 5px; border-radius: 99px; background: var(--line-2); flex: none; }
.pdot.hot { background: var(--accent); }
.pdot.warm { background: var(--ink-3); }
.prow.all {
  border-bottom: 1px solid var(--line-hair);
  border-radius: 4px 4px 0 0;
  padding-bottom: var(--sp-1); margin-bottom: var(--sp-h);
}
.tag {
  font-size: 9.5px; font-weight: 650; letter-spacing: .05em; text-transform: uppercase;
  color: var(--ink-4); background: var(--surface-3);
  border-radius: 2px; padding: 0 4px; line-height: 14px; flex: none;
}

/* ---------- centre: the stream ---------- */
.stream { display: flex; flex-direction: column; min-height: 0; gap: var(--sp-2); }

/* NOW band — live claims. Never a giant box; it is one strip that
   collapses to a single line when nothing is claimed. */
.now {
  flex: none;
  background: var(--surface);
  border: 1px solid var(--line);
  border-left: 3px solid var(--accent);
  border-radius: 5px;
  padding: var(--sp-1) var(--sp-2) var(--sp-1) 13px;
  display: flex; flex-direction: column; gap: 6px;
}
.now.idle { border-left-color: var(--line-2); }
.now-hd {
  display: flex; align-items: baseline; gap: var(--sp-1);
  font-size: 10.5px; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; color: var(--ink-3);
}
.now-hd .k { color: var(--ink-4); letter-spacing: 0; font-weight: 500; text-transform: none; font-size: 11px; }
.now-hd input {
  margin-left: auto; height: 22px; width: 150px;
  background: var(--surface-2); border: 1px solid var(--line); border-radius: 3px;
  color: var(--ink); font: inherit; font-size: 11px; padding: 0 7px;
  text-transform: none; letter-spacing: 0; font-weight: 400;
}
.now-hd input:focus { outline: none; border-color: var(--accent-line); box-shadow: inset 0 0 0 1px var(--accent-line); }
.claims { display: flex; flex-direction: column; }
.claim {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr) 168px 40px;
  align-items: center; gap: var(--sp-2);
  padding: 3px 0;
  font-size: 12px;
}
.claim + .claim { border-top: 1px dashed var(--line-hair); }
.claim .who { display: flex; align-items: center; gap: 6px; min-width: 0; }
.claim .who span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.claim .path {
  font-family: var(--mono); font-size: 11.5px; color: var(--ink-2);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left;
}
.claim .path bdi { direction: ltr; }
.claim .held { font-size: 11px; color: var(--ink-3); text-align: right; }
.claim .why { font-size: 11px; color: var(--ink-4); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.now-empty { font-size: 12px; color: var(--ink-3); }
.now-empty b { color: var(--ink-2); font-weight: 600; }

/* stream card */
.stream .card { flex: 1 1 auto; }
.chips { display: flex; align-items: center; gap: var(--sp-h); flex-wrap: nowrap; overflow: hidden; }
.chip {
  height: 22px; padding: 0 8px; border-radius: 3px;
  border: 1px solid transparent; background: transparent;
  font-size: 11.5px; color: var(--ink-3); cursor: pointer;
  display: inline-flex; align-items: center; gap: 5px; white-space: nowrap;
}
.chip:hover { background: var(--surface-2); color: var(--ink-2); }
.chip[aria-pressed="true"] { background: var(--surface-3); color: var(--ink); font-weight: 600; }
.chip .k { font-size: 10.5px; color: var(--ink-4); }
.chip[aria-pressed="true"] .k { color: var(--ink-3); }
.chip .sw { width: 6px; height: 6px; border-radius: 1px; flex: none; }

.newpill {
  position: absolute; left: 50%; transform: translateX(-50%);
  top: 8px; z-index: 6;
  height: 24px; padding: 0 11px; border-radius: 99px;
  background: var(--accent); color: var(--on-accent);
  border: 0; font-size: 11px; font-weight: 650;
  box-shadow: var(--shadow-2);
}
.newpill:hover { background: var(--accent); filter: brightness(1.08); color: var(--on-accent); }

.streamwrap { position: relative; height: 100%; min-height: 0; }
#streamScroll { height: 100%; }

/* time buckets */
.bucket {
  position: sticky; top: 0; z-index: 4;
  display: flex; align-items: center; gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2) 5px 0;
  margin-left: var(--sp-2);
  background: linear-gradient(var(--surface) 72%, rgba(0,0,0,0));
  font-size: 10px; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; color: var(--ink-4);
}
.bucket .ln { flex: 1 1 auto; height: 1px; background: var(--line-hair); }

/* one event row */
.ev {
  display: grid;
  grid-template-columns: 62px 18px minmax(0, 1fr);
  gap: 0;
  padding: 0 var(--sp-2) 0 var(--sp-2);
  position: relative;
}
.ev:hover { background: var(--surface-2); }
.ev .t {
  font-family: var(--mono); font-variant-numeric: tabular-nums;
  font-size: 10.5px; color: var(--ink-4);
  padding: 7px 0 7px 0; text-align: left; white-space: nowrap;
}
.ev:hover .t { color: var(--ink-3); }
.ev .spine { position: relative; }
.ev .spine::before {
  content: ""; position: absolute; left: 5px; top: 0; bottom: 0; width: 1px; background: var(--line);
}
.ev:first-child .spine::before { top: 8px; }
.ev.last .spine::before { bottom: auto; height: 10px; }
.glyph {
  position: absolute; left: 0; top: 8px; width: 11px; height: 11px;
  background: var(--surface); display: block;
}
.glyph i {
  position: absolute; left: 2px; top: 2px; width: 7px; height: 7px; display: block;
}
.ev:hover .glyph { background: var(--surface-2); }

/* glyph shapes carry the type as well as the colour (not colour-only) */
.g-claim i     { background: var(--accent); border-radius: 1px; }
.g-release i   { background: transparent; border: 1.5px solid var(--green); border-radius: 1px; }
.g-finding i   { background: var(--amber); border-radius: 99px; }
.g-note i      { background: transparent; border: 1.5px solid var(--ink-4); border-radius: 99px; }
.g-task i      { background: var(--accent); transform: rotate(45deg); }
.g-task-blocked i { background: var(--red); transform: rotate(45deg); }
.g-task-review i  { background: var(--amber); transform: rotate(45deg); }
.g-task-closed i  { background: var(--green); transform: rotate(45deg); }
.g-session i   { background: var(--ink-3); width: 7px; height: 3px; top: 4px; }
.g-agent i     { background: var(--ink-3); border-radius: 99px; }
.g-stale i     { background: var(--red); border-radius: 99px; }

.ev .body { padding: 5px 0 7px; min-width: 0; }
.ev .l1 { font-size: 12.5px; color: var(--ink); display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; }
.ev .verb { font-weight: 650; }
.ev .verb.a { color: var(--accent); }
.ev .verb.g { color: var(--green); }
.ev .verb.w { color: var(--amber); }
.ev .verb.r { color: var(--red); }
.ev .path {
  font-family: var(--mono); font-size: 11.5px; color: var(--ink-2);
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ev .l2 { font-size: 11px; color: var(--ink-4); margin-top: 1px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ev .l2 .dot { width: 2px; height: 2px; border-radius: 99px; background: var(--ink-4); flex: none; }
.ev .actor { color: var(--ink-3); font-weight: 600; }
.ev .proj { font-family: var(--mono); font-size: 10.5px; }
.ev .quote {
  margin-top: 5px; padding: 5px var(--sp-1) 5px 9px;
  border-left: 2px solid var(--line-2);
  background: var(--surface-2);
  border-radius: 0 3px 3px 0;
  font-size: 11.5px; color: var(--ink-2); line-height: 1.45;
}
.ev.k-finding .quote { border-left-color: var(--amber); }
.ev.k-note .quote { border-left-color: var(--line-2); }
.ev .quote code { font-family: var(--mono); font-size: 11px; background: var(--surface-3); padding: 0 3px; border-radius: 2px; }
.sev {
  font-size: 9.5px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
  padding: 0 4px; line-height: 14px; border-radius: 2px; flex: none;
}
.sev.high { background: var(--red-wash); color: var(--red); }
.sev.med  { background: var(--amber-wash); color: var(--amber); }
.sev.low  { background: var(--surface-3); color: var(--ink-3); }

.state {
  font-size: 10.5px; font-weight: 600; padding: 0 5px; line-height: 16px; border-radius: 2px;
  border: 1px solid var(--line); color: var(--ink-3); background: var(--surface-2); flex: none;
}
.state.doing    { color: var(--accent); border-color: var(--accent-line); background: var(--accent-wash); }
.state.blocked  { color: var(--red);    border-color: var(--red-line);    background: var(--red-wash); }
.state.review   { color: var(--amber);  border-color: var(--amber-line);  background: var(--amber-wash); }
.state.closed   { color: var(--green);  border-color: var(--green-line);  background: var(--green-wash); }
.arrow { color: var(--ink-4); font-size: 11px; }

/* empty states */
.empty {
  padding: var(--sp-6) var(--sp-3);
  display: flex; flex-direction: column; gap: var(--sp-1);
  max-width: 440px;
}
.empty .ttl { font-size: 13px; font-weight: 650; color: var(--ink-2); }
.empty p { margin: 0; font-size: 12px; color: var(--ink-4); line-height: 1.6; }
.empty ul { margin: var(--sp-h) 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; }
.empty li { font-size: 11.5px; color: var(--ink-4); display: flex; align-items: center; gap: 8px; }
.empty li .sw { width: 7px; height: 7px; flex: none; }
.empty .dashes {
  height: 1px; width: 120px;
  background: repeating-linear-gradient(90deg, var(--line-2) 0 4px, transparent 4px 9px);
  margin-bottom: var(--sp-h);
}
.empty-sm {
  padding: var(--sp-2);
  font-size: 11.5px; color: var(--ink-4); line-height: 1.55;
}
.empty-sm b { color: var(--ink-3); font-weight: 600; display: block; margin-bottom: 2px; font-size: 12px; }

/* ---------- right rail ---------- */
.rail-right { display: flex; flex-direction: column; gap: var(--sp-2); min-height: 0; }
.rail-right .card.flexy { flex: 1 1 auto; }
.rail-right .card.fixed { flex: 0 0 auto; }

/* roster */
.agent {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto 26px;
  align-items: center; gap: var(--sp-1);
  padding: 6px var(--sp-1) 6px var(--sp-2);
  border-bottom: 1px solid var(--line-hair);
}
.agent:last-child { border-bottom: 0; }
.agent:hover { background: var(--surface-2); }
.av {
  width: 22px; height: 22px; border-radius: 3px;
  background: var(--surface-3); color: var(--ink-2);
  font-size: 10px; font-weight: 700; letter-spacing: .02em;
  display: flex; align-items: center; justify-content: center;
  position: relative; flex: none;
}
.av.lead { background: var(--accent-wash); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-line); }
.av .pip {
  position: absolute; right: -2px; bottom: -2px; width: 7px; height: 7px; border-radius: 99px;
  background: var(--green); box-shadow: 0 0 0 2px var(--surface);
}
.agent:hover .av .pip { box-shadow: 0 0 0 2px var(--surface-2); }
.av .pip.warm { background: var(--ink-4); }
.av .pip.cold { background: var(--red); }
.aname { min-width: 0; }
.aname .n { font-size: 12.5px; font-weight: 600; display: flex; align-items: center; gap: 5px; }
.aname .n em {
  font-style: normal; font-size: 9px; font-weight: 700; letter-spacing: .06em;
  color: var(--accent); background: var(--accent-wash); border-radius: 2px; padding: 0 4px; line-height: 13px;
}
.aname .h {
  font-family: var(--mono); font-size: 10.5px; color: var(--ink-4);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.asil { font-size: 11px; color: var(--ink-3); text-align: right; white-space: nowrap; }
.asil.cold { color: var(--red); font-weight: 600; }
.agent .x {
  width: 22px; height: 22px; border: 0; background: transparent; border-radius: 3px;
  color: var(--ink-4); opacity: 0; padding: 0; justify-content: center;
}
.agent:hover .x, .agent .x:focus-visible { opacity: 1; }
.agent .x:hover { background: var(--red-wash); color: var(--red); }

/* tasks */
.tally { display: flex; align-items: stretch; gap: 1px; padding: var(--sp-1) var(--sp-2) 0; }
.tally div {
  flex: 1 1 0; min-width: 0;
  display: flex; flex-direction: column; gap: 3px;
  padding-top: 4px;
  border-top: 2px solid var(--line-2);
}
.tally .v { font-size: 14px; font-weight: 650; letter-spacing: -0.01em; }
.tally .l { font-size: 9.5px; letter-spacing: .05em; text-transform: uppercase; color: var(--ink-4); }
.tally .t-doing   { border-top-color: var(--accent); }
.tally .t-blocked { border-top-color: var(--red); }
.tally .t-review  { border-top-color: var(--amber); }
.tally .t-closed  { border-top-color: var(--green); }
.tally .t-ready   { border-top-color: var(--line-2); }

.needs { padding: var(--sp-1) var(--sp-2) var(--sp-1); display: flex; flex-direction: column; gap: 5px; }
.need {
  display: grid; grid-template-columns: auto minmax(0, 1fr); gap: var(--sp-1);
  align-items: baseline; font-size: 12px; cursor: pointer;
}
.need:hover .nt { color: var(--accent); }
.need .id { font-family: var(--mono); font-size: 10.5px; color: var(--ink-4); }
.need .nt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink-2); }
.need .nb { font-size: 10.5px; color: var(--ink-4); }

/* mini DAG */
.dagwrap {
  border-top: 1px solid var(--line-hair);
  padding: var(--sp-1); overflow-x: auto;
  /* the graph is wider than the rail on purpose -- fade the cut so it reads
     as scrollable rather than clipped */
  -webkit-mask-image: linear-gradient(90deg, #000 calc(100% - 28px), transparent 100%);
  mask-image: linear-gradient(90deg, #000 calc(100% - 28px), transparent 100%);
}
.dag { position: relative; width: 560px; height: 208px; }
.dag svg { position: absolute; inset: 0; overflow: visible; }
.node {
  position: absolute; width: 122px; height: 40px;
  background: var(--surface-2); border: 1px solid var(--line-2); border-radius: 4px;
  padding: 4px 6px; display: flex; flex-direction: column; justify-content: center; gap: 1px;
  overflow: hidden;
}
.node .nid { font-family: var(--mono); font-size: 9.5px; color: var(--ink-4); }
.node .ntx { font-size: 11px; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node.s-doing   { border-color: var(--accent-line); background: var(--accent-wash); }
.node.s-doing .ntx { color: var(--ink); }
.node.s-blocked { border-color: var(--red-line); background: var(--red-wash); }
.node.s-review  { border-color: var(--amber-line); background: var(--amber-wash); }
.node.s-closed  { border-color: var(--green-line); background: var(--green-wash); opacity: .8; }
.node.s-ready   { border-style: dashed; }

/* session */
.sess { padding: var(--sp-1) var(--sp-2) var(--sp-2); }
.sess .nm { font-size: 13px; font-weight: 650; letter-spacing: -0.01em; display: flex; align-items: center; gap: 6px; }
.sess .nm .pulse { width: 6px; height: 6px; border-radius: 99px; background: var(--green); flex: none; }
.sess .meta { font-size: 11px; color: var(--ink-4); margin-top: 2px; }
.kv { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-1); margin-top: var(--sp-1); }
.kv div { display: flex; flex-direction: column; }
.kv .v { font-size: 15px; font-weight: 650; letter-spacing: -0.02em; line-height: 1.2; }
.kv .l { font-size: 9.5px; letter-spacing: .05em; text-transform: uppercase; color: var(--ink-4); }
.arch { border-top: 1px solid var(--line-hair); }
.arch-hd {
  display: flex; align-items: center; gap: 6px; cursor: pointer;
  padding: var(--sp-1) var(--sp-2);
  font-size: 10.5px; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; color: var(--ink-4);
}
.arch-hd:hover { color: var(--ink-2); }
.arch-hd .caret { width: 0; height: 0; border-left: 4px solid currentColor; border-top: 3.5px solid transparent; border-bottom: 3.5px solid transparent; transition: transform .12s ease; }
.arch-hd[data-open="1"] .caret { transform: rotate(90deg); }
.arch-hd .n { margin-left: auto; letter-spacing: 0; font-weight: 600; }
.arow {
  display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--sp-1);
  padding: 5px var(--sp-2); align-items: baseline;
}
.arow:hover { background: var(--surface-2); }
.arow .an { font-size: 12px; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.arow .ar { font-size: 10.5px; color: var(--ink-4); white-space: nowrap; }
.arow .why { grid-column: 1 / -1; font-size: 10.5px; color: var(--ink-4); }
.arow .why .r { color: var(--ink-3); }

/* deferred-render badge */
.held {
  position: absolute; right: var(--sp-2); bottom: var(--sp-1); z-index: 7;
  font-size: 10px; color: var(--ink-4); background: var(--surface-3);
  border: 1px solid var(--line); border-radius: 3px; padding: 1px 6px;
  pointer-events: none; opacity: 0; transition: opacity .15s ease;
}
.held.on { opacity: 1; }

.foot {
  display: flex; align-items: center; gap: var(--sp-1);
  font-size: 10.5px; color: var(--ink-4);
}

/* ---------------------------------------------------------------------
   Additions to the mockup's sheet, for the parts wired to real data.
   Every value here is one of its tokens — no new colours, no new spacing
   steps. If something wants a value that is not a token, the token set is
   what should change.
   --------------------------------------------------------------------- */

.grow { flex: 1 1 auto; min-width: 0; }
.amber { color: var(--amber); }

/* topbar alarms */
.calm { color: var(--ink-4); font-size: 12px; }
.alarm { font-size: 12px; padding: 2px var(--sp-1); border-radius: 5px;
  border: 1px solid var(--line); background: var(--surface-2); color: var(--ink-2);
  white-space: nowrap; max-width: 46ch; overflow: hidden; text-overflow: ellipsis; }
.alarm.r { color: var(--red); background: var(--red-wash); border-color: var(--red-line); }
.alarm.w { color: var(--amber); background: var(--amber-wash); border-color: var(--amber-line); }
.alarm.b { color: var(--accent); background: var(--accent-wash); border-color: var(--accent-line); }

/* projects rail */
.pgroup { display: flex; align-items: center; gap: var(--sp-1);
  font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase;
  color: var(--ink-4); padding: var(--sp-2) var(--sp-1) var(--sp-h); }
.pmore { color: var(--ink-4); font-size: 11.5px; padding: var(--sp-h) var(--sp-1) var(--sp-1); }
.prow { display: flex; align-items: center; gap: var(--sp-1); text-decoration: none;
  padding: 5px var(--sp-1); border-radius: 6px; color: var(--ink-2); }
.prow:hover { background: var(--surface-2); color: var(--ink); }
.prow.sel { background: var(--accent-wash); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-line); }
.prow .pname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prow.unnamed .pname { color: var(--ink-4); }
.prow .page { color: var(--ink-4); font-size: 11px; flex: none; }
.tag { flex: none; font-size: 9.5px; letter-spacing: .05em; text-transform: uppercase;
  border: 1px solid var(--line-2); color: var(--ink-4); border-radius: 3px; padding: 0 4px; }
.tag.amber { color: var(--amber); border-color: var(--amber-line); background: var(--amber-wash); }

/* working now */
/* The card is a flex column whose body takes the remaining height, so a
   sibling between the header and the body collapses to zero unless it says
   otherwise. It had five rows of content and measured 0px tall. */
#nowBand { flex: none; }
/* The mockup pinned .held as an absolute overlay at the bottom of the stream.
   Here it is a band in normal flow directly under the card header, so it has
   to be taken out of that positioning — otherwise it lifts out of the layout
   and its container measures zero while the rows themselves are 165px tall. */
.nowband { border-bottom: 1px solid var(--line); background: var(--surface-2); }
.nowband-hd { display: flex; align-items: baseline; gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2) var(--sp-h);
  font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-4); }
.nowband-hd .count { color: var(--ink-3); text-transform: none; letter-spacing: 0; font-size: 11.5px; }
.nowband-empty { padding: var(--sp-h) var(--sp-2) var(--sp-2); color: var(--ink-4); font-size: 12.5px; }
.hrow { display: flex; align-items: baseline; gap: var(--sp-1);
  padding: 3px var(--sp-2); font-size: 12.5px; }
.hrow:last-child { padding-bottom: var(--sp-1); }
.hrow.quiet { background: var(--amber-wash); }
.hactor { color: var(--accent); flex: none; }
.hpath { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hintent { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.htask { color: var(--ink-4); flex: none; }
.hid { color: var(--ink-4); font-size: 10.5px; flex: none; opacity: .75; }

/* stream buckets */
.bhd { display: flex; align-items: center; gap: var(--sp-1);
  font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-4);
  padding: var(--sp-2) var(--sp-2) var(--sp-h); }
.k- { }

/* roster */
.rrow { display: flex; align-items: center; gap: var(--sp-1); padding: 5px var(--sp-2);
  border-bottom: 1px solid var(--line-hair); font-size: 12.5px; }
.rrow:last-child { border-bottom: 0; }
.av { flex: none; width: 20px; height: 20px; border-radius: 5px; display: grid; place-items: center;
  background: var(--surface-3); color: var(--ink-2); font-size: 10px; font-family: var(--mono); }
.rname { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rage { color: var(--ink-4); font-size: 11px; flex: none; }

/* work tally */
.tbar { display: flex; height: 4px; border-radius: 2px; overflow: hidden;
  margin: var(--sp-2) var(--sp-2) var(--sp-1); background: var(--surface-3); }
.tbar > span { display: block; }
.seg-doing { background: var(--amber); }
.seg-blocked { background: var(--ink-4); }
.seg-review { background: var(--accent); }
.seg-ready { background: var(--green); }
.seg-closed { background: var(--line-2); }
.tally { display: flex; padding: 0 var(--sp-2) var(--sp-1); gap: var(--sp-1); }
.tcell { flex: 1 1 0; min-width: 0; }
.tn { font-size: 15px; color: var(--ink); }
.tl { font-size: 9.5px; letter-spacing: .06em; color: var(--ink-4); }
.stuck { border-top: 1px solid var(--line-hair); padding: var(--sp-1) var(--sp-2); }
.srow { display: flex; gap: var(--sp-1); align-items: baseline; padding: 3px 0; font-size: 12px; }
.srow .mono { color: var(--ink); flex: none; }
.stitle { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.swhy { color: var(--amber); flex: none; margin-left: auto; }
.allgood { padding: var(--sp-1) var(--sp-2) var(--sp-2); color: var(--green); font-size: 12.5px; }

/* this repo */
.sroot { padding: var(--sp-2) var(--sp-2) var(--sp-1); color: var(--ink-3); font-size: 11px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left; }
.stats { display: flex; padding: 0 var(--sp-2) var(--sp-1); gap: var(--sp-1); }
.scell { flex: 1 1 0; min-width: 0; }
.sn { font-size: 15px; color: var(--ink); }
.sl { font-size: 9.5px; letter-spacing: .06em; color: var(--ink-4); }
.sgen { padding: 0 var(--sp-2) var(--sp-2); color: var(--ink-4); font-size: 10.5px; }

/* the DAG, on demand */
.dagwrap { position: fixed; inset: 0; background: var(--bg); z-index: 50;
  display: grid; place-items: stretch; padding: var(--sp-3); }
.dagwrap[hidden] { display: none; }
.dagbox { display: flex; flex-direction: column; min-height: 0;
  border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
  background: var(--surface); box-shadow: var(--shadow-2); }
.dagbar { display: flex; align-items: center; gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2); border-bottom: 1px solid var(--line);
  font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-4); }
.gtab.on { color: var(--accent); background: var(--accent-wash); }
.dagbox iframe { flex: 1 1 auto; width: 100%; border: 0; display: block; min-height: 0; }
</style>
<header class="topbar">
  <div class="brand"><span class="mark"></span>comms</div>
  <div class="sep"></div>
  <div class="live"><span class="livedot" id="livedot"></span><span id="liveTxt">connecting</span><span class="mono" id="clock"></span></div>
  <div class="sep"></div>
  <div id="alarms" style="display:flex;gap:6px;align-items:center;"></div>
  <div class="grow"></div>
  <button id="themeBtn" class="icon ghost" title="Toggle light / dark" aria-label="Toggle theme">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path id="themeIcon" d="M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8 5.6 5.6 0 1 0 13.2 9.6Z" fill="currentColor"/>
    </svg>
  </button>
</header>

<main class="shell">
  <aside class="rail-left">
    <div class="card">
      <div class="card-hd"><span>Projects</span><span class="count" id="projCount"></span></div>
      <div class="search"><input id="projQ" type="search" placeholder="Filter name or hash" autocomplete="off" spellcheck="false"></div>
      <div class="card-bd scroll" id="projScroll"><div class="plist" id="projList"></div></div>
      <div class="card-ft foot" id="projFoot"></div>
    </div>
  </aside>

  <section class="stream">
    <div class="card">
      <div class="card-hd">
        <span>Activity</span>
        <div class="chips" id="chips"></div>
        <div class="grow"></div>
        <span class="count" id="evCount"></span>
      </div>
      <div id="nowBand"></div>
      <div class="card-bd streamwrap">
        <div class="scroll" id="streamScroll"><div id="streamList"></div></div>
        <div class="newpill" id="newPill" hidden></div>
      </div>
    </div>
  </section>

  <aside class="rail-right">
    <div class="card">
      <div class="card-hd"><span>Roster</span><span class="count" id="rosterCount"></span></div>
      <div class="card-bd" id="roster"></div>
    </div>
    <div class="card">
      <div class="card-hd"><span>Work</span><div class="grow"></div><button id="dagBtn" class="ghost">Show graph</button></div>
      <div class="card-bd" id="tasks"></div>
    </div>
    <div class="card">
      <div class="card-hd"><span>This repo</span></div>
      <div class="card-bd scroll" id="sessionScroll"><div id="session"></div></div>
    </div>
  </aside>
</main>

<div class="dagwrap" id="dagWrap" hidden>
  <div class="dagbox">
    <div class="dagbar">
      <button class="ghost gtab on" id="gTasks" data-src="/tasks.html">Task graph</button>
      <button class="ghost gtab" id="gMap" data-src="/map.html">Code map</button>
      <div class="grow"></div><button id="dagClose" class="ghost">Close</button>
    </div>
    <iframe id="dagFrame" title="graph"></iframe>
  </div>
</div>
<script>
var D = null;              // the last snapshot
var FILTER = "all";        // which stream chip is active
var PQ = "";               // projects filter box
var PAUSED_AT_BOTTOM = true;

function el(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function pad(n) { return (n < 10 ? "0" : "") + n; }
function hhmmss(iso) {
  var t = new Date(iso);
  if (isNaN(t)) return "";
  return pad(t.getHours()) + ":" + pad(t.getMinutes()) + ":" + pad(t.getSeconds());
}
function ago(secs) {
  var s = Math.max(0, Math.round(secs));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}
function agoIso(iso) {
  var t = Date.parse(iso);
  return isNaN(t) ? "" : ago((Date.now() - t) / 1000);
}
function shortPath(p) {
  // Keep the tail — the filename is what identifies a claim at a glance, and
  // a long path pushes it off the row entirely.
  var s = String(p || "");
  return s.length > 46 ? "…" + s.slice(-45) : s;
}

/* ---------- projects rail ---------------------------------------------- */

function projRow(p) {
  var label = p.name || "(unnamed)";
  var cls = "prow" + (p.current ? " sel" : "") + (p.name ? "" : " unnamed");
  var h = '<a class="' + cls + '" href="?store=' + esc(p.key) + '" title="' + esc(p.root || p.key) + '">';
  h += '<span class="pname">' + esc(label) + "</span>";
  if (!p.name) { h += '<span class="tag">UNNAMED</span>'; }
  h += '<span class="grow"></span><span class="page mono">' + ago(p.idle_seconds) + "</span></a>";
  return h;
}
function renderProjects() {
  var ps = (D.projects || []).filter(function (p) {
    if (!PQ) return true;
    var q = PQ.toLowerCase();
    return (p.name || "").toLowerCase().indexOf(q) >= 0 || p.key.indexOf(q) >= 0;
  });
  el("projCount").textContent = ps.length;
  var bands = [["active", "Active"], ["quiet", "Quiet"], ["low", "Low signal"]];
  var h = "";
  bands.forEach(function (b) {
    var rows = ps.filter(function (p) { return p.band === b[0]; });
    if (!rows.length) return;
    h += '<div class="pgroup">' + b[1] + '<span class="grow"></span>' + rows.length + "</div>";
    rows.slice(0, b[0] === "low" ? 12 : 40).forEach(function (p) { h += projRow(p); });
    if (rows.length > (b[0] === "low" ? 12 : 40)) {
      h += '<div class="pmore">and ' + (rows.length - (b[0] === "low" ? 12 : 40)) + " more</div>";
    }
  });
  if (!h) { h = '<div class="empty">No projects match.</div>'; }
  el("projList").innerHTML = h;
  el("projFoot").textContent = (D.projects || []).length + " stores on this machine";
}

/* ---------- working now ------------------------------------------------ */

function renderNow() {
  var cs = D.claims || [];
  var h = '<div class="nowband">';
  h += '<div class="nowband-hd"><span>Working now</span><span class="count">' +
       (cs.length ? cs.length + (cs.length === 1 ? " file claimed" : " files claimed") : "") + "</span></div>";
  if (!cs.length) {
    h += '<div class="nowband-empty">Nobody is holding any ground right now.</div>';
  } else {
    cs.forEach(function (c) {
      h += '<div class="hrow' + (c.quiet ? " quiet" : "") + '">';
      h += '<span class="hactor mono">' + esc(c.actor) + "</span>";
      h += '<span class="hpath mono">' + esc(shortPath(c.scope)) + "</span>";
      h += '<span class="hintent">' + esc(c.intent || "") + "</span>";
      if (c.task) { h += '<span class="htask mono">' + esc(c.task) + "</span>"; }
      h += '<span class="grow"></span>';
      if (c.quiet) { h += '<span class="tag amber">quiet ' + ago(c.idle_seconds) + "</span>"; }
      h += '<span class="hid mono" title="claim id — the handle for release --force">' + esc(c.id) + "</span>";
      h += "</div>";
    });
  }
  h += "</div>";
  el("nowBand").innerHTML = h;
}

/* ---------- the stream ------------------------------------------------- */

var KINDS = [
  ["all", "All"], ["claim", "Claims"], ["finding", "Findings"],
  ["note", "Notes"], ["task", "Tasks"], ["session", "Session"]
];
function matchKind(e) {
  if (FILTER === "all") return true;
  if (FILTER === "claim") return e.type === "claim" || e.type === "release";
  if (FILTER === "task") return e.type === "task" || e.type === "task_edge" || e.type === "task_state";
  if (FILTER === "session") return e.type === "hello" || e.type === "blocked";
  return e.type === FILTER;
}
function renderChips() {
  var f = D.feed || [];
  var h = "";
  KINDS.forEach(function (k) {
    var n = k[0] === "all" ? f.length : f.filter(function (e) {
      var save = FILTER; FILTER = k[0]; var m = matchKind(e); FILTER = save; return m;
    }).length;
    h += '<button class="chip' + (FILTER === k[0] ? " on" : "") + '" data-k="' + k[0] + '">' +
         esc(k[1]) + '<span class="n">' + n + "</span></button>";
  });
  el("chips").innerHTML = h;
}
function glyphClass(e) {
  if (e.type === "claim") return "g-claim";
  if (e.type === "release") return "g-release";
  if (e.type === "finding") return e.category === "bug" ? "g-task-blocked" : "g-finding";
  if (e.type === "note") return "g-note";
  if (e.type === "hello") return "g-agent";
  if (e.type === "blocked") return "g-stale";
  if (e.type === "task_state") {
    if (e.state === "verified") return "g-task-closed";
    if (e.state === "rejected") return "g-task-blocked";
    return "g-task-review";
  }
  return "g-task";
}
function stateChip(v) {
  var cls = v === "doing" ? "doing" : v === "blocked" ? "blocked"
          : v === "review" ? "review" : v === "closed" ? "closed" : "";
  var label = v === "review" ? "awaiting-review" : v;
  return '<span class="state ' + cls + '">' + esc(label) + "</span>";
}
// A finding's CATEGORY, not a severity — this build records what kind of thing
// it is, and colouring `decision` red because it sorts near `bug` would be a
// lie dressed as a signal.
function catClass(c) { return c === "bug" ? "high" : c === "gotcha" ? "med" : "low"; }

function eventRow(e, isLast) {
  // `last` stops the timeline spine running past the final row of a group.
  var s = '<div class="ev k-' + esc(e.type) + (isLast ? " last" : "") + '">';
  s += '<div class="t mono">' + esc(hhmmss(e.ts)) + "</div>";
  s += '<div class="spine"><span class="glyph ' + glyphClass(e) + '"><i></i></span></div>';
  s += '<div class="body">';

  if (e.type === "claim" || e.type === "release") {
    var verb = e.type === "claim" ? "claimed" : "released";
    s += '<div class="l1"><span class="verb ' + (e.type === "claim" ? "a" : "g") + '">' + verb + "</span>";
    s += '<span class="path mono">' + esc(shortPath(e.scope)) + "</span></div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + "</span>";
    if (e.task) { s += '<span class="dot"></span><span class="proj mono">' + esc(e.task) + "</span>"; }
    var tail = e.intent || e.result || "";
    if (tail) { s += '<span class="dot"></span><span>' + esc(tail) + "</span>"; }
    s += "</div>";

  } else if (e.type === "task_state") {
    s += '<div class="l1"><span class="verb mono">' + esc(e.task) + "</span></div>";
    s += '<div class="l2">' + stateChip(e.state || "doing");
    s += '<span class="dot"></span><span class="actor">@' + esc(e.actor) + "</span>";
    // Self-reported, and shown as such: these are the doer's claims about
    // their own work, which is exactly why somebody else has to verify it.
    var cr = e.checks || {};
    Object.keys(cr).forEach(function (k) {
      var ok = String(cr[k]).toLowerCase() === "pass";
      s += '<span class="sev ' + (ok ? "low" : "high") + '">' + esc(k) + " " + esc(cr[k]) + "</span>";
    });
    s += "</div>";

  } else if (e.type === "task" || e.type === "task_edge") {
    s += '<div class="l1"><span class="verb">' + (e.type === "task" ? "declared" : "ordered") + "</span>";
    s += '<span class="mono">' + esc(e.task || "") + "</span></div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + "</span></div>";

  } else if (e.type === "finding") {
    s += '<div class="l1"><span class="sev ' + catClass(e.category) + '">' + esc(e.category || "note") + "</span>";
    s += '<span class="verb w">finding</span></div>';
    s += '<div class="quote">' + esc(e.body || "") + "</div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + "</span></div>";

  } else if (e.type === "note") {
    s += '<div class="l1"><span class="verb">note</span></div>';
    s += '<div class="quote">' + esc(e.body || "") + "</div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + "</span></div>";

  } else if (e.type === "blocked") {
    s += '<div class="l1"><span class="verb r">refused</span>';
    s += '<span class="path mono">' + esc(shortPath(e.scope || e.task)) + "</span></div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + "</span>";
    if (e.reason) { s += '<span class="dot"></span><span>' + esc(e.reason) + "</span>"; }
    s += "</div>";

  } else {
    s += '<div class="l1"><span class="verb">' + esc(e.type) + "</span></div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + "</span></div>";
  }
  return s + "</div></div>";
}

function renderStream() {
  var f = (D.feed || []).filter(matchKind);
  el("evCount").textContent = f.length;
  if (!f.length) {
    el("streamList").innerHTML = '<div class="empty">Nothing here yet.</div>';
    return;
  }
  // Grouped by how long ago. "What just happened" and "what happened this
  // morning" are different questions and a flat list answers neither.
  //
  // The bucket heading is a SIBLING of the rows it labels, not a wrapper:
  // .bucket is a sticky flex row, so nesting the events inside it laid them
  // out side by side in columns instead of down the page.
  var now = Date.now();
  var BUCKETS = [
    ["Last 15 minutes", 15 * 60], ["Earlier this hour", 60 * 60],
    ["Today", 86400], ["Before today", Infinity]
  ];
  var groups = BUCKETS.map(function () { return []; });
  f.forEach(function (e) {
    var age = (now - Date.parse(e.ts)) / 1000;
    for (var i = 0; i < BUCKETS.length; i++) {
      if (age <= BUCKETS[i][1]) { groups[i].push(e); return; }
    }
    groups[groups.length - 1].push(e);
  });

  var h = "";
  groups.forEach(function (rows, i) {
    if (!rows.length) return;
    h += '<div class="bucket">' + esc(BUCKETS[i][0]) +
         '<span class="ln"></span><span class="count">' + rows.length + "</span></div>";
    rows.forEach(function (e, j) {
      h += eventRow(e, j === rows.length - 1);
    });
  });
  el("streamList").innerHTML = h;
}

/* ---------- roster ------------------------------------------------------ */

function initials(name) {
  var s = String(name || "?").replace(/[^a-zA-Z0-9]+/g, " ").trim().split(" ");
  return ((s[0] || "?")[0] + (s.length > 1 ? s[1][0] : "")).toLowerCase();
}
function renderRoster() {
  var r = D.roster || [];
  el("rosterCount").textContent = r.length;
  if (!r.length) { el("roster").innerHTML = '<div class="empty">Nobody has done anything here yet.</div>'; return; }
  var h = "";
  r.slice(0, 24).forEach(function (a) {
    var idle = a.last_seen ? (Date.now() - Date.parse(a.last_seen)) / 1000 : 1e9;
    var silent = idle >= 3600;
    h += '<div class="rrow">';
    h += '<span class="av">' + esc(initials(a.actor)) + "</span>";
    h += '<span class="rname">@' + esc(a.actor) + "</span>";
    if (!a.identified) { h += '<span class="tag">unidentified</span>'; }
    if (a.holding) { h += '<span class="tag">holding ' + a.holding + "</span>"; }
    h += '<span class="grow"></span>';
    h += '<span class="rage mono' + (silent ? " amber" : "") + '">' + esc(agoIso(a.last_seen)) + "</span>";
    h += "</div>";
  });
  if (r.length > 24) { h += '<div class="pmore">and ' + (r.length - 24) + " more</div>"; }
  el("roster").innerHTML = h;
}

/* ---------- work -------------------------------------------------------- */

function renderTasks() {
  var c = D.counts || {}, ts = D.tasks || [];
  if (!ts.length) {
    el("tasks").innerHTML = '<div class="empty">No tasks declared yet. An agent adds one with ' +
      '<code class="mono">comms-graph task add &lt;id&gt;</code>.</div>';
    return;
  }
  var order = [["doing", "DOING"], ["blocked", "BLOCKED"], ["review", "REVIEW"],
               ["ready", "READY"], ["closed", "CLOSED"]];
  var total = order.reduce(function (a, k) { return a + (c[k[0]] || 0); }, 0) || 1;
  var h = '<div class="tbar">';
  order.forEach(function (k) {
    var n = c[k[0]] || 0;
    if (n) { h += '<span class="seg-' + k[0] + '" style="flex:' + n + '"></span>'; }
  });
  h += "</div><div class='tally'>";
  order.forEach(function (k) {
    h += '<div class="tcell"><div class="tn mono">' + (c[k[0]] || 0) + '</div><div class="tl">' + k[1] + "</div></div>";
  });
  h += "</div>";

  // What is stuck, in words. The numbers say how much; this says what to do.
  var stuck = ts.filter(function (t) {
    return t.phase === "review" || t.phase === "cycle" ||
           (t.phase === "blocked" && t.blocked_by && t.blocked_by.length) ||
           (t.phase === "blocked" && t.blocked_by && t.blocked_by.length);
  });
  if (stuck.length) {
    h += '<div class="stuck">';
    stuck.slice(0, 6).forEach(function (t) {
      var why = t.phase === "review" ? "needs review"
              : t.phase === "cycle" ? "in a dependency loop"
              : "waiting on " + (t.blocked_by || []).join(", ");
      h += '<div class="srow"><span class="mono">' + esc(t.id) + "</span>" +
           '<span class="stitle">' + esc(t.title || "") + "</span>" +
           '<span class="swhy">' + esc(why) + "</span></div>";
    });
    if (stuck.length > 6) { h += '<div class="pmore">and ' + (stuck.length - 6) + " more</div>"; }
    h += "</div>";
  } else {
    h += '<div class="allgood">Nothing is stuck.</div>';
  }
  el("tasks").innerHTML = h;
}

/* ---------- this repo --------------------------------------------------- */

function renderSession() {
  var c = D.counts || {};
  var h = '<div class="sroot mono" title="' + esc(D.root || "") + '">' + esc(D.root || "") + "</div>";
  h += '<div class="stats">';
  [["events", (D.feed || []).length], ["claims", c.claims || 0],
   ["findings", (D.findings || []).length], ["notes", (D.notes || []).length]].forEach(function (kv) {
    h += '<div class="scell"><div class="sn mono">' + kv[1] + '</div><div class="sl">' + kv[0].toUpperCase() + "</div></div>";
  });
  h += "</div>";
  h += '<div class="sgen">read ' + esc(D.generated || "") + "</div>";
  el("session").innerHTML = h;
}

/* ---------- alarms ------------------------------------------------------ */

function renderAlarms() {
  var a = D.alerts || [];
  if (!a.length) { el("alarms").innerHTML = '<span class="calm">nothing needs you</span>'; return; }
  var tone = { cycle: "r", dangling: "r", quiet: "w", review: "b" };
  var h = "";
  a.forEach(function (x) {
    h += '<span class="alarm ' + (tone[x.kind] || "") + '" title="' + esc(x.hint || "") + '">' +
         esc(x.text) + "</span>";
  });
  el("alarms").innerHTML = h;
}

/* ---------- driver ------------------------------------------------------ */

function renderAll(d) {
  D = d;
  if (d.error) {
    el("alarms").innerHTML = '<span class="alarm r">' + esc(d.error) + "</span>";
    el("streamList").innerHTML = '<div class="empty">' + esc(d.error) + "</div>";
    return;
  }
  renderAlarms(); renderProjects(); renderNow(); renderChips();
  renderStream(); renderRoster(); renderTasks(); renderSession();
}

el("chips").addEventListener("click", function (ev) {
  var b = ev.target.closest(".chip"); if (!b) return;
  FILTER = b.getAttribute("data-k"); renderChips(); renderStream();
});
el("projQ").addEventListener("input", function (ev) { PQ = ev.target.value.trim(); renderProjects(); });

// Theme. Remembered, because a board is left open all day and one that resets
// to the wrong one on every reload is a small daily irritation.
(function () {
  var saved = null;
  try { saved = localStorage.getItem("comms.theme"); } catch (e) {}
  if (saved) { document.documentElement.setAttribute("data-theme", saved); }
  el("themeBtn").addEventListener("click", function () {
    var now = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", now);
    try { localStorage.setItem("comms.theme", now); } catch (e) {}
  });
})();

// The task DAG, on demand. It is a whole vis-network page in an iframe and
// most of the day nobody wants it — so it costs nothing until asked for.
(function () {
  var wrap = el("dagWrap"), frame = el("dagFrame"), shown = "";
  function show(src, btn) {
    // Loading only what is asked for, and only once. Both are full
    // vis-network pages; mounting them behind a closed overlay would pay for
    // two graph layouts on every page load for a view most days nobody opens.
    if (shown !== src) { frame.src = src; shown = src; }
    [el("gTasks"), el("gMap")].forEach(function (b) { b.classList.toggle("on", b === btn); });
    wrap.hidden = false;
  }
  el("dagBtn").addEventListener("click", function () { show("/tasks.html", el("gTasks")); });
  el("gTasks").addEventListener("click", function () { show("/tasks.html", el("gTasks")); });
  el("gMap").addEventListener("click", function () { show("/map.html", el("gMap")); });
  el("dagClose").addEventListener("click", function () { wrap.hidden = true; });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { wrap.hidden = true; } });
})();

function tick() {
  var t = new Date();
  el("clock").textContent = pad(t.getHours()) + ":" + pad(t.getMinutes()) + ":" + pad(t.getSeconds());
}
setInterval(tick, 1000); tick();

var live = el("liveTxt"), dot = el("livedot");
var es = new EventSource("/events" + location.search);
es.onopen = function () { live.textContent = "connected"; dot.classList.remove("off"); };
es.onerror = function () { live.textContent = "disconnected"; dot.classList.add("off"); };
es.onmessage = function (ev) { renderAll(JSON.parse(ev.data)); };
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
