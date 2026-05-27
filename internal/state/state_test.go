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
