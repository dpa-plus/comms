"""``comms-graph mcp`` — the coordination protocol as tools the model already has.

WHY A SECOND SURFACE AT ALL. The CLI is only reached when something tells an
agent to reach for it, and the always-on block that does the telling is one
paragraph competing with the whole task. The evidence is in the log: across
thousands of real claims almost none carried a task, because the skill that
explains tasks does not auto-trigger. A command an agent must be *told* to run
loses to a tool sitting in its tool list on every single turn. Same protocol,
same log, same events — different door.

NO CEREMONY. Six verbs that map one-to-one onto commands that already exist. No
registration, no inbox, no severity ladder. Anything an agent must learn before
its first call is ceremony and does not belong here.

NO DEPENDENCY. It speaks JSON-RPC 2.0 over stdin/stdout by hand. The subset of
MCP a tool server needs — initialize, tools/list, tools/call — is small, stable,
and cheaper to own than a dependency is to carry.

TWO RULES THE LOOP MUST NEVER BREAK:

  * A tool-level failure comes back as text with ``isError``, never as a dead
    process. A server that exits on a claim conflict takes the agent's whole
    session down with it, and a conflict is the single most expected outcome
    here.
  * ``comms_check`` is a pure read. It takes no lock and creates nothing — see
    :func:`_check`.

STDOUT IS THE WIRE. Every diagnostic in this module goes to stderr. One stray
``print`` would land between two JSON-RPC frames and desynchronise the client.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

from . import lock as _lock
from . import log as _log
from . import scope as _scope
from .cli import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    _err,
    _event,
    _hello_if_unknown,
    _read_state,
    _repo_root,
    _store,
    _warn_if_ephemeral,
)

#: The MCP revision this server implements. Reported verbatim in `initialize`;
#: clients negotiate against it.
PROTOCOL_VERSION = "2025-06-18"

SERVER_NAME = "comms"
SERVER_VERSION = "1"

USAGE = """Usage: comms-graph mcp

  Serve the coordination verbs as MCP tools on stdin/stdout.

  Point an MCP-capable agent at this and claiming, checking and releasing
  become tools in its list every turn, rather than commands somebody has to
  remember to run. The tools write the same events to the same log as the CLI,
  so a session using the tools and a session using the CLI coordinate with each
  other without either knowing which the other is.

  Every tool takes an "actor" argument, so one server process can act for
  several agents over one connection — which COMMS_ACTOR alone cannot express,
  being per process. COMMS_ACTOR is still the default when it is omitted.

  Takes no arguments. Exit status: 0 clean shutdown, 2 bad usage."""

#: The five categories `find` accepts, in the order the error message lists
#: them. Imported rather than redefined would be better; cli.FINDING_CATEGORIES
#: is a dict whose iteration order is already this, and the enum in the tool
#: schema has to be a list, so this is the one place the order is written down.
FIND_CATEGORIES = ("bug", "fix", "ship", "decision", "gotcha")

#: A finding in one of these outlives the work that produced it, so it goes to
#: the top of the "prior context" list a claim prints. A `bug` from six weeks
#: ago may well be fixed; a `decision` is still the decision.
_DURABLE = frozenset({"decision", "gotcha"})

#: Free-text ceiling, in Unicode scalars rather than bytes. Matches the Go
#: build's MCP limit exactly: the same body typed at either door must be
#: accepted or refused the same way, or agents learn one limit and hit another.
MAX_TEXT_RUNES = 280


# ---------------------------------------------------------------------------
# Failure that is not the model's fault
# ---------------------------------------------------------------------------


class _Failed(Exception):
    """The call could not run at all — an unresolvable actor, an unreadable log.

    Distinct from a refusal. A refusal (conflict, missing argument, bad
    category) is an ANSWER and goes back as tool text the model can act on; this
    is the protocol saying "ask me again when the machine is fixed", and the
    model can do nothing with it but surface it.
    """

    def __init__(self, message: str, code: int = -32603) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _rpc_error(code: int, message: str) -> dict:
    return {"code": code, "message": message}


# ---------------------------------------------------------------------------
# The stdio loop
# ---------------------------------------------------------------------------


def serve(inp, out) -> int:
    """Read newline-delimited JSON-RPC frames until stdin closes."""
    while True:
        try:
            line = inp.readline()
        except (KeyboardInterrupt, EOFError):
            return EXIT_OK
        if not line:
            return EXIT_OK
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            # A frame we cannot parse carries no id, so there is nothing to
            # answer on. Dropping it and reading the next line is the only
            # option that keeps the connection alive; returning here would let
            # one malformed byte end the agent's session.
            continue
        if not isinstance(req, dict):
            continue
        # A NOTIFICATION (no id) GETS NO REPLY, EVER. `notifications/initialized`
        # arrives from every client immediately after the handshake; answering
        # it is a protocol violation some clients treat as fatal and drop the
        # connection over. The test for absence is `"id" not in req` and not a
        # truth test, because id 0 is a legitimate id.
        if "id" not in req:
            continue
        resp: dict = {"jsonrpc": "2.0", "id": req.get("id")}
        try:
            resp["result"] = _dispatch(req)
        except _Failed as exc:
            resp["error"] = _rpc_error(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - see below
            # Nothing a single tool call can do may end the loop. An unexpected
            # exception here used to be unthinkable and then `_read_state` grew
            # a `sys.exit` for a corrupt log, which — inside a server — is one
            # bad line in the log killing every agent connected to it.
            resp["error"] = _rpc_error(-32603, f"{type(exc).__name__}: {exc}")
        try:
            out.write(json.dumps(resp) + "\n")
            out.flush()
        except BrokenPipeError:
            # The client hung up mid-answer. Not an error worth a traceback.
            return EXIT_OK


def _dispatch(req: dict):
    method = req.get("method")
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tools()}
    if method == "tools/call":
        return _call(req.get("params"))
    raise _Failed(f"unknown method {method}", code=-32601)


# ---------------------------------------------------------------------------
# The tool list
# ---------------------------------------------------------------------------


def _str_prop(desc: str) -> dict:
    return {"type": "string", "description": desc}


def tools() -> list[dict]:
    """The six tools, as MCP declares them.

    EVERY WORD HERE IS PAID FOR ON EVERY TURN, FOREVER. A tool list is resident
    context: it is re-sent to the model on each request for the whole life of
    the connection, so a paragraph of prose in a description is a paragraph the
    agent re-reads a thousand times and a paragraph of its budget that the task
    does not get. One sentence per tool, saying WHEN to call it. The schema says
    what the arguments are; repeating that in the description buys nothing.
    """
    actor_prop = _str_prop(
        "the calling agent's actor name, e.g. claude-dev. Defaults to COMMS_ACTOR.")
    return [
        {
            "name": "comms_check",
            "description": "Is anyone else editing this path? Call before editing a file. "
                           "Returns clear, or who holds it and why.",
            "inputSchema": {"type": "object", "required": ["path"], "properties": {
                "actor": actor_prop,
                "path": _str_prop("repo-relative path, optionally #L10-40 or #symbolName"),
            }},
        },
        {
            "name": "comms_claim",
            "description": "Take an exclusive hold on a path before editing it. Refused if "
                           "someone else holds an overlapping scope, and the refusal is recorded.",
            "inputSchema": {"type": "object", "required": ["path", "intent"], "properties": {
                "actor": actor_prop,
                "path": _str_prop("repo-relative path, optionally #L10-40 or #symbolName"),
                "intent": _str_prop("what you are about to do to it, one line"),
                "task": _str_prop("slug of the task this claim carries out, if any"),
            }},
        },
        {
            "name": "comms_release",
            "description": "Release every claim you hold, with a note of what came of it.",
            "inputSchema": {"type": "object", "required": ["result"], "properties": {
                "actor": actor_prop,
                "result": _str_prop("what happened, e.g. 'merged as #321'"),
            }},
        },
        {
            "name": "comms_status",
            "description": "Who is active, what is claimed, what has gone stale, and how many "
                           "collisions have been prevented.",
            "inputSchema": {"type": "object", "properties": {"actor": actor_prop}},
        },
        {
            "name": "comms_note",
            "description": "Leave a short FYI for the other agents on this repo.",
            "inputSchema": {"type": "object", "required": ["body"], "properties": {
                "actor": actor_prop,
                "body": _str_prop("the note"),
            }},
        },
        {
            "name": "comms_find",
            "description": "Record something worth keeping: a decision and its reason, or a trap "
                           "the next agent should not fall into. Anchored to the file you hold, "
                           "so it resurfaces when somebody claims that file.",
            "inputSchema": {"type": "object", "required": ["category", "summary"], "properties": {
                "actor": actor_prop,
                "category": {"type": "string", "enum": list(FIND_CATEGORIES),
                             "description": "bug=open problem, fix=resolved, ship=released, "
                                            "decision=architectural choice, gotcha=persistent trap"},
                "summary": _str_prop("one line, specific enough to act on"),
                "ref": _str_prop("optional anchor, e.g. path:src/auth.ts"),
            }},
        },
    ]


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


def _text(body: str, is_error: bool) -> dict:
    """The content shape MCP expects for a plain-text result.

    ``isError`` marks a TOOL-level failure — the model sees it, reads the text
    and can do something about it. A protocol error is invisible to the model,
    so a refusal must never be one.
    """
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def _arg(args: dict, key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _call(params):
    if not isinstance(params, dict):
        raise _Failed(f"bad params: expected an object, got {type(params).__name__}",
                      code=-32602)
    name = params.get("name")
    args = params.get("arguments")
    if not isinstance(args, dict):
        args = {}
    actor_arg = _arg(args, "actor")

    handler = {
        "comms_check": lambda: _check(actor_arg, _arg(args, "path")),
        "comms_claim": lambda: _claim(actor_arg, _arg(args, "path"), _arg(args, "intent"),
                                      _arg(args, "task")),
        "comms_release": lambda: _release(actor_arg, _arg(args, "result")),
        "comms_status": lambda: _status(actor_arg),
        "comms_note": lambda: _note(actor_arg, _arg(args, "body")),
        "comms_find": lambda: _find(actor_arg, _arg(args, "category"), _arg(args, "summary"),
                                    _arg(args, "ref")),
    }.get(name)
    if handler is None:
        raise _Failed(f"unknown tool {name}", code=-32602)
    try:
        return handler()
    except SystemExit as exc:
        # The CLI helpers this module reuses report fatal conditions by exiting
        # the process — right for a command, lethal for a server, where it would
        # end every other agent's connection over one bad argument. They print
        # the detail to stderr on the way out; turn the exit itself into a
        # protocol error so the caller gets an answer and the loop lives.
        raise _Failed(f"comms could not complete the call (exit {exc.code}); "
                      "see the server's stderr") from exc
    except _lock.LockError as exc:
        raise _Failed(f"could not take the comms lock: {exc}") from exc


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _quote(s: str) -> str:
    """A double-quoted, escaped rendering — Go's %q, near enough to be the same
    string on the wire for anything an agent actually types."""
    return json.dumps(s, ensure_ascii=False)


def _when(ts) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _reject_control_text(field: str, s: str, max_runes: int) -> str:
    """Guard free text that is later replayed into a terminal. Returns the
    complaint, or "" when the text is fine.

    C0, DEL and C1 are refused because status output prints these fields raw: a
    newline forges an output line, an ESC injects a terminal escape sequence,
    and the log stores whatever it is given forever. Legitimate Unicode passes —
    "Café" and "日本語" are not attacks.
    """
    for ch in s:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            return f"{field} must not contain control characters"
    if max_runes > 0 and len(s) > max_runes:
        return f"{field} is too long ({len(s)} characters, max {max_runes})"
    return ""


def _parse_scope(path: str):
    """The scope, or a refusal to hand back to the model.

    Deliberately NOT `cli._rooted_scope`, which resolves against the process cwd
    and exits when the result lands outside the repo. A server's cwd is not the
    agent's, and its exit would be everybody's. The tool's contract already says
    repo-relative, which is the spelling the log stores.
    """
    try:
        return _scope.parse(path), ""
    except Exception as exc:  # ScopeError, and anything the parser grows later
        return None, str(exc)


def _actor_for(actor_arg: str, *, mutating: bool) -> str:
    """Who is calling. The per-call argument wins over COMMS_ACTOR.

    That precedence is the whole reason the argument exists: COMMS_ACTOR is per
    process, and one server process fields calls for several agents over one
    connection. Reading only the environment would file every one of their
    claims under a single name, and two agents sharing a name cannot detect a
    conflict between them — which is the failure comms exists to prevent.
    """
    if actor_arg:
        # Same rule as the CLI: a leading "@" is display, never identity.
        return actor_arg.strip().lstrip("@").strip() or actor_arg
    env = os.environ.get("COMMS_ACTOR", "").strip()
    if env:
        return env
    if not mutating:
        # A read with no identity is still answerable; it just cannot exclude
        # the caller's own claims, so it sees everything. `check` uses this.
        return ""
    raise _Failed('no actor: pass "actor" in the tool arguments, or set COMMS_ACTOR '
                  "for this server. Two agents sharing one name cannot detect a "
                  "conflict between them, so this refuses to guess.")


#: Log paths already warned about. The CLI warns once per command because a
#: command runs once; a server would otherwise repeat the same paragraph on
#: every tool call for the life of the connection, which trains its reader to
#: ignore it.
_EPHEMERAL_WARNED: set[str] = set()


def _warn_ephemeral_once(log_file: Path) -> None:
    key = str(log_file)
    if key in _EPHEMERAL_WARNED:
        return
    _EPHEMERAL_WARNED.add(key)
    _warn_if_ephemeral(log_file)


@contextlib.contextmanager
def _writing(actor_arg: str):
    """Identity, lock and folded state for one mutating call.

    The read-fold-append cycle happens inside the lock, exactly as the CLI does
    it. Folding outside would let two agents both read "no conflict" and both
    append, which is the collision this whole tool exists to prevent.
    """
    actor = _actor_for(actor_arg, mutating=True)
    root = _repo_root(None)
    log_file, lock_file = _store(root)
    _warn_ephemeral_once(log_file)
    with _lock.file_lock(lock_file) as handle:
        yield actor, log_file, handle, _read_state(log_file)


def _append(log_file: Path, st, actor: str, etype: str, scope, data: dict):
    """Append one event, preceded by this actor's hello when it has none.

    The hello is not bookkeeping: the pre-edit hook that ENFORCES claims is a
    separate process with no COMMS_ACTOR of its own, and it identifies its caller
    by matching the host agent's session id against hello events in the log. An
    agent that only ever touches the MCP tools would have written none, so the
    hook would fail to recognise it and block it from editing the file it had
    just claimed — a conflict message naming itself.

    Minted before the event it explains, because `fold` sorts by TIMESTAMP;
    being first in the list is not enough. Appended as one batch so the identity
    record and the event land together or not at all.
    """
    greeting = _hello_if_unknown(st, actor)
    event = _event(actor, etype, scope, data)
    if greeting is None:
        _log.append(log_file, event)
    else:
        _log.append_batch(log_file, [greeting, event])
    return event


def _stamp_session(st, actor: str, data: dict) -> None:
    """Tag the event with the coordination window its actor is in, if any.

    Shared-log detail: `comms session start` is a Go-build verb, so the session
    id can only ever arrive from over there — but once it has, events this build
    writes must carry it too, or half an agent's work falls outside the window
    and the window's own archive undercounts what happened in it.
    """
    session = st.sessions.get(actor)
    if session is None or not getattr(session, "session_id", ""):
        return
    data.setdefault("comms_session_id", session.session_id)
    data.setdefault("comms_session_name", getattr(session, "session_name", ""))


def _findings_on_scopes(st, scopes, limit: int) -> list:
    """Prior findings anchored to a path that overlaps any of these scopes."""
    matches = []
    for finding in st.findings:
        for ref in finding.refs:
            if getattr(ref, "kind", "") != "path":
                continue
            parsed, _bad = _parse_scope(getattr(ref, "value", ""))
            if parsed is None:
                continue
            if any(_scope.overlaps(parsed, sc) for sc in scopes):
                matches.append(finding)
                break
    # Two stable passes rather than one composite key: durable first, newest
    # within each group, and no arithmetic on timestamps that a year-1 event
    # could make throw.
    matches.sort(key=lambda f: f.ts, reverse=True)
    matches.sort(key=lambda f: 0 if f.category in _DURABLE else 1)
    return matches[:limit] if limit > 0 else matches


# ---------------------------------------------------------------------------
# comms_check — the one that runs constantly
# ---------------------------------------------------------------------------


def _check(actor_arg: str, path: str):
    """Read-only, and it must stay that way.

    NO LOCK: this is the tool an agent calls before every edit. Queuing it behind
    a claim that is mid-write would put a coordination pause in front of every
    file the agent touches, and it reads a log it cannot make wrong by reading.

    NO MKDIR: `_store(create=False)` on purpose. The store was created here once,
    and because the pre-edit hook turns any failure into a block, a single
    unwritable data home then denied every Edit and Write in EVERY repository on
    the machine — including repos with no claims and no log at all.
    """
    if not path:
        return _text("path is required", True)
    scope, bad = _parse_scope(path)
    if scope is None:
        return _text(bad, True)
    actor = _actor_for(actor_arg, mutating=False)
    root = _repo_root(None)
    log_file, _lock_file = _store(root, create=False)
    st = _read_state(log_file)

    conflicts = st.conflicts_for(scope, actor)
    if not conflicts:
        return _text(f"clear: nobody else holds {scope}", False)
    holder = conflicts[0]
    return _text(
        f"BLOCKED: {scope} is held by @{holder.actor} "
        f"(intent: {_quote(holder.intent)}, since {_when(holder.ts)}). "
        "Do not edit it. Pick another file, or ask them in a note.",
        True,
    )


# ---------------------------------------------------------------------------
# comms_claim
# ---------------------------------------------------------------------------


def _claim(actor_arg: str, path: str, intent: str, task: str):
    if not path or not intent:
        return _text("path and intent are both required", True)
    scope, bad = _parse_scope(path)
    if scope is None:
        return _text(bad, True)

    with _writing(actor_arg) as (actor, log_file, handle, st):
        conflicts = st.conflicts_for(scope, actor)
        if conflicts:
            holder = conflicts[0]
            _record_blocked(log_file, st, actor, str(scope), intent, holder)
            return _text(
                f"REFUSED: {scope} is already held by @{holder.actor} "
                f"(intent: {_quote(holder.intent)}). Nothing was claimed.",
                True,
            )
        # The far end of the critical section. Everything above concluded the
        # ground was free, and that conclusion is worth something only if we
        # still hold the lock we reached it under: a lock file deleted mid-hold
        # leaves us flocked to an orphan inode while somebody else flocks a
        # fresh one at the same path, and both of us then record a claim on the
        # same scope. Refusing costs the agent one retry; recording a second
        # holder costs two agents editing the same file at once.
        broken = handle.compromised()
        if broken:
            return _text(f"the coordination lock is no longer exclusive: {broken}. "
                         "Nothing was claimed; try again.", True)
        data: dict = {"intent": intent}
        if task:
            data["task"] = task
        _stamp_session(st, actor, data)
        event = _append(log_file, st, actor, _log.TYPE_CLAIM, [str(scope)], data)
        prior = _findings_on_scopes(st, [scope], 3)

    out = f"claimed {scope} as @{actor} (id {event.id})"
    if prior:
        # The reason findings are anchored to a path at all: this is the moment
        # somebody is about to touch that file, and the only moment a note left
        # six weeks ago has any chance of being read.
        out += "\n\nprior context on this path:"
        for finding in prior:
            out += f"\n  [{finding.category}] {finding.summary} (@{finding.actor})"
    return _text(out, False)


def _record_blocked(log_file: Path, st, actor: str, scope_str: str, intent: str, holder) -> None:
    """Write the refusal down before answering it.

    This is the only event in comms that is evidence the tool did its job: a
    collision that did not happen. Until it was recorded, a refused claim printed
    a message and vanished, which is how a store holding thousands of claims
    could honestly report having prevented nothing.

    Best effort by design. The refusal is already decided; failing to record it
    must never turn a correct block into a crash.
    """
    data = {
        "scope": scope_str,
        "holder": holder.actor,
        "holder_scope": str(holder.scope),
    }
    if intent:
        data["intent"] = intent
    _stamp_session(st, actor, data)
    try:
        _append(log_file, st, actor, _log.TYPE_BLOCKED, [scope_str], data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# comms_release
# ---------------------------------------------------------------------------


def _release(actor_arg: str, result: str):
    if not result:
        return _text("result is required — say what came of the work", True)
    with _writing(actor_arg) as (actor, log_file, _handle, st):
        held = st.active_claims_by_actor(actor)
        if not held:
            # Not an error. An agent that tidies up after itself unconditionally
            # is doing the right thing, and telling it off for holding nothing
            # teaches it to stop.
            return _text("you hold no claims", False)
        scopes = [str(c.scope) for c in held]
        data = {"result": result, "refs": [c.id for c in held]}
        _stamp_session(st, actor, data)
        _append(log_file, st, actor, _log.TYPE_RELEASE, scopes, data)
    return _text(f"released {len(scopes)} claim(s): " + ", ".join(scopes), False)


# ---------------------------------------------------------------------------
# comms_status
# ---------------------------------------------------------------------------


def _status(actor_arg: str):
    # actor_arg is accepted and unused: status is the same board for everybody,
    # and a tool whose schema differs from its siblings for no reason is one the
    # model has to think about. Read-only, so no lock and no mkdir.
    del actor_arg
    root = _repo_root(None)
    log_file, _lock_file = _store(root, create=False)
    st = _read_state(log_file)

    lines = [f"{len(st.claims)} active claim(s), {len(st.sessions)} agent(s) seen"]
    for claim in sorted(st.claims.values(), key=lambda c: c.ts):
        lines.append(f"  {claim.scope} — @{claim.actor} ({claim.intent})")
    if st.blocked:
        lines.append(f"collisions prevented: {len(st.blocked)}")
    return _text("\n".join(lines), False)


# ---------------------------------------------------------------------------
# comms_note
# ---------------------------------------------------------------------------


def _note(actor_arg: str, body: str):
    if not body:
        return _text("body is required", True)
    complaint = _reject_control_text("note", body, MAX_TEXT_RUNES)
    if complaint:
        return _text(complaint, True)
    with _writing(actor_arg) as (actor, log_file, _handle, st):
        data = {"body": body}
        _stamp_session(st, actor, data)
        _append(log_file, st, actor, _log.TYPE_NOTE, None, data)
    return _text("noted", False)


# ---------------------------------------------------------------------------
# comms_find
# ---------------------------------------------------------------------------


def _parse_ref(raw: str):
    """One ``kind:value`` pair, or a complaint.

    Kind and value, never a bare string, because a bare string cannot say which
    of the two it is and that is the useful half. Control characters are refused
    on the same grounds as every other stored field — a ref value must not be
    able to smuggle escape bytes into output that prints it raw.
    """
    kind, sep, value = raw.partition(":")
    if not sep or not kind or not value:
        return None, f"ref {_quote(raw)}: expected kind:value"
    if _reject_control_text("ref", kind + value, 0):
        return None, f"ref {_quote(raw)}: contains control character"
    return {"kind": kind, "value": value}, ""


def _find(actor_arg: str, category: str, summary: str, ref: str):
    if category not in FIND_CATEGORIES:
        return _text("category must be one of " + ", ".join(FIND_CATEGORIES), True)
    if not summary:
        return _text("summary is required", True)
    complaint = _reject_control_text("finding summary", summary, MAX_TEXT_RUNES)
    if complaint:
        return _text(complaint, True)

    refs: list[dict] = []
    if ref:
        parsed, bad = _parse_ref(ref)
        if parsed is None:
            return _text(bad, True)
        refs.append(parsed)

    with _writing(actor_arg) as (actor, log_file, _handle, st):
        # ANCHORING IS NOT OPTIONAL. A finding with no path ref is never read
        # again: nothing surfaces it, because the only thing that surfaces one
        # is somebody claiming the file it is about. When the caller did not say
        # which file, the files it currently holds are the honest answer.
        if not any(r["kind"] == "path" for r in refs):
            for claim in st.active_claims_by_actor(actor):
                refs.append({"kind": "path", "value": claim.scope.path})
        data = {"category": category, "summary": summary, "refs": refs}
        _stamp_session(st, actor, data)
        _append(log_file, st, actor, _log.TYPE_FINDING, None, data)
    return _text(f"recorded [{category}] {summary}", False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    """``argv`` is everything after ``comms-graph mcp``."""
    if any(a in ("-h", "--help", "-?") for a in argv):
        print(USAGE)
        return EXIT_OK
    if argv:
        _err(f"error: mcp takes no arguments (got {' '.join(argv)!r})")
        _err(USAGE)
        return EXIT_USAGE
    try:
        return serve(sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        return EXIT_OK
    except OSError as exc:
        _err(f"error: the MCP connection failed: {exc}")
        return EXIT_ERROR
