"""Identity comes from the agent session, and the first claim is made for you.

Every test drives the CLI entry point with the JSON Claude Code sends its hooks.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comms_graph import cli as ccli
from comms_graph import log as clog
from comms_graph import state as cstate

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")

SESSION_A = "aaaaaa11-0000-4000-8000-000000000001"
SESSION_B = "bbbbbb22-0000-4000-8000-000000000002"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for args in (["init", "-q", "."], ["config", "user.email", "t@t.t"],
                 ["config", "user.name", "t"]):
        subprocess.run([GIT, *args], cwd=repo, check=True)
    (repo / "src" / "a.py").write_text("a = 1\n")
    (repo / "src" / "b.py").write_text("b = 1\n")
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
    subprocess.run([GIT, "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _run(repo: Path, args: list[str], *, stdin: str | None = None,
         env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    was = os.getcwd()
    saved_env = {k: os.environ.get(k) for k in (env or {})}
    saved_stdin = sys.stdin
    os.chdir(repo)
    try:
        os.environ.update(env or {})
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = ccli.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdin = saved_stdin
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.chdir(was)
    return code, out.getvalue(), err.getvalue()


def _hook(repo: Path, verb: str, session: str, **extra) -> tuple[int, str, str]:
    payload = {"session_id": session, "cwd": str(repo), "transcript_path": "/dev/null",
               "hook_event_name": verb}
    payload.update(extra)
    return _run(repo, ["hook", verb], stdin=json.dumps(payload))


def _edit(repo: Path, name: str, session: str, **extra) -> tuple[int, str, str]:
    return _hook(repo, "pre-edit", session, tool_name="Edit",
                 tool_input={"file_path": str(repo / name), "old_string": "1",
                             "new_string": "2"}, **extra)


def _as_session(session: str, **more: str) -> dict[str, str]:
    return {"CLAUDE_CODE_SESSION_ID": session, **more}


def _events(repo: Path) -> list:
    return clog.read(clog.log_path(repo))


def _claims(repo: Path) -> dict[str, object]:
    return {str(c.scope): c for c in cstate.fold(_events(repo)).claims.values()}


def _auto_name(session: str) -> str:
    return "claude-" + session[:6]


def _start_store(repo: Path) -> None:
    code, out, err = _run(repo, ["claim", "src/b.py", "--as", "other-agent",
                                 "--intent", "refactor b"], env=_as_session("other-sess"))
    assert code == 0, out + err


def _append(repo: Path, actor: str, etype: str, data: dict, *, ago: timedelta,
            scope: list[str] | None = None) -> str:
    event = clog.Event(ts=datetime.now(timezone.utc) - ago, id=clog.new_id(), actor=actor,
                       type=etype, scope=scope, data=data)
    log_file = clog.log_path(repo)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    clog.append(log_file, event)
    return event.id


# ---------------------------------------------------------------------------
# session-start
# ---------------------------------------------------------------------------


def test_session_start_in_a_repo_without_comms_is_silent(tmp_path):
    repo = _repo(tmp_path)
    code, out, err = _hook(repo, "session-start", SESSION_A, source="startup")
    assert (code, out, err) == (0, "", "")
    assert not clog.log_path(repo).exists(), "a briefing must not create a store"


def test_session_start_briefs_the_session_under_one_stable_name(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    me = "@" + _auto_name(SESSION_A)

    code, first, _ = _hook(repo, "session-start", SESSION_A, source="startup",
                           model="claude-opus-test")
    assert code == 0
    assert 0 < len(first) <= 900, len(first)
    assert me in first
    assert "src/b.py" in first and "@other-agent" in first, "files others hold are listed"

    for source in ("resume", "clear", "compact"):
        code, again, _ = _hook(repo, "session-start", SESSION_A, source=source)
        assert code == 0 and me in again, (source, again)

    hello = cstate.fold(_events(repo)).sessions[_auto_name(SESSION_A)]
    assert hello.agent_session == SESSION_A
    assert hello.model == "claude-opus-test"


def test_session_start_answers_to_a_name_the_session_already_used(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    _run(repo, ["claim", "src/a.py", "--as", "claude-auth-fix"], env=_as_session(SESSION_A))
    code, out, _ = _hook(repo, "session-start", SESSION_A, source="resume")
    assert code == 0 and "@claude-auth-fix" in out, out


def test_a_broken_payload_never_stops_a_session(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    for verb in ("session-start", "session-end"):
        assert _run(repo, ["hook", verb], stdin="not json")[0] == 0


# ---------------------------------------------------------------------------
# pre-edit: the auto-claim
# ---------------------------------------------------------------------------


def test_the_first_edit_of_a_free_file_claims_it_for_the_session(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)

    code, out, err = _edit(repo, "src/a.py", SESSION_A)
    assert code == 0, err
    assert (out, err) == ("", "")

    holder = _claims(repo)["src/a.py"]
    assert holder.actor == _auto_name(SESSION_A)
    assert holder.auto and holder.intent == "auto: edited"
    code, board, _ = _run(repo, ["board", "--all"])
    assert "src/a.py" in board and _auto_name(SESSION_A) in board, board

    before = clog.log_path(repo).read_bytes()
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    assert clog.log_path(repo).read_bytes() == before, "a second edit is a pure read"


def test_a_notebook_edit_is_claimed_by_its_notebook_path(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    (repo / "nb.ipynb").write_text("{}")
    code, _, err = _hook(repo, "pre-edit", SESSION_A, tool_name="NotebookEdit",
                         tool_input={"notebook_path": str(repo / "nb.ipynb")})
    assert code == 0, err
    assert "nb.ipynb" in _claims(repo)


def test_the_hook_never_creates_a_store(tmp_path):
    repo = _repo(tmp_path)
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    assert not clog.log_path(repo).parent.exists()


def test_the_old_check_spelling_auto_claims_too(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    payload = json.dumps({"session_id": SESSION_A, "tool_name": "Write",
                          "tool_input": {"file_path": str(repo / "src" / "a.py")}})
    assert _run(repo, ["check", "--stdin-json"], stdin=payload)[0] == 0
    assert _claims(repo)["src/a.py"].actor == _auto_name(SESSION_A)


def test_a_subagent_edit_records_which_subagent(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    code, _, err = _edit(repo, "src/a.py", SESSION_A, agent_id="agent-7f2", agent_type="Explore")
    assert code == 0, err
    claim = _claims(repo)["src/a.py"]
    assert claim.agent_id == "agent-7f2" and claim.agent_type == "Explore"
    assert claim.actor == _auto_name(SESSION_A)


# ---------------------------------------------------------------------------
# pre-edit: who is blocked
# ---------------------------------------------------------------------------


def test_another_session_is_blocked_by_the_auto_claim_and_told_who_holds_it(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    (repo / "src" / "a.py").write_text("a = 2\n")

    code, _, err = _edit(repo, "src/a.py", SESSION_B)
    assert code == 2, err
    assert "BLOCKED" in err and "@" + _auto_name(SESSION_A) in err


def test_a_session_is_never_blocked_by_a_claim_it_made_under_an_explicit_name(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    code, out, err = _run(repo, ["claim", "src/a.py", "--as", "claude-auth-fix",
                                 "--intent", "fix auth"], env=_as_session(SESSION_A))
    assert code == 0, out + err

    code, _, err = _edit(repo, "src/a.py", SESSION_A)
    assert code == 0, f"the session's own claim blocked it: {err}"
    assert _edit(repo, "src/a.py", SESSION_B)[0] == 2


def test_a_session_using_two_names_holds_both_names_files(tmp_path):
    repo = _repo(tmp_path)
    for name, path in (("claude-one", "src/a.py"), ("claude-two", "src/b.py")):
        code, out, err = _run(repo, ["claim", path, "--as", name], env=_as_session(SESSION_A))
        assert code == 0, out + err
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    assert _edit(repo, "src/b.py", SESSION_A)[0] == 0


def test_an_explicit_claim_takes_over_the_sessions_own_auto_claim(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    (repo / "src" / "a.py").write_text("a = 2\n")
    code, out, err = _run(repo, ["claim", "src/a.py", "--as", "claude-auth-fix",
                                 "--intent", "now on purpose"], env=_as_session(SESSION_A))
    assert code == 0, out + err
    assert _claims(repo)["src/a.py"].actor == "claude-auth-fix"


# ---------------------------------------------------------------------------
# session-end
# ---------------------------------------------------------------------------


def test_session_end_releases_auto_claims_and_keeps_explicit_ones(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    (repo / "src" / "c.py").write_text("c = 1\n")
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    code, out, err = _run(repo, ["claim", "src/c.py", "--intent", "on purpose"],
                          env=_as_session(SESSION_A))
    assert code == 0, out + err

    code, out, err = _hook(repo, "session-end", SESSION_A, reason="exit")
    assert (code, out, err) == (0, "", "")

    held = _claims(repo)
    assert "src/a.py" not in held
    assert held["src/c.py"].actor == _auto_name(SESSION_A)
    assert held["src/b.py"].actor == "other-agent", "another session's claim is untouched"
    ended = [e for e in _events(repo) if e.type == "release"]
    assert ended[-1].data["result"] == "session ended"


# ---------------------------------------------------------------------------
# auto-claims do not hoard
# ---------------------------------------------------------------------------


def test_an_auto_claim_on_a_clean_file_gives_way_and_says_so(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    # Session A touched the file but left it as committed.

    code, _, err = _edit(repo, "src/a.py", SESSION_B)
    assert code == 0, err
    released = [e for e in _events(repo) if e.type == "release"
                and e.data.get("result") == "auto-claim: file is clean in git"]
    assert len(released) == 1
    assert _claims(repo)["src/a.py"].actor == _auto_name(SESSION_B)


def test_a_cli_claim_also_gets_past_a_clean_auto_claim(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    code, out, err = _run(repo, ["claim", "src/a.py", "--as", "claude-reviewer"],
                          env=_as_session(SESSION_B))
    assert code == 0, out + err


def test_an_auto_claim_with_uncommitted_work_still_blocks(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    assert _edit(repo, "src/a.py", SESSION_A)[0] == 0
    (repo / "src" / "a.py").write_text("a = 3\n")
    assert _edit(repo, "src/a.py", SESSION_B)[0] == 2


# ---------------------------------------------------------------------------
# the way out of a block
# ---------------------------------------------------------------------------


def test_a_silent_holder_block_prints_a_steal_command_that_works(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    _append(repo, "gone-agent", "hello", {"agent_session": "gone-sess"}, ago=timedelta(hours=3))
    claim_id = _append(repo, "gone-agent", "claim", {"intent": "half done"},
                       ago=timedelta(hours=3), scope=["src/a.py"])

    code, _, err = _edit(repo, "src/a.py", SESSION_B)
    assert code == 2
    line = next(ln for ln in err.splitlines() if "--steal" in ln)
    argv = shlex.split(line)
    assert argv[:2] == ["comms-graph", "claim"] and claim_id in argv

    code, out, err = _run(tmp_path, argv[1:], env=_as_session(SESSION_B))
    assert code == 0, out + err
    assert _claims(repo)["src/a.py"].actor == _auto_name(SESSION_B)


def test_a_holder_with_a_fresh_liveness_marker_is_not_called_silent(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    _append(repo, "busy-agent", "hello", {"agent_session": "busy-sess"}, ago=timedelta(hours=3))
    _append(repo, "busy-agent", "claim", {"intent": "long edit"},
            ago=timedelta(hours=3), scope=["src/a.py"])
    before = clog.log_path(repo).read_bytes()
    assert _edit(repo, "src/a.py", "busy-sess")[0] == 0, "the holder edits its own file"
    assert clog.log_path(repo).read_bytes() == before, "liveness is not a log append"

    code, _, err = _edit(repo, "src/a.py", SESSION_B)
    assert code == 2
    assert "--steal" not in err and "silent" not in err
    assert "Holder:" in err and "note" in err

    code, status, _ = _run(repo, ["status"])
    assert "LIKELY DEAD" not in status and "STALE" not in status, status


# ---------------------------------------------------------------------------
# the CLI resolver
# ---------------------------------------------------------------------------


def test_an_actorless_cli_call_resolves_to_the_name_the_session_used(tmp_path):
    repo = _repo(tmp_path)
    code, out, err = _run(repo, ["claim", "src/a.py", "--as", "claude-auth-fix"],
                          env=_as_session(SESSION_A))
    assert code == 0, out + err
    code, out, err = _run(repo, ["claim", "src/b.py"], env=_as_session(SESSION_A))
    assert code == 0, out + err
    assert "by @claude-auth-fix" in out


def test_an_actorless_cli_call_in_a_new_session_gets_the_auto_name_and_a_hello(tmp_path):
    repo = _repo(tmp_path)
    code, out, err = _run(repo, ["note", "hello there"], env=_as_session(SESSION_A))
    assert code == 0, out + err
    st = cstate.fold(_events(repo))
    assert st.notes[-1].actor == _auto_name(SESSION_A)
    assert st.sessions[_auto_name(SESSION_A)].agent_session == SESSION_A


def test_codex_sessions_get_a_codex_auto_name(tmp_path):
    repo = _repo(tmp_path)
    code, out, err = _run(repo, ["claim", "src/a.py"],
                          env=_as_session(SESSION_A, COMMS_VENDOR="openai"))
    assert code == 0, out + err
    assert "by @codex-" + SESSION_A[:6] in out


def test_no_actor_and_no_session_is_one_line_of_error(tmp_path):
    repo = _repo(tmp_path)
    code, out, err = _run(repo, ["claim", "src/a.py"])
    assert code == 2
    assert len(err.strip().splitlines()) == 1 and "no actor" in err


@pytest.mark.parametrize("name", ["eli", "claude", "Codex", "agent", "user", "me"])
def test_generic_names_are_refused_on_write(tmp_path, name):
    repo = _repo(tmp_path)
    code, out, err = _run(repo, ["claim", "src/a.py", "--as", name])
    assert code == 2 and "generic" in err
    assert not clog.log_path(repo).exists()
    code, _, err = _run(repo, ["claim", "src/a.py"], env={"COMMS_ACTOR": name})
    assert code == 2 and "generic" in err


def test_a_generic_name_is_allowed_with_the_override(tmp_path):
    repo = _repo(tmp_path)
    code, out, err = _run(repo, ["claim", "src/a.py", "--as", "eli"],
                          env={"COMMS_ALLOW_GENERIC_ACTOR": "1"})
    assert code == 0, out + err


def test_a_generic_name_still_reads(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    assert _run(repo, ["status", "--as", "claude"])[0] == 0


# ---------------------------------------------------------------------------
# old logs
# ---------------------------------------------------------------------------


_OLD_LOG = [
    {"ts": "2026-10-01T09:00:00Z", "id": "01K6F0000000000000000000A1", "actor": "old-dev",
     "type": "hello", "data": {"agent_session": "old-s", "label": "Old Dev", "vendor": "anthropic",
                                "hostname": "h"}},
    {"ts": "2026-10-01T09:01:00Z", "id": "01K6F0000000000000000000A2", "actor": "old-dev",
     "type": "task", "data": {"task": "fix-auth", "title": "Fix the auth flow"}},
    {"ts": "2026-10-01T09:02:00Z", "id": "01K6F0000000000000000000A3", "actor": "old-dev",
     "type": "task", "data": {"task": "ship-auth", "title": "Ship it"}},
    {"ts": "2026-10-01T09:03:00Z", "id": "01K6F0000000000000000000A4", "actor": "old-dev",
     "type": "task_edge", "data": {"from": "fix-auth", "to": "ship-auth", "kind": "blocks"}},
    {"ts": "2026-10-01T09:04:00Z", "id": "01K6F0000000000000000000A5", "actor": "old-dev",
     "type": "claim", "scope": ["src/a.py"], "data": {"intent": "fixing", "task": "fix-auth"}},
    {"ts": "2026-10-01T09:05:00Z", "id": "01K6F0000000000000000000A6", "actor": "old-dev",
     "type": "task_state", "data": {"task": "fix-auth", "state": "doing"}},
    {"ts": "2026-10-01T09:06:00Z", "id": "01K6F0000000000000000000A7", "actor": "peer",
     "type": "blocked", "data": {"scope": "src/a.py", "holder": "old-dev",
                                  "holder_scope": "src/a.py"}},
    {"ts": "2026-10-01T09:07:00Z", "id": "01K6F0000000000000000000A8", "actor": "peer",
     "type": "note", "data": {"body": "@old-dev ping me when a.py is free"}},
    {"ts": "2026-10-01T09:08:00Z", "id": "01K6F0000000000000000000A9", "actor": "old-dev",
     "type": "finding", "data": {"category": "bug", "summary": "token never refreshed"}},
    {"ts": "2026-10-01T09:09:00Z", "id": "01K6F0000000000000000000AA", "actor": "old-dev",
     "type": "claim", "scope": ["src/b.py"], "data": {"intent": "next"}},
    {"ts": "2026-10-01T09:10:00Z", "id": "01K6F0000000000000000000AB", "actor": "old-dev",
     "type": "release", "data": {"refs": ["01K6F0000000000000000000AA"], "result": "done"}},
]


def test_a_log_with_every_old_event_type_still_folds_and_enforces(tmp_path):
    assert {e["type"] for e in _OLD_LOG} == set(clog.KNOWN_TYPES)
    repo = _repo(tmp_path)
    log_file = clog.log_path(repo)
    log_file.parent.mkdir(parents=True)
    log_file.write_text("".join(json.dumps(e) + "\n" for e in _OLD_LOG))

    st = cstate.fold(clog.read(log_file))
    assert {str(c.scope) for c in st.claims.values()} == {"src/a.py"}
    assert not st.claims["01K6F0000000000000000000A5"].auto
    assert st.families["old-s"].actors == {"old-dev"}
    assert set(st.tasks) == {"fix-auth", "ship-auth"}
    assert len(st.blocked) == 1 and len(st.notes) == 1 and len(st.findings) == 1

    assert _run(repo, ["status"])[0] == 0
    assert _edit(repo, "src/a.py", "old-s")[0] == 0, "the old hello still identifies its session"
    code, _, err = _edit(repo, "src/a.py", SESSION_B)
    assert code == 2 and "@old-dev" in err


def _whoami_actor(repo: Path, session: str) -> str:
    code, out, _ = _run(repo, ["note", "probe"], env={"CLAUDE_CODE_SESSION_ID": session})
    assert code == 0, out
    return out.split(" by @", 1)[1].split()[0]


def test_session_name_survives_a_subagent_speaking_last(tmp_path):
    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    assert _run(repo, ["hello", "--as", "lead"], env=env)[0] == 0
    assert _run(repo, ["claim", "src/b.py", "--intent", "x", "--as", "sub-1"], env=env)[0] == 0
    assert _whoami_actor(repo, SESSION_A) == "lead"


def test_implicit_hello_never_renames_the_session(tmp_path):
    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    assert _run(repo, ["note", "first", "--as", "lead"], env=env)[0] == 0
    assert _run(repo, ["note", "later", "--as", "sub-1"], env=env)[0] == 0
    assert _whoami_actor(repo, SESSION_A) == "lead"


def test_explicit_hello_adds_a_name_and_never_renames_the_session(tmp_path):
    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    assert _run(repo, ["note", "first", "--as", "lead"], env=env)[0] == 0
    assert _run(repo, ["hello", "--as", "captain"], env=env)[0] == 0
    assert _whoami_actor(repo, SESSION_A) == "lead"
    assert "captain" in cstate.fold(_events(repo)).family_of(SESSION_A)


def test_a_session_renamed_by_an_older_build_resolves_to_its_first_actor(tmp_path):
    repo = _repo(tmp_path)
    log_file = clog.log_path(repo)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    clog.append_batch(log_file, [
        clog.Event(ts=t0 + timedelta(minutes=i), id=clog.new_id(), actor=name, type="hello",
                   scope=None, data={"agent_session": SESSION_A, **({"primary": True} if i else {})})
        for i, name in enumerate(["first-one", "second-one", "third-one"])])
    st = cstate.fold(clog.read(log_file))
    assert st.families[SESSION_A].first_actor == "first-one"
    assert _whoami_actor(repo, SESSION_A) == "first-one"
