package state

import (
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/overlap"
)

func parseScope(t *testing.T, s string) (overlap.Scope, error) {
	t.Helper()
	return overlap.Parse(s)
}

func mkEvent(t *testing.T, ts time.Time, actor string, typ event.Type, scope []string, data map[string]interface{}) event.Event {
	t.Helper()
	return event.Event{
		TS:    ts,
		ID:    event.NewID(ts),
		Actor: actor,
		Type:  typ,
		Scope: scope,
		Data:  data,
	}
}

func TestFoldEmpty(t *testing.T) {
	s := Fold(nil)
	if len(s.Claims) != 0 || len(s.Sessions) != 0 {
		t.Errorf("expected empty state, got %+v", s)
	}
}

func TestFoldHelloDedupesByActor(t *testing.T) {
	now := time.Now().UTC()
	events := []event.Event{
		mkEvent(t, now, "claude-3a1f", event.TypeHello, nil, map[string]interface{}{"hostname": "host-1"}),
		mkEvent(t, now.Add(time.Second), "claude-3a1f", event.TypeHello, nil, map[string]interface{}{"hostname": "host-2"}),
	}
	s := Fold(events)
	if len(s.Sessions) != 1 {
		t.Fatalf("expected 1 session, got %d", len(s.Sessions))
	}
	if s.Sessions["claude-3a1f"].Hostname != "host-2" {
		t.Errorf("expected most recent hostname, got %+v", s.Sessions["claude-3a1f"])
	}
}

func TestFoldHelloTracksLeader(t *testing.T) {
	now := time.Now().UTC()
	events := []event.Event{
		mkEvent(t, now, "codex-1", event.TypeHello, nil, map[string]interface{}{"leader": true}),
		mkEvent(t, now.Add(time.Second), "claude-1", event.TypeHello, nil, map[string]interface{}{}),
	}
	s := Fold(events)
	if !s.Sessions["codex-1"].Leader {
		t.Fatalf("codex-1 should be leader")
	}
	if s.Sessions["claude-1"].Leader {
		t.Fatalf("claude-1 should not be leader")
	}
}

func TestFoldHelloTracksDisplayLabel(t *testing.T) {
	now := time.Now().UTC()
	s := Fold([]event.Event{
		mkEvent(t, now, "claude-dev", event.TypeHello, nil, map[string]interface{}{"label": "Claude Dev"}),
	})
	if got := s.Sessions["claude-dev"].Label; got != "Claude Dev" {
		t.Fatalf("label = %q, want Claude Dev", got)
	}
}

func TestFoldSessionRetireRemovesSessionAndClaims(t *testing.T) {
	now := time.Now().UTC()
	hello := mkEvent(t, now, "claude-7e4c", event.TypeHello, nil, map[string]interface{}{"leader": true})
	claim := mkEvent(t, now.Add(time.Second), "claude-7e4c", event.TypeClaim, []string{"src/foo.ts"}, map[string]interface{}{"intent": "old work"})
	retire := mkEvent(t, now.Add(2*time.Second), "claude-dev", event.TypeRelease, nil, map[string]interface{}{
		"refs":           []interface{}{claim.ID},
		"session_retire": true,
		"retired_actor":  "claude-7e4c",
		"reason":         "renamed to claude-dev",
	})

	s := Fold([]event.Event{hello, claim, retire})
	if s.Sessions["claude-7e4c"] != nil {
		t.Fatalf("retired session should be removed from active state: %+v", s.Sessions)
	}
	if s.Claims[claim.ID] != nil {
		t.Fatalf("retired actor's claim should be released")
	}
}

func TestFoldLeaderTransfer(t *testing.T) {
	now := time.Now().UTC()
	events := []event.Event{
		mkEvent(t, now, "claude-7e4c", event.TypeHello, nil, map[string]interface{}{"leader": true}),
		mkEvent(t, now.Add(time.Second), "claude-dev", event.TypeHello, nil, map[string]interface{}{"label": "Claude Dev"}),
		mkEvent(t, now.Add(2*time.Second), "human-eli", event.TypeRelease, nil, map[string]interface{}{
			"leader_transfer": true,
			"leader_actor":    "claude-dev",
			"reason":          "user asked Claude Dev to lead",
		}),
	}

	s := Fold(events)
	if !s.Sessions["claude-dev"].Leader {
		t.Fatalf("claude-dev should be leader: %+v", s.Sessions)
	}
	if s.Sessions["claude-7e4c"].Leader {
		t.Fatalf("old leader should be cleared: %+v", s.Sessions)
	}
}

func TestFoldPriorityNoteAndFinding(t *testing.T) {
	now := time.Now().UTC()
	events := []event.Event{
		mkEvent(t, now, "codex-1", event.TypeNote, nil, map[string]interface{}{"body": "important", "priority": true}),
		mkEvent(t, now.Add(time.Second), "codex-1", event.TypeFinding, nil, map[string]interface{}{"category": "decision", "summary": "important decision", "priority": true}),
	}
	s := Fold(events)
	if len(s.Notes) != 1 || !s.Notes[0].Priority {
		t.Fatalf("priority note not folded: %+v", s.Notes)
	}
	if len(s.Findings) != 1 || !s.Findings[0].Priority {
		t.Fatalf("priority finding not folded: %+v", s.Findings)
	}
}

func TestFoldClaimAndRelease(t *testing.T) {
	now := time.Now().UTC()
	claim := mkEvent(t, now, "claude-3a1f", event.TypeClaim, []string{"src/foo.ts"}, map[string]interface{}{"intent": "fix bug"})
	events := []event.Event{
		claim,
		mkEvent(t, now.Add(time.Second), "claude-3a1f", event.TypeRelease, nil, map[string]interface{}{"refs": []interface{}{claim.ID}}),
	}
	s := Fold(events)
	if len(s.Claims) != 0 {
		t.Fatalf("released claim should be gone, got %+v", s.Claims)
	}
}

func TestFoldCommsSessionEndArchivesWindowAndReleasesAllClaims(t *testing.T) {
	now := time.Now().UTC()
	hello := mkEvent(t, now, "claude-1", event.TypeHello, nil, map[string]interface{}{"leader": true})
	claim := mkEvent(t, now.Add(time.Second), "claude-1", event.TypeClaim, []string{"src/foo.ts"}, map[string]interface{}{"intent": "fix bug"})
	otherHello := mkEvent(t, now.Add(1500*time.Millisecond), "codex-1", event.TypeHello, nil, map[string]interface{}{"hostname": "host"})
	otherClaim := mkEvent(t, now.Add(1800*time.Millisecond), "codex-1", event.TypeClaim, []string{"src/bar.ts"}, map[string]interface{}{"intent": "review bar"})
	note := mkEvent(t, now.Add(1900*time.Millisecond), "codex-1", event.TypeNote, nil, map[string]interface{}{"body": "watch auth"})
	end := mkEvent(t, now.Add(2*time.Second), "human-eli", event.TypeRelease, nil, map[string]interface{}{
		"refs":              []interface{}{claim.ID, otherClaim.ID},
		"comms_session_end": true,
		"reason":            "project done",
	})
	s := Fold([]event.Event{hello, claim, otherHello, otherClaim, note, end})
	if len(s.Claims) != 0 {
		t.Fatalf("comms session end should release claims, got %+v", s.Claims)
	}
	if len(s.Sessions) != 0 {
		t.Fatalf("comms session end should clear active sessions, got %+v", s.Sessions)
	}
	if len(s.EndedCommsSessions) != 1 {
		t.Fatalf("ended comms sessions = %d, want 1", len(s.EndedCommsSessions))
	}
	ended := s.EndedCommsSessions[0]
	if ended.EndedBy != "human-eli" || ended.Reason != "project done" || len(ended.ReleasedRefs) != 2 {
		t.Fatalf("bad archive: %+v", ended)
	}
	if ended.EventCount != 6 || ended.ClaimCount != 2 || ended.NoteCount != 1 {
		t.Fatalf("bad archive counts: %+v", ended)
	}
	wantActors := []string{"claude-1", "codex-1", "human-eli"}
	for i, want := range wantActors {
		if i >= len(ended.Actors) || ended.Actors[i] != want {
			t.Fatalf("actors = %+v, want %+v", ended.Actors, wantActors)
		}
	}
}

func TestFoldNamedCommsSessionEndOnlyClearsMatchingSession(t *testing.T) {
	now := time.Now().UTC()
	aHello := mkEvent(t, now, "claude-dev", event.TypeHello, nil, map[string]interface{}{
		"comms_session_id":   "sess-a",
		"comms_session_name": "dashboard fixes",
	})
	aClaim := mkEvent(t, now.Add(time.Second), "claude-dev", event.TypeClaim, []string{"src/a.ts"}, map[string]interface{}{
		"intent":             "work a",
		"comms_session_id":   "sess-a",
		"comms_session_name": "dashboard fixes",
	})
	bHello := mkEvent(t, now.Add(2*time.Second), "codex-dev", event.TypeHello, nil, map[string]interface{}{
		"comms_session_id":   "sess-b",
		"comms_session_name": "billing fixes",
	})
	bClaim := mkEvent(t, now.Add(3*time.Second), "codex-dev", event.TypeClaim, []string{"src/b.ts"}, map[string]interface{}{
		"intent":             "work b",
		"comms_session_id":   "sess-b",
		"comms_session_name": "billing fixes",
	})
	endA := mkEvent(t, now.Add(4*time.Second), "human-eli", event.TypeRelease, nil, map[string]interface{}{
		"refs":               []interface{}{aClaim.ID},
		"comms_session_end":  true,
		"comms_session_id":   "sess-a",
		"comms_session_name": "dashboard fixes",
		"reason":             "done",
	})

	s := Fold([]event.Event{aHello, aClaim, bHello, bClaim, endA})
	if s.Claims[aClaim.ID] != nil {
		t.Fatalf("ended named session claim should be released")
	}
	if s.Claims[bClaim.ID] == nil {
		t.Fatalf("other named session claim should remain active")
	}
	if s.Sessions["claude-dev"] != nil {
		t.Fatalf("ended named session actor should be removed")
	}
	if s.Sessions["codex-dev"] == nil {
		t.Fatalf("other named session actor should remain active")
	}
	if len(s.EndedCommsSessions) != 1 || s.EndedCommsSessions[0].SessionID != "sess-a" || s.EndedCommsSessions[0].Name != "dashboard fixes" {
		t.Fatalf("bad named archive marker: %+v", s.EndedCommsSessions)
	}
}

func TestFoldNamedCommsSessionArchiveCountsOnlyThatSession(t *testing.T) {
	now := time.Now().UTC()
	aHello := mkEvent(t, now, "claude-dev", event.TypeHello, nil, map[string]interface{}{
		"comms_session_id":   "sess-a",
		"comms_session_name": "dashboard fixes",
	})
	aClaim := mkEvent(t, now.Add(time.Second), "claude-dev", event.TypeClaim, []string{"src/a.ts"}, map[string]interface{}{
		"intent":             "work a",
		"comms_session_id":   "sess-a",
		"comms_session_name": "dashboard fixes",
	})
	bHello := mkEvent(t, now.Add(2*time.Second), "codex-dev", event.TypeHello, nil, map[string]interface{}{
		"comms_session_id":   "sess-b",
		"comms_session_name": "billing fixes",
	})
	bClaim := mkEvent(t, now.Add(3*time.Second), "codex-dev", event.TypeClaim, []string{"src/b.ts"}, map[string]interface{}{
		"intent":             "work b",
		"comms_session_id":   "sess-b",
		"comms_session_name": "billing fixes",
	})
	endA := mkEvent(t, now.Add(4*time.Second), "human-eli", event.TypeRelease, nil, map[string]interface{}{
		"refs":               []interface{}{aClaim.ID},
		"comms_session_end":  true,
		"comms_session_id":   "sess-a",
		"comms_session_name": "dashboard fixes",
		"reason":             "done",
	})

	s := Fold([]event.Event{aHello, aClaim, bHello, bClaim, endA})
	if len(s.EndedCommsSessions) != 1 {
		t.Fatalf("ended comms sessions = %d, want 1", len(s.EndedCommsSessions))
	}
	ended := s.EndedCommsSessions[0]
	if ended.EventCount != 3 || ended.ClaimCount != 1 || ended.StartedAt != aHello.TS {
		t.Fatalf("named archive should count only sess-a events plus end marker: %+v", ended)
	}
	wantActors := []string{"claude-dev", "human-eli"}
	if len(ended.Actors) != len(wantActors) {
		t.Fatalf("actors = %+v, want %+v", ended.Actors, wantActors)
	}
	for i, want := range wantActors {
		if ended.Actors[i] != want {
			t.Fatalf("actors = %+v, want %+v", ended.Actors, wantActors)
		}
	}
}

func TestFoldHelloStartsNewWindowAfterCommsSessionEnd(t *testing.T) {
	now := time.Now().UTC()
	end := mkEvent(t, now, "human-eli", event.TypeRelease, nil, map[string]interface{}{
		"comms_session_end": true,
		"reason":            "done",
	})
	hello := mkEvent(t, now.Add(time.Second), "claude-1", event.TypeHello, nil, map[string]interface{}{"hostname": "host"})
	s := Fold([]event.Event{end, hello})
	if len(s.EndedCommsSessions) != 1 {
		t.Fatalf("ended archive should remain available, got %+v", s.EndedCommsSessions)
	}
	if s.Sessions["claude-1"] == nil {
		t.Fatalf("new hello should create a fresh active comms window")
	}
}

func TestFoldArbitratedSteal(t *testing.T) {
	now := time.Now().UTC()
	alice := mkEvent(t, now, "alice", event.TypeClaim, []string{"src/foo.ts"}, map[string]interface{}{"intent": "long task"})
	bob := mkEvent(t, now.Add(time.Hour), "bob", event.TypeClaim, []string{"src/foo.ts"},
		map[string]interface{}{
			"intent":       "alice gone",
			"steals":       alice.ID,
			"steal_reason": "user verified",
			"arbitrator":   "eli",
		})
	s := Fold([]event.Event{alice, bob})

	if _, ok := s.Claims[alice.ID]; ok {
		t.Errorf("alice's claim should be displaced")
	}
	c, ok := s.Claims[bob.ID]
	if !ok {
		t.Fatalf("bob's claim should be active")
	}
	if c.Actor != "bob" {
		t.Errorf("actor: got %q", c.Actor)
	}
	if c.StolenFromID != alice.ID {
		t.Errorf("stolen-from: got %q", c.StolenFromID)
	}
	if c.Arbitrator != "eli" {
		t.Errorf("arbitrator: got %q", c.Arbitrator)
	}
}

func TestConflictsFor(t *testing.T) {
	now := time.Now().UTC()
	alice := mkEvent(t, now, "alice", event.TypeClaim, []string{"src/foo.ts#bar"}, map[string]interface{}{"intent": ""})
	s := Fold([]event.Event{alice})

	// Different actor, same scope → conflict.
	scopeStr := "src/foo.ts#bar"
	parsed, _ := parseScope(t, scopeStr)
	conflicts := s.ConflictsFor(parsed, "bob")
	if len(conflicts) != 1 {
		t.Errorf("expected 1 conflict, got %d", len(conflicts))
	}

	// Same actor, same scope → no conflict.
	conflicts = s.ConflictsFor(parsed, "alice")
	if len(conflicts) != 0 {
		t.Errorf("self-overlap should not conflict, got %d", len(conflicts))
	}

	// Different actor, different anchor → no conflict.
	other, _ := parseScope(t, "src/foo.ts#baz")
	conflicts = s.ConflictsFor(other, "bob")
	if len(conflicts) != 0 {
		t.Errorf("different anchor should not conflict, got %d", len(conflicts))
	}

	// Different actor, whole-file → conflict.
	whole, _ := parseScope(t, "src/foo.ts")
	conflicts = s.ConflictsFor(whole, "bob")
	if len(conflicts) != 1 {
		t.Errorf("whole-file should conflict with anchored, got %d", len(conflicts))
	}
}

func TestPrefixLookup(t *testing.T) {
	now := time.Now().UTC()
	c := mkEvent(t, now, "alice", event.TypeClaim, []string{"src/foo.ts"}, nil)
	s := Fold([]event.Event{c})
	found := s.ClaimByID(c.ID[:6])
	if found == nil || found.ID != c.ID {
		t.Errorf("prefix lookup failed: got %v", found)
	}
}
