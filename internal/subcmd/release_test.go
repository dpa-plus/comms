package subcmd

import (
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
)

// TestReleaseAllClaimsForActorFreesEveryClaimButKeepsActor verifies the engine
// behind the UI's "release all of an agent's claims" control: every active claim
// the target holds is released in one pass, claims held by OTHER actors are left
// untouched, and (unlike retire) the target stays on the roster.
func TestReleaseAllClaimsForActorFreesEveryClaimButKeepsActor(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	deadClaims := []string{
		"01JX2Q3Y7W5B6N9P0R1S2T3D01",
		"01JX2Q3Y7W5B6N9P0R1S2T3D02",
		"01JX2Q3Y7W5B6N9P0R1S2T3D03",
	}
	otherClaim := "01JX2Q3Y7W5B6N9P0R1S2T3O01"
	setup := []event.Event{
		{TS: time.Now().Add(-10 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3H01", Actor: "codex-dead", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "codex"}},
		{TS: time.Now().Add(-9 * time.Minute).UTC(), ID: deadClaims[0], Actor: "codex-dead", Type: event.TypeClaim, Scope: []string{"src/a.ts"}, Data: map[string]interface{}{"intent": "work a"}},
		{TS: time.Now().Add(-8 * time.Minute).UTC(), ID: deadClaims[1], Actor: "codex-dead", Type: event.TypeClaim, Scope: []string{"src/b.ts"}, Data: map[string]interface{}{"intent": "work b"}},
		{TS: time.Now().Add(-7 * time.Minute).UTC(), ID: deadClaims[2], Actor: "codex-dead", Type: event.TypeClaim, Scope: []string{"src/c.ts"}, Data: map[string]interface{}{"intent": "work c"}},
		{TS: time.Now().Add(-10 * time.Minute).UTC(), ID: "01JX2Q3Y7W5B6N9P0R1S2T3H02", Actor: "claude-live", Type: event.TypeHello, Data: map[string]interface{}{"base_name": "claude"}},
		{TS: time.Now().Add(-6 * time.Minute).UTC(), ID: otherClaim, Actor: "claude-live", Type: event.TypeClaim, Scope: []string{"src/d.ts"}, Data: map[string]interface{}{"intent": "live work"}},
	}
	for _, ev := range setup {
		if err := rt.Append(ev); err != nil {
			t.Fatalf("append setup: %v", err)
		}
	}

	n, err := releaseAllClaimsForActor(rt, "codex-dead", "", "")
	if err != nil {
		t.Fatalf("release all: %v", err)
	}
	if n != 3 {
		t.Fatalf("released %d claims, want 3", n)
	}
	if err := rt.Close(); err != nil {
		t.Fatalf("close runtime: %v", err)
	}

	rt, err = Open(OpenOpts{Mutating: false})
	if err != nil {
		t.Fatalf("reopen runtime: %v", err)
	}
	defer rt.Close()

	for _, id := range deadClaims {
		if rt.State.Claims[id] != nil {
			t.Fatalf("dead actor claim %s still active after release-all", id)
		}
	}
	if rt.State.Claims[otherClaim] == nil {
		t.Fatalf("other actor's claim was released — release-all must be scoped to one actor")
	}
	// Unlike retire, the actor is NOT removed from the roster.
	if rt.State.Sessions["codex-dead"] == nil {
		t.Fatalf("release-all must keep the actor on the roster (use retire to remove)")
	}
	// The releases are real, arbitrated releases (not housekeeping), so they
	// surface in the completed feed and record the original holder.
	var sawArbitrated bool
	for _, ev := range rt.Events {
		if ev.Type == event.TypeRelease && ev.Data["original_actor"] == "codex-dead" {
			sawArbitrated = true
			if ev.Actor != "human-eli" {
				t.Fatalf("arbitrated release should be attributed to the operator, got @%s", ev.Actor)
			}
			if _, ok := ev.Data["reason"]; !ok {
				t.Fatalf("arbitrated release missing reason audit field: %+v", ev.Data)
			}
		}
	}
	if !sawArbitrated {
		t.Fatalf("expected an arbitrated release event recording original_actor=codex-dead")
	}
	if len(rt.State.Releases) == 0 {
		t.Fatalf("expected released claims to appear in the completed feed (not housekeeping-filtered)")
	}
}

// TestReleaseAllClaimsForActorNoClaimsErrors confirms the no-op case is a clean
// 4xx-style error rather than a silent success, so the UI surfaces it.
func TestReleaseAllClaimsForActorNoClaimsErrors(t *testing.T) {
	repo := setupUITestRepo(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("COMMS_ACTOR", "human-eli")
	t.Setenv("USER", "eli")
	t.Chdir(repo)

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		t.Fatalf("open runtime: %v", err)
	}
	defer rt.Close()
	if _, err := releaseAllClaimsForActor(rt, "ghost-agent", "", ""); err == nil {
		t.Fatalf("expected error when actor holds no active claims")
	}
}
