package subcmd

import (
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/state"
)

func TestRosterSessionsKeepsSilentClaimHolder(t *testing.T) {
	now := time.Now()
	s := &state.State{
		Sessions: map[string]*state.Session{
			"active":     {Actor: "active", TS: now.Add(-10 * time.Minute), LastSeen: now.Add(-10 * time.Minute)},
			"deadholder": {Actor: "deadholder", TS: now.Add(-6 * time.Hour), LastSeen: now.Add(-6 * time.Hour)},
			"deadidle":   {Actor: "deadidle", TS: now.Add(-6 * time.Hour), LastSeen: now.Add(-6 * time.Hour)},
		},
		Claims: map[string]*state.Claim{
			"c1": {ID: "c1", Actor: "deadholder", TS: now.Add(-6 * time.Hour)},
		},
	}
	have := map[string]bool{}
	for _, sess := range rosterSessions(s, now.Add(-activeWindow)) {
		have[sess.Actor] = true
	}
	if !have["active"] {
		t.Error("an active actor must be on the roster")
	}
	if !have["deadholder"] {
		t.Error("a >4h-silent actor STILL holding a claim must stay on the roster (the dead-holder case)")
	}
	if have["deadidle"] {
		t.Error("a >4h-silent actor holding nothing must drop off the roster")
	}
	// The retained silent holder must render as likely-dead.
	us := uiSessionFrom(s.Sessions["deadholder"], now, len(s.ActiveClaimsByActor("deadholder")), staleClaimAfter)
	if !us.LikelyDead {
		t.Error("the retained silent holder must render LikelyDead=true")
	}
}

// An actor whose session was deleted (retired, or its named session ended) but
// that kept claiming afterward holds claims with NO Session entry. It must still
// appear on the roster — otherwise its locks show in the claims list with no row
// and no Release/Remove button, looking like claims that "won't go away".
func TestRosterSessionsSurfacesOrphanClaimHolder(t *testing.T) {
	now := time.Now()
	s := &state.State{
		// No Sessions at all for "orphan": it was retired, then re-claimed.
		Sessions: map[string]*state.Session{
			"active": {Actor: "active", TS: now.Add(-10 * time.Minute), LastSeen: now.Add(-10 * time.Minute)},
		},
		Claims: map[string]*state.Claim{
			"o1": {ID: "o1", Actor: "orphan", TS: now.Add(-2 * time.Hour), SessionID: "sx", SessionName: "feature-x"},
			"o2": {ID: "o2", Actor: "orphan", TS: now.Add(-90 * time.Minute), SessionID: "sx", SessionName: "feature-x"},
		},
	}
	var orphan *state.Session
	have := map[string]bool{}
	for _, sess := range rosterSessions(s, now.Add(-activeWindow)) {
		have[sess.Actor] = true
		if sess.Actor == "orphan" {
			orphan = sess
		}
	}
	if !have["active"] {
		t.Error("an active actor must be on the roster")
	}
	if orphan == nil {
		t.Fatal("an actor holding claims with no session entry (orphaned by retire) must be on the roster")
	}
	// Heartbeat is synthesized from its own claim timestamps: earliest as TS,
	// latest as LastSeen, and the agreed session tag is preserved.
	if !orphan.TS.Equal(now.Add(-2 * time.Hour)) {
		t.Errorf("orphan TS = %v, want earliest claim TS", orphan.TS)
	}
	if !orphan.LastSeen.Equal(now.Add(-90 * time.Minute)) {
		t.Errorf("orphan LastSeen = %v, want latest claim TS", orphan.LastSeen)
	}
	if orphan.SessionName != "feature-x" {
		t.Errorf("orphan should carry its claims' agreed session tag, got %q", orphan.SessionName)
	}
	if orphan.Leader {
		t.Error("an orphaned holder must never be marked leader")
	}
	// Silent past the stale window while holding locks -> the crash signal.
	us := uiSessionFrom(orphan, now, len(s.ActiveClaimsByActor("orphan")), staleClaimAfter)
	if us.ClaimCount != 2 || !us.LikelyDead {
		t.Errorf("orphaned holder must render claim_count=2 + LikelyDead=true; got count=%d dead=%v", us.ClaimCount, us.LikelyDead)
	}
}

func TestCollectActiveSessionsUsesLastSeenNotHello(t *testing.T) {
	now := time.Now()
	s := &state.State{Sessions: map[string]*state.Session{
		// hello'd 5h ago (past the window) but acted 10m ago -> still alive.
		"busy": {Actor: "busy", TS: now.Add(-5 * time.Hour), LastSeen: now.Add(-10 * time.Minute)},
		// hello'd recently, no later activity -> active (LastSeen == TS).
		"fresh": {Actor: "fresh", TS: now.Add(-10 * time.Minute), LastSeen: now.Add(-10 * time.Minute)},
		// hello'd long ago AND silent ever since -> crashed, drops off the roster.
		"dead": {Actor: "dead", TS: now.Add(-5 * time.Hour), LastSeen: now.Add(-5 * time.Hour)},
		// Session built without LastSeen (e.g. not via Fold) -> falls back to TS.
		"legacy": {Actor: "legacy", TS: now.Add(-20 * time.Minute)},
	}}
	active := map[string]bool{}
	for _, sess := range collectActiveSessions(s, now.Add(-activeWindow)) {
		active[sess.Actor] = true
	}
	if !active["busy"] {
		t.Error("an agent that hello'd long ago but acted 10m ago must stay active (LastSeen, not hello)")
	}
	if !active["fresh"] {
		t.Error("a freshly-active agent must be on the roster")
	}
	if active["dead"] {
		t.Error("an agent silent past the window must drop off the roster")
	}
	if !active["legacy"] {
		t.Error("a session with no LastSeen must fall back to its hello TS")
	}
}

func TestRecentFindingsIncludesOlderPriorityBeforeNormalLimit(t *testing.T) {
	now := time.Now()
	s := &state.State{}
	for i := 0; i < 6; i++ {
		s.Findings = append(s.Findings, &state.Finding{
			ID:       string(rune('a' + i)),
			TS:       now.Add(time.Duration(i) * time.Minute),
			Category: "fix",
			Summary:  "normal",
		})
	}
	s.Findings = append(s.Findings, &state.Finding{
		ID:       "priority",
		TS:       now.Add(-30 * time.Minute),
		Category: "decision",
		Summary:  "leader note",
		Priority: true,
	})

	got := recentFindings(s, now.Add(-time.Hour), 3)
	if len(got) != 3 {
		t.Fatalf("len = %d, want 3", len(got))
	}
	if got[0].ID != "priority" {
		t.Fatalf("first finding = %q, want priority", got[0].ID)
	}
}

func TestRecentNotesIncludesOlderPriorityBeforeNormalLimit(t *testing.T) {
	now := time.Now()
	s := &state.State{}
	for i := 0; i < 6; i++ {
		s.Notes = append(s.Notes, &state.Note{
			ID:   string(rune('a' + i)),
			TS:   now.Add(time.Duration(i) * time.Minute),
			Body: "normal",
		})
	}
	s.Notes = append(s.Notes, &state.Note{
		ID:       "priority",
		TS:       now.Add(-30 * time.Minute),
		Body:     "everyone should know",
		Priority: true,
	})

	got := recentNotes(s, now.Add(-time.Hour), 3)
	if len(got) != 3 {
		t.Fatalf("len = %d, want 3", len(got))
	}
	if got[0].ID != "priority" {
		t.Fatalf("first note = %q, want priority", got[0].ID)
	}
}
