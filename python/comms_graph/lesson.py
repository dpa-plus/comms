"""``comms-graph lesson`` — the knowledge that outlives one repository.

A claim, a task, a finding: all of those are about ONE checkout and live in
that checkout's log. A lesson is the opposite kind of fact — "check the API
response before blaming the UI" is true in every project an agent will ever
touch — so lessons are stored once per user, under the global comms data dir,
and are readable from a directory that is not a repository at all. Nothing here
opens a repo log or takes the repo lock, because a lesson has nothing to do
with a repo. That is the whole design and it is the one thing not to "fix".

FILE-COMPATIBLE WITH THE GO BUILD. Both builds read and write the same
``<data home>/comms/global/lessons/<slug>.md`` files with the same sidecar
lock, so a lesson written by either is a lesson the other lists, prints and
edits. That compatibility is why the slug rule below is copied from the Go
rather than reusing this package's task-id rule, and why the stub text matches.

WRITTEN RARELY, READ WHENEVER. In six months on this machine, zero lessons have
been written. So the read path is built for the empty shelf: `--list` with no
lessons prints "(no global lessons yet)" and exits 0, because a fresh install
reporting an error would teach every agent that the verb is broken. Adding a
lesson is deliberately a human-in-the-loop act — it opens $EDITOR and stops.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import lock as _lock
from . import log as _log
from .cli import EXIT_CONFLICT, EXIT_ERROR, EXIT_OK, EXIT_USAGE, _actor, _err

USAGE = """Usage: comms-graph lesson [--list | <slug> [--edit]]

  lesson --list                 list the global lessons
  lesson <slug>                 print one to stdout
  lesson <slug> --edit          open it in $EDITOR (creates it if new)

  Lessons are cross-project operating knowledge, stored per user and not in
  any repo. Add one only when the user asks for it or approves it.

  Options:
    --as <actor>   who you are (or set COMMS_ACTOR). Needed for --edit only.

  Exit status: 0 done, 1 no such lesson / somebody is editing it, 2 bad usage."""

#: Copied from the Go build's slug rule, NOT this package's ``_SLUG_RE``. Task
#: ids are 32 chars of ``[a-z0-9-]``; lesson slugs allow dots and underscores up
#: to 81 chars. Tightening it here would refuse to open lessons the Go build
#: writes into the very same directory — the file would be listed and then be
#: unreadable, which is worse than either build alone.
#:
#: It is also the path guard: the slug becomes a filename, and anchored
#: lowercase-with-no-slash is what stops ``lesson ../../.ssh/id_rsa`` from
#: printing a file that is not a lesson.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")

_KNOWN_FLAGS = ("list", "edit", "as")


def lessons_dir() -> Path:
    """The per-user lesson directory, the same one the Go build uses."""
    return _log.user_data_home() / "comms" / "global" / "lessons"


def _parse(argv: list[str]) -> tuple[list[str], dict[str, object]]:
    """Positionals and flags, with ``--list``/``--edit`` known to take no value.

    Not ``cli._parse_flags``: that one treats the next non-``--`` word as a
    flag's value, so ``lesson --edit my-slug`` parsed as ``edit="my-slug"`` with
    zero positionals — the slug vanished and the command answered "provide
    --list or a slug" for a line that named one. Flag order is not something a
    user should have to get right.
    """
    positional: list[str] = []
    flags: dict[str, object] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            positional.append(arg)
            i += 1
        elif arg in ("--list", "--edit"):
            flags[arg[2:]] = True
            i += 1
        elif "=" in arg:
            key, value = arg[2:].split("=", 1)
            flags[key] = value
            i += 1
        else:
            flags[arg[2:]] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2 if i + 1 < len(argv) else 1
    return positional, flags


def main(argv: list[str]) -> int:
    """``argv`` is everything after ``lesson``."""
    if any(a in ("-h", "--help", "-?") for a in argv):
        print(USAGE)
        return EXIT_OK

    positional, flags = _parse(argv)
    unknown = [k for k in flags if k not in _KNOWN_FLAGS]
    if unknown:
        # Silently ignoring `--edt` would print the lesson and report success,
        # so the user believes they edited something they did not.
        _err(f"error: lesson: unknown flag --{unknown[0]}")
        _err(USAGE)
        return EXIT_USAGE

    try:
        directory = lessons_dir()
    except RuntimeError as exc:
        # Windows: there is no agreed lesson location, and inventing one puts
        # the file where no comms build looks.
        _err(f"error: lesson: {exc}")
        return EXIT_ERROR

    if flags.get("list"):
        if positional or flags.get("edit"):
            _err("error: lesson: --list takes no other arguments")
            return EXIT_USAGE
        return _list(directory)

    if len(positional) != 1:
        _err("error: lesson: give --list, or a slug (optionally with --edit)")
        _err(USAGE)
        return EXIT_USAGE

    slug = positional[0]
    if not _SLUG_RE.match(slug):
        _err(f"error: lesson: invalid slug {slug!r}. "
             "Must match [a-z0-9][a-z0-9._-]{0,80}.")
        return EXIT_USAGE

    if flags.get("edit"):
        return _edit(directory, slug, flags)
    return _print(directory, slug)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _list(directory: Path) -> int:
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        # Nobody has written a lesson yet. That is the normal state of a fresh
        # machine, not a fault, so it must not read like one.
        entries = []
    except OSError as exc:
        _err(f"error: lesson: cannot read {directory}: {exc.strerror or exc}")
        return EXIT_ERROR

    slugs = sorted(
        e.name[:-3]
        for e in entries
        if e.is_file() and e.name.endswith(".md") and not e.name.startswith(".")
    )
    if not slugs:
        print("(no global lessons yet)")
        return EXIT_OK

    for slug in slugs:
        hint = _summary(directory / f"{slug}.md")
        print(f"{slug:<30} {hint}" if hint else slug)
    return EXIT_OK


def _print(directory: Path, slug: str) -> int:
    path = directory / f"{slug}.md"
    try:
        # errors="replace" rather than raising: a lesson is prose to read, and
        # one bad byte should not withhold the other forty lines.
        body = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # Exit 1, not 2 — "there is no such lesson" is an answer, and a caller
        # that retries on failure must not retry this.
        _err(f"error: lesson: no lesson {slug!r} in {directory}")
        _err("  `lesson --list` shows what exists; `lesson "
             f"{slug} --edit` starts it.")
        return EXIT_CONFLICT
    except OSError as exc:
        _err(f"error: lesson: cannot read {path}: {exc.strerror or exc}")
        return EXIT_ERROR
    sys.stdout.write(body)
    return EXIT_OK


def _summary(path: Path) -> str:
    """The first line of prose, for the listing's second column."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = raw.split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return line if len(line) <= 70 else line[:67] + "..."
    # A stub that is all headings still deserves a hint, so fall back to the
    # title rather than showing a bare slug twice.
    for line in lines:
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def _edit(directory: Path, slug: str, flags: dict) -> int:
    # Resolved first, before anything is created: the sidecar names who is
    # editing, and "another editor" is a much worse answer to the next agent
    # than a name it can go and ask.
    actor = _actor(flags)

    path = directory / f"{slug}.md"
    try:
        argv = _editor_argv(path)
    except ValueError as exc:
        # Before the stub, deliberately. Checking afterwards left an empty
        # lesson behind every time $EDITOR was unset — the slug then shows up
        # in `--list` as real, curated knowledge that nobody ever wrote.
        _err(f"error: lesson: {exc}")
        return EXIT_ERROR

    try:
        _ensure_stub(path, slug)
    except OSError as exc:
        _err(f"error: lesson: cannot create {path}: {exc.strerror or exc}")
        return EXIT_ERROR

    # A sidecar per lesson, not one global lock: two people editing two
    # different lessons is fine, and a shared lock would make the second wait
    # for however long the first leaves vi open.
    sidecar = directory / f".{slug}.lock"
    try:
        handle = _lock.try_acquire(sidecar)
    except _lock.LockError as exc:
        _err(f"error: lesson: sidecar lock {sidecar}: {exc}")
        return EXIT_ERROR
    if handle is None:
        # Not a failure: somebody has this file open. Exit 1 so a wrapper reads
        # it as "come back later" rather than as a broken install.
        _err(f"error: lesson: {slug} is being edited by {_holder(sidecar)}, "
             "retry later")
        return EXIT_CONFLICT

    try:
        _stamp(sidecar, actor)
        try:
            code = subprocess.call(argv)
        except OSError as exc:
            _err(f"error: lesson: cannot run editor {argv[0]}: {exc.strerror or exc}")
            return EXIT_ERROR
    finally:
        # The lock file is never unlinked, so a leaked handle wedges this slug
        # for every later edit with no process left to blame.
        handle.release()

    if code != 0:
        # Do not claim a save the editor did not make: an agent that reads
        # "Saved" moves on and the note it thought it wrote is not there.
        _err(f"error: lesson: editor exited with status {code}")
        return EXIT_ERROR
    print(f"Saved global lesson {path}")
    return EXIT_OK


def _ensure_stub(path: Path, slug: str) -> None:
    """Create the lesson with the Go build's four headings if it is new.

    The headings are the format, and an empty file is not one: the value of a
    lesson is in "avoid" and "evidence", which is exactly what gets left out
    when the editor opens on a blank buffer.
    """
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path.write_text(
        f"# {slug}\n\nUse when:\n\nEffective pattern:\n\nAvoid:\n\nEvidence:\n"
        f"- Added {today} after user approval.\n",
        encoding="utf-8",
    )
    # Written to disk before the chmod, so a umask that would have left it
    # group-readable is corrected rather than inherited.
    os.chmod(path, 0o600)


def _stamp(sidecar: Path, actor: str) -> None:
    """Leave a name and a time in the lock file, for whoever collides with it."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        sidecar.write_text(f"{actor}\n{stamp}\n", encoding="utf-8")
    except OSError:
        # Best effort. The flock is what actually excludes the second editor;
        # this is only the label on it, and losing the label must not lose the
        # edit session.
        pass


def _holder(sidecar: Path) -> str:
    try:
        lines = sidecar.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return "another editor"
    if len(lines) >= 2 and lines[0].strip():
        return f"@{lines[0].strip()} since {lines[1].strip()}"
    return "another editor"


# ---------------------------------------------------------------------------
# $EDITOR
# ---------------------------------------------------------------------------


def _editor_argv(path: Path) -> list[str]:
    """$VISUAL, then $EDITOR, then vi — as an argv, never as a shell string."""
    spec = ""
    for name in ("VISUAL", "EDITOR"):
        spec = os.environ.get(name, "").strip()
        if spec:
            break
    if not spec:
        if shutil.which("vi") is None:
            raise ValueError(
                "no editor found. Set $EDITOR (e.g. 'export EDITOR=nano') and retry."
            )
        spec = "vi"

    parts = _split_editor_spec(spec)
    if not parts:
        raise ValueError("empty editor command")
    exe = shutil.which(parts[0])
    if exe is None:
        raise ValueError(f"editor {parts[0]!r} not found")
    return [exe, *parts[1:], str(path)]


def _split_editor_spec(spec: str) -> list[str]:
    """Split an editor spec into words, honouring quotes. No shell is invoked.

    $EDITOR routinely carries arguments — ``code --wait``, ``subl -w`` — and the
    binary path may contain spaces (``"/Applications/Visual Studio Code.app/…"``).
    Passing the string to a shell instead would make an editor setting into an
    execution path for whatever else is in it; splitting on whitespace alone
    would break every quoted path.
    """
    tokens: list[str] = []
    cur: list[str] = []
    quote = ""
    in_word = False
    for ch in spec:
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur.append(ch)
        elif ch in ("'", '"'):
            quote = ch
            in_word = True
        elif ch in (" ", "\t", "\n", "\r"):
            if in_word:
                tokens.append("".join(cur))
                cur = []
                in_word = False
        else:
            cur.append(ch)
            in_word = True
    if quote:
        raise ValueError(f"editor command has an unterminated {quote} quote")
    if in_word:
        tokens.append("".join(cur))
    return tokens
