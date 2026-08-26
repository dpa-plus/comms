"""Wire the commit check into git, instead of asking agents to remember it.

``check --staged`` has always refused a commit that stages ground somebody else
is holding. It works. It is correct. It caught nothing, three times, because
nothing runs it.

The third incident is the clearest: an agent committed a translation file while
another agent HELD the claim on it, and seventeen of the holder's in-flight keys
shipped inside somebody else's commit. The guard would have refused that, by
name, with the holder's claim id in the message. Nobody typed the command.

So this is not a detection problem and no amount of better checking fixes it.
The instruction to install a pre-commit hook has been in the skill from the
beginning, as prose, and prose decays: measured, one agent's claim discipline
did not survive a single context compaction. A rule that only exists as a
sentence someone has to remember is a rule that holds right up until the moment
it matters.

THE HOOK IS THE PRODUCT HERE, not the check. Three details decide whether it
actually runs, and each of them fails silently:

1. ``core.hooksPath``. When a repo sets it, ``.git/hooks`` is dead ground and a
   hook written there never executes. ``git rev-parse --git-path hooks`` is the
   only spelling that answers correctly, and it also handles a worktree, where
   ``.git`` is a FILE and the hooks live in the main checkout.
2. PATH. Git hooks do not inherit an interactive shell's environment, so
   ``comms-graph`` is frequently not on PATH inside one. The absolute path of
   the binary that installed the hook is baked in, with a PATH lookup as the
   fallback for when it moves.
3. A hook that is already there. Clobbering somebody's existing pre-commit is
   not acceptable, and neither is silently doing nothing.

FAILING CLOSED IS DELIBERATE. If the guard cannot run at all, the commit is
refused rather than waved through. Every bug this exists to fix was a guard
answering "nothing to worry about" when it had established nothing, and a
missing binary is exactly that case. The refusal prints the one command that
removes the hook, so the escape is always one copy-paste away.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: How the hook recognises itself on a reinstall or an uninstall. Matched as a
#: substring of the file, so it must not be something a hand-written hook would
#: plausibly contain.
MARKER = "comms-graph-commit-guard-v1"

#: Where a pre-existing foreign hook is moved so it can still run first.
CHAINED_NAME = "pre-commit.before-comms"


@dataclass
class GuardStatus:
    #: absent | ours | foreign | unknown
    state: str = "unknown"
    path: Path | None = None
    #: Why we cannot tell, when state is "unknown".
    reason: str = ""
    #: True when a foreign hook was moved aside and is chained ahead of ours.
    chained: bool = False

    @property
    def installed(self) -> bool:
        return self.state == "ours"


def hooks_dir(root: Any) -> tuple[Path | None, str]:
    """The directory git will actually look in. Returns (path, why_not).

    ``git rev-parse --git-path hooks`` rather than ``root/.git/hooks``, because
    the naive spelling is wrong in two ordinary situations and wrong SILENTLY in
    both: a repo with ``core.hooksPath`` set ignores ``.git/hooks`` entirely,
    and inside a worktree ``.git`` is a file. Either way the hook would be
    written to a path git never reads and would report success.
    """
    git = shutil.which("git")
    if git is None:
        return None, "git is not installed"
    try:
        out = subprocess.run([git, "rev-parse", "--git-path", "hooks"],
                             cwd=str(root), capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git could not be asked where hooks live: {exc}"
    if out.returncode != 0:
        err = out.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, (err[0] if err else "this is not a git repository")
    rel = out.stdout.decode("utf-8", "replace").strip()
    if not rel:
        return None, "git named no hooks directory"
    p = Path(rel)
    return (p if p.is_absolute() else Path(root) / p), ""


def _binary() -> str:
    """The absolute path to write into the hook.

    ``sys.argv[0]`` is the console script when comms-graph is run as one, which
    is the case that matters. Falling back to a PATH lookup and then to the bare
    name keeps this working when it is invoked as ``python -m comms_graph``.
    """
    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 and argv0.is_file() and argv0.name.startswith("comms"):
        return str(argv0)
    found = shutil.which("comms-graph")
    return found or "comms-graph"


def script(binary: str = "", hook_path: str = "", chained: bool = False) -> str:
    """The hook itself. POSIX sh, because git runs it with sh, not bash."""
    binary = binary or _binary()
    chain = ""
    if chained:
        chain = (
            '\n# A pre-commit hook was already here when the guard was installed.\n'
            '# It was moved aside and still runs first; its veto still counts.\n'
            'PRIOR="$(dirname "$0")/' + CHAINED_NAME + '"\n'
            'if [ -x "$PRIOR" ]; then "$PRIOR" "$@" || exit $?; fi\n'
        )
    return f"""#!/bin/sh
# {MARKER}
# Installed by `comms-graph guard install`. Refuses a commit that would stage
# ground another agent is holding, or carry a staged deletion of theirs.
# Remove it with:  comms-graph guard uninstall
{chain}
GUARD='{binary}'
if [ ! -x "$GUARD" ]; then
  GUARD="$(command -v comms-graph 2>/dev/null)"
fi
if [ -z "$GUARD" ]; then
  echo "comms-graph commit guard: comms-graph is not on PATH and the copy this" >&2
  echo "  hook was installed from is gone, so NOTHING about this commit was" >&2
  echo "  checked. Refusing rather than assuming it is fine." >&2
  echo "" >&2
  echo "  Reinstall comms-graph, or remove the guard:" >&2
  echo "    rm {hook_path or '"$0"'}" >&2
  exit 1
fi
exec "$GUARD" check --staged
"""


def status(root: Any) -> GuardStatus:
    """Is the guard actually wired into this repo? Never raises."""
    hooks, why = hooks_dir(root)
    if hooks is None:
        return GuardStatus(state="unknown", reason=why)
    path = hooks / "pre-commit"
    try:
        if not path.exists():
            return GuardStatus(state="absent", path=path)
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return GuardStatus(state="unknown", path=path,
                           reason=f"the hook could not be read: {exc}")
    if MARKER in body:
        return GuardStatus(state="ours", path=path,
                           chained=(hooks / CHAINED_NAME).exists())
    return GuardStatus(state="foreign", path=path)


def install(root: Any, chain: bool = False) -> tuple[bool, list[str]]:
    """Write the hook. Returns (ok, lines to print).

    Refuses to overwrite a hook it did not write unless ``chain`` is set, in
    which case the existing one is moved aside and still runs first. Silently
    replacing somebody's build or lint hook would be a worse bug than the one
    this fixes.
    """
    st = status(root)
    if st.state == "unknown":
        return False, [f"guard install: {st.reason}"]
    assert st.path is not None
    hooks = st.path.parent

    was_chained = False
    if st.state == "foreign":
        if not chain:
            return False, [
                f"guard install: {st.path} already exists and comms did not write it.",
                "  It is not going to be overwritten. Either move it aside yourself,",
                "  or run the guard after it:",
                "    comms-graph guard install --chain",
                "  which keeps the existing hook, runs it FIRST, and still lets its",
                "  refusal stop the commit.",
            ]
        try:
            st.path.replace(hooks / CHAINED_NAME)
        except OSError as exc:
            return False, [f"guard install: could not move the existing hook aside: {exc}"]
        was_chained = True
    elif st.state == "ours":
        was_chained = st.chained

    try:
        hooks.mkdir(parents=True, exist_ok=True)
        st.path.write_text(script(hook_path=str(st.path), chained=was_chained),
                           encoding="utf-8")
        # 0o755. A hook without the execute bit is skipped by git in silence,
        # which would install a guard that never runs and report success.
        st.path.chmod(st.path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        return False, [f"guard install: could not write {st.path}: {exc}"]

    out = [f"guard installed at {st.path}"]
    if was_chained:
        out.append(f"  the hook that was there runs first, kept as {CHAINED_NAME}")
    out.append("  every commit in this repo is now checked, whether anybody "
               "remembers to or not")
    return True, out


def uninstall(root: Any) -> tuple[bool, list[str]]:
    """Remove our hook and restore a chained one, if there was one."""
    st = status(root)
    if st.state == "unknown":
        return False, [f"guard uninstall: {st.reason}"]
    assert st.path is not None
    if st.state == "absent":
        return True, ["guard uninstall: nothing to remove, no hook is installed"]
    if st.state == "foreign":
        return False, [
            f"guard uninstall: {st.path} was not written by comms, so it is left alone.",
        ]
    prior = st.path.parent / CHAINED_NAME
    try:
        st.path.unlink()
        if prior.exists():
            prior.replace(st.path)
    except OSError as exc:
        return False, [f"guard uninstall: {exc}"]
    out = ["guard removed; commits in this repo are no longer checked"]
    if prior.exists() or st.chained:
        out.append("  the hook that was there before it has been put back")
    return True, out


def describe(st: GuardStatus) -> str:
    """One line for a surface with room for one line."""
    if st.state == "ours":
        return "commit guard: installed" + (" (chained)" if st.chained else "")
    if st.state == "absent":
        return ("commit guard: NOT installed, so a commit here is not checked "
                "(comms-graph guard install)")
    if st.state == "foreign":
        return ("commit guard: NOT installed; another pre-commit hook is in place "
                "(comms-graph guard install --chain)")
    return f"commit guard: not known ({st.reason})"
