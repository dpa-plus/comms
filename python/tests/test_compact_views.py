"""status and board answer for the caller, briefly; the briefing's commands are true."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from comms_graph import cli, hooks

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")

SESSION = "cccccc33-0000-4000-8000-000000000003"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for args in (["init", "-q", "."], ["config", "user.email", "t@t.t"], ["config", "user.name", "t"]):
        subprocess.run([GIT, *args], cwd=path, check=True)
    (path / "a.txt").write_text("a\n")
    subprocess.run([GIT, "add", "-A"], cwd=path, check=True)
    subprocess.run([GIT, "commit", "-qm", "init"], cwd=path, check=True)
    return path


def _run(cwd: Path, argv: list[str], env: dict[str, str] | None = None,
         stdin: str | None = None) -> tuple[int, str]:
    import sys

    out, err = io.StringIO(), io.StringIO()
    was, saved_stdin = os.getcwd(), sys.stdin
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.chdir(cwd)
    try:
        os.environ.update(env or {})
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdin = saved_stdin
        os.chdir(was)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, out.getvalue() + err.getvalue()


@pytest.fixture
def busy(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    assert _run(repo, ["claim", "a.txt", "--intent", "mine", "--as", "me-a"])[0] == 0
    for i in range(60):
        _run(repo, ["claim", f"src/m{i}/f{i}.py", "--intent", f"work {i}", "--as", f"peer-{i % 7}"])
    return repo


def test_status_stays_small_on_a_store_with_60_claims(busy):
    code, text = _run(busy, ["status", "--as", "me-a"])
    assert code == 0 and len(text) <= 1500, f"{len(text)} chars:\n{text}"
    assert "a.txt" in text and "others hold 60 files" in text and "--all" in text


def test_board_stays_small_on_a_store_with_60_claims(busy):
    code, text = _run(busy, ["board", "--as", "me-a"])
    assert code == 0 and len(text) <= 1500, f"{len(text)} chars:\n{text}"
    assert "others hold 60 files" in text and "--all" in text


def test_all_still_prints_every_claim_and_json_is_unchanged(busy):
    _, board = _run(busy, ["board", "--all", "--as", "me-a"])
    assert "active claims (61)" in board
    _, status = _run(busy, ["status", "--all", "--as", "me-a"])
    assert "ACTIVE CLAIMS" in status and "ACTIVE SESSIONS" in status
    code, raw = _run(busy, ["status", "--json", "--as", "me-a"])
    assert code == 0 and len(json.loads(raw)["claims"]) == 61


def test_the_compact_view_names_clashes_notes_and_stale_holders(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    _run(repo, ["claim", "a.txt", "--intent", "mine", "--as", "me-a"])
    _run(repo, ["claim", "b.txt", "--intent", "theirs", "--as", "peer-1"])
    _run(repo, ["note", "@me-a look at b.txt", "--as", "peer-1"])
    code, text = _run(repo, ["status", "--as", "me-a", "--stale-after", "1ms"])
    assert code == 0
    assert "notes to you:" in text and "@peer-1: @me-a look at b.txt" in text
    assert "STALE @peer-1 b.txt" in text


def test_status_without_an_actor_says_so_instead_of_failing(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    code, text = _run(repo, ["status"])
    assert code == 0 and "not identified" in text


def test_whoami_and_claims_for_the_session_name(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    env = {"CLAUDE_CODE_SESSION_ID": SESSION}
    _run(repo, ["claim", "a.txt", "--intent", "mine"], env)
    code, who = _run(repo, ["whoami"], env)
    assert code == 0 and "@claude-cccccc" in who and "this session's own name" in who
    code, mine = _run(repo, ["mine"], env)
    assert code == 0 and "a.txt" in mine


def test_bare_release_gives_back_the_callers_claims_and_leaves_a_subagents(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "b.txt").write_text("b\n")
    env = {"CLAUDE_CODE_SESSION_ID": SESSION}
    _run(repo, ["claim", "a.txt", "--intent", "lead"], env)
    _run(repo, ["claim", "b.txt", "--intent", "subagent", "--as", "sub-1"], env)
    code, text = _run(repo, ["release", "--result", "done"], env)
    assert code == 0 and "RELEASED a.txt" in text and "b.txt" not in text
    code, text = _run(repo, ["release", "--as", "sub-1"], env)
    assert code == 0 and "RELEASED b.txt" in text


def test_a_subagents_bare_release_leaves_the_leads_claims(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "b.txt").write_text("b\n")
    env = {"CLAUDE_CODE_SESSION_ID": SESSION}
    _run(repo, ["claim", "a.txt", "--intent", "lead"], env)
    _run(repo, ["claim", "b.txt", "--intent", "subagent", "--as", "sub-1"], env)
    code, text = _run(repo, ["release", "--as", "sub-1", "--result", "done"], env)
    assert code == 0 and "RELEASED b.txt" in text and "a.txt" not in text
    assert "a.txt" in _run(repo, ["claims"], env)[1]


def test_bare_release_never_touches_another_sessions_claims(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    _run(repo, ["claim", "a.txt", "--intent", "theirs"], {"CLAUDE_CODE_SESSION_ID": "other-session"})
    code, text = _run(repo, ["release", "--result", "x"], {"CLAUDE_CODE_SESSION_ID": SESSION})
    assert code == 0 and "nothing to release" in text
    assert "a.txt" in _run(repo, ["board", "--all"], {"CLAUDE_CODE_SESSION_ID": SESSION})[1]


def test_the_briefings_release_line_is_a_command_that_works(tmp_path):
    line = next(ln for ln in hooks.COMMAND_LINES if "release" in ln)
    assert 'comms-graph release --result "..."' in line
    repo = _git_repo(tmp_path / "repo")
    _run(repo, ["claim", "a.txt", "--intent", "x", "--as", "me-a"])
    assert _run(repo, ["release", "--result", "..."], {"COMMS_ACTOR": "me-a"})[0] == 0


def _hook(cwd: Path, verb: str, **extra) -> tuple[int, str]:
    payload = {"session_id": SESSION, "cwd": str(cwd), "hook_event_name": verb, **extra}
    return _run(cwd, ["hook", verb], stdin=json.dumps(payload))


def test_a_session_started_above_its_repositories_gets_a_folder_briefing(tmp_path):
    folder = tmp_path / "work"
    busy_repo = _git_repo(folder / "busy")
    _git_repo(folder / "idle")
    _run(busy_repo, ["claim", "a.txt", "--intent", "x", "--as", "peer-1"])
    code, text = _hook(folder, "session-start")
    assert code == 0 and len(text) <= hooks.BRIEFING_CAP
    assert "busy  1 file held by others" in text
    assert "idle" not in text, "a repository with no events is not listed"
    assert 'comms-graph claim <path> --intent "..."' in text
    assert 'comms-graph release --result "..."' in text
    assert 'comms-graph note "@who ..."' in text
    from comms_graph import log as clog
    from comms_graph import state as cstate

    st = cstate.fold(clog.read(clog.log_path(busy_repo)))
    assert SESSION not in st.families, "the folder briefing must not write an identity"


def test_the_folder_briefing_is_silent_without_recent_events(tmp_path):
    folder = tmp_path / "work"
    _git_repo(folder / "idle")
    assert _hook(folder, "session-start") == (0, "")
    stale = _git_repo(folder / "old")
    _run(stale, ["claim", "a.txt", "--intent", "x", "--as", "peer-1"])
    from comms_graph import log as clog

    log_file = clog.log_path(stale)
    long_ago = log_file.stat().st_mtime - 15 * 86400
    os.utime(log_file, (long_ago, long_ago))
    assert _hook(folder, "session-start") == (0, "")


def test_the_folder_briefing_stays_under_the_cap_with_many_repositories(tmp_path):
    folder = tmp_path / "work"
    for i in range(12):
        repo = _git_repo(folder / f"repository-with-a-long-name-{i}")
        _run(repo, ["claim", "a.txt", "--intent", "x", "--as", "peer-1"])
    code, text = _hook(folder, "session-start")
    assert code == 0 and 0 < len(text) <= hooks.BRIEFING_CAP
