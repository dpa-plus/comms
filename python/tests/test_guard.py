"""The commit guard: does it actually run, and does it stop the real thing.

`check --staged` was correct and caught nothing three times, because nothing
ran it. So the property under test here is not "does the check work" (that is
test_tree.py and test_task_graph.py) but "does git invoke it without anybody
remembering to". Every test drives a REAL `git commit` rather than calling the
check directly, because the failure mode being fixed lives entirely in the wiring.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

from comms_graph import guard as cguard

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")


def _repo(tmp_path, monkeypatch, name="repo"):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    repo = tmp_path / name
    repo.mkdir()
    for args in (["init", "-q", "."], ["config", "user.email", "t@t.t"],
                 ["config", "user.name", "t"]):
        subprocess.run([GIT] + args, cwd=repo, check=True)
    (repo / "de.json").write_text('{"a":1}\n')
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
    subprocess.run([GIT, "commit", "-qm", "init"], cwd=repo, check=True)
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


def _commit(repo, actor, message="c"):
    """A real commit, with the environment a real agent shell has."""
    env = dict(os.environ, COMMS_ACTOR=actor)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    done = subprocess.run([GIT, "commit", "-m", message], cwd=repo,
                          capture_output=True, env=env)
    return done.returncode, (done.stdout + done.stderr).decode("utf-8", "replace")


def _log(repo):
    out = subprocess.run([GIT, "log", "--oneline"], cwd=repo, capture_output=True)
    return out.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# the incident
# ---------------------------------------------------------------------------


def test_a_commit_over_a_live_claim_is_refused_without_anybody_running_anything(
    tmp_path, monkeypatch
):
    """IF THIS FAILS: the reported incident happens again. An agent committed a
    translation file while another agent held the claim on it, and seventeen of
    the holder's in-flight keys shipped inside somebody else's commit. The check
    would have refused it by name. Nobody typed the command."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "guard", "install")
    _cli(repo, "claim", "de.json", "--as", "holder",
         "--intent", "17 i18n keys in flight", session="sH")
    (repo / "de.json").write_text('{"a":1,"b":2}\n')
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)

    code, out = _commit(repo, "other", "map changes")
    assert code != 0, out
    assert "@holder" in out, out
    assert "17 i18n keys in flight" in out, out
    assert "map changes" not in _log(repo), "the commit went through anyway"


def test_the_holder_can_still_commit_their_own_work(tmp_path, monkeypatch):
    """IF THIS FAILS: the guard blocks the agent that did everything right,
    which is how a guard gets deleted."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "guard", "install")
    _cli(repo, "claim", "de.json", "--as", "holder", "--intent", "mine", session="sH")
    (repo / "de.json").write_text('{"a":2}\n')
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)

    code, out = _commit(repo, "holder", "the 17 keys")
    assert code == 0, out
    assert "the 17 keys" in _log(repo)


def test_an_ordinary_commit_in_a_repo_nobody_coordinates_in_still_works(
    tmp_path, monkeypatch
):
    """IF THIS FAILS: installing the guard has broken committing for anyone who
    is not using comms in that repo, which is most repos."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "guard", "install")
    (repo / "de.json").write_text('{"a":3}\n')
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)

    code, out = _commit(repo, "somebody", "ordinary")
    assert code == 0, out
    assert "ordinary" in _log(repo)


# ---------------------------------------------------------------------------
# wiring, where the silent failures live
# ---------------------------------------------------------------------------


def test_the_hook_goes_where_core_hookspath_points(tmp_path, monkeypatch):
    """IF THIS FAILS: on any repo that sets core.hooksPath the guard is written
    to .git/hooks, which git does not read, and reports success. An installed
    guard that never runs is worse than none, because it is believed."""
    repo = _repo(tmp_path, monkeypatch)
    (repo / ".githooks").mkdir()
    subprocess.run([GIT, "config", "core.hooksPath", ".githooks"], cwd=repo, check=True)

    code, out = _cli(repo, "guard", "install")
    assert code == 0, out
    assert (repo / ".githooks" / "pre-commit").exists(), out
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()

    # And it really fires from there.
    _cli(repo, "claim", "de.json", "--as", "holder", "--intent", "mine", session="sH")
    (repo / "de.json").write_text("x\n")
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
    rc, msg = _commit(repo, "other")
    assert rc != 0, msg


def test_the_hook_is_executable(tmp_path, monkeypatch):
    """IF THIS FAILS: git skips the hook in silence. There is no error, no
    warning, and every commit passes unchecked."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "guard", "install")
    assert os.access(repo / ".git" / "hooks" / "pre-commit", os.X_OK)


def test_somebody_elses_hook_is_not_quietly_overwritten(tmp_path, monkeypatch):
    """IF THIS FAILS: comms destroys a project's lint or build hook to install
    itself, which is a worse bug than the one it fixes."""
    repo = _repo(tmp_path, monkeypatch)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho theirs\n")
    hook.chmod(0o755)

    code, out = _cli(repo, "guard", "install")
    assert code != 0, out
    assert "--chain" in out, out
    assert hook.read_text() == "#!/bin/sh\necho theirs\n", "it was overwritten"


def test_chaining_keeps_the_other_hook_and_its_veto(tmp_path, monkeypatch):
    """IF THIS FAILS: --chain silently drops the hook it claimed to keep, or
    keeps it but ignores its refusal, which is the same as dropping it."""
    repo = _repo(tmp_path, monkeypatch)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho theirs-ran\nexit 0\n")
    hook.chmod(0o755)

    code, out = _cli(repo, "guard", "install", "--chain")
    assert code == 0, out
    assert (repo / ".git" / "hooks" / cguard.CHAINED_NAME).exists()

    (repo / "de.json").write_text("x\n")
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
    rc, msg = _commit(repo, "somebody", "with chain")
    assert rc == 0, msg
    assert "theirs-ran" in msg, msg

    # Now make the other hook refuse, and the commit must stop.
    prior = repo / ".git" / "hooks" / cguard.CHAINED_NAME
    prior.write_text("#!/bin/sh\necho theirs-says-no\nexit 1\n")
    prior.chmod(0o755)
    (repo / "de.json").write_text("y\n")
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
    rc, msg = _commit(repo, "somebody", "should not land")
    assert rc != 0, msg
    assert "should not land" not in _log(repo)


def test_uninstall_puts_the_displaced_hook_back(tmp_path, monkeypatch):
    """IF THIS FAILS: removing the guard silently loses the hook it moved aside
    when it was installed."""
    repo = _repo(tmp_path, monkeypatch)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho theirs\n")
    hook.chmod(0o755)
    _cli(repo, "guard", "install", "--chain")

    code, out = _cli(repo, "guard", "uninstall")
    assert code == 0, out
    assert hook.read_text() == "#!/bin/sh\necho theirs\n", out


def test_uninstall_leaves_a_hook_it_did_not_write_alone(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho theirs\n")
    hook.chmod(0o755)

    code, out = _cli(repo, "guard", "uninstall")
    assert code != 0, out
    assert hook.exists()


def test_reinstalling_is_not_an_error_and_does_not_stack(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "guard", "install")
    code, out = _cli(repo, "guard", "install")
    assert code == 0, out
    body = (repo / ".git" / "hooks" / "pre-commit").read_text()
    assert body.count(cguard.MARKER) == 1, body


# ---------------------------------------------------------------------------
# saying so
# ---------------------------------------------------------------------------


def test_status_answers_non_zero_when_the_guard_is_not_wired_in(tmp_path, monkeypatch):
    """So a setup script can branch on it without parsing prose."""
    repo = _repo(tmp_path, monkeypatch)
    code, out = _cli(repo, "guard", "status")
    assert code == 1, out
    assert "NOT installed" in out, out

    _cli(repo, "guard", "install")
    code, out = _cli(repo, "guard", "status")
    assert code == 0, out
    assert "installed" in out


def test_the_board_says_when_nothing_is_enforcing_the_check(tmp_path, monkeypatch):
    """IF THIS FAILS: an unenforced guard is invisible, and looks exactly like
    an enforced one until somebody commits over a live claim."""
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "note", "so the log exists", "--as", "alpha", session="sA")

    code, out = _cli(repo, "board", "--as", "alpha", session="sA")
    assert code == 0, out
    assert "commit guard: NOT installed" in out, out

    _cli(repo, "guard", "install")
    code, out = _cli(repo, "board", "--as", "alpha", session="sA")
    # Present and correct is not news; only its absence is worth a line.
    assert "commit guard" not in out, out


def test_a_repo_that_is_not_a_git_repo_says_so_rather_than_claiming_anything(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    st = cguard.status(plain)
    assert st.state == "unknown"
    assert st.reason
    assert not st.installed


def test_a_guard_that_cannot_run_refuses_the_commit_rather_than_waving_it_through(
    tmp_path, monkeypatch
):
    """IF THIS FAILS: the guard fails OPEN, which is the entire bug class this
    exists to close. Every incident behind it was something answering "nothing
    to worry about" when it had established nothing at all.

    The refusal has to name the way out, though: a hook nobody can remove
    without knowing git's layout would break committing for good.
    """
    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "guard", "install")
    hook = repo / ".git" / "hooks" / "pre-commit"

    # The binary moves or is uninstalled. Both lookups now fail.
    body = hook.read_text()
    start = body.index("GUARD='") + len("GUARD='")
    end = body.index("'", start)
    hook.write_text(body[:start] + "/nonexistent/comms-graph" + body[end:])

    (repo / "de.json").write_text("x\n")
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)

    env = dict(os.environ, COMMS_ACTOR="somebody", PATH="/nonexistent-bin")
    done = subprocess.run([GIT, "commit", "-m", "unchecked"], cwd=repo,
                          capture_output=True, env=env)
    msg = (done.stdout + done.stderr).decode("utf-8", "replace")
    assert done.returncode != 0, msg
    assert "NOTHING about this commit was" in msg, msg
    # The escape hatch, spelled out, or this is a repo nobody can commit to.
    assert str(hook) in msg, msg
    assert "unchecked" not in _log(repo)
