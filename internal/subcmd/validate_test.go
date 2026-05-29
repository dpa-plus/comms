package subcmd

import (
	"strings"
	"testing"
)

func TestRejectControlText(t *testing.T) {
	tests := []struct {
		name     string
		field    string
		s        string
		maxRunes int
		wantErr  bool
	}{
		{"empty ok", "note body", "", 280, false},
		{"plain ascii ok", "note body", "deploy is green", 280, false},
		{"latin1 cafe ok", "note body", "Café", 280, false},
		{"cjk ok", "finding summary", "日本語", 280, false},
		{"emoji and dash ok", "--label", "Cläude — Dev ✨", 280, false},
		{"max length ok", "note body", strings.Repeat("x", 280), 280, false},
		{"no cap means any length ok", "note body", strings.Repeat("x", 10000), 0, false},

		// C0 controls.
		{"newline rejected", "note body", "line1\nFAKE: line2", 280, true},
		{"carriage return rejected", "note body", "a\rb", 280, true},
		{"tab rejected", "note body", "a\tb", 280, true},
		{"escape rejected", "note body", "a\x1b[31mb", 280, true},
		{"nul rejected", "note body", "a\x00b", 280, true},
		// DEL.
		{"del rejected", "note body", "a\x7fb", 280, true},
		// C1 controls — the range Round 1's validateLabel missed.
		{"c1 nel 0x85 rejected", "note body", "a\u0085b", 280, true},
		{"c1 low 0x80 rejected", "note body", "a\u0080b", 280, true},
		{"c1 high 0x9f rejected", "note body", "a\u009fb", 280, true},

		// Boundary: U+00A0 (NBSP) is the first rune ABOVE the C1 range — must pass.
		{"nbsp 0x00a0 ok", "note body", "a\u00a0b", 280, false},

		{"too long rejected", "note body", strings.Repeat("x", 281), 280, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := rejectControlText(tt.field, tt.s, tt.maxRunes)
			if (err != nil) != tt.wantErr {
				t.Fatalf("rejectControlText(%q, %q, %d) error = %v, wantErr %v",
					tt.field, tt.s, tt.maxRunes, err, tt.wantErr)
			}
			if err != nil && !strings.HasPrefix(err.Error(), tt.field) {
				t.Fatalf("error %q does not name field %q", err.Error(), tt.field)
			}
		})
	}
}
