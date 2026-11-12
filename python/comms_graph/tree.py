"""What the working tree actually says, next to what the log was told.

EVERY GUARD IN THIS TOOL ASKS THE SAME QUESTION: is this path claimed by
somebody else? When the answer is no it reports that as safety. It is not.
"Nobody has declared anything" and "nothing is happening" are different facts,
and comms was rendering the first as the second.

Measured, on two separate surfaces, on real repositories:

- The board printed "no active claims in this repo" while ``git status`` showed
  fifteen changed files. Four of those paths appear ZERO times in the entire
  log. The agent reading that line had every reason to treat the tree as quiet.
- ``check --staged`` passed a commit carrying a staged deletion left behind by a
  departed agent. The check was correct on its own terms: the file was
  unclaimed, so nobody else held it. The deletion shipped.

Both failed in the reassuring direction, which is the only direction that
matters for a guard.

git already knows all of this and nobody has to remember to tell it anything.
That is the whole point: every other signal here depends on an agent choosing to
emit it, and the measured behaviour is that they often do not, especially after
a context compaction. This module is the one input that does not decay.

WHAT IT DOES NOT DO. It does not guess who made a change. A path is attributed
only when the log says something concrete about it: somebody holds a claim
covering it, or somebody released one. Everything else is reported as
unattributed, in those words, because "we do not know who did this" is the
finding. Inferring an author from mtimes or from who was recently active would
manufacture exactly the false confidence this module exists to remove.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import scope as _scope
from . import task as _task

#: git can be slow on a huge tree, and every caller here is on an interactive
#: path (the board, a pre-commit hook, a dashboard refresh). Better to report
#: that the tree could not be read than to hang the thing the user is waiting on.
GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Change:
    """One path git considers changed, and how."""

    path: str
    #: The two porcelain columns, verbatim. Kept rather than reduced to flags
    #: because a caller that wants to explain itself needs the original letters.
    index_code: str = " "
    tree_code: str = " "

    @property
    def staged(self) -> bool:
        return self.index_code not in (" ", "?")

    @property
    def unstaged(self) -> bool:
        return self.tree_code not in (" ", "?")

    @property
    def untracked(self) -> bool:
        return self.index_code == "?" or self.tree_code == "?"

    @property
    def deleted(self) -> bool:
        """A deletion on either side.

        Called out separately everywhere because it is the change that is
        hardest to notice and least recoverable by reading a diff: an added file
        announces itself, a removed one is an absence.
        """
        return "D" in (self.index_code, self.tree_code)

    def how(self) -> str:
        """Plain words. The porcelain letters mean nothing to a reader."""
        if self.untracked:
            return "new, not in git yet"
        bits = []
        if self.index_code == "D" or self.tree_code == "D":
            bits.append("deleted")
        elif self.index_code == "A":
            bits.append("added")
        elif self.index_code == "R":
            bits.append("renamed")
        else:
            bits.append("changed")
        bits.append("staged" if self.staged else "not staged")
        return ", ".join(bits)


@dataclass(frozen=True)
class Attributed:
    """A change, plus whatever the log can honestly say about who made it."""

    change: Change
    #: "" when the log says nothing. Never a guess.
    actor: str = ""
    #: held | released | "": how we know, so a reader can weigh it.
    basis: str = ""
    #: The claim's stated intent, when there is a live claim.
    intent: str = ""

    @property
    def known(self) -> bool:
        return bool(self.actor)


@dataclass
class TreeReport:
    changes: list[Attributed] = field(default_factory=list)
    #: Set when git could not be consulted at all. A caller MUST distinguish
    #: this from an empty change list: one means "clean", the other means "we
    #: do not know", and collapsing them is the bug this module is about.
    unavailable: str = ""

    @property
    def readable(self) -> bool:
        return not self.unavailable

    @property
    def unattributed(self) -> list[Attributed]:
        return [a for a in self.changes if not a.known]

    @property
    def staged(self) -> list[Attributed]:
        return [a for a in self.changes if a.change.staged]

    def by_actor(self) -> dict[str, list[Attributed]]:
        out: dict[str, list[Attributed]] = {}
        for a in self.changes:
            out.setdefault(a.actor, []).append(a)
        return out


def read(root: Any, timeout: int = GIT_TIMEOUT_SECONDS) -> TreeReport:
    """Ask git what has changed. Never raises.

    ``--porcelain=v1 -z`` is the stable, documented, machine-readable form.
    ``-z`` matters for correctness, not tidiness: the human format quotes and
    backslash-escapes any path with a space or a non-ASCII byte in it, so a
    German filename comes back mangled and a path with a newline could forge a
    second entry. NUL separation has neither problem.
    """
    git = shutil.which("git")
    if git is None:
        return TreeReport(unavailable="git is not installed")
    try:
        out = subprocess.run(
            [git, "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
            cwd=str(root), capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TreeReport(unavailable=f"git status did not answer within {timeout}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return TreeReport(unavailable=f"git status could not run: {exc}")
    if out.returncode != 0:
        err = out.stderr.decode("utf-8", "replace").strip().splitlines()
        return TreeReport(unavailable=(err[0] if err else
                                       f"git status exited {out.returncode}"))

    fields = out.stdout.decode("utf-8", "replace").split("\0")
    changes: list[Change] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        # "XY path". The two status columns are fixed width and a single space
        # follows them; a path may itself begin with spaces, so slice rather
        # than split.
        codes, path = entry[:2], entry[3:]
        if not path:
            continue
        if codes and codes[0] in ("R", "C"):
            # A rename/copy spends a SECOND NUL-separated field on the old path.
            # Consuming it is not optional: leave it and the next loop reads the
            # old path as though it were a status line, producing a garbage
            # entry with a two-character path prefix silently removed from it.
            i += 1
        changes.append(Change(path=path, index_code=codes[0] if codes else " ",
                              tree_code=codes[1] if len(codes) > 1 else " "))
    changes.sort(key=lambda c: c.path)
    return TreeReport(changes=[Attributed(change=c) for c in changes])


def _scope_path(raw: Any) -> str:
    """The path half of a scope, whether it arrives parsed or as text.

    State holds a live claim's scope as a parsed ``Scope``; a release records
    its freed scopes as plain strings. Passing the first to ``parse`` raises,
    and an over-broad ``except`` here turned every live claim into "nobody has
    claimed this" on the very board built to stop that answer being wrong.
    Handle both shapes rather than catching the mistake.
    """
    path = getattr(raw, "path", None)
    if isinstance(path, str):
        return path
    return _scope.parse(str(raw)).path


def _covers(claim_scope: Any, path: str) -> bool:
    """Does a claim's scope cover this path?

    Reuses the same overlap test the claim guard uses, so the board cannot
    disagree with the thing that blocks an edit. A scope that genuinely will not
    parse does not cover anything: a claim nobody can interpret must not be
    allowed to vouch for a file.
    """
    try:
        return _scope.paths_overlap(_scope_path(claim_scope), path)
    except _scope.ScopeError:
        return False


def _release_path_index(releases: list[Any]):
    """Index literal release paths once; keep wildcard scopes for exact matching.

    Dirty-tree attribution used to compare every changed path with every scope
    in the full release history. Real stores have thousands of releases and
    hundreds of changed paths, so that quadratic pass could hold the dashboard
    for seconds before its first event arrived.

    For literal paths, ``paths_overlap`` means equality or an ancestor/child
    relationship (either side may name a directory). ``literal`` answers the
    ancestor half and ``subtree`` the child half. Wildcards retain the original
    matcher because reducing their intersection rules to prefixes would change
    which release is credited.
    """
    literal: dict[str, tuple[int, Any]] = {}
    subtree: dict[str, tuple[int, Any]] = {}
    wildcard: list[tuple[int, Any, Any]] = []
    ordered: list[tuple[int, Any, Any]] = []

    for sequence, rel in enumerate(releases):
        for raw_scope in rel.scopes or []:
            try:
                path = _scope_path(raw_scope)
            except _scope.ScopeError:
                # Same outcome as _covers(): an unreadable historic scope does
                # not attribute any current file.
                continue
            ordered.append((sequence, rel, raw_scope))
            if "*" in path:
                wildcard.append((sequence, rel, raw_scope))
                continue

            parts = [part for part in path.split("/") if part]
            if not parts:
                continue
            canonical = "/".join(parts)
            literal[canonical] = (sequence, rel)
            for end in range(1, len(parts) + 1):
                subtree["/".join(parts[:end])] = (sequence, rel)
    return literal, subtree, wildcard, ordered


def _latest_release_for_path(path: str, literal, subtree, wildcard, ordered):
    """Return the newest release whose scope covers one dirty path."""
    if "*" in path:
        # Git paths may legally contain a literal star. paths_overlap has always
        # interpreted both arguments as patterns, so preserve that existing
        # (slightly surprising) answer instead of sending it through the prefix
        # fast path, which treats the changed path as concrete.
        for _sequence, rel, raw_scope in reversed(ordered):
            if _covers(raw_scope, path):
                return rel
        return None

    parts = [part for part in path.split("/") if part]
    canonical = "/".join(parts)
    best = subtree.get(canonical)
    best_sequence = best[0] if best is not None else -1

    # A literal release on any parent covers the changed child. Exact paths are
    # included and harmlessly considered twice; sequence comparison keeps the
    # result stable.
    for end in range(1, len(parts) + 1):
        candidate = literal.get("/".join(parts[:end]))
        if candidate is not None and candidate[0] > best_sequence:
            best = candidate
            best_sequence = candidate[0]

    # Newest first, matching the old "last chronological match wins" rule.
    # Once entries are older than a literal winner, none can replace it.
    for sequence, rel, raw_scope in reversed(wildcard):
        if sequence <= best_sequence:
            break
        if _covers(raw_scope, path):
            best = (sequence, rel)
            break
    return best[1] if best is not None else None


def attribute(report: TreeReport, state: Any) -> TreeReport:
    """Say who the log knows about for each changed path, and nothing more.

    Live claims win over releases, because "somebody is holding this right now"
    is a different and more urgent fact than "somebody had it earlier". Within
    releases the most recent wins, since the point is who touched it last.
    """
    if not report.readable or not report.changes:
        return report

    claims = list(getattr(state, "claims", {}).values())
    releases = list(getattr(state, "releases", []) or [])
    (literal_releases, release_subtrees,
     wildcard_releases, ordered_releases) = _release_path_index(releases)

    out: list[Attributed] = []
    for a in report.changes:
        path = a.change.path
        holder = next((c for c in claims if _covers(c.scope, path)), None)
        if holder is not None:
            out.append(Attributed(change=a.change, actor=holder.actor, basis="held",
                                  intent=getattr(holder, "intent", "") or ""))
            continue
        # Releases are chronological; the index retains their sequence so the
        # latest matching literal or wildcard scope still wins.
        prior = _latest_release_for_path(
            path, literal_releases, release_subtrees,
            wildcard_releases, ordered_releases,
        )
        if prior is not None:
            # original_actor is who HELD it. On an arbitrated release the
            # `actor` field is whoever took it away, and naming them as the
            # person who changed the file would be exactly wrong.
            who = getattr(prior, "original_actor", "") or prior.actor
            out.append(Attributed(change=a.change, actor=who, basis="released"))
            continue
        out.append(Attributed(change=a.change))
    return TreeReport(changes=out, unavailable=report.unavailable)


def survey(root: Any, state: Any, timeout: int = GIT_TIMEOUT_SECONDS) -> TreeReport:
    """read() then attribute(). The one call every surface should make."""
    return attribute(read(root, timeout=timeout), state)


def headline(report: TreeReport) -> str:
    """One line for a surface that has room for one line.

    Returns "" when the tree is clean AND readable, because there is nothing to
    say. Every other case says something, including the unreadable one.
    """
    if not report.readable:
        return f"working tree: not known ({report.unavailable})"
    n = len(report.changes)
    if not n:
        return ""
    unknown = len(report.unattributed)
    if unknown == n:
        return (f"{n} changed file(s) in the working tree, "
                f"none of them claimed by anybody")
    if unknown:
        return (f"{n} changed file(s) in the working tree, "
                f"{unknown} not claimed by anybody")
    return f"{n} changed file(s) in the working tree, all claimed"


def lines(report: TreeReport, limit: int = 40) -> list[str]:
    """The full picture, grouped by who the log knows about.

    Unattributed paths come LAST but are the reason this exists, so they are
    never the group that gets truncated away: the cap is spent on the groups
    above them first.
    """
    if not report.readable:
        return [f"  the working tree could not be read: {report.unavailable}"]
    if not report.changes:
        return []

    unknown = report.unattributed
    known = [a for a in report.changes if a.known]

    out: list[str] = []
    budget = max(limit - len(unknown), 0)
    groups: dict[str, list[Attributed]] = {}
    for a in known:
        groups.setdefault(a.actor, []).append(a)
    for actor in sorted(groups):
        rows = groups[actor]
        basis = "holding" if any(r.basis == "held" for r in rows) else "released"
        out.append(f"  @{actor} ({basis}):")
        for r in rows[:budget]:
            out.append(f"    {r.change.path}  {r.change.how()}")
        if len(rows) > budget:
            out.append(f"    ... and {len(rows) - budget} more")
        budget = max(budget - len(rows), 0)

    if unknown:
        out.append(f"  nobody has claimed these ({len(unknown)}):")
        for r in unknown[:limit]:
            out.append(f"    {r.change.path}  {r.change.how()}")
        if len(unknown) > limit:
            out.append(f"    ... and {len(unknown) - limit} more")
    return out


def foreign_staged_deletions(report: TreeReport, actor: str) -> list[Attributed]:
    """Staged deletions of ground the log ties to somebody other than `actor`.

    The narrow, high-confidence case worth REFUSING a commit over. A departed
    agent released a claim after staging a deletion; the next agent's `git add`
    swept it up, the staged check found no live claim on it and passed, and the
    deletion shipped in somebody else's commit.

    Deliberately narrow. Blocking every unattributed staged path would fire on
    new files, generated files and anything edited through a shell heredoc,
    which is most of them, and a guard that cries wolf gets disabled.
    """
    mine = _task.base_actor(actor.lstrip("@")) if actor else ""
    out = []
    for a in report.staged:
        if not a.change.deleted or not a.known:
            continue
        # base_actor so a role suffix does not make you a stranger to your own
        # ground: claude-dev/review staging claude-dev's deletion is one agent.
        if _task.base_actor(a.actor.lstrip("@")) != mine:
            out.append(a)
    return out


def paths(entries: Iterable[Attributed]) -> list[str]:
    return [a.change.path for a in entries]
