"""comms keeps agents off each other's files and asks them for nothing else.

The skill and the session briefing are what an agent is told unprompted, so they
may only show the commands that claim, release, look and leave a note.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from comms_graph import log as clog
from comms_graph import state as cstate

from .test_session_identity import SESSION_A, _repo, _run, _start_store

SKILL = Path(__file__).resolve().parents[1] / "comms_graph" / "data" / "using-comms" / "SKILL.md"
COORDINATION = {"claim", "release", "note", "status", "board", "check", "doctor"}
PRESCRIBES = ("--review", "task review", "comms find", "comms-graph find", "--evidence",
              "comms next", "comms brief", "task edge", "comms task", "comms-graph task",
              "comms tasks", "comms-graph tasks", "comms-graph next", "comms-graph brief",
              "--task")


def _commands(text: str) -> set[str]:
    code = re.findall(r"`([^`\n]+)`", text)
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
        code.extend(block.splitlines())
    shown = set()
    for line in code:
        m = re.match(r"\s*comms(?:-graph)?(?: --(?:repo|root|as) \S+)* ([a-z][a-z-]*)", line)
        if m:
            shown.add(m.group(1))
    return shown


def test_the_skill_shows_only_coordination_commands():
    text = SKILL.read_text(encoding="utf-8")
    assert _commands(text) <= COORDINATION, sorted(_commands(text) - COORDINATION)
    for phrase in PRESCRIBES:
        assert phrase not in text, f"the skill tells agents about {phrase!r}"


def test_the_session_briefing_shows_only_coordination_commands(tmp_path):
    repo = _repo(tmp_path)
    _start_store(repo)
    payload = {"session_id": SESSION_A, "cwd": str(repo), "hook_event_name": "SessionStart"}
    code, out, _err = _run(repo, ["hook", "session-start"], stdin=json.dumps(payload))
    assert code == 0 and out
    shown = set(re.findall(r"comms(?:-graph)? ([a-z][a-z-]*)", out))
    assert shown <= COORDINATION, sorted(shown - COORDINATION)
    for phrase in PRESCRIBES:
        assert phrase not in out


def test_the_retired_task_forms_do_nothing_and_say_so_in_one_line(tmp_path):
    from comms_graph import verbs

    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    for argv in (["task", "add", "demo", "--title", "Show the demo", "--review"],
                 ["task", "done", "demo", "--check", "test=pass"], ["next"],
                 ["brief", "demo"], ["tasks", "--out", str(tmp_path / "t.html")]):
        code, out, err = _run(repo, argv, env=env)
        assert (code, out, err.strip()) == (0, "", verbs.RETIRED_NOTICE), argv
    assert len(verbs.RETIRED_NOTICE) < 100
    assert not (tmp_path / "t.html").exists()
    assert not clog.log_path(repo).exists(), "a retired verb wrote to the log"


def test_the_retired_task_flag_never_stops_a_claim_or_a_release(tmp_path):
    from comms_graph import verbs

    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    code, out, err = _run(repo, ["claim", "src/a.py", "--task", "no-such-task",
                                 "--intent", "x", "--as", "dev"], env=env)
    assert code == 0 and err.strip() == verbs.RETIRED_NOTICE, out + err
    held = cstate.fold(clog.read(clog.log_path(repo))).claims.values()
    assert [str(c.scope) for c in held] == ["src/a.py"]
    code, out, err = _run(repo, ["release", "--task", "demo", "--result", "done",
                                 "--as", "dev"], env=env)
    assert code == 0 and "RELEASED src/a.py" in out, out + err
    assert not cstate.fold(clog.read(clog.log_path(repo))).claims
    for path in ("claim", "release"):
        assert "--task" not in verbs.verb_help(verbs.lookup(path))
    assert "task" not in verbs.global_usage().split()
