package render

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/overlap"
	"github.com/dpa-plus/comms/internal/state"
)

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
