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
