package subcmd

import (
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/state"
)

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
