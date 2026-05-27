// Package lock wraps the POSIX advisory flock(2) syscall for use as the
// per-repo serialization primitive.
//
// The contract: every comms command that reads-and-writes the log first
// acquires an exclusive flock on `<logdir>/.lock` and holds it until the
// command exits. The kernel releases the lock automatically when the
// process's file descriptors close — including on `kill -9` — so we don't
// rely on Go defers for correctness.
package lock

import (
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

// Handle owns the lock file descriptor. Close releases the lock.
type Handle struct {
	f *os.File
}

// Acquire opens the file at path (creating it with mode 0600 if absent),
// then blocks waiting for an exclusive flock. It returns a Handle whose
// Close method releases the lock by closing the FD.
//
// NEVER spawn a child process while holding this lock that itself tries to
// acquire it — the child would inherit the FD via fork and deadlock.
func Acquire(path string) (*Handle, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("lock: open %s: %w", path, err)
	}
	// LOCK_EX = exclusive (writer) lock; blocks until acquired.
	if err := unix.Flock(int(f.Fd()), unix.LOCK_EX); err != nil {
		_ = f.Close()
		return nil, fmt.Errorf("lock: flock %s: %w", path, err)
	}
	return &Handle{f: f}, nil
}

// TryAcquire is the non-blocking version. Returns (nil, ErrLocked) if another
// process holds the lock.
func TryAcquire(path string) (*Handle, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("lock: open %s: %w", path, err)
	}
	if err := unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		_ = f.Close()
		if err == unix.EWOULDBLOCK {
			return nil, ErrLocked
		}
		return nil, fmt.Errorf("lock: flock %s: %w", path, err)
	}
	return &Handle{f: f}, nil
}

// Close releases the flock by closing the underlying FD. Safe to call
// multiple times.
func (h *Handle) Close() error {
	if h == nil || h.f == nil {
		return nil
	}
	// Closing the FD releases the flock automatically.
	err := h.f.Close()
	h.f = nil
	return err
}

// ErrLocked is returned by TryAcquire when the file is already locked.
var ErrLocked = errLockedSentinel{}

type errLockedSentinel struct{}

func (errLockedSentinel) Error() string { return "lock: already held by another process" }
