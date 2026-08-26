// Package event defines the on-disk event types for the comms log.
//
// Every event is a single line of canonical JSON in the JSONL log. The shape
// is intentionally narrow: ts + id + actor + type + optional scope + a
// type-specific `data` bag. The reducer in internal/state interprets the
// stream to compute active claims and recent activity.
package event

import (
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/oklog/ulid/v2"
)

// Type is the discriminator for an event. Kept small on purpose.
type Type string

const (
	TypeHello   Type = "hello"
	TypeClaim   Type = "claim"
	TypeRelease Type = "release"
	TypeNote    Type = "note"
	TypeFinding Type = "finding"

	// The task graph. A task is what should happen; an edge is the order it must
	// happen in and what the later task consumes from the earlier one; a state
	// event moves a task along its two steps: do it, then a DIFFERENT agent
	// verifies it.
	TypeTask      Type = "task"
	TypeTaskEdge  Type = "task_edge"
	TypeTaskState Type = "task_state"

	// TypeBlocked records a claim comms REFUSED because someone else held an
	// overlapping scope. It is the only moment that proves the tool did its job,
	// and until now it was the one moment nothing wrote down: the conflict went to
	// stderr and the process exited. That is why a store of 4,356 claims reported
	// zero collisions ever prevented: not because none were, but because a
	// prevented one left no trace.
	TypeBlocked Type = "blocked"
)

// Valid reports whether t is one of the known event types.
func (t Type) Valid() bool {
	switch t {
	case TypeHello, TypeClaim, TypeRelease, TypeNote, TypeFinding,
		TypeTask, TypeTaskEdge, TypeTaskState, TypeBlocked:
		return true
	}
	return false
}

// KnownTypes lists the types this build understands, comma separated.
//
// It exists so the `comms log --type` flag help and its validation error read
// from the same place as Valid. Before this there were two hand-maintained
// whitelists in two packages, and adding a type meant remembering both.
func KnownTypes() string {
	all := []Type{TypeHello, TypeClaim, TypeRelease, TypeNote, TypeFinding,
		TypeTask, TypeTaskEdge, TypeTaskState, TypeBlocked}
	names := make([]string, 0, len(all))
	for _, t := range all {
		names = append(names, string(t))
	}
	return strings.Join(names, ",")
}

// ErrUnknownType marks a decode failure whose only cause is a type this build
// does not recognise. The line is well formed; the binary is simply older than
// whatever wrote it.
//
// Readers skip such a line and keep going (see Read); WRITERS still refuse to
// emit one (see Encode). That asymmetry is the whole point: a binary must never
// author a type it cannot fold, but it must survive meeting one. Without it,
// adding a sixth event type would brick every older binary on the machine: a
// single unrecognised line made Read abort, so `status`, `log`, `claim`, `note`
// and the `check` pre-edit hook all failed on that repository at once.
//
// Ship the tolerant reader, let it reach every machine, and only then add the
// type.
var ErrUnknownType = errors.New("event: unknown type")

// Event is a single log entry.
//
// JSON shape:
//
//	{"ts":"2026-05-22T14:30:00Z","id":"01HZ...","actor":"claude-3a1f",
//	 "type":"claim","scope":["src/foo.ts#bar"],
//	 "data":{"intent":"fix N+1"}}
//
// Scope is optional (only set on claim/release events). Data carries
// type-specific fields and is otherwise opaque to the log reader.
type Event struct {
	TS    time.Time              `json:"ts"`
	ID    string                 `json:"id"`
	Actor string                 `json:"actor"`
	Type  Type                   `json:"type"`
	Scope []string               `json:"scope,omitempty"`
	Data  map[string]interface{} `json:"data,omitempty"`
}

// Encode marshals the event to a single line of canonical JSON, terminated
// with `\n`. The output is safe to append to a JSONL file.
func (e Event) Encode() ([]byte, error) {
	if e.ID == "" {
		return nil, fmt.Errorf("event: missing id")
	}
	if e.Actor == "" {
		return nil, fmt.Errorf("event: missing actor")
	}
	if !e.Type.Valid() {
		return nil, fmt.Errorf("event: invalid type %q", e.Type)
	}
	if e.TS.IsZero() {
		return nil, fmt.Errorf("event: missing ts")
	}
	// Force UTC + RFC3339 representation independent of the caller's location.
	clone := e
	clone.TS = e.TS.UTC()
	b, err := json.Marshal(clone)
	if err != nil {
		return nil, fmt.Errorf("event: marshal: %w", err)
	}
	b = append(b, '\n')
	return b, nil
}

// Decode parses one line of JSONL into an Event.
func Decode(line []byte) (Event, error) {
	var e Event
	if err := json.Unmarshal(line, &e); err != nil {
		return Event{}, fmt.Errorf("event: unmarshal: %w", err)
	}
	if e.ID == "" {
		return Event{}, fmt.Errorf("event: missing id")
	}
	if e.Actor == "" {
		return Event{}, fmt.Errorf("event: missing actor")
	}
	if !e.Type.Valid() {
		return Event{}, fmt.Errorf("%w %q", ErrUnknownType, e.Type)
	}
	if e.TS.IsZero() {
		return Event{}, fmt.Errorf("event: missing ts")
	}
	return e, nil
}

// entropy is a process-wide, mutex-guarded monotonic ULID entropy source.
//
// A MonotonicReader guarantees that two ULIDs minted in the SAME millisecond
// are strictly increasing. The state reducer (internal/state) orders events to
// reconstruct claims/sessions, and the documented invariant is that IDs are
// time-ordered AND monotonic. crypto/rand.Reader alone is NOT monotonic: two
// same-millisecond IDs share the 48-bit timestamp prefix but get independent
// random suffixes, so they sort in random order, which previously let
// same-millisecond events (e.g. a claim and its steal/release) replay out of
// causal order. LockedMonotonicReader is safe for concurrent use.
var entropy = &ulid.LockedMonotonicReader{MonotonicReader: ulid.Monotonic(rand.Reader, 0)}

// NewID returns a fresh, monotonic, time-prefixed ULID (26 chars).
//
// IDs minted within the same process in the same millisecond are guaranteed
// strictly increasing via the shared monotonic entropy source above. On the
// (astronomically unlikely) monotonic-overflow error: generating ~2^80 IDs in
// one millisecond: we fall back to a fresh non-monotonic suffix rather than
// panic. Correctness does not depend on the fallback being monotonic: the state
// reducer orders events by timestamp (see internal/state.Fold), not by ULID.
func NewID(now time.Time) string {
	if id, err := ulid.New(ulid.Timestamp(now), entropy); err == nil {
		return id.String()
	}
	return ulid.MustNew(ulid.Timestamp(now), rand.Reader).String()
}
