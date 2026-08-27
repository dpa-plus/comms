"""A message addressed to somebody has to reach them.

Blocking was pushed and notes were polled, which for something called comms is
the wrong way round. Reported with a receipt: an agent was blocked on a file, so
rather than wait it wrote the holder a note by name with a fix to take along.
The holder committed that exact file six minutes later having never seen it, and
found the note forty minutes after that. The fix is still half-landed.

The holder ran several commands in those six minutes.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-q", "."], cwd=repo, check=True)
    (repo / "OrderSidePanel.tsx").write_text("x\n")
    return repo


def _cli(repo, *args, session=None, actor=None):
    from comms_graph import cli as ccli

    out = io.StringIO()
    was = os.getcwd()
    saved = {k: os.environ.get(k) for k in ("CLAUDE_CODE_SESSION_ID", "COMMS_ACTOR")}
    os.chdir(repo)
    try:
        for k, v in (("CLAUDE_CODE_SESSION_ID", session), ("COMMS_ACTOR", actor)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with redirect_stdout(out), redirect_stderr(out):
            code = ccli.main(list(args))
    finally:
        os.chdir(was)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, out.getvalue()


def test_a_note_addressed_to_you_arrives_on_your_next_command(tmp_path, monkeypatch):
    """IF THIS FAILS: the reported incident repeats. Somebody hands you a fix
    for the file you are holding and you never see it, because the tool files
    the collaborative half and only interrupts on the coercive one."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "claim", "OrderSidePanel.tsx", "--as", "workdesk",
         "--intent", "header work", session="sW")
    _cli(repo, "note", "@workdesk: the header renders skeletons forever", "--as", "auftraege",
         session="sA")

    # ANY command. The holder ran several in the six minutes they had.
    code, out = _cli(repo, "board", "--as", "workdesk", session="sW")
    assert code == 0, out
    assert "message(s) for you" in out, out
    assert "skeletons forever" in out, out


def test_it_does_not_repeat_on_every_command_afterwards(tmp_path, monkeypatch):
    """IF THIS FAILS: the feature is noise by its second hour, and noise is what
    three sessions asked to have cut today."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "note", "@workdesk: take this along", "--as", "auftraege", session="sA")
    _, first = _cli(repo, "board", "--as", "workdesk", session="sW")
    assert "message(s) for you" in first
    _, second = _cli(repo, "board", "--as", "workdesk", session="sW")
    assert "message(s) for you" not in second, second


def test_it_reaches_only_the_person_named(tmp_path, monkeypatch):
    """IF THIS FAILS: everybody gets everything and it is unreadable by lunch."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "note", "@workdesk: only for you", "--as", "auftraege", session="sA")

    _, sender = _cli(repo, "board", "--as", "auftraege", session="sA")
    assert "message(s) for you" not in sender, "your own message is not a message to you"

    _, bystander = _cli(repo, "board", "--as", "karte", session="sK")
    assert "message(s) for you" not in bystander, bystander


def test_a_prefix_of_a_name_is_not_that_name(tmp_path, monkeypatch):
    """IF THIS FAILS: a note to @claude reaches @claude-karte and @claude-dev
    and @claude-workdesk, and a busy project delivers everything to everybody."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "note", "@claude: a general remark", "--as", "auftraege", session="sA")
    _, out = _cli(repo, "board", "--as", "claude-karte", session="sK")
    assert "message(s) for you" not in out, out


def test_a_role_suffix_still_receives(tmp_path, monkeypatch):
    """IF THIS FAILS: a note to @claude-dev misses claude-dev/review, which is
    the same agent wearing a hat."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "note", "@claude-dev: for you", "--as", "auftraege", session="sA")
    _, out = _cli(repo, "board", "--as", "claude-dev/review", session="sR")
    assert "message(s) for you" in out, out


def test_delivery_never_changes_the_exit_code(tmp_path, monkeypatch):
    """IF THIS FAILS: a message waiting for you can fail your command, which
    would make the feature worse than the problem."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "note", "@workdesk: hello", "--as", "auftraege", session="sA")
    code, out = _cli(repo, "board", "--as", "workdesk", session="sW")
    assert code == 0, out


def test_the_commit_guard_restates_a_recent_message(tmp_path, monkeypatch):
    """IF THIS FAILS: the one moment where taking somebody's fix along is still
    free passes in silence. This is the reporter's own suggestion, and the
    incident is exactly a commit that should have carried one."""
    repo = _repo(tmp_path, monkeypatch)
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
    subprocess.run([GIT, "-c", "user.email=t@t.t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    _cli(repo, "note", "@workdesk: take this along", "--as", "auftraege", session="sA")

    # delivered once by an ordinary command
    _cli(repo, "board", "--as", "workdesk", session="sW")

    (repo / "OrderSidePanel.tsx").write_text("y\n")
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
    code, out = _cli(repo, "check", "--staged", "--as", "workdesk", session="sW")
    assert code == 0, out
    assert "take this along" in out, out
    assert "free" in out, out
