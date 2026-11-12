from __future__ import annotations

import os
import shlex
import sys
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path, monkeypatch):
    """No test may write the real data home, and no host agent session leaks in.

    The suite itself usually runs inside a Claude Code session, whose
    CLAUDE_CODE_SESSION_ID would otherwise make every actor-less call resolve to
    whoever ran pytest. A `comms-graph` shim ahead of PATH makes anything that
    shells out (the commit guard) run this checkout, not the installed tool.
    """
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COMMS_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for name in ("COMMS_ACTOR", "CLAUDE_CODE_SESSION_ID", "COMMS_VENDOR",
                 "COMMS_MODEL", "COMMS_LABEL", "COMMS_REPO",
                 "COMMS_ALLOW_GENERIC_ACTOR"):
        monkeypatch.delenv(name, raising=False)
    bindir = tmp_path / "isolated-bin"
    bindir.mkdir()
    shim = bindir / "comms-graph"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m comms_graph "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
