"""Concurrency tests for comms: the ones that can only fail with real processes.

Everything here uses `subprocess`, never threads, and that is not a style
preference. `flock(2)` is keyed to the OPEN FILE DESCRIPTION, not to the
process and not to the thread: two threads in one process that each open the
lock file get two descriptions and therefore genuinely exclude each other,
while two threads sharing one fd never contend at all. Either way a threaded
"proof" describes a different kernel object than the one two agents on a
laptop actually share. Same for O_APPEND atomicity: it is a property of
concurrent write(2) calls against one inode from separate descriptions.

Every test starts its workers behind a real barrier (each child announces
readiness, then spins on a go-file) so that they are all inside the
interesting region at the same instant instead of being serialised by
interpreter startup, which on a cold cache is ~50ms: an eternity next to the
race windows under test.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from comms_graph import lock, log, scope, state

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    lock.fcntl is None, reason="POSIX advisory locks unavailable on this platform"
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_PRELUDE = """
import json, os, sys, time
from datetime import datetime, timezone
from comms_graph import lock, log, scope, state

IDX = int(sys.argv[1])
WORK = sys.argv[2]

def barrier():
    # Announce readiness, then spin (not sleep) on the go-file so that every
    # worker leaves this function within microseconds of every other one.
    ready = os.path.join(WORK, "ready", str(IDX))
    with open(ready, "w") as fh:
        fh.write("1")
    go = os.path.join(WORK, "go")
    deadline = time.monotonic() + 60
    while not os.path.exists(go):
        if time.monotonic() > deadline:
            raise SystemExit("barrier timeout")
    # Spin off the last few ms too: os.path.exists costs a syscall, so a plain
    # poll loop still staggers the wake-ups by the cost of one stat.
    start = float(open(go).read())
    while time.monotonic() < start:
        pass
"""


def _spawn(work: Path, script: str, n: int, *, hold_go: bool = False):
    """Start n workers, release them together, and return the Popen list.

    `hold_go` leaves the barrier closed so the caller can open it itself (used
    when the parent must first take the lock).
    """
    (work / "ready").mkdir(parents=True, exist_ok=True)
    src = work / "worker.py"
    src.write_text(_PRELUDE + textwrap.dedent(script), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"

    procs = [
        subprocess.Popen(
            [sys.executable, str(src), str(i), str(work)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
        )
        for i in range(n)
    ]
    if not hold_go:
        _open_barrier(work, procs)
    return procs


def _open_barrier(work: Path, procs, *, timeout: float = 60.0) -> None:
    ready = work / "ready"
    deadline = time.monotonic() + timeout
    while len(list(ready.iterdir())) < len(procs):
        for p in procs:
            if p.poll() not in (None, 0):
                out, err = p.communicate()
                raise AssertionError(f"worker died before the barrier:\n{out}\n{err}")
        if time.monotonic() > deadline:
            raise AssertionError("workers never reached the barrier")
        time.sleep(0.005)
    # A timestamp a hair in the future, so the release is a scheduled instant
    # rather than "whenever your stat() happens to land".
    #
    # Written via rename, not in place: create-then-write is itself a race, and
    # a worker that stats the go-file between the two sees an empty file. The
    # harness for a concurrency test has to be at least as careful as the code
    # it is testing, or its own flakiness gets blamed on the module.
    staging = work / "go.tmp"
    staging.write_text(str(time.monotonic() + 0.05), encoding="utf-8")
    os.replace(staging, work / "go")


def _collect(procs, *, timeout: float = 120.0) -> list[str]:
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=timeout)
        assert p.returncode == 0, f"worker failed ({p.returncode}):\n{out}\n{err}"
        outs.append(out)
    return outs


def _raw_lines(path: Path) -> list[str]:
    blob = path.read_bytes()
    assert blob.endswith(b"\n"), (
        "the log does not end in a newline: the last append was cut off, which "
        "means a write was not atomic"
    )
    return blob.decode("utf-8").split("\n")[:-1]


# ---------------------------------------------------------------------------
# 1. N processes appending at once
# ---------------------------------------------------------------------------

_APPEND_UNLOCKED = """
n = int(sys.argv[3]) if len(sys.argv) > 3 else 150
path = os.path.join(WORK, "log.jsonl")
ids = []
evs = []
for i in range(n):
    ev = log.Event(
        ts=datetime.now(timezone.utc),
        id=log.new_id(),
        actor="agent-%02d" % IDX,
        type=log.TYPE_NOTE,
        # ~1 KiB per line. The point is to be far larger than nothing and far
        # smaller than a page, so that if anything ever buffers these, several
        # events share a flush and the tear lands mid-object.
        data={"text": "x" * 900, "seq": i, "worker": IDX},
    )
    evs.append(ev)
    ids.append(ev.id)
barrier()
for ev in evs:
    # repair_torn_tail=False because this run is deliberately UNLOCKED, and
    # that repair is only sound under the lock: it ftruncates to a point
    # computed from a size another appender has already moved past, so it
    # cuts away complete lines nobody told it about. On Linux that silently
    # loses ~10% of this run; on macOS it does not reproduce. Production
    # always holds the lock. The property under test here is O_APPEND alone,
    # so the lock-dependent step has no business inside it.
    log.append(path, ev, repair_torn_tail=False)
print(json.dumps(ids))
"""


def test_concurrent_appends_lose_no_event_and_tear_no_line(tmp_path):
    """8 processes append 150 events each, at once, and all 1200 survive intact.

    WHAT BREAKS IF THIS FAILS: an agent runs `comms note` or `comms claim` at
    the same moment as another agent and one of two things happens: the event
    is silently missing (the agent believes it announced a claim that nobody
    can see), or worse, two writes interleave inside one JSON object and
    `read()` raises CorruptLogError from that byte onward. The log is the only
    copy of coordination state and it is append-only, so a torn line does not
    lose one event, it ends the readable log there and takes every good event
    written after it with it.

    The property under test is O_APPEND itself: the offset must be chosen by
    the kernel inside the same write, not by the writer beforehand. Run
    deliberately WITHOUT the lock, because that is the only way to test it: a
    locked version passes against an implementation with no atomicity at all.

    Verified discriminating rather than assumed: replacing append()'s O_APPEND
    write with `lseek(SEEK_END)` + `pwrite`: the obvious-looking rewrite:
    turns this run into 289 surviving lines out of 1200, with 61 torn ones.

    Note what this does NOT prove, so that nobody strengthens the wrong claim:
    append()'s docstring says a buffered `open(path, "a")` would tear a line by
    flushing mid-object, and it would not. CPython's BufferedWriter flushes
    whole records and never splits one across two write(2) calls, so buffered
    appends survive this test too. Raw os.write is still the right call: it is
    what makes the ftruncate rollback and the fsync meaningful, but the reason
    is durability and all-or-nothing batching, not tearing.
    """
    procs = _spawn(tmp_path, _APPEND_UNLOCKED, 8)
    expected: set[str] = set()
    for out in _collect(procs):
        expected.update(json.loads(out.strip().splitlines()[-1]))
    assert len(expected) == 8 * 150

    path = tmp_path / "log.jsonl"
    lines = _raw_lines(path)

    seen = []
    for num, line in enumerate(lines, 1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - the failure we hunt
            pytest.fail(f"torn line {num}: {exc}\n{line[:200]!r}...{line[-200:]!r}")
        # A torn line can still parse if the tear falls between two objects, so
        # check the shape too: every line must be one whole event, and the
        # padding must have survived at full length.
        assert len(obj["data"]["text"]) == 900, f"line {num} lost payload bytes"
        seen.append(obj["id"])

    assert len(seen) == len(set(seen)), "an event id appears twice: a write was replayed"
    assert set(seen) == expected, (
        f"lost {len(expected - set(seen))} events, invented {len(set(seen) - expected)}"
    )
    # And the module's own reader must agree, since that is what every command
    # actually calls.
    assert len(log.read(path)) == len(expected)


_APPEND_LOCKED = """
n = 120
path = os.path.join(WORK, "log.jsonl")
lockp = os.path.join(WORK, ".lock")
ids = []
barrier()
for i in range(n):
    ev = log.Event(
        ts=datetime.now(timezone.utc),
        id=log.new_id(),
        actor="agent-%02d" % IDX,
        type=log.TYPE_NOTE,
        data={"text": "y" * 400, "seq": i},
    )
    with lock.file_lock(lockp, timeout=90):
        log.append(path, ev)
    ids.append(ev.id)
print(json.dumps(ids))
"""


def test_lock_contention_starves_nobody_and_drops_nothing(tmp_path):
    """8 processes each take the lock 120 times under maximum contention.

    WHAT BREAKS IF THIS FAILS: the normal path. Every comms command is
    read-log / decide / append under the lock, so 960 lock cycles with all
    eight processes always waiting on each other is what a busy repo looks
    like. A failure here is one of three real outcomes: a worker exits with
    LockTimeoutError (an agent that could not do its work because it lost the
    poll race too many times in a row: starvation, which the poll-based
    acquire() cannot prove is impossible, only improbable), a released lock
    that stayed held (every later command hangs out its timeout), or an event
    that never reached the file. All 960 events must be present exactly once.
    """
    procs = _spawn(tmp_path, _APPEND_LOCKED, 8)
    expected: set[str] = set()
    for out in _collect(procs):
        expected.update(json.loads(out.strip().splitlines()[-1]))
    assert len(expected) == 8 * 120

    ids = [json.loads(l)["id"] for l in _raw_lines(tmp_path / "log.jsonl")]
    assert len(ids) == len(set(ids))
    assert set(ids) == expected

    # The lock must be free the moment the last holder exits: not after a
    # timeout, and not never.
    handle = lock.try_acquire(tmp_path / ".lock")
    assert handle is not None, "the lock is still held after every holder exited"
    handle.release()


_BATCH = """
per_batch = 220
path = os.path.join(WORK, "log.jsonl")
batch = []
for i in range(per_batch):
    batch.append(log.Event(
        ts=datetime.now(timezone.utc),
        id=log.new_id(),
        actor="agent-%02d" % IDX,
        type=log.TYPE_NOTE,
        # ~1.1 KiB x 220 ~= 250 KiB in ONE os.write, far past any stdio buffer
        # and past a page, which is where short writes and interleaving live.
        data={"text": "z" * 1000, "worker": IDX, "seq": i},
    ))
barrier()
# repair_torn_tail=False for the same reason as the single-append run above:
# this is deliberately unlocked, and that repair truncates from a stale size.
# A 250 KiB batch makes a short write likelier, so it loses even more here --
# 1003 of 1320 lines on Linux -- which would mask the interleaving bug this
# test exists to catch behind a much louder one.
log.append_batch(path, batch, repair_torn_tail=False)
print(json.dumps([e.id for e in batch]))
"""


def test_concurrent_large_batches_stay_whole_and_contiguous(tmp_path):
    """Six processes each append a ~250 KiB batch simultaneously.

    WHAT BREAKS IF THIS FAILS: `comms claim a b c` and any other multi-event
    command. append_batch documents itself as all-or-nothing on disk, and it
    implements that by encoding everything up front and issuing one write. Two
    distinct real failures show up here and nowhere else:

    * If the write comes back short, append_batch's retry loop issues a second
      write(2), which lands at whatever the end of file is by THEN, not where
      the first one stopped. Any batch big enough to be split that way can have
      a stranger's event wedged into the middle of a JSON object. A small batch
      never reaches the size where that is possible, which is why this one is
      ~250 KiB and not five events.
    * If a batch is interleaved but not torn, the log still parses, but the
      command was not atomic: half a claim set is visible to a reader that
      arrives mid-write, so an agent can act on a partially-applied claim.

    Contiguity is therefore asserted per batch, not just line validity.
    """
    procs = _spawn(tmp_path, _BATCH, 6)
    batches = [json.loads(out.strip().splitlines()[-1]) for out in _collect(procs)]

    lines = _raw_lines(tmp_path / "log.jsonl")
    parsed = []
    for num, line in enumerate(lines, 1):
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - the failure we hunt
            pytest.fail(f"torn line {num} in a concurrent batch append: {exc}")
        assert len(parsed[-1]["data"]["text"]) == 1000, f"line {num} lost payload bytes"

    assert len(parsed) == sum(len(b) for b in batches), "a batch was partly lost"

    order = [obj["id"] for obj in parsed]
    position = {ev_id: i for i, ev_id in enumerate(order)}
    assert len(position) == len(order), "an event id appears twice"
    for worker, ids in enumerate(batches):
        spots = sorted(position[i] for i in ids)
        assert spots == list(range(spots[0], spots[0] + len(ids))), (
            f"worker {worker}'s batch was interleaved with another writer's: "
            f"append_batch is not the single atomic write(2) it documents"
        )


# ---------------------------------------------------------------------------
# 2. The lock actually excludes another process
# ---------------------------------------------------------------------------

_HOLDER = """
lockp = os.path.join(WORK, ".lock")
h = lock.acquire(lockp, timeout=30)
open(os.path.join(WORK, "held"), "w").write("1")
# Hold until the parent says stop, NOT for a fixed number of seconds. A timed
# hold makes the test a race between the parent's assertions and a sleep timer,
# so a slow machine turns "the lock excluded me" into "the holder had already
# let go": a flake that reads like a real defect. The safety deadline only
# stops a stranded child from living forever.
stop = os.path.join(WORK, "stop")
deadline = time.monotonic() + 300
while not os.path.exists(stop):
    if time.monotonic() > deadline:
        raise SystemExit("holder: parent never released me")
    time.sleep(0.005)
h.release()
print("released")
"""


def _spawn_holder(tmp_path: Path):
    """Start a process that takes the lock and holds it until _stop_holder()."""
    (tmp_path / "ready").mkdir(parents=True, exist_ok=True)
    src = tmp_path / "holder.py"
    src.write_text(_PRELUDE + textwrap.dedent(_HOLDER), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, str(src), "0", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    held = tmp_path / "held"
    deadline = time.monotonic() + 60
    while not held.exists():
        assert proc.poll() is None, f"holder died: {proc.communicate()}"
        assert time.monotonic() < deadline, "holder never took the lock"
        time.sleep(0.005)
    return proc


def _stop_holder(tmp_path: Path, proc) -> None:
    (tmp_path / "stop").write_text("1", encoding="utf-8")
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, f"holder failed: {out}\n{err}"
    assert "released" in out


def test_a_held_lock_excludes_another_process(tmp_path):
    """While one process holds the lock, another cannot take it: at all.

    WHAT BREAKS IF THIS FAILS: everything the lock is for. The whole
    read-decide-append sequence is only safe if it is exclusive; if two agents
    can be inside it at once, both read a log without the other's claim and
    both decide the scope is free. This must be a separate PROCESS, because
    flock is per open file description: an in-process check can pass against
    an implementation that only ever excludes itself.

    Both shapes are asserted, because they fail differently: try_acquire must
    report "someone has it" as a return value (the polling loop calls it
    thousands of times and must not pay for a traceback), while acquire must
    give up with LockTimeoutError rather than hanging a CLI forever.
    """
    holder = _spawn_holder(tmp_path)
    lockp = tmp_path / ".lock"
    try:
        assert lock.try_acquire(lockp) is None, (
            "a second process took a lock that is already held: the flock is "
            "not exclusive"
        )
        started = time.monotonic()
        with pytest.raises(lock.LockTimeoutError):
            lock.acquire(lockp, timeout=0.4)
        waited = time.monotonic() - started
        # It must actually WAIT, not fail fast: an agent that gives up
        # instantly turns every ordinary overlap into a spurious error.
        assert 0.3 <= waited < 3.0, f"acquire() returned after {waited:.3f}s"

        # Repeated probes must keep reporting held for as long as it is held:
        # a lock that flickers free is worse than one that never frees.
        for _ in range(20):
            assert lock.try_acquire(lockp) is None
    finally:
        _stop_holder(tmp_path, holder)

    # ...and the instant the holder is gone it must be takeable.
    handle = lock.acquire(lockp, timeout=2.0)
    assert handle.held
    handle.release()
    assert not handle.held


def test_lock_is_not_reported_held_when_nobody_holds_it(tmp_path):
    """A free lock is takeable by process after process, forever.

    WHAT BREAKS IF THIS FAILS: comms wedges permanently. The lock file is
    never unlinked by design, so any state that survives a release: a
    leftover flock on a cached descriptor, a mode the next process cannot
    open, a stale sentinel written into the file: turns "nobody is working in
    this repo" into "every command times out". That failure is invisible in a
    single-process test, where the leftover belongs to the same process that
    is asking.

    Twelve sequential processes, each taking and releasing, is the shape of a
    developer running twelve commands in a row.
    """
    lockp = tmp_path / ".lock"
    script = """
lockp = os.path.join(WORK, ".lock")
h = lock.try_acquire(lockp)
if h is None:
    raise SystemExit("try_acquire said HELD on a free lock")
if not h.held:
    raise SystemExit("handle reports not-held immediately after acquiring")
h.release()
h.release()   # idempotent; must not close a stranger's fd
print("ok")
"""
    src = tmp_path / "seq.py"
    src.write_text(_PRELUDE + textwrap.dedent(script), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    for i in range(12):
        r = subprocess.run(
            [sys.executable, str(src), str(i), str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert r.returncode == 0, f"run {i}: {r.stdout}\n{r.stderr}"
        assert "ok" in r.stdout


def test_polling_a_held_lock_thousands_of_times_leaks_no_descriptor(tmp_path):
    """A process that loses the race 1500 times must still be able to work.

    WHAT BREAKS IF THIS FAILS: the failure disguises itself. try_acquire opens
    the lock file before it flocks, so a miss that forgets to close leaks one
    descriptor per attempt. acquire() polls at 25ms, so a two-minute wait is
    ~4800 attempts: past the descriptor limit, and the symptom is not "the
    lock is busy" but a bogus "cannot open lock file" error, or an unrelated
    open() failing somewhere else in the same process.

    The child lowers its own RLIMIT_NOFILE so the leak has to show up in
    seconds instead of only on a machine with a low ulimit.
    """
    holder = _spawn_holder(tmp_path)
    script = """
import resource
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(96, hard), hard))
lockp = os.path.join(WORK, ".lock")
misses = 0
for _ in range(1500):
    h = lock.try_acquire(lockp)
    if h is not None:
        h.release()
        raise SystemExit("took a lock that another process holds")
    misses += 1
# If descriptors leaked, this ordinary open is what fails, exactly as it would
# in real code that polls and then tries to read the log.
with open(os.path.join(WORK, "probe"), "w") as fh:
    fh.write("still able to open files")
print(misses)
"""
    src = tmp_path / "poller.py"
    src.write_text(_PRELUDE + textwrap.dedent(script), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        r = subprocess.run(
            [sys.executable, str(src), "0", str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert r.stdout.strip().splitlines()[-1] == "1500"
    finally:
        _stop_holder(tmp_path, holder)


# ---------------------------------------------------------------------------
# 3. A killed holder releases the lock
# ---------------------------------------------------------------------------


def test_sigkilled_holder_releases_the_lock(tmp_path):
    """SIGKILL the process holding the lock; the next process gets it at once.

    WHAT BREAKS IF THIS FAILS: one crashed or force-quit agent bricks the repo
    for every other agent, permanently, with no way out except deleting a file
    nobody documented. SIGKILL runs no `finally`, no atexit, no signal
    handler, so nothing in Python can clean up. The only reason this works is
    that flock ownership belongs to the open file description and the kernel
    drops it when the last descriptor closes at process teardown.

    That makes this test the guard on a specific tempting rewrite: replacing
    flock with a lock FILE whose existence (or whose recorded pid) means held.
    That version passes every single-process test in the suite and fails here,
    because a killed process leaves its file behind.
    """
    holder = _spawn_holder(tmp_path)
    lockp = tmp_path / ".lock"
    assert lock.try_acquire(lockp) is None, "sanity: the holder should hold it"

    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=30)
    assert holder.returncode == -signal.SIGKILL

    started = time.monotonic()
    handle = lock.acquire(lockp, timeout=5.0)
    elapsed = time.monotonic() - started
    try:
        assert handle.held
        # "Eventually" is not good enough: the next agent must not wait out a
        # timeout because the previous one crashed.
        assert elapsed < 1.0, f"took {elapsed:.3f}s to reclaim a lock nobody holds"
    finally:
        handle.release()

    # And the log the dead process was guarding must still be usable, not
    # merely unlocked.
    ev = log.Event(
        ts=datetime.now(timezone.utc),
        id=log.new_id(),
        actor="survivor",
        type=log.TYPE_NOTE,
        data={"text": "after the crash"},
    )
    with lock.file_lock(lockp, timeout=5.0):
        log.append(tmp_path / "log.jsonl", ev)
    assert [e.id for e in log.read(tmp_path / "log.jsonl")] == [ev.id]


def test_holder_killed_mid_batch_leaves_a_readable_log(tmp_path):
    """Kill a writer while it hammers the log; what is on disk still parses.

    WHAT BREAKS IF THIS FAILS: the recovery story. Agents get Ctrl-C'd and
    killed constantly, and the log is append-only truth: there is no repair
    pass. read() is documented to tolerate exactly one artefact of a killed
    writer, an unterminated final line, and nothing else. If a kill can leave a
    torn line anywhere but the very end, the log stops parsing at that point
    and every event after it is unreachable.
    """
    script = """
path = os.path.join(WORK, "log.jsonl")
lockp = os.path.join(WORK, ".lock")
open(os.path.join(WORK, "held"), "w").write("1")
while True:
    ev = log.Event(
        ts=datetime.now(timezone.utc),
        id=log.new_id(),
        actor="doomed",
        type=log.TYPE_NOTE,
        data={"text": "q" * 2000},
    )
    with lock.file_lock(lockp, timeout=30):
        log.append(path, ev)
"""
    src = tmp_path / "doomed.py"
    src.write_text(_PRELUDE + textwrap.dedent(script), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, str(src), "0", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    try:
        deadline = time.monotonic() + 30
        path = tmp_path / "log.jsonl"
        while not (path.exists() and path.stat().st_size > 50_000):
            assert proc.poll() is None, f"writer died early: {proc.communicate()}"
            assert time.monotonic() < deadline, "writer never got going"
            time.sleep(0.01)
    finally:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)

    events = log.read(path)  # must not raise CorruptLogError
    assert len(events) > 0
    blob = (tmp_path / "log.jsonl").read_bytes()
    # Whether or not the file ends in a newline, the last element of the split
    # is either empty or the truncated tail: either way it is not a whole line.
    complete = blob.split(b"\n")[:-1]
    for num, line in enumerate(complete, 1):
        json.loads(line)  # every terminated line is a whole object
    assert len(events) >= len(complete) - 1


# ---------------------------------------------------------------------------
# 4. Two agents racing for the same scope
# ---------------------------------------------------------------------------

_CLAIM_RACE = """
path = os.path.join(WORK, "log.jsonl")
lockp = os.path.join(WORK, ".lock")
me = "agent-%02d" % IDX
target = scope.parse("src/payments/charge.py")

barrier()
# The real comms claim sequence: take the lock, re-read the log from disk,
# fold it, look for a conflicting claim, and only then append.
with lock.file_lock(lockp, timeout=90):
    st = state.fold(log.read(path))
    conflicts = st.conflicts_for(target, me)
    if conflicts:
        print(json.dumps({"actor": me, "won": False,
                          "blocked_by": sorted(c.actor for c in conflicts)}))
    else:
        log.append(path, log.Event(
            ts=datetime.now(timezone.utc),
            id=log.new_id(),
            actor=me,
            type=log.TYPE_CLAIM,
            scope=["src/payments/charge.py"],
            data={"intent": "rewrite the charge path"},
        ))
        print(json.dumps({"actor": me, "won": True}))
"""


def test_exactly_one_of_ten_racing_agents_wins_the_same_scope(tmp_path):
    """Ten processes claim one file at the same instant. One wins; nine are told why.

    WHAT BREAKS IF THIS FAILS: two agents edit the same file believing they
    own it, and one of them loses their work at merge time, which is the
    single failure comms exists to prevent. Nothing short of real processes
    tests it: the check-then-append window is only unsafe across separate
    address spaces holding separate views of the log.

    Two assertions matter, and they fail for opposite reasons. Exactly one
    winner: more than one means the read-decide-append sequence was not
    exclusive. Exactly nine losers naming the winner: zero winners, or a loser
    blocked by somebody who never claimed, would mean the losers read a
    half-written or reordered log.
    """
    n = 10
    procs = _spawn(tmp_path, _CLAIM_RACE, n)
    results = [json.loads(out.strip().splitlines()[-1]) for out in _collect(procs)]

    winners = [r["actor"] for r in results if r["won"]]
    losers = [r for r in results if not r["won"]]
    assert len(winners) == 1, f"{len(winners)} agents all think they own the file: {winners}"
    assert len(losers) == n - 1
    for r in losers:
        assert r["blocked_by"] == winners, (
            f"{r['actor']} was blocked by {r['blocked_by']}, but the only claim "
            f"ever written was {winners[0]}'s"
        )

    # The log itself must agree with what the processes reported.
    st = state.fold(log.read(tmp_path / "log.jsonl"))
    active = st.conflicts_for(scope.parse("src/payments/charge.py"), "")
    assert [c.actor for c in active] == winners
    assert len(st.active_claims_by_actor(winners[0])) == 1


_OVERLAP_RACE = """
path = os.path.join(WORK, "log.jsonl")
lockp = os.path.join(WORK, ".lock")
me = "agent-%02d" % IDX
# Half the workers claim the whole file, half claim a symbol inside it. These
# are different STRINGS that name overlapping territory.
raw = "src/api/handler.py" if IDX % 2 == 0 else "src/api/handler.py#handle_request"
target = scope.parse(raw)

barrier()
with lock.file_lock(lockp, timeout=90):
    st = state.fold(log.read(path))
    conflicts = st.conflicts_for(target, me)
    if conflicts:
        print(json.dumps({"actor": me, "scope": raw, "won": False}))
    else:
        log.append(path, log.Event(
            ts=datetime.now(timezone.utc),
            id=log.new_id(),
            actor=me,
            type=log.TYPE_CLAIM,
            scope=[raw],
            data={"intent": "work"},
        ))
        print(json.dumps({"actor": me, "scope": raw, "won": True}))
"""


def test_racing_agents_lose_to_an_overlapping_claim_not_just_an_identical_one(tmp_path):
    """A whole-file claim and a symbol-inside-that-file claim cannot both win.

    WHAT BREAKS IF THIS FAILS: the more common half of the collision. Agents
    rarely type the same scope string; they type `src/api/handler.py` and
    `src/api/handler.py#handle_request`, which are different strings naming
    overlapping code. If the exclusion is string equality, or if the overlap
    check runs against a stale in-memory state rather than the log as re-read
    under the lock: both agents are told the coast is clear and both edit the
    same function.

    Racing them is what distinguishes this from a unit test of overlaps(): the
    question here is whether the winner's claim is DURABLE and VISIBLE to
    everyone who folds the log afterwards, not whether two Scope objects
    compare as overlapping in one process.
    """
    n = 8
    procs = _spawn(tmp_path, _OVERLAP_RACE, n)
    results = [json.loads(out.strip().splitlines()[-1]) for out in _collect(procs)]

    winners = [r for r in results if r["won"]]
    assert len(winners) == 1, (
        "an overlapping claim was allowed through: "
        f"{[(w['actor'], w['scope']) for w in winners]}"
    )
    assert len(results) == n

    st = state.fold(log.read(tmp_path / "log.jsonl"))
    assert len(st.claims) == 1
    claimed = next(iter(st.claims.values()))
    assert claimed.actor == winners[0]["actor"]
    # Everyone else must see themselves as blocked when they re-check now.
    for r in results:
        if r["won"]:
            continue
        assert st.conflicts_for(scope.parse(r["scope"]), r["actor"])
