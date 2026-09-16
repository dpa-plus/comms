"""Refresh the long-running dashboard when its installed Python code changes."""

from __future__ import annotations

import csv
import hashlib
import os
import sys
import threading
import time
from pathlib import Path, PurePosixPath
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


def _recorded_source_manifest(
    package_dir: Path,
) -> tuple[Path, bytes, frozenset[str]] | None:
    """Read the wheel RECORD that authoritatively describes this package."""
    package_dir = Path(package_dir)
    records = tuple(
        sorted(package_dir.parent.glob(f"{package_dir.name}-*.dist-info/RECORD"))
    )
    if not records:
        return None
    if len(records) != 1:
        raise OSError("installed package has multiple active distribution records")

    record_path = records[0]
    record_bytes = record_path.read_bytes()
    try:
        rows = csv.reader(record_bytes.decode("utf-8").splitlines())
        sources = set()
        for row in rows:
            if not row:
                continue
            installed_path = PurePosixPath(row[0])
            parts = installed_path.parts
            if len(parts) < 2 or parts[0] != package_dir.name:
                continue
            relative = PurePosixPath(*parts[1:])
            if ".." not in relative.parts and relative.suffix == ".py":
                sources.add(relative.as_posix())
    except (csv.Error, UnicodeError) as exc:
        raise OSError(f"cannot read installed distribution record: {record_path}") from exc
    if not sources:
        raise OSError(f"installed distribution records no Python sources: {record_path}")
    return record_path, record_bytes, frozenset(sources)


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
        (
            self._baseline,
            self._baseline_manifest,
            self._record_required,
        ) = self._read_sample(frozenset(), require_record=False)
        self._pending: str | None = None
        self._pending_since = 0.0
        self.restart_requested = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _read_sample(
        self, required_manifest: frozenset[str], *, require_record: bool
    ) -> tuple[str, frozenset[str], bool]:
        """Fingerprint one tree that agrees with stable package metadata."""
        record_before = _recorded_source_manifest(self.package_dir)
        if require_record and record_before is None:
            raise OSError("installed package distribution record is absent")
        before = _source_manifest(self.package_dir)
        if record_before is None:
            missing = required_manifest - before
            if missing:
                names = ", ".join(sorted(missing))
                raise OSError(
                    f"installed package source tree is incomplete; missing: {names}"
                )
        else:
            recorded = record_before[2]
            if before != recorded:
                missing = recorded - before
                unrecorded = before - recorded
                details = []
                if missing:
                    details.append(f"missing: {', '.join(sorted(missing))}")
                if unrecorded:
                    details.append(f"unrecorded: {', '.join(sorted(unrecorded))}")
                raise OSError(
                    "installed package does not match its distribution record; "
                    + "; ".join(details)
                )
        fingerprint = self._fingerprint(self.package_dir)
        after = _source_manifest(self.package_dir)
        record_after = _recorded_source_manifest(self.package_dir)
        if before != after:
            raise OSError("installed package source manifest changed during its scan")
        if record_before is None:
            if record_after is not None:
                raise OSError("installed package distribution record changed during its scan")
        elif record_after is None or record_before[:2] != record_after[:2]:
            raise OSError("installed package distribution record changed during its scan")
        return fingerprint, after, record_before is not None

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
            current, _manifest, record_backed = self._read_sample(
                self._baseline_manifest,
                require_record=self._record_required,
            )
        except OSError:
            # Installers can briefly move a source file between the directory
            # scan and read. That is neither a stable build nor a reason for
            # the one watcher thread to die; wait for a complete next scan.
            self._pending = None
            return
        if record_backed:
            self._record_required = True
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
