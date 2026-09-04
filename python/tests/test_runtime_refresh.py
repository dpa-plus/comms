"""The dashboard reloads installed Python code without touching Comms data."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_unchanged_python_sources_do_not_request_restart(tmp_path):
    """A quiet installation must leave a long-running dashboard alone."""
    from comms_graph import runtime_refresh

    package = tmp_path / "comms_graph"
    package.mkdir()
    (package / "server.py").write_text("VERSION = 1\n", encoding="utf-8")
    restarts = []

    watcher = runtime_refresh.SourceCodeWatcher(package, lambda: restarts.append("restart"))

    watcher.poll_once()
    watcher.poll_once()

    assert restarts == []


def test_changed_python_source_requests_restart_after_debounce(tmp_path):
    """A stable code replacement should reload once, after installs settle."""
    from comms_graph import runtime_refresh

    package = tmp_path / "comms_graph"
    package.mkdir()
    source = package / "server.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        lambda: restarts.append("restart"),
        debounce_seconds=1.0,
        clock=clock,
    )

    source.write_text("VERSION = 2\n", encoding="utf-8")
    watcher.poll_once()
    clock.advance(0.9)
    watcher.poll_once()
    assert restarts == []

    clock.advance(0.1)
    watcher.poll_once()

    assert restarts == ["restart"]


def test_reinstalled_identical_python_source_requests_restart(tmp_path):
    """Replacing a wheel with identical bytes still reloads its imported modules."""
    from comms_graph import runtime_refresh

    package = tmp_path / "comms_graph"
    package.mkdir()
    source = package / "server.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        lambda: restarts.append("restart"),
        debounce_seconds=1.0,
        clock=clock,
    )

    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    watcher.poll_once()
    clock.advance(1.0)
    watcher.poll_once()

    assert restarts == ["restart"]


def test_non_python_files_do_not_request_restart(tmp_path):
    """Data and package-resource writes must not churn the dashboard process."""
    from comms_graph import runtime_refresh

    package = tmp_path / "comms_graph"
    package.mkdir()
    (package / "server.py").write_text("VERSION = 1\n", encoding="utf-8")
    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        lambda: restarts.append("restart"),
        debounce_seconds=1.0,
        clock=clock,
    )

    (package / "instructions.md").write_text("new instructions\n", encoding="utf-8")
    watcher.poll_once()
    clock.advance(1.0)
    watcher.poll_once()

    assert restarts == []


def test_repeated_polls_of_one_change_request_only_one_restart(tmp_path):
    """One installed build must not cause a restart storm while shutdown runs."""
    from comms_graph import runtime_refresh

    package = tmp_path / "comms_graph"
    package.mkdir()
    source = package / "server.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        lambda: restarts.append("restart"),
        debounce_seconds=1.0,
        clock=clock,
    )

    source.write_text("VERSION = 2\n", encoding="utf-8")
    watcher.poll_once()
    for _ in range(4):
        clock.advance(1.0)
        watcher.poll_once()

    assert restarts == ["restart"]


def test_close_wakes_and_joins_a_sleeping_watcher(tmp_path):
    """Server cleanup must not wait for the next long polling interval."""
    from comms_graph import runtime_refresh

    package = tmp_path / "comms_graph"
    package.mkdir()
    (package / "server.py").write_text("VERSION = 1\n", encoding="utf-8")
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        lambda: None,
        poll_seconds=30.0,
    )
    watcher.start()

    started = time.monotonic()
    watcher.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_running_watcher_periodically_detects_python_changes(tmp_path):
    """Starting the watcher must turn the tested poll logic into live behavior."""
    from comms_graph import runtime_refresh

    package = tmp_path / "comms_graph"
    package.mkdir()
    source = package / "server.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    restarted = threading.Event()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        restarted.set,
        poll_seconds=0.01,
        debounce_seconds=0.01,
    )
    watcher.start()
    try:
        source.write_text("VERSION = 2\n", encoding="utf-8")
        assert restarted.wait(1.0), "the running watcher never requested a restart"
    finally:
        watcher.close()


def test_transient_install_scan_error_does_not_disable_later_refresh(tmp_path):
    """A package file replaced mid-scan must not kill the watcher thread."""
    from comms_graph import runtime_refresh

    fingerprints = iter(["original", OSError("file moved"), "updated", "updated"])

    def fingerprint(_package_dir):
        value = next(fingerprints)
        if isinstance(value, OSError):
            raise value
        return value

    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        tmp_path,
        lambda: restarts.append("restart"),
        debounce_seconds=1.0,
        clock=clock,
        fingerprint=fingerprint,
    )

    watcher.poll_once()
    watcher.poll_once()
    clock.advance(1.0)
    watcher.poll_once()

    assert restarts == ["restart"]


def test_manual_refresh_execs_the_same_ui_arguments(monkeypatch):
    """A manually started board must reload without depending on launchd."""
    from comms_graph import runtime_refresh

    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    exec_calls = []

    runtime_refresh.restart_current_process(
        argv=["/installed/bin/comms-graph", "ui", "--no-open", "--port", "7879"],
        executable="/installed/bin/python",
        exec_process=lambda executable, argv: exec_calls.append((executable, argv)),
    )

    assert exec_calls == [
        (
            "/installed/bin/python",
            [
                "/installed/bin/python",
                "-m",
                "comms_graph",
                "ui",
                "--no-open",
                "--port",
                "7879",
            ],
        )
    ]


def test_launchd_refresh_exits_for_keepalive_instead_of_execing(monkeypatch):
    """A launchd-owned board must not race its supervisor with a replacement."""
    from comms_graph import runtime_refresh

    monkeypatch.setenv("XPC_SERVICE_NAME", "plus.dpa.comms-ui")
    exec_calls = []

    runtime_refresh.restart_current_process(
        argv=["/installed/bin/comms-graph", "ui", "--no-open"],
        executable="/installed/bin/python",
        exec_process=lambda executable, argv: exec_calls.append((executable, argv)),
    )

    assert exec_calls == []


def test_unrelated_xpc_parent_does_not_disable_manual_refresh(monkeypatch):
    """Only the known KeepAlive job may stand in for a manual process refresh."""
    from comms_graph import runtime_refresh

    monkeypatch.setenv("XPC_SERVICE_NAME", "application.com.example.dashboard-launcher")
    exec_calls = []

    runtime_refresh.restart_current_process(
        argv=["/installed/bin/comms-graph", "ui", "--no-open"],
        executable="/installed/bin/python",
        exec_process=lambda executable, argv: exec_calls.append((executable, argv)),
    )

    assert len(exec_calls) == 1


def test_ui_closes_watcher_and_server_before_process_refresh(tmp_path, monkeypatch):
    """The listening socket and watcher must be gone before replacement starts."""
    from comms_graph import cli
    from comms_graph import runtime_refresh
    from comms_graph import server

    events = []

    class FakeServer:
        def serve_forever(self):
            events.append("serve")

        def shutdown(self):
            events.append("server-shutdown")

        def server_close(self):
            events.append("server-close")

    class FakeWatcher:
        restart_requested = True

        def __init__(self, package_dir, request_restart):
            events.append(("watcher-init", package_dir, request_restart.__self__))

        def start(self):
            events.append("watcher-start")

        def close(self):
            events.append("watcher-close")

    fake_server = FakeServer()
    log_file = tmp_path / "log.jsonl"
    monkeypatch.setattr(cli, "_task_runtime", lambda flags: (tmp_path, log_file, None))
    monkeypatch.setattr(server, "serve", lambda *args, **kwargs: fake_server)
    monkeypatch.setattr(runtime_refresh, "SourceCodeWatcher", FakeWatcher)
    monkeypatch.setattr(
        runtime_refresh,
        "restart_current_process",
        lambda: events.append("process-refresh"),
    )

    result = cli._cmd_ui(["--no-open"])

    assert result == cli.EXIT_OK
    assert events == [
        ("watcher-init", Path(runtime_refresh.__file__).resolve().parent, fake_server),
        "watcher-start",
        "serve",
        "watcher-close",
        "server-shutdown",
        "server-close",
        "process-refresh",
    ]


def test_ui_closes_listening_socket_when_initial_fingerprint_fails(tmp_path, monkeypatch):
    """A startup scan failure must not leak the socket that launchd will retry."""
    import pytest

    from comms_graph import cli
    from comms_graph import runtime_refresh
    from comms_graph import server

    events = []

    class FakeServer:
        def shutdown(self):
            events.append("server-shutdown")

        def server_close(self):
            events.append("server-close")

    class BrokenWatcher:
        def __init__(self, package_dir, request_restart):
            raise OSError("installed package is moving")

    fake_server = FakeServer()
    monkeypatch.setattr(
        cli,
        "_task_runtime",
        lambda _flags: (tmp_path, tmp_path / "log.jsonl", None),
    )
    monkeypatch.setattr(server, "serve", lambda *args, **kwargs: fake_server)
    monkeypatch.setattr(runtime_refresh, "SourceCodeWatcher", BrokenWatcher)

    with pytest.raises(OSError, match="installed package is moving"):
        cli._cmd_ui(["--no-open"])

    assert events == ["server-shutdown", "server-close"]
