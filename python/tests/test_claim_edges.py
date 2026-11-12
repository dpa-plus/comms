"""Claims at the edges: files not created yet, takeovers, help and store errors.

Each test says what breaks in the real world if it fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _cli(repo, *args, session=None):
    """Run the real command in `repo`. Returns (exit_code, stdout+stderr).

    `session` is the host agent's session id. Real agents each have their own;
    a test that leaves it unset inherits the session of whatever process is
    running pytest, which makes every actor in the test look like ONE agent.
    """
    import io
    import os
    from contextlib import redirect_stderr, redirect_stdout

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
            try:
                code = ccli.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(was)
        if prior is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior
    return code, out.getvalue()


def _repo(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    git = __import__("shutil").which("git")
    if git is None:
        import pytest
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=repo, check=True)
    return repo


def test_a_leading_slash_does_not_empty_the_base_name():
    """A leading slash is not a role suffix: '/alice' and '/bob' must not both
    reduce to '' and become one agent."""
    from comms_graph.identity import base_actor

    assert base_actor("/alice") == "/alice"
    assert base_actor("/alice") != base_actor("/bob")
    assert base_actor("claude-dev/review") == "claude-dev"
    assert base_actor("a/b/c") == "a"


def test_two_agents_cannot_both_claim_one_not_yet_created_file(tmp_path, monkeypatch):
    """IF THIS FAILS: both agents are cleared to write one file, during exactly
    the window they are both writing it.

    Everything below the deepest existing directory is kept as typed, because a
    file with no spelling on disk has nothing to defer to, so on a case-blind
    filesystem `src/NewFeature.py` and `src/newfeature.py` were two scopes for
    one file. Claiming the file you are about to create is the primary use of
    this tool, so this was the primary case going wrong.
    """
    from comms_graph.cli import _case_blind_here

    repo = _repo(tmp_path, monkeypatch)
    (repo / "src").mkdir(exist_ok=True)
    if not _case_blind_here(repo / "src"):
        pytest.skip("case-sensitive filesystem: these are genuinely two files")

    code, out = _cli(repo, "claim", "src/NewFeature.py", "--as", "alice",
                     "--intent", "writing it", session="agent-A")
    assert code == 0, out
    code, out = _cli(repo, "claim", "src/newfeature.py", "--as", "bob",
                     "--intent", "also writing it", session="agent-B")
    assert code == 1, f"a second spelling of one file must be refused: {out}"

    # an unrelated new file is still free
    code, out = _cli(repo, "claim", "src/Unrelated.py", "--as", "carol",
                     "--intent", "different file", session="agent-C")
    assert code == 0, out


def test_a_case_variant_of_an_uncreated_claim_is_blocked_by_the_hook(tmp_path, monkeypatch):
    """The hook has to agree with claim, or an agent refused a claim can simply
    edit anyway."""
    import json as _json

    from comms_graph.cli import _case_blind_here

    repo = _repo(tmp_path, monkeypatch)
    (repo / "src").mkdir(exist_ok=True)
    if not _case_blind_here(repo / "src"):
        pytest.skip("case-sensitive filesystem")

    _cli(repo, "claim", "src/NewFeature.py", "--as", "alice", "--intent", "x",
         session="agent-A")

    import io as _io
    import os as _os
    import sys as _sys
    from contextlib import redirect_stderr as _re, redirect_stdout as _ro

    from comms_graph import cli as _ccli

    def hook(session, path):
        payload = _json.dumps({"session_id": session, "tool_name": "Edit",
                               "tool_input": {"file_path": str(path)}})
        buf, was, old = _io.StringIO(), _os.getcwd(), _sys.stdin
        _os.chdir(repo)
        _os.environ["CLAUDE_CODE_SESSION_ID"] = session
        try:
            _sys.stdin = _io.StringIO(payload)
            with _ro(buf), _re(buf):
                try:
                    return _ccli.main(["check", "--stdin-json"])
                except SystemExit as exc:
                    return exc.code if isinstance(exc.code, int) else 1
        finally:
            _sys.stdin = old
            _os.chdir(was)
            _os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    assert hook("agent-B", repo / "src" / "newfeature.py") == 2, "the twin must block"
    assert hook("agent-A", repo / "src" / "NewFeature.py") == 0, "the holder may write"
    assert hook("agent-B", repo / "src" / "Another.py") == 0, "unrelated is free"


@pytest.mark.parametrize("dirname", ["2026", "20260820", "0005", "1.2.3"])
def test_a_directory_with_no_cased_letters_does_not_disable_the_twin_check(
        tmp_path, monkeypatch, dirname):
    """IF THIS FAILS: the protection silently switches off for ordinary
    directory names.

    The probe swaps the case of a directory's own name and compares inodes, and
    the first version gave up when that name had no cased letters. Date folders,
    ticket numbers and version directories all hit it, and every one turned the
    whole thing off on a filesystem that IS case-blind. It walks up to an
    ancestor it can ask about now: case-sensitivity is a property of the
    volume, so any ancestor on it answers the same question.
    """
    from comms_graph.cli import _case_blind_here

    probe = tmp_path / dirname / "src"
    probe.mkdir(parents=True)
    # only meaningful where the filesystem really is case-blind
    if not _case_blind_here(tmp_path):
        pytest.skip("case-sensitive filesystem")
    assert _case_blind_here(probe) is True


def test_an_unusable_store_is_an_error_not_a_traceback(tmp_path, monkeypatch):
    """IF THIS FAILS: a wrapper reads an unreachable store as a live conflict.

    Every writing command crashed with a raw FileExistsError and exited 1: the
    code the usage text reserves for "somebody else holds this", so a script
    that retries on conflict would loop forever against a broken store.
    """
    import shutil

    from comms_graph import log as _l

    repo = _repo(tmp_path, monkeypatch)
    _cli(repo, "board", session="agent-A")  # create the store
    store = Path(_l.log_path(repo)).parent
    shutil.move(str(store), str(store) + ".moved")
    Path(store).write_text("in the way\n")

    code, out = _cli(repo, "board", session="agent-A")
    assert code == 2, f"an unusable store is not a conflict: exit {code}\n{out}"
    assert "cannot use the coordination store" in out
    assert "Traceback" not in out


def test_help_on_a_leaf_verb_explains_it_instead_of_running_it(tmp_path, monkeypatch):
    """IF THIS FAILS: asking how a verb works either errors or DOES the thing.

    Every leaf verb ignored --help: `board --help` read the store. For a
    tool whose users are agents, --help is the whole discovery path, and it is
    the one call that must never have a side effect.
    """
    from comms_graph import log as clog2

    repo = _repo(tmp_path, monkeypatch)
    for args in (("claim", "--help"), ("release", "--help"), ("board", "--help"),
                 ("note", "--help")):
        code, out = _cli(repo, *args)
        assert code == 0, (args, out)
        assert out.startswith("Usage: comms-graph"), (args, out[:90])
    assert not clog2.log_path(repo).exists(), "--help wrote to the log"


def test_help_does_not_hijack_free_text_that_mentions_it(tmp_path, monkeypatch):
    """--intent is prose an agent writes. A claim whose intent talks about the
    --help flag must still be a claim."""
    repo = _repo(tmp_path, monkeypatch)
    code, out = _cli(
        repo, "claim", "a.py", "--as", "w", "--intent", "document the --help flag"
    )
    assert code == 0, out
    assert "CLAIMED" in out


def test_reclaiming_ground_you_hold_replaces_it_rather_than_stacking(tmp_path, monkeypatch):
    """IF THIS FAILS: the board's claim count is wrong and one agent reads as two.

    Conflict detection only fires for a DIFFERENT actor, so re-claiming your own
    ground appended a second claim. The board showed two rows for one file, it
    was ambiguous which intent was current, and, because a claim carries a task
    tag: one agent on one file registered as a doer on two tasks, which the
    phase derivation reads as two people still working.
    """
    repo = _repo(tmp_path, monkeypatch)
    (repo / "a.py").write_text("x = 1\n")
    _cli(repo, "claim", "a.py", "--as", "bob", "--intent", "first", session="sB")
    _cli(repo, "claim", "a.py", "--as", "bob", "--intent", "second", session="sB")

    _, out = _cli(repo, "board", "--all", "--as", "bob", session="sB")
    assert "active claims (1)" in out, out
    assert "second" in out and "first" not in out, out

    # a symbol inside the file is different ground; holding both is legitimate
    _cli(repo, "claim", "a.py#parse", "--as", "bob", "--intent", "narrower", session="sB")
    _, out = _cli(repo, "board", "--all", "--as", "bob", session="sB")
    assert "active claims (2)" in out, out

    # and somebody else is still refused
    code, out = _cli(repo, "claim", "a.py", "--as", "carol", "--intent", "x", session="sC")
    assert code == 1, out
    assert "CLAIM CONFLICT" in out, out


def test_an_abandoned_claim_can_be_taken_over(tmp_path, monkeypatch):
    """IF THIS FAILS: clearing up after a crashed agent means impersonating it.

    `release` only ever touched claims held by `--as`, so freeing somebody
    else's ground meant stealing it and handing it back, or passing their name
   , which the CLI accepts and which writes a lie into an append-only log.

    """
    from comms_graph import state as cstate
    from comms_graph import cli as ccli
    from comms_graph import log as clog2

    repo = _repo(tmp_path, monkeypatch)
    for f in ("h.py", "i.py"):
        (repo / f).write_text("x = 1\n")
    _cli(repo, "claim", "h.py", "--as", "ken", "--intent", "k", session="sK")

    _, board = _cli(repo, "board", "--all", "--as", "x", session="sX")
    kid = board.split("[")[1].split("]")[0]

    code, out = _cli(repo, "release", kid, "--as", "nate", "--force", session="sN")
    assert code != 0 and "--reason" in out, out
    code, out = _cli(repo, "release", "h.py", "--as", "nate", "--force",
                     "--reason", "gone", session="sN")
    assert code != 0, ("a path was accepted; it can match more than was meant:\n" + out)

    code, out = _cli(repo, "release", kid, "--as", "nate", "--force",
                     "--reason", "ken's session died 3h ago", session="sN")
    assert code == 0, out
    assert "was @ken" in out and "@nate" in out, out

    # the log says who took it off whom, not that ken let go himself
    log_file, _ = ccli._store(ccli._repo_root(str(repo)), create=False)
    rel = cstate.fold(clog2.read(log_file)).releases[-1]
    assert rel.actor == "nate" and rel.original_actor == "ken", rel

    code, out = _cli(repo, "claim", "h.py", "--as", "nate", "--intent", "finishing it",
                     session="sN")
    assert code == 0, ("the freed file could not be picked up:\n" + out)


def test_a_person_can_free_ground_an_agent_abandoned(tmp_path, monkeypatch):
    """IF THIS FAILS: clearing up after a crashed agent means impersonating it.

    `release` only ever touched claims held by `--as`, so the two ways to free
    somebody else's ground were to steal it and hand it back: two writes, and
    you briefly own work you did not do, or to pass their name, which the CLI
    accepts and which writes a lie into an append-only log. A person tidying up
    after a crash should not have to pretend to be the crash.
    """
    from comms_graph import state as cstate

    repo = _repo(tmp_path, monkeypatch)
    (repo / "a.py").write_text("x = 1\n")
    _cli(repo, "claim", "a.py", "--as", "ghost", "--intent", "then crashed", session="sG")
    _, board = _cli(repo, "board", "--all", "--as", "x", session="sX")
    cid = board.split("[")[1].split("]")[0]

    code, out = _cli(repo, "release", cid, "--as", "eli-desk", "--force", session="sE")
    assert code != 0 and "--reason" in out, out
    code, out = _cli(repo, "release", "a.py", "--as", "eli-desk", "--force",
                     "--reason", "gone", session="sE")
    assert code != 0, ("a path was accepted; it can match more than was meant:\n" + out)

    code, out = _cli(repo, "release", cid, "--as", "eli-desk", "--force",
                     "--reason", "session died 4h ago", session="sE")
    assert code == 0, out
    assert "was @ghost" in out and "@eli" in out, out

    _, board = _cli(repo, "board", "--all", "--as", "x", session="sX")
    assert "no active claims" in board, board

    # and the log says who took it off whom, rather than reading as ghost's own
    from comms_graph import cli as ccli
    from comms_graph import log as clog2
    log_file, _ = ccli._store(ccli._repo_root(str(repo)), create=False)
    st = cstate.fold(clog2.read(log_file))
    rel = st.releases[-1]
    assert rel.actor == "eli-desk", rel
    assert rel.original_actor == "ghost", ("an arbitrated handover folded as an ordinary "
                                           "release: " + repr(rel))
    assert rel.arbitrator == "eli-desk", rel


def test_releasing_your_own_claim_does_not_need_force(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "a.py").write_text("x = 1\n")
    _cli(repo, "claim", "a.py", "--as", "alice", "--intent", "w", session="sA")
    _, board = _cli(repo, "board", "--all", "--as", "x", session="sX")
    cid = board.split("[")[1].split("]")[0]
    code, out = _cli(repo, "release", cid, "--as", "alice", "--force",
                     "--reason", "x", session="sA")
    assert code != 0 and "your own claim" in out, out
    code, out = _cli(repo, "release", "a.py", "--as", "alice", session="sA")
    assert code == 0, out


