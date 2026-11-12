"""A local dashboard: who holds which files, what changed unclaimed, the code map.

WHY A SERVER AND NOT JUST FILES. ``view.py`` writes a complete standalone page,
which is the right shape for sending somebody a snapshot. What they cannot do is change when the log changes, and the whole
value of a coordination board is that you leave it open while agents work. This
adds the one thing files cannot have: it notices a write and pushes.

WHY THE CODE MAP IS AN IFRAME. It is a finished page with its own vis-network
instance, its own layout and its own interaction state. Inlined, every push
would have to avoid destroying its pan, zoom and selection. An iframe reloads
only when its own data changed.

BINDS TO LOOPBACK ONLY, and deliberately has no mutating endpoints. The board
shows what the log says; anything that changes the log goes through the CLI,
where the lock and the fold enforce the rules. A dashboard button that could
release somebody else's claim would be a second writer with none of those
guarantees.
"""

from __future__ import annotations

import hashlib
import html
from urllib.parse import unquote, parse_qs
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from . import lock as _lock
from . import log as _log
from . import state as _state
from . import guard as _guard
from . import tree as _tree
from .board_history import history_page

#: How often the watcher stats the log. The log is appended under a lock and a
#: human reading a board does not need sub-second latency, so this is chosen to
#: be invisible to a reader while costing about two syscalls a second.
POLL_SECONDS = 0.5

#: How long a holder must be silent before their claim reads as quiet. One hour,
#: matching the Go build's stale rule, and deliberately advisory: quiet is a
#: prompt to look, never a licence for the board to act. Nothing expires on its
#: own: a claim ends only when somebody names it in a release or a steal.
QUIET_AFTER_SECONDS = 3600

_FRONTEND_BUILD_MARKER = "__COMMS_FRONTEND_BUILD_TOKEN__"


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _string(data: Any, key: str) -> str:
    """One field out of an event payload, as a string, whatever is in there.

    Event data is agent-written and only conventionally shaped: a key can be
    missing, null, a number, or a nested object. The board must render it
    either way: this is the surface that must never go blank.
    """
    if not isinstance(data, dict):
        return ""
    value = data.get(key)
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value)



#: Directories that are never a project, however much they look like one. Kept
#: as a prefix test rather than a name test: a scratch repo can be called
#: anything, and "repo" is a real name somebody might use.
_THROWAWAY_ROOTS = ("/private/var/folders/", "/var/folders/", "/private/tmp/", "/tmp/")


def _is_throwaway(root: str) -> bool:
    return any(root.startswith(prefix) for prefix in _THROWAWAY_ROOTS)


def _snapshot(root: Path, log_file: Path, graph_path: Path | None = None) -> dict:
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
    # Code, so an agent on any other harness had no `last_seen` at all, and
    # its claim went quiet an hour after it was taken no matter how hard that
    # agent was working. The board then contradicted itself on one screen
    # ("last seen 10 seconds ago" beside "idle 3 hours") and its one
    # recommended manual action was to take live ground off a working agent.
    #
    # The roster was switched to this rule and this computation was not. They
    # read from the same map now, so they cannot disagree again.
    last_by_actor: dict[str, datetime] = {}
    for ev in events:
        # The same events the fold counts, so the board and `status` agree on
        # who has gone silent.
        if not ev.actor or ev.type not in _log.WRITTEN_TYPES:
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
            "intent": c.intent, "auto": c.auto,
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
    # hello in this repo": a board reporting an empty room to somebody watching
    # four agents work in it.
    #
    # A hello still matters and is still shown: it is what ties an actor to a
    # process. An actor without one is present but unidentified.
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
            "model": getattr(s, "model", "") if s else "",
            "host": getattr(s, "hostname", "") if s else "",
            "identified": s is not None,
            "last_seen": last.isoformat().replace("+00:00", "Z") if last else "",
            "holding": sum(1 for c in st.claims.values() if c.actor == actor),
        })

    alerts: list[dict] = []

    # HOW OLD THE MAP IS. Raised by an agent that called `explain` on a symbol
    # and got a line number ~100 lines stale and three of its connections: the
    # map had been extracted once and the file had moved under it. Their point is
    # the right one: a map that is confidently wrong is worse than no map,
    # because "meets nothing" and "meets nothing I know about" read identically,
    # and the recall caveat covers presence, not freshness.
    map_age = None
    try:
        if graph_path and graph_path.is_file():
            map_age = int(now.timestamp() - graph_path.stat().st_mtime)
    except OSError:
        map_age = None
    if map_age is not None and map_age > 86400:
        alerts.append({
            "kind": "stale-map",
            "text": f"the code map is {map_age // 86400} day(s) old, so it may not "
                    f"match the code",
            "hint": "Rebuild it with `graphify extract . --code-only`.",
        })

    # One serializer for the live preview and paged history; a release stays
    # attached to the owner of the claims it ended.
    feed = history_page(events)["events"]

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
    # rather than a page you open per repo. Filesystem only: folding 180 logs
    # to draw a sidebar would cost seconds and the sidebar only needs a name
    # and a recency.
    here = _log.repo_hash(root)
    projects = []
    hidden = 0
    for st_info in _log.known_stores():
        # A store is not a project just because it exists. Measured on this
        # machine: 215 stores, of which 213 were throwaway: 193 whose directory
        # has already been deleted, and the rest scratch repos under a temp dir
        # from running the test suite. The rail listed all of them, so the two
        # real projects sat among 26 entries called "repo" and 156 with no name
        # at all, and the sidebar was unusable for the thing it exists to do.
        #
        # Two rules, both about the ROOT rather than the log: a directory that is
        # gone cannot be worked in, and a directory under the system temp area
        # was never a project. Neither guesses from the contents.
        if not st_info["exists"] or _is_throwaway(st_info["root"]):
            hidden += 1
            continue
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

    # What is ACTUALLY changed on disk, which no agent has to remember to
    # report. Every other number on this board comes from somebody choosing to
    # declare something; this one comes from git. It is the answer to "is the
    # tree quiet", a question the claim count was being read as answering and
    # cannot.
    survey = _tree.survey(root, st)
    dirty = {
        "readable": survey.readable,
        "unavailable": survey.unavailable,
        "headline": _tree.headline(survey),
        "total": len(survey.changes),
        "unclaimed": len(survey.unattributed),
        "files": [
            {"path": a.change.path, "how": a.change.how(), "actor": a.actor,
             "basis": a.basis, "intent": a.intent,
             "staged": a.change.staged, "deleted": a.change.deleted}
            # Capped, and the cap is spent on the unattributed ones first,
            # because those are the reason the panel exists.
            for a in sorted(survey.changes, key=lambda a: (a.known, a.change.path))[:200]
        ],
    }

    # Whether anything enforces the commit check in this repo. Reported because
    # an unenforced guard is indistinguishable from an enforced one until the
    # moment somebody commits over a live claim, which has now happened.
    gst = _guard.status(root)
    guard = {"state": gst.state, "installed": gst.installed,
             "chained": gst.chained, "text": _guard.describe(gst)}

    return {
        "root": str(root),
        "generated": _now_text(),
        "alerts": alerts,
        "projects": projects,
        "dirty": dirty,
        "guard": guard,
        # Never silently. A rail that quietly drops 213 entries is indistinguishable
        # from one that lost them, and the next person to wonder where a project
        # went has nothing to read.
        "projects_hidden": hidden,
        "map_age_seconds": map_age,
        "store_key": here,
        "claims": claims,
        "roster": roster,
        "feed": feed,
        "counts": {"claims": len(claims), "agents": len(roster)},
        "notes": [
            {"actor": n.actor, "body": getattr(n, "body", ""),
             "ts": n.ts.isoformat().replace("+00:00", "Z")}
            for n in list(getattr(st, "notes", []) or [])[-12:][::-1]
        ],
        "blocked_events": [
            {"actor": b.actor, "scope": getattr(b, "scope", ""),
             "holder": getattr(b, "holder", ""),
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
   comms / who holds which files
   The holds are the product. Everything else is a rail.
   Palette: warm paper + warm charcoal. Colour is SEMANTIC only:
     blue  = current / in progress   (also the brand accent)
     amber = needs a human           (a silent holder)
     red   = refused / error
     green = released
     grey  = inert information       (notes, archive)
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
/* Three nodes and the edges between them: the thing the tool is actually
   about. Drawn rather than filled so it stays legible at 16px: a solid glyph
   this small reads as a dot, and the whole point is that they are CONNECTED.
   The edges sit at lower opacity so the nodes carry the shape at a glance. */
.brand .mark { width: 16px; height: 16px; flex: none; display: block; }
.brand .mark path { stroke: var(--accent); stroke-width: 1.3; opacity: .55;
  fill: none; stroke-linecap: round; }
.brand .mark circle { fill: var(--accent); }
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
  font-size: 11px; color: var(--ink-2);
  padding: 1px 5px; border-radius: 3px; background: var(--surface-2);
}
/* overflow-y is not decoration. min-height:0 alone lets a flex child be
   SHORTER than its content, and with the default overflow:visible the surplus
   is painted outside the card rather than clipped: a 24-name roster spilled
   246px past its own border and rendered on top of the two panels beneath it.
   The card must scroll its own body or it is not a box. */
.card-bd { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
.card-ft {
  flex: none; border-top: 1px solid var(--line-hair);
  padding: var(--sp-1) var(--sp-2);
  font-size: 11px; color: var(--ink-3);
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

.plist { padding: 0 var(--sp-h) var(--sp-1); }

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
.prow[aria-selected="true"] { background: var(--accent-wash); color: var(--ink); }
.prow[aria-selected="true"] .pname { font-weight: 650; }
.pname {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 12.5px;
}
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

/* NOW band: live claims. Never a giant box; it is one strip that
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
  font-size: 10.5px; color: var(--ink-3);
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
.g-note i      { background: transparent; border: 1.5px solid var(--ink-4); border-radius: 99px; }
.g-agent i     { background: var(--ink-3); border-radius: 99px; }
.g-stale i     { background: var(--red); border-radius: 99px; }

.ev .body { padding: 5px 0 7px; min-width: 0; }
.ev .l1 { font-size: 12.5px; color: var(--ink); display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; }
.ev .verb { font-weight: 650; }
.ev .verb.a { color: var(--accent); }
.ev .verb.g { color: var(--green); }
.ev .verb.r { color: var(--red); }
.ev .path {
  font-family: var(--mono); font-size: 11.5px; color: var(--ink-2);
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ev .l2 { font-size: 11px; color: var(--ink-3); margin-top: 1px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ev .l2 .dot { width: 2px; height: 2px; border-radius: 99px; background: var(--ink-4); flex: none; }
.ev .actor { color: var(--ink-3); font-weight: 600; }
.ev .quote {
  margin-top: 5px; padding: 5px var(--sp-1) 5px 9px;
  border-left: 2px solid var(--line-2);
  background: var(--surface-2);
  border-radius: 0 3px 3px 0;
  font-size: 11.5px; color: var(--ink-2); line-height: 1.45;
}
.ev.k-note .quote { border-left-color: var(--line-2); }
.ev .quote code { font-family: var(--mono); font-size: 11px; background: var(--surface-3); padding: 0 3px; border-radius: 2px; }

/* empty states */
.empty {
  padding: var(--sp-6) var(--sp-3);
  display: flex; flex-direction: column; gap: var(--sp-1);
  max-width: 440px;
}
.empty p { margin: 0; font-size: 12px; color: var(--ink-4); line-height: 1.6; }
.empty ul { margin: var(--sp-h) 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; }
.empty li { font-size: 11.5px; color: var(--ink-4); display: flex; align-items: center; gap: 8px; }

/* ---------- right rail ---------- */
.rail-right { display: flex; flex-direction: column; gap: var(--sp-2); min-height: 0; }

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
.agent .x {
  width: 22px; height: 22px; border: 0; background: transparent; border-radius: 3px;
  color: var(--ink-4); opacity: 0; padding: 0; justify-content: center;
}
.agent:hover .x, .agent .x:focus-visible { opacity: 1; }
.agent .x:hover { background: var(--red-wash); color: var(--red); }

.needs { padding: var(--sp-1) var(--sp-2) var(--sp-1); display: flex; flex-direction: column; gap: 5px; }
.need {
  display: grid; grid-template-columns: auto minmax(0, 1fr); gap: var(--sp-1);
  align-items: baseline; font-size: 12px; cursor: pointer;
}
.need .id { font-family: var(--mono); font-size: 10.5px; color: var(--ink-4); }

/* mini DAG */
.dagwrap {
  border-top: 1px solid var(--line-hair);
  padding: var(--sp-1); overflow-x: auto;
  /* the graph is wider than the rail on purpose -- fade the cut so it reads
     as scrollable rather than clipped */
  -webkit-mask-image: linear-gradient(90deg, #000 calc(100% - 28px), transparent 100%);
  mask-image: linear-gradient(90deg, #000 calc(100% - 28px), transparent 100%);
}
.node {
  position: absolute; width: 122px; height: 40px;
  background: var(--surface-2); border: 1px solid var(--line-2); border-radius: 4px;
  padding: 4px 6px; display: flex; flex-direction: column; justify-content: center; gap: 1px;
  overflow: hidden;
}
.kv { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-1); margin-top: var(--sp-1); }
.kv div { display: flex; flex-direction: column; }
.arow {
  display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--sp-1);
  padding: 5px var(--sp-2); align-items: baseline;
}
.arow:hover { background: var(--surface-2); }
.arow .an { font-size: 12px; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
  font-size: 10.5px; color: var(--ink-3);
}

/* ---------------------------------------------------------------------
   Additions to the mockup's sheet, for the parts wired to real data.
   Every value here is one of its tokens: no new colours, no new spacing
   steps. If something wants a value that is not a token, the token set is
   what should change.
   --------------------------------------------------------------------- */

.grow { flex: 1 1 auto; min-width: 0; }
.amber { color: var(--amber); }
.alarm { font-size: 12px; padding: 2px var(--sp-1); border-radius: 5px;
  border: 1px solid var(--line); background: var(--surface-2); color: var(--ink-2);
  white-space: nowrap; max-width: 46ch; overflow: hidden; text-overflow: ellipsis; }
.alarm.r { color: var(--red); background: var(--red-wash); border-color: var(--red-line); }
.alarm.b { color: var(--accent); background: var(--accent-wash); border-color: var(--accent-line); }

/* projects rail */
.pgroup { display: flex; align-items: center; gap: var(--sp-1);
  font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase;
  color: var(--ink-4); padding: var(--sp-2) var(--sp-1) var(--sp-h); }
.pmore { color: var(--ink-4); font-size: 11.5px; padding: var(--sp-h) var(--sp-1) var(--sp-1); }
.pmore.rtoggle { cursor: pointer; padding-left: var(--sp-2); }
.pmore.rtoggle:hover { color: var(--accent); }
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
   to be taken out of that positioning: otherwise it lifts out of the layout
   and its container measures zero while the rows themselves are 165px tall. */
.nowband { border-bottom: 1px solid var(--line); background: var(--surface-2); }
.nowband-hd { display: flex; align-items: baseline; gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2) var(--sp-h);
  font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); }
.nowband-hd .count { color: var(--ink-3); text-transform: none; letter-spacing: 0; font-size: 11.5px; }
.nowband-empty { padding: var(--sp-h) var(--sp-2) var(--sp-2); color: var(--ink-4); font-size: 12.5px; }
/* The one empty state that is not a shrug: no claims WITH a dirty tree is a
   thing to look at, not an absence to move past. */
.nowband-empty.amber { color: var(--amber); }
.loosehd { border-top: 1px solid var(--line); }
.afiles.loose { padding-bottom: var(--sp-h); }
.afiles.loose .afrow { justify-content: flex-start; gap: var(--sp-2); }
.afiles.loose .how { color: var(--ink-3); font-size: 11.5px; }
.afiles.loose .how.amber { color: var(--amber); }
.afiles.loose .who { color: var(--ink-3); font-size: 11px; margin-left: auto; }
/* Aligned, not scattered. The paths were ragged across the full width with the
   state column floating wherever each name happened to end. */
.afiles.loose .afrow { display: grid; grid-template-columns: minmax(0, 1fr) auto;
  column-gap: var(--sp-2); align-items: baseline; }
.afiles.loose .afrow .how { justify-self: end; }
.afiles.loose .afrow .who { grid-column: 2; justify-self: end; }
/* Its own column order: the count is a label on the left and the sample of
   paths fills the rest. Inheriting .afrow's grid put the label in the flexible
   column and the paths in the rigid one, so "8 new paths not in git yet"
   wrapped to one word per line. */
.afiles.loose .afrow.freshrow { grid-template-columns: auto minmax(0, 1fr);
  color: var(--ink-4); }
.afiles.loose .afrow.freshrow .how { justify-self: start; white-space: nowrap; }
.freshlist { color: var(--ink-4); font-size: 11px; opacity: .8;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.guardwarn { padding: var(--sp-h) var(--sp-2); border-top: 1px solid var(--line);
  color: var(--amber); font-size: 11.5px; }
.guardwarn .mono { color: var(--ink-4); }
.why { color: var(--ink); }
.alsopaths { display: none; }
.alsopaths { color: var(--ink-4); font-size: 11px; margin: 1px 0 0;
  overflow-wrap: anywhere; }
.hactor { color: var(--accent); flex: none; }
.hintent { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* Grouped by agent: one row per agent, its files quiet underneath. */
.arow { display: flex; align-items: baseline; gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2) 3px; font-size: 12.5px; }
.arow.quiet { background: var(--amber-wash); }
.acount { color: var(--ink-3); flex: none; }
.afiles { padding: 0 0 var(--sp-1) 0; }
.afrow { display: flex; align-items: baseline; gap: var(--sp-1);
  padding: 1px var(--sp-2) 1px calc(var(--sp-2) * 2); font-size: 11.5px; }
.afpath { color: var(--ink-2); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; flex: 1 1 auto; }
.afrel { flex: none; font-size: 10px; padding: 0 5px; color: var(--ink-3);
  border-color: transparent; }
.afiles:not(.loose) .afpath { flex: 0 1 auto; max-width: 45%; }
.afrow .hintent { flex: 1 1 auto; }
.arow .rage, .afrow .rage { flex: none; color: var(--ink-3); font-size: 10.5px; }
.arow.quiet .rage { color: var(--amber); }
.arow.quiet .hintent, .arow.quiet .acount { color: var(--ink-2); }
@media (max-width: 720px) {
  .arow, .afrow { flex-wrap: wrap; }
  .arow .hintent, .afrow .hintent { white-space: normal; }
  .afiles:not(.loose) .afpath { flex-basis: 100%; max-width: none; white-space: normal; overflow-wrap: anywhere; }
}
.stream .activity-card { flex: 0 1 auto; max-height: 32%; }
.activity-card .card-bd { padding: 0 var(--sp-2) var(--sp-1); }
.afrel:hover { color: var(--red); border-color: var(--red-line); }
.arow .rel { flex: none; font-size: 10.5px; padding: 0 7px; color: var(--ink-3);
  border-color: var(--line-2); }
.arow .rel:hover { color: var(--red); border-color: var(--red-line); background: var(--red-wash); }
.k- { }

/* roster */
.rrow { display: flex; align-items: center; gap: var(--sp-1); padding: 5px var(--sp-2);
  border-bottom: 1px solid var(--line-hair); font-size: 12.5px; }
.rrow:last-child { border-bottom: 0; }
.av { flex: none; width: 20px; height: 20px; border-radius: 5px; display: grid; place-items: center;
  background: var(--surface-3); color: var(--ink-2); font-size: 10px; font-family: var(--mono); }
.rname { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rage { color: var(--ink-3); font-size: 11px; flex: none; }

/* this repo */
.sroot { padding: var(--sp-2) var(--sp-2) var(--sp-1); color: var(--ink-3); font-size: 11px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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

/* Work is the primary surface; logs and code stay available on demand. */
.topbar { height: 56px; }
.brand { font-size: 17px; }
.shell { height: calc(100vh - 56px); max-width: 1640px;
  grid-template-columns: 200px minmax(0, 1fr) 310px; gap: 18px; padding: 22px; }
.card { border-radius: 12px; box-shadow: none; }
.card-hd { height: 46px; padding: 0 16px; font-size: 14px; font-weight: 600; }
.work-heading { padding: 22px 22px 16px; border-bottom: 1px solid var(--line-hair); }
.work-heading h1 { margin: 0 0 4px; font-size: 25px; line-height: 1.3; letter-spacing: -.035em; }
.work-heading p { margin: 0; color: var(--ink-3); }
.work-toolbar { display: flex; gap: 6px; margin-top: 16px; }
.work-toolbar button { height: 32px; padding: 0 12px; }
.rail-right { display: flex; flex-direction: column; overflow-y: auto; gap: 18px; }
.rail-right > .card { flex: none; }
.rail-right > .card:first-child { max-height: none; }
.nowband { border: 0; background: none; }
.arow .hactor { font: inherit; font-weight: 550; }
.rrow { flex-wrap: wrap; padding: 12px 15px; }
.rname { overflow-wrap: anywhere; white-space: normal; }
.agent-history { margin-left: auto; }
.history-wrap { position: fixed; inset: 56px 0 0; z-index: 45; background: var(--bg); padding: 22px; }
.history-card { height: 100%; max-width: 1100px; margin: auto; }
.history-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 10px 16px; }
.history-scope { color: var(--ink-3); }
.history-controls .chips { overflow: auto; flex-wrap: wrap; }
#connectionNotice { padding: 10px 22px; background: var(--amber-wash); color: var(--amber); border-bottom: 1px solid var(--amber-line); }
body:has(#connectionNotice:not([hidden])) .shell { height: calc(100vh - 100px); }
#alarms { display: block; margin: 0 22px; }
#alarms:empty { display: none; }
.attention { margin-top: 16px; padding: 12px; background: var(--amber-wash); border-radius: 8px; font-size: 13px; }
.attention strong { display: block; margin-bottom: 4px; }
.attention p { margin: 0; color: var(--ink-2); }
.project-details { padding: 12px 14px; color: var(--ink-3); }
.project-details summary { cursor: pointer; }
.hint-note { color: var(--ink-3); line-height: 1.6; overflow-wrap: anywhere; }
.release-wrap { position: fixed; inset: 0; z-index: 80; padding: 20px; background: #0008; display: grid; place-items: center; }
.release-wrap[hidden] { display: none; }
.release-box { width: min(460px, 100%); max-height: 95vh; overflow-y: auto; padding: 24px; }
.release-box h2 { margin: 0 0 18px; font-size: 20px; }
.release-box label { display: block; margin: 18px 0 7px; }
.release-box input, .release-box textarea { box-sizing: border-box; width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: var(--bg); color: var(--ink); font: inherit; }
.release-box textarea { min-height: 80px; resize: vertical; }
.release-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
#releaseScope { overflow-wrap: anywhere; }
#releaseError { color: var(--red); margin-top: 12px; }
@media (max-width: 1100px) {
  .shell { grid-template-columns: 170px minmax(0, 1fr); overflow-y: auto; align-content: start; }
  .stream { min-height: 480px; }
  .rail-right { display: grid; grid-column: 1 / -1; grid-template-columns: 1fr 1fr; overflow: visible; }
  .rail-left { max-height: 580px; }
}
@media (max-width: 700px) {
  .shell { display: flex; flex-direction: column; padding: 12px; gap: 12px; }
  .rail-left { max-height: 180px; flex: none; }
  .rail-right { display: flex; flex: none; overflow: visible; }
  .stream { flex: none; min-height: 480px; }
  .work-heading { padding: 18px 16px 12px; }
  .work-heading h1 { font-size: 22px; }
  .topbar { padding: 0 15px; }
  .topbar .sep { display: none; }
  #clock { display: none; }
  .history-wrap { padding: 10px; }
}
@media (prefers-reduced-motion: reduce) { .livedot { animation: none; } }
</style>
<header class="topbar">
  <div class="brand"><svg class="mark" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.2 4.6 L11.8 6.2 M4.2 4.6 L7.4 11.6 M11.8 6.2 L7.4 11.6"/><circle cx="4.2" cy="4.6" r="2.3"/><circle cx="11.8" cy="6.2" r="2.3"/><circle cx="7.4" cy="11.6" r="2.3"/></svg>comms</div>
  <div class="sep"></div>
  <div class="live" role="status"><span class="livedot off" id="livedot"></span><span id="liveTxt">Loading work</span><span class="mono" id="clock"></span></div>
  <div class="sep"></div>
  <div class="grow"></div>
  <button id="themeBtn" class="icon ghost" title="Toggle light / dark" aria-label="Toggle theme">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path id="themeIcon" d="M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8 5.6 5.6 0 1 0 13.2 9.6Z" fill="currentColor"/>
    </svg>
  </button>
</header>
<div id="connectionNotice" role="status" hidden></div>

<main class="shell">
  <aside class="rail-left">
    <div class="card">
      <div class="card-hd"><span>Projects</span><span class="count" id="projCount"></span></div>
      <div class="search"><input id="projQ" aria-label="Find a project" type="search" placeholder="Find a project" autocomplete="off" spellcheck="false"></div>
      <div class="card-bd scroll" id="projScroll"><div class="plist" id="projList"></div></div>
      <div class="card-ft foot" id="projFoot"></div>
    </div>
  </aside>

  <section class="stream">
    <div class="card">
      <div class="work-heading">
        <h1 id="projectTitle">Who holds which files</h1>
        <div class="work-toolbar">
          <button id="historyBtn">History</button>
          <button id="dagBtn" class="ghost">Code map</button>
        </div>
      </div>
      <div id="alarms"></div>
      <div class="card-bd"><div id="nowBand"><div class="empty">Loading claims…</div></div></div>
    </div>
    <div class="card activity-card">
      <div class="card-hd"><span>Recent activity</span><span class="count" id="activityCount"></span></div>
      <div class="card-bd" id="activity"></div>
    </div>
  </section>

  <aside class="rail-right">
    <div class="card">
      <div class="card-hd"><span>Team</span><span class="count" id="rosterCount"></span></div>
      <div class="card-bd" id="roster"></div>
    </div>
    <div class="card">
      <details class="project-details"><summary>Project details</summary>
        <div class="card-bd scroll" id="sessionScroll"><div id="session"></div></div>
      </details>
    </div>
  </aside>
</main>

<section class="history-wrap" id="historyWrap" hidden role="dialog" aria-modal="true" aria-label="Work history">
  <div class="card history-card">
    <div class="card-hd"><span>History</span><span id="historyScope" class="history-scope"></span>
      <div class="grow"></div><button class="ghost" id="historyClose">Close</button></div>
    <div class="history-controls"><div class="chips" id="chips"></div><button class="ghost" id="historyAll">All agents</button></div>
    <div class="card-bd streamwrap"><div class="scroll" id="streamScroll"><div id="streamList"></div></div>
      <div class="newpill" id="newPill" hidden></div></div>
    <div class="card-ft"><span id="evCount"></span> events shown <span id="historyStatus" role="status"></span>
      <button class="ghost" id="historyMore" hidden>Load earlier</button>
      <button class="ghost" id="historyReload">Refresh history</button></div>
  </div>
</section>

<section class="release-wrap" id="releaseWrap" hidden role="dialog" aria-modal="true" aria-labelledby="releaseTitle" aria-describedby="releaseHelp">
  <form class="card release-box" id="releaseForm">
    <h2 id="releaseTitle">Release this hold?</h2>
    <div id="releaseScope"></div><p class="hint-note" id="releaseOwner"></p>
    <p class="hint-note" id="releaseHelp">This does not stop the agent or delete its work. A quiet agent may still be running.</p>
    <label for="releaseActor">Your name</label><input id="releaseActor" required autocomplete="off">
    <label for="releaseReason">Reason</label><textarea id="releaseReason" required placeholder="Why is this hold no longer needed?"></textarea>
    <div id="releaseError" role="alert"></div>
    <div class="release-actions"><button type="button" class="ghost" id="releaseCancel">Cancel</button><button type="submit" id="releaseConfirm">Release hold</button></div>
  </form>
</section>

<div class="dagwrap" id="dagWrap" hidden>
  <div class="dagbox">
    <div class="dagbar">
      <button class="ghost gtab on" id="gMap" data-src="/map.html">Code map</button>
      <div class="grow"></div><button id="dagClose" class="ghost">Close</button>
    </div>
    <iframe id="dagFrame" title="Code map"></iframe>
  </div>
</div>
<script>
var PAGE_BUILD = "__COMMS_FRONTEND_BUILD_TOKEN__";
var D = null;              // the last snapshot
var FILTER = "all";        // which stream chip is active
var PQ = "";               // projects filter box
var PAUSED_AT_BOTTOM = true;
var RELOAD_REQUESTED = false;
var LAST_SNAPSHOT_AT = 0;
var LOAD_STARTED_AT = Date.now();
var HISTORY_SCOPE = null;
var HISTORY_EVENTS = null;
var HISTORY_BEFORE = "";
var HISTORY_REQUEST = 0;

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
  // Keep the tail: the filename is what identifies a claim at a glance, and
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

/* The store key in the URL, which is the project every panel is drawn from. */
function currentStore() {
  var m = /[?&]store=([^&]+)/.exec(window.location.search || "");
  return m ? decodeURIComponent(m[1]) : "";
}

function rosterEntry(actor) {
  return (D.roster || []).filter(function (a) { return a.actor === actor; })[0] || {};
}

/* An auto-claim is the edit check taking the file on first touch. Its stored
   intent is internal text, so it reads as what the agent is doing. */
function intentText(c) {
  return c.auto ? "editing" : (c.intent || "");
}

function renderHolds() {
  var cs = D.claims || [];
  var dirt = D.dirty || {};
  // NO REPOSITORY IS NOT A FAILED READ. Run across every project (the login
  // service does, from /), there is no working tree to have an opinion about,
  // and reporting "the working tree could not be read: not a git repository"
  // is alarm about the absence of a question.
  var noRepoHere = dirt.readable === false &&
                   /not a git repository/i.test(dirt.unavailable || "");
  if (noRepoHere) { dirt = {}; }
  var h = '<div class="nowband">';
  if (!cs.length) {
    // "Nobody is holding anything" is only reassuring if the tree agrees.
    if (dirt.readable === false) {
      h += '<div class="nowband-empty">Nobody holds a file right now, and ' +
           'the working tree could not be read (' + esc(dirt.unavailable || "no reason given") +
           '), so this is not the same as quiet.</div>';
    } else if (dirt.total) {
      h += '<div class="nowband-empty amber">Nobody holds a file, but ' +
           dirt.total + (dirt.total === 1 ? " file has" : " files have") +
           ' uncommitted changes.</div>';
    } else if (noRepoHere) {
      h += '<div class="nowband-empty">Nobody holds a file. This view spans ' +
           'every project, so there is no single working tree to check. Open one on ' +
           'the left to see what is changed in it.</div>';
    } else {
      h += '<div class="nowband-empty">Nobody holds a file, and there are ' +
           'no uncommitted changes.</div>';
    }
  } else {
    // BY AGENT, not by file: the question somebody arrives with is "is that
    // agent still alive, and if not, free its files". Whoever has gone quiet
    // sorts first, because that is the one that needs a person.
    var byActor = {};
    cs.forEach(function (c) { (byActor[c.actor] = byActor[c.actor] || []).push(c); });
    Object.keys(byActor).sort(function (a, b) {
      var qa = byActor[a].some(function (c) { return c.quiet; }) ? 0 : 1;
      var qb = byActor[b].some(function (c) { return c.quiet; }) ? 0 : 1;
      return qa - qb || byActor[b].length - byActor[a].length;
    }).forEach(function (actor) {
      var held = byActor[actor];
      var who = rosterEntry(actor);
      var quiet = held.some(function (c) { return c.quiet; });
      var idle = Math.min.apply(null, held.map(function (c) { return c.idle_seconds || 0; }));
      var about = [who.label, who.model].filter(Boolean).join(" · ");
      h += '<div class="arow' + (quiet ? " quiet" : "") + '">';
      h += '<button class="hactor ghost agent-history" data-history-actor="' + esc(actor) + '">@' + esc(actor) + "</button>";
      if (about) { h += '<span class="hintent">' + esc(about) + "</span>"; }
      h += '<span class="grow"></span>';
      h += '<span class="acount">' + held.length + (held.length === 1 ? " file" : " files") + "</span>";
      h += '<span class="rage mono' + (quiet ? " amber" : "") + '">' +
           (quiet ? "Silent for " + ago(idle) : "Active " + ago(idle) + " ago") + "</span>";
      h += "</div>";
      h += '<div class="afiles">';
      held.forEach(function (c) {
        var since = Date.parse(c.ts || "");
        var heldFor = isFinite(since) ? ago((Date.now() - since) / 1000) : "";
        h += '<div class="afrow" title="claim ' + esc(c.id) + '">' +
             '<span class="mono afpath">' + esc(shortPath(c.scope)) + "</span>" +
             '<span class="hintent">' + esc(intentText(c)) + "</span>" +
             '<span class="rage mono">' + (heldFor ? "held " + heldFor : "") + "</span>" +
             '<button class="ghost rel afrel" data-id="' + esc(c.id) + '" data-scope="' +
             esc(c.scope) + '" data-actor="' + esc(actor) + '">Release</button></div>';
      });
      h += "</div>";
    });
  }
  // Everything dirty that NO LIVE CLAIM covers: the half of the picture no
  // agent has to remember to report. Edits to tracked files are listed;
  // untracked paths are usually build output, so they are summed up in one row.
  var loose = (dirt.files || []).filter(function (f) { return f.basis !== "held"; });
  if (loose.length) {
    var edited = loose.filter(function (f) { return f.how.indexOf("new, not in git") !== 0; });
    var fresh  = loose.filter(function (f) { return f.how.indexOf("new, not in git") === 0; });
    h += '<div class="nowband-hd loosehd"><span>Changed on disk, nobody holding it</span>' +
         '<span class="count">' + loose.length + " of " + dirt.total + "</span></div>";
    h += '<div class="afiles loose">';
    edited.slice(0, 25).forEach(function (f) {
      h += '<div class="afrow">' +
           '<span class="mono afpath">' + esc(shortPath(f.path)) + "</span>" +
           '<span class="how' + (f.deleted ? " amber" : "") + '">' + esc(f.how) + "</span>" +
           (f.actor ? '<span class="who mono">@' + esc(f.actor) + " let go of it</span>" : "") +
           "</div>";
    });
    if (edited.length > 25) {
      h += '<div class="pmore">and ' + (edited.length - 25) + " more</div>";
    }
    if (fresh.length) {
      h += '<div class="afrow freshrow" title="' +
           esc(fresh.map(function (f) { return f.path; }).join(String.fromCharCode(10))) + '">' +
           '<span class="how">' + fresh.length +
           (fresh.length === 1 ? " new path" : " new paths") + " not in git yet</span>" +
           '<span class="mono freshlist">' +
           esc(fresh.slice(0, 4).map(function (f) { return shortPath(f.path); }).join("  ")) +
           (fresh.length > 4 ? "  +" + (fresh.length - 4) : "") + "</span></div>";
    }
    h += "</div>";
  }
  // An unenforced guard looks exactly like an enforced one right up until
  // somebody commits over a live claim. Say which this repo is.
  var g = noRepoHere ? {} : (D.guard || {});
  if (g.installed === false) {
    h += '<div class="guardwarn">' + esc(g.text || "") + "</div>";
  }
  h += "</div>";
  el("nowBand").innerHTML = h;
  bindHistoryButtons(el("nowBand"));
  Array.prototype.forEach.call(el("nowBand").querySelectorAll(".rel"), function (b) {
    b.onclick = function () { releaseClaim(b); };
  });
}

/* Who is at the keyboard.

   A release is written under a name, permanently, and the board used to take
   that name from the server process alone: started without COMMS_ACTOR, it
   could free nothing, and "Free all" answered with the same refusal once per
   claim. The person clicking is the one making the judgement, so the page asks
   in the confirmation form and remembers the answer after a successful
   release. `?actor=name` supplies a default; the operator can change it. */
function boardActor() {
  var m = /[?&]actor=([^&]+)/.exec(window.location.search || "");
  var fromUrl = "";
  try { fromUrl = m ? decodeURIComponent(m[1]).replace(/^@+/, "").trim() : ""; } catch (e) {}
  if (fromUrl) return fromUrl;
  var saved = "";
  try { saved = (localStorage.getItem("comms.actor") || "").trim(); } catch (e) {}
  return saved || D.board_actor || "";
}

var RELEASE_PENDING = null;
var RELEASE_BUSY = false;
function releaseClaim(btn) {
  RELEASE_PENDING = {id: btn.getAttribute("data-id"), actor: btn.getAttribute("data-actor"),
    scope: btn.getAttribute("data-scope"), store: currentStore(), button: btn};
  el("releaseScope").textContent = RELEASE_PENDING.scope;
  el("releaseOwner").textContent = "Held by @" + RELEASE_PENDING.actor;
  el("releaseActor").value = boardActor();
  el("releaseReason").value = "";
  el("releaseError").textContent = "";
  el("releaseWrap").hidden = false;
  el(el("releaseActor").value ? "releaseReason" : "releaseActor").focus();
}
function closeRelease() {
  if (RELEASE_BUSY) return;
  var previous = RELEASE_PENDING;
  RELEASE_PENDING = null;
  el("releaseWrap").hidden = true;
  if (previous) {
    var target = previous.button.isConnected ? previous.button : el("historyBtn");
    if (target && target.focus) target.focus();
  }
}
function submitRelease(e) {
  e.preventDefault();
  if (!RELEASE_PENDING || RELEASE_BUSY) return;
  var me = el("releaseActor").value.trim().replace(/^@+/, "");
  var reason = el("releaseReason").value.trim();
  if (!me || !reason) {
    el("releaseError").textContent = "Enter your name and a reason.";
    return;
  }
  var pending = RELEASE_PENDING;
  RELEASE_BUSY = true;
  el("releaseConfirm").disabled = true;
  el("releaseCancel").disabled = true;
  el("releaseConfirm").textContent = "Releasing…";
  el("releaseError").textContent = "";
  fetch("/api/release", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: pending.id, reason: reason, store: pending.store, actor: me})
  }).then(function (r) {
    return r.json().then(function (body) {
      if (!r.ok || !body.ok) throw new Error(body.error || "Release was not confirmed.");
    });
  }).then(function () {
    try { localStorage.setItem("comms.actor", me); } catch (e) {}
    RELEASE_BUSY = false;
    closeRelease();
    fetch("/api/status?store=" + encodeURIComponent(pending.store)).then(function (r) {
      if (!r.ok) throw new Error("Status unavailable");
      return r.json();
    }).then(acceptSnapshot).catch(function () {
      setConnectionState("Update pending", "The hold was released. Waiting for the board to update.");
    });
  }).catch(function (error) {
    el("releaseError").textContent = error.message || "Could not confirm release. Check history before retrying.";
  }).finally(function () {
    RELEASE_BUSY = false;
    el("releaseConfirm").disabled = false;
    el("releaseCancel").disabled = false;
    el("releaseConfirm").textContent = "Release hold";
  });
}

/* ---------- the stream ------------------------------------------------- */

var KINDS = [
  ["all", "All"], ["claim", "Claims"], ["note", "Notes"], ["session", "Session"]
];
function matchKind(e) {
  if (HISTORY_SCOPE && HISTORY_SCOPE.kind === "actor" && e.actor !== HISTORY_SCOPE.value && e.original_actor !== HISTORY_SCOPE.value && (e.original_actors || []).indexOf(HISTORY_SCOPE.value) < 0) return false;
  if (FILTER === "all") return true;
  if (FILTER === "claim") return e.type === "claim" || e.type === "release";
  if (FILTER === "session") return e.type === "hello" || e.type === "blocked";
  return e.type === FILTER;
}

function openHistory(kind, value) {
  HISTORY_SCOPE = kind && value ? {kind: kind, value: value} : null;
  FILTER = "all";
  el("historyScope").textContent = HISTORY_SCOPE ? (kind === "actor" ? "@" : "") + value : "";
  el("historyWrap").hidden = false;
  HISTORY_EVENTS = null;
  HISTORY_BEFORE = "";
  renderChips(); renderStream();
  el("historyClose").focus();
  loadHistory(false);
}
function loadHistory(append) {
  var request = ++HISTORY_REQUEST;
  var url = "/api/history?store=" + encodeURIComponent(currentStore());
  if (HISTORY_SCOPE) url += "&" + HISTORY_SCOPE.kind + "=" + encodeURIComponent(HISTORY_SCOPE.value);
  if (append && HISTORY_BEFORE) url += "&before=" + encodeURIComponent(HISTORY_BEFORE);
  el("historyStatus").textContent = " · Loading…";
  el("historyMore").disabled = true;
  fetch(url).then(function (r) {
    if (!r.ok) throw new Error("History could not be loaded.");
    return r.json();
  }).then(function (page) {
    if (request !== HISTORY_REQUEST) return;
    if (!Array.isArray(page.events)) throw new Error("Unreadable history.");
    HISTORY_EVENTS = append ? (HISTORY_EVENTS || []).concat(page.events) : page.events;
    HISTORY_BEFORE = page.next_before || "";
    el("historyStatus").textContent = "";
    el("historyMore").hidden = !HISTORY_BEFORE;
    el("historyMore").disabled = false;
    renderChips(); renderStream();
  }).catch(function () {
    if (request !== HISTORY_REQUEST) return;
    el("historyStatus").textContent = " · Could not load history. Refresh to retry; existing entries are kept.";
    el("historyMore").disabled = false;
  });
}
function bindHistoryButtons(container) {
  Array.prototype.forEach.call(container.querySelectorAll("[data-history-actor]"), function (b) {
    b.onclick = function () { openHistory("actor", b.getAttribute("data-history-actor")); };
  });
}
function renderChips() {
  var saveFilter = FILTER;
  FILTER = "all";
  var f = (HISTORY_EVENTS || D.feed || []).filter(matchKind);
  FILTER = saveFilter;
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
  if (e.type === "note") return "g-note";
  if (e.type === "hello") return "g-agent";
  return "g-stale";
}
function eventRow(e, isLast) {
  // `last` stops the timeline spine running past the final row of a group.
  var s = '<div class="ev k-' + esc(e.type) + (isLast ? " last" : "") + '">';
  var date = new Date(e.ts);
  var day = !isNaN(date) && date.toDateString() !== new Date().toDateString()
    ? date.toLocaleDateString(undefined, {month: "short", day: "numeric", year: "numeric"}) : "";
  s += '<div class="t mono">' + (day ? esc(day) + "<br>" : "") + esc(hhmmss(e.ts)) + "</div>";
  s += '<div class="spine"><span class="glyph ' + glyphClass(e) + '"><i></i></span></div>';
  s += '<div class="body">';

  if (e.type === "claim" || e.type === "release") {
    var verb = e.type === "claim" ? "claimed" : "released";
    s += '<div class="l1"><span class="verb ' + (e.type === "claim" ? "a" : "g") + '">' + verb + "</span>";
    // One atomic claim of several files is one row. The paths are all named
    // rather than counted, because a count is exactly what nobody can act on.
    // THE REASON FIRST, THE PATHS AFTER. A row led with
    // `frontend/src/features/map/AconMap.tsx` tells a reader who does not read
    // code nothing at all, while "Nachlauf-Timer sauber" tells them what
    // happened. The paths stay, quieter, because they are what an agent greps.
    var many = (e.scopes && e.scopes.length > 1) ? e.scopes : null;
    var why = e.auto ? "editing" : (e.intent || e.result || "");
    if (why) {
      s += '<span class="why">' + esc(why) + "</span>";
    } else {
      s += '<span class="path mono">' + esc(shortPath(many ? many[0] : e.scope)) + "</span>";
    }
    s += "</div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + "</span>";
    if (why) {
      s += '<span class="dot"></span><span class="path mono">' +
           esc(shortPath(many ? many[0] : e.scope)) +
           (many ? " +" + (many.length - 1) : "") + "</span>";
    }
    s += "</div>";
    if (many && many.length > 1) {
      s += '<div class="alsopaths mono">' +
           many.slice(1).map(function (x) { return esc(shortPath(x)); }).join(", ") +
           "</div>";
    }

  } else if (e.type === "note") {
    s += '<div class="l1"><span class="verb">note</span></div>';
    s += '<div class="quote">' + esc(e.body || "") + "</div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + "</span></div>";

  } else if (e.type === "blocked") {
    s += '<div class="l1"><span class="verb r">refused</span>';
    s += '<span class="path mono">' + esc(shortPath(e.scope)) + "</span></div>";
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
  var f = (HISTORY_EVENTS || D.feed || []).filter(matchKind);
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
    ["Last 24 hours", 86400], ["Earlier", Infinity]
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
var rosterAll = false;

function renderRoster() {
  var r = D.roster || [];
  if (!r.length) {
    el("rosterCount").textContent = 0;
    el("roster").innerHTML = '<div class="empty">Nobody has done anything here yet.</div>';
    return;
  }
  // WHO IS HERE, not everyone who has ever been. Agents take a fresh name each
  // session: claude-karte, claude-kartenansicht, claude-karte-fachebenen are
  // one person on three days: so the list only grows and the three who are
  // actually working sit among twenty who are not. Measured on the real store:
  // 24 names, 3 seen in the last hour, 16 older than a week.
  //
  // Anyone HOLDING a claim stays visible whatever their age. A stale claim is
  // the one thing on this board that needs a human, and hiding its holder would
  // hide the only name that can free it.
  function idleOf(a) { return a.last_seen ? (Date.now() - Date.parse(a.last_seen)) / 1000 : 1e9; }
  var here = r.filter(function (a) { return idleOf(a) < 3600 || a.holding; });
  var rest = r.filter(function (a) { return here.indexOf(a) === -1; });
  var shown = rosterAll ? here.concat(rest) : here;

  el("rosterCount").textContent = here.length;
  var h = "";
  if (!shown.length && !here.length) {
    h += '<div class="held-empty">Nobody has been active in the last hour.</div>';
  }
  shown.forEach(function (a) {
    var silent = idleOf(a) >= 3600;
    h += '<div class="rrow">';
    h += '<span class="av">' + esc(initials(a.actor)) + "</span>";
    h += '<span class="rname">@' + esc(a.actor) + "</span>";
    if (!a.identified) { h += '<span class="tag">unidentified</span>'; }
    if (a.holding) { h += '<span class="tag amber">holding ' + a.holding + "</span>"; }
    h += '<span class="grow"></span>';
    h += '<span class="rage mono' + (silent ? " amber" : "") + '">Reported ' + esc(agoIso(a.last_seen)) + " ago</span>";
    h += '<button class="ghost agent-history" data-history-actor="' + esc(a.actor) + '">History</button>';
    h += "</div>";
  });
  if (rest.length) {
    h += '<div class="pmore rtoggle">' + (rosterAll
          ? "show only who is here"
          : rest.length + " more from earlier days") + "</div>";
  }
  el("roster").innerHTML = h;
  bindHistoryButtons(el("roster"));
  var t = el("roster").querySelector(".rtoggle");
  if (t) { t.onclick = function () { rosterAll = !rosterAll; renderRoster(); }; }
}

function closeHistory() {
  if (el("historyWrap").hidden) return;
  el("historyWrap").hidden = true;
  el("historyBtn").focus();
}

/* ---------- this repo --------------------------------------------------- */

function renderSession() {
  var c = D.counts || {};
  // Truncated HERE rather than by CSS. The obvious trick -- direction:rtl
  // plus text-overflow -- clips at the correct end, but bidi then reorders
  // the neutral characters at the edges: a leading "/" is placed at the far
  // right, so "/Users/me/Projects/comms" displayed as ".../Projects/comms/",
  // a path that does not exist. A wrong path is worse than a long one.
  var root = D.root || "";
  var parts = root.split("/").filter(Boolean);
  var shortRoot = parts.length > 2 ? "\u2026/" + parts.slice(-2).join("/") : root;
  var h = '<div class="sroot mono" title="' + esc(root) + '">' + esc(shortRoot) + "</div>";
  h += '<div class="stats">';
  [["events", (D.feed || []).length], ["claims", c.claims || 0],
   ["notes", (D.notes || []).length]].forEach(function (kv) {
    h += '<div class="scell"><div class="sn mono">' + kv[1] + '</div><div class="sl">' + kv[0].toUpperCase() + "</div></div>";
  });
  h += "</div>";
  (D.alerts || []).filter(function (a) { return a.kind === "stale-map"; }).forEach(function (a) {
    h += '<p class="hint-note">' + esc(a.text) + "</p>";
  });
  h += '<div class="sgen">read ' + esc(D.generated || "") + "</div>";
  el("session").innerHTML = h;
}

/* ---------- alarms ------------------------------------------------------ */

function renderAlarms() {
  var quiet = (D.alerts || []).some(function (x) { return x.kind === "quiet"; });
  el("alarms").innerHTML = quiet
    ? '<div class="attention"><strong>A hold may need your attention</strong><p>' +
      "An agent holding files has been silent for over an hour. It may still be running, " +
      "so check before you release anything.</p></div>"
    : "";
}

/* The newest events, the same rows History shows. */
function renderActivity() {
  var f = (D.feed || []).slice(0, 15);
  el("activityCount").textContent = f.length ? f.length : "";
  el("activity").innerHTML = f.length
    ? f.map(function (e, i) { return eventRow(e, i === f.length - 1); }).join("")
    : '<div class="empty">Nothing has happened here yet.</div>';
}

/* ---------- driver ------------------------------------------------------ */

function renderAll(d) {
  D = d;
  if (d.error) {
    el("alarms").innerHTML = '<span class="alarm r">' + esc(d.error) + "</span>";
    el("streamList").innerHTML = '<div class="empty">' + esc(d.error) + "</div>";
    return;
  }
  renderAlarms(); renderProjects(); renderHolds(); renderActivity();
  renderChips(); renderStream(); renderRoster(); renderSession();
}

function acceptSnapshot(d) {
  if (!d || typeof d !== "object" || d.error ||
      !Array.isArray(d.claims) || !Array.isArray(d.feed)) {
    throw new Error(d && d.error ? d.error : "The board received an unreadable update.");
  }
  if (d.frontend_build && d.frontend_build !== PAGE_BUILD) {
    if (!RELOAD_REQUESTED) {
      RELOAD_REQUESTED = true;
      es.close();
      location.reload();
    }
    return;
  }
  var previous = D;
  try { renderAll(d); }
  catch (error) {
    D = previous;
    if (previous) renderAll(previous);
    throw error;
  }
  LAST_SNAPSHOT_AT = Date.now();
  setConnectionState("Live", "");
}

function setConnectionState(label, detail) {
  el("liveTxt").textContent = label;
  el("livedot").classList.toggle("off", label !== "Live");
  var notice = el("connectionNotice");
  notice.hidden = !detail;
  notice.textContent = detail;
}

el("chips").addEventListener("click", function (ev) {
  var b = ev.target.closest(".chip"); if (!b) return;
  FILTER = b.getAttribute("data-k"); renderChips(); renderStream();
});
el("projQ").addEventListener("input", function (ev) { PQ = ev.target.value.trim(); renderProjects(); });
el("historyBtn").addEventListener("click", function () { if (D) openHistory(); });
el("historyAll").addEventListener("click", function () { if (D) openHistory(); });
el("historyMore").addEventListener("click", function () { if (HISTORY_BEFORE) loadHistory(true); });
el("historyReload").addEventListener("click", function () { loadHistory(false); });
el("historyClose").addEventListener("click", closeHistory);
el("releaseCancel").addEventListener("click", closeRelease);
el("releaseForm").addEventListener("submit", submitRelease);
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") { closeRelease(); closeHistory(); }
  if (e.key === "Tab") {
    var modal = !el("releaseWrap").hidden ? el("releaseWrap") : !el("historyWrap").hidden ? el("historyWrap") : null;
    if (!modal) return;
    var focusable = Array.prototype.slice.call(modal.querySelectorAll("button, a[href], input, select, textarea, summary, [tabindex='0']"))
      .filter(function (node) { return !node.disabled && node.getClientRects().length; });
    var first = focusable[0], last = focusable[focusable.length - 1];
    if (first && (focusable.indexOf(document.activeElement) < 0 || (e.shiftKey ? document.activeElement === first : document.activeElement === last))) {
      e.preventDefault(); (e.shiftKey ? last : first).focus();
    }
  }
});

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

// The code map, on demand. It is a whole vis-network page in an iframe and
// most of the day nobody wants it, so it costs nothing until asked for.
(function () {
  var wrap = el("dagWrap"), frame = el("dagFrame"), shown = "";
  function show(src, btn) {
    var store = currentStore();
    if (store) { src += "?store=" + encodeURIComponent(store); }
    // Loaded only when asked for, and only once per project.
    if (shown !== src) { frame.src = src; shown = src; }
    wrap.hidden = false;
  }
  el("dagBtn").addEventListener("click", function () { show("/map.html", el("gMap")); });
  el("gMap").addEventListener("click", function () { show("/map.html", el("gMap")); });
  el("dagClose").addEventListener("click", function () { wrap.hidden = true; });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { wrap.hidden = true; } });
})();

function tick() {
  el("clock").textContent = LAST_SNAPSHOT_AT ? "updated " + ago((Date.now() - LAST_SNAPSHOT_AT) / 1000) + " ago" : "";
  if (LAST_SNAPSHOT_AT && Date.now() - LAST_SNAPSHOT_AT > 30000) {
    setConnectionState("Updates delayed", "Showing the last received work. Updates are delayed; reconnecting does not stop any agent.");
  } else if (!LAST_SNAPSHOT_AT && Date.now() - LOAD_STARTED_AT > 15000) {
    setConnectionState("Still loading", "Work has not loaded yet. This is not an empty project. You can reload this page to retry.");
  }
}
setInterval(tick, 1000); tick();

var live = el("liveTxt"), dot = el("livedot");
var es = new EventSource("/events" + location.search);
es.onopen = function () { setConnectionState(LAST_SNAPSHOT_AT ? "Reconnecting" : "Loading work", ""); };
es.onerror = function () {
  setConnectionState("Reconnecting", LAST_SNAPSHOT_AT
    ? "Connection lost. Your last received work is still shown; reconnecting automatically."
    : "Waiting for the board connection. Work has not loaded yet.");
};
es.onmessage = function (ev) {
  try { acceptSnapshot(JSON.parse(ev.data)); }
  catch (e) { setConnectionState("Update failed", "Could not read the latest update. " + (LAST_SNAPSHOT_AT ? "Your previous work is still shown." : "Work has not loaded yet.")); }
};
</script>
"""


def _frontend_build_token(page: str) -> str:
    """Identify the exact in-memory document served by this process."""
    return hashlib.sha256(page.encode("utf-8")).hexdigest()


#: Appended to the embedded code map to give the graph the whole pane.
#:
#: The map is a complete, standalone page with its own right-hand panel
#: (search, node info, a community filter). Inside the board, beside the
#: board's own rail, that panel squeezed the graph into what was left.
#:
#: Hidden rather than removed, so the map keeps working on its own and its
#: generator does not have to know it is inside a frame.
_EMBED_CSS = (
    "<style>"
    "#comms-panel,#sidebar,#side{display:none!important}"
    # Collapse the TRACK the panel occupied, not just the panel. #wrap is a grid
    # with a fixed second column, so hiding #side left a 290px empty track and
    # the graph stayed short by exactly that much: the picture then read as
    # small and left-biased in a pane that looked like it had room.
    "#wrap{grid-template-columns:minmax(0,1fr)!important}"
    "#graph,#wrap{width:100%!important;flex:1 1 auto!important}"
    "body{overflow:hidden!important}"
    "</style>"
    # Hiding the panel widens the canvas, and an ELEMENT resize fires no window
    # resize event, so vis kept the narrower viewport it had measured at
    # construction and the picture sat small and off-centre in a pane with room
    # to spare. Both pages already re-fit on window resize; this gives them the
    # event they were waiting for, and a ResizeObserver keeps it true afterwards
    # when the browser window changes or a pane is dragged.
    "<script>(function(){"
    "var g=document.getElementById('graph')||document.body;"
    "var fire=function(){window.dispatchEvent(new Event('resize'));"
    "var n=window.network;"
    # setSize BEFORE redraw: vis measured its canvas when the panel was still
    # there, and fit() centres inside the CANVAS, not inside the container. So
    # the picture sat centred in a box narrower than the pane and read as
    # left-biased and small. redraw() alone does not re-measure: setSize is
    # what makes it look again.
    "if(n){try{n.setSize('100%','100%');n.redraw();"
    "if(n.fit){n.fit({animation:false});}}catch(e){}}};"
    "if(window.ResizeObserver){new ResizeObserver(fire).observe(g);}"
    "setTimeout(fire,0);setTimeout(fire,120);setTimeout(fire,400);"
    "})();</script>"
)


class _DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server whose close boundary preserves accepted writes."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, *args, **kwargs) -> None:
        self.shutdown_requested = threading.Event()
        super().__init__(*args, **kwargs)

    def verify_request(self, request, client_address) -> bool:
        """Reject work accepted by the socket after graceful shutdown began."""
        return not self.shutdown_requested.is_set()

    def shutdown(self) -> None:
        # Wake long-lived SSE handlers before waiting for serve_forever().
        self.shutdown_requested.set()
        super().shutdown()

    def server_close(self) -> None:
        # Also covers startup errors and KeyboardInterrupt, whose cleanup can
        # reach server_close() without going through shutdown().
        self.shutdown_requested.set()
        super().server_close()


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

    def do_POST(self) -> None:  # noqa: N802
        """The only write this server does. Everything else is GET."""
        path = self.path.split("?", 1)[0]
        board = self.server.board  # type: ignore[attr-defined]
        if path != "/api/release":
            self._send(b'{"error":"not found"}', "application/json", 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(b'{"error":"bad request body"}', "application/json", 400)
            return
        code, body = board.release(str(payload.get("id") or ""),
                                   str(payload.get("reason") or ""),
                                   str(payload.get("store") or ""),
                                   str(payload.get("actor") or ""))
        self._send(json.dumps(body).encode("utf-8"), "application/json", code)

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        board = self.server.board  # type: ignore[attr-defined]
        store = ""
        for part in query.split("&"):
            if part.startswith("store="):
                store = unquote(part[len("store="):])
        if path == "/":
            self._send(board.page().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/history":
            params = parse_qs(query)
            try:
                payload = board.history(store, actor=params.get("actor", [""])[0],
                                        before=params.get("before", [""])[0])
                self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")
            except (ValueError, OSError) as exc:
                self._send(json.dumps({"error": str(exc)}).encode("utf-8"), "application/json", 400)
        elif path == "/api/status":
            self._send(json.dumps(board.snapshot(store)).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/map.html":
            self._send(board.map_html(store).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/events":
            self._stream(board, store)
        else:
            self._send(b"not found", "text/plain; charset=utf-8", 404)

    def _stream(self, board, store: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = None
        stopping = self.server.shutdown_requested  # type: ignore[attr-defined]
        try:
            while not stopping.is_set():
                stamp = board.stamp()
                snap = board.snapshot(store)
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
                if stopping.wait(POLL_SECONDS):
                    break
                waited = POLL_SECONDS
                while board.stamp() == last and waited < 10.0:
                    if stopping.wait(POLL_SECONDS):
                        break
                    waited += POLL_SECONDS
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The reader closed the tab. Entirely normal for a stream somebody
            # leaves open all day, and it used to print a traceback on the
            # terminal running the board, which reads as something being wrong.
            pass
        finally:
            self.close_connection = True


def _actor_name(raw: str) -> str:
    """One rule for an actor typed at the board, the same one the CLI applies.

    The @ is display, never identity: every surface prints "@name", so a name
    copied off the board arrives with one, and a stored "@name" would be a
    different agent from "name". Whitespace around it is noise. Anything else
    is the person's business: `--as` accepts it, and the board should not be
    stricter than the tool it fronts.
    """
    return (raw or "").strip().lstrip("@").strip()


class Board:
    """Holds what the pages need and regenerates them only when the log moves."""

    def __init__(self, root: Path, log_file: Path, graph_file: str | None = None) -> None:
        self.root = Path(root)
        self.log_file = Path(log_file)
        self.graph_file = graph_file
        self._page = _PAGE
        self.frontend_build = _frontend_build_token(self._page)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, str]] = {}
        #: One lock per page key, so a slow map build for one project does not
        #: hold up another. Guarded by _lock while being created.
        self._builders: dict[str, threading.Lock] = {}

    def stamp(self) -> str:
        """A cheap value that changes when anything the pages are built from does.

        The log alone was not enough. The map is drawn from graphify-out/graph.json
        as well, so re-running `graphify extract` changed the picture completely
        while the stamp sat still, and the board went on serving the map it had
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

    def release(self, claim_id: str, reason: str, store: str = "",
                actor: str = "") -> tuple[int, dict]:
        """Free somebody else's claim, from the board, under the operator's name.

        THE BOARD IS OTHERWISE READ-ONLY AND THAT IS DELIBERATE: the log is
        written under a lock through a fold that enforces the rules, and a
        dashboard that wrote around either would be a second writer with none of
        those guarantees. So this does not write around them: it takes the same
        per-repo lock, re-reads and re-folds inside it, and appends the same
        release event the CLI's `release --force` appends. The only thing that is
        new here is the button.

        Two refusals it keeps from the CLI, for the same reasons:

        * An exact claim id, never a path. A path can match more ground than the
          person meant, and this verb exists precisely for making a judgement
          about somebody else's work: that judgement should be about one named
          thing.
        * A reason, always. It goes in the log under the operator's name,
          permanently, and "who freed this and why" is the only question anybody
          asks afterwards.

        WHO THE AUTHOR IS. The request says, and the process's COMMS_ACTOR is
        only the fallback. The author used to come from the process alone, and
        a board started without COMMS_ACTOR (a launcher, an app, a plain
        `comms-graph ui`) could not free anything: every click on "Free all"
        came back 403, and the ground stayed held by an agent that was gone.
        The person at the keyboard is the one making the judgement, so the page
        asks them for their name once and sends it with every release; that is
        the same self-declared identity `--as` is on the CLI, and it is recorded
        as the arbitrator, the way `release --force` records it. What this never
        does is sign as the holder: a release under the holder's own name would
        say they finished, which is the one thing the log must not say.

        It still refuses when neither the request nor the process names anyone.
        A release with no author is worse than no release: the claim is gone and
        the log cannot say who did it.
        """
        actor = _actor_name(actor) or _actor_name(os.environ.get("COMMS_ACTOR", ""))
        if not actor:
            return 403, {"error": "this board has no actor, so a release would have "
                                  "no author. The page asks for your name when you "
                                  "free something; from elsewhere, send \"actor\" "
                                  "or start the board with COMMS_ACTOR set."}
        claim_id = (claim_id or "").strip()
        if not claim_id:
            return 400, {"error": "release needs the exact claim id"}
        reason = (reason or "").strip()
        if not reason:
            return 400, {"error": "release needs a reason; it is recorded under "
                                  "your name permanently"}

        # Same store, same lock file the CLI takes: store dir + .lock.
        # THE PROJECT ON SCREEN, not the one this process was started in. The
        # button sends a claim id read out of the selected project's log; looking
        # it up in a different log finds nothing, and the board answered "no
        # active claim with id 01M1177A..." for a claim that plainly existed two
        # panels away. Same plumbing gap as the snapshot had, on the one path
        # that writes.
        _key, root, log_file = self._for_store(store)
        lock_file = _log.store_dir(root) / ".lock"
        with _lock.file_lock(lock_file):
            st = _state.fold(_log.read(log_file))
            held = st.claim_by_id(claim_id)
            if held is None:
                return 404, {"error": f"no active claim with id {claim_id}"}
            if held.actor == actor:
                return 400, {"error": "that is your own claim; release it from the CLI"}
            # The same two fields `release --force` writes, so the fold shows
            # "somebody took it off them" rather than "they finished". The board
            # wrote neither, and its releases folded as ordinary ones.
            ev = _log.Event(
                ts=datetime.now(timezone.utc), id=_log.new_id(), actor=actor,
                type=_log.TYPE_RELEASE, scope=None,
                data={"refs": [held.id], "result": reason,
                      "original_actor": held.actor, "arbitrator": actor,
                      "freed_from": held.actor, "via": "board"},
            )
            _log.append(log_file, ev)
        return 200, {"ok": True, "released": held.id, "was": held.actor,
                     "scope": str(held.scope)}

    def _graph_path(self, root: Path | None = None) -> Path:
        # Follows the project being SHOWN. An explicit --graph still wins, but
        # otherwise a code map is a property of the repo on screen, not of
        # wherever this process happens to have been started.
        if self.graph_file:
            return Path(self.graph_file)
        return (root or self.root) / "graphify-out" / "graph.json"

    def snapshot(self, store: str = "") -> dict:
        """The board for one project. Its own by default, any of them by key.

        THE PROJECTS RAIL WAS A LIST OF LINKS THAT DID NOTHING. Every entry set
        `?store=<key>` in the URL, the server ignored it, and the page came back
        built from the process's own repo every time. Run as a login service
        from `/`, that repo is empty, so the rail listed five real projects and
        clicking any of them showed zero events, zero claims and an empty roster
        beside a sidebar saying that project was active 29 seconds ago.

        Two things said different things on one screen, and the wrong one was
        the one with the numbers on it.
        """
        root, log_file = self.root, self.log_file
        if store and store != _log.repo_hash(self.root):
            picked = self._store_by_key(store)
            if picked is not None:
                root, log_file = picked
        snap = _snapshot(root, log_file, graph_path=self._graph_path(root))
        # Whether this process can sign a release itself. When it cannot, the
        # page asks the person for their name before freeing anything, so the
        # request does not go out only to come back 403.
        snap["board_actor"] = _actor_name(os.environ.get("COMMS_ACTOR", ""))
        snap["frontend_build"] = self.frontend_build
        snap["version"] = __version__
        return snap

    def _store_by_key(self, key: str):
        """(root, log file) for a store key, or None if it is not one of ours.

        Resolved from the SAME enumeration the rail is drawn from, so a key the
        page can offer is a key this can open, and a key it cannot is refused
        rather than guessed at.
        """
        for info in _log.known_stores():
            if info.get("key") != key:
                continue
            root = Path(info.get("root") or "")
            # The store directory is named by the key, so the log is derivable
            # without trusting the root: a project whose directory has been
            # moved still has readable history.
            log_file = Path(info["dir"]) / _log.LOG_FILENAME
            return (root, log_file) if log_file.exists() else None
        return None

    def history(self, store: str = "", *, actor: str = "", before: str = "") -> dict:
        if store and store != _log.repo_hash(self.root) and self._store_by_key(store) is None:
            raise ValueError("The selected project is unavailable.")
        _key, _root, log_file = self._for_store(store)
        return history_page(_log.read(log_file), actor=actor, before=before)

    def page(self) -> str:
        # Returned verbatim. _PAGE is NOT a format string: it is full of CSS and
        # JavaScript braces, and an earlier version doubled them for a .format()
        # call that never happened, so the doubled braces shipped literally and
        # every rule and script block in the page was invalid.
        return self._page.replace(_FRONTEND_BUILD_MARKER, self.frontend_build)

    def _cached(self, key: str, build) -> str:
        """One builder at a time per page, because building is not side-effect free.

        Each build RENDERS TO A FILE and reads it back. Two requests missing the
        cache together: a browser fetching both frames, or a reload landing on
        top of a push: had both builders writing the same path at once, and one
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

    def _render_dir(self, key: str) -> Path:
        """Where a generated page is written. NOT into the repository.

        It used to write `<root>/graphify-out/…`, which fails outright when the
        board is a login service: its root is `/`, and `/graphify-out` is a
        read-only filesystem. Both graphs showed an Errno 30 instead of a
        picture. Writing into somebody's repository was wrong anyway; these are
        derived files, and the store directory already holds the derived data
        for exactly this project and is already writable.
        """
        found = next((Path(i["dir"]) for i in _log.known_stores() if key and i["key"] == key), None)
        d = (found or (_log.user_data_home() / "comms" / key if key else _log.store_dir(self.root))) / "render"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _for_store(self, store: str):
        """(key, root, log file) for the project a page should draw."""
        key = store or _log.repo_hash(self.root)
        if store and store != _log.repo_hash(self.root):
            picked = self._store_by_key(store)
            if picked is not None:
                return key, picked[0], picked[1]
        return _log.repo_hash(self.root), self.root, self.log_file

    def map_html(self, store: str = "") -> str:
        def build() -> str:
            from . import view as _view

            key, root, log_file = self._for_store(store)
            out = self._render_dir(key) / "graph.comms.live.html"
            try:
                res = _view.render(root, out, graph_file=self.graph_file,
                                   log_file=log_file)
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
                    ": claims still record and still block without it.",
                    str(exc))
        return self._cached("map:" + (store or ""), build)

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
    httpd = _DashboardHTTPServer((host, port), _Handler)
    httpd.board = Board(root, log_file, graph_file)  # type: ignore[attr-defined]
    return httpd
