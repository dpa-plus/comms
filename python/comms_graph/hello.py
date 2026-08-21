"""``hello``, ``version`` and ``help`` — the three verbs that answer, not act.

Ported from the Go comms (``internal/subcmd/hello.go``, ``version.go``). They
live in one module because none of them is big enough to be its own file and
all three are things an agent runs BEFORE it has any state to coordinate: who
am I, what am I running, what can I type.

``hello`` is the one with a side effect, and the side effect is the point. The
pre-edit hook that enforces claims is a separate process with no ``COMMS_ACTOR``
of its own — it learns who is asking by looking the host agent's session id up
against the ``hello`` events in the log. A session with no hello on record is a
session the hook cannot recognise, so its own claims come back as somebody
else's and it gets blocked from editing the file it just claimed.

This build already mints a hello implicitly on an actor's first action (see
``cli._hello_if_unknown``), so nothing here is load-bearing for an agent that
just starts claiming. What the explicit verb adds is the two cases the implicit
one cannot cover: saying hello BEFORE doing anything (so a human can check the
actor name is right while it is still cheap to fix), and changing the display
label of an actor that already said hello — which the implicit path skips
precisely because it only fires when the pairing is unknown.

THE PAYLOAD IS THE SAME PAYLOAD. Both paths write the same keys with the same
spellings, so a reader cannot tell an explicit hello from an implicit one and
does not have to care. See :func:`_identity_data`; the test suite pins the two
against each other, because a key that drifts here is a key the hook, the review
gate and the independence label all stop seeing.
"""

from __future__ import annotations

import os
import platform
import socket
import sys
import unicodedata

from . import cli as _cli
from . import lock as _lock
from . import log as _log

# Imported as a MODULE, not `from .cli import _actor, ...`. cli is the dispatcher
# that will call into here, so the two import each other; binding the module
# object works mid-import (the partially-built module is already in sys.modules)
# while pulling names out of it at import time would raise ImportError depending
# on which file Python happened to start with.


# ---------------------------------------------------------------------------
# hello
# ---------------------------------------------------------------------------

#: Caps the --label display string. The label is rendered raw next to an actor
#: name on the board and in the UI, so a pathological value would flood the one
#: line a reader uses to tell two agents apart.
_MAX_LABEL_CHARS = 120

#: Shorter cap for --model / --vendor: these are identifiers, not prose, and the
#: independence label prints them inline.
_MAX_IDENT_CHARS = 60


def _reject_control_text(what: str, value: str, limit: int) -> str | None:
    """Refuse text that could forge an output line. Returns the complaint, or None.

    Every value this guards is printed raw into human output, so a newline in a
    label writes a second line that looks like ours and an ESC repaints the
    terminal — a display name is exactly where an agent would hide "@someone
    else holds nothing". Unicode category Cc is the whole control range at once:
    C0 including newline and ESC, DEL, and C1 (U+0080-U+009F), which is the half
    that gets forgotten when this is written as a `< 0x20` test.
    """
    if len(value) > limit:
        return f"{what} is {len(value)} characters; keep it under {limit}"
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        return f"{what} contains a control character; it is printed raw, so it is refused"
    return None


def _base_name(actor: str) -> str:
    """Everything before the first ``-``: ``claude-3a1f`` -> ``claude``.

    Groups the sessions of one kind of agent so hello can say how many of them
    are already running. A leading dash is not a separator (Go indexes for `> 0`
    and so does this), or ``-foo`` would report itself as the family ``-foo``.
    """
    i = actor.find("-")
    return actor[:i] if i > 0 else actor


def _identity_data(label: str, model: str, vendor: str) -> dict:
    """The hello payload. Must stay key-for-key what ``cli._hello_if_unknown`` writes.

    Two writers, one record: if this spelled the host session ``session_id``
    while the implicit path spelled it ``agent_session``, the hook would resolve
    exactly half the agents on the machine and silently treat the other half as
    strangers. Unknown keys are dropped by the reader, so an EXTRA key costs
    nothing; a different key for the same fact costs everything.

    Absent, not guessed, is the rule for every optional value here.
    ``independence_of`` reports "unknown" for a missing vendor, and that is the
    honest answer — inferring anthropic from a name that starts with "claude"
    would manufacture the evidence that a verification was independent.
    """
    data: dict = {}
    agent_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if agent_session:
        data["agent_session"] = agent_session
    if label:
        data["label"] = label
    if model:
        data["model"] = model
    if vendor:
        data["vendor"] = vendor
    try:
        data["hostname"] = socket.gethostname()
    except Exception:
        # A machine that will not name itself is not a reason to refuse an
        # identity record; the hostname is for a human reading the board.
        pass
    return data


def cmd_hello(argv: list[str]) -> int:
    positional, flags = _cli._parse_flags(argv)
    if len(positional) > 1:
        _cli._err(f"error: hello takes at most one name, got {len(positional)}")
        _cli._err('  e.g. comms-graph hello claude-dev --label "Claude Dev"')
        return _cli.EXIT_USAGE

    # A positional name overrides $COMMS_ACTOR for this one command, which is
    # how the Go build lets you check "would I be the right actor?" without
    # exporting anything. Go implements that by setting the env var; this does
    # not, because the CLI is importable and a verb that mutates the process
    # environment would silently rename the actor of every later call in the
    # same process — the server and the test suite both make several.
    # An empty name is no name: it falls through to --as / $COMMS_ACTOR, and to
    # the refusal there, rather than registering an actor called "".
    named = positional[0].strip() if positional else ""
    actor = named or _cli._actor(flags)

    label = (flags.get("label") or os.environ.get("COMMS_LABEL", "")).strip()
    model = (flags.get("model") or os.environ.get("COMMS_MODEL", "")).strip()
    # Lowercased so `Anthropic` and `anthropic` are one vendor. independence_of
    # compares these strings, and two spellings of one vendor would read as two
    # different vendors — i.e. as independent verification that is not.
    vendor = (flags.get("vendor") or os.environ.get("COMMS_VENDOR", "")).strip().lower()
    for what, value, limit in (
        ("--label", label, _MAX_LABEL_CHARS),
        ("--model", model, _MAX_IDENT_CHARS),
        ("--vendor", vendor, _MAX_IDENT_CHARS),
    ):
        complaint = _reject_control_text(what, value, limit)
        if complaint is not None:
            _cli._err(f"error: {complaint}")
            return _cli.EXIT_USAGE

    root, log_file, lock_file = _cli._task_runtime(flags)
    with _lock.file_lock(lock_file):
        st = _cli._read_state(log_file)
        # Unconditional, unlike the implicit path: somebody typing `hello` is
        # asking to be registered NOW, and a re-run that changes only --label
        # would otherwise print success and record nothing.
        _log.append(log_file, _cli._event(actor, _log.TYPE_HELLO, None,
                                          _identity_data(label, model, vendor)))
        base = _base_name(actor)
        # Counted as a SET of actor names that includes this one, not as a
        # number of hello events: an agent that says hello twice is still one
        # session, and the state is read before this hello lands, so counting
        # rows would have reported "1 claude session" to the second claude —
        # the exact collision this line exists to warn about.
        family = {name for name in st.sessions if _base_name(name) == base}
        family.add(actor)
        siblings = len(family)

    # FIRST LINE: the actor name, so it is still visible when everything below
    # scrolls away. Getting this wrong is the failure hello exists to catch.
    print(f"@{actor} registered.")
    if label:
        print(f"  Label:   {label}")
    print(f"  Project: {root.name}  (hash: {_log.repo_hash(root)})")
    print(f"  Log:     {log_file}")
    print(f"  ({siblings} {base} session{'' if siblings == 1 else 's'} active right now.)")
    if not os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip():
        # Without a host session id the pre-edit hook has nothing to match this
        # actor against, so it cannot tell this agent's own claims from a
        # stranger's. Said out loud because the symptom otherwise arrives much
        # later as "blocked from editing the file I just claimed".
        print()
        print("  NOTE: no CLAUDE_CODE_SESSION_ID in this environment, so the pre-edit")
        print("  hook cannot tie this actor to a process. Claims are still recorded;")
        print("  enforcement just cannot recognise you.")
    print()
    print("If this is the wrong actor name, set $COMMS_ACTOR and re-run `comms-graph hello`.")
    print('Use `comms-graph hello --label "Claude Dev"` to change only the display name.')
    return _cli.EXIT_OK


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

#: Distribution names to ask, in order. The wheel is still published as
#: `graphifyy` while the fork is called comms-graph, so asking for only one of
#: them would print "dev" for real installs on one side of the rename or the
#: other. Both are asked; the first that answers wins.
_DIST_NAMES = ("comms-graph", "graphifyy")


def _version() -> str:
    """The installed version, or ``dev`` when there is no installed dist.

    Running from a source checkout is normal here and is not an error, so it
    reports the same marker the Go build reports for a binary built without
    version stamping rather than raising or printing "unknown".
    """
    from importlib.metadata import PackageNotFoundError, version as _dist_version

    for name in _DIST_NAMES:
        try:
            found = _dist_version(name)
        except PackageNotFoundError:
            continue
        if found:
            return found
    return "dev"


def cmd_version(argv: list[str]) -> int:
    positional, _flags = _cli._parse_flags(argv)
    if positional:
        _cli._err(f"error: version takes no arguments, got {positional[0]!r}")
        return _cli.EXIT_USAGE
    # One line, same shape as the Go build's: name, version, then the runtime
    # facts a bug report needs. Version stays the second field in both builds so
    # a script that cut it out of `comms version` still works against this one.
    print(f"comms-graph {_version()} (python {platform.python_version()}, "
          f"{platform.system().lower()}/{platform.machine()})")
    return _cli.EXIT_OK


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------


def cmd_help(argv: list[str]) -> int:
    # Deliberately prints cli.USAGE rather than a copy of it: two copies of the
    # usage text means one of them is out of date, and for a tool whose users
    # are agents the usage text IS the discovery path.
    print(_cli.USAGE)
    return _cli.EXIT_OK


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

_VERBS = {"hello": cmd_hello, "version": cmd_version, "help": cmd_help}


def main(argv: list[str]) -> int:
    """Dispatch one of ``hello`` / ``version`` / ``help``.

    ``argv[0]`` is the verb — this module holds three of them, so unlike a
    single-verb module it has to be told which one was typed. A caller that has
    already dispatched can skip this and call ``cmd_hello`` and friends directly.
    """
    verb = argv[0] if argv else ""
    handler = _VERBS.get(verb)
    if handler is None:
        _cli._err(f"error: unknown comms command {verb!r}")
        _cli._err(_cli.USAGE)
        return _cli.EXIT_USAGE
    # Asking how a verb works must never have a side effect — `hello --help`
    # printing usage instead of writing an identity record is the whole point.
    if any(a in ("-h", "--help", "-?") for a in argv[1:]):
        print(_cli.USAGE)
        return _cli.EXIT_OK
    return handler(argv[1:])


if __name__ == "__main__":  # pragma: no cover - parity with the other modules
    sys.exit(main(sys.argv[1:]))
