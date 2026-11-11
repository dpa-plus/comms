"""One build, one version, and a way to tell when the install is stale."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import comms_graph
from comms_graph import doctor
from comms_graph import guard as cguard
from comms_graph import log as clog
from comms_graph import server as cserver

PYTHON_DIR = Path(__file__).resolve().parent.parent
REPO = PYTHON_DIR.parent
GIT = shutil.which("git")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- 1. packaging -----------------------------------------------------------


def test_wheel_has_both_console_scripts_and_the_source_version(tmp_path):
    pytest.importorskip("build")
    src = tmp_path / "src"
    shutil.copytree(PYTHON_DIR, src, ignore=shutil.ignore_patterns(
        ".venv", "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache"))
    out = tmp_path / "dist"
    done = subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(src)],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    wheel = next(out.glob("*.whl"))
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        entry = next(n for n in names if n.endswith("entry_points.txt"))
        meta = next(n for n in names if n.endswith("METADATA"))
        scripts = z.read(entry).decode()
        metadata = z.read(meta).decode()
        packaged_skill = z.read("comms_graph/data/using-comms/SKILL.md")
    assert "comms-graph = comms_graph.__main__:main" in scripts
    assert "comms = comms_graph.__main__:main" in scripts
    assert f"Version: {comms_graph.__version__}\n" in metadata
    assert packaged_skill == (src / "comms_graph" / "data" / "using-comms" / "SKILL.md").read_bytes()


def test_packaged_skill_is_the_repo_skill():
    repo_skill = REPO / "skills" / "using-comms" / "SKILL.md"
    if not repo_skill.exists():
        pytest.skip("not inside the repo checkout")
    assert doctor._packaged_skill() == repo_skill.read_bytes(), \
        "run `make sync-skill`: the packaged skill drifted from skills/using-comms/SKILL.md"


# --- 2. doctor --------------------------------------------------------------


class _Board(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"version": comms_graph.__version__}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def machine(tmp_path, monkeypatch):
    """An empty HOME, a fake launchctl, a stub board, and a git repo as cwd."""
    if GIT is None:
        pytest.skip("git is not installed")
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    state = tmp_path / "launchctl.loaded"
    calls = tmp_path / "launchctl.calls"
    launchctl = fakebin / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        f'case "$1" in\n'
        f'  list) [ -e "{state}" ] ;;\n'
        f'  bootstrap) touch "{state}" ;;\n'
        f'  kickstart) [ -e "{state}" ] ;;\n'
        f'esac\n')
    launchctl.chmod(launchctl.stat().st_mode | stat.S_IEXEC)
    httpd = HTTPServer(("127.0.0.1", 0), _Board)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-q", "."], cwd=repo, check=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("COMMS_BOARD_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.chdir(repo)
    try:
        yield home, repo, calls
    finally:
        httpd.shutdown()
        httpd.server_close()


def _put_links_on_path(home, monkeypatch):
    monkeypatch.setenv("PATH", f"{home / '.local' / 'bin'}{os.pathsep}{os.environ['PATH']}")


def test_doctor_on_an_empty_machine_lists_every_check_as_fix_with_a_command(machine, capsys):
    rc = doctor.main([])
    lines = capsys.readouterr().out.splitlines()
    assert rc == 1
    checks = [l for l in lines if l.startswith(("ok ", "fix"))]
    assert len(checks) == 7
    assert all(l.startswith("fix") for l in checks), lines
    for i, l in enumerate(lines):
        if l.startswith("fix"):
            assert lines[i + 1].strip().startswith("run: "), f"no command after: {l}"


def test_doctor_fix_repairs_the_install_and_is_idempotent(machine, capsys, monkeypatch):
    home, repo, calls = machine
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    other_pre = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo guard"}]}
    mixed_start = {"hooks": [{"type": "command", "command": "echo hello"},
                             {"type": "command", "command": "comms hello && comms status"}]}
    fixture = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "SessionStart": [mixed_start],
            "PreToolUse": [other_pre,
                           {"matcher": "Edit|Write",
                            "hooks": [{"type": "command", "command": "comms check --stdin-json"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}],
        },
    }
    settings.write_text(json.dumps(fixture, indent=2) + "\n")
    _put_links_on_path(home, monkeypatch)

    assert doctor.main(["--fix", "--board"]) == 1, "the commit guard is not installed yet"
    capsys.readouterr()

    packaged = doctor._packaged_skill()
    for target in doctor._skill_targets():
        assert target.read_bytes() == packaged
    after = json.loads(settings.read_text())
    assert after["model"] == "opus" and after["permissions"] == fixture["permissions"]
    assert after["hooks"]["Stop"] == fixture["hooks"]["Stop"]
    assert other_pre in after["hooks"]["PreToolUse"]
    assert {"hooks": [{"type": "command", "command": "echo hello"}]} in after["hooks"]["SessionStart"]
    flat = [(ev, e.get("matcher"), h["command"])
            for ev, es in after["hooks"].items() for e in es for h in e["hooks"]]
    assert not any("check --stdin-json" in c or "comms hello" in c for _e, _m, c in flat)
    for event, matcher, verb in doctor.HOOKS:
        assert (event, matcher, doctor._expected_command(verb)) in flat
    assert len(list(settings.parent.glob("settings.json.bak-*"))) == 1

    cguard.install(repo)
    assert doctor.main([]) == 0, capsys.readouterr().out
    snapshot = settings.read_bytes()
    calls_before = calls.read_text()
    assert doctor.main(["--fix"]) == 0
    assert settings.read_bytes() == snapshot
    assert len(list(settings.parent.glob("settings.json.bak-*"))) == 1
    assert "kickstart" not in calls.read_text().replace(calls_before, "")


def test_doctor_does_not_write_through_a_symlinked_skill(machine, tmp_path, monkeypatch):
    home, _repo, _calls = machine
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "SKILL.md").write_text("custom\n")
    link = home / ".claude" / "skills" / "using-comms"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    doctor.main(["--fix"])
    assert (real / "SKILL.md").read_text() == "custom\n"


# --- 3. /api/status ---------------------------------------------------------


def test_board_status_reports_the_package_version(tmp_path, monkeypatch):
    if GIT is None:
        pytest.skip("git is not installed")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-q", "."], cwd=repo, check=True)
    httpd = cserver.serve(repo, clog.log_path(repo), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/status"
        with urllib.request.urlopen(url, timeout=20) as r:
            assert json.loads(r.read())["version"] == comms_graph.__version__
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- 4. install.sh ----------------------------------------------------------


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("tar") is None, reason="needs sh and tar")
def test_install_script_links_working_commands_and_reruns(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    launchctl = fakebin / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 1\n")
    launchctl.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
           "COMMS_BOARD_URL": f"http://127.0.0.1:{_free_port()}"}
    env.pop("XDG_DATA_HOME", None)
    for _ in range(2):
        done = subprocess.run(["sh", str(REPO / "install.sh"), "--from", str(PYTHON_DIR)],
                              env=env, capture_output=True, text=True, timeout=900, cwd=tmp_path)
        assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]
        for name in ("comms-graph", "comms"):
            link = home / ".local" / "bin" / name
            assert link.is_symlink()
            out = subprocess.run([str(link), "version"], env=env, capture_output=True,
                                 text=True, timeout=60)
            assert out.returncode == 0 and comms_graph.__version__ in out.stdout
    assert (home / ".claude" / "skills" / "using-comms" / "SKILL.md").read_bytes() \
        == doctor._packaged_skill()


# --- 5. release tag check ---------------------------------------------------

TAG_SCRIPT = REPO / "scripts" / "check_release_tag.py"


@pytest.mark.skipif(not TAG_SCRIPT.exists(), reason="not inside the repo checkout")
def test_release_tag_check_fails_when_tag_and_version_differ():
    def run(tag):
        return subprocess.run([sys.executable, str(TAG_SCRIPT), tag], capture_output=True, text=True)

    assert run(f"v{comms_graph.__version__}").returncode == 0
    wrong = run("v0.0.1")
    assert wrong.returncode == 1 and comms_graph.__version__ in wrong.stderr
    assert run(comms_graph.__version__).returncode == 1, "a tag without the v prefix is not a release tag"
    assert run("").returncode == 1
