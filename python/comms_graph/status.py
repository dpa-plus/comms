"""``comms status`` and ``comms log``: the two READ surfaces.

Port of comms' ``internal/subcmd/status.go`` and ``internal/subcmd/log.go``.
``status`` answers "who is here, what is held, what has gone quiet, and how many
collisions has this thing actually prevented"; ``log`` is the raw event history
with filters. They are the first thing a session runs and the last thing anybody
reads before deciding a claim is abandoned.

Three properties hold for everything in this module:

* **READ-ONLY.** No lock is taken, no event is appended, and the store is never
  created. A surface that mkdir'd on the way to answering a question is how one
  unwritable HOME took out every repository on the machine (see ``_store``'s own
  docstring in ``cli.py``); a surface that took the lock would make "check the
  board" contend with the agents doing real work.
* **IT SAYS WHEN IT CANNOT READ.** A corrupt or truncated log is reported with
  its line number and exit 2, never rendered as an empty board. "Nobody holds
  anything" and "I could not find out" are opposite answers and an agent acts on
  them in opposite ways.
* **THE LOG IS SHARED WITH THE GO BUILD.** Every data key read here is a key the
  Go build writes. Nothing is invented; where this build's reducer does not
  carry a fact the Go one does (finding/note ``priority``, task slots), the fact
  is OMITTED rather than defaulted, because a defaulted ``"priority": false`` is
  a confident wrong answer about a message somebody marked urgent.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import log as _log
from . import scope as _scope
from . import state as _state
from . import task as _task
from .cli import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    FINDING_CATEGORIES,
    _err,
    _parse_flags,
    _repo_root,
    _store,
    _warn_if_ephemeral,
)

__all__ = ["status_main", "log_main"]


STATUS_USAGE = """Usage: comms-graph status [options]

  Who is active, what is claimed, what has gone quiet, and how many collisions
  have been prevented. Reads only; never writes.

  Options:
    --since <dur>        lookback for findings and notes (default 24h)
    --stale-after <dur>  age past which a held claim is STALE and a silent
                         holder is flagged likely dead (default 1h)
    --json               machine-readable output
    --root <path>        repo root (default: the git root above the cwd)

  Exit status: 0 printed, 2 bad usage or an unreadable log."""

LOG_USAGE = """Usage: comms-graph log [options]

  The event history, filtered. Reads only; never writes.

  Options:
    --since <dur>        lookback window (default 24h)
    --actor <name>       only events by this actor
    --scope <path>       only events whose scope overlaps this path
    --type a,b           only these event types ({types})
    --category <cat>     findings only, of this category ({cats})
    --json               raw JSONL, exactly as it sits in the log
    --root <path>        repo root (default: the git root above the cwd)

  Exit status: 0 printed, 2 bad usage or an unreadable log.""".format(
    types=_log.known_types(), cats=", ".join(FINDING_CATEGORIES)
)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

#: The single coordination-recency window: an actor counts as "active" if it has
#: been seen inside it. One constant rather than a 4h literal per call site,
#: because two surfaces disagreeing about who is alive is how an agent is told
#: the ground is free by one command and refused by the next.
ACTIVE_WINDOW = timedelta(hours=4)

#: The age at which a held claim is stale: flagged STALE, its silent holder
#: flagged likely dead, and: in the Go build: stealable without confirmation.
#: Exposed as ``--stale-after`` so the CLI and the dashboard can be pointed at
#: the same threshold; a board that calls a claim stale while the steal path
#: still calls it live is worse than no flag at all.
STALE_CLAIM_AFTER = timedelta(hours=1)


# Go's time.ParseDuration grammar, which is what the Go build's --since accepts.
# Deliberately NOT extended with a `d` unit: the two builds read the same log and
# scripts get copied between them, so a window this build accepted and the Go one
# rejected would work here and die there: with the failure landing on whoever
# inherited the script, not whoever wrote it.
_DURATION_PART = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|μs|ms|s|m|h)")
_UNIT_SECONDS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "μs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


def _parse_duration(raw: str) -> timedelta:
    """Interpret ``1h``, ``30m``, ``168h``, ``1h30m``. Raises ValueError."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty")
    sign = 1
    if text[0] in "+-":
        if text[0] == "-":
            sign = -1
        text = text[1:]
    if text == "0":
        return timedelta(0)
    seconds = 0.0
    pos = 0
    while pos < len(text):
        part = _DURATION_PART.match(text, pos)
        if part is None:
            raise ValueError(f"cannot read {raw!r} as a duration")
        seconds += float(part.group(1)) * _UNIT_SECONDS[part.group(2)]
        pos = part.end()
    return timedelta(seconds=sign * seconds)


def _duration_flag(flags: dict, name: str, default: timedelta) -> timedelta | None:
    """A duration flag, or None once the failure has been reported.

    A negative window is rejected rather than clamped: ``--since -24h`` means the
    caller's arithmetic is wrong somewhere, and silently showing them the last 24
    hours would hide the bug behind a plausible-looking board.
    """
    raw = flags.get(name)
    if not raw:
        return default
    try:
        value = _parse_duration(raw)
    except ValueError:
        _err(f"error: invalid --{name} {raw!r} (use 1h, 30m, 168h, etc.)")
        _err("  Days are not a unit here, in either build: say 168h, not 7d.")
        return None
    if value < timedelta(0):
        _err(f"error: --{name} must not be negative (got {raw!r})")
        return None
    return value


def _short_age(delta: timedelta) -> str:
    """``now`` / ``12m`` / ``5h`` / ``3d``. Same buckets as the dashboard."""
    seconds = delta.total_seconds()
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def _stamp(ts: datetime | None) -> str:
    if ts is None:
        return ""
    return ts.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Reading, and saying so when we cannot
# ---------------------------------------------------------------------------


def _events_or_none(log_file: Path) -> list | None:
    """Every event in the log, or None once the failure has been reported.

    Deliberately not ``cli._read_state``: ``log`` needs the raw events, not the
    fold, and one error path shared by both surfaces beats two that can drift
    apart on what an unreadable store looks like.

    An ABSENT log is not a failure: a repo nobody has claimed in yet honestly
    has no events. An UNREACHABLE one is: ``Path.is_file()`` answers False for a
    symlink loop and for a stray file where the store directory should be, which
    is how "we could not look" came to render as an empty, calm board.
    """
    try:
        log_file.stat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        _err(f"error: cannot reach the comms log at {log_file}: {exc.strerror or exc}")
        _err("  Nothing is being reported because nothing could be read.")
        return None
    try:
        return _log.read(log_file)
    except _log.CorruptLogError as exc:
        _err(f"error: the comms log is unreadable: {exc}")
        _err(f"  It is at {log_file}. Fix or move it; comms will not guess past it.")
        return None
    except OSError as exc:
        _err(f"error: cannot read the comms log at {log_file}: {exc.strerror or exc}")
        return None


def _open_read_only(flags: dict) -> tuple[Path, list] | None:
    """Resolve the store and read it, without creating anything."""
    root = _repo_root(flags.get("root"))
    log_file, _lock_file = _store(root, create=False)
    _warn_if_ephemeral(log_file)
    events = _events_or_none(log_file)
    if events is None:
        return None
    return root, events


# ---------------------------------------------------------------------------
# The roster
# ---------------------------------------------------------------------------


def _last_seen(session) -> datetime:
    """The actor's passive heartbeat, falling back to its hello.

    Liveness is judged from the most recent event of ANY type, not from the
    one-shot hello: an agent that greeted at 09:00 and has been claiming and
    releasing ever since is plainly alive, and a crashed one drops off the roster
    once its silence outlasts the window.
    """
    return session.last_seen or session.ts


def _sort_by_activity(sessions: list) -> None:
    """Most recently active first, ties broken by name.

    The name tiebreak is not cosmetic: ``State.sessions`` is a dict and two
    actors sharing a heartbeat would otherwise swap places between runs, which
    makes a diff of two status outputs unreadable.
    """
    sessions.sort(key=lambda s: (-_last_seen(s).timestamp(), s.actor))


def _active_sessions(st: _state.State, cutoff: datetime) -> list:
    return [s for s in st.sessions.values() if _last_seen(s) > cutoff]


def _mark_leader(sessions: list) -> None:
    """Exactly one leader among the ACTIVE sessions, or none if there are none.

    An explicit leader wins; otherwise the earliest greeting does. Mutating the
    folded sessions is safe here and nowhere else: this State was built from the
    log a few lines ago and is thrown away when the command returns: nothing
    written to disk depends on it.
    """
    if not sessions:
        return
    explicit = next((s for s in sessions if s.leader), None)
    for s in sessions:
        s.leader = False
    leader = explicit or min(sessions, key=lambda s: s.ts)
    leader.leader = True


def _orphan_claim_holders(st: _state.State, exclude: set[str]) -> list:
    """Roster rows for actors that hold claims but have no session at all.

    An actor is orphaned when its session was deleted: a retire, or its named
    session ended, while its process kept claiming. Without these rows the
    operator sees locks in ACTIVE CLAIMS with nobody to attribute them to, which
    reads as "claims that will not go away" and offers no one to chase.

    The synthetic heartbeat comes from the actor's own claim timestamps, so the
    row can still be flagged LIKELY DEAD. The session tag is kept only when every
    orphan claim agrees on it: a mixed set has no single true answer and
    inventing one would misattribute a lock to a window it was never opened in.
    """
    spans: dict[str, dict] = {}
    for claim in st.claims.values():
        if claim.actor in exclude or claim.actor in st.sessions:
            continue
        span = spans.get(claim.actor)
        if span is None:
            spans[claim.actor] = {
                "first": claim.ts,
                "last": claim.ts,
                "session_id": claim.session_id,
                "session_name": claim.session_name,
                "mixed": False,
            }
            continue
        span["first"] = min(span["first"], claim.ts)
        span["last"] = max(span["last"], claim.ts)
        if claim.session_id != span["session_id"]:
            span["mixed"] = True
    out = []
    for actor, span in spans.items():
        session = _state.Session(actor=actor, ts=span["first"], last_seen=span["last"])
        if not span["mixed"]:
            session.session_id = span["session_id"]
            session.session_name = span["session_name"]
        out.append(session)
    return out


def _roster(st: _state.State, cutoff: datetime) -> list:
    """Actors active in the window, PLUS silent actors that still hold ground.

    The second set is the whole point of the roster being separate from "who is
    active". A crashed holder drops out of the activity window at exactly the
    moment its orphaned locks most need releasing, so without it the row: and
    the LIKELY DEAD flag that tells you to release them: disappears precisely
    when somebody needs it. Leader is marked among the ACTIVE set only: a dead
    agent must never be shown leading anything.
    """
    active = _active_sessions(st, cutoff)
    _sort_by_activity(active)
    _mark_leader(active)
    in_roster = {s.actor for s in active}
    holders = []
    for session in st.sessions.values():
        if session.actor in in_roster:
            continue
        if st.active_claims_by_actor(session.actor):
            session.leader = False
            holders.append(session)
            in_roster.add(session.actor)
    holders.extend(_orphan_claim_holders(st, in_roster))
    _sort_by_activity(holders)
    return active + holders


def _sorted_claims(st: _state.State) -> list:
    return sorted(st.claims.values(), key=lambda c: c.ts)


def _recent(items: list, cutoff: datetime, limit: int) -> list:
    """The newest entries at or after ``cutoff``, at most ``limit`` of them."""
    out = [x for x in items if x.ts >= cutoff]
    out.sort(key=lambda x: x.ts, reverse=True)
    if limit > 0:
        out = out[:limit]
    return out


def _counts_for(st: _state.State, actor: str) -> tuple[int, int]:
    claims = sum(1 for c in st.claims.values() if c.actor == actor)
    findings = sum(1 for f in st.findings if f.actor == actor)
    return claims, findings


def _sorted_tasks(st: _state.State) -> list:
    _epoch = datetime(1, 1, 1, tzinfo=timezone.utc)
    return sorted(st.tasks.values(), key=lambda t: (t.ts or _epoch, t.id))


def _markdown_slugs(directory: Path) -> list[str]:
    """The ``.md`` basenames in a directory, sorted. Missing dir -> nothing."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return sorted(
        e.name[:-3]
        for e in entries
        if e.is_file() and e.name.endswith(".md") and not e.name.startswith(".")
    )


def _list_docs(root: Path) -> list[str]:
    return _markdown_slugs(root / ".comms" / "docs")


def _list_global_lessons() -> list[str]:
    try:
        home = _log.user_data_home()
    except (RuntimeError, OSError):
        # An unsupported platform has no lessons directory to list. That costs a
        # decorative line; it must not cost the roster and the claims.
        return []
    return _markdown_slugs(home / "comms" / "global" / "lessons")


def _limit_head(items: list, cap: int) -> tuple[list, int]:
    if len(items) <= cap:
        return items, 0
    return items[:cap], len(items) - cap


def _limit_tail(items: list, cap: int) -> tuple[list, int]:
    """Keep the LAST ``cap`` entries of a chronological list.

    For claims the newest are the live conflict surface somebody is about to
    edit into; the oldest are already called out by STALE and LIKELY DEAD. The
    enforcement paths (``claim``, ``check``) and ``--json`` are uncapped, so no
    agent can edit into a conflict the cap hid.
    """
    if len(items) <= cap:
        return items, 0
    return items[len(items) - cap:], len(items) - cap


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status_main(argv: list[str]) -> int:
    """``argv`` is everything after ``status``."""
    if any(a in ("-h", "--help", "-?") for a in argv):
        print(STATUS_USAGE)
        return EXIT_OK
    _positional, flags = _parse_flags(argv)

    since_raw = flags.get("since") or "24h"
    since = _duration_flag(flags, "since", timedelta(hours=24))
    if since is None:
        return EXIT_USAGE
    stale_after = _duration_flag(flags, "stale-after", STALE_CLAIM_AFTER)
    if stale_after is None:
        return EXIT_USAGE
    if stale_after <= timedelta(0):
        # `--stale-after 0` would flag every claim ever opened, including the one
        # taken a second ago, which makes the flag mean nothing.
        stale_after = STALE_CLAIM_AFTER

    opened = _open_read_only(flags)
    if opened is None:
        return EXIT_ERROR
    root, events = opened
    st = _state.fold(events)

    now = datetime.now(timezone.utc)
    cutoff = now - since
    if "json" in flags:
        _emit_status_json(st, root, now, cutoff, stale_after)
        return EXIT_OK
    _emit_status_human(st, root, now, cutoff, since_raw, stale_after)
    return EXIT_OK


def _emit_status_human(
    st: _state.State,
    root: Path,
    now: datetime,
    cutoff: datetime,
    since_raw: str,
    stale_after: timedelta,
) -> None:
    all_sessions = _roster(st, now - ACTIVE_WINDOW)
    all_claims = _sorted_claims(st)
    all_docs = _list_docs(root)
    all_lessons = _list_global_lessons()
    sessions, omitted_sessions = _limit_head(all_sessions, 10)
    claims, omitted_claims = _limit_tail(all_claims, 50)
    findings = _recent(st.findings, cutoff, 5)
    notes = _recent(st.notes, cutoff, 3)
    docs, omitted_docs = _limit_head(all_docs, 10)
    lessons, omitted_lessons = _limit_head(all_lessons, 8)
    omitted = omitted_sessions + omitted_claims + omitted_docs + omitted_lessons

    print(f"ACTIVE SESSIONS (active in last {_short_age(ACTIVE_WINDOW)})")
    if not sessions:
        print("  (none)")
    for session in sessions:
        held, found = _counts_for(st, session.actor)
        role = "  leader" if session.leader else ""
        label = f" ({session.label})" if session.label else ""
        named = f'  session="{session.session_name}"' if session.session_name else ""
        silent = now - _last_seen(session)
        seen = "active now" if silent < timedelta(minutes=1) else f"seen {_short_age(silent)} ago"
        # Holding locks WHILE silent is the crash signal. Silence on its own is
        # an idle agent and perfectly benign; locks on their own are somebody
        # working. Only the pair is worth interrupting a reader for, and the fix
        # is `release --force` on the ids below.
        dead = ""
        if held > 0 and silent >= stale_after:
            dead = f"   ** LIKELY DEAD: holds {held}, silent {_short_age(silent)} **"
        print(
            f"  @{session.actor:<14}{label} {seen}   "
            f"{held} claim{_plural(held)}  {found} finding{_plural(found)}"
            f"{role}{named}{dead}"
        )

    print()
    print("ACTIVE CLAIMS")
    if not claims:
        print("  (none)")
    for claim in claims:
        named = f'   session="{claim.session_name}"' if claim.session_name else ""
        # Age and the STALE tag, because a lock opened 14h ago must not read the
        # same as one opened 2m ago: that difference is the whole basis for
        # deciding whether to go around somebody.
        age = now - claim.ts
        stale = "  STALE" if age >= stale_after else ""
        when = claim.ts.astimezone().strftime("%H:%M")
        print(
            f"  @{claim.actor:<14} {claim.scope}   \"{claim.intent}\"   "
            f"(since {when} · {_short_age(age)}){stale}{named}"
        )

    print()
    print(f"RECENT FINDINGS (last {since_raw})")
    if not findings:
        print("  (none)")
    for finding in findings:
        print(f"  {finding.category:<9} @{finding.actor:<14} {finding.summary}")

    print()
    print(f"RECENT NOTES (last {since_raw})")
    if not notes:
        print("  (none)")
    for note in notes:
        print(f"  @{note.actor:<14} {note.body}")

    _emit_task_summary(st)
    _emit_blocked_summary(st, cutoff, since_raw)

    if docs:
        print()
        print(f"DOCS ({len(docs)}): {', '.join(docs)}")
    if lessons:
        print()
        print(f"GLOBAL LESSONS ({len(lessons)}): {', '.join(lessons)}")
    if omitted:
        print()
        print(f"... {omitted} more; run `comms-graph log --since {since_raw}` for details")


def _emit_task_summary(st: _state.State) -> None:
    """The work graph, but only the parts somebody could act on.

    A wall of blocked tasks is not status, it is noise: blocked work is a count,
    and the things that need a person are named.
    """
    if not st.tasks:
        return
    buckets: dict[str, list] = {
        _task.PHASE_REVIEW: [],
        _task.PHASE_READY: [],
        _task.PHASE_DOING: [],
        _task.PHASE_BLOCKED: [],
        _task.PHASE_CLOSED: [],
        _task.PHASE_CYCLE: [],
    }
    for t in _sorted_tasks(st):
        if t.phase in buckets:
            buckets[t.phase].append(t)
    closed = buckets[_task.PHASE_CLOSED]
    open_count = sum(len(v) for k, v in buckets.items() if k != _task.PHASE_CLOSED)

    print()
    print(f"TASKS ({open_count} open, {len(closed)} closed)")
    for t in buckets[_task.PHASE_REVIEW]:
        print(f"  VERIFY  {t.id:<16} {t.title}")
        print(f"          done by @{t.did}: needs someone else")
    for t in buckets[_task.PHASE_READY]:
        print(f"  READY   {t.id:<16} {t.title}")
    for t in buckets[_task.PHASE_DOING]:
        who = ", @".join(t.doers)
        print(f"  DOING   {t.id:<16} {t.title}  (@{who})")
    if buckets[_task.PHASE_BLOCKED]:
        print(f"  {len(buckets[_task.PHASE_BLOCKED])} waiting on work that has not been verified yet")
    for t in buckets[_task.PHASE_CYCLE]:
        print(f"  CYCLE   {t.id:<16} depends on itself: the plan needs fixing")
    if st.refused_task_states:
        n = len(st.refused_task_states)
        print(f"  {n} refused transition{_plural(n)}: see `comms-graph brief <task>`")


def _emit_blocked_summary(st: _state.State, cutoff: datetime, since_raw: str) -> None:
    """How many collisions comms actually prevented.

    This is the number the tool exists to produce and the only one that answers
    "is claiming files worth the ceremony". Reported all-time, not just for the
    window: a prevented collision is not news that goes stale, and the point of
    the line is to let a total accumulate somewhere visible.
    """
    if not st.blocked:
        return
    recent = sum(1 for b in st.blocked if b.ts >= cutoff)
    print()
    line = f"COLLISIONS PREVENTED: {len(st.blocked)} all time"
    if recent:
        line += f", {recent} in the last {since_raw}"
    print(line)
    for b in st.blocked[-3:]:
        # A refusal against a TASK: a self-review, a failing check: has no
        # scope and no holder. Rendering it through the claim sentence printed
        # "stopped from editing  (held by @)", which reads as a bug in comms
        # rather than as the rule doing its job.
        if b.scope:
            print(f"  @{b.actor} was stopped from editing {b.scope} (held by @{b.holder})")
        elif b.task:
            why = f" ({b.reason})" if b.reason else ""
            print(f"  @{b.actor} was refused on task {b.task}{why}")
        else:
            print(f"  @{b.actor} was refused")


def _emit_status_json(
    st: _state.State,
    root: Path,
    now: datetime,
    cutoff: datetime,
    stale_after: timedelta,
) -> None:
    """The canonical machine shape. Uncapped on purpose: see ``_limit_tail``."""
    out: dict = {
        "sessions": [],
        "claims": [],
        "findings": [],
        "notes": [],
        "docs": _list_docs(root),
        "lessons": _list_global_lessons(),
        "tasks": [],
    }
    for s in _roster(st, now - ACTIVE_WINDOW):
        row = {
            "actor": s.actor,
            "ts": _stamp(s.ts),
            "last_seen": _stamp(_last_seen(s)),
            "leader": s.leader,
        }
        if s.label:
            row["label"] = s.label
        if s.session_id:
            row["session_id"] = s.session_id
        if s.session_name:
            row["session_name"] = s.session_name
        out["sessions"].append(row)
    for c in _sorted_claims(st):
        age = now - c.ts
        row = {
            "id": c.id,
            "actor": c.actor,
            "scope": str(c.scope),
            "intent": c.intent,
            "ts": _stamp(c.ts),
            "age": _short_age(age),
            "stale": age >= stale_after,
        }
        if c.stolen_from_id:
            row["stole_id"] = c.stolen_from_id
        if c.task:
            row["task"] = c.task
        if c.session_id:
            row["session_id"] = c.session_id
        if c.session_name:
            row["session_name"] = c.session_name
        out["claims"].append(row)
    for f in _recent(st.findings, cutoff, 50):
        row = {
            "id": f.id,
            "actor": f.actor,
            "category": f.category,
            "summary": f.summary,
            "ts": _stamp(f.ts),
        }
        if f.session_id:
            row["session_id"] = f.session_id
        if f.session_name:
            row["session_name"] = f.session_name
        out["findings"].append(row)
    for n in _recent(st.notes, cutoff, 50):
        row = {"id": n.id, "actor": n.actor, "body": n.body, "ts": _stamp(n.ts)}
        if n.session_id:
            row["session_id"] = n.session_id
        if n.session_name:
            row["session_name"] = n.session_name
        out["notes"].append(row)
    for t in _sorted_tasks(st):
        row = {"id": t.id, "title": t.title, "phase": t.phase}
        if t.size:
            row["size"] = t.size
        if t.doers:
            row["doers"] = list(t.doers)
        if t.did:
            row["done_by"] = t.did
        if t.verified_by:
            row["verified_by"] = t.verified_by
        if t.independence:
            row["independence"] = t.independence
        if t.rejections:
            row["rejections"] = t.rejections
        if t.blocked_by:
            row["blocked_by"] = list(t.blocked_by)
        if t.ref:
            row["ref"] = t.ref
        out["tasks"].append(row)
    print(json.dumps(out, indent=2))


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


def log_main(argv: list[str]) -> int:
    """``argv`` is everything after ``log``."""
    if any(a in ("-h", "--help", "-?") for a in argv):
        print(LOG_USAGE)
        return EXIT_OK
    _positional, flags = _parse_flags(argv)

    since = _duration_flag(flags, "since", timedelta(hours=24))
    if since is None:
        return EXIT_USAGE

    type_filter = _parse_type_set(flags.get("type", ""))
    if type_filter is False:
        return EXIT_USAGE

    category = (flags.get("category") or "").strip()
    if category and category not in FINDING_CATEGORIES:
        _err(f"error: unknown category {category!r}; choose "
             + ", ".join(FINDING_CATEGORIES))
        return EXIT_USAGE

    scope_filter = None
    raw_scope = flags.get("scope") or ""
    if raw_scope:
        try:
            scope_filter = _scope.parse(raw_scope)
        except _scope.ScopeError as exc:
            _err(f"error: --scope {raw_scope!r} is not a scope: {exc}")
            return EXIT_USAGE

    # `--actor` is the Go build's spelling; `--as` is what every other verb in
    # this CLI takes, and an agent that has typed `--as me` ten times today will
    # type it here too.
    actor = (flags.get("actor") or flags.get("as") or "").strip()

    opened = _open_read_only(flags)
    if opened is None:
        return EXIT_ERROR
    _root, events = opened

    cutoff = datetime.now(timezone.utc) - since
    as_json = "json" in flags
    shown: list = []
    for ev in events:
        if ev.ts < cutoff:
            continue
        if actor and ev.actor != actor:
            continue
        if type_filter is not None and ev.type not in type_filter:
            continue
        if scope_filter is not None and not _event_touches(ev, scope_filter):
            continue
        if category:
            # A category is a question about findings, so it excludes everything
            # else outright rather than letting claims and notes through a filter
            # that cannot apply to them.
            if ev.type != _log.TYPE_FINDING:
                continue
            if (ev.data or {}).get("category") != category:
                continue
        if as_json:
            # The stored bytes, re-encoded canonically: the same line the Go
            # build would write. Piping `log --json` back into a log must not
            # change what it says.
            sys.stdout.write(ev.encode().decode("utf-8"))
        else:
            shown.append(ev)
    if not as_json:
        _print_events_human(shown)
    return EXIT_OK


def _parse_type_set(raw: str):
    """A set of event types, None for "no filter", or False once refused.

    Validated against the ONE list of types this build knows (``log.KNOWN_TYPES``)
    rather than a second copy kept here: the Go port carried two hand-maintained
    whitelists in two packages, and adding a type meant remembering both.
    """
    text = (raw or "").strip()
    if not text:
        return None
    out = set()
    for part in text.split(","):
        name = part.strip()
        if not _log.is_known_type(name):
            _err(f"error: unknown event type {name!r}; choose {_log.known_types()}")
            return False
        out.add(name)
    return out


def _path_refs(data) -> list[str]:
    """Every ``data.refs`` value whose kind is ``path``."""
    if not isinstance(data, dict):
        return []
    refs = data.get("refs")
    if not isinstance(refs, list):
        return []
    out = []
    for entry in refs:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") == "path" and isinstance(entry.get("value"), str):
            if entry["value"]:
                out.append(entry["value"])
    return out


def _event_touches(ev, want: _scope.Scope) -> bool:
    """Whether an event's scope OR one of its path refs overlaps ``want``.

    The path-ref half is not an extra: findings and notes carry their target file
    in ``data.refs`` and have an EMPTY top-level scope, so without it
    ``log --scope <file> --type finding``: the query an agent runs to learn a
    file's history: returns nothing while hundreds of findings name that file.
    """
    for raw in list(ev.scope or []) + _path_refs(ev.data):
        try:
            parsed = _scope.parse(raw)
        except _scope.ScopeError:
            # A scope that will not parse cannot be matched. Skipping it is the
            # only safe direction: claiming a match would attribute somebody
            # else's event to this file.
            continue
        if _scope.overlaps(parsed, want):
            return True
    return False


def _release_summary(data) -> str:
    """What a release event says it did, in the Go build's own words."""
    data = data or {}

    def text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    def flag(key: str) -> bool:
        return data.get(key) is True

    refs = data.get("refs")
    count = len(refs) if isinstance(refs, list) else 0
    if flag("comms_session_end"):
        return f"ended comms session; released {count} claim{_plural(count)}"
    if flag("session_retire"):
        return (f"retired @{text('retired_actor')} from active sessions; "
                f"released {count} claim{_plural(count)}")
    if flag("leader_transfer"):
        return f"@{text('leader_actor')} became comms leader"
    return text("result") or text("reason")


def _join_scope(scope) -> str:
    return ",".join(scope) if scope else "-"


def _short_id(value: str) -> str:
    return value[:6] if len(value) > 6 else value


def _print_events_human(events: list) -> None:
    """Rows, with one atomic claim rendered as one row.

    `claim a b c` appends one event per scope, which is correct in the log and
    unreadable on the way out: claiming eleven files produced eleven consecutive
    rows carrying the same actor, the same second and the same intent, so a
    single decision arrived as eleven facts. An agent reading its own history
    described it as one fact rendered as twenty-one rows.

    Grouped at RENDER time, not in the log. The per-scope events are what make a
    claim checkable path by path, so they stay exactly as written; this only
    stops the reader from having to collapse them by eye. It works on logs
    already on disk for the same reason.

    The test for "one action" is deliberately strict: adjacent in the file, same
    actor, same type, same intent, and within a second. append_batch writes a
    batch contiguously and microseconds apart, so anything looser would start
    merging decisions that were genuinely separate.
    """
    run: list = []

    def flush() -> None:
        if run:
            _print_event_human(run[0], extra_scopes=[e.scope for e in run[1:]])
            run.clear()

    for ev in events:
        if run:
            first = run[0]
            same = (ev.type == first.type == _log.TYPE_CLAIM
                    and ev.actor == first.actor
                    and (ev.data or {}).get("intent") == (first.data or {}).get("intent")
                    and abs((ev.ts - first.ts).total_seconds()) <= 1.0
                    and not (ev.data or {}).get("steals")
                    and not (first.data or {}).get("steals"))
            if same:
                run.append(ev)
                continue
            flush()
        run.append(ev)
    flush()


def _print_event_human(ev, extra_scopes: list | None = None) -> None:
    """One readable row per event.

    Every branch renders a type this build UNDERSTANDS. The fallback exists for
    the other case: a newer comms wrote a type we can read past but not render:
    and prints the row rather than dropping it, so an unknown event is at least
    visible in the history instead of silently missing from it.
    """
    data = ev.data or {}

    def text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    ts = ev.ts.astimezone().strftime("%m-%d %H:%M:%S")
    if ev.type == _log.TYPE_HELLO:
        print(f"{ts}  hello    @{ev.actor}")
    elif ev.type == _log.TYPE_CLAIM:
        stole = text("steals") or text("stole_id")
        tail = f"  (stole {_short_id(stole)})" if stole else ""
        scopes = _join_scope(ev.scope)
        if extra_scopes:
            # Named, not counted. "+5 more" would hide exactly the thing
            # somebody greps this output for.
            more = ", ".join(_join_scope(x) for x in extra_scopes)
            scopes = f"{scopes}, {more}"
        print(f'{ts}  claim    @{ev.actor}  {scopes}  "{text("intent")}"{tail}')
    elif ev.type == _log.TYPE_RELEASE:
        original = text("original_actor")
        summary = _release_summary(data)
        if original and original != ev.actor:
            print(f'{ts}  release  @{ev.actor} arbitrated @{original}\'s claim  "{summary}"')
        else:
            print(f'{ts}  release  @{ev.actor}  "{summary}"')
    elif ev.type == _log.TYPE_NOTE:
        print(f"{ts}  note     @{ev.actor}  {text('body')}")
    elif ev.type == _log.TYPE_FINDING:
        print(f"{ts}  finding  @{ev.actor}  [{text('category')}] {text('summary')}")
    elif ev.type == _log.TYPE_BLOCKED:
        # The one event that is evidence the tool did its job. Rendering it as a
        # bare "blocked @actor" row would hide what was refused and who held it,
        # which is the only part anybody reads this line for.
        target = text("scope") or (f"task {text('task')}" if text("task") else "")
        holder = text("holder")
        by = f" (held by @{holder})" if holder else ""
        why = f": {text('reason')}" if text("reason") else ""
        print(f"{ts}  blocked  @{ev.actor}  {target}{by}{why}")
    elif ev.type == _log.TYPE_TASK:
        print(f"{ts}  task     @{ev.actor}  {text('task')}  {text('title')}")
    elif ev.type == _log.TYPE_TASK_EDGE:
        kind = text("kind")
        label = f"  [{kind}]" if kind else ""
        print(f"{ts}  edge     @{ev.actor}  {text('from')} -> {text('to')}{label}")
    elif ev.type == _log.TYPE_TASK_STATE:
        print(f"{ts}  state    @{ev.actor}  {text('task')} -> {text('state')}")
    else:
        print(f"{ts}  {ev.type:<8} @{ev.actor}")


def main(argv: list[str]) -> int:
    """Dispatch for ``status`` and ``log`` as one module.

    ``argv`` is everything after the verb, so the verb itself has to be the first
    element here: this exists for a caller that has both surfaces behind one
    entry point, and for the module to be runnable on its own.
    """
    verb = argv[0] if argv else ""
    if verb == "status":
        return status_main(argv[1:])
    if verb == "log":
        return log_main(argv[1:])
    _err(f"error: unknown read surface {verb!r}; this module serves status and log")
    return EXIT_USAGE
