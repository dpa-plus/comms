"""The short answers: the caller's own view for status and board, whoami and claims.

An agent reads these to decide what to do next, so they say what it holds, who
else holds how many files, what clashes with its ground and who wrote to it, and
point at ``--all`` for the rest. ``--all`` and ``--json`` are unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import identity as _identity
from . import state as _state
from .cli import EXIT_OK, _parse_flags, _read_state, _repo_root, _store

STALE_AFTER = timedelta(hours=1)
OWN_SHOWN = 8
HOLDERS_SHOWN = 6
CLASHES_SHOWN = 3
NOTES_SHOWN = 3
STALE_SHOWN = 3
NOTE_CHARS = 100


def _age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def _names(st: _state.State, me: str) -> frozenset[str]:
    if not me:
        return frozenset()
    return _identity.family(st, _identity.host_session(), me)


def _is_stale(st, store: Path, claim, now: datetime, stale_after: timedelta) -> bool:
    if now - claim.ts < stale_after:
        return False
    alive = _identity.last_alive(st, store, claim.actor) or claim.ts
    return now - max(alive, claim.ts) >= stale_after


def view(st: _state.State, store: Path, me: str, *, now: datetime | None = None,
         stale_after: timedelta = STALE_AFTER) -> list[str]:
    """Own claims, others as counts per holder, clashes, notes to you."""
    from . import cli as _cli
    from . import scope as _scope

    now = now or datetime.now(timezone.utc)
    mine_names = _names(st, me)
    claims = sorted(st.claims.values(), key=lambda c: c.ts)
    mine = [c for c in claims if c.actor in mine_names]
    others = [c for c in claims if c.actor not in mine_names]
    lines: list[str] = []

    if me:
        lines.append(f"you are @{me}: {len(mine)} claim{'' if len(mine) == 1 else 's'} held")
    else:
        lines.append("you are not identified (pass --as <name>), so every claim shows as somebody else's")
    for c in mine[-OWN_SHOWN:]:
        intent = f'  "{c.intent}"' if c.intent else ""
        lines.append(f"  {c.scope}{intent}  {_age(now - c.ts)}")
    if len(mine) > OWN_SHOWN:
        lines.append(f"  ... {len(mine) - OWN_SHOWN} older ones")

    if others:
        by_actor: dict[str, list] = {}
        for c in others:
            by_actor.setdefault(c.actor, []).append(c)
        lines.append(f"others hold {len(others)} file{'' if len(others) == 1 else 's'} "
                     f"({len(by_actor)} agent{'' if len(by_actor) == 1 else 's'}):")
        ranked = sorted(by_actor.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        for actor, held in ranked[:HOLDERS_SHOWN]:
            stale = sum(1 for c in held if _is_stale(st, store, c, now, stale_after))
            mark = f", {stale} STALE" if stale else ""
            newest = _age(now - max(c.ts for c in held))
            lines.append(f"  @{actor}  {len(held)}{mark}  newest {newest}")
        if len(ranked) > HOLDERS_SHOWN:
            lines.append(f"  ... {len(ranked) - HOLDERS_SHOWN} more agents")
        stale = [c for c in others if _is_stale(st, store, c, now, stale_after)]
        for c in stale[:STALE_SHOWN]:
            lines.append(f"  STALE @{c.actor} {c.scope}  [{c.id}]")
    else:
        lines.append("nobody else holds anything")

    clashes = []
    for c in mine:
        for other in st.conflicts_outside(c.scope, mine_names):
            clashes.append((c, other))
    if clashes:
        lines.append(f"clashes with what you hold ({len(clashes)}):")
        for c, other in clashes[:CLASHES_SHOWN]:
            lines.append(f"  {c.scope} overlaps @{other.actor}'s {other.scope}")

    if mine_names:
        addressed = [n for n in st.notes
                     if n.actor not in mine_names and now - n.ts < timedelta(hours=24)
                     and any(_cli._mentions(n.body, a) for a in mine_names)]
        if addressed:
            lines.append("notes to you:")
            for n in addressed[-NOTES_SHOWN:]:
                lines.append(f"  @{n.actor}: {n.body[:NOTE_CHARS]}")
    return lines


def all_hint(command: str) -> str:
    return f"(`comms-graph {command} --all` shows everything)"


def _open_state(flags: dict) -> tuple[Path, Path, _state.State]:
    root = _repo_root(flags.get("root"))
    log_file, _lock = _store(root, create=False)
    st = _read_state(log_file) if log_file.exists() else _state.fold([])
    return root, log_file.parent, st


def claims_main(argv: list[str]) -> int:
    from . import cli as _cli

    _, flags = _parse_flags(argv)
    me = _cli._actor(flags, write=False)
    _root, store, st = _open_state(flags)
    if not me:
        print("no actor, so nothing is yours. `comms-graph board` shows who holds what.")
        return EXIT_OK
    now = datetime.now(timezone.utc)
    names = _names(st, me)
    mine = sorted((c for c in st.claims.values() if c.actor in names), key=lambda c: c.ts)
    if not mine:
        print(f"@{me} holds nothing here")
        return EXIT_OK
    print(f"@{me} holds {len(mine)}:")
    for c in mine:
        intent = f'  "{c.intent}"' if c.intent else ""
        print(f"  {c.scope}{intent}  {_age(now - c.ts)}  [{c.id}]")
    return EXIT_OK


_SOURCE = {
    "flag": "from --as",
    "env": "from COMMS_ACTOR",
    "session": "this session's own name",
    "auto": "auto-name for this session, recorded on your first write",
}


def whoami_main(argv: list[str]) -> int:
    _, flags = _parse_flags(argv)
    root, _store_dir, st = _open_state(flags)
    session = _identity.host_session()
    found = _identity.resolve(flags.get("as") or "", lambda: st, session)
    if found is None:
        print("no actor. Pass --as <name>, set COMMS_ACTOR, or run inside a Claude Code session.")
        return EXIT_OK
    names = _names(st, found.name)
    held = sum(1 for c in st.claims.values() if c.actor in names)
    print(f"@{found.name}  ({_SOURCE[found.source]})  in {root.name}, holds {held}")
    if session:
        print(f"  session {session[:8]}")
    return EXIT_OK
