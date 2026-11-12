"""`doctor --fix` edits ~/.claude/settings.json: only comms hooks, never the link."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from comms_graph import doctor


def _settings(hooks: object) -> Path:
    path = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": "opus", "hooks": hooks}, indent=2) + "\n")
    return path


def _commands(path: Path) -> list[str]:
    hooks = json.loads(path.read_text())["hooks"]
    return [h["command"] for entries in hooks.values() for e in entries for h in e["hooks"]]


def _cmd(text: str) -> dict:
    return {"type": "command", "command": text}


def test_fix_writes_through_a_symlinked_settings_file_and_keeps_the_link(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"model": "opus"}) + "\n")
    link = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    doctor._fix_hooks()

    assert link.is_symlink() and link.resolve() == real.resolve()
    written = json.loads(real.read_text())
    assert written["model"] == "opus"
    assert doctor._expected_command("pre-edit") in _commands(real)
    assert doctor._check_hooks().ok


@pytest.mark.parametrize("text", [
    'echo "comms check ok" >> file',
    "echo comms hook pre-edit is great",
    "grep comms status.txt",
    "printf 'comms-graph check' | cat",
])
def test_a_hook_that_only_mentions_comms_is_kept(text):
    path = _settings({"PreToolUse": [{"matcher": "Edit", "hooks": [_cmd(text)]}]})
    doctor._fix_hooks()
    assert text in _commands(path)


@pytest.mark.parametrize("text", [
    "comms hello && comms status --since 24h",
    "/Users/x/.local/bin/comms-graph check --stdin-json",
    "cd /tmp; comms-graph hook pre-edit",
    "true || comms status",
    "cat payload | comms check --stdin-json",
    "'/opt/my tools/comms' hook session-start",
])
def test_a_hook_that_runs_comms_is_replaced(text):
    path = _settings({"PreToolUse": [{"matcher": "Edit", "hooks": [_cmd(text)]}]})
    doctor._fix_hooks()
    assert text not in _commands(path)


def test_only_the_comms_hook_leaves_a_mixed_entry():
    keep = ['echo "comms check ok" >> file', "echo guard"]
    path = _settings({"SessionStart": [{"hooks": [_cmd(keep[0]), _cmd("comms hello"),
                                                  _cmd(keep[1])]}]})
    doctor._fix_hooks()
    entries = json.loads(path.read_text())["hooks"]["SessionStart"]
    assert {"hooks": [_cmd(keep[0]), _cmd(keep[1])]} in entries


@pytest.mark.parametrize("hooks", [
    "not an object",
    {"PreToolUse": {"matcher": "Edit"}},
    {"PreToolUse": [{"matcher": "Edit", "hooks": "comms check --stdin-json"}]},
])
def test_a_hooks_value_of_the_wrong_shape_is_reported_and_left_alone(hooks):
    path = _settings(hooks)
    before = path.read_bytes()
    check = doctor._check_hooks()
    assert not check.ok
    assert "by hand" in check.command
    assert check.fixer is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("text", [
    "comms check --stdin-json; /opt/bin/other-guard",
    "/opt/bin/other-guard && comms-graph hook pre-edit",
])
def test_a_line_that_also_runs_another_program_is_left_alone(text):
    path = _settings({"PreToolUse": [{"matcher": "Edit", "hooks": [_cmd(text)]}]})
    doctor._fix_hooks()
    assert text in _commands(path)
