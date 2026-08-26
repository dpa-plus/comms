"""``comms-graph doc`` — the repo-local wiki.

Docs live at ``<repo>/.comms/docs/<slug>.md``, INSIDE the repository, and not in
the per-machine store the event log uses. That split is the whole point. The log
is one machine's record of who held what and is disposable; a doc is the reason
a decision was made, so it belongs to the project, gets committed, and travels
with a clone. A doc written into the store would be visible only to the machine
that wrote it — which is exactly the audience that already knows.

THE READ PATH CARRIES THE WEIGHT. In real use these are 2–5KB design and
feasibility notes that somebody opens weeks later to remember why a thing was
decided. So ``--list`` prints a one-line hint beside each slug instead of a bare
column of names: a directory listing is not a table of contents, and a reader
who cannot tell which of five slugs holds the answer opens all five.

WHY ``--edit`` IS THE FIDDLY ONE. It hands control to ``$EDITOR`` for an
unbounded stretch of human time, so three things are arranged around that:

  * The per-repo comms flock is NOT held while the editor runs. Holding it would
    stall every other agent's ``claim`` behind one person's open buffer, which
    turns a coordination tool into the thing being coordinated around.
  * A sidecar flock at ``.comms/docs/.<slug>.lock`` keeps two editors off one
    doc, and is stamped with the holder's name so the second one is told *who*
    rather than just "busy".
  * The editor never touches the real file. It edits a scratch copy and the
    result is installed with one atomic rename. An editor that dies mid-write
    therefore cannot leave a truncated doc where the doc was, and an abandoned
    edit of a brand-new slug leaves no file at all — an empty stub is worse than
    nothing, because it shows up in ``--list`` as if somebody had written it.

The event log is shared with the Go build, so the finding recorded after a save
uses the Go's keys exactly: category ``decision``, summary ``updated doc:<slug>``
and a ``doc`` ref. A different key for the same fact would be dropped silently by
the other reader, which is the failure mode that looks like nothing happening.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import lock as _lock
from . import log as _log
from .cli import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    _actor,
    _err,
    _event,
    _hello_if_unknown,
    _parse_flags,
    _read_state,
    _repo_root,
    _store,
    _warn_if_ephemeral,
)

USAGE = """Usage: comms-graph doc [--list | <slug> [--edit]]

  doc --list              every doc in .comms/docs/, with a one-line hint
  doc <slug>              print .comms/docs/<slug>.md
  doc <slug> --edit       open it in $EDITOR, then save it back

  Options:
    --as <actor>    who you are. Only --edit needs it (it records a finding).
                    Or set COMMS_ACTOR.
    --root <path>   repo root (default: the git root above the cwd)

  Docs are committed with the code. The event log is not; that is per machine.

  Exit status: 0 done, 1 no such doc / somebody else has it open, 2 bad usage."""

#: The slug grammar, copied from the Go build so a doc written by either build is
#: addressable from the other. It also happens to be the whole path-safety story:
#: no slashes, and a leading dot is impossible, so ".." and "/etc/passwd" are
#: rejected here and a slug can never name a file outside .comms/docs.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")

#: Hint width for --list. Measured in CHARACTERS, never bytes: these docs are
#: routinely German, and slicing "Suchkreis-Größe" by bytes lands mid-character
#: and prints a replacement glyph in the one line meant to orient the reader.
_HINT_MAX = 70
_HINT_KEEP = 67

#: Column the hint starts in, matching the Go's "%-30s %s".
_SLUG_COLUMN = 30


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------


def _docs_dir(root: Path) -> Path:
    return root / ".comms" / "docs"


def _doc_path(docs: Path, slug: str) -> Path:
    return docs / f"{slug}.md"


def _sidecar_path(docs: Path, slug: str) -> Path:
    """The editor lock, hidden beside the doc.

    Dot-prefixed on purpose: the repo's own ``.comms/.gitignore`` ignores
    ``docs/.*.lock``, and every listing here skips dotfiles, so a lock file is
    never mistaken for content by git or by ``--list``.
    """
    return docs / f".{slug}.lock"


# ---------------------------------------------------------------------------
# doc --list
# ---------------------------------------------------------------------------


def _hint(path: Path) -> str:
    """The first line of a doc that actually says something.

    Headings are skipped because a heading usually just restates the slug —
    "kataster-machbarkeit" next to "Kataster Machbarkeit" tells a reader nothing
    they did not already have. The first prose line is the sentence that says
    whether this is the doc they wanted. A doc with no prose at all falls back to
    its heading, which beats an empty column.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    heading = ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not heading and line.startswith("# "):
                heading = line[2:].strip()
            continue
        if len(line) > _HINT_MAX:
            return line[:_HINT_KEEP] + "..."
        return line
    return heading


def _list(root: Path) -> int:
    docs = _docs_dir(root)
    try:
        names = [entry.name for entry in docs.iterdir()]
    except FileNotFoundError:
        # No docs directory is not a failure. A repo that has never written a doc
        # is the common case, and bootstrapping one as a side effect of reading
        # would put an empty directory in somebody's diff.
        names = []
    except OSError as exc:
        _err(f"error: cannot read {docs}: {exc.strerror or exc}")
        return EXIT_ERROR

    slugs = sorted(
        name[:-3]
        for name in names
        if name.endswith(".md") and not name.startswith(".") and (docs / name).is_file()
    )
    if not slugs:
        print("(no docs yet)")
        return EXIT_OK
    for slug in slugs:
        hint = _hint(_doc_path(docs, slug))
        print(f"{slug:<{_SLUG_COLUMN}} {hint}" if hint else slug)
    return EXIT_OK


# ---------------------------------------------------------------------------
# doc <slug>
# ---------------------------------------------------------------------------


def _print(root: Path, slug: str) -> int:
    docs = _docs_dir(root)
    path = _doc_path(docs, slug)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # A refusal, not a breakage: the command worked and the answer is "there
        # is no such doc", which is exit 1. A caller that retries on 2 must not
        # retry this.
        _err(f"error: there is no doc {slug!r} in {docs}")
        _err("  `comms-graph doc --list` shows what is there.")
        return EXIT_CONFLICT
    except OSError as exc:
        _err(f"error: cannot read {path}: {exc.strerror or exc}")
        return EXIT_ERROR
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Still print it. Somebody opened this to recover a decision, and a
        # traceback in place of 4KB of reasoning helps nobody — but say the text
        # has been altered, so nothing mangled gets quoted back as verbatim.
        text = raw.decode("utf-8", "replace")
        _err(f"warning: {path} is not valid UTF-8; the bad bytes print as U+FFFD.")
    # Written, not printed: a doc is copied out byte for byte, and print() would
    # append a newline the file does not have.
    sys.stdout.write(text)
    return EXIT_OK


# ---------------------------------------------------------------------------
# doc <slug> --edit
# ---------------------------------------------------------------------------


def _editor_command() -> list[str] | None:
    """The program (and its own flags) to open a file with, or None on failure.

    $VISUAL then $EDITOR then ``vi``, the order every other tool uses. The value
    is split with shell quoting rules because real settings carry arguments and
    spaces — ``code --wait``, or a quoted "/Applications/Visual Studio
    Code.app/.../code" — and splitting on plain whitespace would try to execute
    "/Applications/Visual". Nothing is handed to a shell; this is only tokenising.
    """
    spec = ""
    for name in ("VISUAL", "EDITOR"):
        spec = (os.environ.get(name) or "").strip()
        if spec:
            break
    if not spec:
        if shutil.which("vi") is None:
            _err("error: no editor to open it with.")
            _err("  Set $EDITOR (e.g. `export EDITOR=nano`) and try again.")
            return None
        spec = "vi"
    try:
        parts = shlex.split(spec)
    except ValueError as exc:
        _err(f"error: cannot read the editor setting {spec!r}: {exc}")
        return None
    if not parts:
        _err(f"error: the editor setting {spec!r} names no program.")
        return None
    program = shutil.which(parts[0])
    if program is None:
        _err(f"error: the editor {parts[0]!r} is not on PATH.")
        return None
    return [program, *parts[1:]]


def _stamp(path: Path, actor: str) -> None:
    """Write who holds the sidecar lock, so the next editor is told a name.

    Truncating the lock file does not disturb the flock — the lock lives on the
    open file description, not on the bytes — and a stamp we fail to write costs
    the next editor a name, never the exclusion itself. So this never raises.
    """
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{actor}\n{when}\n")
    except OSError:
        pass


def _holder(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return "another editor"
    if len(lines) >= 2 and lines[0].strip():
        return f"@{lines[0].strip()} since {lines[1].strip()}"
    return "another editor"


def _install(doc_path: Path, content: bytes) -> None:
    """Put `content` at `doc_path` in one step. Raises OSError on failure.

    Write-then-rename rather than writing over the doc: a reader — `--list`, a
    grep, the other agent — must never catch the file half written, and a failure
    partway through must leave the previous version exactly as it was. The
    temporary lands in the same directory because rename is only atomic within
    one filesystem, and is dot-prefixed so it cannot show up as a doc if the
    process is killed between the write and the rename.
    """
    mode = 0o644
    try:
        mode = doc_path.stat().st_mode & 0o777
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(
        dir=str(doc_path.parent), prefix=f".{doc_path.stem}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            # fsync before the rename. Without it a crash can land the rename
            # while the bytes are still only in the page cache, and the doc comes
            # back present but empty — which reads as "we decided nothing here"
            # rather than as the loss it is.
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, doc_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _record(root: Path, slug: str, actor: str) -> None:
    """Append the finding that says this doc moved. Never fails the save.

    Keys are the Go build's, verbatim, because both builds read one log: a
    ``decision`` finding summarised ``updated doc:<slug>`` with a ``doc`` ref.
    Spelling the ref differently would hide every doc edit from the other build's
    reader without any error to notice.

    If the log cannot be written the doc is still on disk, and reporting failure
    would send somebody back to re-type an edit that was in fact saved. So the
    log problem is said out loud and the save stands.
    """
    log_file, lock_file = _store(root)
    _warn_if_ephemeral(log_file)
    data = {
        "category": "decision",
        "summary": f"updated doc:{slug}",
        "refs": [{"kind": "doc", "value": slug}],
    }
    try:
        with _lock.file_lock(lock_file):
            st = _read_state(log_file)
            greeting = _hello_if_unknown(st, actor)
            event = _event(actor, _log.TYPE_FINDING, None, data)
            if greeting is None:
                _log.append(log_file, event)
            else:
                # The greeting must land BEFORE the event it explains, or the
                # pre-edit hook cannot map this session to an actor yet.
                _log.append_batch(log_file, [greeting, event])
    except (_lock.LockError, OSError) as exc:
        _err(f"warning: the doc was saved, but the log was not updated: {exc}")


def _run_editor(command: list[str], doc_path: Path, slug: str) -> int:
    """Edit a scratch copy, then install it. The real file moves once or not at all."""
    try:
        original = doc_path.read_bytes()
    except FileNotFoundError:
        # A new doc starts as a heading, the way the Go build starts one, so the
        # editor opens something rather than a blank buffer.
        original = f"# {slug}\n\n".encode()
    except OSError as exc:
        _err(f"error: cannot read {doc_path}: {exc.strerror or exc}")
        return EXIT_ERROR

    scratch_dir = tempfile.mkdtemp(prefix="comms-doc-")
    # Named for the slug so the editor's title bar, syntax highlighting and
    # ":w" all behave as if this were the doc itself.
    scratch = Path(scratch_dir) / f"{slug}.md"
    scratch.write_bytes(original)
    keep = False
    try:
        try:
            completed = subprocess.run([*command, str(scratch)])
        except OSError as exc:
            _err(f"error: could not run {command[0]!r}: {exc.strerror or exc}")
            return EXIT_ERROR
        if completed.returncode != 0:
            # The editor said the session went wrong, so nothing is installed and
            # the doc is untouched. The scratch file stays: whatever the user did
            # manage to save is the only copy of it, and deleting it here would
            # throw away their work to keep a temp directory tidy.
            keep = True
            _err(
                f"error: the editor exited with status {completed.returncode}; "
                f"{doc_path} is unchanged."
            )
            _err(f"  Anything you saved is still at {scratch}.")
            return EXIT_ERROR
        try:
            edited = scratch.read_bytes()
        except OSError as exc:
            keep = True
            _err(f"error: cannot read back {scratch}: {exc.strerror or exc}")
            return EXIT_ERROR
        try:
            _install(doc_path, edited)
        except OSError as exc:
            keep = True
            _err(f"error: could not save {doc_path}: {exc.strerror or exc}")
            _err(f"  Your edited copy is still at {scratch}.")
            return EXIT_ERROR
    finally:
        if not keep:
            shutil.rmtree(scratch_dir, ignore_errors=True)
    return EXIT_OK


def _edit(root: Path, slug: str, flags: dict) -> int:
    actor = _actor(flags)
    docs = _docs_dir(root)
    doc_path = _doc_path(docs, slug)

    # Resolve the editor before creating anything at all. An unset $EDITOR that
    # was discovered only after the doc had been stubbed out left an empty doc
    # behind on every failed attempt, and an empty doc is worse than no doc: it
    # lists as if somebody had written something.
    command = _editor_command()
    if command is None:
        return EXIT_USAGE

    try:
        docs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _err(f"error: cannot create {docs}: {exc.strerror or exc}")
        return EXIT_ERROR

    sidecar = _sidecar_path(docs, slug)
    try:
        handle = _lock.try_acquire(sidecar)
    except _lock.LockError as exc:
        _err(f"error: cannot take the editor lock at {sidecar}: {exc}")
        return EXIT_ERROR
    if handle is None:
        # Non-blocking on purpose: an editor session has no deadline, so waiting
        # would mean waiting until somebody's lunch is over. Refusal (exit 1)
        # with the holder's name is the useful answer.
        _err(f"error: {slug} is already open in {_holder(sidecar)}; try again later.")
        return EXIT_CONFLICT

    try:
        _stamp(sidecar, actor)
        # The per-repo comms lock is deliberately NOT held here — see the module
        # docstring. It is taken for the few milliseconds of _record() instead.
        code = _run_editor(command, doc_path, slug)
    finally:
        handle.release()

    if code != EXIT_OK:
        return code
    _record(root, slug, actor)
    print(f"Saved .comms/docs/{slug}.md")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    """``argv`` is everything after ``comms-graph doc``."""
    if any(arg in ("-h", "--help", "-?") for arg in argv):
        # Asking how a verb works must never have a side effect — and for a tool
        # whose users are agents, this is the whole discovery path.
        print(USAGE)
        return EXIT_OK

    # --list and --edit are booleans and are pulled out before the shared flag
    # parser sees them. That parser reads `--edit foo` as edit="foo", so it would
    # swallow the slug and leave `doc --edit tracker` with nothing to open.
    want_list = "--list" in argv
    want_edit = "--edit" in argv
    positional, flags = _parse_flags([a for a in argv if a not in ("--list", "--edit")])

    if want_list:
        if positional or want_edit:
            _err("error: --list takes nothing else")
            _err(USAGE)
            return EXIT_USAGE
        return _list(_repo_root(flags.get("root")))

    if len(positional) != 1:
        _err("error: doc needs --list, or exactly one doc name")
        _err(USAGE)
        return EXIT_USAGE

    slug = positional[0]
    if not _SLUG_RE.match(slug):
        _err(f"error: {slug!r} is not a doc name")
        _err("  Names are lowercase [a-z0-9][a-z0-9._-] up to 81 characters, with")
        _err("  no slashes, which is also what keeps a name inside .comms/docs.")
        return EXIT_USAGE

    root = _repo_root(flags.get("root"))
    if want_edit:
        return _edit(root, slug, flags)
    return _print(root, slug)
