"""comms keeps agents off each other's files and asks them for nothing else.

The skill and the session briefing are what an agent is told unprompted, so they
may only show the commands that claim, release, look and leave a note.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .test_session_identity import SESSION_A, _repo, _run, _start_store

SKILL = Path(__file__).resolve().parents[1] / "comms_graph" / "data" / "using-comms" / "SKILL.md"
COORDINATION = {"claim", "release", "note", "status", "board", "check", "task", "doctor"}
PRESCRIBES = ("--review", "task review", "comms find", "comms-graph find", "--evidence",
              "comms next", "comms brief", "task edge")


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


def test_adding_and_finishing_a_task_does_not_mention_review(tmp_path):
    repo = _repo(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": SESSION_A}
    code, out, err = _run(repo, ["task", "add", "demo", "--title", "Show the demo"], env=env)
    assert code == 0 and "review" not in (out + err).lower()
    code, out, err = _run(repo, ["task", "done", "demo"], env=env)
    assert code == 0 and "review" not in (out + err).lower()
