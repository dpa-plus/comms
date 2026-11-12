"""Who is calling: one resolver for every verb, and the liveness marker.

Resolution order: ``--as``, ``COMMS_ACTOR``, the session's own actor (the first
one tied to the host agent session in this repo's log), an auto-name derived
from that session, else a one-line error. On the edit path a claim is the
caller's by the agent session recorded on it (see ``owns``), not by name.
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import log as _log

GENERIC_ACTORS = frozenset({"eli", "claude", "codex", "agent", "user", "me"})

_VENDOR_PREFIX = {"": "claude", "anthropic": "claude", "openai": "codex"}

LIVENESS_DIR = "alive"


@dataclass(frozen=True)
class Resolved:
    name: str
    # "flag" | "env" | "session" | "auto"
    source: str


def host_session() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()


def auto_name(st, agent_session: str) -> str:
    """``<vendor>-<first 6>``, lengthened while another session already uses it."""
    vendor = os.environ.get("COMMS_VENDOR", "").strip().lower()
    prefix = _VENDOR_PREFIX.get(vendor, vendor or "claude")
    for length in (6, 8, 10, 12, len(agent_session)):
        name = f"{prefix}-{agent_session[:length]}"
        users = st.agent_sessions_of(name) if st is not None else []
        if all(s == agent_session for s in users):
            return name
    return name


def base_actor(actor: str) -> str:
    """The actor without its role suffix: ``claude-dev/review`` -> ``claude-dev``.

    A leading slash is not a role suffix: ``/alice`` stays ``/alice``, because
    an empty base would make every such name the same agent.
    """
    head = actor.split("/", 1)[0] if "/" in actor else actor
    return head or actor


def is_generic(name: str) -> bool:
    return name.casefold() in GENERIC_ACTORS and \
        os.environ.get("COMMS_ALLOW_GENERIC_ACTOR", "") != "1"


def _clean(name: str) -> str:
    return (name or "").strip().lstrip("@").strip()


def resolve(explicit: str, st_for_session, agent_session: str, *,
            hook: bool = False) -> Resolved | None:
    """``st_for_session`` is called only when the log has to be consulted.

    On the hook path a generic ``COMMS_ACTOR`` is ignored: it would put every
    session on the machine under one name the CLI refuses to write.
    """
    if _clean(explicit):
        return Resolved(_clean(explicit), "flag")
    env = _clean(os.environ.get("COMMS_ACTOR", ""))
    if env and not (hook and is_generic(env)):
        return Resolved(env, "env")
    if not agent_session:
        return None
    st = st_for_session()
    family = st.families.get(agent_session) if st is not None else None
    if family is not None and family.first_actor:
        return Resolved(family.first_actor, "session")
    return Resolved(auto_name(st, agent_session), "auto")


def owns(st, claim, agent_session: str, name: str = "") -> bool:
    """Is ``claim`` the caller's own on the edit path?

    By the agent session recorded on the claim. A claim with none (older logs,
    a CLI call outside a session) is the caller's when a hello ties its holder
    to the caller's session. Without a session of its own, the caller has only
    its name to go by.
    """
    if agent_session:
        if claim.agent_session:
            return claim.agent_session == agent_session
        return claim.actor in st.family_of(agent_session)
    return bool(name) and claim.actor == name


def foreign(st, scope, agent_session: str, name: str = "") -> list:
    """Live claims overlapping ``scope`` that are not the caller's, oldest first."""
    from .scope import overlaps

    return sorted((c for c in st.claims.values()
                   if overlaps(c.scope, scope) and not owns(st, c, agent_session, name)),
                  key=lambda c: c.ts)


def other_names_line(st, agent_session: str, me: str) -> str:
    """One line naming the claims held under this session's other names, or ''.

    Every view counts only ``me``, the name ``release`` frees, so the rest are
    said out loud instead of being counted as yours or as somebody else's.
    """
    names = st.family_of(agent_session) - {me}
    counts: dict[str, int] = {}
    for claim in st.claims.values():
        if claim.actor in names:
            counts[claim.actor] = counts.get(claim.actor, 0) + 1
    if not counts:
        return ""
    held = ", ".join(f"@{n} {k}" for n, k in sorted(counts.items()))
    name = next(iter(counts)) if len(counts) == 1 else "<name>"
    return (f"this session also holds claims under other names: {held} "
            f"(`comms-graph release --as {name}` frees them)")


def hello_data(agent_session: str, model: str = "") -> dict:
    """Key-for-key what ``cli._hello_if_unknown`` and ``hello`` write."""
    data: dict = {"agent_session": agent_session}
    label = os.environ.get("COMMS_LABEL", "").strip()
    if label:
        data["label"] = label
    vendor = os.environ.get("COMMS_VENDOR", "").strip()
    if vendor:
        data["vendor"] = vendor
    model = model or os.environ.get("COMMS_MODEL", "").strip()
    if model:
        data["model"] = model
    try:
        data["hostname"] = socket.gethostname()
    except Exception:
        pass
    return data


def cli_actor(flags: dict, *, write: bool) -> str:
    """The actor for a CLI verb. Exits on failure when the verb writes; else ''."""
    from . import cli as _cli
    from . import lock as _lock

    agent_session = host_session()
    root_and_state: list = []

    def state():
        root = _cli._repo_root(flags.get("root"))
        log_file = _log.log_path(root)
        st = _cli._read_state(log_file) if log_file.exists() else None
        root_and_state[:] = [root, st]
        return st

    found = resolve(flags.get("as") or "", state, agent_session)
    if found is None:
        if not write:
            return ""
        print("error: no actor. Pass --as <name>, set COMMS_ACTOR, or run inside "
              "a Claude Code session.", file=sys.stderr)
        sys.exit(_cli.EXIT_USAGE)
    if write and is_generic(found.name):
        print(f"error: {found.name!r} is too generic to tell two agents apart; pick a "
              "specific name (e.g. claude-auth-fix), or set COMMS_ALLOW_GENERIC_ACTOR=1.",
              file=sys.stderr)
        sys.exit(_cli.EXIT_USAGE)
    if write and found.source == "auto":
        root = root_and_state[0]
        log_file, lock_file = _cli._store(root)
        with _lock.file_lock(lock_file):
            st = _cli._read_state(log_file)
            if found.name not in st.family_of(agent_session):
                _log.append(log_file, _cli._tagged(
                    st, found.name, _log.TYPE_HELLO, None, hello_data(agent_session)))
    return found.name


def marker(store: Path, agent_session: str) -> Path:
    return store / LIVENESS_DIR / agent_session.replace("/", "_")


def touch_marker(store: Path, agent_session: str) -> None:
    """Mtime only, never a log append, so a quiet editor does not look dead."""
    if not agent_session:
        return
    path = marker(store, agent_session)
    try:
        os.utime(path)
    except FileNotFoundError:
        try:
            path.parent.mkdir(exist_ok=True)
            path.touch()
        except OSError:
            pass
    except OSError:
        pass


def last_alive(st, store: Path, actor: str) -> datetime | None:
    """The later of the actor's last event and any of its sessions' markers."""
    best = st.last_seen.get(actor)
    for agent_session in st.agent_sessions_of(actor):
        try:
            mtime = marker(store, agent_session).stat().st_mtime
        except OSError:
            continue
        seen = datetime.fromtimestamp(mtime, timezone.utc)
        if best is None or seen > best:
            best = seen
    return best
