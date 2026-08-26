"""``comms-graph session``: named coordination windows.

Port of comms' ``internal/subcmd/session.go``. A session is a NAME two or more
agents agree to work under: "auth-refactor", "ad-dashboard-fixes". Everything
they do while it is open is tagged with it, so a reader can later ask what that
piece of work cost, who was on it, and what it left behind, and so ending it
can sweep up exactly the ground it took and nothing else.

THE MECHANIC, WHICH IS NOT AN EVENT TYPE. There is no ``session`` event. A
session exists because the actor's most recent ``hello`` carries
``comms_session_id`` / ``comms_session_name``, and because every event that
actor writes afterwards repeats those two keys in its ``data``. The fold in
``state.py`` already reads them everywhere. Claim, Note, Finding, Blocked,
Release and the ended-session archive all have the pair, so this module only
has to WRITE them: the hello here, and :func:`stamp` for every other verb.

Not its own event type on purpose: a session that were one would need the fold
to join two streams (which window was open when this claim landed?) and would
be wrong the moment the two disagreed. Carrying the tag on the event makes the
window a property of the fact, which is a thing that cannot drift.

FILE-COMPATIBLE WITH THE GO BUILD. Both builds read one log, so the keys here
are copied from real events the Go build wrote, not invented: 8,698 events on
this machine carry the pair across 17 named sessions. An extra key is dropped
silently by the other reader and is therefore safe; a DIFFERENT key for the same
fact is a session the other build cannot see.

EXIT CODES. 0 recorded, 2 bad usage. 1 is used where the Go build returns a
plain error and exits 2: "that name is already active", "no session called
that", "@x is not active". Those are refusals: the command worked, read the
log and the answer is no, nothing was written, and this build reserves 1 for
exactly that, the same way ``claim`` reports a conflict. A wrapper that only
tests for zero sees no difference.
"""

from __future__ import annotations

import os
import socket
import sys
from datetime import datetime, timedelta, timezone

from . import lock as _lock
from . import log as _log
from .cli import (
    EXIT_CONFLICT,
    EXIT_OK,
    EXIT_USAGE,
    _actor,
    _err,
    _event,
    _parse_flags,
    _read_state,
    _repo_root,
    _store,
    _warn_if_ephemeral,
)

USAGE = """Usage: comms-graph session <command>

  start "<name>" [--label "..."]     create a named session and join it
  join "<name>" [--label "..."]      join one somebody else started
  end "<name>" [--reason "..."]      close it and release its claims
  retire <actor> [--reason "..."]    take an actor off the roster and free
                                     whatever it still holds
  lead [<actor>] [--reason "..."]    hand leadership to one active actor

  A session is a name a group of agents works under. Every event written while
  it is open carries the name, so `end` can free exactly that work's ground and
  a reader can tell one piece of work from another months later.

  Options:
    --as <actor>     who you are. Required (or set COMMS_ACTOR).
    --label "..."    display name for this actor on the board (start/join)
    --reason "..."   why, in the log, under your name, permanently
    --root <path>    repo root (default: the git root above the cwd)

  Exit status: 0 recorded, 1 the answer is no, 2 bad usage."""

#: The one coordination-recency window, matching the Go build's `activeWindow`.
#: An actor, and therefore a named session, which is only alive while somebody
#: in it is: counts as active if it has been SEEN inside this span. Seen means
#: any event at all, not the hello: an agent that greeted six hours ago and has
#: been claiming ever since is working, and judging it on the one-shot greeting
#: would drop a live session off the board mid-flight.
ACTIVE_WINDOW = timedelta(hours=4)

#: Caps the --label display string, as the Go build does. A pathological label
#: is rendered raw by the board and by `status`.
_MAX_LABEL = 120

#: Same cap for a session name. The Go build prints names through %q, which
#: escapes control characters for free; this one prints them raw, so the guard
#: that Go gets from its formatter has to be explicit here or a name containing
#: ESC or a newline can forge output lines.
_MAX_NAME = 120


# ---------------------------------------------------------------------------
# Reading the roster
# ---------------------------------------------------------------------------


def _last_seen(sess) -> datetime:
    """The passive heartbeat, falling back to the hello for a hand-built session."""
    return sess.last_seen or sess.ts


def _active_sessions(st, cutoff: datetime) -> list:
    """Actors seen since `cutoff`, most recently active first.

    Ties break on actor name, and only because ``sessions`` is a dict: without
    the second key the leader of a group that all greeted in the same second
    would be whoever happened to be first in the map, which is not the same
    answer twice.
    """
    out = [s for s in st.sessions.values() if _last_seen(s) > cutoff]
    out.sort(key=lambda s: s.actor)
    out.sort(key=_last_seen, reverse=True)
    return out


def _active_leader(st, cutoff: datetime) -> str:
    """Who leads right now, or "" when nobody is active.

    An explicit leader flag wins; otherwise it is the actor who has been here
    longest (earliest hello), which is the rule that makes leadership stable:
    electing the most recent arrival would move it on every new agent.

    Reads without marking. The Go original writes the answer back onto the
    Session structs; doing that here would mutate the caller's folded state,
    and the fold is the one thing in this package that is meant to be pure.
    """
    sessions = _active_sessions(st, cutoff)
    if not sessions:
        return ""
    for sess in sessions:
        if sess.leader:
            return sess.actor
    leader = sessions[0]
    for sess in sessions:
        if sess.ts < leader.ts:
            leader = sess
    return leader.actor


def _session_by_name(st, name: str, cutoff: datetime) -> tuple[str, str]:
    """Find an active named session by name, case-blind. ("", "") when there is none.

    Falls back to the CLAIMS when no live actor carries the name. An agent can
    hold ground under a session and then go quiet past the window: its claims
    are still tagged, and `end` still has to be able to find and free them.
    Without the second pass the ground taken by a crashed session would be
    unreachable by name and could only be released one claim id at a time.

    Returns the CANONICAL spelling as well as the id, so joining "Auth-API"
    records the name the session was started with rather than a second spelling
    of it that later reads as a different piece of work.
    """
    name = name.strip()
    if not name:
        return "", ""
    for sess in _active_sessions(st, cutoff):
        if sess.session_id and sess.session_name.lower() == name.lower():
            return sess.session_id, sess.session_name
    for claim in sorted(st.claims.values(), key=lambda c: c.ts):
        if claim.session_id and claim.session_name.lower() == name.lower():
            return claim.session_id, claim.session_name
    return "", ""


def _claims_in_session(st, session_id: str) -> list:
    """Every active claim taken under `session_id`, oldest first."""
    if not session_id:
        return []
    return sorted(
        (c for c in st.claims.values() if c.session_id == session_id),
        key=lambda c: c.ts,
    )


def stamp(st, actor: str, data: dict) -> None:
    """Tag an outgoing event with the actor's named session, if it has one.

    This is the whole writing side of the mechanic, and it belongs on EVERY
    verb that appends: claim, release, find, note, task. An event that skips it
    is invisible to the session: `end` will not release its claim, the archive
    will not count it, and a reader asking what that piece of work produced
    gets an answer that is quietly short.

    A caller that has already set ``comms_session_id`` is left alone: the
    session-lifecycle events below name the session they are ACTING ON, which
    is not always the one the writer is in: retiring somebody out of another
    window is the case that gets this wrong if the stamp overwrites.
    """
    if st is None or data is None:
        return
    if "comms_session_id" in data:
        return
    sess = st.sessions.get(actor)
    if sess is None or not sess.session_id:
        return
    data["comms_session_id"] = sess.session_id
    data["comms_session_name"] = sess.session_name


# ---------------------------------------------------------------------------
# Small shared bits
# ---------------------------------------------------------------------------


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def _base_name(actor: str) -> str:
    """`claude-3a1f` -> `claude`, `human-eli` -> `human`, `alice` -> `alice`."""
    i = actor.find("-")
    return actor[:i] if i > 0 else actor


def _control_text_error(what: str, value: str, cap: int | None) -> str | None:
    """Refuse text that could forge a line of output. None when it is fine.

    C0 (below 0x20, so newline, carriage return and ESC), DEL, and the C1 range
    0x80-0x9f. These land in the log and are then printed raw by the board, by
    `status` and by this command; a name containing ESC can repaint the screen
    and a name containing a newline can invent a second agent on the roster.

    `cap` is None for a --reason. A name and a label are identifiers that get a
    column somewhere, so a length limit is the difference between a roster and a
    wall of text; a reason is prose, and refusing a long one where the Go build
    accepts it would reject a sentence somebody meant to leave in the log.
    """
    for ch in value:
        point = ord(ch)
        if point < 0x20 or point == 0x7F or 0x80 <= point <= 0x9F:
            return f"{what} contains a control character (U+{point:04X})"
    if cap is not None and len(value) > cap:
        return f"{what} is longer than {cap} characters"
    return None


def _checked(what: str, value: str, cap: int | None) -> str | None:
    """`value`, or None after reporting why it was refused."""
    problem = _control_text_error(what, value, cap)
    if problem is None:
        return value
    _err(f"error: {problem}")
    return None


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return ""


def _tty() -> str:
    """The controlling terminal, best effort.

    The Go build shells out to `tty(1)`; asking the descriptor directly is the
    same answer without a process, and returns "" in exactly the same case:
    stdin is not a terminal, which is true for every agent that is not a person.
    """
    try:
        return os.ttyname(sys.stdin.fileno())
    except Exception:
        return ""


def _unknown_flags(flags: dict, allowed: set[str]) -> str | None:
    """The first flag this verb does not take, or None."""
    for key in flags:
        if key not in allowed:
            return key
    return None


def _ev(actor: str, etype: str, data: dict, ts: datetime) -> _log.Event:
    """An event at a CHOSEN instant, for the two places ordering is load-bearing."""
    return _log.Event(ts=ts, id=_log.new_id(ts), actor=actor, type=etype,
                      scope=None, data=data)


# ---------------------------------------------------------------------------
# Switching sessions
# ---------------------------------------------------------------------------


def _release_before_switch(st, actor: str, target_id: str, target_name: str,
                           release_at: datetime) -> _log.Event | None:
    """The audit event that takes an actor out of the session it is leaving.

    None when there is nothing to leave. Otherwise a release that does two
    things at once, and both matter:

    * It frees the claims the actor holds under the OLD session. Ground taken
      for one piece of work must not silently follow the agent into the next
      one: `end` on the old session would no longer find it (its holder has
      moved), so it would sit there held by somebody who has stopped thinking
      about it until it went stale.
    * It carries ``session_retire``, which the fold reads as "drop this actor
      from the roster". The hello written a millisecond later puts it straight
      back, in the new session. Without the retire the actor would be listed
      under whichever of the two the reader looked at first.

    Claims already tagged with the session being JOINED are kept: re-joining a
    window you are already in must not throw away your own work.
    """
    refs = [c.id for c in st.active_claims_by_actor(actor) if c.session_id != target_id]
    current = st.sessions.get(actor)
    stale_session = (
        current is not None and current.session_id and current.session_id != target_id
    )
    if not refs and not stale_session:
        return None
    data: dict = {
        "refs": refs,
        "session_retire": True,
        "retired_actor": actor,
        "reason": f'actor moved to comms session "{target_name}"',
    }
    if stale_session:
        # The session being LEFT, not the one being joined. This event belongs
        # to the old window's story: it is where that ground went.
        data["comms_session_id"] = current.session_id
        data["comms_session_name"] = current.session_name
    return _ev(actor, _log.TYPE_RELEASE, data, release_at)


def _session_hello(st, actor: str, session_id: str, session_name: str,
                   label: str, start: bool, ts: datetime) -> _log.Event:
    """The hello that IS the session: it carries the id every later event repeats."""
    leader = _active_leader(st, ts - ACTIVE_WINDOW)
    data: dict = {
        "base_name": _base_name(actor),
        "hostname": _hostname(),
        "tty": _tty(),
        # An empty room has no leader to defer to, so the first one in leads.
        "leader": leader == "" or leader == actor,
        "comms_session_id": session_id,
        "comms_session_name": session_name,
    }
    if start:
        data["comms_session_start"] = True
        # The Go build repeats the name as the reason so the roster's "why" column
        # says something for a session's opening event. Same key, same value.
        data["reason"] = session_name
    else:
        data["comms_session_join"] = True
    if label:
        data["label"] = label

    # WHAT THIS PREVENTS: an agent that starts a session and can then no longer
    # edit the files it just claimed.
    #
    # The pre-edit hook has no COMMS_ACTOR of its own; it identifies the caller
    # by matching the host's session id against `agent_session` on the hello
    # (see _hook_actor in cli.py). This hello REPLACES the actor's previous one
    # in the fold: only the most recent survives, so leaving the pairing out
    # would erase it, and every claim in the repo, the caller's own included,
    # would come back as somebody else's.
    #
    # It is also what keeps the session alive. `_hello_if_unknown` writes a
    # fresh hello whenever the recorded pairing does not match, and that hello
    # carries no session at all: without this key the very next `claim` would
    # write one and silently drop the actor out of the session it just started.
    agent_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if agent_session:
        data["agent_session"] = agent_session
    # Model identity, on the same terms as `_hello_if_unknown`: absent when
    # unset rather than guessed. Carried here because this hello supersedes the
    # one that had it, and dropping it downgrades every later verification from
    # "independent" to "unknown": a weaker claim than the truth.
    vendor = os.environ.get("COMMS_VENDOR", "").strip()
    if vendor:
        data["vendor"] = vendor
    model = os.environ.get("COMMS_MODEL", "").strip()
    if model:
        data["model"] = model
    return _ev(actor, _log.TYPE_HELLO, data, ts)


def _enter(argv: list[str], start: bool) -> int:
    """`session start` and `session join`: the same write with a different gate."""
    verb = "start" if start else "join"
    positional, flags = _parse_flags(argv)
    unknown = _unknown_flags(flags, {"as", "root", "label"})
    if unknown:
        _err(f"error: session {verb}: unknown flag --{unknown}")
        _err(USAGE)
        return EXIT_USAGE
    name = (positional[0] if positional else "").strip()
    if not name:
        _err(f"error: session {verb} needs a name, e.g. "
             f'comms-graph session {verb} "auth-refactor"')
        return EXIT_USAGE
    if len(positional) > 1:
        # One name, quoted. Silently keeping the first word would open a session
        # under a name nobody else will type.
        _err(f"error: session {verb} takes one name, but {len(positional)} words were given: "
             + ", ".join(repr(p) for p in positional))
        _err(f'  Quote it: comms-graph session {verb} "{" ".join(positional)}"')
        return EXIT_USAGE
    if _checked("the session name", name, _MAX_NAME) is None:
        return EXIT_USAGE
    label = (flags.get("label") or os.environ.get("COMMS_LABEL") or "").strip()
    if label and _checked("--label", label, _MAX_LABEL) is None:
        return EXIT_USAGE
    actor = _actor(flags)
    root = _repo_root(flags.get("root"))
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)

    with _lock.file_lock(lock_file):
        st = _read_state(log_file)
        now = datetime.now(timezone.utc)
        existing_id, canonical = _session_by_name(st, name, now - ACTIVE_WINDOW)
        if start and existing_id:
            _err(f'error: session start: "{canonical}" is already active')
            _err(f'  Join it instead: comms-graph session join "{canonical}"')
            _err("  Starting a second one under the same name would split the group")
            _err("  in two without either half being told.")
            return EXIT_CONFLICT
        if not start and not existing_id:
            _err(f'error: session join: no active comms session named "{name}"')
            _err(f'  Create it: comms-graph session start "{name}"')
            return EXIT_CONFLICT

        # A millisecond apart, and that is the whole point. The fold sorts by
        # TIMESTAMP, so the release that ends the old session has to be earlier
        # IN TIME than the hello that begins the new one: appending it first is
        # not enough. Sharing an instant would let the two fold in either order,
        # and in the wrong one the retire pops the session the hello just made,
        # leaving an actor that has joined nothing.
        hello_at = now + timedelta(milliseconds=1)
        session_id = existing_id or _log.new_id(hello_at)
        session_name = canonical or name

        leaving = _release_before_switch(st, actor, session_id, session_name, now)
        if leaving is not None:
            # Appended on its own, then re-read, exactly as the Go build does:
            # the hello's leader flag is computed against the roster AFTER this
            # actor has been taken off it, which is the roster the rest of the
            # room will see. Written first so that a crash between the two
            # leaves the ground FREE rather than held under a session its holder
            # has already walked out of.
            _log.append(log_file, leaving)
            st = _read_state(log_file)
        _log.append(log_file, _session_hello(
            st, actor, session_id, session_name, label, start, hello_at))

    if start:
        print(f'@{actor} started and joined comms session "{session_name}".')
    else:
        print(f'@{actor} joined comms session "{session_name}".')
    print(f"  Session ID: {session_id}")
    if leaving is not None and leaving.data["refs"]:
        freed = len(leaving.data["refs"])
        print(f"  Released {freed} claim{_plural(freed)} held in the session you left.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Ending, retiring, leading
# ---------------------------------------------------------------------------


def _end(argv: list[str]) -> int:
    """`session end "<name>"`: close a window and free everything taken in it."""
    positional, flags = _parse_flags(argv)
    unknown = _unknown_flags(flags, {"as", "root", "reason"})
    if unknown:
        _err(f"error: session end: unknown flag --{unknown}")
        _err(USAGE)
        return EXIT_USAGE
    name = (positional[0] if positional else "").strip()
    if not name:
        _err('error: session end needs a name, e.g. comms-graph session end "auth-refactor"')
        return EXIT_USAGE
    reason = (flags.get("reason") or "").strip() or "named comms session ended"
    if _checked("--reason", reason, None) is None:
        return EXIT_USAGE
    actor = _actor(flags)
    root = _repo_root(flags.get("root"))
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)

    with _lock.file_lock(lock_file):
        st = _read_state(log_file)
        session_id, canonical = _session_by_name(
            st, name, datetime.now(timezone.utc) - ACTIVE_WINDOW)
        if not session_id:
            _err(f'error: session end: no active comms session named "{name}"')
            _err("  `comms-graph board` lists what is actually held here.")
            return EXIT_CONFLICT
        refs = [c.id for c in _claims_in_session(st, session_id)]
        # Named, never blank. A release event with no comms_session_id is the
        # GLOBAL end, and the fold reads that one as "archive everything and
        # start over": it drops every claim and every actor in the repo,
        # including the four working in the session next door.
        _log.append(log_file, _event(actor, _log.TYPE_RELEASE, None, {
            "refs": refs,
            "comms_session_end": True,
            "comms_session_id": session_id,
            "comms_session_name": canonical,
            "reason": reason,
        }))

    print(f'Ended comms session "{canonical}"; released {len(refs)} claim{_plural(len(refs))}.')
    return EXIT_OK


def _session_metadata_for_retire(st, target: str, claims: list) -> tuple[str, str]:
    """Which session a retire belongs to: the actor's own, or its claims' if they agree.

    Mixed claims give ("", ""): tagging the event with one of two windows would
    put a whole actor's disappearance into the story of a session that only
    owned half of its work.
    """
    sess = st.sessions.get(target)
    if sess is not None and sess.session_id:
        return sess.session_id, sess.session_name
    session_id = session_name = ""
    for claim in claims:
        if not claim.session_id:
            continue
        if not session_id:
            session_id, session_name = claim.session_id, claim.session_name
            continue
        if claim.session_id != session_id:
            return "", ""
    return session_id, session_name


def _retire(argv: list[str]) -> int:
    """`session retire <actor>`: take somebody off the roster and free their ground.

    The verb for an agent that is not coming back. It appends; it does not edit
    the log, and it does not pretend the actor was never here. `release --force`
    is the one-claim version of this, and this is the whole-actor one.
    """
    positional, flags = _parse_flags(argv)
    unknown = _unknown_flags(flags, {"as", "root", "reason"})
    if unknown:
        _err(f"error: session retire: unknown flag --{unknown}")
        _err(USAGE)
        return EXIT_USAGE
    target = (positional[0] if positional else "").strip()
    if not target:
        _err("error: session retire needs an actor, e.g. comms-graph session retire claude-dev")
        return EXIT_USAGE
    reason = (flags.get("reason") or "").strip() or "retired from active sessions"
    if _checked("--reason", reason, None) is None:
        return EXIT_USAGE
    actor = _actor(flags)
    root = _repo_root(flags.get("root"))
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)

    with _lock.file_lock(lock_file):
        st = _read_state(log_file)
        claims = st.active_claims_by_actor(target)
        if st.sessions.get(target) is None and not claims:
            # Nothing to do is not the same as done. Reporting success here
            # would let a typo'd actor name read as a completed cleanup, and the
            # real holder would still be holding.
            _err(f"error: session retire: @{target} has no active session or claims")
            _err("  `comms-graph board` lists who actually holds what.")
            return EXIT_CONFLICT
        data: dict = {
            "refs": [c.id for c in claims],
            "session_retire": True,
            "retired_actor": target,
            "reason": reason,
        }
        session_id, session_name = _session_metadata_for_retire(st, target, claims)
        if session_id:
            data["comms_session_id"] = session_id
            data["comms_session_name"] = session_name
        _log.append(log_file, _event(actor, _log.TYPE_RELEASE, None, data))

    freed = len(claims)
    print(f"Retired @{target} from active sessions; released {freed} claim{_plural(freed)}. "
          "History remains in the append-only log.")
    return EXIT_OK


def _lead(argv: list[str]) -> int:
    """`session lead [<actor>]`: move leadership to one active actor.

    The leader's only extra privilege is posting priority notes and findings, so
    this is a small decision; it is a command rather than an election because
    the alternative: inferring it from who greeted first: cannot be corrected
    when the first one to greet is the one who has gone quiet.
    """
    positional, flags = _parse_flags(argv)
    unknown = _unknown_flags(flags, {"as", "root", "reason"})
    if unknown:
        _err(f"error: session lead: unknown flag --{unknown}")
        _err(USAGE)
        return EXIT_USAGE
    if len(positional) > 1:
        _err("error: session lead takes at most one actor")
        return EXIT_USAGE
    reason = (flags.get("reason") or "").strip() or "leader transfer"
    if _checked("--reason", reason, None) is None:
        return EXIT_USAGE
    actor = _actor(flags)
    target = (positional[0] if positional else "").strip() or actor
    root = _repo_root(flags.get("root"))
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)

    with _lock.file_lock(lock_file):
        st = _read_state(log_file)
        sess = st.sessions.get(target)
        # Judged on the passive heartbeat, not the hello, and the difference is
        # not academic: an agent that greeted this morning and has been claiming
        # all afternoon is shown active by every other reader in this build, and
        # gating on the one-shot greeting would refuse to hand leadership to
        # somebody the board is calling the most active actor in the repo.
        cutoff = datetime.now(timezone.utc) - ACTIVE_WINDOW
        if sess is None or not _last_seen(sess) > cutoff:
            _err(f"error: session lead: @{target} is not active")
            _err(f'  Have them run: COMMS_ACTOR={target} comms-graph session join "<name>"')
            _err("  Leadership on an actor nobody can hear from is a priority channel")
            _err("  with nobody on the other end.")
            return EXIT_CONFLICT
        data: dict = {
            "leader_transfer": True,
            "leader_actor": target,
            "reason": reason,
        }
        if sess.session_id:
            data["comms_session_id"] = sess.session_id
            data["comms_session_name"] = sess.session_name
        _log.append(log_file, _event(actor, _log.TYPE_RELEASE, None, data))

    print(f"@{target} is now the comms leader.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    """``argv`` is everything after ``session``."""
    if not argv:
        print(USAGE)
        return EXIT_USAGE
    if any(a in ("-h", "--help", "-?", "help") for a in argv):
        # Asking how a verb works must never write anything, and for a tool
        # whose users are agents, it is the whole discovery path.
        print(USAGE)
        return EXIT_OK
    sub, rest = argv[0], argv[1:]
    if sub == "start":
        return _enter(rest, True)
    if sub == "join":
        return _enter(rest, False)
    if sub == "end":
        return _end(rest)
    if sub == "retire":
        return _retire(rest)
    if sub == "lead":
        return _lead(rest)
    _err(f"error: unknown session command {sub!r}")
    _err(USAGE)
    return EXIT_USAGE
