package subcmd

import (
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/overlap"
	"github.com/dpa-plus/comms/internal/state"
)

func mustScope(t *testing.T, s string) overlap.Scope {
	t.Helper()
	sc, err := overlap.Parse(s)
	if err != nil {
		t.Fatalf("parse %q: %v", s, err)
	}
	return sc
}

// In real logs every finding has an empty scope[] and stores its file in a
// path: ref, so `comms log --scope <file>` must match the ref, not just scope[].
func TestEventScopeOverlapsMatchesFindingPathRef(t *testing.T) {
	want := mustScope(t, "src/routes/billing.ts")

	matching := event.Event{Type: event.TypeFinding, Data: map[string]interface{}{
		"category": "bug",
		"refs":     []interface{}{map[string]interface{}{"kind": "path", "value": "src/routes/billing.ts"}},
	}}
	if !eventScopeOverlaps(matching, want) {
		t.Fatal("a finding whose path ref matches must overlap --scope")
	}

	otherPath := event.Event{Type: event.TypeFinding, Data: map[string]interface{}{
		"refs": []interface{}{map[string]interface{}{"kind": "path", "value": "src/other.ts"}},
	}}
	if eventScopeOverlaps(otherPath, want) {
		t.Fatal("a finding referencing a different path must NOT overlap")
	}

	// A non-path ref that happens to hold a path-like string must not match.
	commitRef := event.Event{Type: event.TypeFinding, Data: map[string]interface{}{
		"refs": []interface{}{map[string]interface{}{"kind": "commit", "value": "src/routes/billing.ts"}},
	}}
	if eventScopeOverlaps(commitRef, want) {
		t.Fatal("only kind==path refs should be treated as scopes")
	}
}

func TestFindingsOnScopesPrefersDurableThenRecent(t *testing.T) {
	base := time.Now()
	pathRef := func(p string) []state.Ref { return []state.Ref{{Kind: "path", Value: p}} }
	s := &state.State{Findings: []*state.Finding{
		{Category: "fix", Summary: "old fix", TS: base.Add(-3 * time.Hour), Refs: pathRef("src/crypto.ts")},
		{Category: "gotcha", Summary: "key immutable", TS: base.Add(-2 * time.Hour), Refs: pathRef("src/crypto.ts")},
		{Category: "decision", Summary: "crypto is SoT", TS: base.Add(-1 * time.Hour), Refs: pathRef("src/crypto.ts")},
		{Category: "bug", Summary: "unrelated", TS: base, Refs: pathRef("src/other.ts")},
	}}
	got := findingsOnScopes(s, []overlap.Scope{mustScope(t, "src/crypto.ts")}, 3)
	if len(got) != 3 {
		t.Fatalf("expected 3 matches on src/crypto.ts, got %d", len(got))
	}
	if !durableCategory(got[0].Category) || !durableCategory(got[1].Category) {
		t.Fatalf("durable findings must come first, got %s,%s,%s", got[0].Category, got[1].Category, got[2].Category)
	}
	if got[2].Category != "fix" {
		t.Fatalf("the churn 'fix' should sort last, got %s", got[2].Category)
	}
	for _, f := range got {
		if f.Summary == "unrelated" {
			t.Fatal("a finding on a different path must not match")
		}
	}
}
