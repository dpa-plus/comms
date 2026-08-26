"""The per-repo lock: the only thing standing between two agents and one file.

Every command that reads-then-appends the log holds this lock for the whole
cycle. What is tested here is not "does flock work" but the three things that
have actually gone wrong in this kind of code:

  * the lock not being exclusive across PROCESSES, which is the only place it
    matters: agents are separate processes, often on separate machines'
    checkouts of one repo;
  * a wait with no end, inside a library that is running in somebody else's
    process, where a hang is indistinguishable from a crash and gets blamed on
    whatever is holding the terminal;
  * a released handle whose fd NUMBER has been handed to somebody else's file,
    so a second release closes the log and silently drops a buffered claim.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from comms_graph import lock as clock

REPO = str(__import__("pathlib").Path(__file__).resolve().parents[2])

HOLDER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {repo!r})
    from comms_graph import lock
    h = lock.acquire(sys.argv[1], timeout=20)
    print("held", flush=True)
    sys.stdin.readline()
    h.release()
    print("released", flush=True)
    """
)


@pytest.fixture
def lockfile(tmp_path):
    return str(tmp_path / "store" / ".lock")


@pytest.fixture
def other_process(lockfile):
    """A second process that takes the lock and holds it until told to stop."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(repo=REPO), lockfile],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    if line.strip() != "held":
        proc.kill()
        pytest.fail(f"helper never took the lock: {line!r} {proc.stderr.read()!r}")
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def test_a_second_process_cannot_take_a_held_lock(lockfile, other_process):
    """IF THIS FAILS: two agents run the read-fold-append cycle at the same time.
    Both read a board without the other's claim, both decide their scope is free,
    and both append a claim on the same function: the exact collision the whole
    tool exists to prevent, and it happens silently."""
    assert clock.try_acquire(lockfile) is None


def test_the_wait_for_a_held_lock_always_ends(lockfile, other_process):
    """IF THIS FAILS: an agent that meets a wedged peer hangs forever inside a
    library call: in an editor hook or a watcher thread, where there is no
    prompt to interrupt and nothing that says which file it is waiting on. A
    bounded wait turns that into an error somebody can read."""
    started = time.monotonic()
    with pytest.raises(clock.LockTimeoutError) as exc:
        clock.acquire(lockfile, timeout=0.25)
    waited = time.monotonic() - started
    assert waited < 20, "the bounded wait did not bound anything"
    assert lockfile in str(exc.value), "the error must name the path it waited on"


def test_a_zero_timeout_still_tries_once(lockfile, other_process):
    """IF THIS FAILS: `timeout=0` means "fail without looking", so a caller that
    wanted a non-blocking attempt never acquires an uncontended lock."""
    with pytest.raises(clock.LockTimeoutError):
        clock.acquire(lockfile, timeout=0)
    # and the same call succeeds the moment nobody holds it
    other_process.stdin.write("\n")
    other_process.stdin.flush()
    other_process.wait(timeout=30)
    with clock.file_lock(lockfile, timeout=5):
        pass


def test_the_lock_is_handed_over_when_the_holder_finishes(lockfile, other_process):
    """IF THIS FAILS: the lock is never actually released and the second command
    in any repository waits out the full timeout and fails."""
    other_process.stdin.write("\n")
    other_process.stdin.flush()
    assert other_process.stdout.readline().strip() == "released"
    other_process.wait(timeout=30)
    handle = clock.try_acquire(lockfile)
    assert handle is not None
    handle.release()


def test_a_holder_killed_outright_does_not_wedge_the_repository(lockfile, other_process):
    """IF THIS FAILS: an agent killed by the OS (OOM, kill -9, a closed laptop)
    leaves the repository locked with no holder to blame and no stale-lock file
    to clean up: every later command in that repo fails until somebody finds the
    store by hand. The lock lives in the kernel precisely so this cannot happen."""
    other_process.kill()
    other_process.wait(timeout=30)
    handle = clock.try_acquire(lockfile)
    assert handle is not None, "a dead process is still holding the lock"
    handle.release()


# ---------------------------------------------------------------------------
# In-process contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1.0])
def test_an_unbounded_wait_cannot_be_asked_for(lockfile, bad):
    """IF THIS FAILS: a timeout that came from config (a JSON null coerced with
    float(), a "no limit" default of inf) silently reinstates the unbounded wait
    this module exists to remove, and nan is worse than inf, because it compares
    False against every deadline and polls until the process is killed."""
    with pytest.raises(ValueError):
        clock.acquire(lockfile, timeout=bad)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_a_broken_poll_interval_is_refused_too(lockfile, bad):
    """IF THIS FAILS: a zero backoff pins a core spinning on a syscall while a
    peer does its work, and a non-finite one either sleeps forever or reports a
    timeout it never waited out."""
    with pytest.raises(ValueError):
        clock.acquire(lockfile, timeout=1.0, poll_interval=bad)


def test_releasing_twice_does_not_close_a_strangers_file(tmp_path, lockfile):
    """IF THIS FAILS: the log loses a write and nothing says so.

    An fd number is an index the kernel reuses the instant it is free, so after a
    caller releases early, the very next open() in that process is likely to land
    on the same number. If a second release() then closes "the lock", it closes
    the LOG instead: the buffered claim line is gone and the only symptom is an
    EBADF somewhere else entirely. This test forces the reuse (dup2) so the trap
    is set deterministically rather than by luck."""
    handle = clock.acquire(lockfile, timeout=5)
    number = handle.fd
    handle.release()

    stranger_path = tmp_path / "log.jsonl"
    tmp = os.open(str(stranger_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if tmp != number:
        os.dup2(tmp, number)  # the stranger now sits on the freed lock fd number
        os.close(tmp)

    handle.release()  # the double release that used to close the stranger
    os.write(number, b"claim line\n")  # raises EBADF if the fd was closed
    os.close(number)
    assert stranger_path.read_text() == "claim line\n"


def test_a_released_handle_refuses_to_hand_back_its_number(lockfile):
    """IF THIS FAILS: code that passes `handle.fd` to a syscall after release
    operates on whatever file inherited that number, with no error to notice."""
    handle = clock.acquire(lockfile, timeout=5)
    assert handle.held
    handle.release()
    assert not handle.held
    with pytest.raises(clock.LockError):
        handle.fd


def test_a_raw_descriptor_is_not_accepted_where_a_handle_belongs():
    """IF THIS FAILS: callers written against the older API (which returned a
    bare fd) silently close an unrelated file instead of failing loudly."""
    with pytest.raises(TypeError):
        clock.release(3)


def test_an_exception_inside_the_block_still_releases(lockfile):
    """IF THIS FAILS: any command that errors mid-work leaves the repository
    locked, and every later command pays the full timeout before failing: the
    tool looks broken from the first mistake onwards."""
    with pytest.raises(RuntimeError):
        with clock.file_lock(lockfile, timeout=5):
            raise RuntimeError("boom")
    handle = clock.try_acquire(lockfile)
    assert handle is not None
    handle.release()


def test_releasing_early_inside_the_block_is_not_a_second_release(lockfile):
    """IF THIS FAILS: the documented "you may release early" pattern turns into
    the double-release bug above, in the one code path most likely to be doing
    something else with file descriptors."""
    with clock.file_lock(lockfile, timeout=5) as handle:
        handle.release()
    assert not handle.held


def test_the_lock_file_is_left_behind_on_purpose(lockfile):
    """IF THIS FAILS: unlinking the lock file allows TWO holders: a waiter stays
    blocked on an inode that has lost its name while a newcomer creates a fresh
    inode at the same path and locks that instead. Both then believe they hold
    the repository. One empty file left on disk forever is much cheaper."""
    with clock.file_lock(lockfile, timeout=5):
        pass
    assert os.path.exists(lockfile)
    assert os.stat(lockfile).st_mode & 0o077 == 0


def test_the_store_can_be_created_on_a_machine_that_has_never_run_comms(tmp_path):
    """IF THIS FAILS: the FIRST comms command on a machine wedges that machine
    permanently.

    The store is two levels deep (`<data home>/comms/<repo hash>/`), so a first
    run creates both. mkdir subtracts the umask exactly as open() does, so under
    a umask that strips the owner execute bit the intermediate `comms` directory
    lands at 0600, and nothing can then be created inside it. The repair this
    module does afterwards never runs, because the failure happens during the
    mkdir. Every later command takes the same path and fails the same way: the
    directory is already there, at a mode nothing can enter, and no comms command
    removes it. This is the "permanent wedge" the module's own docstrings say
    must not be reachable."""
    old = os.umask(0o177)
    try:
        p = str(tmp_path / "comms" / "a1b2c3d4e5f6" / ".lock")
        with clock.file_lock(p, timeout=5):
            pass
        parent = os.path.dirname(p)
        assert os.stat(parent).st_mode & 0o700 == 0o700
        with clock.file_lock(p, timeout=5):  # a later process must get in too
            pass
    finally:
        os.umask(old)


def test_nesting_the_lock_fails_loudly_instead_of_hanging(lockfile):
    """IF THIS FAILS: a self-deadlock: one command taking the lock twice, which
    is easy to do once acquiring is buried inside a helper: hangs the agent
    forever instead of raising something with a path and a stack in it."""
    with clock.file_lock(lockfile, timeout=5):
        with pytest.raises(clock.LockTimeoutError):
            clock.acquire(lockfile, timeout=0.25)


def test_a_deleted_lock_file_is_noticed_by_the_process_still_holding_it(tmp_path):
    """IF THIS FAILS: exclusion silently stops existing the moment anything
    unlinks the lock file, and nothing anywhere reports it.

    flock is held on an INODE, not on a name. Delete the file mid-hold and the
    next process finds nothing at that path, creates a fresh inode, and locks
    that: a different lock. Both processes are then inside the critical
    section. Measured before this check existed: one `rm` of the lock file, and
    ten concurrent agents were each told CLAIMED on the same scope, exit 0 every
    time, with the board listing ten holders of one file.

    The incumbent is the only party that can tell, because unlinking its file
    takes that inode's link count to zero and its own fd reports it. The
    newcomer sees a healthy file at a healthy path and has no way to know.
    """
    store = tmp_path / "store"
    store.mkdir()
    p = store / ".lock"
    with clock.file_lock(p) as handle:
        assert handle.compromised() == "", "an untouched lock must read as sound"
        os.unlink(p)
        reason = handle.compromised()
        assert reason, "a deleted lock file must not still read as exclusive"
        assert "deleted" in reason or "gone" in reason


def test_a_lock_file_replaced_underneath_us_is_noticed(tmp_path):
    """IF THIS FAILS: the narrower version of the same hole. Something removes
    the lock file AND another process recreates one at the same path while we
    still hold ours. Our inode survives with a link count of one, so the
    link-count test alone would call it healthy, while the file everyone else
    now contends on is a different inode entirely: we would be holding a lock
    nobody else consults and calling it exclusion."""
    store = tmp_path / "store"
    store.mkdir()
    p = store / ".lock"
    with clock.file_lock(p) as handle:
        keep = tmp_path / "kept"
        os.link(p, keep)          # keep our inode alive: st_nlink stays 1
        os.unlink(p)
        p.write_text("")          # a different inode now sits at the path
        reason = handle.compromised()
        assert reason, "a replaced lock file must not read as exclusive"
        assert "replaced" in reason
