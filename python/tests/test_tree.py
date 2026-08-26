"""The working tree, read from git rather than taken on trust.

Every test here corresponds to a way comms was measured reporting the absence
of declarations as the absence of work. If one of them fails, a guard has gone
back to failing in the reassuring direction, which is the only failure that
matters for a guard: an agent reads "nothing to worry about" and proceeds.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

from comms_graph import tree as ctree

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-q", "."], cwd=repo, check=True)
    subprocess.run([GIT, "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run([GIT, "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def _commit(repo, **files):
    for name, body in files.items():
        (repo / name.replace("__", "/")).write_text(body)
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
    subprocess.run([GIT, "commit", "-qm", "x"], cwd=repo, check=True)


def _cli(repo, *args, session=None):
    from comms_graph import cli as ccli

    out = io.StringIO()
    was = os.getcwd()
    prior = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.chdir(repo)
    try:
        if session is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = session
        else:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        with redirect_stdout(out), redirect_stderr(out):
            code = ccli.main(list(args))
    finally:
        os.chdir(was)
        if prior is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior
    return code, out.getvalue()


def _state(repo):
    from comms_graph import cli as ccli

    return ccli._read_state(ccli._store(ccli._repo_root(str(repo)), create=False)[0])


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def test_a_clean_tree_and_an_unreadable_one_are_not_the_same_answer(tmp_path):
    """IF THIS FAILS: every caller collapses "we could not look" into "nothing
    is happening", which is the whole bug in one line."""
    repo = tmp_path / "not-a-repo"
    repo.mkdir()
    report = ctree.read(repo)
    assert not report.readable
    assert report.unavailable
    assert ctree.headline(report).startswith("working tree: not known")


def test_a_rename_does_not_smuggle_the_old_path_in_as_a_status_line(tmp_path, monkeypatch):
    """IF THIS FAILS: a rename produces a phantom entry whose first two
    characters have been silently eaten, because porcelain -z spends a second
    NUL-separated field on the old path and the parser read it as a new line."""
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, before_ts="x\n")
    subprocess.run([GIT, "mv", "before_ts", "after_ts"], cwd=repo, check=True)

    report = ctree.read(repo)
    assert report.readable
    assert [c.change.path for c in report.changes] == ["after_ts"], report.changes


def test_a_path_with_a_space_and_an_umlaut_survives(tmp_path, monkeypatch):
    """IF THIS FAILS: git's human format has quoted and backslash-escaped the
    path, and comms is reporting a filename that does not exist."""
    repo = _repo(tmp_path, monkeypatch)
    name = "src/Gebäude Farben.ts"
    (repo / "src").mkdir()
    _commit(repo, **{"src__Gebäude Farben.ts": "x\n"})
    (repo / name).write_text("y\n")

    report = ctree.read(repo)
    assert [c.change.path for c in report.changes] == [name], report.changes


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


def test_a_live_claim_attributes_a_dirty_file(tmp_path, monkeypatch):
    """IF THIS FAILS: the board says nobody has claimed a file its own claim
    list is showing. State keeps a claim's scope PARSED and a release's as
    text; a covers() that handles only one shape returns a silent no."""
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, a_ts="x\n")
    _cli(repo, "claim", "a_ts", "--as", "alpha", "--intent", "rework it", session="sA")
    (repo / "a_ts").write_text("y\n")

    report = ctree.survey(repo, _state(repo))
    got = report.changes[0]
    assert got.actor == "alpha", report.changes
    assert got.basis == "held"
    assert got.intent == "rework it"
    assert not report.unattributed


def test_a_released_claim_still_says_who_had_it(tmp_path, monkeypatch):
    """IF THIS FAILS: a file somebody released and left uncommitted appears
    under neither "working now" nor "claimed by nobody", so it drops off the
    board entirely at the exact moment it is nobody's responsibility."""
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, a_ts="x\n")
    _cli(repo, "claim", "a_ts", "--as", "alpha", "--intent", "i", session="sA")
    (repo / "a_ts").write_text("y\n")
    _cli(repo, "release", "--all-mine", "--as", "alpha", "--result", "done", session="sA")

    report = ctree.survey(repo, _state(repo))
    assert report.changes[0].actor == "alpha"
    assert report.changes[0].basis == "released"


def test_a_file_nobody_declared_is_reported_as_unknown_not_guessed(tmp_path, monkeypatch):
    """IF THIS FAILS: comms has started inferring an author from who happened to
    be active, which manufactures exactly the false confidence this exists to
    remove. "We do not know who did this" IS the finding."""
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, a_ts="x\n", b_ts="x\n")
    _cli(repo, "claim", "a_ts", "--as", "alpha", "--intent", "i", session="sA")
    (repo / "b_ts").write_text("touched by nobody who said so\n")

    report = ctree.survey(repo, _state(repo))
    loose = report.unattributed
    assert [a.change.path for a in loose] == ["b_ts"], report.changes
    assert loose[0].actor == ""


# ---------------------------------------------------------------------------
# the board
# ---------------------------------------------------------------------------


def test_the_board_does_not_call_a_dirty_tree_quiet(tmp_path, monkeypatch):
    """IF THIS FAILS: the reported bug is back. The board printed "no active
    claims in this repo" while git status showed fifteen changed files, four of
    which appear nowhere in the log. It was believed, because it reads as an
    all-clear."""
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, a_ts="x\n", b_ts="x\n")
    _cli(repo, "note", "so the log exists", "--as", "alpha", session="sA")
    (repo / "a_ts").write_text("y\n")
    (repo / "b_ts").write_text("y\n")

    code, out = _cli(repo, "board", "--as", "alpha", session="sA")
    assert code == 0, out
    assert "the tree is not quiet" in out, out
    assert "2 changed file(s)" in out, out
    assert "a_ts" in out and "b_ts" in out, out


def test_the_board_says_so_plainly_when_there_is_genuinely_nothing(tmp_path, monkeypatch):
    """IF THIS FAILS: the fix has made a quiet repo look alarming, which is how
    a guard gets ignored."""
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, a_ts="x\n")
    _cli(repo, "note", "hi", "--as", "alpha", session="sA")

    code, out = _cli(repo, "board", "--as", "alpha", session="sA")
    assert code == 0, out
    assert "no uncommitted changes" in out, out


# ---------------------------------------------------------------------------
# check --staged
# ---------------------------------------------------------------------------


def test_a_staged_deletion_of_somebody_elses_ground_is_refused(tmp_path, monkeypatch):
    """IF THIS FAILS: the reported incident ships again. An agent released a
    claim after staging a deletion; the next agent's `git add` swept it up,
    check --staged found no LIVE claim on it and passed, and the deletion went
    out in somebody else's commit."""
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, keep_ts="x\n", colours_ts="y\n")
    _cli(repo, "claim", "colours_ts", "--as", "mapper", "--intent", "drop it", session="sM")
    (repo / "colours_ts").unlink()
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
    _cli(repo, "release", "--all-mine", "--as", "mapper", "--result", "done", session="sM")

    _cli(repo, "hello", "--as", "other", session="sO")
    code, out = _cli(repo, "check", "--staged", "--as", "other", session="sO")
    # 1, not 2: git aborts a commit on any non-zero, and 2 is reserved here for
    # "comms could not find out".
    assert code == 1, out
    assert "colours_ts" in out, out
    assert "@mapper" in out, out


def test_your_own_staged_deletion_is_not_somebody_elses(tmp_path, monkeypatch):
    """IF THIS FAILS: the guard blocks the agent that did the right thing, which
    is how a guard gets turned off."""
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, gone_ts="x\n")
    _cli(repo, "claim", "gone_ts", "--as", "alpha", "--intent", "remove it", session="sA")
    (repo / "gone_ts").unlink()
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)

    code, out = _cli(repo, "check", "--staged", "--as", "alpha", session="sA")
    assert code == 0, out


def test_staged_paths_nobody_claimed_are_named_rather_than_passed_over(tmp_path, monkeypatch):
    """IF THIS FAILS: the guard reports "checked, none held by anybody else" and
    stops, which reads as "all of this was verified" when in fact nothing about
    these paths was verified at all. It must not block them, though: after a
    compaction or a run of heredoc edits that is most of a commit."""
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, a_ts="x\n")
    _cli(repo, "hello", "--as", "alpha", session="sA")
    (repo / "a_ts").write_text("y\n")
    (repo / "new_ts").write_text("z\n")
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)

    code, out = _cli(repo, "check", "--staged", "--as", "alpha", session="sA")
    assert code == 0, out
    assert "claimed by nobody" in out, out
    assert "a_ts" in out and "new_ts" in out, out
