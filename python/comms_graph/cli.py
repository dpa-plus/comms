"""``comms-graph`` — the surface an agent actually types.

This module is the discoverability half of comms. The always-on instruction
block (``graphify/always_on/*.md``) tells an agent to claim before editing; this
is the command that block names. Everything below is presentation and process
plumbing: the coordination decisions live in ``state.py`` (what conflicts),
``resolve.py`` (where a claim lands on the map) and ``contact.py`` (who is near).

THE ONE RULE THE OUTPUT MUST NEVER BREAK. There are exactly two kinds of reply
and they are not the same kind of thing:

  * A CLAIM CONFLICT is exact. Somebody else holds an overlapping scope; that is
    read straight out of the log, not inferred from anything. It BLOCKS: nothing
    is recorded and the exit status is non-zero.
  * A CONTACT WARNING is advisory. It is derived from the map, which is measured
    to flag more pairs than really change together and to miss between a third
    and a half of the ones that do. It never blocks, never sets a non-zero exit
    status, and is never phrased as a verdict. See ``docs/COMMS.md``.

Mixing those two would destroy the value of both: an advisory that blocks gets
switched off, and a hard conflict buried in advice gets skimmed past.

A THIRD REPLY THAT IS NOT SILENCE. A claim whose name resolves to nothing on the
map is reported loudly, with the reason. It must never render as "no
connections" — a mistyped symbol and an genuinely isolated one look identical
otherwise, and in testing a quarter of names typed from memory named something
that does not exist. The claim is still recorded: the log is about paths, and a
path is true whether or not the map has indexed it.

NO ORDERING ANYWHERE. Deriving "who goes first" from the map was tested and was
right about as often as a coin flip. It is not printed, not hinted at, not
computed.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import subprocess
import unicodedata
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import contact as _contact
from . import lock as _lock
from . import log as _log
from . import task as _task
from . import taskview as _taskview
from . import resolve as _resolve
from . import scope as _scope
from . import state as _state

USAGE = """Usage: comms-graph <command>

  claim <scope> --as <actor> [--intent "..."] [--task <id>]
                                                take ground before you edit it
        ... --steal <claim-id> --reason "..."    take it off somebody who is
                                                not coming back
  release <scope|claim-id> --as <actor> [--result "..."]   give it back
        ... <claim-id> --force --reason "..."     free ground somebody else
                                                 abandoned
  board [--as <actor>]                          who holds what right now
  check <path> | check --stdin-json             would an edit here collide?

  task add|edge|done|review ...                 declare and move work
  plan --from <file.json>                       a whole plan, atomically
  next [--as <actor>]                           what you could start now
  brief <task-id>                               what it is, and what came before
  find <category> "..." [--ref kind:value]      something worth keeping
  note "..."                                    a short FYI
  tasks [--out <file.html>]                     draw the task graph
  ui [--port 7878]                              live board: both graphs, live claims

  <scope> is a path, optionally anchored:
      src/foo.py              the whole file
      src/foo.py#parse        one symbol
      src/foo.py#L20-48       a line range

  Options:
    --as <actor>     who you are. Required (or set COMMS_ACTOR); two agents
                     sharing one name cannot detect a conflict between them.
    --intent "..."   one line on what you are about to do
    --result "..."   one line on how it went (release only)
    --root <path>    repo root (default: the git root above the cwd)
    --graph <path>   graph.json (default: <root>/graphify-out/graph.json)

  Exit status: 0 recorded, 1 blocked by somebody else's claim, 2 bad usage."""

# Exit codes. 1 is reserved for "somebody else holds this" so a wrapper script
# can tell a real conflict from a typo without parsing text.
EXIT_OK = 0
EXIT_CONFLICT = 1
EXIT_USAGE = 2
# Same code as a usage error — 2 is "this did not work", as distinct from 1's
# "it worked and the answer is no". Named separately because a compromised lock
# is not the user mistyping something, and a reader of the call site should not
# have to think it was.
EXIT_ERROR = 2


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _repo_root(explicit: str | None) -> Path:
    """The key the store is hashed by, so every agent in one checkout agrees.

    The git root, not the cwd: two agents run from different subdirectories of
    the same repository all the time, and keying on the cwd would silently give
    them two separate logs that cannot see each other's claims — coordination
    that reports success while coordinating nothing.
    """
    if explicit:
        chosen = Path(explicit).expanduser().resolve()
        if not chosen.is_dir():
            # Creating a ledger at a path that does not exist is how a typo
            # becomes a private board: the command succeeds, the claim is
            # recorded somewhere nobody else looks, and the agent is told it
            # holds ground on a log no peer will ever read.
            _err(f"error: --root {explicit!r} is not a directory")
            sys.exit(EXIT_USAGE)
        return chosen
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    # No git root above us. Falling back to the cwd is the behaviour this
    # function's own docstring warns about, so it must not happen quietly: run
    # from the top of such a directory and from a subdirectory of it and you get
    # two different store keys, two separate logs, and both agents told the
    # ground is free. Nothing in the instructions says a git repo is required,
    # and the commonest places to hit this — a scratch directory, an unpacked
    # tarball, a freshly scaffolded project — are exactly where somebody is
    # likeliest to be running two agents at once.
    print(
        f"comms: warning: no git repository above {here}, so this directory is being "
        "used as the coordination root.\n"
        "  Agents run from a subdirectory will key a DIFFERENT log and will not see "
        "these claims.\n"
        "  Run `git init` here, or pass --root to name one root for everybody.",
        file=sys.stderr,
    )
    return here


def _graph_path(root: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    try:
        from graphify.paths import GRAPHIFY_OUT as out
    except Exception:
        out = "graphify-out"
    name = Path(out)
    return (name if name.is_absolute() else root / name) / "graph.json"


def _load_graph(path: Path):
    """The map, as an UNDIRECTED view, or None when there is no usable map.

    Undirected on purpose: ``contact.py`` walks neighbours in both directions,
    because direction is exactly what the abandoned ordering experiment needed
    and exactly what failed to predict anything. Handing it a DiGraph would
    silently drop every inbound edge and under-report contact.

    A missing or unreadable map is not an error here. It costs the advisory
    half of the reply, which is then said out loud; the exact half — the claim
    conflict — does not depend on the map at all.
    """
    if not path.is_file():
        return None
    try:
        from graphify.affected import load_graph

        graph = load_graph(path)
    except Exception:
        return None
    try:
        return graph.to_undirected(as_view=True)
    except Exception:
        return graph


def _parse_flags(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Positional args and ``--flag value`` / ``--flag=value`` pairs."""
    positional: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            if "=" in arg:
                key, value = arg[2:].split("=", 1)
                flags[key] = value
                i += 1
            else:
                key = arg[2:]
                value = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else ""
                flags[key] = value
                i += 2 if value else 1
        else:
            positional.append(arg)
            i += 1
    return positional, flags


def _actor(flags: dict[str, str]) -> str:
    name = (flags.get("as") or os.environ.get("COMMS_ACTOR") or "").strip()
    if not name:
        _err(
            "error: no actor. Pass --as <name> or set COMMS_ACTOR.\n"
            "  Two agents sharing one name cannot detect a conflict between them,\n"
            "  so this refuses to guess."
        )
        sys.exit(EXIT_USAGE)
    return name


def _store(root: Path, *, create: bool = True) -> tuple[Path, Path]:
    """The log and lock paths for a repo. Only creates the store when writing.

    `check` is a pure read and must not mkdir. It did, and because the hook
    wrapper turns any exception into a block, one unwritable data home — a
    read-only HOME, a file where the store directory should be — denied every
    Edit and Write in EVERY repository on the machine, including repos with no
    claims and no log at all. Failing closed is right when we cannot read the
    log; it is not right when we were never going to read one.
    """
    store = _log.store_dir(root)
    if create:
        try:
            store.mkdir(parents=True, exist_ok=True)
            # Leave the repo's name next to its log. The store is keyed by a
            # hash of the path, so without this a store is a 12-hex directory
            # and nothing on the machine can say which project it belongs to —
            # the Go build writes it, this one did not, and a board listing
            # every project on the machine would have shown ours as UNNAMED.
            _log.write_repo_path(store, root)
        except OSError as exc:
            # A raw traceback here exited 1 — the code the usage text reserves
            # for "somebody else holds this". A wrapper trusting the exit code
            # read an unreachable store as a live conflict, and one that retries
            # on conflict looped. Say what is wrong and use the code that means
            # "this did not work".
            _err(f"error: cannot use the coordination store at {store}: "
                 f"{exc.strerror or exc}")
            _err("  comms keeps its log there. Fix or remove whatever is in the way.")
            sys.exit(EXIT_ERROR)
    return _log.log_path(root), store / ".lock"


def _warn_if_ephemeral(log_file: Path) -> None:
    """A store under a temp root is a private log nobody else will ever read."""
    try:
        if _log.is_ephemeral_store(log_file):
            _err(
                f"warning: the comms store is under a temporary directory ({log_file.parent}).\n"
                "  That usually means HOME is overridden. Other agents write to a different\n"
                "  log and will not see these claims."
            )
    except Exception:
        pass


def _read_state(log_file: Path) -> _state.State:
    try:
        events = _log.read(log_file)
    except _log.CorruptLogError as exc:
        _err(f"error: the comms log is unreadable: {exc}")
        _err(f"  It is at {log_file}. Fix or move it; comms will not guess past it.")
        sys.exit(EXIT_USAGE)
    except FileNotFoundError:
        events = []
    return _state.fold(events)


def _event(actor: str, etype: str, scope: list[str] | None, data: dict) -> _log.Event:
    return _log.Event(
        ts=datetime.now(timezone.utc),
        id=_log.new_id(),
        actor=actor,
        type=etype,
        scope=scope,
        data=data,
    )


#: macOS fcntl command that asks an open descriptor for its own path, spelled the
#: way the filesystem spells it. Absent elsewhere, which the caller handles.
_F_GETPATH = 50


def _disk_spelling(target: Path) -> str | None:
    """The path as the FILESYSTEM spells it, in one syscall, or None.

    Listing each directory to find the real spelling costs O(entries) per
    component, and this runs in front of every agent tool call: measured at
    21.7ms per call in a 50,000-entry node_modules, per component. F_GETPATH
    answers the same question for the whole path at once, in 0.013ms.

    Opened with O_NONBLOCK because a FIFO opened for reading BLOCKS until a
    writer appears — a hook that hangs is worse than one that answers wrongly,
    since nothing times it out. O_CLOEXEC so a descriptor cannot leak into a
    child. Any failure returns None and the caller falls back.
    """
    if not hasattr(fcntl, "fcntl"):
        return None
    try:
        fd = os.open(str(target), os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        raw = fcntl.fcntl(fd, _F_GETPATH, b"\0" * 1024)
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    text = os.fsdecode(raw.split(b"\0", 1)[0])
    return text or None


def _canonical_relpath(target: Path, root: Path) -> str:
    """A repo-relative path spelled the way the FILESYSTEM spells it.

    THE BYPASS THIS CLOSES. Overlap between claims is a string comparison, which
    is right on a case-sensitive filesystem and wrong on the one most of this
    runs on. On macOS "src/a.py" and "src/A.py" are ONE file, so an agent could
    hold one spelling while another edited the other. Unicode is the same defect:
    macOS stores filenames decomposed while a claim is typed composed.

    Three earlier attempts each fixed one case and broke another, so the rule is
    written out in full:

    * Ask the filesystem for the spelling of the DEEPEST PART THAT EXISTS, and
      append whatever does not exist yet exactly as typed. An earlier version
      bailed out entirely when the leaf was missing, which stopped correcting the
      DIRECTORIES above it — so a claim on the directory `src` did not block an
      edit to `SRC/newfile.py`, and two agents could claim one not-yet-created
      file under two spellings.
    * Canonicalise the ROOT the same way before making the path relative to it.
      Comparing the kernel's fully-normalised answer against a raw
      os.path.realpath silently threw the whole canonicalisation away whenever
      the two disagreed — a differently-cased repo root, or a
      /System/Volumes/Data firmlink prefix, was enough.
    * A path that does not exist at all is kept exactly as typed. A file about
      to be created has no spelling on disk to defer to, and this is the cheap
      and commonest case.

    Deliberately not done inside scope.py: the overlap arithmetic must stay a
    pure string operation with no filesystem in it, or two agents on two
    machines could fold the same log into different answers.
    """
    # Canonicalise the root itself, or the comparison below is against a
    # differently-spelled prefix and silently fails.
    root_real = _disk_spelling(root) or os.path.realpath(str(root))
    try:
        rel = target.relative_to(root)
    except ValueError:
        try:
            rel = Path(os.path.relpath(str(target), str(root)))
        except ValueError:
            return str(target)

    parts = list(rel.parts)
    # Walk DOWN to the deepest ancestor that exists; everything below it is
    # being created and keeps the spelling it was given.
    probe = root
    depth = 0
    for part in parts:
        nxt = probe / part
        try:
            if not nxt.exists():
                break
        except OSError:
            break
        probe = nxt
        depth += 1

    tail = parts[depth:]
    if depth == 0:
        # Nothing below the root exists yet.
        return "/".join(parts)

    spelled = _disk_spelling(probe)
    if spelled:
        try:
            head = Path(spelled).relative_to(Path(root_real)).as_posix()
            return "/".join([head, *tail]) if tail else head
        except ValueError:
            # Resolved outside the repo — a symlink out, most likely. Not ours
            # to rename; the caller's outside-the-repo guard handles it.
            return "/".join(parts)

    # Fallback for platforms without F_GETPATH: read the directories, exact
    # match always winning over a case variant.
    here = root
    out: list[str] = []
    for part in parts:
        actual = part
        try:
            names = {e.name for e in here.iterdir()} if here.is_dir() else set()
            if part not in names and (here / part).exists():
                want = unicodedata.normalize("NFC", part).casefold()
                for name in sorted(names):
                    if unicodedata.normalize("NFC", name).casefold() == want:
                        actual = name
                        break
        except OSError:
            pass
        out.append(actual)
        here = here / actual
    return "/".join(out)


def _case_blind_here(directory: Path) -> bool:
    """Does this filesystem treat two cases of one name as the same file?

    Asked of the filesystem rather than guessed from the platform: a Mac can
    mount a case-sensitive volume and a Linux box the reverse.

    The probe swaps the case of a directory's own name and compares inodes —
    but it can only do that on a name that HAS cased letters, and the first
    version gave up when the deepest existing directory did not. Directories
    named `2026`, `20260820`, `0005` are ordinary (dates, ticket numbers,
    version folders) and every one of them silently switched the whole
    protection off on a filesystem that is genuinely case-blind. So it walks UP
    until it finds an ancestor it can actually ask about.

    Case-sensitivity is a property of the volume, so any ancestor on the same
    volume answers the same question. The exception is a differently-mounted
    subtree — a case-sensitive image mounted inside a case-blind checkout — and
    there this can be wrong in either direction. That is a documented limit
    rather than something a name probe can settle.
    """
    probe = directory
    for _ in range(64):  # bounded: a path is not infinitely deep
        name = probe.name
        if name and name.swapcase() != name:
            twin = probe.parent / name.swapcase()
            try:
                a, b = probe.stat(), twin.stat()
            except OSError:
                return False
            return (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)
        if probe.parent == probe:
            break
        probe = probe.parent
    return False


def _same_file_by_name(a: str, b: str) -> bool:
    """Two scope paths that name one file on a case-blind filesystem."""
    na = unicodedata.normalize("NFC", a).casefold()
    nb = unicodedata.normalize("NFC", b).casefold()
    return na == nb


def _uncreated_twin(st, scope_str: str, root: Path, actor: str):
    """A live claim naming the same not-yet-created file under another spelling.

    THE HOLE THIS CLOSES. Everything below the deepest existing directory is
    kept exactly as typed, because a file with no spelling on disk has nothing
    to defer to — so on a case-blind filesystem `src/NewFeature.py` and
    `src/newfeature.py` were two scopes for one file. Both agents were told they
    held it, the board listed it twice, and the hook cleared both to write it,
    for exactly the window they were both writing. Claiming the file you are
    about to create is the primary use of this tool.

    Deliberately narrow: only when the path does NOT exist, only when the
    filesystem is genuinely case-blind, and only comparing against claims held
    by somebody else. An existing file already resolves to one spelling and
    needs none of this.
    """
    path_part = scope_str.split("#", 1)[0]
    target = root / path_part
    try:
        if target.exists():
            return None
    except OSError:
        return None
    probe = target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not _case_blind_here(probe):
        return None
    for claim in st.claims.values():
        if claim.actor == actor:
            continue
        other = str(claim.scope).split("#", 1)[0]
        if other != path_part and _same_file_by_name(other, path_part):
            return claim
    return None


def _rooted_scope(raw: str, root: Path) -> str:
    """Re-express a scope's path relative to the repo root.

    THE BUG THIS EXISTS TO PREVENT. The STORE is keyed by the git root, so two
    agents in one checkout share a board — see :func:`_repo_root`. The scope
    STRING was not given the same treatment, so it was recorded exactly as
    typed. An agent standing at the root claiming ``src/alpha.py`` and one
    standing in ``src/`` claiming ``alpha.py`` are naming the same file, but the
    overlap check compares strings: both claims were accepted, both agents were
    told they held it, and the board listed one file twice. That is the one
    guarantee this tool makes without hedging, failing silently.

    Paths resolve against the CWD, the way git resolves them: that is what the
    person typing is looking at. A scope that lands outside the repository is
    refused rather than recorded, because the board it would appear on belongs
    to a different checkout.

    The anchor (``#symbol`` or ``#L10-40``) is split off untouched — it is not a
    path and must never be resolved as one.
    """
    text = str(raw or "").strip()
    path_part, sep, anchor = text.partition("#")
    if not path_part:
        return text
    candidate = Path(path_part).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve()
        base = root.resolve()
    except OSError:
        # Cannot resolve (a vanished cwd, a permission wall). Leave it alone
        # rather than guess: a wrong path recorded confidently is worse than an
        # unnormalised one.
        return text
    try:
        rel = resolved.relative_to(base)
    except ValueError:
        _err(f"error: {path_part!r} is outside this repository ({base})")
        _err("  A claim is only meaningful on the board for the repo it names.")
        sys.exit(EXIT_USAGE)
    rel_text = _canonical_relpath(resolved, base)
    if not rel_text or rel_text == ".":
        _err(f"error: {path_part!r} names the repository root, not something in it")
        sys.exit(EXIT_USAGE)
    return rel_text + sep + anchor


def _single_scope_or_exit(positional: list[str], verb: str) -> str:
    """Take exactly one scope, and refuse the spellings that silently lost work.

    Both refusals are here because both used to exit 0 having recorded less than
    the user asked for:

    * ``claim a.py b.py c.py`` kept ``a.py`` and dropped the rest. The agent was
      told it had claimed, believed it held its whole working set, and left two
      files advertised as free ground for somebody else to take.
    * ``claim "a.py,b.py"`` recorded the comma string as ONE scope. It matches no
      file, so it can never conflict with anything and never protects anything —
      a claim that is inert by construction.
    """
    if len(positional) > 1:
        _err(f"error: {verb} takes one scope, but {len(positional)} were given: "
             + ", ".join(repr(p) for p in positional))
        _err(f"  Claim them one at a time — earlier versions kept only {positional[0]!r} "
             "and silently dropped the rest.")
        sys.exit(EXIT_USAGE)
    scope_str = positional[0]
    if "," in scope_str:
        _err(f"error: {scope_str!r} looks like several scopes joined by commas")
        _err("  A comma is not a separator here; the whole string would be recorded "
             "as one scope that matches no file. Claim them one at a time.")
        sys.exit(EXIT_USAGE)
    return scope_str


def _hello_if_unknown(st, actor: str) -> "object | None":
    """A hello event when this agent session has not yet been tied to this actor.

    WHY THIS EXISTS AT ALL — the integration failure it fixes. The pre-edit hook
    that actually ENFORCES claims is a separate process with no COMMS_ACTOR of
    its own: it inherits the session environment, not the per-command prefix
    agents use. To learn who is asking, it looks up the agent session id from the
    hook payload against the ``hello`` events in the log, and takes that actor.

    This CLI never wrote a hello. So the lookup found nothing, the hook fell back
    to a sentinel actor that matches nobody, and every claim in the repo — the
    caller's OWN included — came back as somebody else's. The result was the
    exact opposite of the tool's purpose: an agent claimed a file and was then
    blocked from editing the file it had just claimed, with a conflict message
    naming itself. Observed in a four-agent run; every one of them would have
    hit it.

    Written implicitly rather than left to a ``hello`` verb because nothing tells
    an agent to run one — the always-on instructions describe claim and release
    and nothing else — and an identity record that agents must remember to write
    is one they will not write.

    Emitted only when the pairing is not already recorded, so a long session adds
    one event, not one per claim.
    """
    agent_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not agent_session:
        # Not running under a host agent that reports a session. Nothing to tie.
        return None
    known = st.sessions.get(actor)
    if known is not None and known.agent_session == agent_session:
        return None
    data = {"agent_session": agent_session}
    label = os.environ.get("COMMS_LABEL", "").strip()
    if label:
        data["label"] = label
    # Who made this agent. Read from the environment because nothing else can
    # know it, and left ABSENT when unset rather than guessed: independence_of
    # reports "unknown" for a missing vendor and that is the honest answer.
    # Verified, and verified by another instance of the same model, are
    # different claims — inferring the stronger one from a naming convention
    # would be manufacturing evidence.
    vendor = os.environ.get("COMMS_VENDOR", "").strip()
    if vendor:
        data["vendor"] = vendor
    try:
        data["hostname"] = socket.gethostname()
    except Exception:
        pass
    return _event(actor, _log.TYPE_HELLO, None, data)


def _parse_scope_or_exit(raw: str) -> _scope.Scope:
    try:
        return _scope.parse(raw)
    except Exception as exc:
        _err(f"error: cannot read the scope {raw!r}: {exc}")
        _err('  Expected "path", "path#symbol" or "path#L10-40".')
        sys.exit(EXIT_USAGE)


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def _render_conflicts(conflicts: list, scope_str: str) -> str:
    lines = [
        "CLAIM CONFLICT — not recorded. Somebody else already holds this ground.",
        f"  you asked for: {scope_str}",
    ]
    for claim in conflicts:
        when = claim.ts.isoformat().replace("+00:00", "Z")
        intent = f'  "{claim.intent}"' if claim.intent else ""
        lines.append(f"  @{claim.actor} holds {claim.scope} since {when}{intent}")
    lines.append("  This one is exact, not a guess: it is read out of the log.")
    lines.append("  Claim something narrower, or agree with them who takes it.")
    return "\n".join(lines)


def _cmd_claim(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    if not positional:
        _err("error: claim needs a scope, e.g. comms-graph claim src/foo.py#parse --as me")
        return EXIT_USAGE
    scope_str = _single_scope_or_exit(positional, "claim")
    actor = _actor(flags)
    intent = flags.get("intent", "")
    root = _repo_root(flags.get("root"))
    scope_str = _rooted_scope(scope_str, root)
    scope = _parse_scope_or_exit(scope_str)
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)

    # The whole read-fold-append cycle under one lock. Folding outside it would
    # let two agents both read "no conflict" and both append, which is precisely
    # the collision this command exists to prevent.
    with _lock.file_lock(lock_file) as handle:
        st = _read_state(log_file)
        task_tag = ""
        if flags.get("task"):
            task_tag = _slug_or_exit(flags["task"])
            if task_tag not in st.tasks:
                # Silently ignoring an unknown tag would put us back where we
                # started: the claim records, the task never shows a doer, and
                # nothing says why.
                _err(f"error: no task called {task_tag!r} to tag this claim with")
                if st.tasks:
                    _err("  Declared: " + ", ".join(sorted(st.tasks)[:8]))
                else:
                    _err("  Declare one first with `comms-graph task add <id>`.")
                return EXIT_USAGE
            # ONE TASK, ONE AGENT — enforced here rather than counted.
            #
            # Two agents sharing a task produced every severity-1 the task
            # graph ever had: a review that covered half the work, a gate you
            # could opt out of by never submitting, a rejection that un-did the
            # bookkeeping, and a plain file release that silently closed a task
            # somebody else was still writing. None of them were separately
            # fixable — they were the same fact wearing different clothes.
            #
            # Nothing is lost. Two agents on one task still take DIFFERENT
            # files, so the file lock already gives the parallelism; sharing a
            # task label only added one review covering work that was not all
            # there yet. Two tasks and an edge say the same thing and cannot
            # go wrong this way.
            #
            # Sequential handoff is still fine: this only refuses while
            # somebody else is HOLDING ground. When they release — or you free
            # an abandoned claim with `release --force` — the task is yours.
            on_it = sorted({c.actor for c in st.claims.values()
                            if (getattr(c, "task", "") or "") == task_tag
                            and c.actor != actor})
            if on_it:
                _err(f"error: {task_tag} is already being worked by "
                     + ", ".join("@" + a for a in on_it))
                _err("  A task belongs to one agent at a time. Take a different task,")
                _err("  or split this one so you each have your own to be checked.")
                holders = [c for c in st.claims.values()
                           if (getattr(c, "task", "") or "") == task_tag and c.actor != actor]
                for c in holders[:3]:
                    _err(f"    @{c.actor} holds {c.scope}  [{c.id}]")
                return EXIT_CONFLICT
        conflicts = st.conflicts_for(scope, actor)
        # Taking ground off somebody who is not coming back. The fold has always
        # understood this — a claim carrying `steals` displaces the named one
        # atomically — and nothing wrote it, so an abandoned claim could only be
        # ended by the agent that left. That was already bad; once a task-tagged
        # claim began holding its successors blocked, one crashed agent stalled
        # the whole downstream graph with no command anybody else could run.
        #
        # Deliberately blunt: it needs the exact claim id and a reason, both go
        # in the log under the taker's name, and it refuses anything it was not
        # pointed at. It is an arbitration somebody is accountable for, not a
        # retry that happens to work.
        steal_id = (flags.get("steal") or "").strip()
        if steal_id:
            target = next((c for c in conflicts if c.id == steal_id), None)
            if target is None:
                held = st.claims.get(steal_id)
                _err(f"error: {steal_id} is not what is holding {scope_str}")
                if held is not None:
                    _err(f"  That id is @{held.actor}'s claim on {held.scope}.")
                if conflicts:
                    _err("  Blocking you: "
                         + ", ".join(f"{c.id} (@{c.actor} on {c.scope})" for c in conflicts))
                else:
                    _err("  Nothing is blocking you here — claim it without --steal.")
                return EXIT_USAGE
            reason = (flags.get("reason") or "").strip()
            if not reason:
                _err("error: --steal needs --reason saying why the holder is not coming back")
                _err(f"  @{target.actor} has held {target.scope} since "
                     + target.ts.isoformat().replace("+00:00", "Z") + ".")
                _err("  It goes in the log under your name, permanently.")
                return EXIT_USAGE
            claim_data_steal = {"steals": target.id, "steal_reason": reason}
            stolen_from_actor = target.actor
            conflicts = [c for c in conflicts if c.id != target.id]
        else:
            claim_data_steal = {}
        if not conflicts:
            twin = _uncreated_twin(st, scope_str, root, actor)
            if twin is not None:
                conflicts = [twin]
        if conflicts:
            # Write down the refusal. A prevented collision that leaves no trace
            # is why a log of thousands of claims can honestly report having
            # prevented nothing.
            holder = conflicts[0]
            try:
                _log.append(
                    log_file,
                    _event(
                        actor,
                        _log.TYPE_BLOCKED,
                        None,
                        {
                            "scope": scope_str,
                            "intent": intent,
                            "holder": holder.actor,
                            "holder_scope": str(holder.scope),
                        },
                    ),
                )
            except Exception:
                pass
            print(_render_conflicts(conflicts, scope_str))
            return EXIT_CONFLICT

        # The far end of the critical section. Everything above read the log and
        # concluded the ground was free; that conclusion is only worth anything
        # if we still hold the lock we read it under. See LockHandle.compromised:
        # a lock file deleted mid-hold leaves us flocked to an orphan inode while
        # somebody else flocks a fresh one at the same path, and both of us then
        # write a claim on the same scope. Fail closed — refusing a claim costs
        # one retry, recording a second holder costs two agents editing at once.
        broken = handle.compromised()
        if broken:
            _err(f"error: the coordination lock is no longer exclusive: {broken}")
            _err(f"  Nothing was recorded. Check {lock_file} and try again.")
            return EXIT_ERROR
        # --task is what makes task state DERIVED rather than a second set of
        # books somebody has to remember to update: doers come from live claims
        # tagged this way, so releasing the file empties them for free. It was
        # accepted and then dropped on the floor — _parse_flags takes any flag,
        # so `--task auth-api` exited 0 having recorded nothing, which left
        # PHASE_DOING unreachable and the task never showing a doer.
        claim_data: dict = dict(claim_data_steal)
        if intent:
            claim_data["intent"] = intent
        if task_tag:
            claim_data["task"] = task_tag
        # Minted BEFORE the event it explains. fold() sorts by TIMESTAMP, so
        # putting the greeting first in the list is not enough — it has to be
        # first in time. It was created second, landed second, and every reader
        # that needed the session (independence, the session half of the
        # self-review gate) saw an actor with no hello on record yet.
        greeting = _hello_if_unknown(st, actor)
        event = _event(actor, _log.TYPE_CLAIM, [str(scope)], claim_data)
        # Re-claiming ground you already hold REPLACES your claim on it; it does
        # not add a second. Two rows for one file made the board's claim count
        # wrong, left it ambiguous which intent was current, and — since a claim
        # carries a task tag — put one agent on two tasks from one file, which
        # the phase derivation reads as two people still working.
        #
        # Recorded as a release rather than dropped in the fold, so the audit
        # trail says what happened. Exact scope only: a file and a symbol inside
        # it are different ground and holding both is legitimate.
        superseded = [c for c in st.claims.values()
                      if c.actor == actor and str(c.scope) == str(scope)]
        batch = ([greeting] if greeting is not None else [])
        if superseded:
            batch.append(_event(actor, _log.TYPE_RELEASE, None, {
                "refs": [c.id for c in superseded],
                "result": "superseded by a new claim on the same ground",
            }))
        if not batch:
            _log.append(log_file, event)
        else:
            # One batch, so the identity record and the claim it explains land
            # together or not at all. A claim on disk whose hello is missing is
            # precisely the state that made the hook block its own caller.
            _log.append_batch(log_file, batch + [event])
        others = [
            (claim.actor, str(claim.scope), claim.scope)
            for claim in st.claims.values()
            if claim.actor != actor
        ]

    # Everything from here is advisory and needs no lock.
    print(f"CLAIMED {scope} by @{actor}  (id {event.id})")
    if claim_data.get("steals"):
        print(f"  TAKEN FROM @{stolen_from_actor} — their claim {claim_data['steals']} is ended.")
        print("  Recorded as a steal under your name, with your reason. If they come")
        print("  back and re-claim it, they will be told the same way you were.")
    print(_advice(root, flags.get("graph"), scope, others))
    return EXIT_OK


def _staleness_note(root: Path, map_path: Path, rel_path: str) -> str:
    """Say when the map predates the code it is being asked about.

    README-COMMS.md sells this as one of the tool's honest limits: "a claim
    pointing at code that changed since the last graphify extract is reported as
    out of date, not as truth". The implementation existed — view._stale_note —
    but nothing outside its own test ever imported that module, so the promise
    was never kept on the path anybody actually uses. A map built a month ago
    printed exactly the same confident all-clear as one built a second ago.

    That is the worst direction for this failure to run in: agents edit
    constantly and re-extract rarely, so the map is most out of date precisely
    when the repository is busiest, and the reassurance is loudest exactly when
    it is least earned.

    Two checks, because they answer different questions. The claimed file being
    newer than the map says THIS answer is suspect. The map being older than the
    newest thing in the repo says the whole map is drifting, which the caller
    should hear even when the file they named has not moved.
    """
    try:
        map_mtime = map_path.stat().st_mtime
    except OSError:
        return ""
    notes: list[str] = []
    try:
        target = root / rel_path
        if target.is_file() and target.stat().st_mtime > map_mtime:
            notes.append(
                "  OUT OF DATE — " + rel_path + " changed after the map was built, "
                "so what follows may describe code that is no longer there."
            )
    except OSError:
        pass
    if not notes:
        newest = _newest_source_mtime(root)
        if newest is not None and newest > map_mtime:
            age = int((newest - map_mtime) // 3600)
            when = (str(age) + "h") if age >= 1 else "under an hour"
            notes.append(
                "  OUT OF DATE — the map is older than the newest code in this repo "
                "(by " + when + "); rebuild with `graphify extract . --code-only`."
            )
    return "\n".join(notes)


def _newest_source_mtime(root: Path) -> float | None:
    """Newest mtime among tracked source files, or None if it cannot be told.

    Asks git rather than walking the tree: a walk on a large repo would put a
    filesystem crawl on the claim path, which runs in front of every edit, and
    would count build output and virtualenvs as "code" besides.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        newest = None
        for raw in out.stdout.split(b"\0"):
            if not raw:
                continue
            try:
                name = raw.decode("utf-8", "surrogateescape")
            except Exception:
                continue
            if not name.endswith((".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
                                  ".java", ".rb", ".c", ".h", ".cpp", ".cs", ".php")):
                continue
            try:
                m = (root / name).stat().st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
        return newest
    except Exception:
        return None


def _advice(root: Path, graph_flag: str | None, scope: _scope.Scope, others: list) -> str:
    """The contact block: a prompt to look, never a verdict, never an order."""
    map_path = _graph_path(root, graph_flag)
    graph = _load_graph(map_path)
    stale = _staleness_note(root, map_path, getattr(scope, "path", "") or "") if graph is not None else ""
    mine = _resolve.resolve(graph, scope, root)
    if mine.miss_reason:
        # Loud. This is the reply that must never be mistaken for "all clear".
        # The hint has to match the reason: telling somebody to check their
        # spelling when the real problem is that no map exists sends them
        # hunting for a typo that is not there.
        if graph is None:
            hint = "  Nothing is wrong with the claim — there is just nothing to check it against."
        else:
            hint = "  Check the name: a typo looks exactly like code nothing else touches."
        body = (
            f"  NOT ON THE MAP — the claim is recorded, but no contact check was possible:\n"
            f"    {mine.miss_reason}\n"
            f"{hint}"
        )
        return (stale + "\n" + body) if stale else body
    resolved_others = []
    for actor, scope_str, other_scope in others:
        res = _resolve.resolve(graph, other_scope, root)
        resolved_others.append((actor, scope_str, res))
    body = _contact.render(_contact.contact(graph, mine, resolved_others))
    return (stale + "\n" + body) if stale else body


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def _cmd_release(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    actor = _actor(flags)
    target = positional[0] if positional else ""
    root = _repo_root(flags.get("root"))
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)

    with _lock.file_lock(lock_file):
        st = _read_state(log_file)
        # Freeing ground somebody ELSE left behind. Until this existed the only
        # ways were to steal it and hand it back — two writes, and you briefly
        # own work you did not do — or to pass `--as` their name, which the CLI
        # accepts and which writes a lie into an append-only log. A person
        # clearing up after a crashed agent should not have to impersonate it.
        #
        # It says whose it was and who took it, under the real name, with a
        # reason. The fold needed nothing: a release is popped by claim id and
        # has never cared who wrote it.
        force = "force" in flags or "force" in _repeated(argv, "force")
        if force:
            return _release_somebody_elses(
                st, log_file, actor, target, flags.get("reason", "").strip())
        mine = st.active_claims_by_actor(actor)
        if not mine:
            print(f"nothing to release — @{actor} holds no claims here")
            return EXIT_OK
        if not target:
            chosen = mine
        else:
            by_id = st.claim_by_id(target)
            if by_id is not None and by_id.actor == actor:
                chosen = [by_id]
            else:
                # Rooted the same way claim roots it, and for the same reason: a
                # claim taken at the repo root must be releasable from a
                # subdirectory. Without this the two spellings of one file stop
                # matching and an agent cannot give back ground it holds — the
                # claim then sits there until it goes stale, blocking everybody.
                # Only reached when the target was not a claim id, so an id is
                # never mangled by path resolution.
                # Try the canonicalised spelling AND the raw one. The scope was
                # frozen into the log at claim time; the canonicaliser answers
                # from the disk as it is NOW. After a case-only rename those two
                # disagree, and the holder could not release her own claim —
                # the refusal even printed "holds nothing matching
                # 'src/Widget.py'  Held: src/Widget.py", naming the string it
                # had just said matched nothing. Only the claim id worked, so
                # the ground sat stranded on the board.
                chosen = []
                for spelling in (_rooted_scope(target, root), target):
                    wanted = _parse_scope_or_exit(spelling)
                    chosen = [c for c in mine if _scope.overlaps(c.scope, wanted)]
                    if chosen:
                        break
        if not chosen:
            _err(f"error: @{actor} holds nothing matching {target!r}")
            _err("  Held: " + ", ".join(str(c.scope) for c in mine))
            return EXIT_USAGE
        data: dict = {"refs": [c.id for c in chosen]}
        if flags.get("result"):
            data["result"] = flags["result"]
        _log.append(log_file, _event(actor, _log.TYPE_RELEASE, None, data))

    for claim in chosen:
        print(f"RELEASED {claim.scope}  (was @{actor}, id {claim.id})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def _slug_or_exit(raw: str, what: str = "task id") -> str:
    """Task ids are human-speakable, because agents have to quote them.

    Not the event ULID: measured on a real store, 99.5% of 11,049 event ids
    shared their six-character prefix, so an agent asked to name one had nothing
    short to say and nothing memorable to repeat. A slug is what somebody can
    type into the next command without going back to look it up.
    """
    slug = (raw or "").strip().lower()
    if not _SLUG_RE.match(slug):
        _err(f"error: {what} {raw!r} is not a slug")
        _err("  Lowercase letters, digits and hyphens, up to 32 characters, "
             "starting with a letter or digit — e.g. auth-api.")
        sys.exit(EXIT_USAGE)
    return slug


def _repeated(argv: list[str], name: str) -> list[str]:
    """Every value given for a repeatable flag.

    _parse_flags keeps one value per flag, which is right for --intent and wrong
    for --check: a task with three required checks needs all three results, and
    silently keeping the last one would let two failing checks vanish behind one
    passing one.
    """
    out: list[str] = []
    flag = "--" + name
    i = 0
    while i < len(argv):
        if argv[i] == flag and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 2
        elif argv[i].startswith(flag + "="):
            out.append(argv[i][len(flag) + 1:])
            i += 1
        else:
            i += 1
    return out


def _task_runtime(flags: dict) -> tuple[Path, Path, Path]:
    root = _repo_root(flags.get("root"))
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)
    return root, log_file, lock_file



# ---------------------------------------------------------------------------
# check — the pre-edit hook
# ---------------------------------------------------------------------------

#: Claude Code reads exit 2 from a PreToolUse hook as "block this tool call and
#: show stderr to the model". EVERY OTHER non-zero code means "the hook itself
#: errored" and the edit proceeds anyway. So 2 is the only code that stops an
#: edit, and anything we cannot answer confidently must also be 2 — a
#: coordination tool that cannot read its log has not established that the file
#: is free, and waving the edit through would be the one wrong direction to fail
#: in.
EXIT_BLOCK = 2


def _hook_payload(stream) -> tuple[str, str]:
    """(file_path, agent_session) from Claude Code's PreToolUse JSON.

    The payload looks like:
        {"session_id": "...", "tool_name": "Edit",
         "tool_input": {"file_path": "/abs/or/rel/path.ts", ...}}

    A missing file_path is NOT an error: plenty of tool calls have no file at
    all (Bash, Read of a URL), and the caller treats an empty path as "nothing
    to check".
    """
    raw = stream.read()
    if not raw.strip():
        raise ValueError("empty stdin: expected Claude Code's PreToolUse JSON")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    tool_input = payload.get("tool_input")
    path = ""
    if isinstance(tool_input, dict):
        path = str(tool_input.get("file_path") or "")
    return path, str(payload.get("session_id") or "")


def _repo_root_for_file(target: Path) -> Path | None:
    """The repository a FILE belongs to, not the one the process is standing in.

    A hook does not run where the file lives. The host agent's working directory
    is wherever the session was started, routinely a parent folder holding
    several checkouts and very often not a repository at all — so resolving from
    the process made the hook fail on every edit whenever that was true,
    including for files plainly inside a repository.

    Walks up to the nearest directory that EXISTS first: creating a new file in
    a new directory is ordinary, but the hook runs BEFORE the editor makes those
    parents, so the file's own directory frequently does not exist yet.
    """
    probe = target if target.is_dir() else target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    for candidate in (probe, *probe.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _hook_actor(st, agent_session: str, explicit: str) -> str:
    """Who is asking, when the caller is a hook with no environment of its own.

    An explicit actor wins. Otherwise the agent session id from the payload is
    matched against the hello events in the log — which is the entire reason
    claim writes one. With no match we deliberately return a sentinel that can
    never equal a real actor, so the caller's own claims are NOT excluded and it
    is blocked by its own work rather than waved through on somebody else's.
    Being told to wait for yourself is annoying; editing a file somebody else
    holds because we could not identify you is the failure this tool exists to
    prevent.
    """
    if explicit:
        return explicit
    if agent_session:
        best = None
        for sess in st.sessions.values():
            if getattr(sess, "agent_session", "") != agent_session:
                continue
            if best is None or (sess.ts and best.ts and sess.ts > best.ts):
                best = sess
        if best is not None:
            return best.actor
    return "\x00not-a-real-actor"


def _cmd_ui(argv: list[str]) -> int:
    """Serve the live board until interrupted."""
    _, flags = _parse_flags(argv)
    root, log_file, _lockf = _task_runtime(flags)
    host = flags.get("host") or "127.0.0.1"
    try:
        port = int(flags.get("port") or 7878)
    except ValueError:
        _err(f"error: --port {flags['port']!r} is not a number")
        return EXIT_USAGE

    from . import server as _server

    try:
        httpd = _server.serve(root, log_file, host=host, port=port,
                              graph_file=flags.get("graph"))
    except OSError as exc:
        _err(f"error: cannot listen on {host}:{port}: {exc.strerror or exc}")
        _err("  Something else is probably already using it — try --port 7879.")
        return EXIT_ERROR

    print(f"comms board on http://{host}:{port}  (ctrl-c to stop)")
    print(f"  watching {log_file}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.shutdown()
        httpd.server_close()
    return EXIT_OK


def _cmd_check(argv: list[str]) -> int:
    """The hook entry point. NOTHING may escape this alive.

    Exit 2 is the only code that stops an edit; every other non-zero code tells
    Claude Code the hook itself errored, and the edit proceeds. So an unhandled
    exception here does not merely produce an ugly traceback — it turns the
    enforcement layer OFF for that call, silently, on ground somebody holds.

    Measured before this wrapper: a filename containing a tab, a 300-byte path
    component, and the repository root itself each raised out of the body and
    exited 1, on a directory another agent had claimed. The body guards the
    payload read and the log read; it did not guard scope parsing, path
    resolution, or repo discovery, and there is no way to enumerate what might
    raise next. So the guarantee is made structural rather than case by case.
    """
    try:
        return _cmd_check_inner(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — deliberately everything
        _err(f"check: refusing to answer — {type(exc).__name__}: {exc}")
        _err("  comms could not establish that this path is free, so it is not "
             "saying that it is.")
        return EXIT_BLOCK


def _cmd_check_inner(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    use_stdin = "stdin-json" in flags

    agent_session = ""
    if use_stdin:
        try:
            raw_path, agent_session = _hook_payload(sys.stdin)
        except (ValueError, json.JSONDecodeError) as exc:
            _err(f"check: cannot read the hook payload: {exc}")
            return EXIT_BLOCK
        if not raw_path:
            # A tool call with no file in it. Nothing to check; allow.
            return EXIT_OK
    else:
        if len(positional) != 1:
            _err("error: check needs a path, or --stdin-json to read a hook payload")
            return EXIT_USAGE
        raw_path = positional[0]

    target = Path(raw_path).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target)

    explicit_root = flags.get("root")
    root = Path(explicit_root).expanduser().resolve() if explicit_root else _repo_root_for_file(target)
    if root is None:
        # Not in a repository at all. There is nothing this could conflict with,
        # and blocking every edit made outside a checkout would be absurd.
        return EXIT_OK

    try:
        resolved_target = target.resolve()
        resolved_root = root.resolve()
        resolved_target.relative_to(resolved_root)
    except (ValueError, OSError):
        # Outside the repo we know about. We claim nothing there.
        return EXIT_OK
    rel_text = _canonical_relpath(resolved_target, resolved_root)
    rel = Path(rel_text)

    log_file, _lock_file = _store(root, create=False)
    try:
        log_file.stat()
    except FileNotFoundError:
        # Genuinely absent: nobody has ever coordinated in this repo, so there
        # is nothing this edit can collide with. Do NOT create the store to
        # find that out — check never writes, and mkdir'ing here meant an
        # unwritable data home denied every edit on the machine.
        return EXIT_OK
    except OSError as exc:
        # ANY OTHER failure means we could not find out. Path.is_file() was used
        # here and returns False for ENOTDIR, ELOOP and ENOENT alike, so a store
        # that was merely UNREACHABLE — a stray file where the directory should
        # be, a symlink loop — read as "nobody has ever coordinated here" and
        # the edit was allowed while a live claim sat on disk the whole time.
        # One stray file in the data home turned enforcement off for every
        # repository, silently, with exit 0. Absent and unreachable are not the
        # same answer and must not share a branch.
        _err(f"check: cannot reach the coordination log: {exc.strerror or exc}")
        _err("  comms could not establish that this path is free, so it is not "
             "saying that it is.")
        return EXIT_BLOCK
    try:
        st = _read_state(log_file)
    except Exception as exc:
        _err(f"check: cannot read the coordination log: {exc}")
        return EXIT_BLOCK

    scope = _scope.parse(rel_text)
    actor = _hook_actor(st, agent_session, (flags.get("as") or os.environ.get("COMMS_ACTOR", "")).strip())
    conflicts = st.conflicts_for(scope, actor)
    if not conflicts:
        twin = _uncreated_twin(st, rel_text, root, actor)
        if twin is None:
            return EXIT_OK
        conflicts = [twin]

    holder = conflicts[0]
    when = holder.ts.isoformat().replace("+00:00", "Z")
    _err(f"BLOCKED: {rel_text} is claimed.")
    _err(f"  Holder:  @{holder.actor}")
    _err(f"  Claim:   {holder.id}")
    if holder.intent:
        _err(f'  Intent:  "{holder.intent}"')
    _err(f"  Since:   {when}")
    _err("  Ask them, or claim something narrower. Do not edit ground you were refused.")
    return EXIT_BLOCK


def _cmd_tasks(argv: list[str]) -> int:
    """Draw the task graph to an HTML file."""
    _, flags = _parse_flags(argv)
    root, log_file, _lockf = _task_runtime(flags)
    st = _read_state(log_file)
    out = flags.get("out")
    if not out:
        out = _graph_path(root, flags.get("graph")).parent / _taskview.DEFAULT_FILENAME
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    res = _taskview.render(st, out, generated=generated)
    if res.task_count == 0:
        print("no tasks declared yet — the page will say so")
    print(f"wrote {res.output_path}")
    print(f"  {res.task_count} task(s), {res.edge_count} edge(s)"
          + (f", {res.blocked} blocked" if res.blocked else "")
          + (f", {res.cycles} unreachable" if res.cycles else ""))
    return EXIT_OK


def _cmd_task(argv: list[str]) -> int:
    verb = argv[0] if argv else ""
    rest = argv[1:]
    if verb == "add":
        return _task_add(rest)
    if verb == "edge":
        return _task_edge(rest)
    if verb == "done":
        return _task_done(rest)
    if verb == "review":
        return _task_review(rest)
    if verb in ("", "-h", "--help"):
        print(TASK_USAGE)
        return EXIT_OK if verb else EXIT_USAGE
    _err(f"error: unknown task command {verb!r}")
    _err(TASK_USAGE)
    return EXIT_USAGE


def _task_add(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    if not positional:
        _err("error: task add needs an id, e.g. comms-graph task add auth-api --title \"Auth API\"")
        return EXIT_USAGE
    slug = _slug_or_exit(positional[0])
    actor = _actor(flags)
    _, log_file, lock_file = _task_runtime(flags)

    data: dict = {"task": slug}
    for key in ("title", "size", "ref"):
        if flags.get(key):
            data[key] = flags[key]
    checks = [c.strip() for c in _repeated(argv, "check") if c.strip()]
    if checks:
        data["checks"] = checks

    with _lock.file_lock(lock_file) as handle:
        broken = handle.compromised()
        if broken:
            _err(f"error: the coordination lock is no longer exclusive: {broken}")
            return EXIT_ERROR
        greeting = _hello_if_unknown(_read_state(log_file), actor)
        events = [_event(actor, _log.TYPE_TASK, None, data)]
        if greeting is not None:
            events.insert(0, greeting)
        _log.append_batch(log_file, events)
    print(f"TASK {slug}" + (f"  {data.get('title')}" if data.get("title") else ""))
    if checks:
        print(f"  must pass before done: {', '.join(checks)}")
    return EXIT_OK


def _task_edge(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    if len(positional) < 2:
        _err("error: task edge needs two ids: comms-graph task edge <from> <to>")
        _err("  Meaning: <to> comes AFTER <from>.")
        return EXIT_USAGE
    from_ = _slug_or_exit(positional[0], "from-id")
    to = _slug_or_exit(positional[1], "to-id")
    if from_ == to:
        _err(f"error: {from_!r} cannot come after itself")
        return EXIT_USAGE
    actor = _actor(flags)
    _, log_file, lock_file = _task_runtime(flags)

    # Do NOT default the kind here when the edge may already exist. Writing
    # "sequence" because the user did not retype --kind is how re-noting an edge
    # erased the rework dependency on it: since the kind became load-bearing,
    # that flipped a task a rejection had reopened straight back to closed, with
    # nobody re-reviewing it, and printed "(sequence)" beside a provides note
    # that only makes sense for an interface. The stored kind is looked up under
    # the lock below; this is only the explicitly-given one.
    kind = (flags.get("kind") or "").lower()
    if kind and kind not in (_task.EDGE_INTERFACE, _task.EDGE_ARTIFACT, _task.EDGE_SEQUENCE):
        _err(f"error: --kind {kind!r} is not one of interface, artifact, sequence")
        _err("  interface/artifact mean <to> consumes something from <from>, so reworking")
        _err("  <from> flags <to>. sequence is ordering only.")
        return EXIT_USAGE

    with _lock.file_lock(lock_file) as handle:
        broken = handle.compromised()
        if broken:
            _err(f"error: the coordination lock is no longer exclusive: {broken}")
            return EXIT_ERROR
        st = _read_state(log_file)
        for end in (from_, to):
            if end not in st.tasks:
                _err(f"error: no task called {end!r}")
                _err("  Declare it first with `comms-graph task add " + end + "`.")
                return EXIT_USAGE
        # Refuse the edge that would close a loop rather than accept it and then
        # describe the wreckage: the fold survives a cycle, but a graph
        # reporting "cycle" is a graph nobody can act on.
        if _task.would_cycle(st.task_edges, from_, to):
            _err(f"error: {from_} -> {to} would close a dependency loop")
            _err(f"  {to} already comes before {from_}, directly or through other tasks.")
            return EXIT_USAGE
        effective = kind
        if not effective:
            # Keep what is already recorded; only fall back to the weakest kind
            # for an edge nobody has declared before. Guessing "interface" for a
            # brand-new edge would invent a rework dependency nobody asked for.
            for existing in st.task_edges:
                if existing.from_ == from_ and existing.to == to:
                    effective = existing.kind
                    break
            else:
                effective = _task.EDGE_SEQUENCE
        data = {"from": from_, "to": to, "kind": effective}
        if flags.get("provides"):
            data["provides"] = flags["provides"]
        _log.append(log_file, _event(actor, _log.TYPE_TASK_EDGE, None, data))
    kind = effective
    print(f"EDGE {from_} -> {to}  ({kind})")
    if flags.get("provides"):
        print(f"  {to} consumes: {flags['provides']}")
    return EXIT_OK


def _task_done(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    if not positional:
        _err("error: task done needs an id")
        return EXIT_USAGE
    slug = _slug_or_exit(positional[0])
    actor = _actor(flags)
    _, log_file, lock_file = _task_runtime(flags)

    results: dict = {}
    for raw in _repeated(argv, "check"):
        name, _, verdict = raw.partition("=")
        if not name.strip() or not verdict.strip():
            _err(f"error: --check {raw!r} should read name=pass or name=fail")
            return EXIT_USAGE
        results[name.strip()] = verdict.strip()
    notes = [n for n in _repeated(argv, "note") if n.strip()]

    with _lock.file_lock(lock_file) as handle:
        broken = handle.compromised()
        if broken:
            _err(f"error: the coordination lock is no longer exclusive: {broken}")
            return EXIT_ERROR
        st = _read_state(log_file)
        t = st.tasks.get(slug)
        if t is None:
            _err(f"error: no task called {slug!r}")
            return EXIT_USAGE
        # Say no here as well as in the fold. The fold is the authority — a
        # second writer must not be able to sneak past a rule by not asking —
        # but a refusal the user never sees is a command that silently did
        # nothing, and exit 0 would be a lie.
        missing = [c for c in t.checks
                   if str(results.get(c, "")).strip().lower() != "pass"]
        if missing:
            # Write the refusal down, exactly as claim records a blocked event.
            # The CLI declines before the fold ever sees this transition, so
            # without a record the gate fired in complete silence — and a rule
            # that leaves no trace when it works looks like a rule nobody wrote.
            try:
                _log.append(log_file, _event(actor, _log.TYPE_BLOCKED, None, {
                    "task": slug, "attempted": "done",
                    "reason": "checks did not pass: " + ", ".join(missing),
                }))
            except Exception:
                # Recording a refusal must never turn a clean refusal into a
                # crash. The refusal itself is the part that matters.
                pass
            _err(f"error: {slug} declares checks that have not passed: {', '.join(missing)}")
            _err("  Report each one, e.g. --check " + missing[0] + "=pass")
            return EXIT_CONFLICT
        data: dict = {"task": slug, "state": "done"}
        if results:
            data["checks"] = results
        if notes:
            data["notes"] = notes
        greeting = _hello_if_unknown(st, actor)
        events = [_event(actor, _log.TYPE_TASK_STATE, None, data)]
        if greeting is not None:
            events.insert(0, greeting)
        _log.append_batch(log_file, events)
    print(f"DONE {slug} by @{actor} — awaiting review")
    print("  Somebody else has to verify it before anything after it unblocks.")
    return EXIT_OK


def _task_review(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    if not positional:
        _err("error: task review needs an id")
        return EXIT_USAGE
    slug = _slug_or_exit(positional[0])
    actor = _actor(flags)
    passed = "pass" in flags
    failed = "fail" in flags
    if passed == failed:
        _err("error: say --pass or --fail (exactly one)")
        return EXIT_USAGE
    _, log_file, lock_file = _task_runtime(flags)

    with _lock.file_lock(lock_file) as handle:
        broken = handle.compromised()
        if broken:
            _err(f"error: the coordination lock is no longer exclusive: {broken}")
            return EXIT_ERROR
        st = _read_state(log_file)
        t = st.tasks.get(slug)
        if t is None:
            _err(f"error: no task called {slug!r}")
            return EXIT_USAGE
        if not t.did:
            _err(f"error: nothing is awaiting review on {slug}")
            return EXIT_CONFLICT
        # Compare against the environment directly as well as the log: this check
        # runs before this actor's own hello is written, so at this moment the
        # log does not yet know which process is asking.
        here = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
        doer_session = getattr(st.sessions.get(t.did), "agent_session", "") or ""
        # Only --pass is gated on authorship. Rejecting your own work is not the
        # failure mode this protects against, and refusing it deadlocked any
        # task two agents had both submitted: neither could verify (correct) and
        # neither could reject, so nothing downstream could ever move.
        acknowledged = "acknowledge-self-review" in flags
        author = _task._authored_by(t, actor, st.sessions) if passed else None
        # Remember whether this actor really IS an author, because the reducer
        # decides from that and not from the flag. Printing SELF-SIGNED off the
        # flag alone meant a genuine third party who passed it was told they had
        # recorded a self-sign while the log recorded a full independent review —
        # wrong in the exact direction the disclosure exists to prevent, on the
        # one surface the acting agent definitely reads.
        is_author = author is not None
        if acknowledged:
            author = None  # recorded below as self-acknowledged, not independent
        if not acknowledged and (
            author is not None or (passed and here and here == doer_session.strip())
        ):
            whom = author or t.did
            try:
                _log.append(log_file, _event(actor, _log.TYPE_BLOCKED, None, {
                    "task": slug, "attempted": "verified" if passed else "rejected",
                    "holder": whom, "reason": "self-review",
                }))
            except Exception:
                pass
            # Two different bars, two different sentences. Somebody who holds
            # ground on the task but has never pressed `done` was told they
            # "cannot verify work done by @themselves" — work they had not
            # submitted — which reads as a bug in the tool rather than a rule.
            submitted = [x for x in ([t.did] if t.did else [])
                         + list(getattr(t, "submitters", [])) if x]
            worked = [x for x in getattr(t, "workers", []) if x]
            # Pick the branch from the SAME comparison that produced the bar.
            # Choosing it by name alone mislabelled every bar that came from the
            # session match: an actor who had never claimed or submitted
            # anything was told "you took ground tagged to it".
            by_submission = any(_task.same_agent(actor, x, st.sessions) for x in submitted)
            by_ground = any(_task.same_agent(actor, x, st.sessions) for x in worked)
            if by_ground and not by_submission:
                # Not "you hold ground" — the bar is permanent and survives a
                # release, so that sentence was false for anybody who had
                # already let go, and the remedy it named (submit your half)
                # would have made them a real co-author of work they had
                # finished with.
                _err(f"error: self-review — @{actor} has worked on this task")
                _err("  You took ground tagged to it, so the check has to come from "
                     "somebody who did not. Rejecting it with --fail is still yours to do.")
            elif by_submission:
                _err(f"error: self-review — @{actor} cannot verify work done by @{whom}")
                _err("  Verification only means something from somebody with different "
                     "blind spots.")
            else:
                # Neither name matched: the bar came from the session. Say that,
                # rather than asserting work this name did not do.
                _err(f"error: self-review — @{actor} is the same agent as @{whom}")
                _err("  You are running in the session that did this work. A different "
                     "name is not a different set of blind spots.")
            # Everyone the gate bars, not just everyone who SUBMITTED. Holding
            # ground on a task now bars you too, so a task can be unverifiable
            # with a single submitter — and the actor was then refused, told to
            # "let somebody who did not write it check", and never told the
            # escape exists. On a two-agent project that advice cannot be
            # followed and the task stops forever.
            barred = {x for x in ([t.did] if t.did else [])
                      + list(getattr(t, "submitters", []))
                      + list(getattr(t, "workers", [])) if x}
            if len(barred) > 1:
                _err("  Everyone who has worked this task is barred, so it needs somebody "
                     "who has not touched it — or reject it with --fail, which you may do.")
                _err("  If there IS nobody else, --acknowledge-self-review records it as "
                     "yours: the board will show it as self-acknowledged, never as verified.")
            return EXIT_CONFLICT
        data: dict = {"task": slug, "state": "verified" if passed else "rejected"}
        if acknowledged and passed:
            data["acknowledged_self_review"] = True
        if flags.get("evidence"):
            if passed:
                data["notes"] = [flags["evidence"]]
            else:
                data["findings"] = [{"what": flags["evidence"]}]
        # The greeting is what ties this actor to a process. Without it a
        # reviewer has no session on record, so the fold's session comparison
        # has nothing to compare and falls back to the name alone — which is
        # exactly the hole a homoglyph walks through.
        greeting = _hello_if_unknown(st, actor)
        events = [_event(actor, _log.TYPE_TASK_STATE, None, data)]
        if greeting is not None:
            events.insert(0, greeting)
        _log.append_batch(log_file, events)
    # Report what the LOG says happened, not what we were about to do. The CLI's
    # pre-check and the fold's are two implementations of one rule and they can
    # disagree — the CLI reads sessions as they stand, the fold reads them after
    # this actor's own hello has landed in the same batch. When they did, the
    # banner said VERIFIED and exit 0 while the fold had refused the transition
    # and recorded nothing: an agent reads its own output, believes the work is
    # signed off, and moves on. Whatever the rule, the answer has to come from
    # the one place that decides it.
    after = _read_state(log_file).tasks.get(slug)
    if after is not None:
        landed = bool(after.verified_by) if passed else not after.did
        if not landed:
            why = ""
            for r in reversed(_read_state(log_file).refused_task_states):
                if getattr(r, "task", "") == slug:
                    why = getattr(r, "reason", "") or ""
                    break
            _err(f"error: {slug} was NOT " + ("verified" if passed else "rejected")
                 + " — the log refused it" + (f": {why}" if why else "."))
            return EXIT_CONFLICT
    if passed and acknowledged and is_author:
        print(f"SELF-SIGNED {slug} by @{actor} (work by @{t.did})")
        print("  Recorded as signed off by its own author, not as a review. That is")
        print("  permanent and your name is on it.")
    elif passed:
        print(f"VERIFIED {slug} by @{actor} (work by @{t.did})")
        print("  Anything waiting on it is now unblocked.")
    else:
        print(f"REJECTED {slug} by @{actor} (work by @{t.did})")
        print(f"  Back to @{t.did} to rework; the graph is unchanged.")
    return EXIT_OK



# ---------------------------------------------------------------------------
# find / note — what an agent learned, for the person watching
# ---------------------------------------------------------------------------

#: The five that earn their own word. Not a taxonomy for its own sake: each one
#: answers a different question a person asks the board, and a single "note"
#: bucket collapses them into a wall the reader has to sort by hand.
FINDING_CATEGORIES = {
    "bug": "an open problem",
    "fix": "a problem now resolved",
    "ship": "released or deployed",
    "decision": "an architectural choice and its reason",
    "gotcha": "a trap the next agent would fall into",
}


def _cmd_find(argv: list[str]) -> int:
    """Record something worth keeping. The fold has always stored these and
    nothing in this build could write one, so `state.findings` was permanently
    empty and the board's panel over it permanently blank."""
    positional, flags = _parse_flags(argv)
    if len(positional) < 2:
        _err("error: find needs a category and a summary")
        _err('  e.g. comms-graph find decision "uuid PKs, never cuid" --as me')
        _err("  Categories: " + ", ".join(f"{k} ({v})" for k, v in FINDING_CATEGORIES.items()))
        return EXIT_USAGE
    category = positional[0].strip().lower()
    if category not in FINDING_CATEGORIES:
        _err(f"error: {category!r} is not a category")
        _err("  Use one of: " + ", ".join(FINDING_CATEGORIES))
        return EXIT_USAGE
    summary = " ".join(positional[1:]).strip()
    if not summary:
        _err("error: find needs something to say")
        return EXIT_USAGE
    if len(summary) > 400:
        _err(f"error: that summary is {len(summary)} characters; keep it under 400")
        _err("  A finding is the one line somebody reads later, not the writeup.")
        return EXIT_USAGE
    actor = _actor(flags)
    root, log_file, lock_file = _task_runtime(flags)

    # `--ref path:src/auth.ts`, `--ref pr:#321`. Kind and value, because a bare
    # string cannot say which it is and that is the useful half.
    refs = []
    for raw in _repeated(argv, "ref"):
        kind, _, value = raw.partition(":")
        if not value:
            kind, value = "note", raw
        refs.append({"kind": kind.strip(), "value": value.strip()})

    data: dict = {"category": category, "summary": summary}
    if refs:
        data["refs"] = refs
    with _lock.file_lock(lock_file):
        st = _read_state(log_file)
        greeting = _hello_if_unknown(st, actor)
        event = _event(actor, _log.TYPE_FINDING, None, data)
        if greeting is None:
            _log.append(log_file, event)
        else:
            _log.append_batch(log_file, [greeting, event])
    print(f"{category.upper()} recorded by @{actor}")
    print(f"  {summary}")
    return EXIT_OK


def _cmd_note(argv: list[str]) -> int:
    """A short FYI. Deliberately separate from `find`: a note is something you
    would say out loud and forget, a finding is something the next agent needs
    six weeks from now, and mixing them buries the second in the first."""
    positional, flags = _parse_flags(argv)
    body = " ".join(positional).strip()
    if not body:
        _err("error: note needs something to say")
        _err('  e.g. comms-graph note "schema migration lands next session" --as me')
        return EXIT_USAGE
    if len(body) > 400:
        _err(f"error: that note is {len(body)} characters; keep it under 400")
        return EXIT_USAGE
    actor = _actor(flags)
    root, log_file, lock_file = _task_runtime(flags)
    with _lock.file_lock(lock_file):
        st = _read_state(log_file)
        greeting = _hello_if_unknown(st, actor)
        event = _event(actor, _log.TYPE_NOTE, None, {"body": body})
        if greeting is None:
            _log.append(log_file, event)
        else:
            _log.append_batch(log_file, [greeting, event])
    print(f"NOTED by @{actor}")
    print(f"  {body}")
    return EXIT_OK


def _cmd_next(argv: list[str]) -> int:
    _, flags = _parse_flags(argv)
    actor = _actor(flags)
    _, log_file, _lockf = _task_runtime(flags)
    st = _read_state(log_file)
    if not st.tasks:
        print("no tasks declared yet — add one with `comms-graph task add <id>`")
        return EXIT_OK

    ready = _task.ready_tasks(st.tasks)
    mine_to_review = _task.awaiting_review(st.tasks, actor, st.sessions)
    if not ready and not mine_to_review:
        blocked = [t for t in st.tasks.values() if t.phase == _task.PHASE_BLOCKED]
        cycles = [t for t in st.tasks.values() if t.phase == _task.PHASE_CYCLE]
        if not blocked and not cycles and st.tasks and all(
            t.phase == _task.PHASE_CLOSED for t in st.tasks.values()
        ):
            print(f"all {len(st.tasks)} task(s) are closed — the plan is finished.")
            return EXIT_OK
        print("nothing is startable by you right now.")
        if blocked:
            print(f"  {len(blocked)} task(s) waiting on something unverified:")
            for t in sorted(blocked, key=lambda x: x.id)[:5]:
                print(f"    {t.id} <- {', '.join(t.blocked_by)}")
        if cycles:
            print(f"  {len(cycles)} task(s) in a dependency loop and unreachable: "
                  + ", ".join(sorted(t.id for t in cycles)))
        return EXIT_OK

    if mine_to_review:
        # Review first, deliberately. A task sitting in review blocks everything
        # after it, so an idle agent picking up new work instead of clearing a
        # review is how a plan stalls with everybody busy.
        print("WAITING ON YOUR REVIEW — these block whatever comes after them:")
        for t in mine_to_review:
            print(f"  {t.id}  {t.title}".rstrip())
            print(f"    done by @{t.did}" + (f"; notes: {t.notes[-1]}" if t.notes else ""))
            print(f"    comms-graph task review {t.id} --pass --as {actor}")
    if ready:
        print("READY — nothing upstream is outstanding:")
        for t in ready:
            held = ""
            print(f"  {t.id}  {t.title}{held}".rstrip())
            # "nothing outstanding" is true and can still be misleading: a
            # predecessor may have been signed off by its own author. The agent
            # about to build on it is exactly who needs telling.
            selfsigned = [
                e.from_ for e in _task.incoming(st.task_edges, t.id)
                if (up := st.tasks.get(e.from_)) is not None
                and up.independence == "self-acknowledged"
            ]
            if selfsigned:
                print("      note: " + ", ".join(sorted(selfsigned))
                      + " was signed off by its own author, not independently reviewed")
    return EXIT_OK


def _cmd_brief(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    if not positional:
        _err("error: brief needs a task id")
        return EXIT_USAGE
    slug = _slug_or_exit(positional[0])
    _, log_file, _lockf = _task_runtime(flags)
    st = _read_state(log_file)
    t = st.tasks.get(slug)
    if t is None:
        _err(f"error: no task called {slug!r}")
        if st.tasks:
            _err("  Declared: " + ", ".join(sorted(st.tasks)[:8]))
        return EXIT_USAGE

    print(f"{t.id}  {t.title}".rstrip())
    bits = [f"phase: {t.phase}"]
    if t.size:
        bits.append(f"size: {t.size}")
    if t.doers:
        bits.append("doing: " + ", ".join("@" + d for d in t.doers))
    if t.did and not t.verified_by:
        # Only while it really is awaiting one. Emitting this whenever a doer is
        # recorded meant every CLOSED task announced it was awaiting review in
        # the same breath as naming its verifier.
        bits.append(f"awaiting review of @{t.did}'s work")
    if t.verified_by:
        bits.append(f"verified by @{t.verified_by} ({t.independence})")
    if t.rejections:
        bits.append(f"rejected {t.rejections}x")
    print("  " + " · ".join(bits))
    if t.checks:
        print("  must pass before done: " + ", ".join(t.checks))
    if t.ref:
        print(f"  ref: {t.ref}")

    if t.blocked_by:
        print("  BLOCKED until these are VERIFIED: " + ", ".join(t.blocked_by))
        # And who to chase. "waiting on something unverified" is a fact with no
        # action attached; a task held up by a named person who has not
        # submitted is something the reader can actually do something about.
        for dep in t.blocked_by:
            up = st.tasks.get(dep)
            if up is not None and up.doers:
                print(f"    {dep} is being worked by "
                      + ", ".join("@" + a for a in up.doers))

    # The point of this verb: what the upstream work decided, delivered to
    # whoever picks this up. Without it, a doer's decisions sit on their own
    # node and never reach the next person.
    incoming = _task.incoming(st.task_edges, slug)
    if incoming:
        print("  comes after:")
        for e in incoming:
            up = st.tasks.get(e.from_)
            state = up.phase if up else "not declared"
            # Say it HERE, not only on the task's own line. Whoever picks this
            # up is building against that predecessor's interface, and is the
            # one person who most needs to know it was approved by its author.
            mark = ""
            if up is not None and up.independence == "self-acknowledged":
                mark = f" — signed off by @{up.verified_by} themselves, not independently reviewed"
            print(f"    {e.from_} ({state}) [{e.kind}]"
                  + (f" — provides: {e.provides}" if e.provides else "")
                  + mark)
            if up and up.notes:
                for note in up.notes[-3:]:
                    print(f"        decided: {note}")
            # Kept separate from the author's notes above. Whoever picks this up
            # is trusting that the thing they build against was checked; what
            # the checker actually ran is the difference between trusting the
            # gate and trusting a green tick.
            if up and up.verification:
                print(f"        verified by @{up.verified_by}: {up.verification}")
    if t.findings:
        print("  came back from review with:")
        for f in t.findings:
            print(f"    {f.what}" + (f"  ({f.where})" if f.where else ""))
    if t.notes:
        print("  decisions recorded here:")
        for note in t.notes[-5:]:
            print(f"    {note}")
    return EXIT_OK


def _cmd_plan(argv: list[str]) -> int:
    _, flags = _parse_flags(argv)
    source = flags.get("from")
    if not source:
        _err("error: plan needs --from <file.json>")
        _err('  {"tasks": [{"id": "api", "title": "...", "checks": ["test"]}],')
        _err('   "edges": [{"from": "api", "to": "ui", "kind": "interface", "provides": "..."}]}')
        return EXIT_USAGE
    actor = _actor(flags)
    _, log_file, lock_file = _task_runtime(flags)
    try:
        raw = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        _err(f"error: cannot read {source}: {exc.strerror}")
        return EXIT_USAGE
    except json.JSONDecodeError as exc:
        _err(f"error: {source} is not valid JSON: {exc}")
        return EXIT_USAGE

    if not isinstance(raw, dict):
        _err(f"error: {source} must contain a JSON object, not a "
             f"{type(raw).__name__}")
        _err('  Expected {"tasks": [...], "edges": [...]}.')
        return EXIT_USAGE
    _plan_greeting_holder = [_hello_if_unknown(_read_state(log_file), actor)]
    tasks = raw.get("tasks") or []
    edges = raw.get("edges") or []
    if not isinstance(tasks, list) or not isinstance(edges, list):
        _err("error: 'tasks' and 'edges' must both be lists")
        return EXIT_USAGE

    # Validate the WHOLE plan before a single byte is written. A half-applied
    # plan is worse than a refused one: the graph would claim a shape nobody
    # designed, and the missing half is invisible.
    declared: set[str] = set()
    # Minted FIRST, before any task event exists, because fold() orders by
    # timestamp. Every other command was fixed for this; the plan path was
    # missed, and a real five-agent run caught it — the planner's hello carried
    # a timestamp 426us LATER than the tasks it was appended ahead of, so the
    # log's ids and timestamps disagreed about order.
    events: list = []
    for item in tasks:
        if not isinstance(item, dict):
            _err(f"error: every task must be an object, got {type(item).__name__}")
            return EXIT_USAGE
        slug = _slug_or_exit(str(item.get("id") or ""))
        declared.add(slug)
        data: dict = {"task": slug}
        for key in ("title", "size", "ref"):
            if item.get(key):
                data[key] = str(item[key])
        if item.get("checks"):
            checks = item["checks"]
            if isinstance(checks, str):
                # A string is iterable, so this used to become one check per
                # CHARACTER: "test" turned into t, e, s, t — four checks that
                # can never pass, on a field whose entire job is to gate.
                _err(f"error: task {slug!r} has checks={checks!r}; "
                     "it must be a list, e.g. [\"test\"]")
                return EXIT_USAGE
            if not isinstance(checks, list):
                _err(f"error: task {slug!r} has checks of type "
                     f"{type(checks).__name__}; it must be a list")
                return EXIT_USAGE
            data["checks"] = [str(c) for c in checks]
        events.append(_event(actor, _log.TYPE_TASK, None, data))

    pending: list = []
    for item in edges:
        if not isinstance(item, dict):
            _err(f"error: every edge must be an object, got {type(item).__name__}")
            return EXIT_USAGE
        from_ = _slug_or_exit(str(item.get("from") or ""), "edge 'from'")
        to = _slug_or_exit(str(item.get("to") or ""), "edge 'to'")
        for end in (from_, to):
            if end not in declared:
                _err(f"error: edge names {end!r}, which this plan does not declare")
                return EXIT_USAGE
        if from_ == to:
            _err(f"error: {from_!r} cannot come after itself")
            return EXIT_USAGE
        kind = str(item.get("kind") or _task.EDGE_SEQUENCE).lower()
        if kind not in (_task.EDGE_INTERFACE, _task.EDGE_ARTIFACT, _task.EDGE_SEQUENCE):
            _err(f"error: edge kind {kind!r} is not one of interface, artifact, sequence")
            return EXIT_USAGE
        if _task.would_cycle(pending, from_, to):
            _err(f"error: this plan contains a dependency loop: {from_} -> {to} closes it")
            return EXIT_USAGE
        pending.append(_task.TaskEdge(from_=from_, to=to, kind=kind))
        data = {"from": from_, "to": to, "kind": kind}
        if item.get("provides"):
            data["provides"] = str(item["provides"])
        events.append(_event(actor, _log.TYPE_TASK_EDGE, None, data))

    if not events:
        print("nothing to do: the plan declares no tasks and no edges")
        return EXIT_OK

    with _lock.file_lock(lock_file) as handle:
        broken = handle.compromised()
        if broken:
            _err(f"error: the coordination lock is no longer exclusive: {broken}")
            return EXIT_ERROR
        st = _read_state(log_file)
        # A loop can also be closed against edges ALREADY in the log, not just
        # against the ones in this file.
        existing = list(st.task_edges)
        for e in pending:
            if _task.would_cycle(existing, e.from_, e.to):
                _err(f"error: {e.from_} -> {e.to} would close a loop with edges already recorded")
                return EXIT_USAGE
            existing.append(e)
        greeting = _plan_greeting_holder[0] if _plan_greeting_holder else None
        if greeting is not None:
            events.insert(0, greeting)
        _log.append_batch(log_file, events)
    print(f"PLAN recorded: {len(tasks)} task(s), {len(edges)} edge(s)")
    print("  See what is startable with `comms-graph next`.")
    return EXIT_OK


TASK_USAGE = """Usage: comms-graph task <command>

  add <id> --as <actor> [--title "..."] [--size S|M|L]
                        [--check <name>]... [--ref "omni:AUF-2291"]
  edge <from> <to> --as <actor> [--kind interface|artifact|sequence]
                                [--provides "..."]      <to> comes AFTER <from>
  done <id> --as <actor> [--check name=pass]... [--note "..."]
  review <id> --as <actor> --pass|--fail [--evidence "..."]
                        [--acknowledge-self-review]  last resort: sign off your
                        own work, recorded as self-acknowledged, never verified

  A task unblocks what follows it when it is VERIFIED, not when it is done,
  and you cannot verify your own work."""



def _release_somebody_elses(st, log_file, actor: str, target: str, reason: str) -> int:
    """`release <claim-id> --force --reason "..."` — end a claim that is not yours.

    Deliberately narrow: an exact claim id, never a path. A path can match more
    ground than the reader meant, and the whole point of this verb is that the
    person running it is making a judgement about somebody else's work — that
    judgement should be about one named thing.
    """
    if not target:
        _err("error: release --force needs the exact claim id")
        _err("  It is on the board next to the claim, and in `comms-graph board`.")
        return EXIT_USAGE
    if not reason:
        _err("error: release --force needs --reason saying why it is not theirs to hold")
        _err("  It goes in the log under your name, permanently.")
        return EXIT_USAGE
    held = st.claim_by_id(target)
    if held is None:
        _err(f"error: no active claim with id {target!r}")
        _err("  --force takes a claim id, not a path — a path can match more than you meant.")
        return EXIT_USAGE
    if held.actor == actor:
        _err(f"error: {target} is your own claim — release it without --force")
        return EXIT_USAGE
    _log.append(log_file, _event(actor, _log.TYPE_RELEASE, None, {
        "refs": [held.id],
        "result": reason,
        "original_actor": held.actor,
        "arbitrator": actor,
    }))
    print(f"RELEASED {held.scope}  (was @{held.actor}, id {held.id})")
    print(f"  Freed by @{actor}, not by them. Recorded with your reason.")
    print(f"  If @{held.actor} is still alive it will find the ground gone and can re-claim it.")
    return EXIT_OK


def _cmd_board(argv: list[str]) -> int:
    _, flags = _parse_flags(argv)
    me = (flags.get("as") or os.environ.get("COMMS_ACTOR") or "").strip()
    root = _repo_root(flags.get("root"))
    log_file, _lock_file = _store(root)
    _warn_if_ephemeral(log_file)
    st = _read_state(log_file)
    claims = sorted(st.claims.values(), key=lambda c: c.ts)
    if not claims:
        print("no active claims in this repo")
        return EXIT_OK
    print(f"active claims ({len(claims)}):")
    for claim in claims:
        when = claim.ts.isoformat().replace("+00:00", "Z")
        mark = " <- you" if me and claim.actor == me else ""
        intent = f'  "{claim.intent}"' if claim.intent else ""
        print(f"  {claim.scope}  @{claim.actor}  {when}  [{claim.id}]{intent}{mark}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    """``argv`` is everything after ``comms-graph``."""
    sub = argv[0] if argv else ""
    rest = argv[1:]
    if sub in ("", "-h", "--help", "help"):
        print(USAGE)
        return EXIT_OK if sub else EXIT_USAGE
    # --help on a LEAF verb. Without this, every leaf ignored it: `task done
    # --help` errored with "needs an id", and `board --help` / `tasks --help`
    # ran the command — `tasks` wrote an HTML file. Asking how a verb works is
    # the one thing that must never have a side effect, and for a tool whose
    # users are agents it is the whole discovery path. Exact-match only, so a
    # free-text --intent that mentions --help is unaffected.
    if any(a in ("-h", "--help", "-?") for a in rest):
        print(TASK_USAGE if sub == "task" else USAGE)
        return EXIT_OK
    try:
        if sub == "claim":
            return _cmd_claim(rest)
        if sub == "release":
            return _cmd_release(rest)
        if sub == "board":
            return _cmd_board(rest)
        if sub == "task":
            return _cmd_task(rest)
        if sub == "next":
            return _cmd_next(rest)
        if sub == "brief":
            return _cmd_brief(rest)
        if sub == "tasks":
            return _cmd_tasks(rest)
        if sub == "check":
            return _cmd_check(rest)
        if sub == "ui":
            return _cmd_ui(rest)
        if sub == "plan":
            return _cmd_plan(rest)
        if sub == "find":
            return _cmd_find(rest)
        if sub == "note":
            return _cmd_note(rest)
    except _lock.LockError as exc:
        _err(f"error: could not take the comms lock: {exc}")
        return EXIT_USAGE
    _err(f"error: unknown comms command {sub!r}")
    _err(USAGE)
    return EXIT_USAGE
