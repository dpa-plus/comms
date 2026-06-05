package render

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/overlap"
	"github.com/dpa-plus/comms/internal/state"
)

func mkWholeScope(raw string) overlap.Scope {
	return overlap.Scope{Raw: raw, Path: raw, Anchor: overlap.Anchor{Kind: overlap.AnchorWhole}}
}

// A stale blocking claim (idle past StaleAfter) is offered as a direct,
// no-confirmation steal; a fresh one routes to user confirmation.
func TestWriteConflictStaleClaimOffersDirectSteal(t *testing.T) {
	var buf bytes.Buffer
	WriteConflict(&buf, Conflict{
		AttemptedScope:  "src/auth.ts",
		AttemptedActor:  "claude-night",
		AttemptedIntent: "my work",
		StaleAfter:      time.Hour,
		Holders: []*state.Claim{
			{ID: "01ABCDEF", TS: time.Now().Add(-2 * time.Hour), Actor: "codex-dead", Scope: mkWholeScope("src/auth.ts"), Intent: "old"},
		},
	})
	out := buf.String()
	if !strings.Contains(out, "STALE") || !strings.Contains(out, "no --reason needed") {
		t.Fatalf("a stale conflict must offer a direct steal; got:\n%s", out)
	}
	if !strings.Contains(out, "--steal 01ABCD") {
		t.Fatalf("must suggest stealing the stale claim id; got:\n%s", out)
	}
}

// Just-under-the-threshold must NOT advertise a no-reason steal: the decision
// uses the exact age, not the minute-rounded display value (which would round
// 59m30s up to 1h and tell the caller to run a command claim.go then rejects).
func TestWriteConflictNearBoundaryDoesNotOfferReasonlessSteal(t *testing.T) {
	var buf bytes.Buffer
	WriteConflict(&buf, Conflict{
		AttemptedScope: "src/auth.ts",
		StaleAfter:     time.Hour,
		Holders: []*state.Claim{
			{ID: "01ABCDEF", TS: time.Now().Add(-59*time.Minute - 30*time.Second), Actor: "codex", Scope: mkWholeScope("src/auth.ts")},
		},
	})
	if out := buf.String(); strings.Contains(out, "no --reason needed") {
		t.Fatalf("a claim idle 59m30s (< 1h) must NOT be advertised as a reasonless steal; got:\n%s", out)
	}
}

func TestWriteConflictFreshClaimRequiresConfirmation(t *testing.T) {
	var buf bytes.Buffer
	WriteConflict(&buf, Conflict{
		AttemptedScope: "src/auth.ts",
		StaleAfter:     time.Hour,
		Holders: []*state.Claim{
			{ID: "01ABCDEF", TS: time.Now().Add(-10 * time.Minute), Actor: "codex-live", Scope: mkWholeScope("src/auth.ts")},
		},
	})
	out := buf.String()
	if strings.Contains(out, "no --reason needed") {
		t.Fatalf("a fresh (non-stale) claim must NOT offer a direct steal; got:\n%s", out)
	}
	if !strings.Contains(out, "Ask whether") {
		t.Fatalf("a fresh claim should route to user confirmation; got:\n%s", out)
	}
}

// TestWriteConflictSanitizesControlBytes verifies that attacker-controlled
// fields read back from the append-only log (actor names, scopes, intents)
// cannot inject raw terminal-escape sequences into conflict output.
func TestWriteConflictSanitizesControlBytes(t *testing.T) {
	evil := "evil\x1b[31m\nFAKE: injected\rx\x7f"

	mkScope := func(raw string) overlap.Scope {
		return overlap.Scope{Raw: raw, Path: raw, Anchor: overlap.Anchor{Kind: overlap.AnchorWhole}}
	}

	var buf bytes.Buffer
	WriteConflict(&buf, Conflict{
		AttemptedScope:  evil,
		AttemptedActor:  "me",
		AttemptedIntent: evil,
		Holders: []*state.Claim{
			{ID: "abc123def", TS: time.Now().Add(-time.Hour), Actor: evil, Scope: mkScope("a.go"), Intent: evil},
			{ID: "zzz999", TS: time.Now().Add(-30 * time.Minute), Actor: evil, Scope: mkScope(evil)},
		},
	})

	out := buf.String()
	for _, r := range out {
		if (r < 0x20 || r == 0x7f) && r != '\n' && r != '\t' {
			t.Fatalf("output contains raw control byte %#x; injection not neutralized:\n%q", r, out)
		}
	}
	// The ESC byte in particular must be gone.
	if strings.ContainsRune(out, 0x1b) {
		t.Fatalf("output still contains ESC (0x1b): %q", out)
	}
	// The visible placeholder should appear where control bytes were.
	if !strings.Contains(out, "?") {
		t.Fatalf("expected sanitized placeholder '?' in output, got: %q", out)
	}
}
