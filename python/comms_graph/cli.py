"""``comms-graph``: the surface an agent actually types.

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
    status, and is never phrased as a verdict. See the README section on the code map.

Mixing those two would destroy the value of both: an advisory that blocks gets
switched off, and a hard conflict buried in advice gets skimmed past.

A THIRD REPLY THAT IS NOT SILENCE. A claim whose name resolves to nothing on the
map is reported loudly, with the reason. It must never render as "no
connections": a mistyped symbol and an genuinely isolated one look identical
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
import socket
import shlex
import shutil
import subprocess
import unicodedata
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import hooks as _hooks
from . import identity as _identity
from . import lock as _lock
from . import log as _log
from . import resolve as _resolve
from . import scope as _scope
from . import guard as _guard
from . import tree as _tree
from . import state as _state
from . import verbs as _verbs

# Exit codes. 1 is reserved for "somebody else holds this" so a wrapper script
# can tell a real conflict from a typo without parsing text.
EXIT_OK = 0
EXIT_CONFLICT = 1
EXIT_USAGE = 2
# Same code as a usage error: 2 is "this did not work", as distinct from 1's
# "it worked and the answer is no". Named separately because a compromised lock
# is not the user mistyping something, and a reader of the call site should not
# have to think it was.
EXIT_ERROR = 2


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


#: Repository named by a global --repo/--root, before the verb. Module state
#: rather than an environment write, and reset on every main() entry.
_GLOBAL_ROOT: str | None = None

#: Booleans of the verb being run, so `_parse_flags` does not hand a flag the
#: positional that follows it. Set per call by `main`.
_BOOLEANS: frozenset[str] = frozenset()


def _repo_root(explicit: str | None) -> Path:
    """The key the store is hashed by, so every agent in one checkout agrees.

    The git root, not the cwd: two agents run from different subdirectories of
    the same repository all the time, and keying on the cwd would silently give
    them two separate logs that cannot see each other's claims: coordination
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
    # Then a global --repo/--root given before the verb, then COMMS_REPO. Both
    # exist for the same reason: when the process cannot read its own working
    # directory: routine for a checkout under Desktop or Documents on macOS,
    # where privacy access can be withdrawn from a running process: naming the
    # repository is the only way to keep working. The flag wins over the
    # variable, so what you typed on this line beats what you exported earlier.
    #
    # Both are validated exactly like --root: a path that is not a directory is
    # refused rather than used. A typo that is silently accepted opens a private
    # ledger: the command succeeds, the claim records, and the agent is told it
    # holds ground on a log no peer will ever read.
    for value, label in ((_GLOBAL_ROOT, "--repo"),
                         (os.environ.get("COMMS_REPO", "").strip(), "COMMS_REPO")):
        if not value:
            continue
        chosen = Path(value).expanduser().resolve()
        if not chosen.is_dir():
            _err(f"error: {label} {value!r} is not a directory")
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
    # and the commonest places to hit this: a scratch directory, an unpacked
    # tarball, a freshly scaffolded project: are exactly where somebody is
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
    half of the reply, which is then said out loud; the exact half: the claim
    conflict: does not depend on the map at all.
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
        if _verbs.is_flag(arg):
            if "=" in arg:
                key, value = arg[2:].split("=", 1)
                flags[key] = value
                i += 1
            else:
                key = arg[2:]
                value = ""
                if key not in _BOOLEANS and i + 1 < len(argv) and not _verbs.is_flag(argv[i + 1]):
                    value = argv[i + 1]
                flags[key] = value
                i += 2 if value else 1
        else:
            positional.append(arg)
            i += 1
    # ONE place, for every verb. Every surface PRINTS an actor as "@name", so
    # "@name" is exactly the spelling an agent copies back out of `status` and
    # passes to --as: the tool trains you into the broken form. Stripping it in
    # `_actor` alone was not enough: `check --staged` and the hook path read the
    # flag directly, so `--as @me` compared "@me" against a stored "me" and
    # reported an agent's own eleven files as "claimed by somebody else", while
    # naming that somebody as itself in the same breath.
    #
    # The @ is display. It never reaches identity, whichever verb is asking.
    for key in ("as", "actor"):
        if key in flags:
            flags[key] = flags[key].strip().lstrip("@").strip()
    return positional, flags


def _actor(flags: dict[str, str], *, write: bool = True) -> str:
    """Who is calling. See ``identity`` for the order; '' only when not writing."""
    return _identity.cli_actor(flags, write=write)


def _store(root: Path, *, create: bool = True) -> tuple[Path, Path]:
    """The log and lock paths for a repo. Only creates the store when writing.

    `check` is a pure read and must not mkdir. It did, and because the hook
    wrapper turns any exception into a block, one unwritable data home: a
    read-only HOME, a file where the store directory should be: denied every
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
            # and nothing on the machine can say which project it belongs to:
            # the Go build writes it, this one did not, and a board listing
            # every project on the machine would have shown ours as UNNAMED.
            _log.write_repo_path(store, root)
        except OSError as exc:
            # A raw traceback here exited 1: the code the usage text reserves
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


def _tagged(st, actor: str, etype: str, scope: list[str] | None, data: dict) -> _log.Event:
    """An event stamped with the actor's named session, if it is in one.

    Every append goes through here rather than through `_event` directly. An
    event that skips the stamp is invisible to its session: `session end` will
    not release the claim, the archive's counts come up short, and a reader
    asking what that window produced gets an answer that is quietly wrong. On
    the real store 8,698 events carry the pair, so an untagged one is the
    visible difference against the Go build.
    """
    from . import session as _session

    _session.stamp(st, actor, data)
    return _event(actor, etype, scope, data)


def _event(actor: str, etype: str, scope: list[str] | None, data: dict) -> _log.Event:
    if etype == _log.TYPE_CLAIM and "agent_session" not in data and _identity.host_session():
        # Ownership on the edit path is by session; a name alone can be shared.
        data = {**data, "agent_session": _identity.host_session()}
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
    writer appears: a hook that hangs is worse than one that answers wrongly,
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
      DIRECTORIES above it, so a claim on the directory `src` did not block an
      edit to `SRC/newfile.py`, and two agents could claim one not-yet-created
      file under two spellings.
    * Canonicalise the ROOT the same way before making the path relative to it.
      Comparing the kernel's fully-normalised answer against a raw
      os.path.realpath silently threw the whole canonicalisation away whenever
      the two disagreed: a differently-cased repo root, or a
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
            # Resolved outside the repo: a symlink out, most likely. Not ours
            # to rename; the caller's outside-the-repo guard handles it.
            return "/".join(parts)

    # Fallback for platforms without F_GETPATH: read the directories, exact
    # match always winning over a case variant. Only a case-blind filesystem
    # can hold a name under another spelling, so a case-sensitive one is never
    # listed: listing costs O(entries) on every edit.
    here = root
    out: list[str] = []
    blind: bool | None = None
    for part in parts:
        actual = part
        try:
            if blind is None:
                blind = _case_blind_here(here)
            names = ({e.name for e in here.iterdir()}
                     if blind and here.is_dir() else {part})
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

    The probe swaps the case of a directory's own name and compares inodes:
    but it can only do that on a name that HAS cased letters, and the first
    version gave up when the deepest existing directory did not. Directories
    named `2026`, `20260820`, `0005` are ordinary (dates, ticket numbers,
    version folders) and every one of them silently switched the whole
    protection off on a filesystem that is genuinely case-blind. So it walks UP
    until it finds an ancestor it can actually ask about.

    Case-sensitivity is a property of the volume, so any ancestor on the same
    volume answers the same question. The exception is a differently-mounted
    subtree: a case-sensitive image mounted inside a case-blind checkout: and
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


def _uncreated_twin(st, scope_str: str, root: Path, is_mine):
    """A live claim naming the same not-yet-created file under another spelling.

    THE HOLE THIS CLOSES. Everything below the deepest existing directory is
    kept exactly as typed, because a file with no spelling on disk has nothing
    to defer to, so on a case-blind filesystem `src/NewFeature.py` and
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
        if is_mine(claim):
            continue
        other = str(claim.scope).split("#", 1)[0]
        if other != path_part and _same_file_by_name(other, path_part):
            return claim
    return None


def _rooted_scope(raw: str, root: Path) -> str:
    """Re-express a scope's path relative to the repo root.

    THE BUG THIS EXISTS TO PREVENT. The STORE is keyed by the git root, so two
    agents in one checkout share a board: see :func:`_repo_root`. The scope
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

    The anchor (``#symbol`` or ``#L10-40``) is split off untouched: it is not a
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


def _warn_missing_dir(scope_str: str, root: Path) -> None:
    """One line when the claimed path's directory is not there: usually a typo."""
    path_part = scope_str.split("#", 1)[0]
    parent = (root / path_part).parent
    if parent != root and not parent.exists():
        _err(f"warning: {parent.relative_to(root)}/ does not exist under {root.name}; "
             "claimed anyway (a new directory is fine, a typo is not).")


def _single_scope_or_exit(positional: list[str], verb: str) -> str:
    """Take exactly one scope, and refuse the spellings that silently lost work.

    Both refusals are here because both used to exit 0 having recorded less than
    the user asked for:

    * ``claim a.py b.py c.py`` kept ``a.py`` and dropped the rest. The agent was
      told it had claimed, believed it held its whole working set, and left two
      files advertised as free ground for somebody else to take.
    * ``claim "a.py,b.py"`` recorded the comma string as ONE scope. It matches no
      file, so it can never conflict with anything and never protects anything:
      a claim that is inert by construction.
    """
    if len(positional) > 1:
        _err(f"error: {verb} takes one scope, but {len(positional)} were given: "
             + ", ".join(repr(p) for p in positional))
        _err(f"  Claim them one at a time: earlier versions kept only {positional[0]!r} "
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

    WHY THIS EXISTS AT ALL: the integration failure it fixes. The pre-edit hook
    that actually ENFORCES claims is a separate process with no COMMS_ACTOR of
    its own: it inherits the session environment, not the per-command prefix
    agents use. To learn who is asking, it looks up the agent session id from the
    hook payload against the ``hello`` events in the log, and takes that actor.

    This CLI never wrote a hello. So the lookup found nothing, the hook fell back
    to a sentinel actor that matches nobody, and every claim in the repo: the
    caller's OWN included: came back as somebody else's. The result was the
    exact opposite of the tool's purpose: an agent claimed a file and was then
    blocked from editing the file it had just claimed, with a conflict message
    naming itself. Observed in a four-agent run; every one of them would have
    hit it.

    Written implicitly rather than left to a ``hello`` verb because nothing tells
    an agent to run one: the always-on instructions describe claim and release
    and nothing else, and an identity record that agents must remember to write
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
    # Who made this agent, shown on the board. Read from the environment
    # because nothing else can know it, and left ABSENT when unset rather than
    # guessed from a naming convention.
    vendor = os.environ.get("COMMS_VENDOR", "").strip()
    if vendor:
        data["vendor"] = vendor
    try:
        data["hostname"] = socket.gethostname()
    except Exception:
        pass
    # Stamped like any other event. Without this an automatic greeting wiped
    # the actor's own session out of the fold: a hello replaces the session
    # record, so an unstamped one silently removed the actor from the window it
    # had just joined, and everything it wrote afterwards was invisible to it.
    return _tagged(st, actor, _log.TYPE_HELLO, None, data)


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


def _claim_obstacles(st, root: Path, scope, scope_str: str, actor: str,
                     agent_session: str) -> tuple[list, list]:
    """What stands in the way of a CLI claim, and the releases that clear the rest.

    A claim under the caller's name never blocks it. A claim under any other
    name does, even from the caller's own session: names are the only thing
    that separates a lead from its subagents. The one exception is the session's
    own auto-claim, which the hand claim supersedes. Writes nothing, so a
    refused claim leaves the log as it was.
    """
    def mine(claim) -> bool:
        return claim.actor == actor

    overlapping = sorted((c for c in st.claims.values() if _scope.overlaps(c.scope, scope)),
                         key=lambda c: c.ts)
    candidates = [c for c in overlapping
                  if not mine(c) or (c.auto and c.actor != actor)]
    conflicts, releases = _hooks.give_way(st, root, candidates, actor, agent_session)
    if not conflicts:
        twin = _uncreated_twin(st, scope_str, root, mine)
        if twin is not None:
            conflicts = [twin]
    return conflicts, releases


def _render_conflicts(conflicts: list, scope_str: str) -> str:
    lines = [
        "CLAIM CONFLICT: not recorded. Somebody else already holds this ground.",
        f"  you asked for: {scope_str}",
    ]
    for claim in conflicts:
        when = claim.ts.isoformat().replace("+00:00", "Z")
        intent = f'  "{claim.intent}"' if claim.intent else ""
        lines.append(f"  @{claim.actor} holds {claim.scope} since {when}{intent}")
    lines.append("  This one is exact, not a guess: it is read out of the log.")
    lines.append("  Claim something narrower, or agree with them who takes it.")
    return "\n".join(lines)


def _cmd_claim_many(positional: list[str], flags: dict) -> int:
    """Claim several scopes atomically: all of them, or none of them.

    One change is usually more than one file: the route, the handler and
    its test, and taking them one at a time is not the same thing. Half a
    boundary is the bad state: you hold two of three, somebody else holds the
    third, and neither of you can finish without giving something back.

    So every scope is validated and conflict-checked BEFORE a single event is
    written, and the claims land in one append. The single-scope path below is
    untouched by this and stays the common case.

    Deliberately NOT supported here: `--steal`. Displacing somebody names one
    claim id, which cannot mean anything across several scopes. The Go build
    refuses the same combination for the same reason.
    """
    if flags.get("steal"):
        _err("error: --steal takes exactly one scope")
        _err("  A claim id names one claim, so it cannot say what to displace "
             "across several. Steal that one, then claim the rest.")
        return EXIT_USAGE
    intent = flags.get("intent", "")
    if not intent:
        # Required for a batch specifically: one intent has to explain why these
        # scopes belong together, or the board shows N unrelated rows and the
        # reason they were taken as a unit is nowhere.
        _err("error: claiming several scopes at once needs --intent saying what "
             "they have in common")
        return EXIT_USAGE

    actor = _actor(flags)
    root = _repo_root(flags.get("root"))
    seen: dict[str, str] = {}
    scopes = []
    for raw in positional:
        scope_str = _rooted_scope(raw, root)
        _warn_missing_dir(scope_str, root)
        scope = _parse_scope_or_exit(scope_str)
        key = str(scope)
        if key in seen:
            _err(f"error: {raw!r} and {seen[key]!r} are the same ground")
            _err("  Claiming one scope twice in a batch would record two rows for "
                 "one file and make the board's count wrong.")
            return EXIT_USAGE
        seen[key] = raw
        scopes.append((scope_str, scope))

    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)

    with _lock.file_lock(lock_file) as handle:
        st = _read_state(log_file)
        # EVERY scope first. The whole point is that nothing is written until we
        # know all of them are free.
        blocked: list[tuple[str, object]] = []
        releases: dict[str, object] = {}
        for scope_str, scope in scopes:
            conflicts, gives_way = _claim_obstacles(
                st, root, scope, scope_str, actor, _identity.host_session())
            for event in gives_way:
                releases.setdefault(event.data["refs"][0], event)
            if conflicts:
                blocked.append((scope_str, conflicts[0]))

        if blocked:
            for scope_str, holder in blocked:
                try:
                    _log.append(log_file, _event(actor, _log.TYPE_BLOCKED, None, {
                        "scope": scope_str, "intent": intent,
                        "holder": holder.actor, "holder_scope": str(holder.scope),
                    }))
                except Exception:
                    pass
            _err(f"CLAIM CONFLICT: nothing was recorded. {len(blocked)} of "
                 f"{len(scopes)} scopes are held by somebody else:")
            for scope_str, holder in blocked:
                extra = f'  "{holder.intent}"' if holder.intent else ""
                _err(f"  {scope_str}: @{holder.actor}{extra}")
            _err("  The batch is all-or-nothing, so your other scopes are still "
                 "free. Narrow the set, or agree with them who takes what.")
            return EXIT_CONFLICT

        broken = handle.compromised()
        if broken:
            _err(f"error: the coordination lock is no longer exclusive: {broken}")
            _err("  Nothing was recorded.")
            return EXIT_ERROR

        claim_data: dict = {"intent": intent}
        greeting = _hello_if_unknown(st, actor)
        batch = [greeting] if greeting is not None else []
        batch.extend(releases.values())
        superseded = [c for c in st.claims.values()
                      if c.actor == actor and str(c.scope) in seen]
        if superseded:
            batch.append(_tagged(st, actor, _log.TYPE_RELEASE, None, {
                "refs": [c.id for c in superseded],
                "result": "superseded by a new claim on the same ground",
            }))
        events = [_tagged(st, actor, _log.TYPE_CLAIM, [str(scope)], dict(claim_data))
                  for _scope_str, scope in scopes]
        _log.append_batch(log_file, batch + events)

    for (scope_str, scope), event in zip(scopes, events):
        print(f"CLAIMED {scope} by @{actor}  (id {event.id})")
    print(f"  {len(events)} scopes taken together as one boundary: {intent}")
    return EXIT_OK


def _cmd_claim(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    if not positional:
        _err("error: claim needs a scope, e.g. comms-graph claim src/foo.py#parse --as me")
        return EXIT_USAGE
    if len(positional) > 1:
        return _cmd_claim_many(positional, flags)
    scope_str = _single_scope_or_exit(positional, "claim")
    actor = _actor(flags)
    intent = flags.get("intent", "")
    root = _repo_root(flags.get("root"))
    scope_str = _rooted_scope(scope_str, root)
    _warn_missing_dir(scope_str, root)
    scope = _parse_scope_or_exit(scope_str)
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)

    # The whole read-fold-append cycle under one lock. Folding outside it would
    # let two agents both read "no conflict" and both append, which is precisely
    # the collision this command exists to prevent.
    with _lock.file_lock(lock_file) as handle:
        st = _read_state(log_file)
        conflicts, gives_way = _claim_obstacles(
            st, root, scope, scope_str, actor, _identity.host_session())
        # Taking ground off somebody who is not coming back. The fold has always
        # understood this: a claim carrying `steals` displaces the named one
        # atomically, and nothing wrote it, so an abandoned claim could only be
        # ended by the agent that left.
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
                    _err("  Nothing is blocking you here: claim it without --steal.")
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
        # write a claim on the same scope. Fail closed: refusing a claim costs
        # one retry, recording a second holder costs two agents editing at once.
        broken = handle.compromised()
        if broken:
            _err(f"error: the coordination lock is no longer exclusive: {broken}")
            _err(f"  Nothing was recorded. Check {lock_file} and try again.")
            return EXIT_ERROR
        claim_data: dict = dict(claim_data_steal)
        if intent:
            claim_data["intent"] = intent
        # Minted BEFORE the event it explains. fold() sorts by TIMESTAMP, so
        # putting the greeting first in the list is not enough: it has to be
        # first in time. It was created second, landed second, and every reader
        # that needed the session saw an actor with no hello on record yet.
        greeting = _hello_if_unknown(st, actor)
        event = _tagged(st, actor, _log.TYPE_CLAIM, [str(scope)], claim_data)
        # Re-claiming ground you already hold REPLACES your claim on it; it does
        # not add a second. Two rows for one file made the board's claim count
        # wrong and left it ambiguous which intent was current.
        #
        # Recorded as a release rather than dropped in the fold, so the audit
        # trail says what happened. Exact scope only: a file and a symbol inside
        # it are different ground and holding both is legitimate.
        superseded = [c for c in st.claims.values()
                      if c.actor == actor and str(c.scope) == str(scope)]
        batch = ([greeting] if greeting is not None else []) + gives_way
        if superseded:
            batch.append(_tagged(st, actor, _log.TYPE_RELEASE, None, {
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
        print(f"  TAKEN FROM @{stolen_from_actor}: their claim {claim_data['steals']} is ended.")
        print("  Recorded as a steal under your name, with your reason. If they come")
        print("  back and re-claim it, they will be told the same way you were.")
    advice = _advice(root, flags.get("graph"), scope, others) if "map" in flags else ""
    if advice:
        print(advice)
    return EXIT_OK


def _staleness_note(root: Path, map_path: Path, rel_path: str) -> str:
    """Say when the map predates the code it is being asked about.

    README-COMMS.md sells this as one of the tool's honest limits: "a claim
    pointing at code that changed since the last graphify extract is reported as
    out of date, not as truth". The implementation existed: view._stale_note:
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
                "  OUT OF DATE: " + rel_path + " changed after the map was built, "
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
                "  OUT OF DATE: the map is older than the newest code in this repo "
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
    """The contact block: a prompt to look, never a verdict, never an order.

    SILENT WHEN IT HAS NOTHING TO SAY, which used to be most of the time. This
    printed three lines of NOT ON THE MAP on every claim in any repo without a
    graphify map, including when nobody else held anything at all, so the
    commonest claim in the commonest repo carried a paragraph explaining why a
    check nobody had asked for could not be run. An agent that made ~9 claims
    reported reading none of it and using the map zero times.

    The check only ever answers one question: does the ground you just took sit
    next to ground somebody ELSE is holding. With no other claims there is no
    question, so there is no answer worth printing, whatever the map says.
    """
    # Nothing to be in contact with. This is the case that was drowning the rest.
    if not others:
        return ""

    map_path = _graph_path(root, graph_flag)
    graph = _load_graph(map_path)
    stale = _staleness_note(root, map_path, getattr(scope, "path", "") or "") if graph is not None else ""
    mine = _resolve.resolve(graph, scope, root)
    if mine.miss_reason:
        # Still said, because somebody else IS holding ground and "we could not
        # check" must never be mistaken for "you are clear". One line, though:
        # the three-line version was explaining itself to nobody.
        held = f"{len(others)} other claim" + ("s" if len(others) != 1 else "")
        return (f"  not on the map, so contact with the {held} could not be "
                f"checked: {mine.miss_reason}")
    resolved_others = []
    for actor, scope_str, other_scope in others:
        res = _resolve.resolve(graph, other_scope, root)
        resolved_others.append((actor, scope_str, res))
    from . import contact as _contact

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
        # ways were to steal it and hand it back: two writes, and you briefly
        # own work you did not do, or to pass `--as` their name, which the CLI
        # accepts and which writes a lie into an append-only log. A person
        # clearing up after a crashed agent should not have to impersonate it.
        #
        # It says whose it was and who took it, under the real name, with a
        # reason. The fold needed nothing: a release is popped by claim id and
        # has never cared who wrote it.
        force = "force" in flags
        if force:
            return _release_somebody_elses(
                st, log_file, actor, target, flags.get("reason", "").strip())
        mine = sorted((c for c in st.claims.values() if c.actor == actor), key=lambda c: c.ts)
        targets = [p for p in positional if p]
        if not targets:
            if not mine:
                print(f"nothing to release: @{actor} holds no claims here")
                also = _identity.other_names_line(st, _identity.host_session(), actor)
                if also:
                    print(also)
                return EXIT_OK
            chosen = mine[-1:] if "latest" in flags else mine
            unmatched: list[str] = []
        else:
            chosen, unmatched = [], []
            for target in targets:
                found: list = []
                by_id = st.claim_by_id(target)
                if by_id is not None and by_id.actor == actor:
                    found = [by_id]
                else:
                    # Rooted the same way claim roots it, and for the same
                    # reason: a claim taken at the repo root must be releasable
                    # from a subdirectory. Only reached when the target was not a
                    # claim id, so an id is never mangled by path resolution.
                    # The canonical spelling AND the raw one: after a case-only
                    # rename the frozen scope and the disk disagree, and the
                    # holder must still be able to release her own claim.
                    for spelling in (_rooted_scope(target, root), target):
                        wanted = _parse_scope_or_exit(spelling)
                        found = [c for c in mine if _scope.overlaps(c.scope, wanted)]
                        if found:
                            break
                if not found:
                    unmatched.append(target)
                chosen.extend(c for c in found if c not in chosen)
        if unmatched:
            _err("error: @" + actor + " holds nothing matching "
                 + ", ".join(repr(t) for t in unmatched))
            if mine:
                shown = ", ".join(str(c.scope) for c in mine[:8])
                _err("  Held: " + shown + (f" (+{len(mine) - 8} more)" if len(mine) > 8 else ""))
            else:
                _err(f"  @{actor} holds no claims here.")
            also = _identity.other_names_line(st, _identity.host_session(), actor)
            if also:
                _err("  " + also)
        if chosen:
            data: dict = {"refs": [c.id for c in chosen]}
            if flags.get("result"):
                data["result"] = flags["result"]
            _log.append(log_file, _tagged(st, actor, _log.TYPE_RELEASE, None, data))

    for claim in chosen:
        print(f"RELEASED {claim.scope}  (was @{claim.actor}, id {claim.id})")
    return EXIT_CONFLICT if unmatched else EXIT_OK


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------



def _runtime(flags: dict) -> tuple[Path, Path, Path]:
    root = _repo_root(flags.get("root"))
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)
    return root, log_file, lock_file



# ---------------------------------------------------------------------------
# check: the pre-edit hook
# ---------------------------------------------------------------------------

#: Claude Code reads exit 2 from a PreToolUse hook as "block this tool call and
#: show stderr to the model". EVERY OTHER non-zero code means "the hook itself
#: errored" and the edit proceeds anyway. So 2 is the only code that stops an
#: edit, and anything we cannot answer confidently must also be 2: a
#: coordination tool that cannot read its log has not established that the file
#: is free, and waving the edit through would be the one wrong direction to fail
#: in.
EXIT_BLOCK = 2


def _repo_root_for_file(target: Path) -> Path | None:
    """The repository a FILE belongs to, not the one the process is standing in.

    A hook does not run where the file lives. The host agent's working directory
    is wherever the session was started, routinely a parent folder holding
    several checkouts and very often not a repository at all, so resolving from
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


def _cmd_ui(argv: list[str]) -> int:
    """Serve the live board until interrupted."""
    _, flags = _parse_flags(argv)
    root, log_file, _lockf = _runtime(flags)
    host = flags.get("host") or "127.0.0.1"
    try:
        port = int(flags.get("port") or 7878)
    except ValueError:
        _err(f"error: --port {flags['port']!r} is not a number")
        return EXIT_USAGE

    from . import runtime_refresh as _runtime_refresh
    from . import server as _server

    try:
        httpd = _server.serve(root, log_file, host=host, port=port,
                              graph_file=flags.get("graph"))
    except OSError as exc:
        _err(f"error: cannot listen on {host}:{port}: {exc.strerror or exc}")
        _err("  Something else is probably already using it: try --port 7879.")
        return EXIT_ERROR

    print(f"comms board on http://{host}:{port}  (ctrl-c to stop)")
    print(f"  watching {log_file}")
    code_watcher = None
    try:
        code_watcher = _runtime_refresh.SourceCodeWatcher(
            Path(_runtime_refresh.__file__).resolve().parent,
            httpd.shutdown,
        )
        code_watcher.start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        if code_watcher is not None:
            code_watcher.close()
        httpd.server_close()
    if code_watcher is not None and code_watcher.restart_requested:
        print("installed comms code changed; refreshing the dashboard process", flush=True)
        _runtime_refresh.restart_current_process()
    return EXIT_OK


def _cmd_check(argv: list[str]) -> int:
    """The hook entry point. NOTHING may escape this alive.

    Exit 2 is the only code that stops an edit; every other non-zero code tells
    Claude Code the hook itself errored, and the edit proceeds. So an unhandled
    exception here does not merely produce an ugly traceback: it turns the
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
    except BaseException as exc:  # noqa: BLE001: deliberately everything
        _err(f"check: refusing to answer: {type(exc).__name__}: {exc}")
        _err("  comms could not establish that this path is free, so it is not "
             "saying that it is.")
        return EXIT_BLOCK


def _amending(root: Path) -> bool:
    """Is the commit we are guarding an --amend?

    Git tells a pre-commit hook nothing about this: GIT_REFLOG_ACTION is empty
    here, and there is no flag in the environment. The parent process is the
    only witness, and the shipped hook ends in `exec`, which REPLACES the shell,
    so our parent is git itself.
    """
    try:
        out = subprocess.run(["ps", "-o", "args=", "-p", str(os.getppid())],
                             capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--amend" in out.stdout.decode("utf-8", "replace")


def _head_subject(git: str, root: Path) -> str:
    """`abc1234 their subject line`, for naming the commit an amend would eat."""
    try:
        out = subprocess.run([git, "log", "-1", "--pretty=format:%h %s"],
                             cwd=root, capture_output=True, timeout=10)
        line = out.stdout.decode("utf-8", "replace").strip()
        return line or "the commit at HEAD"
    except (OSError, subprocess.SubprocessError):
        return "the commit at HEAD"


def _head_paths(git: str, root: Path) -> list[str]:
    """The paths the commit at HEAD touches, which an amend would rewrite."""
    try:
        out = subprocess.run(
            [git, "show", "--name-only", "--pretty=format:", "-z", "HEAD"],
            cwd=root, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def _cmd_check_staged(flags: dict) -> int:
    """Refuse a commit whose git index touches somebody else's claimed files.

    A DIFFERENT exit code on purpose. The hook path must answer 2, because that
    is the only code Claude Code reads as "block this tool call". Git is the
    opposite: a pre-commit hook aborts on ANY non-zero, so a conflict answers 1
    and 2 is kept for "comms could not find out", which also aborts, and should,
    because a guard that cannot read its log has not established anything.

    Every blocked path is listed, not just the first. A commit guard that reports
    one file at a time turns one fix into five commits.
    """
    root = _repo_root(flags.get("root"))
    if not (root / ".git").exists():
        print(f"check --staged: {root} is not a git repository, so nothing is staged.")
        return EXIT_OK
    git = shutil.which("git")
    if git is None:
        _err("check --staged: git is not installed, so the index cannot be read")
        return EXIT_BLOCK
    try:
        out = subprocess.run(
            [git, "diff", "--cached", "--name-only", "-z"],
            cwd=root, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _err(f"check --staged: cannot read the git index: {exc}")
        return EXIT_BLOCK
    if out.returncode != 0:
        _err(f"check --staged: git exited {out.returncode}: "
             f"{out.stderr.decode('utf-8', 'replace').strip()}")
        return EXIT_BLOCK

    staged = [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]

    # AN AMEND REWRITES HEAD, AND THE INDEX SAYS NOTHING ABOUT HEAD.
    #
    # Reported with the exact sequence. An agent was blocked correctly, unstaged
    # the peer files it was told to, and retried `git commit --amend`. In the
    # seconds between, the peer committed. The retry ran with an empty index, so
    # this check answered "nothing is staged, so there was nothing to check" and
    # the amend rewrote the peer's seconds-old commit. The content survived by
    # luck, precisely because the index was empty. The hash did not.
    #
    # On an amend the commit at HEAD is at risk too, so its paths are checked
    # exactly like staged ones. Also when nothing is staged at all: an ordinary
    # commit is refused outright in that state, so anything still proceeding is
    # an amend or --allow-empty, and looking costs nothing.
    amend_paths: list[str] = []
    in_git_hook = bool(os.environ.get("GIT_INDEX_FILE"))
    if _amending(root) or (not staged and in_git_hook):
        amend_paths = _head_paths(git, root)

    if not staged and not amend_paths:
        # Say WHY it passed. Silence on exit 0 cannot distinguish "checked them,
        # all clear" from "there was nothing to check", and an agent testing the
        # guard against a peer-held path read the second as a false negative and
        # nearly filed it as a bug. The commonest cause is the honest one: the
        # peer already committed, so `git add` produced no index entry.
        print("check --staged: nothing is staged, so there was nothing to check.")
        return EXIT_OK

    log_file, _lock_file = _store(root, create=False)
    try:
        log_file.stat()
    except FileNotFoundError:
        # Third way to exit 0 without checking anything, and the quietest.
        # Nobody has ever coordinated in this repo, so there is nothing to
        # collide with, but a silent pass here looks identical to a real one.
        print("check --staged: no coordination log in this repo yet, "
              "so there are no claims to collide with.")
        return EXIT_OK
    except OSError as exc:
        _err(f"check --staged: cannot reach the coordination log: {exc.strerror or exc}")
        return EXIT_BLOCK
    try:
        st = _read_state(log_file)
    except Exception as exc:
        _err(f"check --staged: cannot read the coordination log: {exc}")
        return EXIT_BLOCK

    # Identity, resolved the way the EDIT hook resolves it, because a git hook
    # has no environment of its own either. Without this the caller is a stranger
    # to its own claims: an agent that claimed its files, wrote them and staged
    # them was told "3 staged file(s) are claimed by somebody else" and handed a
    # command to unstage its own work. That is not a small bug: it rewards not
    # claiming, which is the one behaviour the tool exists to encourage.
    #
    # `--as` cannot be the answer on its own: a pre-commit hook does not know
    # which agent is committing, so it has nothing to pass.
    actor = _actor(flags, write=False)
    agent_session = _identity.host_session()
    unidentified = not (actor or agent_session)

    # Carry WHERE each path came from. A path in HEAD is not a staged path, and
    # telling somebody to `git restore --staged` a file that is not in their
    # index is the same failure as every other one on this list: right that
    # something is wrong, wrong about what.
    from_amend = {p for p in amend_paths if p not in staged}
    blocked: list[tuple[str, object]] = []
    for rel_text in list(staged) + sorted(from_amend):
        # Spelled the way the filesystem spells it, exactly as the hook path
        # does: a claim on src/a.py must still catch a staged src/A.py where
        # those are one file.
        target = (root / rel_text)
        try:
            canon = _canonical_relpath(target.resolve(), root.resolve())
        except (ValueError, OSError):
            canon = rel_text
        conflicts = _identity.foreign(st, _scope.parse(canon), agent_session, actor)
        if conflicts:
            blocked.append((canon, conflicts[0]))

    if not blocked:
        # "Nothing staged is claimed by anybody else" was the whole answer, and
        # it passed a commit carrying a staged DELETION left behind by an agent
        # that had already released. The check was true and the deletion
        # shipped: unclaimed is not the same as yours.
        survey = _tree.attribute(_tree.read(root), st)
        foreign = ([] if unidentified
                   else _tree.foreign_staged_deletions(survey, actor))
        if foreign:
            _err(f"BLOCKED: {len(foreign)} staged deletion(s) remove files the log "
                 "ties to somebody else.")
            for a in foreign:
                _err(f"  {a.change.path}: last {a.basis} by @{a.actor}")
            _err("  You are about to commit somebody else's removal. Unstage it, "
                 "or confirm with them that it was meant to go:")
            for a in foreign:
                _err(f"    git restore --staged -- "
                     f"{shlex.quote(':(literal)' + a.change.path)}")
            return EXIT_CONFLICT

        n = len(staged)
        print(f"check --staged: {n} staged path{'s' if n != 1 else ''} checked, "
              f"none held by anybody else.")
        # Their own suggestion, and the right one: a commit is the last point at
        # which taking somebody's fix along costs nothing. Recent messages are
        # restated here even if an ordinary command already delivered them.
        if not unidentified:
            _deliver_addressed_notes(root, actor, restate=True)
        # Said out loud rather than passed over. These are the paths the log
        # knows nothing about, which after a compaction or a run of shell
        # heredoc edits is most of them, and a guard that stays silent about
        # what it could not check reads as a guard that checked everything.
        unknown = [a for a in survey.staged if not a.known]
        if unknown:
            was = "is" if len(unknown) == 1 else "are"
            print(f"  {len(unknown)} of them {was} claimed by nobody, so nothing "
                  "was verified about who wrote them:")
            for a in unknown[:12]:
                print(f"    {a.change.path}  {a.change.how()}")
            if len(unknown) > 12:
                print(f"    ... and {len(unknown) - 12} more")
        return EXIT_OK

    if unidentified:
        # Something staged is claimed, and we do not know whether it is yours.
        # Saying "claimed by somebody else" here would be a guess stated as a
        # fact, and the guess is wrong exactly for the agent that did the right
        # thing. Report the identity problem instead, and use 2: this command's
        # code for "could not find out": rather than 1, which means conflict.
        _err("check --staged: cannot establish who is committing, so it cannot "
             "tell your own claims from somebody else's.")
        for rel_text, holder in blocked:
            _err(f"  {rel_text}: claimed by @{holder.actor}")
        _err("  Run `comms-graph hello` in this session so the log knows you, or "
             "pass --as <actor>, or set COMMS_ACTOR.")
        return EXIT_BLOCK

    # Before the first commit there is no HEAD to restore FROM, so `git restore
    # --staged` fails there and `git rm --cached` is the one that works. Getting
    # this wrong hands somebody a command that errors at the exact moment they
    # are trying to unpick a blocked commit.
    head = subprocess.run([git, "rev-parse", "--verify", "-q", "HEAD"],
                          cwd=root, capture_output=True)
    unstage = "git restore --staged" if head.returncode == 0 else "git rm --cached -f"

    in_head = [b for b in blocked if b[0] in from_amend]
    in_index = [b for b in blocked if b[0] not in from_amend]

    if in_index:
        _err(f"BLOCKED: {len(in_index)} staged file(s) are claimed by somebody else.")
        for rel_text, holder in in_index:
            intent = f'  "{holder.intent}"' if holder.intent else ""
            _err(f"  {rel_text}: @{holder.actor}{intent}")
        _err("  Unstage them, or agree with the holder before committing their work:")
        for rel_text, _holder in in_index:
            # :(literal) so a path containing *, ? or [ ] is treated as the name
            # it is rather than as a glob that could unstage other files too.
            _err(f"    {unstage} -- {shlex.quote(':(literal)' + rel_text)}")

    if in_head:
        subject = _head_subject(git, root)
        _err(f"BLOCKED: this amend would rewrite {subject}, which touches ground "
             "somebody else is holding.")
        for rel_text, holder in in_head:
            intent = f'  "{holder.intent}"' if holder.intent else ""
            _err(f"  {rel_text}: @{holder.actor}{intent}")
        _err("  Nothing of yours is staged, so there is nothing to unstage: the")
        _err("  commit itself is the problem. This happens when somebody commits in")
        _err("  the seconds between your first attempt and your retry, moving HEAD")
        _err("  onto their work.")
        _err("  Check `git log -1` before amending again. To add to your own commit,")
        _err("  make a new one instead.")
    return EXIT_CONFLICT


def _cmd_check_inner(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    if "staged" in flags:
        return _cmd_check_staged(flags)
    explicit = (flags.get("as") or "").strip().lstrip("@")

    if "stdin-json" in flags:
        try:
            payload = _hooks.parse_payload(sys.stdin.read())
        except (ValueError, json.JSONDecodeError) as exc:
            _err(f"check: cannot read the hook payload: {exc}")
            return EXIT_BLOCK
        if not payload.path:
            return EXIT_OK
        return _hooks.check_edit(Path(payload.path).expanduser(),
                                 agent_session=payload.session_id, explicit=explicit,
                                 explicit_root=flags.get("root"), payload=payload)
    if not positional:
        _err("error: check needs a path, or --stdin-json to read a hook payload")
        return EXIT_USAGE
    worst = EXIT_OK
    for one in positional:
        code = _hooks.check_edit(Path(one).expanduser(),
                                 agent_session=_identity.host_session(), explicit=explicit,
                                 explicit_root=flags.get("root"), payload=None)
        if code == EXIT_BLOCK:
            worst = EXIT_BLOCK
    return worst


# ---------------------------------------------------------------------------
# note: a short FYI for the other agents and the person watching
# ---------------------------------------------------------------------------

#: A note is one line somebody reads later, not the writeup. Agents
#: hit the old 400 cap 48 times, so it is now 1000.
NOTE_MAX = 1000


def _too_long(kind: str, text: str, cap: int) -> int:
    """Refuse, but show WHERE it would cut and name the verb that fits.

    The old message said only "that note is 586 characters; keep it under 400",
    which is true and useless: it does not say what to drop, and it does not say
    that the thing being written is not a note. Three agents reported it and one
    did the predictable thing: split the handoff into two notes, which then read
    OUT OF ORDER in the feed, because the feed is ordered by time and the second
    half was written first in their head.

    That is the damage worth preventing, so it is said explicitly. Splitting is
    the one option that looks like it works and does not.
    """
    over = len(text) - cap
    _err(f"error: that {kind} is {len(text)} characters, {over} over the {cap} cap.")
    _err("  It would end here:")
    _err(f"    …{text[max(0, cap - 60):cap]}")
    _err(f"    ⟨{over} more character{'s' if over != 1 else ''} after that⟩")
    # A version that FITS, cut at a word. Reported: an agent trimmed to 538, was
    # refused, trimmed again to 401, and was refused a second time for one
    # character. Two round trips for one note, when the answer was always
    # computable here.
    fits = text[:cap].rsplit(" ", 1)[0] if " " in text[:cap] else text[:cap]
    if fits and len(fits) < len(text):
        _err(f"  This fits ({len(fits)} characters), if it still says what you meant:")
        _err(f"    {fits}")
    _err(f"  A {kind} is one line somebody reads later. Put anything longer in a "
         "file in the repo and name the file here.")
    _err("  Do not split it into two notes. The feed is ordered by time, so they "
         "will not read in the order you wrote them.")
    return EXIT_USAGE


def _cmd_note(argv: list[str]) -> int:
    """A short FYI, up to NOTE_MAX characters. Start with @name to address one agent."""
    positional, flags = _parse_flags(argv)
    body = " ".join(positional).strip()
    if not body:
        _err("error: note needs something to say")
        _err('  e.g. comms-graph note "schema migration lands next session" --as me')
        return EXIT_USAGE
    if len(body) > NOTE_MAX:
        return _too_long("note", body, NOTE_MAX)
    actor = _actor(flags)
    root, log_file, lock_file = _runtime(flags)
    with _lock.file_lock(lock_file):
        st = _read_state(log_file)
        greeting = _hello_if_unknown(st, actor)
        event = _tagged(st, actor, _log.TYPE_NOTE, None, {"body": body})
        if greeting is None:
            _log.append(log_file, event)
        else:
            _log.append_batch(log_file, [greeting, event])
    print(f"NOTED by @{actor}")
    print(f"  {body}")
    return EXIT_OK


def _release_somebody_elses(st, log_file, actor: str, target: str, reason: str) -> int:
    """`release <claim-id> --force --reason "..."`: end a claim that is not yours.

    Deliberately narrow: an exact claim id, never a path. A path can match more
    ground than the reader meant, and the whole point of this verb is that the
    person running it is making a judgement about somebody else's work: that
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
        _err("  --force takes a claim id, not a path: a path can match more than you meant.")
        return EXIT_USAGE
    if held.actor == actor:
        _err(f"error: {target} is your own claim: release it without --force")
        return EXIT_USAGE
    _log.append(log_file, _tagged(st, actor, _log.TYPE_RELEASE, None, {
        "refs": [held.id],
        "result": reason,
        "original_actor": held.actor,
        "arbitrator": actor,
    }))
    print(f"RELEASED {held.scope}  (was @{held.actor}, id {held.id})")
    print(f"  Freed by @{actor}, not by them. Recorded with your reason.")
    print(f"  If @{held.actor} is still alive it will find the ground gone and can re-claim it.")
    return EXIT_OK


def _cmd_guard(argv: list[str]) -> int:
    positional, flags = _parse_flags(argv)
    sub = positional[0] if positional else "status"
    root = _repo_root(flags.get("root"))

    if sub == "status":
        st = _guard.status(root)
        print(_guard.describe(st))
        if st.path is not None:
            print(f"  hook path: {st.path}")
        # Exit 1 when it is NOT wired in, so a setup script can branch on it
        # without parsing prose. Not 2: nothing failed, the answer is just no.
        return EXIT_OK if st.installed else EXIT_CONFLICT

    if sub == "install":
        ok, lines = _guard.install(root, chain="chain" in flags)
        for line in lines:
            (print if ok else _err)(line)
        return EXIT_OK if ok else EXIT_CONFLICT

    if sub == "uninstall":
        ok, lines = _guard.uninstall(root)
        for line in lines:
            (print if ok else _err)(line)
        return EXIT_OK if ok else EXIT_CONFLICT

    return EXIT_BLOCK


def _cmd_board(argv: list[str]) -> int:
    _, flags = _parse_flags(argv)
    me = _actor(flags, write=False)
    root = _repo_root(flags.get("root"))
    log_file, _lock_file = _store(root)
    _warn_if_ephemeral(log_file)
    st = _read_state(log_file)
    claims = sorted(st.claims.values(), key=lambda c: c.ts)

    # The tree, not just the log. "No active claims" was measured saying the
    # repo was quiet while git status showed fifteen changed files, four of
    # which appear nowhere in the log at all. A board that reports the absence
    # of declarations as the absence of work is worse than no board, because it
    # is believed.
    survey = _tree.survey(root, st)
    head = _tree.headline(survey)

    # And whether anything is actually ENFORCING the commit check here. The
    # check has always been correct and has caught nothing three times, because
    # it only runs when somebody types it. An unenforced guard should not be
    # invisible on the board that is supposed to be telling you the truth.
    guard = _guard.status(root)
    guard_line = "" if guard.installed else "  " + _guard.describe(guard)

    if not claims:
        if head:
            print("no active claims in this repo, but the tree is not quiet:")
            print(f"  {head}")
            for line in _tree.lines(survey):
                print(line)
        else:
            print("no active claims in this repo"
                  + (" and no uncommitted changes" if survey.readable else ""))
            if not survey.readable:
                print(f"  {head or survey.unavailable}")
        if guard_line:
            print(guard_line)
        return EXIT_OK
    if "all" not in flags:
        from . import compact as _compact

        print("\n".join(_compact.view(st, log_file.parent, me)))
        if head:
            print(head)
        if guard_line:
            print(guard_line.strip())
        print(_compact.all_hint("board"))
        return EXIT_OK
    print(f"active claims ({len(claims)}):")
    for claim in claims:
        when = claim.ts.isoformat().replace("+00:00", "Z")
        mark = " <- you" if me and claim.actor == me else ""
        intent = f'  "{claim.intent}"' if claim.intent else ""
        print(f"  {claim.scope}  @{claim.actor}  {when}  [{claim.id}]{intent}{mark}")

    # And what is actually changed on disk, which is a different question. A
    # claim is a statement of intent; this is the evidence.
    if head:
        print()
        print(head + ":")
        for line in _tree.lines(survey):
            print(line)
    if guard_line:
        print()
        print(guard_line.strip())
    return EXIT_OK


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# messages addressed to you
# ---------------------------------------------------------------------------

_ADDRESSED = None  # set lazily; see _mentions


def _mentions(body: str, actor: str) -> bool:
    """Does this text address `actor` by name?

    Matches the base name, so a note to @claude-dev reaches claude-dev/review.
    Deliberately not a substring test: "@claude" must not match "@claude-karte",
    or a busy project delivers everything to everybody and the feature becomes
    noise on its first day.
    """
    import re as _re

    base = _identity.base_actor((actor or "").lstrip("@")).casefold()
    if not base:
        return False
    for m in _re.finditer(r"@([A-Za-z0-9._-]+)", body or ""):
        if _identity.base_actor(m.group(1)).casefold() == base:
            return True
    return False


def _seen_marker(root: Path, actor: str) -> Path:
    """Where we remember what this actor has already been shown.

    NOT an event. Delivery is a property of one machine's terminal, not a fact
    about the work, and writing a "delivered" event per read would double the
    log to record something no other agent can use. Losing this file re-shows a
    message once, which is the harmless direction.
    """
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in actor)[:64]
    return _log.store_dir(root) / "seen" / (safe or "unknown")


#: How far back the commit guard reaches when it restates messages. A commit is
#: the last moment taking somebody's fix along is free, so it is worth saying
#: again there even if an ordinary command already delivered it once.
RESTATE_WITHIN_SECONDS = 2 * 3600


def _deliver_addressed_notes(root: Path, actor: str, restate: bool = False) -> None:
    """Print messages addressed to `actor` that they have not been shown.

    BLOCKING IS PUSHED AND NOTES WERE POLLED, which for something called comms
    is the wrong way round. Reported with a receipt: an agent was blocked on a
    file, so rather than wait it wrote the holder a note by name, with a fix to
    take along. The holder committed that exact file six minutes later having
    never seen it, and found the note forty minutes after that. The fix is now
    half-landed.

    The holder ran several commands in those six minutes. Any one of them could
    have printed a line, which is the whole idea: no new discipline, because the
    discipline is what fails.
    """
    if not actor:
        return
    log_file, _lock_file = _store(root, create=False)
    try:
        log_file.stat()
    except OSError:
        return
    try:
        events = _log.read(log_file)
    except Exception:
        return

    marker = _seen_marker(root, actor)
    try:
        last_seen = marker.read_text(encoding="utf-8").strip()
    except OSError:
        last_seen = ""

    waiting = []
    for ev in events:
        if ev.type != _log.TYPE_NOTE:
            continue
        if _identity.base_actor((ev.actor or "").lstrip("@")).casefold() == \
                _identity.base_actor(actor.lstrip("@")).casefold():
            continue  # your own message is not a message to you
        recent = ((datetime.now(timezone.utc) - ev.ts).total_seconds()
                  <= RESTATE_WITHIN_SECONDS)
        if ev.id <= last_seen and not (restate and recent):
            continue
        data = ev.data or {}
        body = str(data.get("body") or data.get("summary") or "")
        if _mentions(body, actor):
            waiting.append((ev, body))

    sys.stdout.flush()

    if not waiting:
        # Still move the marker: everything up to here has been considered, so a
        # message written before this actor existed is not delivered later as
        # though it were new.
        _remember_seen(marker, events)
        return

    # Newest last, because a terminal is read from the bottom.
    shown = waiting[-3:]
    _err("")
    _err(f"  {len(waiting)} message(s) for you"
         + (f", showing the last {len(shown)}" if len(waiting) > len(shown) else "") + ":")
    for ev, body in shown:
        when = ev.ts.astimezone().strftime("%H:%M")
        _err(f"    {when}  @{ev.actor}: {body}")
    if restate:
        _err("  This is the last point where taking it along is free.")
    else:
        _err("  Said once, here, because you were not going to go looking.")
    _remember_seen(marker, events)


def _remember_seen(marker: Path, events) -> None:
    if not events:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(events[-1].id, encoding="utf-8")
    except OSError:
        pass  # a board that cannot remember re-shows once; never fatal


def _lazy(module: str, name: str):
    """A handler that imports its module only when the verb runs.

    `claim` and `check` are on the pre-edit path and must survive any other
    module failing to import.
    """
    def call(argv: list[str]) -> int:
        import importlib

        return getattr(importlib.import_module("." + module, __package__), name)(argv)
    return call


def _help_verb(argv: list[str]) -> int:
    positional, _ = _parse_flags(argv)
    if not positional:
        print(_verbs.global_usage())
        return EXIT_OK
    try:
        call = _verbs.prepare([*positional, "--help"])
    except _verbs.Refusal as refusal:
        for line in refusal.lines:
            _err(line)
        return refusal.code
    if call.verb.retired:
        _err(_verbs.RETIRED_NOTICE)
        return EXIT_OK
    print(_verbs.verb_help(call.verb, call.path))
    return EXIT_OK


def _guard_verb(sub: str):
    return lambda argv: _cmd_guard([sub, *argv])


def _hook_verb(sub: str):
    return lambda argv: _hooks.main([sub, *argv])


def _session_verb(sub: str):
    def call(argv: list[str]) -> int:
        from . import session as _session
        return _session.main([sub, *argv])
    return call


def _version_verb(argv: list[str]) -> int:
    from . import hello as _hello
    return _hello.cmd_version(argv)


#: One handler per dispatchable path in ``verbs.leaves()`` that is not an alias.
_HANDLERS = {
    "claim": _cmd_claim,
    "release": _cmd_release,
    "board": _cmd_board,
    "check": _cmd_check,
    "note": _cmd_note,
    "doc": _lazy("doc", "main"),
    "status": _lazy("status", "status_main"),
    "log": _lazy("status", "log_main"),
    "hello": _lazy("hello", "cmd_hello"),
    "mcp": _lazy("mcp", "main"),
    "doctor": _lazy("doctor", "main"),
    "whoami": _lazy("compact", "whoami_main"),
    "claims": _lazy("compact", "claims_main"),
    "ui": _cmd_ui,
    "version": _version_verb,
    "help": _help_verb,
    "guard install": _guard_verb("install"),
    "guard status": _guard_verb("status"),
    "guard uninstall": _guard_verb("uninstall"),
    "hook session-start": _hook_verb("session-start"),
    "hook pre-edit": _hook_verb("pre-edit"),
    "hook session-end": _hook_verb("session-end"),
    "session start": _session_verb("start"),
    "session join": _session_verb("join"),
    "session end": _session_verb("end"),
    "session retire": _session_verb("retire"),
    "session lead": _session_verb("lead"),
}


def _run(argv: list[str]) -> int:
    """``argv`` is everything after ``comms-graph``."""
    # A global --repo/--root before the verb is held in a module global rather
    # than exported into os.environ: the environment outlives the call and
    # would hand the next one in the same process a repository it never asked
    # for. Reset on every entry, so one call can never colour the next.
    global _GLOBAL_ROOT, _BOOLEANS
    _GLOBAL_ROOT = None
    _BOOLEANS = frozenset()
    env_actor = os.environ.get("COMMS_ACTOR")
    if env_actor and env_actor.strip() != env_actor.strip().lstrip("@").strip():
        os.environ["COMMS_ACTOR"] = env_actor.strip().lstrip("@").strip()
    try:
        call = _verbs.prepare(argv)
    except _verbs.Refusal as refusal:
        for line in refusal.lines:
            _err(line)
        return refusal.code
    if call.verb.retired:
        _err(_verbs.RETIRED_NOTICE)
        return EXIT_OK
    if call.retired:
        _err(_verbs.RETIRED_FLAG_NOTICE)
    if call.root:
        _GLOBAL_ROOT = call.root
    if call.help:
        print(_verbs.verb_help(call.verb, call.path))
        return EXIT_OK
    _BOOLEANS = _verbs.booleans(call.verb)
    handler = _HANDLERS.get(call.path)
    try:
        return handler(call.argv)
    except _lock.LockError as exc:
        _err(f"error: could not take the comms lock: {exc}")
        return EXIT_USAGE
    finally:
        _BOOLEANS = frozenset()


def main(argv: list[str]) -> int:
    """The verb, then anything addressed to whoever ran it.

    Delivery is bolted on AFTER the command rather than inside each one, so a
    verb cannot forget to do it, and it can never change an exit code: a
    message waiting for you is not a reason for your command to fail.
    """
    code = _run(argv)
    try:
        call = _verbs.prepare(argv)
        if call.verb.retired:
            return code
        flags = _parse_flags(list(call.argv))[1]
        verb = call.verb.name
        # Not on the hook path and not into machine-readable output. `check
        # --stdin-json` has a strict contract with Claude Code, and anything
        # --json is being parsed by something that did not ask for prose.
        if verb == "check" and "stdin-json" in flags:
            return code
        if "json" in flags or verb in ("mcp", "ui", "hook", "help", "version", "doctor"):
            return code
        actor = _actor(flags, write=False)
        if actor:
            _deliver_addressed_notes(_repo_root(flags.get("root")), actor)
    except (Exception, SystemExit):
        pass  # never let delivery break the command that carried it
    return code
