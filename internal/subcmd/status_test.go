package subcmd

import (
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/state"
)

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
