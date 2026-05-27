// Package state folds a JSONL event stream into the current state of the
// world: which claims are active, who's hello'd recently, what findings and
// notes have landed.
//
// The reducer is pure: given the same events in order it always returns the
// same state. There are no side effects, no file IO. Callers do `Read` from
// the log package and pipe the result through `Fold`.
package state

import (
	"sort"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/overlap"
)

// State is the materialized view of the event log.
type State struct {
	// Claims keyed by claim event ID. Only ACTIVE claims (not released, not
	// stolen) appear in this map. Inactive claims are dropped after the fold.
	Claims map[string]*Claim

	// Sessions keyed by actor name. Only the most recent hello per actor is kept.
	Sessions map[string]*Session

	// Findings and Notes in chronological order. Caller filters by `since`.
	Findings []*Finding
	Notes    []*Note
}

// Claim is an active exclusive claim on a scope.
type Claim struct {
	ID     string
	TS     time.Time
	Actor  string
	Scope  overlap.Scope
	Intent string

	// If non-empty, this claim displaced ForcedBy (an arbitrated steal).
	StolenFromID string
	StealReason  string
	Arbitrator   string
}

// Session is the most-recent hello per actor.
type Session struct {
	Actor    string
	TS       time.Time
	BaseName string
	Hostname string
	TTY      string
}

// Finding is a `comms find` event.
type Finding struct {
	ID       string
	TS       time.Time
	Actor    string
	Category string
	Summary  string
	Refs     []Ref
}

// Ref is a `--ref kind:value` pair attached to a finding.
type Ref struct {
	Kind  string
	Value string
}

// Note is a short FYI.
type Note struct {
	ID    string
	TS    time.Time
	Actor string
	Body  string
}

// Fold replays events in chronological order to produce the current state.
//
// Events MUST be sorted by ID (which is ULID — time-prefixed and monotonic).
// If they aren't, Fold sorts a copy first.
func Fold(events []event.Event) *State {
	// Sort defensively.
	sorted := make([]event.Event, len(events))
	copy(sorted, events)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].ID < sorted[j].ID })

	s := &State{
		Claims:   make(map[string]*Claim),
		Sessions: make(map[string]*Session),
	}

	for _, ev := range sorted {
		switch ev.Type {
		case event.TypeHello:
			s.Sessions[ev.Actor] = &Session{
				Actor:    ev.Actor,
				TS:       ev.TS,
				BaseName: stringOf(ev.Data, "base_name"),
				Hostname: stringOf(ev.Data, "hostname"),
				TTY:      stringOf(ev.Data, "tty"),
			}
		case event.TypeClaim:
			c, err := claimFromEvent(ev)
			if err != nil {
				// Drop malformed events silently — log corruption is exit-2 territory,
				// handled in event.Read.
				continue
			}
			// Atomic steal: if this claim references another, deactivate the prior one.
			if c.StolenFromID != "" {
				delete(s.Claims, c.StolenFromID)
			}
			s.Claims[c.ID] = c
		case event.TypeRelease:
			// data.refs may be a single string or a []string for backward compat;
			// we accept either.
			for _, ref := range refList(ev.Data, "refs") {
				delete(s.Claims, ref)
			}
		case event.TypeFinding:
			s.Findings = append(s.Findings, &Finding{
				ID:       ev.ID,
				TS:       ev.TS,
				Actor:    ev.Actor,
				Category: stringOf(ev.Data, "category"),
				Summary:  stringOf(ev.Data, "summary"),
				Refs:     parseRefs(ev.Data),
			})
		case event.TypeNote:
			s.Notes = append(s.Notes, &Note{
				ID:    ev.ID,
				TS:    ev.TS,
				Actor: ev.Actor,
				Body:  stringOf(ev.Data, "body"),
			})
		}
	}
	return s
}

func claimFromEvent(ev event.Event) (*Claim, error) {
	if len(ev.Scope) == 0 {
		return nil, errMalformed
	}
	sc, err := overlap.Parse(ev.Scope[0])
	if err != nil {
		return nil, err
	}
	return &Claim{
		ID:           ev.ID,
		TS:           ev.TS,
		Actor:        ev.Actor,
		Scope:        sc,
		Intent:       stringOf(ev.Data, "intent"),
		StolenFromID: stringOf(ev.Data, "steals"),
		StealReason:  stringOf(ev.Data, "steal_reason"),
		Arbitrator:   stringOf(ev.Data, "arbitrator"),
	}, nil
}

// ActiveClaimsByPath returns the subset of active claims whose path-glob
// could overlap the given path. Caller usually filters further by anchor.
func (s *State) ActiveClaimsByPath(path string) []*Claim {
	if s == nil {
		return nil
	}
	var out []*Claim
	for _, c := range s.Claims {
		if overlap.PathsOverlap(c.Scope.Path, path) {
			out = append(out, c)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TS.Before(out[j].TS) })
	return out
}

// ConflictsFor returns active claims that overlap the given scope AND are
// held by a different actor than `actor`. Empty actor means "any other".
func (s *State) ConflictsFor(scope overlap.Scope, actor string) []*Claim {
	if s == nil {
		return nil
	}
	var out []*Claim
	for _, c := range s.Claims {
		if c.Actor == actor {
			continue
		}
		if overlap.Scopes(c.Scope, scope) {
			out = append(out, c)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TS.Before(out[j].TS) })
	return out
}

// ClaimByID returns the active claim with the given ID, or nil. Supports
// partial-ID prefix matching: if exactly one active claim has ID with the
// given prefix, that claim is returned.
func (s *State) ClaimByID(id string) *Claim {
	if s == nil || id == "" {
		return nil
	}
	if c, ok := s.Claims[id]; ok {
		return c
	}
	var match *Claim
	for _, c := range s.Claims {
		if len(c.ID) >= len(id) && c.ID[:len(id)] == id {
			if match != nil {
				return nil // ambiguous prefix
			}
			match = c
		}
	}
	return match
}

// LatestClaimByActor returns the most-recently-opened active claim owned by
// the given actor, or nil if they hold none.
func (s *State) LatestClaimByActor(actor string) *Claim {
	if s == nil {
		return nil
	}
	var latest *Claim
	for _, c := range s.Claims {
		if c.Actor != actor {
			continue
		}
		if latest == nil || c.TS.After(latest.TS) {
			latest = c
		}
	}
	return latest
}

// ActiveClaimsByActor returns all active claims held by actor.
func (s *State) ActiveClaimsByActor(actor string) []*Claim {
	if s == nil {
		return nil
	}
	var out []*Claim
	for _, c := range s.Claims {
		if c.Actor == actor {
			out = append(out, c)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TS.Before(out[j].TS) })
	return out
}

// ---- helpers ----

func stringOf(m map[string]interface{}, key string) string {
	if m == nil {
		return ""
	}
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func refList(m map[string]interface{}, key string) []string {
	if m == nil {
		return nil
	}
	v, ok := m[key]
	if !ok {
		return nil
	}
	if s, ok := v.(string); ok {
		return []string{s}
	}
	if arr, ok := v.([]interface{}); ok {
		out := make([]string, 0, len(arr))
		for _, x := range arr {
			if s, ok := x.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

func parseRefs(m map[string]interface{}) []Ref {
	if m == nil {
		return nil
	}
	v, ok := m["refs"]
	if !ok {
		return nil
	}
	arr, ok := v.([]interface{})
	if !ok {
		return nil
	}
	out := make([]Ref, 0, len(arr))
	for _, x := range arr {
		obj, ok := x.(map[string]interface{})
		if !ok {
			continue
		}
		out = append(out, Ref{Kind: stringOfMap(obj, "kind"), Value: stringOfMap(obj, "value")})
	}
	return out
}

func stringOfMap(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// errMalformed is the sentinel for events that fail mid-fold. We treat them
// as drops because the bigger picture (log corruption → exit 2) is handled
// upstream in event.Read.
var errMalformed = stateErr("malformed event")

type stateErr string

func (e stateErr) Error() string { return string(e) }
