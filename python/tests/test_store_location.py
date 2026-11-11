"""Where a store lives: COMMS_HOME, throwaway repos, and the test suite itself."""

from __future__ import annotations

import io
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from comms_graph import cli as ccli
from comms_graph import log as clog


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n")
    return path


def _claim(repo: Path, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    was = os.getcwd()
    os.chdir(repo)
    try:
        with redirect_stdout(out), redirect_stderr(out):
            code = ccli.main(["claim", "a.py", "--as", "store-tester", *args])
    finally:
        os.chdir(was)
    return code, out.getvalue()


def test_the_suite_never_resolves_the_real_data_home(tmp_path):
    assert tmp_path in clog.user_data_home().parents


def test_comms_home_moves_the_store(tmp_path, monkeypatch):
    elsewhere = tmp_path / "chosen-home"
    monkeypatch.setenv("COMMS_HOME", str(elsewhere))
    assert clog.user_data_home() == elsewhere
    monkeypatch.setattr(clog, "_temp_store_base", lambda root: None)
    repo = _git_repo(tmp_path / "repo")
    code, out = _claim(repo)
    assert code == 0, out
    assert (elsewhere / "comms" / clog.repo_hash(repo) / "log.jsonl").is_file()


def test_a_relative_comms_home_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMS_HOME", "relative/dir")
    assert clog.user_data_home().is_absolute()


def test_a_repo_under_the_temp_dir_stores_under_the_temp_dir(tmp_path):
    repo = _git_repo(tmp_path / "scratch-repo")
    code, out = _claim(repo)
    assert code == 0, out
    store = tmp_path / "comms-stores" / clog.repo_hash(repo)
    assert (store / "log.jsonl").is_file()
    assert not (clog.user_data_home() / "comms").exists()
    assert "temporary directory" not in out
