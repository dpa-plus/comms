package subcmd

import (
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
)

func TestRunSessionRetireRemovesActorAndReleasesClaims(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	claimID := "01JX2Q3Y7W5B6N9P0R1S2T3R1A"
	setup := []event.Event{
		{TS: time.Now().Add(-10 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3R0A", Actor: "claude-7e4c", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude", "leader": true}},
		{TS: time.Now().Add(-9 * time.Minute).UTC(), ID: claimID, Actor: "claude-7e4c", Type: event.TypeClaim, Scope: []string{"src/foo.ts"}, Data: map[string]interface{}{"intent": "old work"}},
	}
	for _, ev := range setup {
		if err := rt.Append(ev); err != nil {
			t.Fatalf("append setup: %v", err)
		}
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	if err := runSessionRetire("claude-7e4c", "renamed to claude-dev"); err != nil {
		t.Fatalf("retire: %v", err)
	}
	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if rt.State.Sessions["claude-7e4c"] != nil {
		t.Fatalf("retired actor still active: %+v", rt.State.Sessions)
	}
	if rt.State.Claims[claimID] != nil {
		t.Fatalf("retired actor claim still active")
	}
	last := rt.Events[len(rt.Events)-1]
	if last.Type != event.TypeRelease || last.Data["retired_actor"] != "claude-7e4c" {
		t.Fatalf("bad retire audit event: %+v", last)
	}
}

func TestRunSessionRetireReleasesClaimsWithoutActiveSession(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	claimID := "01JX2Q3Y7W5B6N9P0R1S2T3R2A"
	if err := rt.Append(event.Event{
		TS:    time.Now().Add(-9 * time.Minute).UTC(),
		ID:    claimID,
		Actor: "claude-old",
		Type:  event.TypeClaim,
		Scope: []string{"src/old.ts"},
		Data:  map[string]interface{}{"intent": "stale work"},
	}); err != nil {
		t.Fatalf("append setup: %v", err)
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	if err := runSessionRetire("claude-old", "stale claim"); err != nil {
		t.Fatalf("retire stale claim actor: %v", err)
	}
	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if rt.State.Claims[claimID] != nil {
		t.Fatalf("claim-only actor claim still active")
	}
}

func TestRunSessionLeadTransfersLeader(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	setup := []event.Event{
		{TS: time.Now().Add(-10 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3L0A", Actor: "claude-7e4c", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude", "leader": true}},
		{TS: time.Now().Add(-9 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3L1A", Actor: "claude-dev", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude", "label": "Claude Dev"}},
	}
	for _, ev := range setup {
		if err := rt.Append(ev); err != nil {
			t.Fatalf("append setup: %v", err)
		}
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	if err := runSessionLead("claude-dev", "user asked Claude Dev to lead"); err != nil {
		t.Fatalf("lead: %v", err)
	}
	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	if !rt.State.Sessions["claude-dev"].Leader {
		t.Fatalf("claude-dev should be leader: %+v", rt.State.Sessions)
	}
	if rt.State.Sessions["claude-7e4c"].Leader {
		t.Fatalf("old leader should not remain leader: %+v", rt.State.Sessions)
	}
}

func TestRunSessionLeadStampsTargetCommsSession(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "claude-dev")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	setup := []event.Event{
		{TS: time.Now().Add(-10 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3M0A", Actor: "claude-dev", Type: event.TypeHello, Data: map[string]interface{}{
			"base_name":          "claude",
			"leader":             true,
			"comms_session_id":   "sess-a",
			"comms_session_name": "frontend work",
		}},
		{TS: time.Now().Add(-9 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3M1A", Actor: "codex-dev", Type: event.TypeHello, Data: map[string]interface{}{
			"base_name":          "codex",
			"comms_session_id":   "sess-b",
			"comms_session_name": "backend work",
		}},
	}
	for _, ev := range setup {
		if err := rt.Append(ev); err != nil {
			t.Fatalf("append setup: %v", err)
		}
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	if err := runSessionLead("codex-dev", "backend lead owns the next decision"); err != nil {
		t.Fatalf("lead: %v", err)
	}
	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()
	last := rt.Events[len(rt.Events)-1]
	if got := last.Data["comms_session_id"]; got != "sess-b" {
		t.Fatalf("leader transfer session id = %v, want target session sess-b; event=%+v", got, last)
	}
	if got := last.Data["comms_session_name"]; got != "backend work" {
		t.Fatalf("leader transfer session name = %v, want backend work; event=%+v", got, last)
	}
}
