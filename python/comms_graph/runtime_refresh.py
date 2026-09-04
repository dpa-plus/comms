"""Refresh the long-running dashboard when its installed Python code changes."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable


LAUNCHD_SERVICE_NAME = "plus.dpa.comms-ui"


def _source_paths(package_dir: Path) -> tuple[Path, ...]:
    """List one nonempty installed Python source tree or reject the sample."""
    package_dir = Path(package_dir)
    if not package_dir.is_dir():
        raise FileNotFoundError(f"installed package directory is absent: {package_dir}")
    paths = tuple(sorted(item for item in package_dir.rglob("*.py") if item.is_file()))
    if not paths:
        raise OSError(f"installed package source tree is empty: {package_dir}")
    return paths


def _source_manifest(package_dir: Path) -> frozenset[str]:
    """Return every Python source path present in one package sample."""
    package_dir = Path(package_dir)
    return frozenset(
        str(path.relative_to(package_dir)) for path in _source_paths(package_dir)
    )


def _source_fingerprint(package_dir: Path) -> str:
    """Return a stable fingerprint for the installed package files."""
    package_dir = Path(package_dir)
    digest = hashlib.sha256()
    for path in _source_paths(package_dir):
        stat = path.stat()
        digest.update(str(path.relative_to(package_dir)).encode("utf-8"))
        digest.update(f"\0{stat.st_mtime_ns}:{stat.st_size}\0".encode("ascii"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class SourceCodeWatcher:
    """Compare the installed package with the snapshot taken at startup."""

    def __init__(
        self,
        package_dir: Path,
        request_restart: Callable[[], None],
        *,
        poll_seconds: float = 1.0,
        debounce_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        fingerprint: Callable[[Path], str] = _source_fingerprint,
    ) -> None:
        self.package_dir = Path(package_dir)
        self.request_restart = request_restart
        self.poll_seconds = poll_seconds
        self.debounce_seconds = debounce_seconds
        self._clock = clock
        self._fingerprint = fingerprint
        self._baseline, self._baseline_manifest = self._read_sample(frozenset())
        self._pending: str | None = None
        self._pending_since = 0.0
        self.restart_requested = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _read_sample(
        self, required_manifest: frozenset[str]
    ) -> tuple[str, frozenset[str]]:
        """Fingerprint one stable tree containing every baseline source path."""
        before = _source_manifest(self.package_dir)
        missing = required_manifest - before
        if missing:
            names = ", ".join(sorted(missing))
            raise OSError(f"installed package source tree is incomplete; missing: {names}")
        fingerprint = self._fingerprint(self.package_dir)
        after = _source_manifest(self.package_dir)
        if before != after:
            raise OSError("installed package source manifest changed during its scan")
        return fingerprint, after

    def start(self) -> None:
        """Start periodic checks in one daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="comms-source-code-watcher",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop promptly and wait until no further poll can run."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.poll_once()
            if self.restart_requested:
                return

    def poll_once(self) -> None:
        """Inspect the package once and request a restart after it settles."""
        if self.restart_requested:
            return
        try:
            current, _manifest = self._read_sample(self._baseline_manifest)
        except OSError:
            # Installers can briefly move a source file between the directory
            # scan and read. That is neither a stable build nor a reason for
            # the one watcher thread to die; wait for a complete next scan.
            self._pending = None
            return
        if current == self._baseline:
            self._pending = None
            return
        now = self._clock()
        if current != self._pending:
            self._pending = current
            self._pending_since = now
            return
        if now - self._pending_since >= self.debounce_seconds:
            # Mark first. The callback stops the server and can take a moment;
            # no second poll may request a competing restart in that window.
            self.restart_requested = True
            self.request_restart()


def restart_current_process(
    *,
    argv: list[str] | None = None,
    executable: str | None = None,
    exec_process: Callable[[str, list[str]], object] = os.execv,
) -> None:
    """Reload this command in place when it is not owned by a supervisor.

    A launchd job should exit after the server closes: KeepAlive then creates a
    fresh process and a fresh PID. An interactive command must not rely on that
    supervisor, so it execs the same module with the same arguments instead.
    """
    if os.environ.get("XPC_SERVICE_NAME") == LAUNCHD_SERVICE_NAME:
        return
    current_argv = list(sys.argv if argv is None else argv)
    python = sys.executable if executable is None else executable
    exec_process(python, [python, "-m", "comms_graph", *current_argv[1:]])
