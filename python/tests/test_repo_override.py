"""Naming the repository from outside it.

WHY THIS EXISTS AT ALL. On macOS a checkout under Desktop, Documents or
Downloads can lose privacy access to the running process, and when it does,
everything that calls getcwd() starts failing at once — comms, git, node. The
documented way out is to stop relying on the working directory: either pass the
repository explicitly, or export it once for the session.

That recovery is worth a test precisely because it is used at the worst moment.
It was missing from this build entirely — the briefing taught `--repo`, and this
build answered "unknown comms command '--repo'" — which is the sort of gap that
is invisible until the one day it is not.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from comms_graph import cli


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("COMMS_ACTOR", "alice")
    monkeypatch.delenv("COMMS_REPO", raising=False)
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.py").write_text("x = 1\n")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=r, check=True)
    # Stand somewhere else entirely: the point is that cwd must not decide.
    monkeypatch.chdir(tmp_path)
    return r


def test_the_repository_can_be_named_before_the_verb(repo, capsys):
    """`comms-graph --repo <path> board`, the spelling the briefing teaches."""
    assert cli.main(["--repo", str(repo), "board"]) == 0
    assert "claims" in capsys.readouterr().out.lower()


def test_root_is_accepted_there_too(repo):
    """--root is this build's per-verb spelling; it must work globally as well,
    or which spelling works depends on where in the line you put it."""
    assert cli.main(["--root", str(repo), "board"]) == 0


def test_the_environment_variable_works_for_a_whole_session(repo, monkeypatch):
    """`export COMMS_REPO=...` once, rather than a flag on every command."""
    monkeypatch.setenv("COMMS_REPO", str(repo))
    assert cli.main(["board"]) == 0


def test_the_flag_beats_the_environment(repo, tmp_path, monkeypatch):
    """Both set is not an error, but it must be predictable: the one typed on
    this command wins over one exported minutes ago."""
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    monkeypatch.setenv("COMMS_REPO", str(other))
    assert cli.main(["--repo", str(repo), "board"]) == 0
    # And it must not LEAK: the flag colours this call only. An environment
    # write would outlive it and hand the next caller a repo it never named.
    assert os.environ["COMMS_REPO"] == str(other)


def test_a_typo_is_refused_rather_than_opening_a_private_ledger(repo, monkeypatch, capsys):
    """IF THIS FAILS: a mistyped path silently becomes a board nobody reads.

    The claim is recorded, the command exits 0, and the agent is told it holds
    ground on a log no peer will ever look at. That is worse than an error,
    which is why the flag already refuses a non-directory and the variable now
    does too.
    """
    monkeypatch.setenv("COMMS_REPO", str(repo) + "-nope")
    with pytest.raises(SystemExit) as e:
        cli.main(["board"])
    assert e.value.code != 0
    assert "COMMS_REPO" in capsys.readouterr().err


def test_the_flag_needs_a_path(repo, capsys):
    assert cli.main(["--repo"]) != 0
    assert "needs a path" in capsys.readouterr().err


def test_an_at_prefixed_name_is_the_same_agent(repo, monkeypatch, capsys):
    """IF THIS FAILS: an agent is locked out of its own files and told nothing.

    Every surface PRINTS an actor as "@name", so an agent copying its own name
    off the board and passing it back arrives as "@name" — and a stored "@name"
    was a DIFFERENT agent from "name". Found on a real store: one agent held 24
    events under "@claude-karte-fachebenen" while everyone else was plain.

    The `release --all-mine` half is the dangerous one. It did not error. It said
    "nothing to release", which reads exactly like success, so the agent believes
    it has let go of ground it is still holding.
    """
    monkeypatch.setenv("COMMS_ACTOR", "@bob")
    assert cli.main(["claim", "src/a.py", "--intent", "mine"]) == 0
    capsys.readouterr()

    monkeypatch.setenv("COMMS_ACTOR", "bob")
    assert cli.main(["check", "src/a.py"]) == 0, "blocked from its own file"
    assert cli.main(["release", "--all-mine", "--result", "done"]) == 0
    assert "RELEASED" in capsys.readouterr().out, "could not release its own claim"


def test_an_at_prefixed_name_already_on_disk_heals(repo, monkeypatch, capsys):
    """The log is append-only, so events written before the writer refused "@"
    are still there. Both spellings must fold to one agent or that agent stays
    locked out forever — folding on READ is the only repair the format allows."""
    from datetime import datetime, timezone

    from comms_graph import log as clog

    log_file = clog.log_path(repo)
    clog.append(log_file, clog.Event(
        ts=datetime.now(timezone.utc), id=clog.new_id(), actor="@carol",
        type=clog.TYPE_CLAIM, scope=["src/b.py"], data={"intent": "written the old way"},
    ))
    monkeypatch.setenv("COMMS_ACTOR", "carol")
    assert cli.main(["check", "src/b.py"]) == 0, "still locked out of its own claim"
    cli.main(["board"])
    assert "@@" not in capsys.readouterr().out
