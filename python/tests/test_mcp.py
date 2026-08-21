"""The MCP door: same log, same refusals, and it must not fall over.

Two things make this surface different from the CLI and both are tested here.
It is a LONG-LIVED process, so anything that would exit a command — a malformed
frame, a bad argument, a conflict — has to become an answer instead, or one bad
call ends every agent's session at once. And it is the surface an agent reaches
for on EVERY turn, so `comms_check` has to stay a pure read: the moment it takes
a lock or creates a directory, the cost lands in front of every edit in the repo.

The rest is parity. An agent using the tools and an agent using the CLI are
coordinating through one file; if the events drift apart, both of them are told
the ground is free.

Each test says what breaks in the real world if it fails.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess

import pytest

from comms_graph import log as clog
from comms_graph import mcp as cmcp
from comms_graph import state as cstate


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _repo(tmp_path, monkeypatch):
    """A git checkout with its own HOME, so the store is this test's alone."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.delenv("COMMS_ACTOR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    (repo / "b.py").write_text("y = 2\n")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=repo, check=True)
    return repo


def _drive(repo, *frames, session=None):
    """Run JSON-RPC frames through the server and return the decoded replies.

    `session` is the host agent's session id, which real agents each have their
    own of. Left unset a test inherits pytest's, which makes every actor in it
    look like ONE agent to anything that reads hello events.
    """
    out = io.StringIO()
    was = os.getcwd()
    prior = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.chdir(repo)
    try:
        if session is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = session
        else:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        cmcp.serve(io.StringIO("\n".join(frames) + "\n"), out)
    finally:
        os.chdir(was)
        if prior is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _frame(rpc_id, tool, **args):
    return json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}})


def _call(repo, tool, *, session=None, **args):
    """One tool call. Returns (text, is_error)."""
    replies = _drive(repo, _frame(1, tool, **args), session=session)
    assert len(replies) == 1, replies
    result = replies[0].get("result")
    assert result is not None, replies[0]
    return result["content"][0]["text"], result["isError"]


def _events(repo):
    return clog.read(clog.log_path(repo))


def _state(repo):
    return cstate.fold(_events(repo))


# ---------------------------------------------------------------------------
# The protocol itself
# ---------------------------------------------------------------------------


def test_the_handshake_advertises_all_six_tools_with_schemas(tmp_path, monkeypatch):
    """IF THIS FAILS: the tools are invisible or unusable. A client discovers
    what it can call exactly once, from tools/list; a tool with no inputSchema
    leaves the model guessing its arguments, and it will guess wrong."""
    repo = _repo(tmp_path, monkeypatch)
    replies = _drive(
        repo,
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
    )
    assert len(replies) == 2, replies
    init = replies[0]["result"]
    assert init["protocolVersion"] == cmcp.PROTOCOL_VERSION
    assert init["serverInfo"]["name"] == "comms"
    assert "tools" in init["capabilities"]

    tools = replies[1]["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "comms_check", "comms_claim", "comms_release",
        "comms_status", "comms_note", "comms_find",
    }
    for tool in tools:
        assert tool["description"].strip(), tool["name"]
        assert isinstance(tool["inputSchema"], dict), tool["name"]
        assert tool["inputSchema"]["type"] == "object", tool["name"]


def test_tool_descriptions_stay_cheap(tmp_path, monkeypatch):
    """IF THIS FAILS: every turn of every session pays for the extra prose. The
    tool list is resident context — it is re-sent to the model on every single
    request — so a paragraph added here is a paragraph taken off the task, a
    thousand times over. The Go build's six sit around 1100 characters."""
    repo = _repo(tmp_path, monkeypatch)
    replies = _drive(repo, '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    total = sum(len(t["description"]) for t in replies[0]["result"]["tools"])
    assert total < 1400, f"descriptions total {total} characters"


def test_a_notification_is_never_answered(tmp_path, monkeypatch):
    """IF THIS FAILS: the connection dies at startup. Every client sends
    `notifications/initialized` right after the handshake, and a reply to a
    frame that carries no id is a protocol violation clients drop the
    connection over."""
    repo = _repo(tmp_path, monkeypatch)
    replies = _drive(
        repo,
        '{"jsonrpc":"2.0","method":"notifications/initialized"}',
        '{"jsonrpc":"2.0","id":7,"method":"ping"}',
    )
    assert len(replies) == 1, replies
    assert replies[0]["id"] == 7


def test_an_id_of_zero_is_still_answered(tmp_path, monkeypatch):
    """IF THIS FAILS: a client that numbers its requests from zero hangs on its
    first call forever. Absent and falsy are not the same test, and id 0 is a
    perfectly legal JSON-RPC id."""
    repo = _repo(tmp_path, monkeypatch)
    replies = _drive(repo, '{"jsonrpc":"2.0","id":0,"method":"ping"}')
    assert len(replies) == 1 and replies[0]["id"] == 0, replies


def test_a_garbage_frame_does_not_end_the_session(tmp_path, monkeypatch):
    """IF THIS FAILS: one truncated write from the client kills a server that
    several agents may be sharing. An unparseable line has no id to answer on;
    the only safe thing is to skip it and read the next one."""
    repo = _repo(tmp_path, monkeypatch)
    replies = _drive(
        repo,
        '{"jsonrpc":"2.0"',
        "not json at all",
        "[1, 2, 3]",
        '{"jsonrpc":"2.0","id":9,"method":"ping"}',
    )
    assert len(replies) == 1 and replies[0]["id"] == 9, replies


def test_an_unknown_method_is_a_protocol_error_not_a_crash(tmp_path, monkeypatch):
    """IF THIS FAILS: a client probing for an optional capability (resources,
    prompts) takes the server down with it."""
    repo = _repo(tmp_path, monkeypatch)
    replies = _drive(
        repo,
        '{"jsonrpc":"2.0","id":3,"method":"resources/list"}',
        '{"jsonrpc":"2.0","id":4,"method":"ping"}',
    )
    assert replies[0]["error"]["code"] == -32601
    assert replies[1]["id"] == 4, "the loop must survive it"


def test_an_unknown_tool_is_refused_without_ending_the_loop(tmp_path, monkeypatch):
    """IF THIS FAILS: a hallucinated tool name ends the connection instead of
    being corrected, and the agent loses every claim it was about to release."""
    repo = _repo(tmp_path, monkeypatch)
    replies = _drive(
        repo,
        _frame(1, "comms_teleport", actor="claude-dev"),
        _frame(2, "comms_status", actor="claude-dev"),
    )
    assert replies[0]["error"]["code"] == -32602
    assert replies[1]["result"]["isError"] is False


# ---------------------------------------------------------------------------
# comms_check — the pure read
# ---------------------------------------------------------------------------


def test_check_neither_locks_nor_creates_anything(tmp_path, monkeypatch):
    """IF THIS FAILS: the cost of coordination lands in front of every edit in
    the repo. check runs on every turn; taking the lock queues it behind whoever
    is mid-claim, and creating the store means an unwritable data home turns one
    read into a failure in a repo that has never coordinated at all."""
    repo = _repo(tmp_path, monkeypatch)
    store = clog.store_dir(repo)
    assert not store.exists(), "precondition: nothing has coordinated here yet"

    text, is_error = _call(repo, "comms_check", actor="claude-dev", path="a.py")
    assert is_error is False
    assert "clear" in text

    assert not store.exists(), "check created the store it was only meant to read"


def test_check_answers_while_somebody_else_holds_the_lock(tmp_path, monkeypatch):
    """IF THIS FAILS: every edit an agent makes waits behind whoever is mid-claim.
    check runs on every turn and reads a log it cannot corrupt by reading, so
    queueing it behind the write lock buys nothing and costs a pause in front of
    all the work in the repo — up to the full lock timeout when a holder stalls."""
    import time

    from comms_graph import lock as clock

    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_claim", actor="claude-dev", path="a.py", intent="parser",
          session="agent-A")
    lock_file = clog.store_dir(repo) / ".lock"

    handle = clock.acquire(lock_file)
    try:
        started = time.monotonic()
        text, is_error = _call(repo, "comms_check", actor="codex-dev", path="a.py")
        elapsed = time.monotonic() - started
    finally:
        handle.release()
    assert "BLOCKED" in text and is_error is True
    assert elapsed < 2.0, f"check waited {elapsed:.1f}s for a lock it must not take"


def test_check_reports_the_holder_and_ignores_your_own_claim(tmp_path, monkeypatch):
    """IF THIS FAILS: check is worthless in both directions. Missing somebody
    else's claim waves through the collision it exists to stop; reporting your
    OWN claim blocks you from editing the file you just took, which is the
    failure that makes agents switch the tool off."""
    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_claim", actor="claude-dev", path="a.py",
          intent="rewrite the parser", session="agent-A")

    text, is_error = _call(repo, "comms_check", actor="codex-dev", path="a.py")
    assert is_error is True
    assert "BLOCKED" in text and "@claude-dev" in text
    assert "rewrite the parser" in text, "the intent is what tells you whether to wait"

    text, is_error = _call(repo, "comms_check", actor="claude-dev", path="a.py")
    assert is_error is False, text
    assert "clear" in text


def test_check_refuses_an_unreadable_path_as_text_not_as_a_protocol_error(
        tmp_path, monkeypatch):
    """IF THIS FAILS: the model cannot see what it got wrong. A protocol error
    is invisible to it — only tool text with isError reaches the model, and a
    malformed scope is exactly the mistake it can fix by itself."""
    repo = _repo(tmp_path, monkeypatch)
    text, is_error = _call(repo, "comms_check", actor="claude-dev", path="a.py#L40-10")
    assert is_error is True
    assert text, "a refusal with no reason is not a refusal"

    text, is_error = _call(repo, "comms_check", actor="claude-dev", path="")
    assert is_error is True and "path is required" in text


# ---------------------------------------------------------------------------
# Claiming, refusing, releasing
# ---------------------------------------------------------------------------


def test_a_claim_through_the_tools_is_a_claim_to_the_cli(tmp_path, monkeypatch):
    """IF THIS FAILS: the two doors open onto different rooms. An agent on the
    MCP tools and one on the CLI are coordinating through one log; if the tool
    writes an event the fold does not recognise as a claim, both are told the
    ground is free and both edit it."""
    repo = _repo(tmp_path, monkeypatch)
    text, is_error = _call(repo, "comms_claim", actor="claude-dev", path="a.py",
                           intent="rewrite the parser", session="agent-A")
    assert is_error is False, text
    assert "claimed a.py as @claude-dev" in text

    st = _state(repo)
    held = st.active_claims_by_actor("claude-dev")
    assert [str(c.scope) for c in held] == ["a.py"]
    assert held[0].intent == "rewrite the parser"


def test_a_second_agent_is_refused_and_the_refusal_is_recorded(tmp_path, monkeypatch):
    """IF THIS FAILS: comms cannot prove it ever did anything. A prevented
    collision that leaves no trace is why a log of thousands of claims can
    honestly report having prevented nothing — and a refusal that still records
    the claim is worse: two agents both believe they hold the file."""
    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_claim", actor="claude-dev", path="a.py", intent="parser",
          session="agent-A")

    text, is_error = _call(repo, "comms_claim", actor="codex-dev", path="a.py",
                           intent="add types", session="agent-B")
    assert is_error is True
    assert "REFUSED" in text and "@claude-dev" in text
    assert "Nothing was claimed" in text

    st = _state(repo)
    assert st.active_claims_by_actor("codex-dev") == [], "the refusal recorded a claim"
    assert len(st.blocked) == 1, "the prevented collision left no evidence"
    assert st.blocked[0].actor == "codex-dev"
    assert st.blocked[0].holder == "claude-dev"
    assert st.blocked[0].scope == "a.py"


def test_a_narrower_scope_inside_a_held_file_is_still_refused(tmp_path, monkeypatch):
    """IF THIS FAILS: claiming one symbol of a file somebody holds whole reads as
    free ground, and the two agents edit the same file at once — the overlap
    rules are the entire product."""
    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_claim", actor="claude-dev", path="a.py", intent="parser",
          session="agent-A")
    text, is_error = _call(repo, "comms_claim", actor="codex-dev", path="a.py#parse",
                           intent="types", session="agent-B")
    assert is_error is True and "REFUSED" in text


def test_release_frees_everything_you_hold_and_says_so(tmp_path, monkeypatch):
    """IF THIS FAILS: ground stays held after the work is done. Every later agent
    is blocked by a claim nobody is behind, which is how a coordination tool
    becomes the thing everybody routes around."""
    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_claim", actor="claude-dev", path="a.py", intent="parser",
          session="agent-A")
    _call(repo, "comms_claim", actor="claude-dev", path="b.py", intent="types",
          session="agent-A")

    text, is_error = _call(repo, "comms_release", actor="claude-dev",
                           result="merged as #321", session="agent-A")
    assert is_error is False, text
    assert "released 2 claim(s)" in text and "a.py" in text and "b.py" in text

    st = _state(repo)
    assert st.active_claims_by_actor("claude-dev") == []
    assert st.releases[-1].result == "merged as #321"
    assert sorted(st.releases[-1].scopes) == ["a.py", "b.py"]

    text, is_error = _call(repo, "comms_check", actor="codex-dev", path="a.py")
    assert is_error is False and "clear" in text


def test_releasing_nothing_is_an_answer_not_an_error(tmp_path, monkeypatch):
    """IF THIS FAILS: an agent that tidies up unconditionally gets told off for
    doing the right thing, and learns to stop calling release at all."""
    repo = _repo(tmp_path, monkeypatch)
    text, is_error = _call(repo, "comms_release", actor="claude-dev", result="nothing to do")
    assert is_error is False
    assert "no claims" in text


def test_missing_required_arguments_come_back_as_readable_refusals(tmp_path, monkeypatch):
    """IF THIS FAILS: the model gets a protocol error it cannot see, or the
    server dies on an argument it should simply have asked for again."""
    repo = _repo(tmp_path, monkeypatch)
    for tool, args, wanted in (
        ("comms_claim", {"path": "a.py"}, "intent"),
        ("comms_claim", {"intent": "x"}, "path"),
        ("comms_release", {}, "result"),
        ("comms_note", {}, "body"),
        ("comms_find", {"summary": "x"}, "category"),
    ):
        text, is_error = _call(repo, tool, actor="claude-dev", **args)
        assert is_error is True, (tool, text)
        assert wanted in text, (tool, text)
    assert _events(repo) == [], "a refused call wrote to the log anyway"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_the_actor_argument_beats_the_environment(tmp_path, monkeypatch):
    """IF THIS FAILS: one server acting for several agents files all their work
    under one name — and two agents sharing a name cannot detect a conflict
    between them, which is the exact failure comms exists to prevent. COMMS_ACTOR
    is per PROCESS; the argument is the only thing that can be per agent."""
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("COMMS_ACTOR", "claude-dev")

    _call(repo, "comms_claim", actor="codex-dev", path="a.py", intent="types",
          session="agent-B")
    st = _state(repo)
    assert [c.actor for c in st.claims.values()] == ["codex-dev"]

    # ...and with no argument, the environment is still the default.
    _call(repo, "comms_claim", path="b.py", intent="parser", session="agent-A")
    st = _state(repo)
    assert {c.actor for c in st.claims.values()} == {"codex-dev", "claude-dev"}


def test_a_mutation_with_no_identity_at_all_is_refused(tmp_path, monkeypatch):
    """IF THIS FAILS: work lands in the log under a name nobody owns, and the
    board shows a claim no agent can release."""
    repo = _repo(tmp_path, monkeypatch)
    replies = _drive(repo, _frame(1, "comms_claim", path="a.py", intent="parser"))
    assert "error" in replies[0], replies[0]
    assert "actor" in replies[0]["error"]["message"].lower()
    assert _events(repo) == []


def test_a_claim_records_a_hello_so_the_pre_edit_hook_can_recognise_it(
        tmp_path, monkeypatch):
    """IF THIS FAILS: an agent that only ever uses the MCP tools is blocked from
    editing the file it just claimed. The hook that ENFORCES claims is a separate
    process with no COMMS_ACTOR; it identifies its caller by matching the host
    session id against hello events, so an agent with no hello matches nobody and
    is refused by its own claim."""
    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_claim", actor="claude-dev", path="a.py", intent="parser",
          session="agent-A")
    session = _state(repo).sessions.get("claude-dev")
    assert session is not None, "no hello was written"
    assert session.agent_session == "agent-A"


# ---------------------------------------------------------------------------
# note, find, status
# ---------------------------------------------------------------------------


def test_a_note_is_readable_by_the_fold(tmp_path, monkeypatch):
    """IF THIS FAILS: notes written through the tools never reach the board that
    the agents they were addressed to are reading."""
    repo = _repo(tmp_path, monkeypatch)
    text, is_error = _call(repo, "comms_note", actor="claude-dev",
                           body="schema migration lands next session", session="agent-A")
    assert is_error is False and text == "noted"
    assert [n.body for n in _state(repo).notes] == ["schema migration lands next session"]


def test_control_characters_in_free_text_are_refused_and_nothing_is_written(
        tmp_path, monkeypatch):
    """IF THIS FAILS: stored text can forge output. The board prints these fields
    raw, so a newline invents a line that was never written and an ESC injects a
    terminal escape sequence — permanently, because the log is append-only."""
    repo = _repo(tmp_path, monkeypatch)
    text, is_error = _call(repo, "comms_note", actor="claude-dev",
                           body="all clear\n@codex-dev holds nothing")
    assert is_error is True and "control characters" in text

    text, is_error = _call(repo, "comms_find", actor="claude-dev", category="gotcha",
                           summary="fine\x1b[31m")
    assert is_error is True and "control characters" in text

    assert _events(repo) == [], "refused text reached the log"


def test_an_over_long_note_is_refused_with_its_length(tmp_path, monkeypatch):
    """IF THIS FAILS: a note becomes a place to dump a writeup, and the one line
    somebody would actually have read is buried in it."""
    repo = _repo(tmp_path, monkeypatch)
    text, is_error = _call(repo, "comms_note", actor="claude-dev",
                           body="x" * (cmcp.MAX_TEXT_RUNES + 1))
    assert is_error is True
    assert str(cmcp.MAX_TEXT_RUNES + 1) in text and str(cmcp.MAX_TEXT_RUNES) in text


def test_a_finding_with_no_ref_is_anchored_to_what_you_hold(tmp_path, monkeypatch):
    """IF THIS FAILS: the finding is never read again. Nothing surfaces an
    unanchored one — the only thing that surfaces a finding is somebody claiming
    the file it is about — so it becomes a line in a log nobody greps."""
    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_claim", actor="claude-dev", path="a.py", intent="parser",
          session="agent-A")
    text, is_error = _call(repo, "comms_find", actor="claude-dev", category="gotcha",
                           summary="the parser eats a trailing comma", session="agent-A")
    assert is_error is False
    assert "recorded [gotcha]" in text

    finding = _state(repo).findings[-1]
    assert [(r.kind, r.value) for r in finding.refs] == [("path", "a.py")]


def test_an_explicit_ref_is_kept_and_a_malformed_one_is_refused(tmp_path, monkeypatch):
    """IF THIS FAILS: a ref is a bare string that cannot say whether it is a
    path, a PR or a commit — and that is the useful half of it."""
    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_find", actor="claude-dev", category="decision",
          summary="uuid PKs, never cuid", ref="path:src/db.py", session="agent-A")
    finding = _state(repo).findings[-1]
    assert [(r.kind, r.value) for r in finding.refs] == [("path", "src/db.py")]

    text, is_error = _call(repo, "comms_find", actor="claude-dev", category="fix",
                           summary="fixed it", ref="justastring")
    assert is_error is True and "kind:value" in text

    text, is_error = _call(repo, "comms_find", actor="claude-dev", category="nonsense",
                           summary="fixed it")
    assert is_error is True and "bug, fix, ship, decision, gotcha" in text


def test_claiming_a_file_resurfaces_what_was_learned_about_it(tmp_path, monkeypatch):
    """IF THIS FAILS: the durable half of the log is write-only. A gotcha is
    worth recording only because it comes back at the one moment it matters —
    when the next agent is about to touch that file."""
    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_find", actor="claude-dev", category="gotcha",
          summary="a.py is generated; edit the template", ref="path:a.py",
          session="agent-A")

    text, is_error = _call(repo, "comms_claim", actor="codex-dev", path="a.py",
                           intent="add types", session="agent-B")
    assert is_error is False, text
    assert "prior context on this path" in text
    assert "[gotcha] a.py is generated" in text

    # A finding about a different file must not follow you around.
    text, _ = _call(repo, "comms_claim", actor="codex-dev", path="b.py",
                    intent="tidy", session="agent-B")
    assert "prior context" not in text


def test_status_counts_claims_agents_and_prevented_collisions(tmp_path, monkeypatch):
    """IF THIS FAILS: the only number that says whether comms is earning its
    keep goes missing, and an agent asking who is working cannot find out."""
    repo = _repo(tmp_path, monkeypatch)
    _call(repo, "comms_claim", actor="claude-dev", path="a.py", intent="parser",
          session="agent-A")
    _call(repo, "comms_claim", actor="codex-dev", path="a.py", intent="types",
          session="agent-B")

    text, is_error = _call(repo, "comms_status", actor="claude-dev")
    assert is_error is False
    assert "1 active claim(s)" in text
    assert "a.py — @claude-dev (parser)" in text
    assert "collisions prevented: 1" in text


def test_status_on_a_repo_that_has_never_coordinated_creates_nothing(
        tmp_path, monkeypatch):
    """IF THIS FAILS: merely asking who is working writes to disk, and a
    read-only or unwritable data home turns a question into a failure."""
    repo = _repo(tmp_path, monkeypatch)
    text, is_error = _call(repo, "comms_status", actor="claude-dev")
    assert is_error is False
    assert "0 active claim(s)" in text
    assert not clog.store_dir(repo).exists()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_refuses_arguments_and_explains_itself(tmp_path, monkeypatch, capsys):
    """IF THIS FAILS: `comms mcp --port 7878` blocks on stdin forever with no
    output, looking exactly like a hung server."""
    repo = _repo(tmp_path, monkeypatch)
    assert cmcp.main(["--port", "7878"]) != 0
    assert "no arguments" in capsys.readouterr().err

    assert cmcp.main(["--help"]) == 0
    assert "stdin/stdout" in capsys.readouterr().out
    assert repo.exists()
