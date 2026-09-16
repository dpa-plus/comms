"""A local dashboard: both graphs, the live claims, and who is here.

WHY A SERVER AND NOT JUST FILES. ``view.py`` and ``taskview.py`` each write a
complete standalone page, which is the right shape for sending somebody a
snapshot. What they cannot do is change when the log changes, and the whole
value of a coordination board is that you leave it open while agents work. This
adds the one thing files cannot have: it notices a write and pushes.

WHY THE TWO GRAPHS ARE IFRAMES. Each is a finished page with its own vis-network
instance, its own layout, and its own interaction state. Inlining them into one
document would mean two vis instances fighting over one global, and: worse:
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

from . import lock as _lock
from . import log as _log
from . import state as _state
from . import task as _task
from . import guard as _guard
from . import tree as _tree
from . import taskcode as _taskcode
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
    # hello in this repo": a board reporting an empty room to somebody watching
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

    # WHICH FILES A TASK TOUCHES. Nothing carried this, and it is the first
    # question anybody opening a task asks. It is not stored on the task: it is
    # derived from claims tagged `--task <id>`, which is the point of that flag:
    # the task tracks itself from work the agent was doing anyway.
    #
    # Built from the LOG, not from live claims alone, so a file stays on the task
    # after it is released. A task whose files vanish the moment the work is
    # finished answers "what did this touch" with "nothing", which is exactly
    # backwards.
    held_now = {(c.actor, str(c.scope)) for c in st.claims.values()}
    task_files: dict[str, dict[str, dict]] = {}
    for ev in events:
        if ev.type != _log.TYPE_CLAIM or not ev.scope:
            continue
        tag = _string(ev.data, "task")
        if not tag:
            continue
        scope = ev.scope[0]
        row = task_files.setdefault(tag, {}).setdefault(
            scope, {"scope": scope, "actor": ev.actor, "held": False, "intent": ""})
        row["actor"] = ev.actor
        row["intent"] = _string(ev.data, "intent") or row["intent"]
        row["held"] = (ev.actor, scope) in held_now

    latest_task_activity = {}
    for ev in events:
        task_id = _string(ev.data, "task")
        if task_id:
            latest_task_activity[task_id] = ev.ts.isoformat().replace("+00:00", "Z")
    tasks = []
    for t in sorted(st.tasks.values(), key=lambda t: t.id):
        files = sorted(task_files.get(t.id, {}).values(), key=lambda r: r["scope"])
        tasks.append({
            "files": files,
            "files_held": sum(1 for f in files if f["held"]),
            "id": t.id, "title": t.title, "phase": t.phase,
            "last_activity": latest_task_activity.get(t.id, ""),
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

    # THE JOIN. The log knows which files a task touched; the map knows how files
    # reach each other. Neither knew the other, so the task graph could only show
    # dependencies somebody typed by hand, and nobody ever typed one: eight
    # tasks, zero declared edges, in a log with thousands of events. Derived, it
    # is true whether or not anybody noticed.
    #
    # Failure here must never take the board down. The map is optional, may be
    # stale, may be absent, and is somebody else's file format; a board that
    # disappears because a graph would not load is worse than one that says it
    # has no map.
    #
    # `alerts` is created HERE, above everything that can raise one. It was
    # created a hundred lines further down, next to the dependency-loop check,
    # and the two alerts raised before that point (a map that would not load,
    # a map older than a day) each hit an unbound name. The second is the
    # ordinary state of any repo whose map is a week old, so on such a repo
    # every snapshot raised, /api/status answered nothing, and the board was
    # blank: exactly the failure the "never raises" promise above exists to
    # prevent. The tests never saw it because their map is fresh or absent.
    alerts: list[dict] = []
    code_map_available = False
    try:
        from .cli import _load_graph  # the one loader, undirected view and all
        graph = _load_graph(graph_path) if graph_path else None
        code_map_available = graph is not None
        links = _taskcode.link(
            graph, {t["id"]: t for t in tasks},
            {t["id"]: [f["scope"] for f in t.get("files") or []] for t in tasks},
            root,
            {t["id"]: {f["actor"] for f in t.get("files") or []} for t in tasks},
            # The board's viewer is the operator, not one of the agents, so
            # nothing is "your own" here and the label correctly never appears.
            os.environ.get("COMMS_ACTOR", "").strip(),
        ) if graph is not None else {}
    except Exception as exc:  # pragma: no cover - defensive by intent
        links = {}
        alerts.append({
            "kind": "map",
            "text": f"the code map could not be read, so tasks are not linked to it: {exc}",
            "hint": "Rebuild it with `graphify extract . --code-only`.",
        })
    for t in tasks:
        info = links.get(t["id"]) or {}
        t["touches"] = info.get("touches", 0)
        t["related"] = info.get("related", [])

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
            "text": f"the code map is {map_age // 86400} day(s) old, so what tasks "
                    f"'meet in the code' may be out of date",
            "hint": "Rebuild it with `graphify extract . --code-only`.",
        })

    # One serializer for the live preview and paged history; released holds
    # remain attached to their original task and owner.
    feed = history_page(events)["events"]

    # Things that are WRONG with the plan itself, as opposed to slow. Both are
    # computed by the task-graph page and both live inside its side panel,
    # which the board hides to give the drawing room, so a dependency loop, the
    # one state a plan cannot recover from on its own, was invisible on the
    # surface somebody actually watches.
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
        "tasks": tasks,
        # The main dashboard draws the recorded plan itself.  ``blocked_by``
        # is only today's derived state and drops a predecessor after it is
        # verified; it is not a substitute for the edge somebody declared.
        "task_edges": [
            {"from": edge.from_, "to": edge.to, "kind": edge.kind,
             "provides": edge.provides}
            for edge in (st.task_edges or [])
        ],
        # An empty relation set means something only when a map was loaded.
        # Keep that distinct from the ordinary first-run state with no map.
        "code_map_available": code_map_available,
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
        # line, and nothing caught it, because there was no way to write a
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
        # rows saying only "@alice was refused": the two facts worth having,
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
/* overflow-y is not decoration. min-height:0 alone lets a flex child be
   SHORTER than its content, and with the default overflow:visible the surplus
   is painted outside the card rather than clipped: a 24-name roster spilled
   246px past its own border and rendered on top of the two panels beneath it.
   The card must scroll its own body or it is not a box. */
.card-bd { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
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
   Every value here is one of its tokens: no new colours, no new spacing
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
  font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-4); }
.nowband-hd .count { color: var(--ink-3); text-transform: none; letter-spacing: 0; font-size: 11.5px; }
.nowband-empty { padding: var(--sp-h) var(--sp-2) var(--sp-2); color: var(--ink-4); font-size: 12.5px; }
/* The one empty state that is not a shrug: no claims WITH a dirty tree is a
   thing to look at, not an absence to move past. */
.nowband-empty.amber { color: var(--amber); }
.loosehd { border-top: 1px solid var(--line); }
.afiles.loose { padding-bottom: var(--sp-h); }
.afiles.loose .afrow { justify-content: flex-start; gap: var(--sp-2); }
.afiles.loose .how { color: var(--ink-4); font-size: 11.5px; }
.afiles.loose .how.amber { color: var(--amber); }
.afiles.loose .who { color: var(--ink-4); font-size: 11px; margin-left: auto; }
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
/* A count that opens the detail behind it. Styled as text, because it is a
   number first and an affordance second. */
.acount.filestoggle, .count.loosetoggle {
  background: none; border: 0; padding: 0; font: inherit; cursor: pointer;
  color: var(--ink-4); border-bottom: 1px dotted var(--line-2); }
.acount.filestoggle:hover, .count.loosetoggle:hover { color: var(--ink); }
.why { color: var(--ink); }
.alsopaths { display: none; }
.alsopaths { color: var(--ink-4); font-size: 11px; margin: 1px 0 0;
  overflow-wrap: anywhere; }
.hrow { display: flex; align-items: baseline; gap: var(--sp-1);
  padding: 3px var(--sp-2); font-size: 12.5px; }
.hrow:last-child { padding-bottom: var(--sp-1); }
.hrow.quiet { background: var(--amber-wash); }
.hactor { color: var(--accent); flex: none; }
.hpath { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hintent { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.htask { color: var(--ink-4); flex: none; }
.hid { color: var(--ink-4); font-size: 10.5px; flex: none; opacity: .75; }
/* Grouped by agent: one row per agent, its files quiet underneath. */
.arow { display: flex; align-items: baseline; gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2) 3px; font-size: 12.5px; }
.arow.quiet { background: var(--amber-wash); }
.acount { color: var(--ink-3); flex: none; }
.afiles { padding: 0 0 var(--sp-1) 0; }
.afrow { display: flex; align-items: baseline; gap: var(--sp-1);
  padding: 1px var(--sp-2) 1px calc(var(--sp-2) * 2); font-size: 11.5px; }
.afpath { color: var(--ink-4); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; flex: 1 1 auto; }
.afrel { flex: none; font-size: 10px; padding: 0 5px; color: var(--ink-4);
  border-color: transparent; opacity: 0; transition: opacity .12s; }
.afrow:hover .afrel { opacity: 1; }
.afrel:hover { color: var(--red); border-color: var(--red-line); }
.arow .rel { flex: none; font-size: 10.5px; padding: 0 7px; color: var(--ink-3);
  border-color: var(--line-2); }
.arow .rel:hover { color: var(--red); border-color: var(--red-line); background: var(--red-wash); }
/* Visible without hovering. Hover-to-reveal was the first version and it fails
   the actual complaint: "I don't see where I can release their claims": since
   an affordance you have to find by accident is one you do not know exists.
   Understated instead of hidden: quiet until you look at it, red when you mean
   it. */
.hrow .rel { flex: none; font-size: 10.5px; padding: 0 6px; color: var(--ink-4);
  border-color: var(--line-hair); transition: color .12s, border-color .12s; }
.hrow:hover .rel { color: var(--ink-3); border-color: var(--line-2); }
.hrow .rel:hover { color: var(--red); border-color: var(--red-line); background: var(--red-wash); }

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
.tcell { flex: 1 1 0; min-width: 0; border-top: 2px solid var(--line); padding-top: 5px; }
.tcell.c-doing   { border-top-color: var(--amber); }
.tcell.c-review  { border-top-color: var(--accent); }
.tcell.c-blocked { border-top-color: var(--red); }
.tcell.c-ready   { border-top-color: var(--green); }
.tcell.c-closed  { border-top-color: var(--line-2); }
/* A count of zero is not news, and at full contrast it competed with the ones
   that are. */
.tcell.zero { border-top-color: var(--line-hair); }
.tcell.zero .tn { color: var(--ink-4); }
.tn { font-size: 15px; color: var(--ink); }
.tl { font-size: 9.5px; letter-spacing: .06em; color: var(--ink-4); }
.stuck { border-top: 1px solid var(--line-hair); padding: var(--sp-1) var(--sp-2); }

/* the task list, and one task in full */
.tlist { border-top: 1px solid var(--line-hair); }
/* The title gets the row. The phase is a 3px bar down the left edge, because a
   chip spelling out "CLOSED" cost about a third of the width and pushed the one
   thing anybody reads into an ellipsis at twenty characters. */
.trow { padding: 7px var(--sp-2) 7px calc(var(--sp-2) + 5px); position: relative;
  border-bottom: 1px solid var(--line-hair); cursor: pointer; }
.trow::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0;
  width: 3px; background: var(--line-2); }
.trow:last-child { border-bottom: 0; }
.trow:hover { background: var(--surface-2); }
.trow.p-doing::before   { background: var(--amber); }
.trow.p-review::before  { background: var(--accent); }
.trow.p-blocked::before,
.trow.p-cycle::before   { background: var(--red); }
.trow.p-ready::before   { background: var(--green); }
.trow.p-closed::before  { background: var(--line); }
.trow.p-closed .ttitle  { color: var(--ink-3); }
/* Two lines, then clamp. Truncating mid-word at one line made every task on a
   real board indistinguishable from every other. */
.ttitle { color: var(--ink); line-height: 1.35; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.tmeta { display: flex; gap: var(--sp-1); align-items: baseline; margin-top: 2px; }
.tfiles { color: var(--ink-4); font-size: 11px; }
/* Closed work folds away: it is history, and on a real project it was 23 rows
   burying the 3 that could still be acted on. */
.tfold { display: flex; justify-content: space-between; align-items: baseline;
  padding: 7px var(--sp-2); cursor: pointer; color: var(--ink-4);
  font-size: 11.5px; border-bottom: 1px solid var(--line-hair); }
.tfold:hover { background: var(--surface-2); }
.tfoldc { color: var(--accent); }
.swhy { color: var(--amber); font-size: 11px; }
/* Still used by the task DETAIL header, where there is one task and room to
   spell the phase out. It was only ever wrong in a list. */
.tphase { flex: none; font-size: 9.5px; letter-spacing: .05em; text-transform: uppercase;
  padding: 1px 6px; border-radius: 3px; border: 1px solid var(--line);
  color: var(--ink-4); }
.tphase.p-doing { color: var(--amber); border-color: var(--amber-line); background: var(--amber-wash); }
.tphase.p-review { color: var(--accent); border-color: var(--accent-line); background: var(--accent-wash); }
.tphase.p-blocked, .tphase.p-cycle { color: var(--red); border-color: var(--red-line); background: var(--red-wash); }
.tphase.p-ready { color: var(--green); border-color: var(--green); background: transparent; }
.allgood { padding: var(--sp-1) var(--sp-2) var(--sp-2); color: var(--green); font-size: 12.5px; }

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
#tasks { padding: 4px 20px 24px; overflow-y: auto; }
.work-group h2 { font-size: 13px; font-weight: 600; color: var(--ink-3); margin: 22px 0 9px; }
.work-group h2 span { margin-left: 7px; color: var(--ink-4); }
.trow { display: block; width: 100%; height: auto; text-align: left;
  padding: 15px 16px; border: 1px solid var(--line); border-radius: 8px; margin: 8px 0; }
.trow:last-child { border-bottom: 1px solid var(--line); }
.trow::before { width: 3px; top: 14px; bottom: 14px; border-radius: 3px; }
.ttitle { display: block; overflow: visible; font-size: 15px; line-height: 1.45; font-weight: 550; }
.tmeta { flex-wrap: wrap; gap: 5px 12px; margin-top: 8px; font-size: 12px; font-weight: 400; color: var(--ink-3); }
.tmeta .owner { color: var(--ink-2); }
.tmeta .tfiles { font: inherit; }
.work-note { margin: 8px 0 0; color: var(--ink-3); font-size: 12px; font-weight: 400; }

/* The work graph is deliberately static.  Positions come from a stable order
   in the page state; there is no physics pass to make the picture jump on each
   live update.  Only this viewport scrolls, so a wide scene cannot make the
   mobile page itself overflow. */
#tasks { min-width: 0; }
.graph-shell { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
.graph-controls { display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
  position: sticky; top: 0; z-index: 8; padding: 12px 0 8px; background: var(--surface); }
.graph-search { flex: 1 1 210px; min-width: 120px; height: 34px; border: 1px solid var(--line);
  border-radius: 7px; padding: 0 10px; background: var(--bg); color: var(--ink); font: inherit; }
.graph-controls button { height: 32px; padding: 0 10px; }
.graph-zoom { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.graph-controls .pressed { color: var(--accent); border-color: var(--accent-line); background: var(--accent-wash); }
.graph-count { color: var(--ink-4); font-size: 11.5px; white-space: nowrap; }
.graph-key { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 15px;
  color: var(--ink-3); font-size: 11.5px; }
.graph-key span { display: inline-flex; align-items: center; gap: 6px; }
.graph-key i { width: 24px; height: 0; border-top: 2px solid var(--ink-3); }
.graph-key i.dep { position: relative; }
.graph-key i.dep::after { content: ""; position: absolute; right: -1px; top: -4px;
  border-left: 6px solid var(--ink-3); border-top: 3px solid transparent; border-bottom: 3px solid transparent; }
.graph-key i.rel { border-top-style: dashed; border-top-color: var(--accent); }
.graph-focus { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  min-height: 34px; padding: 8px 10px; border: 1px solid var(--line-hair);
  border-radius: 8px; background: var(--surface-2); color: var(--ink-3); font-size: 12px; }
.graph-focus strong { color: var(--ink); font-weight: 600; }
.graph-focus button { height: 28px; padding: 0 9px; }
.graph-viewport { width: 100%; min-width: 0; min-height: 360px; max-height: calc(100vh - 330px);
  overflow: auto; overscroll-behavior: contain; border: 1px solid var(--line-hair);
  border-radius: 9px; background: var(--bg); }
.graph-camera { position: relative; }
.graph-scene { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
.graph-scene > svg { position: absolute; inset: 0; overflow: visible; pointer-events: none; }
.graph-edge { fill: none; vector-effect: non-scaling-stroke; }
.graph-edge.dependency { stroke: var(--ink-3); stroke-width: 1.6; }
.graph-arrow { fill: var(--ink-3); }
.graph-edge.related { stroke: var(--accent); stroke-width: 1.5; stroke-dasharray: 5 5; opacity: .75; }
.graph-edge.dim { opacity: .2; }
.graph-node { position: absolute; width: 260px; min-height: 84px; display: grid;
  grid-template-columns: 40px minmax(0, 1fr); align-items: center; column-gap: 10px;
  padding: 7px 8px; text-align: left; border: 0; border-radius: 10px;
  color: var(--ink); background: transparent; }
.graph-node:hover .graph-label, .graph-node:focus-visible .graph-label { background: var(--surface-2); }
.graph-node:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.graph-node.selected .graph-orb { box-shadow: 0 0 0 4px var(--accent-wash), 0 0 0 5px var(--accent-line); }
.graph-node.connected:not(.selected) .graph-orb { box-shadow: 0 0 0 3px var(--surface-3); }
.graph-orb { width: 34px; height: 34px; border-radius: 50%; border: 2px solid var(--line-2);
  background: var(--surface); display: grid; place-items: center; color: var(--ink-3);
  font-size: 10px; font-weight: 700; text-transform: uppercase; }
.graph-node.p-doing .graph-orb { color: var(--amber); border-color: var(--amber); background: var(--amber-wash); }
.graph-node.p-review .graph-orb { color: var(--accent); border-color: var(--accent); background: var(--accent-wash); }
.graph-node.p-blocked .graph-orb, .graph-node.p-cycle .graph-orb { color: var(--red); border-color: var(--red); background: var(--red-wash); }
.graph-node.p-ready .graph-orb { color: var(--green); border-color: var(--green); }
.graph-node.p-closed .graph-orb { color: var(--green); border-color: var(--green); background: var(--green-wash); }
.graph-label { min-width: 0; padding: 5px 6px; border-radius: 6px; background: var(--bg); }
.graph-title { display: block; white-space: normal; overflow-wrap: anywhere;
  font-size: 14px; line-height: 1.3; font-weight: 600; }
.graph-meta { display: flex; flex-wrap: wrap; gap: 3px 8px; margin-top: 5px;
  color: var(--ink-4); font-size: 10.5px; line-height: 1.25; }
.graph-meta .status { color: var(--ink-3); font-weight: 600; }
.graph-list { display: flex; flex-direction: column; gap: 6px; }
.graph-list .graph-list-row { width: 100%; height: auto; display: flex; align-items: center;
  gap: 10px; padding: 10px 12px; text-align: left; border: 1px solid var(--line);
  border-radius: 8px; }
.graph-list .graph-list-row .graph-title { flex: 1; }
.graph-empty { padding: 36px 12px; color: var(--ink-4); line-height: 1.55; }
.rail-right { display: flex; flex-direction: column; overflow-y: auto; gap: 18px; }
.rail-right > .card { flex: none; }
.rail-right > .card:first-child { max-height: none; }
.nowband { border: 0; padding: 0 14px 14px; max-height: none; }
.nowband-hd { padding-top: 14px; }
.arow { display: flex; flex-wrap: wrap; gap: 6px 10px; padding: 15px 0; }
.arow .hactor { width: calc(100% - 68px); font: inherit; font-weight: 550; overflow-wrap: anywhere; }
.arow .hintent { width: 100%; flex: auto; white-space: normal; overflow: visible; line-height: 1.5; color: var(--ink-3); }
.arow .htask { display: none; }
.arow .grow { display: none; }
.arow .rage { margin-right: auto; }
.afrow { flex-wrap: wrap; }
.afpath { overflow-wrap: anywhere; white-space: normal; }
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
.tdetwrap { inset: 0; padding-left: max(20px, calc(100vw - 680px)); background: #0005; }
.tdet-title { font-size: 18px; }
.tdet-id { display: none; }
.tdetbox { width: 100%; max-width: none; }
#tdet { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.tdet-hd { display: flex; align-items: flex-start; gap: 10px; padding: 20px; border-bottom: 1px solid var(--line); }
.tdet-title { flex: 1; line-height: 1.4; }
.tdet-bd { padding: 20px; overflow-y: auto; }
.tdet-state { font-size: 15px; margin-bottom: 12px; }
.tdet-sec { margin: 24px 0 10px; font-size: 12px; color: var(--ink-3); font-weight: 600; }
.tdet-checks { display: flex; flex-wrap: wrap; gap: 8px; }
.chk { padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px; }
.chk.ok { color: var(--green); } .chk.bad { color: var(--red); }
.tdet-note { color: var(--ink-3); line-height: 1.6; overflow-wrap: anywhere; }
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
.technical-details { margin-top: 24px; border-top: 1px solid var(--line); padding-top: 16px; }
.technical-details summary { cursor: pointer; color: var(--ink-2); }
.tfrow { display: flex; flex-wrap: wrap; gap: 6px 12px; padding: 10px 0; border-bottom: 1px solid var(--line-hair); }
.tfpath { width: 100%; overflow-wrap: anywhere; font-size: 12px; }
.tfactor, .tfstate, .tvia { font-size: 12px; color: var(--ink-3); }
.tmeet { cursor: pointer; } .tmeet:hover { background: var(--surface-2); }
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
  #tasks { padding: 0 14px 20px; }
  .graph-controls { top: 0; }
  .graph-viewport { min-height: 420px; max-height: 72vh; }
  .topbar { padding: 0 15px; }
  .topbar .sep { display: none; }
  #clock { display: none; }
  .history-wrap { padding: 10px; }
  .tdetwrap { padding: 10px; }
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
        <h1 id="projectTitle">Your work</h1>
        <p>What is moving forward, and who is on it.</p>
        <div class="work-toolbar">
          <button id="historyBtn">History</button>
          <button id="dagBtn" class="ghost">Connections</button>
        </div>
      </div>
      <div id="alarms"></div>
      <div class="card-bd" id="tasks"><div class="empty">Loading work…</div></div>
    </div>
  </section>

  <aside class="rail-right">
    <div class="card">
      <div class="card-hd"><span>Team</span><span class="count" id="rosterCount"></span></div>
      <div id="nowBand"></div>
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
    <div class="history-controls"><div class="chips" id="chips"></div><button class="ghost" id="historyAll">All agents and tasks</button></div>
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
    <div id="releaseScope"></div><p class="tdet-note" id="releaseOwner"></p>
    <p class="tdet-note" id="releaseHelp">This does not stop the agent or delete its work. A quiet agent may still be running.</p>
    <label for="releaseActor">Your name</label><input id="releaseActor" required autocomplete="off">
    <label for="releaseReason">Reason</label><textarea id="releaseReason" required placeholder="Why is this hold no longer needed?"></textarea>
    <div id="releaseError" role="alert"></div>
    <div class="release-actions"><button type="button" class="ghost" id="releaseCancel">Cancel</button><button type="submit" id="releaseConfirm">Release hold</button></div>
  </form>
</section>

<div class="dagwrap tdetwrap" id="tdetWrap" hidden role="dialog" aria-modal="true" aria-label="Task details">
    <div class="dagbox tdetbox"><div id="tdet"></div></div>
  </div>
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
var PAGE_BUILD = "__COMMS_FRONTEND_BUILD_TOKEN__";
var D = null;              // the last snapshot
var FILTER = "all";        // which stream chip is active
var PQ = "";               // projects filter box
var PAUSED_AT_BOTTOM = true;
var RELOAD_REQUESTED = false;
var LAST_SNAPSHOT_AT = 0;
var LOAD_STARTED_AT = Date.now();
var HISTORY_SCOPE = null;
var OPEN_TASK = null;
var OPEN_FILES = {};
var HISTORY_EVENTS = null;
var HISTORY_BEFORE = "";
var HISTORY_REQUEST = 0;
var GRAPH_RELATED_LIMIT = 12;
var GRAPH_STATE = freshGraphState("");

function freshGraphState(projectKey) {
  return {projectKey: projectKey || "", showCompleted: false, query: "", view: "graph",
          zoom: 1, left: 0, top: 0, selected: null, relatedExpanded: false,
          order: [], rank: {}, sceneWidth: 0, sceneHeight: 0};
}

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

function renderNow() {
  var cs = D.claims || [];
  var h = '<div class="nowband">';
  h += '<div class="nowband-hd"><span>Holding work</span><span class="count">' +
       (cs.length ? cs.length + (cs.length === 1 ? " file claimed" : " files claimed") : "") + "</span></div>";
  var dirt = D.dirty || {};
  // NO REPOSITORY IS NOT A FAILED READ. Run across every project (the login
  // service does, from /), there is no working tree to have an opinion about,
  // and reporting "the working tree could not be read: not a git repository"
  // plus "commit guard: not known" is two lines of alarm about the absence of a
  // question. The principle is never to render "could not look" as "all clear";
  // it is not to invent a problem where there is no subject.
  var noRepoHere = dirt.readable === false &&
                   /not a git repository/i.test(dirt.unavailable || "");
  if (noRepoHere) { dirt = {}; }
  if (!cs.length) {
    // "Nobody is holding any ground" is only reassuring if the tree agrees, and
    // it was measured NOT agreeing: no claims on the board while fifteen files
    // were changed on disk. Say which of the two situations this is.
    if (dirt.readable === false) {
      h += '<div class="nowband-empty">Nobody is holding any ground right now, and ' +
           'the working tree could not be read (' + esc(dirt.unavailable || "no reason given") +
           '), so this is not the same as quiet.</div>';
    } else if (dirt.total) {
      h += '<div class="nowband-empty amber">Nobody is holding any ground, but ' +
           dirt.total + (dirt.total === 1 ? " file has" : " files have") +
           ' uncommitted changes. Nothing here is claimed, which is not the same as ' +
           'nothing happening.</div>';
    } else if (noRepoHere) {
      h += '<div class="nowband-empty">Nobody is holding any ground. This view spans ' +
           'every project, so there is no single working tree to check; open one on ' +
           'the left to see what is changed in it.</div>';
    } else {
      h += '<div class="nowband-empty">Nobody is holding any ground, and there are ' +
           'no uncommitted changes.</div>';
    }
  } else {
    // BY AGENT, not by file. One agent holding eight files produced eight
    // near-identical rows: same name, same truncated intent, same task, eight
    // opaque claim ids: and the question somebody actually arrives with is not
    // "which files" but "is that agent still alive, and if not, free its
    // ground". A list of files cannot be acted on. A list of agents can.
    var byActor = {};
    cs.forEach(function (c) { (byActor[c.actor] = byActor[c.actor] || []).push(c); });
    Object.keys(byActor).sort(function (a, b) {
      // Whoever has gone quiet first: that is the one that needs a person.
      var qa = byActor[a].some(function (c) { return c.quiet; }) ? 0 : 1;
      var qb = byActor[b].some(function (c) { return c.quiet; }) ? 0 : 1;
      return qa - qb || byActor[b].length - byActor[a].length;
    }).forEach(function (actor) {
      var held = byActor[actor];
      var quiet = held.some(function (c) { return c.quiet; });
      var idle = Math.max.apply(null, held.map(function (c) { return c.idle_seconds || 0; }));
      // Since WHEN, which the row never said. "active" answers whether they are
      // alive; the question in front of a board is how long this has been going
      // on, and a claim taken four hours ago reads very differently from one
      // taken four minutes ago even when both are active.
      var oldest = Math.min.apply(null, held.map(function (c) {
        var t = Date.parse(c.ts || "");
        return isFinite(t) ? t : Date.now();
      }));
      var heldFor = Math.max(0, Math.round((Date.now() - oldest) / 1000));
      var intents = [], tasks = [];
      held.forEach(function (c) {
        if (c.intent && intents.indexOf(c.intent) === -1) { intents.push(c.intent); }
        if (c.task && tasks.indexOf(c.task) === -1) { tasks.push(c.task); }
      });
      h += '<div class="arow' + (quiet ? " quiet" : "") + '">';
      h += '<button class="hactor ghost agent-history" data-history-actor="' + esc(actor) + '">@' + esc(actor) + "</button>";
      // The count is the magnitude, which is what a person needs; the paths are
      // code detail and are one click away rather than always on screen.
      h += '<button class="acount mono filestoggle" data-actor="' + esc(actor) + '">' +
           held.length + (held.length === 1 ? " file" : " files") + "</button>";
      if (tasks.length) { h += '<span class="htask mono">' + esc(tasks.join(", ")) + "</span>"; }
      var taskTitles = tasks.map(function (id) {
        var task = (D.tasks || []).filter(function (t) { return t.id === id; })[0];
        return task ? task.title : id;
      });
      h += '<span class="hintent">' + esc((taskTitles.length ? taskTitles : intents).join(" · ")) + "</span>";
      h += '<span class="grow"></span>';
      h += '<span class="rage mono' + (quiet ? " amber" : "") + '">' +
           (quiet ? "No report for " + ago(idle) : "Reported " + ago(idle) + " ago") + "</span>";
      h += '<button class="ghost filestoggle" data-actor="' + esc(actor) + '">Review holds</button>';
      h += "</div>";
      // The files, quiet underneath: available without being in the way. The
      // claim id moves to the row's tooltip: it is a handle for the CLI, not
      // something a person reads.
      h += '<div class="afiles"' + (OPEN_FILES[actor] ? "" : " hidden") + '>';
      held.forEach(function (c) {
        h += '<div class="afrow" title="claim ' + esc(c.id) + '">' +
             '<span class="mono afpath">' + esc(shortPath(c.scope)) + "</span>" +
             '<button class="ghost rel afrel" data-id="' + esc(c.id) + '" data-scope="' +
             esc(c.scope) + '" data-actor="' + esc(actor) + '">Release hold</button></div>';
      });
      h += "</div>";
    });
  }
  // Changed on disk with nothing in the log about it. This is the half of the
  // picture no agent has to remember to emit, so it is the half that survives a
  // context compaction, a run of shell heredoc edits, or an agent that simply
  // never claimed anything.
  // Everything dirty that NO LIVE CLAIM covers. Filtering to the wholly
  // unattributed would have hidden the other half of the problem: a file
  // somebody released and left uncommitted appears under neither "working now"
  // nor "claimed by nobody", so it fell out of the board entirely.
  var loose = (dirt.files || []).filter(function (f) { return f.basis !== "held"; });
  if (loose.length) {
    // EDITS TO TRACKED FILES ARE THE SIGNAL. Untracked paths are usually build
    // output (graphify-out/, output/, a tool's cache directory) and git reports
    // a whole untracked directory as one entry, so they are not edits anybody
    // made to code. Six of them buried the single row that mattered: a
    // translation file changed, and released by the agent that had it.
    var edited = loose.filter(function (f) { return f.how.indexOf("new, not in git") !== 0; });
    var fresh  = loose.filter(function (f) { return f.how.indexOf("new, not in git") === 0; });

    h += '<div class="nowband-hd loosehd"><span>Changed on disk, nobody holding it</span>' +
         '<button class="count loosetoggle">' + loose.length + " of " + dirt.total +
         "</button></div>";
    h += '<div class="afiles loose" hidden>';
    edited.slice(0, 25).forEach(function (f) {
      h += '<div class="afrow">' +
           '<span class="mono afpath">' + esc(shortPath(f.path)) + "</span>" +
           '<span class="how' + (f.deleted ? " amber" : "") + '">' + esc(f.how) + "</span>" +
           // Only when the log HAS something to say. Repeating "claimed by
           // nobody" on every row restated the heading six times and left no
           // room for the one row that named an actor.
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
    // describe() already carries the command, so appending it again printed it
    // twice on one line.
    h += '<div class="guardwarn">' + esc(g.text || "") + "</div>";
  }
  h += "</div>";
  el("nowBand").innerHTML = h;
  bindHistoryButtons(el("nowBand"));
  Array.prototype.forEach.call(el("nowBand").querySelectorAll(".rel"), function (b) {
    b.onclick = function () { releaseClaim(b); };
  });
  var lt = el("nowBand").querySelector(".loosetoggle");
  if (lt) {
    lt.onclick = function () {
      var box = el("nowBand").querySelector(".afiles.loose");
      if (box) { box.hidden = !box.hidden; }
    };
  }
  Array.prototype.forEach.call(el("nowBand").querySelectorAll(".filestoggle"), function (b) {
    b.onclick = function (ev) {
      ev.stopPropagation();
      var box = b.parentNode.nextSibling;
      if (box && box.classList && box.classList.contains("afiles")) {
        box.hidden = !box.hidden;
        OPEN_FILES[b.getAttribute("data-actor")] = !box.hidden;
      }
    };
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
  ["all", "All"], ["claim", "Claims"], ["finding", "Findings"],
  ["note", "Notes"], ["task", "Tasks"], ["session", "Session"]
];
function matchKind(e) {
  if (HISTORY_SCOPE && HISTORY_SCOPE.kind === "actor" && e.actor !== HISTORY_SCOPE.value && e.original_actor !== HISTORY_SCOPE.value && (e.original_actors || []).indexOf(HISTORY_SCOPE.value) < 0) return false;
  if (HISTORY_SCOPE && HISTORY_SCOPE.kind === "task" && e.task !== HISTORY_SCOPE.value && (e.tasks || []).indexOf(HISTORY_SCOPE.value) < 0) return false;
  if (FILTER === "all") return true;
  if (FILTER === "claim") return e.type === "claim" || e.type === "release";
  if (FILTER === "task") return e.type === "task" || e.type === "task_edge" || e.type === "task_state";
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
// A finding's CATEGORY, not a severity: this build records what kind of thing
// it is, and colouring `decision` red because it sorts near `bug` would be a
// lie dressed as a signal.
function catClass(c) { return c === "bug" ? "high" : c === "gotcha" ? "med" : "low"; }

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
    var why = e.intent || e.result || "";
    if (why) {
      s += '<span class="why">' + esc(why) + "</span>";
    } else {
      s += '<span class="path mono">' + esc(shortPath(many ? many[0] : e.scope)) + "</span>";
    }
    s += "</div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + "</span>";
    if (e.task) { s += '<span class="dot"></span><span class="proj mono">' + esc(e.task) + "</span>"; }
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

  } else if (e.type === "task_edge") {
    s += '<div class="l1"><span class="verb">linked tasks</span></div>';
    s += '<div class="quote">' + esc(e.from) + " → " + esc(e.to) + "</div>";
    s += '<div class="l2"><span class="actor">@' + esc(e.actor) + '</span><span>' + esc(e.kind) + "</span></div>";
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
  var shown = (rosterAll ? here.concat(rest) : here).filter(function (a) { return !a.holding; });

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

/* ---------- work -------------------------------------------------------- */

function readableTaskTitle(t) {
  var title = String(t && t.title || "").trim();
  if (!title) return "Untitled task";
  // Only remove the conventional tracker prefix when a plain-English title
  // remains.  IDs and paths are never promoted into invented summaries.
  return title.replace(/^[A-Z]{2,8}[0-9]{1,5}[ ]*[-:–—][ ]+/, "");
}

function taskStatus(task) {
  var phase = task && typeof task === "object" ? task.phase : task;
  if (phase === "closed" && task && typeof task === "object") {
    return task.verified_by ? "Completed · checked" : "Finished · verification not recorded";
  }
  return ({doing: "In progress", review: "Checking", blocked: "Waiting",
           cycle: "Needs attention", ready: "Up next", closed: "Completed"})[phase] || "Recorded";
}

function taskOwners(t) {
  return (t.doers || []).length ? t.doers : t.did ? [t.did] : [];
}

function graphTask(id) {
  return (D.tasks || []).filter(function (t) { return t.id === id; })[0] || null;
}

function graphDeclaredEdges() {
  var known = {};
  (D.tasks || []).forEach(function (t) { known[t.id] = true; });
  var seen = {};
  return (D.task_edges || []).filter(function (edge) {
    if (!edge || !known[edge.from] || !known[edge.to] || edge.from === edge.to) return false;
    var key = edge.from + "\u0000" + edge.to;
    if (seen[key]) return false;
    seen[key] = true;
    return true;
  });
}

function graphRelated(id) {
  var found = {}, known = {};
  (D.tasks || []).forEach(function (t) { known[t.id] = true; });
  function add(other, relation) {
    if (!other || other === id || !known[other]) return;
    var prior = found[other];
    if (!prior || Number(relation.shared || 0) > Number(prior.shared || 0)) {
      found[other] = {task: other, shared: Number(relation.shared || 0), via: relation.via || []};
    }
  }
  var own = graphTask(id);
  (own && own.related || []).forEach(function (r) { add(r.task, r); });
  // Older snapshots were not guaranteed to carry the symmetric half.  Reading
  // the reverse record is still an undirected relation, never an inferred edge.
  (D.tasks || []).forEach(function (t) {
    (t.related || []).forEach(function (r) { if (r.task === id) add(t.id, r); });
  });
  return Object.keys(found).map(function (key) { return found[key]; }).sort(function (a, b) {
    return b.shared - a.shared || readableTaskTitle(graphTask(a.task)).localeCompare(readableTaskTitle(graphTask(b.task)));
  });
}

function graphOrderTasks(tasks) {
  var priority = {doing: 0, review: 1, blocked: 2, cycle: 3, ready: 4, closed: 5};
  return tasks.slice().sort(function (a, b) {
    var phase = (priority[a.phase] === undefined ? 9 : priority[a.phase]) -
                (priority[b.phase] === undefined ? 9 : priority[b.phase]);
    return phase || readableTaskTitle(a).localeCompare(readableTaskTitle(b)) || a.id.localeCompare(b.id);
  });
}

function ensureGraphOrder(tasks) {
  var known = {}, adjacency = {};
  tasks.forEach(function (t) { known[t.id] = t; adjacency[t.id] = {}; });
  graphDeclaredEdges().forEach(function (e) { adjacency[e.from][e.to] = true; adjacency[e.to][e.from] = true; });
  tasks.forEach(function (t) {
    (t.related || []).forEach(function (r) {
      if (known[r.task] && r.task !== t.id) { adjacency[t.id][r.task] = true; adjacency[r.task][t.id] = true; }
    });
  });
  GRAPH_STATE.order = GRAPH_STATE.order.filter(function (id) { return known[id]; });
  var already = {};
  GRAPH_STATE.order.forEach(function (id) { already[id] = true; });
  var additions = tasks.filter(function (t) { return !already[t.id]; });
  if (!GRAPH_STATE.order.length) {
    // Open work gets a compact area of its own; within it, breadth-first
    // component order keeps declared and advisory neighbours near each other.
    var ordered = [];
    [false, true].forEach(function (closed) {
      var pool = graphOrderTasks(additions.filter(function (t) { return (t.phase === "closed") === closed; }));
      var allowed = {}, visited = {};
      pool.forEach(function (t) { allowed[t.id] = true; });
      pool.forEach(function (seed) {
        if (visited[seed.id]) return;
        var queue = [seed.id]; visited[seed.id] = true;
        while (queue.length) {
          var id = queue.shift(); ordered.push(id);
          Object.keys(adjacency[id] || {}).filter(function (other) { return allowed[other] && !visited[other]; })
            .sort(function (a, b) { return readableTaskTitle(known[a]).localeCompare(readableTaskTitle(known[b])); })
            .forEach(function (other) { visited[other] = true; queue.push(other); });
        }
      });
    });
    GRAPH_STATE.order = ordered;
  } else {
    graphOrderTasks(additions).forEach(function (t) { GRAPH_STATE.order.push(t.id); });
  }
  GRAPH_STATE.rank = {};
  GRAPH_STATE.order.forEach(function (id, index) { GRAPH_STATE.rank[id] = index; });
}

function rememberGraphCamera() {
  var viewport = document.getElementById("graphViewport");
  if (!viewport) return;
  GRAPH_STATE.left = Number(viewport.scrollLeft || 0);
  GRAPH_STATE.top = Number(viewport.scrollTop || 0);
}

function graphFocusSnapshot() {
  var active = document.activeElement;
  if (!active) return null;
  var task = active.getAttribute ? active.getAttribute("data-graph-task") : "";
  if (task) return {task: task};
  var ids = ["graphSearch", "graphCompleted", "graphView", "listView",
             "graphZoomOut", "graphFit", "graphZoomIn", "graphDetails",
             "graphShowCompleted", "graphMoreRelated"];
  if (ids.indexOf(active.id) < 0) return null;
  return {id: active.id, start: active.selectionStart, end: active.selectionEnd};
}

function restoreGraphFocus(saved) {
  if (!saved) return;
  var target = null;
  if (saved.task) {
    target = Array.prototype.slice.call(el("tasks").querySelectorAll("[data-graph-task]")).filter(function (node) {
      return node.getAttribute("data-graph-task") === saved.task;
    })[0] || null;
  } else if (saved.id) {
    target = document.getElementById(saved.id);
  }
  if (!target || !target.focus) return;
  target.focus();
  if (target.setSelectionRange && typeof saved.start === "number") {
    target.setSelectionRange(saved.start, typeof saved.end === "number" ? saved.end : saved.start);
  }
}

function chooseGraphFocus(tasks) {
  // Snapshots are serialized by task id, not by the graph layout.  Choosing
  // from that raw order can focus a useful task several rows below the initial
  // viewport even though the same task set has a stable, readable display
  // order.  Existing selection bypasses this function, so live pushes never
  // pull a reader away from the node they chose.
  var ordered = tasks.slice().sort(function (a, b) {
    return GRAPH_STATE.rank[a.id] - GRAPH_STATE.rank[b.id];
  });
  var open = ordered.filter(function (t) { return t.phase !== "closed"; });
  var openIds = {};
  open.forEach(function (t) { openIds[t.id] = true; });
  var doing = open.filter(function (t) { return t.phase === "doing"; })[0];
  if (doing) return doing.id;
  var connected = open.filter(function (t) {
    return graphRelated(t.id).some(function (r) { return openIds[r.task]; }) ||
           graphDeclaredEdges().some(function (e) {
             return (e.from === t.id && openIds[e.to]) || (e.to === t.id && openIds[e.from]);
           });
  })[0];
  return connected ? connected.id : open.length ? open[0].id : null;
}

function setGraphCompleted(value) {
  rememberGraphCamera(); GRAPH_STATE.showCompleted = !!value; renderTasks();
}
function setGraphQuery(value) {
  rememberGraphCamera(); GRAPH_STATE.query = String(value || "");
  var query = GRAPH_STATE.query.trim().toLowerCase();
  if (query) {
    var matches = (D.tasks || []).filter(function (t) {
      return (GRAPH_STATE.showCompleted || t.phase !== "closed") &&
             (String(t.title || "").toLowerCase().indexOf(query) >= 0 || String(t.id || "").toLowerCase().indexOf(query) >= 0);
    }).sort(function (a, b) { return GRAPH_STATE.rank[a.id] - GRAPH_STATE.rank[b.id]; });
    GRAPH_STATE.selected = matches.length ? matches[0].id : null;
  }
  renderTasks();
}
function selectGraphTask(id) {
  if (!graphTask(id)) return;
  rememberGraphCamera(); GRAPH_STATE.selected = id; GRAPH_STATE.relatedExpanded = false; renderTasks();
}
function setGraphRelatedExpanded(value) {
  rememberGraphCamera(); GRAPH_STATE.relatedExpanded = !!value; renderTasks();
}
function setGraphView(view) {
  rememberGraphCamera(); GRAPH_STATE.view = view === "list" ? "list" : "graph"; renderTasks();
}
function setGraphZoom(value) {
  rememberGraphCamera(); GRAPH_STATE.zoom = Math.max(.55, Math.min(1.6, Number(value) || 1)); renderTasks();
}
function fitGraph() {
  var viewport = document.getElementById("graphViewport");
  var width = viewport && viewport.clientWidth ? viewport.clientWidth : 760;
  setGraphZoom(Math.min(1, (width - 24) / Math.max(1, GRAPH_STATE.sceneWidth)));
}

function bindGraphControls() {
  var search = document.getElementById("graphSearch");
  if (search) {
    search.value = GRAPH_STATE.query;
    search.oninput = function (event) { setGraphQuery(event.target.value); };
  }
  var completed = document.getElementById("graphCompleted");
  if (completed) completed.onclick = function () { setGraphCompleted(!GRAPH_STATE.showCompleted); };
  var graphView = document.getElementById("graphView");
  if (graphView) graphView.onclick = function () { setGraphView("graph"); };
  var listView = document.getElementById("listView");
  if (listView) listView.onclick = function () { setGraphView("list"); };
  var zoomOut = document.getElementById("graphZoomOut");
  if (zoomOut) zoomOut.onclick = function () { setGraphZoom(GRAPH_STATE.zoom - .1); };
  var zoomIn = document.getElementById("graphZoomIn");
  if (zoomIn) zoomIn.onclick = function () { setGraphZoom(GRAPH_STATE.zoom + .1); };
  var fit = document.getElementById("graphFit");
  if (fit) fit.onclick = fitGraph;
  var details = document.getElementById("graphDetails");
  if (details) details.onclick = function () { openTask(GRAPH_STATE.selected); };
  var showDone = document.getElementById("graphShowCompleted");
  if (showDone) showDone.onclick = function () { setGraphCompleted(true); };
  var more = document.getElementById("graphMoreRelated");
  if (more) more.onclick = function () { setGraphRelatedExpanded(true); };
  Array.prototype.forEach.call(el("tasks").querySelectorAll("[data-graph-task]"), function (node) {
    node.onclick = function () { selectGraphTask(node.getAttribute("data-graph-task")); };
  });
}

function renderTasks() {
  var savedFocus = graphFocusSnapshot();
  var tasks = D.tasks || [];
  var project = (D.projects || []).filter(function (p) { return p.current; })[0];
  el("projectTitle").textContent = project && project.name ? project.name : "Your work";
  if (!tasks.length) {
    el("tasks").innerHTML = '<div class="empty">No tasks recorded yet. Team activity and held work are still shown alongside.</div>';
    return;
  }

  ensureGraphOrder(tasks);
  if (GRAPH_STATE.selected && !graphTask(GRAPH_STATE.selected)) GRAPH_STATE.selected = null;

  var query = GRAPH_STATE.query.trim().toLowerCase();
  var visible = tasks.filter(function (t) {
    if (t.phase === "closed" && !GRAPH_STATE.showCompleted) return false;
    return !query || String(t.title || "").toLowerCase().indexOf(query) >= 0 || String(t.id || "").toLowerCase().indexOf(query) >= 0;
  }).sort(function (a, b) { return GRAPH_STATE.rank[a.id] - GRAPH_STATE.rank[b.id]; });
  var visibleIds = {};
  visible.forEach(function (t) { visibleIds[t.id] = true; });
  if (query && !visibleIds[GRAPH_STATE.selected]) {
    GRAPH_STATE.selected = visible.length ? visible[0].id : null;
  } else if (!query && !GRAPH_STATE.selected) {
    GRAPH_STATE.selected = chooseGraphFocus(tasks);
  }
  var positions = {}, nodeWidth = 260, stepX = 282;
  var availableWidth = Number(el("tasks").clientWidth || 760);
  var columns = Math.max(1, Math.min(4, Math.floor((availableWidth - 20) / stepX)));
  var rows = Math.ceil(visible.length / columns), rowHeights = [];
  visible.forEach(function (t, index) {
    var titleLines = Math.max(1, Math.ceil(readableTaskTitle(t).length / 22));
    var metaLength = taskStatus(t).length + taskOwners(t).join(", ").length + 12;
    var metaLines = Math.max(1, Math.ceil(metaLength / 27));
    var height = Math.max(92, 38 + titleLines * 18 + metaLines * 14);
    var row = Math.floor(index / columns);
    rowHeights[row] = Math.max(rowHeights[row] || 0, height);
    positions[t.id] = {x: 24 + (index % columns) * stepX, row: row, h: height};
  });
  var rowTops = [], y = 24;
  rowHeights.forEach(function (height, row) { rowTops[row] = y; y += height + 26; });
  visible.forEach(function (t) { positions[t.id].y = rowTops[positions[t.id].row]; });
  var sceneWidth = Math.max(320, Math.min(columns, Math.max(1, visible.length)) * stepX + 12);
  var sceneHeight = Math.max(330, y + 4);
  GRAPH_STATE.sceneWidth = sceneWidth; GRAPH_STATE.sceneHeight = sceneHeight;

  var selected = graphTask(GRAPH_STATE.selected);
  var declared = graphDeclaredEdges();
  var related = selected ? graphRelated(selected.id) : [];
  var relatedVisible = related.filter(function (r) { return visibleIds[r.task]; });
  var relatedShown = relatedVisible.slice();
  if (!GRAPH_STATE.relatedExpanded) relatedShown = relatedShown.slice(0, GRAPH_RELATED_LIMIT);
  var connected = {};
  declared.forEach(function (edge) {
    if (selected && (edge.from === selected.id || edge.to === selected.id)) connected[edge.from === selected.id ? edge.to : edge.from] = true;
  });
  relatedShown.forEach(function (r) { connected[r.task] = true; });

  var closedConnections = {};
  if (selected && !GRAPH_STATE.showCompleted) {
    declared.forEach(function (edge) {
      var other = edge.from === selected.id ? edge.to : edge.to === selected.id ? edge.from : "";
      if (other && graphTask(other) && graphTask(other).phase === "closed") closedConnections[other] = true;
    });
    related.forEach(function (r) { if (graphTask(r.task) && graphTask(r.task).phase === "closed") closedConnections[r.task] = true; });
  }
  var hiddenCompleted = Object.keys(closedConnections).length;

  var h = '<div class="graph-shell"><div class="graph-controls">' +
          '<input class="graph-search" id="graphSearch" type="search" aria-label="Search tasks" placeholder="Search tasks" value="' + esc(GRAPH_STATE.query) + '">' +
          '<button class="ghost' + (GRAPH_STATE.showCompleted ? " pressed" : "") + '" id="graphCompleted">' +
            (GRAPH_STATE.showCompleted ? "Hide completed" : "Show completed") + '</button>' +
          '<button class="ghost' + (GRAPH_STATE.view === "graph" ? " pressed" : "") + '" id="graphView">Graph</button>' +
          '<button class="ghost' + (GRAPH_STATE.view === "list" ? " pressed" : "") + '" id="listView">List</button>' +
          '<span class="graph-count">' + visible.length + ' shown · ' + tasks.length + ' total</span>' +
          '<span class="grow"></span><span class="graph-zoom"><button class="ghost" id="graphZoomOut" aria-label="Zoom out">−</button>' +
          '<button class="ghost" id="graphFit">Fit</button><button class="ghost" id="graphZoomIn" aria-label="Zoom in">+</button></span></div>';
  h += '<div class="graph-key" aria-label="Connection legend"><span><i class="dep"></i>Unlocks (prerequisite → dependent)</span>' +
       '<span><i class="rel"></i>Related work</span></div>';

  if (selected) {
    var declaredCount = declared.filter(function (edge) { return edge.from === selected.id || edge.to === selected.id; }).length;
    h += '<div class="graph-focus"><strong>' + esc(readableTaskTitle(selected)) + '</strong>' +
         '<span>' + declaredCount + (declaredCount === 1 ? " dependency" : " dependencies") + '</span>';
    if (!D.code_map_available) {
      h += '<span>Code-related links unavailable — no code map is loaded.</span>';
    } else if (!related.length) {
      h += '<span>No code-related links were found in the loaded map.</span>';
    } else {
      h += '<span>' + (relatedShown.length < relatedVisible.length
           ? "Showing " + relatedShown.length + " of " + relatedVisible.length + " visible related links."
           : relatedShown.length + (relatedShown.length === 1 ? " visible related link." : " visible related links.")) + '</span>';
      if (relatedShown.length < relatedVisible.length && !GRAPH_STATE.relatedExpanded) h += '<button class="ghost" id="graphMoreRelated">Show all related</button>';
    }
    if (hiddenCompleted) {
      h += '<span>' + hiddenCompleted + (hiddenCompleted === 1 ? " connection" : " connections") +
           ' to completed work.</span><button class="ghost" id="graphShowCompleted">Show completed</button>';
    }
    h += '<button id="graphDetails">Details</button></div>';
  }

  if (!visible.length) {
    h += '<div class="graph-empty">No tasks match this view. Clear the search or show completed work.</div></div>';
    el("tasks").innerHTML = h; bindGraphControls(); restoreGraphFocus(savedFocus); return;
  }

  if (GRAPH_STATE.view === "list") {
    h += '<div class="graph-list" aria-label="Task list">';
    visible.forEach(function (t) {
      var owners = taskOwners(t), holds = Number(t.files_held || 0);
      h += '<button class="graph-list-row" id="graphNode-' + GRAPH_STATE.rank[t.id] + '" data-graph-task="' + esc(t.id) + '" title="' + esc(t.title || "Untitled task") + '">' +
           '<span class="graph-orb">' + esc(taskStatus(t).slice(0, 1)) + '</span><span class="graph-title">' + esc(readableTaskTitle(t)) + '</span>' +
           '<span class="graph-meta"><span class="status">' + esc(taskStatus(t)) + '</span>' +
           (owners.length ? '<span>' + esc(owners.map(function (o) { return "@" + o; }).join(", ")) + '</span>' : '<span>Unassigned</span>') +
           (holds ? '<span>' + holds + (holds === 1 ? " hold" : " holds") + '</span>' : "") + '</span></button>';
    });
    h += '</div></div>';
    el("tasks").innerHTML = h; bindGraphControls(); restoreGraphFocus(savedFocus); return;
  }

  function graphCurve(from, to) {
    var fx = from.x + 25, fy = from.y + from.h / 2;
    var tx = to.x + 25, ty = to.y + to.h / 2;
    var dx = tx - fx, dy = ty - fy, length = Math.sqrt(dx * dx + dy * dy) || 1;
    var sx = fx + dx / length * 20, sy = fy + dy / length * 20;
    var ex = tx - dx / length * 20, ey = ty - dy / length * 20;
    if (Math.abs(dx) > 30) {
      var midX = (sx + ex) / 2;
      return "M" + sx + "," + sy + " C" + midX + "," + sy + " " + midX + "," + ey + " " + ex + "," + ey;
    }
    var midY = (sy + ey) / 2;
    return "M" + sx + "," + sy + " C" + sx + "," + midY + " " + ex + "," + midY + " " + ex + "," + ey;
  }
  var lines = '';
  declared.forEach(function (edge) {
    if (!visibleIds[edge.from] || !visibleIds[edge.to]) return;
    var from = positions[edge.from], to = positions[edge.to];
    var dim = selected && edge.from !== selected.id && edge.to !== selected.id;
    lines += '<path class="graph-edge dependency' + (dim ? " dim" : "") + '" d="' + graphCurve(from, to) +
             '" marker-end="url(#taskArrow)"><title>Unlocks dependent task</title></path>';
  });
  relatedShown.forEach(function (relation) {
    if (!selected || !positions[selected.id] || !positions[relation.task]) return;
    var from = positions[selected.id], to = positions[relation.task];
    lines += '<path class="graph-edge related" d="' + graphCurve(from, to) + '"><title>Related work</title></path>';
  });
  var nodes = '';
  visible.forEach(function (t) {
    var p = positions[t.id], owners = taskOwners(t), holds = Number(t.files_held || 0);
    var classes = 'graph-node p-' + esc(t.phase) + (t.id === GRAPH_STATE.selected ? " selected" : "") + (connected[t.id] ? " connected" : "");
    nodes += '<button class="' + classes + '" id="graphNode-' + GRAPH_STATE.rank[t.id] + '" data-graph-task="' + esc(t.id) + '" style="left:' + p.x + 'px;top:' + p.y +
             'px;min-height:' + p.h + 'px" title="' + esc(t.title || "Untitled task") + ' — ' + esc(taskStatus(t)) + '" aria-label="' +
             esc((t.title || "Untitled task") + ". " + taskStatus(t)) + '">' +
             '<span class="graph-orb" aria-hidden="true">' + esc(taskStatus(t).slice(0, 1)) + '</span><span class="graph-label">' +
             '<span class="graph-title">' + esc(readableTaskTitle(t)) + '</span><span class="graph-meta"><span class="status">' +
             esc(taskStatus(t)) + '</span>' +
             (owners.length ? '<span>' + esc(owners.map(function (o) { return "@" + o; }).join(", ")) + '</span>' : '<span>Unassigned</span>') +
             (holds ? '<span>' + holds + (holds === 1 ? " hold" : " holds") + '</span>' : "") + '</span></span></button>';
  });
  var scaledWidth = Math.ceil(sceneWidth * GRAPH_STATE.zoom), scaledHeight = Math.ceil(sceneHeight * GRAPH_STATE.zoom);
  h += '<div class="graph-viewport" id="graphViewport" tabindex="0" aria-label="Task connection graph">' +
       '<div class="graph-camera" style="width:' + scaledWidth + 'px;height:' + scaledHeight + 'px">' +
       '<div class="graph-scene" style="width:' + sceneWidth + 'px;height:' + sceneHeight + 'px;transform:scale(' + GRAPH_STATE.zoom + ')">' +
       '<svg width="' + sceneWidth + '" height="' + sceneHeight + '" aria-hidden="true"><defs><marker id="taskArrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path class="graph-arrow" d="M0,0 L7,3.5 L0,7 z"></path></marker></defs>' +
       lines + '</svg>' + nodes + '</div></div></div></div>';
  el("tasks").innerHTML = h;
  var viewport = document.getElementById("graphViewport");
  if (viewport) { viewport.scrollLeft = GRAPH_STATE.left; viewport.scrollTop = GRAPH_STATE.top; }
  bindGraphControls();
  restoreGraphFocus(savedFocus);
}

/* One task, in full. Opened from a row rather than always on screen: the list
   answers "what is there", this answers "what is this and where does it live".
   Rebuilt from D on every open, so it is never stale against the feed. */
function openTask(id) {
  var t = (D.tasks || []).filter(function (x) { return x.id === id; })[0];
  if (!t) { return; }
  var refreshing = OPEN_TASK === id;
  var focusedId = document.activeElement && document.activeElement.id;
  var oldDetails = el("taskTechnical");
  var expanded = refreshing && oldDetails && oldDetails.open;
  var previousScroll = el("tdet").querySelector(".tdet-bd");
  var detailScroll = refreshing && previousScroll ? previousScroll.scrollTop : 0;
  OPEN_TASK = id;
  var h = '<div class="tdet-hd">' +
          '<span class="tphase p-' + esc(t.phase) + '">' + esc(t.phase) + "</span>" +
          '<span class="tdet-title">' + esc(t.title || t.id) + "</span>" +
          '<span class="mono tdet-id">' + esc(t.id) + "</span>" +
          '<button class="ghost" id="tdetClose">Close</button></div>';

  h += '<div class="tdet-bd">';

  // Is it done? Said in words, because "closed" and "somebody checked it" are
  // different claims and the difference is the whole point of the review gate.
  var state;
  if (t.phase === "closed" && t.verified_by) {
    state = "Done, and checked by @" + esc(t.verified_by || "?") +
            (t.independence ? " (" + esc(t.independence) + ")" : "");
  } else if (t.phase === "closed") {
    state = "Finished. Independent verification was not recorded.";
  } else if (t.phase === "review") {
    state = "Finished by @" + esc(t.did || "?") + ", waiting for somebody else to check it.";
  } else if (t.phase === "doing") {
    state = "Being worked on by " + (t.doers || []).map(function (d) { return "@" + esc(d); }).join(", ");
  } else if (t.phase === "blocked") {
    state = "Blocked until these are verified: " + esc((t.blocked_by || []).join(", "));
  } else if (t.phase === "cycle") {
    state = "In a dependency loop, so nothing here can start.";
  } else {
    state = "Ready: nobody has claimed it.";
  }
  h += '<div class="tdet-state">' + state + "</div>";
  h += '<button class="ghost" id="taskHistory">Task history</button>';
  if (t.rejections) {
    h += '<div class="tdet-note amber">Sent back ' + t.rejections +
         (t.rejections === 1 ? " time" : " times") + " before.</div>";
  }

  // The checks it declared, and what actually came back for each.
  var checks = t.checks || [], res = t.check_results || {};
  if (checks.length) {
    h += '<div class="tdet-sec">CHECKS</div><div class="tdet-checks">';
    checks.forEach(function (c) {
      var r = res[c] || "";
      var cls = r === "pass" ? "ok" : r ? "bad" : "none";
      h += '<span class="chk ' + cls + '"><span class="mono">' + esc(c) + "</span>" +
           (r ? " " + esc(r) : " not run") + "</span>";
    });
    h += "</div>";
  }
  if (t.verification) {
    h += '<div class="tdet-sec">WHAT THE REVIEWER RAN</div>' +
         '<div class="tdet-note">' + esc(t.verification) + "</div>";
  }

  h += '<div class="tdet-sec">Visual evidence</div><div class="tdet-note">No screenshots linked here yet.</div>';
  h += '<details class="technical-details" id="taskTechnical"' + (expanded ? " open" : "") + '><summary id="taskTechnicalToggle">Files and code connections</summary>';

  // RECORDED DIRECTION, kept separate from the undirected map relation below.
  // The kind and provides note are technical detail, but they are also the
  // evidence for why one task really comes after another.
  var declaredForTask = graphDeclaredEdges().filter(function (edge) {
    return edge.from === id || edge.to === id;
  });
  if (declaredForTask.length) {
    h += '<div class="tdet-sec">DECLARED DEPENDENCIES (' + declaredForTask.length + ')</div><div class="tdet-files">';
    declaredForTask.forEach(function (edge) {
      var incoming = edge.to === id;
      var otherId = incoming ? edge.from : edge.to;
      var other = graphTask(otherId);
      h += '<div class="tfrow tmeet" data-task="' + esc(otherId) + '">' +
           '<span class="tfdot ' + (incoming ? "wait" : "done") + '"></span>' +
           '<span class="tfpath">' + (incoming ? "Depends on " : "Used by ") +
             esc(other ? readableTaskTitle(other) : otherId) + '</span>' +
           '<span class="tfstate">' + esc(edge.kind || "sequence") +
             (edge.provides ? " · " + esc(edge.provides) : "") + '</span></div>';
    });
    h += '</div>';
  }

  // WHAT ELSE THIS MEETS, from the code map rather than from anybody declaring
  // it. This is the join the whole thing was for: the log knows which files the
  // task touched, the map knows how files reach each other, and neither knew the
  // other. Nobody has ever declared a task edge on this machine, so without this
  // the graph could only ever be a list.
  //
  // Said as "meets", never as an ordering. A declared edge is a judgement about
  // sequence; a code connection is a fact with no direction, and the map runs at
  // roughly a third to a half recall: an inferred arrow would be confidently
  // wrong often enough to poison the board.
  var rel = t.related || [];
  if (rel.length) {
    h += '<div class="tdet-sec">MEETS IN THE CODE (' + rel.length + ")</div>";
    h += '<div class="tdet-note">Not declared by anyone: these tasks touch files that ' +
         "reach each other in the map.</div>";
    h += '<div class="tdet-files">';
    // Demote, never drop: same reason as the CLI. Sorting own work last and
    // then slicing deleted it, and that row is the one an agent reported as the
    // win.
    var strangers = rel.filter(function (r) { return !r.same_actor; });
    var ownRel = rel.filter(function (r) { return r.same_actor; });
    strangers.slice(0, 8).concat(ownRel.slice(0, 4)).forEach(function (r) {
      // NAME the places. Three agents said the same thing independently: a
      // count is a number without a noun, and you cannot act on it: whether to
      // go and knock on somebody's door depends on whether the shared files are
      // the component you both edit or four barrels every file imports. Naming
      // them also lets a reader discount a 3000-line god file on sight.
      var via = (r.via || []).map(function (v) {
        var parts = String(v).split("/");
        return parts.length > 2 ? parts.slice(-2).join("/") : v;
      });
      var more = r.shared - (r.via || []).length;
      h += '<div class="tfrow tmeet" data-task="' + esc(r.task) + '">' +
           '<span class="tfdot meet"></span>' +
           '<span class="mono tfpath">' + esc(r.task) + "</span>" +
           (r.same_actor ? '<span class="tag">your own</span>' : "") +
           '<span class="tfstate">' + r.shared +
           (r.shared === 1 ? " shared file" : " shared files") + "</span></div>";
      if (via.length) {
        h += '<div class="tvia mono">' + esc(via.join(", ")) +
             (more > 0 ? ", +" + more + " more" : "") + "</div>";
      }
    });
    h += "</div>";
  } else if (t.touches) {
    h += '<div class="tdet-sec">MEETS IN THE CODE</div>' +
         '<div class="tdet-note">Nothing. Its files reach ' + t.touches +
         " place(s) in the map, none of them touched by another task.</div>";
  }

  // WHERE IT LIVES. Derived from claims tagged to this task, kept after release
  // so finishing the work does not empty the answer.
  var files = t.files || [];
  h += '<div class="tdet-sec">FILES (' + files.length + ")</div>";
  if (!files.length) {
    h += '<div class="tdet-note">No files are tagged to this task. An agent ties ' +
         'them on as it works: <code class="mono">comms-graph claim &lt;path&gt; --task ' +
         esc(t.id) + "</code></div>";
  } else {
    h += '<div class="tdet-files">';
    files.forEach(function (f) {
      h += '<div class="tfrow">' +
           '<span class="tfdot ' + (f.held ? "held" : "done") + '"></span>' +
           '<span class="mono tfpath">' + esc(f.scope) + "</span>" +
           '<span class="tfactor">@' + esc(f.actor) + "</span>" +
           '<span class="tfstate">' + (f.held ? "held now" : "released") + "</span>" +
           "</div>";
    });
    h += "</div>";
  }
  h += "</details></div>";

  el("tdet").innerHTML = h;
  el("tdetWrap").hidden = false;
  el("tdetClose").onclick = closeTask;
  el("taskHistory").onclick = function () { closeTask(); openHistory("task", id); };
  var body = el("tdet").querySelector(".tdet-bd");
  if (body) body.scrollTop = detailScroll;
  var focusTarget = el(refreshing && focusedId ? focusedId : "tdetClose");
  if (focusTarget && focusTarget.focus) focusTarget.focus();
  // Follow the connection. Being told two tasks meet and then having to go and
  // find the other one by hand is most of the cost of knowing.
  Array.prototype.forEach.call(el("tdet").querySelectorAll(".tmeet"), function (row) {
    row.onclick = function () { openTask(row.getAttribute("data-task")); };
  });
}

function closeTask() {
  var id = OPEN_TASK;
  OPEN_TASK = null;
  el("tdetWrap").hidden = true;
  if (id) {
    var rows = Array.prototype.slice.call(el("tasks").querySelectorAll("[data-task]"));
    var target = rows.filter(function (row) { return row.getAttribute("data-task") === id; })[0] || el("historyBtn");
    if (target && target.focus) target.focus();
  }
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
   ["findings", (D.findings || []).length], ["notes", (D.notes || []).length]].forEach(function (kv) {
    h += '<div class="scell"><div class="sn mono">' + kv[1] + '</div><div class="sl">' + kv[0].toUpperCase() + "</div></div>";
  });
  h += "</div>";
  (D.alerts || []).filter(function (a) { return a.kind === "stale-map"; }).forEach(function (a) {
    h += '<p class="tdet-note">' + esc(a.text) + "</p>";
  });
  h += '<div class="sgen">read ' + esc(D.generated || "") + "</div>";
  el("session").innerHTML = h;
}

/* ---------- alarms ------------------------------------------------------ */

function renderAlarms() {
  var a = (D.alerts || []).filter(function (x) { return ["quiet", "cycle", "dangling"].indexOf(x.kind) >= 0; });
  if (!a.length) { el("alarms").innerHTML = ""; return; }
  var h = "";
  a.forEach(function (x) {
    h += '<div class="attention"><strong>' + (x.kind === "quiet" ? "A hold may need your attention" : "Task plan needs attention") + "</strong><p>" +
         (x.kind === "quiet" ? "An agent has not reported recently. Check its holds in Team before releasing anything." : esc(x.text)) + "</p></div>";
  });
  el("alarms").innerHTML = h;
}

/* ---------- driver ------------------------------------------------------ */

function renderAll(d) {
  // A pushed snapshot for the same project refreshes facts without moving the
  // reader's camera or clearing their focus.  A project switch is a new scene
  // and must not inherit a hidden completed filter or an unrelated selection.
  var nextProject = d && (d.store_key || d.root || "");
  if (D) rememberGraphCamera();
  if (GRAPH_STATE.projectKey !== nextProject) GRAPH_STATE = freshGraphState(nextProject);
  D = d;
  if (d.error) {
    el("alarms").innerHTML = '<span class="alarm r">' + esc(d.error) + "</span>";
    el("streamList").innerHTML = '<div class="empty">' + esc(d.error) + "</div>";
    return;
  }
  renderAlarms(); renderProjects(); renderNow(); renderChips();
  renderStream(); renderRoster(); renderTasks(); renderSession();
  if (OPEN_TASK) {
    if ((D.tasks || []).some(function (t) { return t.id === OPEN_TASK; })) openTask(OPEN_TASK);
    else closeTask();
  }
}

function acceptSnapshot(d) {
  if (!d || typeof d !== "object" || d.error || !Array.isArray(d.tasks) ||
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
  if (e.key === "Escape") { closeRelease(); closeTask(); closeHistory(); }
  if (e.key === "Tab") {
    var modal = !el("releaseWrap").hidden ? el("releaseWrap") : !el("historyWrap").hidden ? el("historyWrap") : !el("tdetWrap").hidden ? el("tdetWrap") : null;
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

// The task DAG, on demand. It is a whole vis-network page in an iframe and
// most of the day nobody wants it: so it costs nothing until asked for.
(function () {
  var wrap = el("dagWrap"), frame = el("dagFrame"), shown = "";
  function show(src, btn) {
    var store = currentStore();
    if (store) { src += "?store=" + encodeURIComponent(store); }
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


#: Appended to each embedded page to give the graph the whole pane.
#:
#: Both pages are complete, standalone documents, and each carries its own
#: right-hand panel: the map has search, node info and a community filter; the
#: task graph has its counts and legend. Standalone that is exactly right. Side
#: by side inside the board, with the board's own rail beside them, it meant
#: THREE columns of chrome saying overlapping things, and the graphs: the
#: reason anybody opens this: were squeezed into what was left.
#:
#: Hidden rather than removed, so both pages keep working on their own and
#: neither generator has to know it is inside a frame.
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
    "var n=window.commsTaskNetwork||window.network;"
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
                                        task=params.get("task", [""])[0], before=params.get("before", [""])[0])
                self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")
            except (ValueError, OSError) as exc:
                self._send(json.dumps({"error": str(exc)}).encode("utf-8"), "application/json", 400)
        elif path == "/api/status":
            self._send(json.dumps(board.snapshot(store)).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/map.html":
            self._send(board.map_html(store).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/tasks.html":
            self._send(board.tasks_html(store).encode("utf-8"), "text/html; charset=utf-8")
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
        #: One lock per page key, so a slow map build does not hold up the task
        #: frame. Guarded by _lock while being created.
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
            log_file = _log.user_data_home() / "comms" / key / _log.LOG_FILENAME
            return (root, log_file) if log_file.exists() else None
        return None

    def history(self, store: str = "", *, actor: str = "", task: str = "", before: str = "") -> dict:
        if store and store != _log.repo_hash(self.root) and self._store_by_key(store) is None:
            raise ValueError("The selected project is unavailable.")
        _key, _root, log_file = self._for_store(store)
        return history_page(_log.read(log_file), actor=actor, task=task, before=before)

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
        d = _log.user_data_home() / "comms" / (key or _log.repo_hash(self.root)) / "render"
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

    def tasks_html(self, store: str = "") -> str:
        def build() -> str:
            from . import taskview as _taskview

            key, _root, log_file = self._for_store(store)
            out = self._render_dir(key) / "tasks.comms.html"
            try:
                st = _state.fold(_log.read(log_file))
                res = _taskview.render(st, out, generated=_now_text())
                return Path(res.output_path).read_text(encoding="utf-8") + _EMBED_CSS
            except Exception as exc:
                return _placeholder("The task graph could not be drawn.", "", str(exc))
        return self._cached("tasks:" + (store or ""), build)


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
