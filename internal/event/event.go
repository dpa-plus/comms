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

// NewID returns a fresh ULID (monotonic, time-prefixed, 26 chars).
//
// We use a fresh entropy source per call. This is fine for human-scale
// concurrency (a few processes per second); for higher rates we would
// switch to ulid.MonotonicEntropy with a shared mutex.
func NewID(now time.Time) string {
	return ulid.MustNew(ulid.Timestamp(now), rand.Reader).String()
}
