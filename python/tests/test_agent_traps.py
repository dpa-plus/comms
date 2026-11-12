"""The call shapes agents actually got wrong, one row each.

Taken from failed calls in real transcripts. A row passes when the call does
what the agent meant, or is refused in a few lines without the global usage, a
git usage error or a traceback.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from comms_graph import cli

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")

LONG = ("@peer handoff: the parser now keeps the request id across retries, so the "
        "duplicate guard reads the stored id instead of the header. ") * 5

OK, HINT, HELP, SIZE = "ok", "hint", "help", "size"

CASES = [
    ("actor flag before the verb", OK, ["--as", "a1", "status"], {}),
    ("actor flag before board", OK, ["--as", "a1", "board"], {}),
    ("status with no actor in a known session", OK, ["status"], {"session": "S1"}),
    ("board with no actor in a known session", OK, ["board"], {"session": "S1"}),
    ("hello with no actor in a new session", OK, ["hello"], {"session": "S9"}),
    ("claim with no actor in a known session", OK,
     ["claim", "free.txt", "--intent", "x"], {"session": "S1"}),
    ("claims", OK, ["claims", "--as", "a1"], {}),
    ("whoami", OK, ["whoami", "--as", "a1"], {}),
    ("task list", OK, ["task", "list", "--as", "a1"], {}),
    ("task show", OK, ["task", "show", "demo-task", "--as", "a1"], {}),
    ("findings", OK, ["findings", "--as", "a1"], {}),
    ("--version", OK, ["--version"], {}),
    ("-V", OK, ["-V"], {}),
    ("claim --help is about claim", HELP, ["claim", "--help"], {"needs": "--steal", "max_lines": 30}),
    ("task add --help lists --title", HELP, ["task", "add", "--help"],
     {"needs": "--title", "max_lines": 30}),
    ("note of 640 characters", OK, ["note", LONG[:640], "--as", "a1"], {}),
    ("finding of 640 characters", OK, ["find", "decision", LONG[:640], "--as", "a1"], {}),
    ("task id of 40 characters", OK,
     ["task", "add", "haken-gates-planen-nicht-aufraeumen-jetzt",
      "--title", "Freeze switch stops planning only", "--as", "a1"], {}),
    ("task id with capitals and underscore", OK,
     ["task", "add", "Fix_Login-Flow", "--title", "Let people sign in again", "--as", "a1"], {}),
    ("claim tagged to a task that does not exist", HINT,
     ["claim", "t.txt", "--task", "no-such-task", "--intent", "x", "--as", "a1"],
     {"needs": "demo-task"}),
    ("check three paths", OK, ["check", "a.txt", "b.txt", "c.txt", "--as", "a1"], {}),
    ("release two scopes", OK, ["release", "a.txt", "b.txt", "--as", "a1", "--result", "done"],
     {"before": [["claim", "a.txt", "b.txt", "--intent", "x", "--as", "a1"]]}),
    ("release a scope nobody holds is not success", HINT, ["release", "nope.txt", "--as", "a1"], {}),
    ("release --all-mine with nothing held", OK,
     ["release", "--all-mine", "--result", "x", "--as", "a2"], {}),
    ("check --staged where the root is not a git repo", OK,
     ["check", "--staged", "--as", "a1"], {"nongit": True}),
    ("board where the root is not a git repo", OK, ["board", "--as", "a1"], {"nongit": True}),
    ("status where the root is not a git repo", OK, ["status", "--as", "a1"], {"nongit": True}),
    ("log --since with no actor", OK, ["log", "--since", "10d"], {}),
    ("log --type note", OK, ["log", "--type", "note", "--since", "30m", "--as", "a1"], {}),
    ("brief with no actor", OK, ["brief", "demo-task"], {}),
    ("tasks", OK, ["tasks", "--as", "a1", "--out", "{tmp}/tasks.html"], {}),
    ("unknown verb gets a short hint", HINT, ["inbox", "--as", "a1"], {}),
    ("unknown flag gets a short hint", HINT, ["claim", "x.txt", "--reason", "y", "--as", "a1"], {}),
    ("claim --why means --intent", OK, ["claim", "free.txt", "--why", "x", "--as", "a1"], {}),
    ("actor written with @", OK, ["status", "--as", "@a1"], {}),
    ("status stays small with 60 claims", SIZE, ["status", "--as", "a1"],
     {"fill": 60, "max_chars": 1800}),
    ("board stays small with 60 claims", SIZE, ["board", "--as", "a1"],
     {"fill": 60, "max_chars": 1800}),
]


def _call(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    was = os.getcwd()
    saved = {k: os.environ.get(k) for k in env}
    os.chdir(cwd)
    try:
        os.environ.update(env)
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(was)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, out.getvalue() + err.getvalue()


def _setup(tmp: Path, opt: dict) -> tuple[Path, dict[str, str]]:
    repo = tmp / "repo"
    repo.mkdir()
    env = {"COMMS_VENDOR": "anthropic"}
    if opt.get("nongit"):
        (repo / "lane").mkdir()
        env["COMMS_REPO"] = str(repo)
    else:
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "t"]):
            subprocess.run([GIT, *args], cwd=repo, check=True, capture_output=True)
        for name in ("a.txt", "b.txt", "c.txt", "held.txt", "free.txt"):
            (repo / name).write_text("x\n")
        subprocess.run([GIT, "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run([GIT, "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    _call(["hello", "--as", "a1"], repo, dict(env, CLAUDE_CODE_SESSION_ID="S1"))
    _call(["hello", "--as", "a2"], repo, dict(env, CLAUDE_CODE_SESSION_ID="S2"))
    _call(["task", "add", "demo-task", "--title", "Show the demo", "--as", "a1"], repo, env)
    _call(["claim", "held.txt", "--intent", "theirs", "--as", "a2"], repo, env)
    for i in range(opt.get("fill", 0)):
        _call(["claim", f"src/mod{i}/file{i}.py", "--intent", f"work {i}", "--as", f"peer-{i % 6}"],
              repo, env)
    for argv in opt.get("before", []):
        _call(argv, repo, env)
    if "session" in opt:
        env["CLAUDE_CODE_SESSION_ID"] = opt["session"]
    return repo, env


@pytest.mark.parametrize("name,expect,argv,opt", CASES, ids=[c[0] for c in CASES])
def test_the_call_shape_an_agent_typed(name, expect, argv, opt, tmp_path):
    repo, env = _setup(tmp_path, opt)
    code, text = _call([a.replace("{tmp}", str(tmp_path)) for a in argv], repo, env)
    lines = [ln for ln in text.splitlines() if ln.strip()]

    assert "Usage: comms-graph <command>" not in text or len(lines) <= 12, "dumped the usage"
    assert "usage: git" not in text, "leaked a git usage error"
    if expect == OK:
        assert code == 0, text
    elif expect == HINT:
        assert code != 0 and len(lines) <= 6, text
        assert opt.get("needs", "") in text
    elif expect == HELP:
        assert code == 0 and opt["needs"] in text and len(lines) <= opt["max_lines"], text
    else:
        assert code == 0 and len(text) <= opt["max_chars"], f"{len(text)} chars:\n{text}"


def test_the_released_scopes_are_really_released(tmp_path):
    repo, env = _setup(tmp_path, {"before": [["claim", "a.txt", "b.txt", "--intent", "x",
                                              "--as", "a1"]]})
    code, _ = _call(["release", "a.txt", "b.txt", "--as", "a1"], repo, env)
    assert code == 0
    code, text = _call(["claims", "--as", "a1"], repo, env)
    assert "holds nothing" in text


def test_check_blocks_when_any_of_several_paths_is_held(tmp_path):
    repo, env = _setup(tmp_path, {})
    code, text = _call(["check", "a.txt", "held.txt", "--as", "a1"], repo, env)
    assert code == 2 and "held.txt" in text and "@a2" in text


def test_a_partly_matching_release_frees_what_matches_and_exits_1(tmp_path):
    repo, env = _setup(tmp_path, {"before": [["claim", "a.txt", "--intent", "x", "--as", "a1"]]})
    code, text = _call(["release", "a.txt", "nope.txt", "--as", "a1"], repo, env)
    assert code == 1 and "RELEASED a.txt" in text and "'nope.txt'" in text
    assert _call(["claims", "--as", "a1"], repo, env)[1].startswith("@a1 holds nothing")


def test_release_force_still_takes_the_claim_id_after_the_flag(tmp_path):
    repo, env = _setup(tmp_path, {})
    _, text = _call(["board", "--all", "--as", "a1"], repo, env)
    claim_id = text.split("[")[1].split("]")[0]
    code, text = _call(["release", "--force", claim_id, "--reason", "gone", "--as", "a1"], repo, env)
    assert code == 0 and "RELEASED held.txt" in text, text


def test_a_claim_in_a_directory_that_does_not_exist_warns_in_one_line(tmp_path):
    repo, env = _setup(tmp_path, {})
    code, text = _call(["claim", "srcc/new.py", "--intent", "x", "--as", "a1"], repo, env)
    assert code == 0 and "CLAIMED" in text
    assert [ln for ln in text.splitlines() if ln.startswith("warning")] == [
        "warning: srcc/ does not exist under repo; claimed anyway "
        "(a new directory is fine, a typo is not)."]


def test_an_unknown_task_lists_the_open_ones_and_the_add_line(tmp_path):
    repo, env = _setup(tmp_path, {})
    code, text = _call(["claim", "t.txt", "--task", "demo-taks", "--intent", "x", "--as", "a1"],
                       repo, env)
    assert code == 2
    assert "Did you mean demo-task?" in text and "Open tasks: demo-task" in text
    assert 'comms-graph task add demo-taks --title "..."' in text


def test_the_documented_global_flags_work_in_any_position(tmp_path):
    repo, env = _setup(tmp_path, {})
    before = _call(["--as", "a1", "--json", "status"], repo, env)
    after = _call(["status", "--as", "a1", "--json"], repo, env)
    assert before == after and before[0] == 0
    assert _call(["--repo", str(repo), "whoami", "--as", "a1"], repo, env)[0] == 0


def test_the_python_module_entry_point_agrees(tmp_path):
    repo, env = _setup(tmp_path, {})
    run = subprocess.run([sys.executable, "-m", "comms_graph", "--as", "a1", "status"],
                         cwd=repo, env={**os.environ, **env}, capture_output=True, text=True)
    assert run.returncode == 0 and "you are @a1" in run.stdout, run.stdout + run.stderr


def test_task_done_takes_summary_and_result_as_the_note(tmp_path, monkeypatch):
    from comms_graph import cli, log as clog, state as cstate
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    assert cli.main(["task", "add", "demo", "--title", "Show the demo", "--as", "a1"]) == 0
    assert cli.main(["task", "done", "demo", "--summary", "kept the old cookie name", "--as", "a1"]) == 0
    events = clog.read(clog.log_path(repo))
    assert any("kept the old cookie name" in str(e.data) for e in events)


def test_release_task_frees_only_the_claims_tagged_to_it(tmp_path, monkeypatch, capsys):
    from comms_graph import cli
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    assert cli.main(["task", "add", "demo", "--title", "Show the demo", "--as", "a1"]) == 0
    assert cli.main(["claim", "a.txt", "--task", "demo", "--intent", "x", "--as", "a1"]) == 0
    assert cli.main(["claim", "b.txt", "--intent", "y", "--as", "a1"]) == 0
    capsys.readouterr()
    assert cli.main(["release", "--task", "demo", "--result", "done", "--as", "a1"]) == 0
    out = capsys.readouterr().out
    assert "RELEASED a.txt" in out and "b.txt" not in out
