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

	// EndedCommsSessions are archive boundaries. Legacy entries represent all
	// events since the previous global comms-session end marker. Named session
	// entries represent only events stamped with that named session ID.
	EndedCommsSessions []*EndedCommsSession

	// Findings and Notes in chronological order. Caller filters by `since`.
	Findings []*Finding
	Notes    []*Note

	// Releases of actual claims (refs present), in chronological order — the
	// "recently completed work" trail. Session-end/retire/leader releases are
	// excluded. Caller filters by `since`.
	Releases []*Release
}

// Claim is an active exclusive claim on a scope.
type Claim struct {
	ID          string
	TS          time.Time
	Actor       string
	Scope       overlap.Scope
	Intent      string
	SessionID   string
	SessionName string

	// If non-empty, this claim displaced ForcedBy (an arbitrated steal).
	StolenFromID string
	StealReason  string
	Arbitrator   string
}

// Session is the most-recent hello per actor.
type Session struct {
	Actor    string
	Label    string
	TS       time.Time
	BaseName string
	Hostname string
	TTY      string
	Leader   bool
	// LastSeen is the timestamp of this actor's most-recent event of ANY type
	// (hello/claim/release/finding/note), not just its hello. It is the actor's
	// passive heartbeat: every command it runs proves it is alive, so liveness
	// can be judged from real activity instead of a one-shot hello. Always >=
	// TS. Derived in Fold; consumers should fall back to TS if it is zero.
	LastSeen    time.Time
	SessionID   string
	SessionName string
}

// EndedCommsSession is an archived project-level coordination window.
type EndedCommsSession struct {
	ID           string
	SessionID    string
	Name         string
	StartedAt    time.Time
	EndedAt      time.Time
	EndedBy      string
	Reason       string
	Actors       []string
	ReleasedRefs []string
	EventCount   int
	ClaimCount   int
	FindingCount int
	NoteCount    int
}

// Finding is a `comms find` event.
type Finding struct {
	ID          string
	TS          time.Time
	Actor       string
	Category    string
	Summary     string
	Priority    bool
	Refs        []Ref
	SessionID   string
	SessionName string
}

// Ref is a `--ref kind:value` pair attached to a finding.
type Ref struct {
	Kind  string
	Value string
}

// Note is a short FYI.
type Note struct {
	ID          string
	TS          time.Time
	Actor       string
	Body        string
	Priority    bool
	SessionID   string
	SessionName string
}

// Release records a completed claim release: the outcome string plus the scopes
// that were freed, so the UI can show a "recently completed" feed.
type Release struct {
	ID          string
	TS          time.Time
	Actor       string
	Result      string
	Scopes      []string
	SessionID   string
	SessionName string
}

// Fold replays events in chronological order to produce the current state.
//
// Ordering policy: events are replayed in timestamp order, and events that
// share a timestamp keep their original (append) order via a STABLE sort. The
// log is append-only under a per-repo flock, so the input order is already the
// causal order; sorting by timestamp only re-seats anything written out of
// wall-clock order while preserving append order for ties. We deliberately do
// NOT order by ULID string: same-millisecond ULIDs are not guaranteed to sort
// in causal order, which would silently reorder a claim vs. its steal/release.
func Fold(events []event.Event) *State {
	sorted := make([]event.Event, len(events))
	copy(sorted, events)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].TS.Before(sorted[j].TS) })

	s := &State{
		Claims:   make(map[string]*Claim),
		Sessions: make(map[string]*Session),
	}

	var windowStart time.Time
	windowActors := map[string]bool{}
	// lastSeen[actor] = TS of that actor's most-recent event of ANY type. Because
	// `sorted` is in ascending TS order, the final write per actor is the max —
	// the actor's passive heartbeat. Assigned onto each Session after the fold.
	lastSeen := map[string]time.Time{}
	windowEvents, windowClaims, windowFindings, windowNotes := 0, 0, 0, 0
	type sessionWindow struct {
		start    time.Time
		actors   map[string]bool
		events   int
		claims   int
		findings int
		notes    int
	}
	namedWindows := map[string]*sessionWindow{}

	for _, ev := range sorted {
		if windowStart.IsZero() {
			windowStart = ev.TS
		}
		windowEvents++
		windowActors[ev.Actor] = true
		if ev.Actor != "" {
			lastSeen[ev.Actor] = ev.TS // ascending TS → last write is the max
		}
		switch ev.Type {
		case event.TypeClaim:
			windowClaims++
		case event.TypeFinding:
			windowFindings++
		case event.TypeNote:
			windowNotes++
		}
		sessionID := stringOf(ev.Data, "comms_session_id")
		var named *sessionWindow
		if sessionID != "" {
			named = namedWindows[sessionID]
			if named == nil {
				named = &sessionWindow{start: ev.TS, actors: map[string]bool{}}
				namedWindows[sessionID] = named
			}
			named.events++
			named.actors[ev.Actor] = true
			switch ev.Type {
			case event.TypeClaim:
				named.claims++
			case event.TypeFinding:
				named.findings++
			case event.TypeNote:
				named.notes++
			}
		}

		switch ev.Type {
		case event.TypeHello:
			s.Sessions[ev.Actor] = &Session{
				Actor:       ev.Actor,
				Label:       stringOf(ev.Data, "label"),
				TS:          ev.TS,
				BaseName:    stringOf(ev.Data, "base_name"),
				Hostname:    stringOf(ev.Data, "hostname"),
				TTY:         stringOf(ev.Data, "tty"),
				Leader:      boolOf(ev.Data, "leader"),
				SessionID:   stringOf(ev.Data, "comms_session_id"),
				SessionName: stringOf(ev.Data, "comms_session_name"),
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
			refs := refList(ev.Data, "refs")
			var releasedScopes []string
			for _, ref := range refs {
				if c, ok := s.Claims[ref]; ok {
					releasedScopes = append(releasedScopes, c.Scope.String())
				}
				delete(s.Claims, ref)
			}
			// An actual claim release (refs present) is a completed unit of work;
			// record it for the "recently completed" feed. Session lifecycle
			// releases — retire, leader-transfer, comms-session-end — ALSO carry
			// refs (the claims they sweep), but they are coordination admin, not
			// finished work, so exclude them from the feed.
			isHousekeeping := boolOf(ev.Data, "session_retire") ||
				boolOf(ev.Data, "leader_transfer") ||
				boolOf(ev.Data, "comms_session_end")
			if len(refs) > 0 && !isHousekeeping {
				s.Releases = append(s.Releases, &Release{
					ID:          ev.ID,
					TS:          ev.TS,
					Actor:       ev.Actor,
					Result:      stringOf(ev.Data, "result"),
					Scopes:      releasedScopes,
					SessionID:   stringOf(ev.Data, "comms_session_id"),
					SessionName: stringOf(ev.Data, "comms_session_name"),
				})
			}
			if boolOf(ev.Data, "session_retire") {
				delete(s.Sessions, stringOf(ev.Data, "retired_actor"))
			}
			if boolOf(ev.Data, "leader_transfer") {
				for _, sess := range s.Sessions {
					sess.Leader = false
				}
				if sess := s.Sessions[stringOf(ev.Data, "leader_actor")]; sess != nil {
					sess.Leader = true
				}
			}
			if boolOf(ev.Data, "comms_session_end") {
				reason := stringOf(ev.Data, "reason")
				if reason == "" {
					reason = stringOf(ev.Data, "result")
				}
				startedAt := windowStart
				actors := sortedActorSet(windowActors)
				eventCount, claimCount, findingCount, noteCount := windowEvents, windowClaims, windowFindings, windowNotes
				if sessionID != "" && named != nil {
					startedAt = named.start
					actors = sortedActorSet(named.actors)
					eventCount, claimCount, findingCount, noteCount = named.events, named.claims, named.findings, named.notes
				}
				s.EndedCommsSessions = append(s.EndedCommsSessions, &EndedCommsSession{
					ID:           ev.ID,
					SessionID:    sessionID,
					Name:         stringOf(ev.Data, "comms_session_name"),
					StartedAt:    startedAt,
					EndedAt:      ev.TS,
					EndedBy:      ev.Actor,
					Reason:       reason,
					Actors:       actors,
					ReleasedRefs: refs,
					EventCount:   eventCount,
					ClaimCount:   claimCount,
					FindingCount: findingCount,
					NoteCount:    noteCount,
				})
				if sessionID == "" {
					s.Claims = make(map[string]*Claim)
					s.Sessions = make(map[string]*Session)
					namedWindows = map[string]*sessionWindow{}
					windowStart = time.Time{}
					windowActors = map[string]bool{}
					windowEvents, windowClaims, windowFindings, windowNotes = 0, 0, 0, 0
				} else {
					for id, claim := range s.Claims {
						if claim.SessionID == sessionID {
							delete(s.Claims, id)
						}
					}
					for actor, sess := range s.Sessions {
						if sess.SessionID == sessionID {
							delete(s.Sessions, actor)
						}
					}
					delete(namedWindows, sessionID)
				}
			}
		case event.TypeFinding:
			s.Findings = append(s.Findings, &Finding{
				ID:          ev.ID,
				TS:          ev.TS,
				Actor:       ev.Actor,
				Category:    stringOf(ev.Data, "category"),
				Summary:     stringOf(ev.Data, "summary"),
				Priority:    boolOf(ev.Data, "priority"),
				Refs:        parseRefs(ev.Data),
				SessionID:   stringOf(ev.Data, "comms_session_id"),
				SessionName: stringOf(ev.Data, "comms_session_name"),
			})
		case event.TypeNote:
			s.Notes = append(s.Notes, &Note{
				ID:          ev.ID,
				TS:          ev.TS,
				Actor:       ev.Actor,
				Body:        stringOf(ev.Data, "body"),
				Priority:    boolOf(ev.Data, "priority"),
				SessionID:   stringOf(ev.Data, "comms_session_id"),
				SessionName: stringOf(ev.Data, "comms_session_name"),
			})
		}
	}
	// Stamp each surviving session with its actor's passive heartbeat. lastSeen is
	// always >= the hello TS (the hello is itself one of that actor's events), so
	// LastSeen >= TS; guard defensively anyway.
	for _, sess := range s.Sessions {
		if ls := lastSeen[sess.Actor]; ls.After(sess.TS) {
			sess.LastSeen = ls
		} else {
			sess.LastSeen = sess.TS
		}
	}
	return s
}

func sortedActorSet(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for actor := range set {
		if actor != "" {
			out = append(out, actor)
		}
	}
	sort.Strings(out)
	return out
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
		SessionID:    stringOf(ev.Data, "comms_session_id"),
		SessionName:  stringOf(ev.Data, "comms_session_name"),
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

func boolOf(m map[string]interface{}, key string) bool {
	if m == nil {
		return false
	}
	if v, ok := m[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return false
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
	if arr, ok := v.([]string); ok {
		return append([]string(nil), arr...)
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
