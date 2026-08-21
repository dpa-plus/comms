"""hello / version / help — the identity record, and the two verbs that answer.

What is actually at stake here is `hello`. It is the record that ties an actor
name to a host agent session, and three separate mechanisms read it: the
pre-edit hook resolves "who is asking" through it, the review gate decides
self-review through it, and the independence label is read off its vendor. If
the record is missing, spelled differently from the one the rest of the CLI
writes, or silently skipped on a re-run, all three fail QUIETLY — an agent gets
blocked by its own claim, or a self-review passes as independent.

Each test says what breaks in the real world if it fails.
"""

from __future__ import annotations

import io
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

from comms_graph import cli as ccli
from comms_graph import hello as chello
from comms_graph import log as clog
from comms_graph import state as cstate


def _run(repo, *args, session=None, env=None):
    """Run the real verb in `repo`. Returns (exit_code, stdout, stderr).

    Kept apart rather than merged the way the other suites merge them, because
    two of the properties here are about WHICH stream a line lands on: the actor
    name has to be the first line of stdout (the store may print a warning to
    stderr first), and a refusal has to reach stderr so a wrapper can show it.

    `session` is the host agent's session id, the thing hello exists to record.
    A test that leaves it unset is testing the no-host-agent case, which is a
    real one: a human at a terminal has no CLAUDE_CODE_SESSION_ID.
    """
    out, err = io.StringIO(), io.StringIO()
    was = os.getcwd()
    saved = {k: os.environ.get(k) for k in
             ("CLAUDE_CODE_SESSION_ID", "COMMS_ACTOR", "COMMS_LABEL",
              "COMMS_VENDOR", "COMMS_MODEL")}
    os.chdir(repo)
    try:
        for key in saved:
            os.environ.pop(key, None)
        if session is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = session
        os.environ.update(env or {})
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = chello.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(was)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, out.getvalue(), err.getvalue()


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_text("x = 1\n")
    git = __import__("shutil").which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=repo, check=True)
    return repo


def _state_of(repo):
    return cstate.fold(clog.read(clog.log_path(repo)))


def _events_of(repo):
    path = clog.log_path(repo)
    return clog.read(path) if path.exists() else []


# ---------------------------------------------------------------------------
# hello — the identity record the hook reads
# ---------------------------------------------------------------------------


def test_hello_lets_the_pre_edit_hook_recognise_the_actor(tmp_path, monkeypatch):
    """IF THIS FAILS: the hook cannot tell this agent from a stranger. It has no
    COMMS_ACTOR of its own and resolves the caller by matching the host session
    id against hello events; with no usable record it falls back to a sentinel
    that matches nobody, and the agent is blocked from editing the file it just
    claimed itself."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "hello", "--as", "claude-dev", session="sess-1")
    assert code == 0, out + err
    st = _state_of(repo)
    assert ccli._hook_actor(st, "sess-1", "") == "claude-dev"


def test_an_explicit_hello_is_written_exactly_like_the_automatic_one(tmp_path, monkeypatch):
    """IF THIS FAILS: there are two spellings of one identity record. The
    automatic hello (cli._hello_if_unknown) and this verb both feed the same
    readers, so a key that drifts here — session id under a different name, a
    vendor that stops being recorded — makes half the agents on a machine
    invisible to the hook and to the independence label, with no error anywhere."""
    repo = _repo(tmp_path, monkeypatch)
    code, _out, err = _run(repo, "hello", "--as", "claude-dev", session="sess-1",
                   env={"COMMS_VENDOR": "anthropic", "COMMS_LABEL": "Claude Dev"})
    assert code == 0
    written = [e for e in _events_of(repo) if e.type == clog.TYPE_HELLO]
    assert len(written) == 1

    # The automatic path, minted against an empty state so it never declines.
    saved = {k: os.environ.get(k) for k in
             ("CLAUDE_CODE_SESSION_ID", "COMMS_VENDOR", "COMMS_LABEL")}
    os.environ.update({"CLAUDE_CODE_SESSION_ID": "sess-1", "COMMS_VENDOR": "anthropic",
                       "COMMS_LABEL": "Claude Dev"})
    try:
        automatic = ccli._hello_if_unknown(cstate.fold([]), "claude-dev")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert automatic is not None
    assert written[0].data == automatic.data


def test_saying_hello_again_updates_the_label(tmp_path, monkeypatch):
    """IF THIS FAILS: `hello --label` is a no-op for anyone who has already been
    seen, which is everyone by the time they want to fix their display name. The
    automatic path deliberately writes nothing once the pairing is known, so if
    this verb copied that condition the board would keep showing the old name
    while the command reported success."""
    repo = _repo(tmp_path, monkeypatch)
    _run(repo, "hello", "--as", "claude-dev", "--label", "Old", session="sess-1")
    code, out, err = _run(repo, "hello", "--as", "claude-dev", "--label", "New", session="sess-1")
    assert code == 0, out + err
    assert _state_of(repo).sessions["claude-dev"].label == "New"


def test_hello_records_the_vendor_so_independence_is_not_guessed(tmp_path, monkeypatch):
    """IF THIS FAILS: "verified" stops distinguishing an independent check from a
    second opinion by the same model. independence_of reads vendor off each
    actor's hello; a vendor that is dropped, or stored under a different case,
    reads as a different vendor and manufactures independence that does not exist."""
    repo = _repo(tmp_path, monkeypatch)
    _run(repo, "hello", "--as", "claude-dev", session="s1", env={"COMMS_VENDOR": "Anthropic"})
    _run(repo, "hello", "--as", "codex-dev", session="s2", env={"COMMS_VENDOR": "openai"})
    st = _state_of(repo)
    assert st.sessions["claude-dev"].vendor == "anthropic", "case must not split one vendor"
    assert st.independence_of("codex-dev", "claude-dev") == "independent"

    repo2 = _repo(tmp_path / "two", monkeypatch)
    _run(repo2, "hello", "--as", "claude-dev", session="s1")
    _run(repo2, "hello", "--as", "codex-dev", session="s2")
    assert _state_of(repo2).independence_of("codex-dev", "claude-dev") == "unknown", \
        "an unset vendor must stay absent, not be inferred from the name"


def test_a_label_that_could_forge_an_output_line_is_refused(tmp_path, monkeypatch):
    """IF THIS FAILS: an actor can write its own lines into the board. The label
    is printed raw next to the actor name, so a newline or an ESC in it lets one
    agent render text that looks like the tool speaking — including a claim that
    somebody holds nothing. Nothing may be recorded when the value is refused."""
    repo = _repo(tmp_path, monkeypatch)
    for bad in ("Real\n@someone else holds nothing", "esc\x1b[2Jgone", "bell\x07",
                "c1\x9bhidden"):
        code, out, err = _run(repo, "hello", "--as", "claude-dev", "--label", bad, session="s1")
        assert code == 2, f"{bad!r} was accepted: {out}{err}"
        assert "control character" in err
    assert _events_of(repo) == [], "a refused label must not leave a record behind"


def test_a_label_long_enough_to_flood_the_board_is_refused(tmp_path, monkeypatch):
    """IF THIS FAILS: one actor's display name can push every other agent's row
    off the screen — the board is the one place a human checks who holds what."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "hello", "--as", "claude-dev", "--label", "x" * 200, session="s1")
    assert code == 2 and "200 characters" in err
    assert _events_of(repo) == []


def test_hello_without_a_host_session_still_records_and_says_what_is_missing(
        tmp_path, monkeypatch):
    """IF THIS FAILS: a human at a terminal either gets no identity record at all,
    or gets one silently. Both are bad in different ways — the second is worse,
    because the hook's inability to recognise this actor then shows up much later
    as an unexplained block on a file the agent had claimed."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "hello", "--as", "human-eli")
    assert code == 0, out + err
    assert "CLAUDE_CODE_SESSION_ID" in out, "the missing piece must be named"
    st = _state_of(repo)
    assert "human-eli" in st.sessions
    assert st.sessions["human-eli"].agent_session == ""


def test_a_positional_name_overrides_the_environment(tmp_path, monkeypatch):
    """IF THIS FAILS: `comms hello alice` registers whoever COMMS_ACTOR happens to
    be, so the one command whose job is checking your actor name is right
    confirms a name you did not type."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "hello", "alice", session="s1", env={"COMMS_ACTOR": "stale-name"})
    assert code == 0, out + err
    assert out.startswith("@alice registered."), out
    assert set(_state_of(repo).sessions) == {"alice"}


def test_hello_with_no_actor_anywhere_refuses_rather_than_guessing(tmp_path, monkeypatch):
    """IF THIS FAILS: an unnamed agent gets a default identity, and two agents
    sharing one name cannot detect a conflict between them — the exact failure
    the actor requirement exists to prevent."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "hello", session="s1")
    assert code == 2 and "no actor" in err
    assert _events_of(repo) == []


def test_hello_names_the_actor_on_the_first_line(tmp_path, monkeypatch):
    """IF THIS FAILS: the one fact hello exists to surface — which actor you are
    about to work as — scrolls away behind the rest of the output, and a
    misconfigured name is found after the claims are already recorded under it."""
    repo = _repo(tmp_path, monkeypatch)
    _, out, _err = _run(repo, "hello", "--as", "claude-dev", "--label", "Claude Dev", session="s1")
    assert out.splitlines()[0] == "@claude-dev registered."
    assert "Claude Dev" in out
    assert str(clog.log_path(repo)) in out, "a reader must be able to find the log"


def test_hello_counts_the_sessions_that_share_a_family_name(tmp_path, monkeypatch):
    """IF THIS FAILS: the "you are the 2nd claude here" signal is wrong, and the
    misconfiguration it catches — two agents accidentally running as one family
    with one name — stays invisible until they collide."""
    repo = _repo(tmp_path, monkeypatch)
    _, out, _err = _run(repo, "hello", "--as", "claude-a", session="s1")
    assert "(1 claude session active right now.)" in out
    _, out, _err = _run(repo, "hello", "--as", "claude-b", session="s2")
    assert "(2 claude sessions active right now.)" in out
    _, out, _err = _run(repo, "hello", "--as", "codex-a", session="s3")
    assert "(1 codex session active right now.)" in out


def test_hello_asked_for_help_records_nothing(tmp_path, monkeypatch):
    """IF THIS FAILS: asking how a verb works has a side effect. For a tool whose
    users are agents, --help is the discovery path, and a discovery path that
    writes to the shared log is one agents learn not to use."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "hello", "--help", "--as", "claude-dev", session="s1")
    assert code == 0
    assert "Usage: comms-graph" in out
    assert _events_of(repo) == []


# ---------------------------------------------------------------------------
# version and help
# ---------------------------------------------------------------------------


def test_version_prints_the_version_as_the_second_field(tmp_path, monkeypatch):
    """IF THIS FAILS: every script that cuts the version out of `comms version` —
    the conventional way to check what is installed before filing a bug — reads
    the wrong field or nothing at all."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "version")
    assert code == 0
    fields = out.split()
    assert fields[0] == "comms-graph"
    assert fields[1] and fields[1][0].isdigit() or fields[1] == "dev"
    assert len(out.strip().splitlines()) == 1, "one line, so it stays greppable"


def test_version_from_a_source_checkout_says_dev_rather_than_failing(monkeypatch):
    """IF THIS FAILS: running from a checkout — how everyone working ON comms runs
    it — raises out of a command whose entire job is to answer a question."""
    from importlib.metadata import PackageNotFoundError

    def _missing(_name):
        raise PackageNotFoundError(_name)

    monkeypatch.setattr("importlib.metadata.version", _missing)
    assert chello._version() == "dev"


def test_version_refuses_arguments_instead_of_ignoring_them(tmp_path, monkeypatch):
    """IF THIS FAILS: a typo like `comms version --as me` exits 0, so a wrapper
    script reads a successful run where the user meant something else entirely."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "version", "extra")
    assert code == 2 and "takes no arguments" in err


def test_help_prints_the_same_usage_the_bare_command_does(tmp_path, monkeypatch):
    """IF THIS FAILS: there are two usage texts and one of them is stale. The
    usage text is how an agent discovers what it may type, so a second copy that
    drifts teaches agents commands that do not exist."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "help")
    assert code == 0
    assert out.strip() == ccli.USAGE.strip()


def test_an_unknown_verb_is_a_usage_error_not_a_silent_success(tmp_path, monkeypatch):
    """IF THIS FAILS: a mistyped command exits 0 having done nothing, and a
    wrapper that trusts the exit code reports coordinated work that never
    happened."""
    repo = _repo(tmp_path, monkeypatch)
    code, out, err = _run(repo, "helo")
    assert code == 2
    assert "unknown comms command" in err
