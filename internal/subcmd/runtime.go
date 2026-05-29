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
	"fmt"
	"os"

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

	// RepoIDOverride bypasses git discovery. Used by tests and the
	// `--repo-id` flag.
	RepoIDOverride string

	// RepoRootOverride is an explicit repo path. It bypasses cwd discovery and
	// does not spawn git, which keeps comms usable from a safe directory when a
	// desktop app process has lost macOS TCC access to its cwd.
	RepoRootOverride string

	// SkipLock disables flock acquisition even when Mutating==true. Used
	// only by `comms check` to avoid blocking on a long-running command;
	// since check is read-only on the log it's safe.
	SkipLock bool
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
	// Bootstrap is idempotent + cheap; safe to run unconditionally.
	if err := repo.Bootstrap(p); err != nil {
		return nil, err
	}
	rt := &Runtime{Actor: a, Repo: id, Paths: p}

	if opts.Mutating && !opts.SkipLock {
		h, err := lock.Acquire(p.Lock)
		if err != nil {
			return nil, fmt.Errorf("acquire lock: %w", err)
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
