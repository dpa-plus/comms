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
	"fmt"
	"time"

	"github.com/oklog/ulid/v2"
)

// Type is the discriminator for an event. Five total — kept small on purpose.
type Type string

const (
	TypeHello   Type = "hello"
	TypeClaim   Type = "claim"
	TypeRelease Type = "release"
	TypeNote    Type = "note"
	TypeFinding Type = "finding"
)

// Valid reports whether t is one of the five known event types.
func (t Type) Valid() bool {
	switch t {
	case TypeHello, TypeClaim, TypeRelease, TypeNote, TypeFinding:
		return true
	}
	return false
}

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
		return Event{}, fmt.Errorf("event: invalid type %q", e.Type)
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
// random suffixes, so they sort in random order — which previously let
// same-millisecond events (e.g. a claim and its steal/release) replay out of
// causal order. LockedMonotonicReader is safe for concurrent use.
var entropy = &ulid.LockedMonotonicReader{MonotonicReader: ulid.Monotonic(rand.Reader, 0)}

// NewID returns a fresh, monotonic, time-prefixed ULID (26 chars).
//
// IDs minted within the same process in the same millisecond are guaranteed
// strictly increasing via the shared monotonic entropy source above. On the
// (astronomically unlikely) monotonic-overflow error — generating ~2^80 IDs in
// one millisecond — we fall back to a fresh non-monotonic suffix rather than
// panic. Correctness does not depend on the fallback being monotonic: the state
// reducer orders events by timestamp (see internal/state.Fold), not by ULID.
func NewID(now time.Time) string {
	if id, err := ulid.New(ulid.Timestamp(now), entropy); err == nil {
		return id.String()
	}
	return ulid.MustNew(ulid.Timestamp(now), rand.Reader).String()
}
