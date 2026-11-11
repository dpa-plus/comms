"""Who is calling: one resolver for every verb, and the liveness marker.

Resolution order: ``--as``, ``COMMS_ACTOR``, the session's own actor (the first
one tied to the host agent session in this repo's log, or the latest ``hello
--as``), an auto-name derived from that session, else a one-line error. "Mine" is a session family (see ``state.SessionFamily``), not
a single name.
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


def auto_name(agent_session: str) -> str:
    vendor = os.environ.get("COMMS_VENDOR", "").strip().lower()
    prefix = _VENDOR_PREFIX.get(vendor, vendor or "claude")
    return f"{prefix}-{agent_session[:6]}"


def is_generic(name: str) -> bool:
    return name.casefold() in GENERIC_ACTORS and \
        os.environ.get("COMMS_ALLOW_GENERIC_ACTOR", "") != "1"


def _clean(name: str) -> str:
    return (name or "").strip().lstrip("@").strip()


def resolve(explicit: str, st_for_session, agent_session: str) -> Resolved | None:
    """``st_for_session`` is called only when the log has to be consulted."""
    if _clean(explicit):
        return Resolved(_clean(explicit), "flag")
    env = _clean(os.environ.get("COMMS_ACTOR", ""))
    if env:
        return Resolved(env, "env")
    if not agent_session:
        return None
    st = st_for_session()
    family = st.families.get(agent_session) if st is not None else None
    if family is not None and family.own_actor:
        return Resolved(family.own_actor, "session")
    return Resolved(auto_name(agent_session), "auto")


def family(st, agent_session: str, actor: str = "") -> frozenset[str]:
    """Every name this caller answers to: its session's actors, its auto-name, ``actor``."""
    names = set(st.family_of(agent_session))
    if agent_session:
        names.add(auto_name(agent_session))
    if actor:
        names.add(actor)
    return frozenset(names)


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
