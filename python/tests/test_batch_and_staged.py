"""Two things the briefing has always taught that this build could not do.

Both were found by auditing the skill file against the CLI rather than by a
failing test, which is the point: an instruction file is a promise to whoever
types it, and nothing in a test suite types it.

* `claim a b c --intent "..."` — a task boundary is usually several files, and
  taking them one at a time leaves the half-held state where you have two of
  three and somebody else has the third.
* `check --staged` — the guard that stops you committing somebody else's
  claimed work.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from comms_graph import cli


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.delenv("COMMS_REPO", raising=False)
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    for name in ("a.ts", "b.ts", "c.ts"):
        (r / "src" / name).write_text("x = 1\n")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=r, check=True)
    subprocess.run([git, "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run([git, "config", "user.name", "t"], cwd=r, check=True)
    monkeypatch.chdir(r)
    return r


def _as(monkeypatch, who):
    monkeypatch.setenv("COMMS_ACTOR", who)


def test_several_scopes_are_claimed_together(repo, monkeypatch):
    _as(monkeypatch, "alice")
    assert cli.main(["claim", "src/a.ts", "src/b.ts", "src/c.ts",
                     "--intent", "rework auth"]) == 0


def test_a_batch_takes_all_of_it_or_none_of_it(repo, monkeypatch, capsys):
    """IF THIS FAILS: an agent ends up holding half a task boundary.

    That is the state with no good move in it — you hold two files, somebody
    else holds the third, and neither of you can finish without handing
    something back. Partial acquisition is worse than refusal.
    """
    _as(monkeypatch, "alice")
    cli.main(["claim", "src/c.ts", "--intent", "mine"])
    capsys.readouterr()

    _as(monkeypatch, "bob")
    assert cli.main(["claim", "src/a.ts", "src/c.ts", "--intent", "bob's set"]) == 1
    capsys.readouterr()

    # The scope bob could have had must NOT be his.
    cli.main(["board"])
    board = capsys.readouterr().out
    assert "bob" not in board, f"bob kept part of a refused batch:\n{board}"


def test_stealing_takes_exactly_one_scope(repo, monkeypatch, capsys):
    """A claim id names one claim, so it cannot say what to displace across
    several. Go refuses this combination too."""
    _as(monkeypatch, "bob")
    assert cli.main(["claim", "src/a.ts", "src/b.ts", "--intent", "x",
                     "--steal", "01ABC", "--reason", "y"]) == 2
    assert "exactly one scope" in capsys.readouterr().err


def test_a_batch_must_say_what_the_scopes_have_in_common(repo, monkeypatch):
    _as(monkeypatch, "bob")
    assert cli.main(["claim", "src/a.ts", "src/b.ts"]) == 2


def test_one_scope_twice_in_a_batch_is_refused(repo, monkeypatch):
    """Two rows for one file makes the board's claim count wrong and leaves it
    ambiguous which intent is current."""
    _as(monkeypatch, "bob")
    assert cli.main(["claim", "src/a.ts", "src/a.ts", "--intent", "dup"]) == 2


def test_a_batch_cannot_be_used_to_share_a_task(repo, monkeypatch, capsys):
    """One task, one agent — the rule the single-scope path enforces. A batch
    must not be the way round it."""
    _as(monkeypatch, "alice")
    cli.main(["task", "add", "auth-api", "--title", "Auth"])
    cli.main(["claim", "src/a.ts", "--task", "auth-api", "--intent", "mine"])
    capsys.readouterr()
    _as(monkeypatch, "bob")
    assert cli.main(["claim", "src/b.ts", "src/c.ts",
                     "--task", "auth-api", "--intent", "also mine"]) == 1
    assert "one agent at a time" in capsys.readouterr().err


def _stage(repo, *names):
    git = shutil.which("git")
    subprocess.run([git, "add", *names], cwd=repo, check=True)


def test_staged_is_quiet_when_nothing_is_claimed(repo, monkeypatch):
    _as(monkeypatch, "alice")
    _stage(repo, "src/a.ts")
    assert cli.main(["check", "--staged"]) == 0


def test_staged_refuses_a_commit_of_somebody_elses_claimed_work(repo, monkeypatch, capsys):
    """IF THIS FAILS: you commit work somebody else is still writing.

    Exit 1, not 2, and that difference is the whole reason this path is
    separate: git aborts on any non-zero, while Claude Code reads only 2 as
    "block". Using the hook's code here would work by accident; using git's
    makes the intent explicit.
    """
    _as(monkeypatch, "bob")
    cli.main(["claim", "src/a.ts", "--intent", "bob is on it"])
    capsys.readouterr()
    _as(monkeypatch, "alice")
    _stage(repo, "src/a.ts")
    assert cli.main(["check", "--staged"]) == 1
    err = capsys.readouterr().err
    assert "src/a.ts" in err and "bob" in err


def test_staged_lists_every_blocked_file_not_just_the_first(repo, monkeypatch, capsys):
    """A guard that reports one file at a time turns one fix into five commits."""
    _as(monkeypatch, "bob")
    cli.main(["claim", "src/a.ts", "src/b.ts", "--intent", "bob's pair"])
    capsys.readouterr()
    _as(monkeypatch, "alice")
    _stage(repo, "src/a.ts", "src/b.ts")
    assert cli.main(["check", "--staged"]) == 1
    err = capsys.readouterr().err
    assert "src/a.ts" in err and "src/b.ts" in err


def test_staged_lets_the_holder_commit_their_own_work(repo, monkeypatch):
    """The guard is about other people's ground, not your own."""
    _as(monkeypatch, "bob")
    cli.main(["claim", "src/a.ts", "--intent", "mine"])
    _stage(repo, "src/a.ts")
    assert cli.main(["check", "--staged"]) == 0
