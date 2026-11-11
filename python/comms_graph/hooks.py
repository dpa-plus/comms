"""``hook session-start | pre-edit | session-end``: Claude Code's hook JSON on stdin.

Exit 2 from ``pre-edit`` is the only code that stops an edit. The other two
hooks always exit 0: a briefing or a cleanup that fails must not stand in the
way of a session.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import identity as _identity
from . import lock as _lock
from . import log as _log
from . import scope as _scope

EXIT_OK = 0
EXIT_BLOCK = 2

AUTO_INTENT = "auto: edited"
CLEAN_RELEASE_REASON = "auto-claim: file is clean in git"
SAME_SESSION_REASON = "auto-claim: superseded by a claim from the same session"
SESSION_END_RESULT = "session ended"
SILENT_AFTER = timedelta(hours=1)
BRIEFING_CAP = 900
FOLDER_WINDOW = timedelta(days=14)
FOLDER_REPOS_SHOWN = 8
COMMAND_LINES = (
    '  comms-graph claim <path> --intent "..."   take several files before a change',
    '  comms-graph release --result "..."        give everything back when done',
    '  comms-graph note "@who ..."               tell another agent something',
)
_HOOK_LOCK_TIMEOUT = 1.0

USAGE = """Usage: comms-graph hook session-start | pre-edit | session-end

  Each reads Claude Code's hook JSON on stdin.
  session-start   prints a short briefing when this repo uses comms
  pre-edit        exit 2 blocks an edit of a file somebody else holds; the
                  first edit of a free file claims it for this session
  session-end     releases the session's automatic claims"""


@dataclass(frozen=True)
class HookPayload:
    session_id: str = ""
    cwd: str = ""
    path: str = ""
    agent_id: str = ""
    agent_type: str = ""
    model: str = ""


def parse_payload(raw: str) -> HookPayload:
    if not raw.strip():
        raise ValueError("empty stdin: expected Claude Code's hook JSON")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    tool_input = payload.get("tool_input")
    path = ""
    if isinstance(tool_input, dict):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    model = payload.get("model")
    if isinstance(model, dict):
        model = model.get("id") or model.get("display_name") or ""

    def text(key: str) -> str:
        value = payload.get(key)
        return value.strip() if isinstance(value, str) else ""

    return HookPayload(
        session_id=text("session_id"), cwd=text("cwd"), path=path,
        agent_id=text("agent_id"), agent_type=text("agent_type"),
        model=model.strip() if isinstance(model, str) else "",
    )


def main(argv: list[str]) -> int:
    verb = argv[0] if argv else ""
    if verb == "pre-edit":
        from . import cli as _cli
        return _cli._cmd_check(["--stdin-json", *argv[1:]])
    if verb == "session-start":
        return _never_raise(session_start)
    if verb == "session-end":
        return _never_raise(session_end)
    print(USAGE, file=sys.stderr if verb else sys.stdout)
    return EXIT_OK if verb in ("", "-h", "--help") else EXIT_BLOCK


def _never_raise(fn) -> int:
    try:
        fn(parse_payload(sys.stdin.read()))
    except BaseException:  # noqa: BLE001: a session hook must never get in the way
        pass
    return EXIT_OK


# ---------------------------------------------------------------------------
# pre-edit
# ---------------------------------------------------------------------------


def check_edit(target: Path, *, agent_session: str, explicit: str,
               explicit_root: str | None, payload: HookPayload | None) -> int:
    """Would an edit of ``target`` collide? With a payload, also auto-claim it."""
    from . import cli as _cli

    if not target.is_absolute():
        target = Path.cwd() / target
    root = (Path(explicit_root).expanduser().resolve() if explicit_root
            else _cli._repo_root_for_file(target))
    if root is None:
        return EXIT_OK
    try:
        resolved_target = target.resolve()
        resolved_root = root.resolve()
        resolved_target.relative_to(resolved_root)
    except (ValueError, OSError):
        return EXIT_OK
    rel_text = _cli._canonical_relpath(resolved_target, resolved_root)

    log_file = _log.log_path(root)
    try:
        log_file.stat()
    except FileNotFoundError:
        return EXIT_OK
    except OSError as exc:
        _cli._err(f"check: cannot reach the coordination log: {exc.strerror or exc}")
        _cli._err("  comms could not establish that this path is free, so it is not "
                  "saying that it is.")
        return EXIT_BLOCK
    try:
        st = _cli._read_state(log_file)
    except Exception as exc:
        _cli._err(f"check: cannot read the coordination log: {exc}")
        return EXIT_BLOCK

    store = log_file.parent
    _identity.touch_marker(store, agent_session)
    found = _identity.resolve(explicit, lambda: st, agent_session, hook=payload is not None)
    me = found.name if found is not None else ""
    scope = _scope.parse(rel_text)
    conflicts = _blocking(st, scope, rel_text, root, agent_session, me)
    if conflicts:
        conflicts = _yield_clean_auto_claims_locked(root, log_file, conflicts, me,
                                                    agent_session)
    if conflicts:
        _cli._err(render_block(st, store, root, rel_text, conflicts[0],
                               identified=bool(agent_session or me)))
        return EXIT_BLOCK
    if payload is not None and agent_session and me:
        if not _held(st, scope, agent_session):
            _auto_claim(root, log_file, scope, rel_text, me, agent_session, payload)
    return EXIT_OK


def _held(st, scope, agent_session: str) -> bool:
    return any(_identity.owns(st, c, agent_session) and _scope.overlaps(c.scope, scope)
               for c in st.claims.values())


def _blocking(st, scope, rel_text: str, root: Path, agent_session: str, me: str) -> list:
    from . import cli as _cli

    conflicts = _identity.foreign(st, scope, agent_session, me)
    if not conflicts:
        twin = _cli._uncreated_twin(
            st, rel_text, root, lambda c: _identity.owns(st, c, agent_session, me))
        if twin is not None:
            conflicts = [twin]
    return conflicts


def _auto_claim(root: Path, log_file: Path, scope, rel_text: str, actor: str,
                agent_session: str, payload: HookPayload) -> None:
    """The only write on the edit path. Any failure leaves the edit allowed and silent."""
    from . import cli as _cli

    try:
        with _lock.file_lock(log_file.parent / ".lock", timeout=_HOOK_LOCK_TIMEOUT) as handle:
            st = _cli._read_state(log_file)
            if _blocking(st, scope, rel_text, root, agent_session, actor):
                return
            if _held(st, scope, agent_session):
                return
            if handle.compromised():
                return
            batch = []
            if actor not in st.family_of(agent_session):
                batch.append(_cli._tagged(st, actor, _log.TYPE_HELLO, None,
                                          _identity.hello_data(agent_session)))
            data: dict = {"intent": AUTO_INTENT, "auto": True,
                          "agent_session": agent_session}
            if payload.agent_id:
                data["agent_id"] = payload.agent_id
            if payload.agent_type:
                data["agent_type"] = payload.agent_type
            batch.append(_cli._tagged(st, actor, _log.TYPE_CLAIM, [str(scope)], data))
            _log.append_batch(log_file, batch)
    except BaseException:  # noqa: BLE001: a convenience write never blocks an edit
        return


def _clean_in_git(root: Path, path: str) -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    try:
        tracked = subprocess.run([git, "ls-files", "--error-unmatch", "--", path],
                                 cwd=root, capture_output=True, timeout=5)
        if tracked.returncode != 0:
            return False
        status = subprocess.run([git, "status", "--porcelain", "--", path],
                                cwd=root, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return status.returncode == 0 and not status.stdout.strip()


def give_way(st, root: Path, conflicts: list, actor: str,
             agent_session: str = "") -> tuple[list, list]:
    """The conflicts that still stand, and the releases of auto-claims that give way.

    An auto-claim gives way when it belongs to the caller's own agent session,
    or when its file has nothing uncommitted. Writes nothing.
    """
    from . import cli as _cli

    remaining, releases = [], []
    for claim in conflicts:
        reason = ""
        if claim.auto and claim.id in st.claims:
            if agent_session and _identity.owns(st, claim, agent_session):
                reason = SAME_SESSION_REASON
            elif _clean_in_git(root, str(claim.scope).split("#", 1)[0]):
                reason = CLEAN_RELEASE_REASON
        if not reason:
            remaining.append(claim)
            continue
        writer = actor or claim.actor
        releases.append(_cli._tagged(st, writer, _log.TYPE_RELEASE, None, {
            "refs": [claim.id],
            "result": reason,
            "original_actor": claim.actor,
            "arbitrator": writer,
        }))
    return remaining, releases


def _yield_clean_auto_claims_locked(root: Path, log_file: Path, conflicts: list,
                                    actor: str, agent_session: str) -> list:
    from . import cli as _cli

    if not any(c.auto for c in conflicts):
        return conflicts
    try:
        with _lock.file_lock(log_file.parent / ".lock", timeout=_HOOK_LOCK_TIMEOUT):
            st = _cli._read_state(log_file)
            current = [st.claims[c.id] for c in conflicts if c.id in st.claims]
            remaining, releases = give_way(st, root, current, actor, agent_session)
            if releases:
                _log.append_batch(log_file, releases)
            return remaining
    except Exception:
        return conflicts


def _age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def render_block(st, store: Path, root: Path, rel_text: str, holder, *,
                 identified: bool) -> str:
    when = holder.ts.isoformat().replace("+00:00", "Z")
    lines = [
        f"BLOCKED: {rel_text} is claimed.",
        f"  Holder:  @{holder.actor}",
        f"  Claim:   {holder.id}",
    ]
    if holder.intent:
        lines.append(f'  Intent:  "{holder.intent}"')
    lines.append(f"  Since:   {when}")
    if not identified:
        lines.append("  comms could not work out who you are, so it cannot tell whether "
                     "this claim is yours.")
        lines.append("  If it is: set COMMS_ACTOR, or pass --as <actor>.")
        return "\n".join(lines)
    alive = _identity.last_alive(st, store, holder.actor) or holder.ts
    silent = datetime.now(timezone.utc) - alive
    if silent >= SILENT_AFTER:
        lines.append(f"  @{holder.actor} has been silent for {_age(silent)}. "
                     "If they are gone, take it over:")
        lines.append(
            f"    comms-graph claim {shlex.quote(str(root / rel_text))} "
            f"--root {shlex.quote(str(root))} --steal {holder.id} "
            f"--reason {shlex.quote(f'holder silent {_age(silent)}')}")
    else:
        lines.append(f'  Leave them a note (comms-graph note "@{holder.actor} ..."), '
                     "or claim something narrower.")
        lines.append("  Do not edit ground you were refused.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# session-start / session-end
# ---------------------------------------------------------------------------


def _root_of(payload: HookPayload) -> Path | None:
    from . import cli as _cli

    start = Path(payload.cwd).expanduser() if payload.cwd else Path.cwd()
    return _cli._repo_root_for_file(start / ".")


def session_start(payload: HookPayload) -> None:
    from . import cli as _cli

    root = _root_of(payload)
    if root is None:
        text = folder_briefing(payload)
        if text:
            print(text)
        return
    if not payload.session_id:
        return
    log_file = _log.log_path(root)
    if not log_file.is_file():
        return
    store = log_file.parent
    with _lock.file_lock(store / ".lock", timeout=_HOOK_LOCK_TIMEOUT):
        st = _cli._read_state(log_file)
        found = _identity.resolve("", lambda: st, payload.session_id)
        actor = found.name
        known = st.sessions.get(actor)
        tied = actor in st.family_of(payload.session_id)
        model_changed = bool(payload.model) and (known is None or known.model != payload.model)
        if not tied or model_changed:
            _log.append(log_file, _cli._tagged(
                st, actor, _log.TYPE_HELLO, None,
                _identity.hello_data(payload.session_id, payload.model)))
    _identity.touch_marker(store, payload.session_id)
    print(briefing(st, store, root, actor, payload.session_id))


def folder_briefing(payload: HookPayload) -> str:
    """For a session started above its repositories: which of them are in use.

    Silent when none had an event lately. Nothing is written: identity is
    recorded on the first edit or call inside a repository.
    """
    from . import cli as _cli

    try:
        cwd = Path(payload.cwd).expanduser().resolve() if payload.cwd else Path.cwd().resolve()
    except OSError:
        return ""
    cutoff = datetime.now(timezone.utc).timestamp() - FOLDER_WINDOW.total_seconds()
    found = []
    for entry in _log.known_stores():
        root = entry.get("root") or ""
        if not entry.get("exists") or entry["modified"] < cutoff:
            continue
        try:
            Path(root).resolve().relative_to(cwd)
        except (ValueError, OSError):
            continue
        found.append(entry)
    if not found:
        return ""
    lines = [f"comms: {len(found)} repositor{'y' if len(found) == 1 else 'ies'} under "
             f"{cwd.name or str(cwd)} use{'s' if len(found) == 1 else ''} it. "
             "Your first edit or call inside one identifies you there."]
    for entry in found[:FOLDER_REPOS_SHOWN]:
        try:
            st = _cli._read_state(Path(entry["dir"]) / _log.LOG_FILENAME)
            held = sum(1 for c in st.claims.values()
                       if not _identity.owns(st, c, payload.session_id))
        except BaseException:  # noqa: BLE001
            continue
        lines.append(f"  {entry['name'] or Path(entry['root']).name}  "
                     f"{held} file{'' if held == 1 else 's'} held by others")
    if len(found) > FOLDER_REPOS_SHOWN:
        lines.append(f"  ... {len(found) - FOLDER_REPOS_SHOWN} more")
    lines.extend(COMMAND_LINES)
    return _cap(lines)


def _cap(lines: list[str]) -> str:
    out = ""
    for line in lines:
        if len(out) + len(line) + 1 > BRIEFING_CAP:
            break
        out += line + "\n"
    return out.rstrip("\n")


def briefing(st, store: Path, root: Path, actor: str, agent_session: str) -> str:
    from . import cli as _cli

    now = datetime.now(timezone.utc)
    names = st.family_of(agent_session) | {actor}
    lines = [
        f"comms: you are @{actor} in {root.name}. Your first edit of a file claims it "
        "for this session; a file someone else holds blocks the edit.",
        *COMMAND_LINES,
    ]
    others = [c for c in sorted(st.claims.values(), key=lambda c: c.ts, reverse=True)
              if not _identity.owns(st, c, agent_session, actor)]
    if others:
        lines.append(f"Held by others ({len(others)}):")
        for claim in others[:5]:
            alive = _identity.last_alive(st, store, claim.actor) or claim.ts
            stale = "  STALE" if now - alive >= SILENT_AFTER else ""
            lines.append(f"  {claim.scope}  @{claim.actor}  {_age(now - claim.ts)}{stale}")
    addressed = [n for n in st.notes
                 if n.actor not in names and any(_cli._mentions(n.body, a) for a in names)
                 and now - n.ts < timedelta(hours=24)]
    if addressed:
        lines.append("Notes to you:")
        for note in addressed[-3:]:
            lines.append(f"  @{note.actor}: {note.body[:100]}")
    return _cap(lines)


def session_end(payload: HookPayload) -> None:
    from . import cli as _cli

    root = _root_of(payload)
    if root is None or not payload.session_id:
        return
    log_file = _log.log_path(root)
    if not log_file.is_file():
        return
    with _lock.file_lock(log_file.parent / ".lock", timeout=_HOOK_LOCK_TIMEOUT):
        st = _cli._read_state(log_file)
        by_actor: dict[str, list[str]] = {}
        for claim in st.auto_claims_of(payload.session_id):
            by_actor.setdefault(claim.actor, []).append(claim.id)
        events = [_cli._tagged(st, actor, _log.TYPE_RELEASE, None,
                               {"refs": refs, "result": SESSION_END_RESULT})
                  for actor, refs in sorted(by_actor.items())]
        if events:
            _log.append_batch(log_file, events)
