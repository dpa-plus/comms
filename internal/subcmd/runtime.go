// Package subcmd contains one file per CLI subcommand plus this shared
// runtime helper that wires together repo discovery, bootstrapping, lock
// acquisition, and log reading.
//
// Every mutating command follows the same skeleton:
//
//	rt, err := Open(OpenOpts{Mutating: true})
//	if err != nil { return err }
//	defer rt.Close()
//	// ...read rt.State, append events via rt.Append...
//
// Read-only commands pass Mutating: false and don't acquire the flock.
package subcmd

import (
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/dpa-plus/comms/internal/actor"
	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/lock"
	"github.com/dpa-plus/comms/internal/paths"
	"github.com/dpa-plus/comms/internal/policy"
	"github.com/dpa-plus/comms/internal/repo"
	"github.com/dpa-plus/comms/internal/state"
)

// OpenOpts controls runtime initialization.
type OpenOpts struct {
	// Mutating: true acquires the per-repo flock and resolves actor in
	// mutating mode (generic names rejected). false leaves the lock unheld
	// and resolves actor read-only (empty COMMS_ACTOR is fine).
	Mutating bool

	// RepoIDOverride bypasses git discovery for tests and legacy callers.
	RepoIDOverride string

	// RepoRootOverride is an explicit repo path. It bypasses cwd discovery and
	// does not spawn git, which keeps comms usable from a safe directory when a
	// desktop app process has lost macOS TCC access to its cwd.
	RepoRootOverride string

	// SkipLock disables flock acquisition even when Mutating==true. Used
	// only by `comms check` to avoid blocking on a long-running command;
	// since check is read-only on the log it's safe.
	SkipLock bool

	// LockTimeout bounds how long Open waits for the flock when Mutating &&
	// !SkipLock. When >0, Open polls a non-blocking acquire with a short backoff
	// until the deadline and returns a timeout error if it expires, so a caller
	// (e.g. the local UI server handling concurrent HTTP requests) never has a
	// goroutine block forever behind a CLI that holds the lock. When ==0, Open
	// keeps the original unbounded blocking acquire (the CLI behavior).
	LockTimeout time.Duration
}

// Runtime is the resolved working context for a single command.
type Runtime struct {
	Actor    string
	Repo     repo.Identity
	Paths    paths.Paths
	Policy   *policy.Policy
	Events   []event.Event
	State    *state.State
	lockH    *lock.Handle
	noEvents bool
}

// Open resolves identity, bootstraps directories, optionally acquires the
// flock, and reads the current event log.
func Open(opts OpenOpts) (*Runtime, error) {
	mode := actor.ReadOnly
	if opts.Mutating {
		mode = actor.Mutating
	}
	a, err := actor.Resolve(mode)
	if err != nil {
		return nil, err
	}
	id, err := discoverRuntimeRepo(opts)
	if err != nil {
		return nil, err
	}
	p, err := paths.For(id.Root, id.Hash)
	if err != nil {
		return nil, err
	}
	if p.EphemeralStore() {
		fmt.Fprintf(os.Stderr,
			"comms: WARNING: store resolved under a throwaway temp dir:\n"+
				"         %s\n"+
				"       This almost always means HOME was overridden (e.g. HOME=/tmp). Events\n"+
				"       written here are invisible to `comms ui` and to other agents on the\n"+
				"       repo, so coordination silently breaks. Unset HOME; for protected-folder\n"+
				"       errors use `cd /tmp` plus --repo \"<abs repo>\" (or COMMS_REPO), which\n"+
				"       keep HOME intact.\n",
			p.LogDir)
	}
	// Bootstrap WRITES (creates the per-machine log dir + the committed .comms
	// tree, policy.txt, .gitignore). Read-only commands — status, log, and
	// especially `check`, which runs on every edit via the PreToolUse hook —
	// must not create files as a side effect of inspecting a repo. Only
	// bootstrap when we're actually going to mutate. Read paths tolerate
	// missing dirs: event.Read returns an empty log when the file is absent and
	// policy.Load treats a missing policy file as empty.
	if opts.Mutating {
		if err := repo.Bootstrap(p); err != nil {
			return nil, err
		}
	}
	rt := &Runtime{Actor: a, Repo: id, Paths: p}

	if opts.Mutating && !opts.SkipLock {
		h, err := acquireLock(p.Lock, opts.LockTimeout)
		if err != nil {
			return nil, err
		}
		rt.lockH = h
	}

	// Load policy (best effort — missing file is OK; malformed → exit 2).
	pol, err := policy.Load(p.Policy)
	if err != nil {
		_ = rt.Close()
		return nil, err
	}
	rt.Policy = pol

	events, err := event.Read(p.Log)
	if err != nil {
		_ = rt.Close()
		return nil, err
	}
	rt.Events = events
	rt.State = state.Fold(events)
	return rt, nil
}

func discoverRuntimeRepo(opts OpenOpts) (repo.Identity, error) {
	if opts.RepoRootOverride != "" {
		return repo.DiscoverExplicit(opts.RepoRootOverride)
	}
	if globalRepoRoot != "" {
		return repo.DiscoverExplicit(globalRepoRoot)
	}
	if envRepo := os.Getenv("COMMS_REPO"); envRepo != "" {
		return repo.DiscoverExplicit(envRepo)
	}
	if globalRepoID != "" {
		return repo.Identity{}, fmt.Errorf("--repo-id is no longer used for repo selection; use --repo /absolute/repo/path instead")
	}
	return repo.DiscoverFromCWD(opts.RepoIDOverride)
}

// lockBackoff is the poll interval used by the bounded acquire loop.
const lockBackoff = 25 * time.Millisecond

// ErrLockTimeout is wrapped by Open when a bounded LockTimeout expires while
// another process holds the per-repo flock. Callers (e.g. UI handlers) can test
// for it with errors.Is to map it to a "busy, try again" response instead of a
// generic failure.
var ErrLockTimeout = errors.New("acquire lock: timed out waiting for the per-repo lock")

// acquireLock obtains the per-repo flock. With timeout<=0 it keeps the original
// unbounded blocking acquire (CLI behavior). With timeout>0 it polls a
// non-blocking acquire on a short backoff until the deadline, returning a clear
// timeout error if the lock is still held — so no caller goroutine blocks
// forever behind another process that holds the lock.
func acquireLock(path string, timeout time.Duration) (*lock.Handle, error) {
	if timeout <= 0 {
		h, err := lock.Acquire(path)
		if err != nil {
			return nil, fmt.Errorf("acquire lock: %w", err)
		}
		return h, nil
	}
	deadline := time.Now().Add(timeout)
	for {
		h, ok, err := lock.TryAcquireOK(path)
		if err != nil {
			return nil, fmt.Errorf("acquire lock: %w", err)
		}
		if ok {
			return h, nil
		}
		if time.Now().Add(lockBackoff).After(deadline) {
			return nil, fmt.Errorf("%w after %s (another comms process holds %s)", ErrLockTimeout, timeout, path)
		}
		time.Sleep(lockBackoff)
	}
}

// Append writes an event to the log, then re-folds the state so the caller
// can immediately observe the new event. Caller MUST hold the flock
// (OpenOpts.Mutating must have been true).
func (r *Runtime) Append(ev event.Event) error {
	if r.lockH == nil {
		return fmt.Errorf("subcmd: Append called without holding flock")
	}
	if err := event.Append(r.Paths.Log, ev); err != nil {
		return err
	}
	r.Events = append(r.Events, ev)
	r.State = state.Fold(r.Events)
	return nil
}

// Close releases the flock if held. Always safe to call.
func (r *Runtime) Close() error {
	if r == nil {
		return nil
	}
	if r.lockH != nil {
		err := r.lockH.Close()
		r.lockH = nil
		return err
	}
	return nil
}

// Fatalf writes msg to stderr and exits with the given code. Convenience
// wrapper for the subcommands' main-package callers.
func Fatalf(code int, format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "comms: "+format+"\n", args...)
	os.Exit(code)
}
