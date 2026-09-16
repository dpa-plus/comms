"""The dashboard reloads installed Python code without touching Comms data."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest


_TEST_PACKAGE_FILES = (
    "__init__.py",
    "__main__.py",
    "cli.py",
    "contact.py",
    "runtime_refresh.py",
    "server.py",
)


def _installed_package(tmp_path, server_source="VERSION = 1\n"):
    package = tmp_path / "comms_graph"
    package.mkdir()
    for name in _TEST_PACKAGE_FILES:
        (package / name).write_text("# installed source\n", encoding="utf-8")
    source = package / "server.py"
    source.write_text(server_source, encoding="utf-8")
    _write_distribution_record(package)
    return package, source


def _write_distribution_record(package, source_names=None):
    """Publish the wheel's authoritative list of installed Python sources."""
    if source_names is None:
        source_names = sorted(
            str(path.relative_to(package)) for path in package.rglob("*.py")
        )
    metadata = package.parent / "comms_graph-0.1.0.dist-info"
    metadata.mkdir(exist_ok=True)
    record = metadata / "RECORD"
    record.write_text(
        "".join(f"{package.name}/{name},,\n" for name in source_names),
        encoding="utf-8",
    )
    return record


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

    package, _source = _installed_package(tmp_path)
    restarts = []

    watcher = runtime_refresh.SourceCodeWatcher(package, lambda: restarts.append("restart"))

    watcher.poll_once()
    watcher.poll_once()

    assert restarts == []


def test_changed_python_source_requests_restart_after_debounce(tmp_path):
    """A stable code replacement should reload once, after installs settle."""
    from comms_graph import runtime_refresh

    package, source = _installed_package(tmp_path)
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

    package, source = _installed_package(tmp_path)
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

    package, _source = _installed_package(tmp_path)
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

    package, source = _installed_package(tmp_path)
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

    package, _source = _installed_package(tmp_path)
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

    package, source = _installed_package(tmp_path)
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

    package, _source = _installed_package(tmp_path)
    fingerprints = iter(["original", OSError("file moved"), "updated", "updated"])

    def fingerprint(_package_dir):
        value = next(fingerprints)
        if isinstance(value, OSError):
            raise value
        return value

    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
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


@pytest.mark.parametrize(
    "create_directory",
    [False, True],
)
def test_missing_or_empty_package_tree_is_not_a_source_sample(
    tmp_path, create_directory
):
    """There is no valid baseline manifest without a package and Python source."""
    from comms_graph import runtime_refresh

    package = tmp_path / "comms_graph"
    if create_directory:
        package.mkdir()

    with pytest.raises(OSError):
        runtime_refresh._source_fingerprint(package)


def test_removed_package_tree_is_ignored_until_complete_sources_return(tmp_path):
    """Removal during pip replacement must reset debounce until a full build exists."""
    from comms_graph import runtime_refresh

    package, source = _installed_package(tmp_path)
    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        lambda: restarts.append("restart"),
        debounce_seconds=1.0,
        clock=clock,
    )

    removed = tmp_path / "comms_graph.installing"
    package.rename(removed)
    watcher.poll_once()
    clock.advance(5.0)
    watcher.poll_once()
    assert restarts == []

    removed.rename(package)
    source.write_text("VERSION = 2\n", encoding="utf-8")
    watcher.poll_once()
    clock.advance(1.0)
    watcher.poll_once()

    assert restarts == ["restart"]


def test_missing_recorded_module_is_unstable_until_restored(tmp_path):
    """A module still listed by the candidate release cannot enter debounce."""
    from comms_graph import runtime_refresh

    package, source = _installed_package(tmp_path)
    contact = package / "contact.py"
    displaced = tmp_path / "contact.py.installing"
    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        lambda: restarts.append("restart"),
        debounce_seconds=1.0,
        clock=clock,
    )

    contact.rename(displaced)
    source.write_text("VERSION = 2\n", encoding="utf-8")
    watcher.poll_once()
    clock.advance(5.0)
    watcher.poll_once()
    assert restarts == [], "an incomplete package survived the debounce"

    displaced.write_text("# restored by the completed install\n", encoding="utf-8")
    displaced.rename(contact)
    watcher.poll_once()
    clock.advance(1.0)
    watcher.poll_once()
    watcher.poll_once()

    assert restarts == ["restart"]


def test_new_recorded_module_must_arrive_before_candidate_can_debounce(tmp_path):
    """A newly introduced dependency cannot trigger refresh before it is installed."""
    from comms_graph import runtime_refresh

    package, source = _installed_package(tmp_path)
    next_module = package / "contacts.py"
    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        lambda: restarts.append("restart"),
        debounce_seconds=1.0,
        clock=clock,
    )

    source.write_text("VERSION = 2\n", encoding="utf-8")
    _write_distribution_record(
        package,
        source_names=sorted((*_TEST_PACKAGE_FILES, next_module.name)),
    )
    watcher.poll_once()
    clock.advance(5.0)
    watcher.poll_once()
    assert restarts == [], "a RECORD-listed source was still absent"

    next_module.write_text("# installed dependency\n", encoding="utf-8")
    watcher.poll_once()
    clock.advance(1.0)
    watcher.poll_once()
    watcher.poll_once()

    assert restarts == ["restart"]


def test_complete_record_can_intentionally_remove_a_baseline_module(tmp_path):
    """A complete candidate release may delete a module from the old release."""
    from comms_graph import runtime_refresh

    package, source = _installed_package(tmp_path)
    restarts = []
    clock = ManualClock()
    watcher = runtime_refresh.SourceCodeWatcher(
        package,
        lambda: restarts.append("restart"),
        debounce_seconds=1.0,
        clock=clock,
    )

    (package / "contact.py").unlink()
    source.write_text("VERSION = 2\n", encoding="utf-8")
    _write_distribution_record(package)
    watcher.poll_once()
    clock.advance(1.0)
    watcher.poll_once()
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


def test_ui_closes_watcher_and_server_socket_before_process_refresh(
    tmp_path, monkeypatch
):
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
        "server-close",
        "process-refresh",
    ]


def test_ui_closes_unstarted_real_server_when_initial_fingerprint_fails(
    tmp_path, monkeypatch
):
    """A startup scan failure must not deadlock before closing the real socket."""
    from comms_graph import cli
    from comms_graph import runtime_refresh
    from comms_graph import server

    real_serve = server.serve
    bound = threading.Event()
    servers = []

    def capture_server(*args, **kwargs):
        httpd = real_serve(*args, **kwargs)
        servers.append(httpd)
        bound.set()
        return httpd

    class BrokenWatcher:
        def __init__(self, package_dir, request_restart):
            raise OSError("installed package is moving")

    monkeypatch.setattr(
        cli,
        "_task_runtime",
        lambda _flags: (tmp_path, tmp_path / "log.jsonl", None),
    )
    monkeypatch.setattr(server, "serve", capture_server)
    monkeypatch.setattr(runtime_refresh, "SourceCodeWatcher", BrokenWatcher)

    outcome = {}

    def run_ui():
        try:
            outcome["result"] = cli._cmd_ui(["--no-open", "--port", "0"])
        except BaseException as exc:  # captured so the test can bound the call
            outcome["error"] = exc

    worker = threading.Thread(target=run_ui, daemon=True)
    worker.start()
    assert bound.wait(1.0), "the real HTTP server never bound"
    worker.join(0.25)
    try:
        assert not worker.is_alive(), (
            "_cmd_ui blocked in BaseServer.shutdown() before serve_forever() started"
        )
    finally:
        if worker.is_alive():
            # RED on d5d07b1 lands here. Starting the loop after shutdown was
            # requested lets BaseServer set its completion event, so the
            # deliberately blocked daemon thread cannot leak out of this test.
            rescue = threading.Thread(
                target=servers[0].serve_forever,
                kwargs={"poll_interval": 0.01},
                daemon=True,
            )
            rescue.start()
            worker.join(2.0)
            rescue.join(2.0)

    assert isinstance(outcome.get("error"), OSError)
    assert str(outcome["error"]) == "installed package is moving"
    assert servers[0].socket.fileno() == -1


def test_ui_refresh_shuts_down_and_closes_a_real_running_server(tmp_path, monkeypatch):
    """The ordinary watcher path still stops a serving socket before refresh."""
    from comms_graph import cli
    from comms_graph import runtime_refresh
    from comms_graph import server

    real_serve = server.serve
    serving = threading.Event()
    servers = []
    refreshes = []

    def capture_server(*args, **kwargs):
        httpd = real_serve(*args, **kwargs)
        real_loop = httpd.serve_forever

        def tracked_loop():
            serving.set()
            return real_loop(poll_interval=0.01)

        httpd.serve_forever = tracked_loop
        servers.append(httpd)
        return httpd

    class TriggeringWatcher:
        restart_requested = True

        def __init__(self, package_dir, request_restart):
            self.request_restart = request_restart
            self.thread = None

        def start(self):
            def request_after_serving_begins():
                if serving.wait(1.0):
                    self.request_restart()

            self.thread = threading.Thread(
                target=request_after_serving_begins,
                daemon=True,
            )
            self.thread.start()

        def close(self):
            if self.thread is not None:
                self.thread.join(2.0)

    monkeypatch.setattr(
        cli,
        "_task_runtime",
        lambda _flags: (tmp_path, tmp_path / "log.jsonl", None),
    )
    monkeypatch.setattr(server, "serve", capture_server)
    monkeypatch.setattr(runtime_refresh, "SourceCodeWatcher", TriggeringWatcher)
    monkeypatch.setattr(
        runtime_refresh,
        "restart_current_process",
        lambda: refreshes.append("process-refresh"),
    )

    outcome = {}

    def run_ui():
        try:
            outcome["result"] = cli._cmd_ui(["--no-open", "--port", "0"])
        except BaseException as exc:  # captured for a bounded assertion below
            outcome["error"] = exc

    worker = threading.Thread(target=run_ui, daemon=True)
    worker.start()
    worker.join(2.0)
    if worker.is_alive() and servers:
        servers[0].shutdown()
        worker.join(2.0)

    assert not worker.is_alive(), "ordinary watcher refresh left _cmd_ui blocked"
    assert outcome == {"result": cli.EXIT_OK}
    assert refreshes == ["process-refresh"]
    assert servers[0].socket.fileno() == -1


def test_ui_drains_an_accepted_release_before_process_replacement(
    tmp_path, monkeypatch
):
    """A refresh must not exec/exit while an accepted release is being fsynced."""
    import json
    import urllib.request
    from datetime import datetime, timezone

    from comms_graph import cli
    from comms_graph import log as clog
    from comms_graph import runtime_refresh
    from comms_graph import server
    from comms_graph import state

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    log_file = clog.log_path(repo)
    claim = clog.Event(
        ts=datetime.now(timezone.utc),
        id=clog.new_id(),
        actor="working-agent",
        type=clog.TYPE_CLAIM,
        scope=["src/release.py"],
        data={"intent": "finish safely"},
    )
    clog.append(log_file, claim)

    real_append = clog.append
    mutation_started = threading.Event()
    allow_mutation_to_finish = threading.Event()
    mutation_finished = threading.Event()
    events = []

    def blocking_append(path, event):
        if event.type == clog.TYPE_RELEASE:
            events.append("mutation-started")
            mutation_started.set()
            if not allow_mutation_to_finish.wait(3.0):
                raise TimeoutError("test did not release the pending mutation")
        result = real_append(path, event)
        if event.type == clog.TYPE_RELEASE:
            events.append("mutation-finished")
            mutation_finished.set()
        return result

    monkeypatch.setattr(clog, "append", blocking_append)
    serving = threading.Event()
    servers = []
    real_serve = server.serve

    def capture_server(*args, **kwargs):
        httpd = real_serve(*args, **kwargs)
        real_loop = httpd.serve_forever

        def tracked_loop():
            serving.set()
            return real_loop(poll_interval=0.01)

        httpd.serve_forever = tracked_loop
        servers.append(httpd)
        return httpd

    trigger_refresh = threading.Event()

    class ControlledWatcher:
        restart_requested = True

        def __init__(self, package_dir, request_restart):
            self.request_restart = request_restart
            self.thread = None

        def start(self):
            def trigger_when_requested():
                if trigger_refresh.wait(3.0):
                    self.request_restart()

            self.thread = threading.Thread(target=trigger_when_requested, daemon=True)
            self.thread.start()

        def close(self):
            if self.thread is not None:
                self.thread.join(3.0)

    replacement = threading.Event()

    def record_replacement():
        events.append("process-replacement")
        replacement.set()

    monkeypatch.setattr(cli, "_task_runtime", lambda _flags: (repo, log_file, None))
    monkeypatch.setattr(server, "serve", capture_server)
    monkeypatch.setattr(runtime_refresh, "SourceCodeWatcher", ControlledWatcher)
    monkeypatch.setattr(runtime_refresh, "restart_current_process", record_replacement)

    ui_outcome = {}

    def run_ui():
        try:
            ui_outcome["result"] = cli._cmd_ui(["--no-open", "--port", "0"])
        except BaseException as exc:
            ui_outcome["error"] = exc

    ui_thread = threading.Thread(target=run_ui, daemon=True)
    ui_thread.start()
    assert serving.wait(1.0), "the UI server never entered serve_forever"

    request_outcome = {}
    request = urllib.request.Request(
        f"http://127.0.0.1:{servers[0].server_address[1]}/api/release",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "id": claim.id,
                "reason": "runtime refresh",
                "actor": "human-reviewer",
            }
        ).encode("utf-8"),
    )

    def post_release():
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                request_outcome["status"] = response.status
                request_outcome["body"] = json.loads(response.read())
        except BaseException as exc:
            request_outcome["error"] = exc

    request_thread = threading.Thread(target=post_release, daemon=True)
    request_thread.start()
    assert mutation_started.wait(1.0), "the release never reached the append boundary"

    trigger_refresh.set()
    try:
        assert not replacement.wait(0.4), (
            "process replacement crossed while an accepted release was still appending"
        )
        assert ui_thread.is_alive(), "the UI returned before its accepted mutation drained"
    finally:
        allow_mutation_to_finish.set()
        request_thread.join(3.0)
        ui_thread.join(3.0)
        if ui_thread.is_alive() and servers:
            servers[0].shutdown()
            servers[0].server_close()
            ui_thread.join(2.0)

    assert mutation_finished.is_set()
    assert replacement.is_set()
    assert events.index("mutation-finished") < events.index("process-replacement")
    assert request_outcome == {
        "status": 200,
        "body": {
            "ok": True,
            "released": claim.id,
            "was": "working-agent",
            "scope": "src/release.py",
        },
    }
    assert ui_outcome == {"result": cli.EXIT_OK}
    assert claim.id not in {item.id for item in state.fold(clog.read(log_file)).claims.values()}
