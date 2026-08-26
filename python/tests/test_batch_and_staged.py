"""Two things the briefing has always taught that this build could not do.

Both were found by auditing the skill file against the CLI rather than by a
failing test, which is the point: an instruction file is a promise to whoever
types it, and nothing in a test suite types it.

* `claim a b c --intent "..."`: a task boundary is usually several files, and
  taking them one at a time leaves the half-held state where you have two of
  three and somebody else has the third.
* `check --staged`: the guard that stops you committing somebody else's
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

    That is the state with no good move in it: you hold two files, somebody
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
    """One task, one agent: the rule the single-scope path enforces. A batch
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


def test_staged_prints_a_recovery_command_that_actually_runs(repo, monkeypatch, capsys):
    """IF THIS FAILS: the guard tells you that you are stuck without telling you
    how to get out, or worse, hands you a command that errors.

    Two details carry it. Before the first commit there is no HEAD to restore
    FROM, so `git restore --staged` fails and `git rm --cached` is the one that
    works, and being blocked on an initial commit is not rare. And the pathspec
    is `:(literal)`, so a filename containing a glob character names itself
    instead of matching other people's staged files.
    """
    _as(monkeypatch, "bob")
    odd = repo / "src" / "odd [name].ts"
    odd.write_text("x = 1\n")
    cli.main(["claim", "src/odd [name].ts", "--intent", "bob's"])
    capsys.readouterr()

    _as(monkeypatch, "alice")
    _stage(repo, "src/odd [name].ts", "src/b.ts")
    assert cli.main(["check", "--staged"]) == 1
    err = capsys.readouterr().err

    line = next(l.strip() for l in err.splitlines() if "git rm --cached" in l)
    assert ":(literal)" in line
    subprocess.run(line, cwd=repo, shell=True, check=True,
                   capture_output=True)

    git = shutil.which("git")
    staged = subprocess.run([git, "diff", "--cached", "--name-only"],
                            cwd=repo, capture_output=True, text=True).stdout.split()
    assert "src/b.ts" in staged, "the recovery command unstaged a file it was not given"
    assert not any("odd" in s for s in staged), "the blocked file is still staged"


def test_a_passing_staged_check_says_which_kind_of_pass_it_was(repo, monkeypatch, capsys):
    """IF THIS FAILS: exit 0 is ambiguous and gets misread as a false negative.

    There are three ways to pass and they mean different things: nothing staged,
    no coordination log in this repo, or paths checked and clear. Silence made
    them identical. An agent testing the guard against a peer-held path got exit
    0 because the peer had already committed (so `git add` produced no index
    entry), and nearly filed correct behaviour as a bug.
    """
    _as(monkeypatch, "alice")

    assert cli.main(["check", "--staged"]) == 0
    assert "nothing is staged" in capsys.readouterr().out

    _stage(repo, "src/a.ts")
    assert cli.main(["check", "--staged"]) == 0
    assert "no coordination log" in capsys.readouterr().out

    cli.main(["claim", "src/b.ts", "--intent", "mine"])
    capsys.readouterr()
    assert cli.main(["check", "--staged"]) == 0
    out = capsys.readouterr().out
    assert "checked" in out and "none held by anybody else" in out


def test_the_recovery_command_never_touches_the_working_tree(repo, monkeypatch, capsys):
    """IF THIS FAILS: the guard destroys the work it exists to protect.

    The blocked file is, by definition, one somebody else is in the middle of,
    so this line is handed to a person and run against a peer's live work.

    `git restore --staged` touches the index only. The neighbouring spellings do
    not: plain `git restore <path>` rewrites the WORKING TREE and does not
    unstage at all, and `git restore --source=HEAD` overwrites the file outright.
    One dropped word turns a recovery into either a no-op that looks like a fix
    or a silent loss of somebody's uncommitted edit, so the property is pinned by
    running the emitted command rather than by reading it.

    Verified by running the emitted command and asserting the content survives,
    not by reading it.
    """
    git = shutil.which("git")
    subprocess.run([git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([git, "commit", "-qm", "base"], cwd=repo, check=True)

    _as(monkeypatch, "alice")
    cli.main(["claim", "src/a.ts", "--intent", "alice is mid-edit"])
    capsys.readouterr()
    (repo / "src" / "a.ts").write_text("alice work in progress\n")
    _stage(repo, "src/a.ts")

    _as(monkeypatch, "bob")
    assert cli.main(["check", "--staged"]) == 1
    line = next(l.strip() for l in capsys.readouterr().err.splitlines()
                if "git restore" in l)
    subprocess.run(line, cwd=repo, shell=True, check=True, capture_output=True)

    assert (repo / "src" / "a.ts").read_text() == "alice work in progress\n", (
        "the recovery command discarded somebody's uncommitted work"
    )
    staged = subprocess.run([git, "diff", "--cached", "--name-only"],
                            cwd=repo, capture_output=True, text=True).stdout.split()
    assert not staged, "it did not actually unstage"


def test_staged_does_not_block_you_with_your_own_claims(repo, monkeypatch, capsys):
    """IF THIS FAILS: the guard punishes the agent that did the right thing.

    A pre-commit hook runs with no COMMS_ACTOR and cannot know which agent is
    committing, so `--as` is not available to it. Without resolving identity the
    caller is a stranger to its own claims: an agent that claimed its files,
    wrote them and staged them was told "3 staged file(s) are claimed by somebody
    else" and handed a command to unstage its own work.

    The incentive that creates is exactly backwards: claim nothing and commit
    freely, claim properly and you cannot commit at all, so this is worse than
    no guard.
    """
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-alice")
    _as(monkeypatch, "alice")
    cli.main(["hello", "--label", "Alice"])
    cli.main(["claim", "src/a.ts", "--intent", "alice's own"])
    capsys.readouterr()
    _stage(repo, "src/a.ts")

    monkeypatch.delenv("COMMS_ACTOR")          # what a git hook actually sees
    assert cli.main(["check", "--staged"]) == 0, "blocked by its own claim"
    assert "none held by anybody else" in capsys.readouterr().out


def test_staged_still_blocks_a_peer_when_identity_comes_from_the_session(repo, monkeypatch, capsys):
    """The fix must not turn the guard off: resolving identity has to keep
    somebody else's claim blocking."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-alice")
    _as(monkeypatch, "alice")
    cli.main(["hello", "--label", "Alice"])
    cli.main(["claim", "src/a.ts", "--intent", "alice's own"])
    capsys.readouterr()

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-bob")
    _as(monkeypatch, "bob")
    cli.main(["hello", "--label", "Bob"])
    capsys.readouterr()
    _stage(repo, "src/a.ts")
    monkeypatch.delenv("COMMS_ACTOR")
    assert cli.main(["check", "--staged"]) == 1
    assert "alice" in capsys.readouterr().err


def test_staged_says_so_when_it_cannot_tell_who_is_committing(repo, monkeypatch, capsys):
    """IF THIS FAILS: a guess is stated as a fact, and the guess is wrong for the
    agent that claimed properly.

    With no actor and no session there is no way to know whose claims those are.
    "Claimed by somebody else" would be an assertion we cannot support, so this
    reports the identity problem and uses 2: this command's code for "could not
    find out": rather than 1, which means conflict.
    """
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-alice")
    _as(monkeypatch, "alice")
    cli.main(["hello", "--label", "Alice"])
    cli.main(["claim", "src/a.ts", "--intent", "alice's own"])
    capsys.readouterr()
    _stage(repo, "src/a.ts")

    monkeypatch.delenv("COMMS_ACTOR")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert cli.main(["check", "--staged"]) == 2
    err = capsys.readouterr().err
    assert "cannot establish who is committing" in err
    # It may SAY it cannot tell yours from somebody else's. What it must not do
    # is assert the conflict as fact, which is the BLOCKED wording.
    assert "are claimed by somebody else" not in err, (
        "asserted a conflict it could not establish"
    )
    assert "BLOCKED" not in err
