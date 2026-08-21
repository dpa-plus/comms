"""Advisory file locking — the per-repo serialization primitive for the log.

Ported from comms `internal/lock/flock.go`.

The contract: every command that reads-and-appends the coordination log first
takes an exclusive flock on ``<logdir>/.lock`` and holds it for the whole
read-modify-append cycle. The lock lives in the kernel, not in a file we
maintain, so it is released even on ``kill -9`` — there is no stale-lock file
to garbage-collect and no PID liveness heuristic to get wrong.

Three deliberate departures from the Go, each load-bearing:

1. There is no unbounded blocking acquire. Go keeps ``lock.Acquire`` (plain
   ``LOCK_EX``) for the CLI, where hanging is at least visible to a human, and
   only its UI server passes a ``LockTimeout``. Here every caller is library
   code inside somebody else's process — a watcher thread, an editor hook — so
   a wait that never ends is indistinguishable from a hang and gets blamed on
   graphify. Every acquire is bounded and names the path it waited on.

2. The lock file is never unlinked. graphify's own rebuild lock
   (``graphify/watch.py``) deletes it on release, which allows two holders: a
   waiter can still be blocked on an inode that has lost its name while a
   newcomer creates a fresh inode at the same path and locks that instead.
   Leaving one empty file behind forever is much cheaper than that race.

3. Missing ``fcntl`` is a hard error. ``watch.py`` degrades to ``yield True``,
   a lock that was never taken — tolerable for a rebuild (you lose parallelism
   safety on a derived artifact), fatal here (concurrent writers silently
   interleave into the log, which is the one thing that must never be lost).
   graphify ships Windows installers; comms does not support Windows. Say so
   instead of inheriting that promise.

What the Go and this module agree on: an acquire hands back a *handle*, not the
raw descriptor underneath it. See :class:`LockHandle` for why that is not a
stylistic preference.
"""

from __future__ import annotations

import contextlib
import errno
import itertools
import math
import os
import stat
import threading
import time
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows / any non-POSIX build
    # Deferred, not raised: importing this module must not break unrelated
    # graphify commands on Windows. The error belongs at the acquire call,
    # where it can say what the caller actually cannot do.
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "LockError",
    "LockTimeoutError",
    "LockUnsupportedError",
    "LockHandle",
    "DEFAULT_TIMEOUT",
    "POLL_INTERVAL",
    "file_lock",
    "acquire",
    "try_acquire",
    "release",
]

# Matches the Go's lockBackoff. Short enough that a normal append (open, read,
# fold, write, fsync) is picked up almost immediately, long enough that a
# waiter costs ~40 syscalls/second instead of a pinned core.
POLL_INTERVAL = 0.025

# Generous relative to a real hold time (milliseconds), so a timeout means
# something is genuinely wrong — a peer wedged mid-append, or a caller that
# nested two acquires — rather than ordinary contention.
DEFAULT_TIMEOUT = 10.0

# The lock file carries no data — it is a name to hang an inode off — so the
# owner needs exactly rw and nobody else needs anything. The directory needs
# the execute bit on top, or nothing inside it can be opened.
_LOCK_MODE = 0o600
_DIR_MODE = 0o700

# Distinguishes the temporary names two threads in one process pick in the same
# microsecond; the pid alone does not.
_tmp_counter = itertools.count()


class LockError(Exception):
    """Base for every failure in this module."""


class LockTimeoutError(LockError):
    """The bounded wait expired while another process held the lock."""


class LockUnsupportedError(LockError):
    """No POSIX advisory locking on this platform, so no safe way to proceed."""


class LockHandle:
    """An owned, exclusively-held lock. Releasing twice is safe.

    WHY this is an object and not the ``int`` fd it wraps — this module used to
    return the bare fd, and that shape lost a write:

        with file_lock(p) as fd:
            release(fd)            # caller is done early
            log = open(logfile, "w")   # the kernel hands out the LOWEST free
                                       # fd number, which is the one we just
                                       # freed, so `log` IS that number now
            log.write(claim_line)      # buffered, not yet on disk
        # the contextmanager's finally-clause releases "the lock" a second time
        # -> closes the LOG -> the buffered claim line is gone and flush()
        #    raises EBADF

    An fd number is not an identity: it is an index that the kernel reuses the
    instant it is free. A handle is an identity, so a stale one can be detected
    (``held``) and a second release can be a no-op instead of a stranger's
    close(). The log is the one thing in comms that must never be lost, and
    this is the failure mode that loses it silently — the write vanishes and
    the only symptom is an EBADF somewhere else entirely.

    Mirrors the Go's ``*Handle``, whose ``Close`` is documented safe to call
    multiple times.
    """

    __slots__ = ("_fd", "_path", "_guard")

    def __init__(self, fd: int, path: str) -> None:
        self._fd = fd
        self._path = path
        # Idempotence has to hold against threads too, not just against a
        # second sequential call: without this, two threads can both read a
        # live _fd and both close it, which is the same stranger's-close bug
        # with a narrower window.
        self._guard = threading.Lock()

    @property
    def path(self) -> str:
        """The lock file this handle holds."""
        return self._path

    @property
    def held(self) -> bool:
        """True until the first release()."""
        return self._fd >= 0

    def compromised(self) -> str:
        """Why this lock no longer guarantees exclusion, or "" if it still does.

        THE HOLE THIS CLOSES. flock is held on an INODE, not on a name. If
        anything unlinks the lock file while we hold it, the next process to
        come along finds nothing at that path, creates a FRESH inode there, and
        flocks that instead — a different inode, so a different lock. Both
        processes are then inside the critical section, both read "no conflict",
        and both append. Measured: with one ``rm`` of the lock file at any point
        during a hold, ten concurrent agents were each told CLAIMED on the same
        scope, exit 0 every time, and the board listed ten holders of one file.
        Nothing warned. That is the exact outcome this tool exists to prevent.

        The removal does not have to be malicious or even unusual. A person who
        finds a tool wedged and deletes a file called ``.lock`` is doing the
        obvious thing, and stores routinely live under /tmp or /var/folders,
        which the OS reaps on a schedule — ``log.is_ephemeral_store`` exists
        because that is where they land.

        WHY THE CHECK IS THIS ONE, AND WHY IT BELONGS AT THE END. Comparing our
        fd's inode with the inode now at the path does not catch the newcomer:
        the newcomer's inode IS the file at the path, so it looks perfectly
        healthy from where it stands. The party that can always tell is the
        INCUMBENT, because unlinking its file drops that inode's link count to
        zero and ``fstat`` on the fd it already holds says so. Which means the
        check is worth nothing at acquire time — the damage happens during the
        hold — and must be made at the far end of the critical section, in the
        moment before the write that the exclusion was protecting.
        """
        fd = self._fd
        if fd < 0:
            return "the lock was already released"
        try:
            st = os.fstat(fd)
        except OSError as exc:
            return f"the lock file could not be checked ({exc.strerror})"
        if st.st_nlink == 0:
            return (
                "the lock file was deleted while it was held, so another process "
                "has since created a new one at the same path and is holding that "
                "instead — two writers, no exclusion"
            )
        try:
            now = os.stat(self._path)
        except FileNotFoundError:
            return "the lock file is gone from its path"
        except OSError as exc:
            return f"the lock path could not be checked ({exc.strerror})"
        if (now.st_ino, now.st_dev) != (st.st_ino, st.st_dev):
            return (
                "the lock file at this path was replaced while it was held, so "
                "the lock we hold is on an inode nobody else consults"
            )
        return ""

    @property
    def fd(self) -> int:
        """The underlying descriptor, for callers that must pass it to a syscall.

        Raises once released rather than returning the stale number: handing
        back an fd that now belongs to somebody else's file is precisely the
        bug this class exists to prevent.
        """
        fd = self._fd
        if fd < 0:
            raise LockError(
                f"lock: the handle for {self._path} was already released; its fd "
                f"number now belongs to whatever file the process opened next"
            )
        return fd

    def release(self) -> None:
        """Drop the lock and close the fd. Safe to call any number of times."""
        with self._guard:
            fd = self._fd
            if fd < 0:
                return
            # Cleared BEFORE the syscalls: if close() raises we must still never
            # touch this fd number again, and the handle must not be left in a
            # half-released state that a retry could double-close.
            self._fd = -1
            # close() alone would release the flock; the explicit LOCK_UN only
            # narrows the window if close() fails, and is harmless otherwise.
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            # Not suppressed: this handle is the sole owner of the fd, so an
            # EBADF here means something else closed it — a bug worth hearing
            # about, and the state above is already consistent, so raising
            # cannot strand the handle.
            os.close(fd)

    # Name parity with the Go's Handle.Close.
    close = release

    def __enter__(self) -> LockHandle:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def __repr__(self) -> str:
        state = f"fd={self._fd}" if self._fd >= 0 else "released"
        return f"<LockHandle {self._path!r} {state}>"


def _require_flock() -> None:
    if fcntl is None:
        raise LockUnsupportedError(
            "comms coordination needs POSIX advisory locks (fcntl.flock), which "
            "this platform does not provide. graphify itself runs on Windows; "
            "the comms log does not, because without a lock two agents would "
            "interleave writes into it undetected. Use WSL or a POSIX host."
        )


def _make_lock_dir(parent: str) -> None:
    """mkdir -p the store directory, at a mode the next process can enter.

    mkdir subtracts the umask from its mode argument exactly as open() does, so
    under a umask that strips the owner execute bit a plain makedirs() creates a
    directory nothing can descend into. chmod ignores the umask, so the repair is
    to fix what we just made — and only what we just made, since directories that
    already existed are somebody else's setup.

    ONE LEVEL AT A TIME, TOP DOWN. makedirs() creates the whole chain in one call
    and therefore fails partway: it makes ``<data home>/comms`` without the
    execute bit and then cannot descend into it to make ``<repo hash>``, so it
    raises EPERM having left the outer directory unusable and the store wedged
    for every later command. The first comms command on a fresh machine creates
    exactly those two levels, so this is not an edge case — it is every first run
    under a strict umask. Creating and repairing each level before moving into it
    is the only order that works.
    """
    missing: list[str] = []
    d = parent
    while d and not os.path.isdir(d):
        missing.append(d)
        up = os.path.dirname(d)
        if up == d:  # reached the filesystem root
            break
        d = up

    for created in reversed(missing):  # shallowest first
        try:
            os.mkdir(created, _DIR_MODE)
        except FileExistsError:
            pass  # another agent won the race; its mode is repaired below too
        except OSError as exc:
            raise LockError(f"lock: mkdir {created}: {exc}") from exc
        # Suppressed: a filesystem without POSIX modes has nothing to repair, and
        # losing the race is not a failure — 0o700 is what the winner made too.
        with contextlib.suppress(OSError):
            os.chmod(created, _DIR_MODE)

    if not os.path.isdir(parent):
        raise LockError(f"lock: mkdir {parent}: directory could not be created")


def _heal_lock_dir_mode(parent: str) -> bool:
    """Restore owner access to the directory we created for the lock file.

    WHY this exists alongside _heal_lock_mode: the lock file is not the only
    thing this module creates under the caller's umask. mkdir subtracts the
    umask exactly as open() does, so under umask 0o177 the store directory
    lands at 0o600 — readable, but with no execute bit, so nothing inside it
    can be opened at all and every acquire in that repo fails EACCES forever.
    That is the same permanent wedge one level up, and it is the case the old
    comment in this module pointed at while "proving" it with a umask under
    which the *file* mode was already correct.

    Only the owner bits are widened, and only when they are missing; group and
    other are left exactly as found, because those may be somebody's deliberate
    policy while a store directory its owner cannot enter never is.
    """
    if not parent:
        return False
    try:
        st = os.lstat(parent)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    mode = stat.S_IMODE(st.st_mode)
    if mode & _DIR_MODE == _DIR_MODE:
        return False
    try:
        os.chmod(parent, mode | _DIR_MODE)
    except OSError:
        return False
    return True


def _heal_lock_mode(p: str) -> bool:
    """Repair a lock file whose mode denies us the open. True if worth retrying.

    WHY: a lock file created under a umask that strips owner bits (0o600 leaves
    mode 0000, 0o277 leaves 0400) is unopenable by the *next* process, and the
    lock file is deliberately never unlinked — so without this, one unlucky
    creation wedges every future command in the repo with EACCES and nothing
    ever undoes it. _create_lock_file() no longer produces such a file, but
    files left behind by an older version, or by a process killed inside the
    old create/fchmod window, still exist on disk and must self-heal.

    Narrow on purpose. It only touches an empty regular file, only when exactly
    the owner rw bits are missing, and the opens above pass O_NOFOLLOW so a
    symlink at the lock path is an error rather than something we chase. An
    EACCES for any other reason (an ACL, a file owned by another user) is a real
    permission problem the caller should see, not something to paper over by
    widening modes.
    """
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size != 0:
        return False
    if stat.S_IMODE(st.st_mode) & _LOCK_MODE == _LOCK_MODE:
        # Permissions are already fine, so the EACCES is about something else
        # and a chmod would only hide it.
        return False
    try:
        # Widen, never narrow: an owner-less mode is always breakage, but a
        # group bit someone set on purpose (two accounts sharing one checkout)
        # is not ours to drop while repairing something else.
        os.chmod(p, stat.S_IMODE(st.st_mode) | _LOCK_MODE)
    except OSError:
        return False
    return True


def _heal_wedged_path(p: str, parent: str) -> bool:
    """Try to undo a mode that makes the lock file unopenable. True if retrying is worth it.

    Directory first: while the store directory has no execute bit, the lock
    file inside it cannot even be stat-ed, so the file-level check would report
    "nothing to heal" for a file that is also wedged.
    """
    healed_dir = _heal_lock_dir_mode(parent)
    healed_file = _heal_lock_mode(p)
    return healed_dir or healed_file


# EPERM is what Linux reports for "the filesystem does not do hard links".
_NO_HARDLINK_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("EPERM", "ENOSYS", "EOPNOTSUPP", "ENOTSUP", "EXDEV", "EMLINK")
    if hasattr(errno, name)
)


def _create_lock_file(p: str, flags: int) -> int | None:
    """Publish a new lock file at 0600. Returns the fd, or None if we lost the race.

    WHY the temp-file-and-link dance instead of O_CREAT|O_EXCL then fchmod:
    open() subtracts the umask from the mode it is given, so under a strict
    umask the file is *visible at the wrong mode* for the whole window between
    open() and fchmod(). Another process opening in that window gets EACCES,
    and a process killed in that window leaves the file wedged forever (see
    _heal_lock_mode). Fixing the mode while the inode still has only a
    throwaway name, and then giving it its real name with link() — which is
    atomic and refuses to overwrite — means no other process ever observes the
    lock file at a mode it cannot open.
    """
    tmp = f"{p}.{os.getpid()}.{next(_tmp_counter)}.new"
    try:
        fd = os.open(tmp, flags | os.O_CREAT | os.O_EXCL, _LOCK_MODE)
    except OSError as exc:
        raise LockError(f"lock: create {tmp}: {exc}") from exc

    try:
        # fchmod ignores the umask, and here it runs before the inode is
        # reachable under the name anyone else looks for.
        with contextlib.suppress(OSError):  # filesystems without POSIX modes
            os.fchmod(fd, _LOCK_MODE)
        try:
            os.link(tmp, p)
        except FileExistsError:
            # Somebody else published first. Theirs is as good as ours — and it
            # must be theirs, because the flock is on the inode, and two
            # processes locking two different inodes both "hold the lock".
            os.close(fd)
            return None
        except OSError as exc:
            if exc.errno not in _NO_HARDLINK_ERRNOS:
                os.close(fd)
                raise LockError(f"lock: link {tmp} -> {p}: {exc}") from exc
            # No hard links on this filesystem (some network and FAT mounts).
            # Fall back to the plain create; the mode window is back, but
            # _heal_lock_mode covers what falls into it.
            os.close(fd)
            return _create_lock_file_unatomic(p, flags)
        return fd
    finally:
        # The real name is a second link to the same inode, so dropping the
        # throwaway one leaves exactly the file we wanted and no litter if we
        # bailed out above.
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _create_lock_file_unatomic(p: str, flags: int) -> int | None:
    """Last-resort create for filesystems without hard links."""
    try:
        fd = os.open(p, flags | os.O_CREAT | os.O_EXCL, _LOCK_MODE)
    except FileExistsError:
        return None
    except OSError as exc:
        raise LockError(f"lock: create {p}: {exc}") from exc
    with contextlib.suppress(OSError):
        os.fchmod(fd, _LOCK_MODE)
    return fd


def _open_lock_file(path: str | os.PathLike[str]) -> int:
    """Open (creating if absent) the lock file and return its fd."""
    p = os.fspath(path)
    parent = os.path.dirname(p)
    if parent:
        # The store directory legitimately does not exist before the first
        # claim, and the lock is what guards its creation, so it cannot be
        # somebody else's job to have made it first.
        _make_lock_dir(parent)

    # O_CLOEXEC so the fd does not survive into a child process. flock
    # ownership follows the open file description, not the process: an
    # inherited fd would keep the lock held after we exit, and a long-lived
    # child (a spawned watcher) would wedge the repo for everyone with no
    # holder left to blame. Python sets close-on-exec by default since PEP 446,
    # but this is load-bearing enough to state rather than inherit.
    #
    # O_NOFOLLOW so the lock path is a file we can reason about: it is what
    # lets _heal_lock_mode chmod by path without racing a symlink swap.
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    # Bounded, not a loop: each benign race can only flip the state once from
    # our side — the file appeared between our open and our create, or it was
    # wedged and we just healed it. Anything still failing on the second pass
    # is a real problem and must surface as an error rather than spin here.
    for attempt in (1, 2):
        try:
            fd: int | None = os.open(p, flags)
        except FileNotFoundError:
            # None means somebody else published the file first; go round and
            # open theirs, because the flock lives on the inode and locking a
            # different one would let both of us "hold the lock".
            fd = _create_lock_file(p, flags)
        except PermissionError as exc:
            # EACCES here is not always the caller's problem to solve: it is
            # also what a lock file (or store directory) left at a mode nothing
            # can open looks like, and that state never clears itself because
            # the lock file is deliberately never unlinked.
            if attempt == 1 and _heal_wedged_path(p, parent):
                continue
            raise LockError(f"lock: open {p}: {exc}") from exc
        except OSError as exc:
            raise LockError(f"lock: open {p}: {exc}") from exc
        if fd is None:
            continue
        os.set_inheritable(fd, False)
        return fd

    raise LockError(
        f"lock: open {p}: the lock file kept appearing and vanishing under us; "
        f"something else is creating and deleting it (the comms lock file is "
        f"never meant to be removed)"
    )


def try_acquire(path: str | os.PathLike[str]) -> LockHandle | None:
    """One non-blocking attempt. Returns a handle, or None if someone holds it.

    Port of the Go's TryAcquireOK: "already held" is a return value, not an
    exception, because the bounded loop below hits it on every iteration and
    should not pay for a traceback each time.
    """
    _require_flock()
    p = os.fspath(path)
    fd = _open_lock_file(p)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # The fd MUST close here. A caller polls this thousands of times while
        # waiting; leaking one fd per attempt exhausts the process limit and
        # then reports itself as a bogus "cannot open lock file".
        os.close(fd)
        return None
    except OSError as exc:
        os.close(fd)
        raise LockError(f"lock: flock {p}: {exc}") from exc
    return LockHandle(fd, p)


def release(handle: LockHandle) -> None:
    """Release the lock and close the fd. Safe to call more than once.

    Identical to ``handle.release()``; kept as a function so acquire/release
    still read as a pair.
    """
    if not isinstance(handle, LockHandle):
        raise TypeError(
            "release() takes the LockHandle returned by acquire()/try_acquire(), "
            f"not {type(handle).__name__}. This module used to hand back a raw fd, "
            "and a second release() then closed whatever unrelated file had "
            "inherited that fd number — silently losing its buffered writes. "
            "Pass the handle (or just call handle.release())."
        )
    handle.release()


def acquire(
    path: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
) -> LockHandle:
    """Take the exclusive lock within `timeout` seconds, or raise.

    Returns a handle, which the caller owns and must release(). Prefer
    file_lock() unless the lock has to outlive a `with` block.
    """
    # `timeout < 0` is NOT sufficient validation, and this is why the check is
    # written against isfinite instead: NaN compares False against everything,
    # so it slips past `< 0` and then past `now + poll > deadline` on every
    # iteration — the loop polls until the process is killed. +inf passes the
    # `< 0` check honestly and does the same. Both are reachable from a
    # config-derived value (a JSON `null` coerced with float(), a missing key
    # defaulted to inf to mean "no limit"), and an unbounded wait is the exact
    # failure this module exists to prevent, so it must be a loud error.
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(
            f"timeout must be a finite, non-negative number of seconds, got {timeout!r}; "
            "an unbounded wait is not offered on purpose — inf and nan do not mean "
            "'wait forever', they mean 'hang with no error to point at'"
        )
    # Same shape of hole: nan slips past `<= 0` and reaches time.sleep(), and
    # inf turns the deadline check into an instant timeout that misreports how
    # long it waited.
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError(
            f"poll_interval must be a finite number > 0, got {poll_interval!r}; "
            "a zero backoff is a spin loop, and a non-finite one either sleeps "
            "forever or reports a timeout it never actually waited out"
        )

    # Monotonic, not wall clock: an NTP correction or a DST jump must not turn
    # a ten-second wait into an hour or into an instant failure.
    deadline = time.monotonic() + timeout
    p = os.fspath(path)
    while True:
        handle = try_acquire(p)
        if handle is not None:
            return handle
        # Checked after an attempt, so timeout=0 still means "try exactly once"
        # rather than "fail without looking".
        if time.monotonic() + poll_interval > deadline:
            raise LockTimeoutError(
                f"lock: timed out after {timeout:g}s waiting for {p} "
                f"(another process holds it)"
            )
        time.sleep(poll_interval)


@contextlib.contextmanager
def file_lock(
    path: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
) -> Iterator[LockHandle]:
    """Hold the exclusive lock for the duration of the block.

    Yields the handle only when the lock is genuinely held — on any failure this
    raises instead of yielding, so a `with` body never runs unprotected. A body
    that releases early is fine: the release below is idempotent.

    Not reentrant, and cannot be: flock is keyed to the open file description,
    so a nested call opens a second fd and contends with the outer one. That
    self-deadlock surfaces as LockTimeoutError instead of hanging forever,
    which is the whole reason the wait is bounded.
    """
    handle = acquire(path, timeout=timeout, poll_interval=poll_interval)
    try:
        yield handle
    finally:
        # finally, not a plain trailing call: an exception in the body must
        # still release, or the next command in this repo waits out the full
        # timeout for a lock nobody is using.
        handle.release()
