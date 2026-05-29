package subcmd

import (
	"fmt"
	"unicode/utf8"
)

// rejectControlText guards free-text fields that are later rendered raw via %s
// in human-facing status/log output. Such output is a terminal-injection vector:
// a control character (newline/carriage-return can forge output lines, ESC can
// inject terminal-escape sequences) embedded in stored text would be replayed
// verbatim when the field is printed.
//
// It returns an error if s contains any control rune — the C0 range and DEL
// (r < 0x20 || r == 0x7f) plus the C1 range (0x80–0x9f) — or, when maxRunes > 0,
// if s is longer than maxRunes Unicode runes (scalar values, NOT bytes).
//
// Legitimate Unicode (any rune >= 0x100, e.g. "Café", "日本語") passes; only the
// control ranges are rejected. field names the offending input for the message,
// e.g. rejectControlText("note body", body, 280) →
// "note body must not contain control characters".
func rejectControlText(field, s string, maxRunes int) error {
	for _, r := range s {
		if r < 0x20 || r == 0x7f || (r >= 0x80 && r <= 0x9f) {
			return fmt.Errorf("%s must not contain control characters", field)
		}
	}
	if maxRunes > 0 {
		if n := utf8.RuneCountInString(s); n > maxRunes {
			return fmt.Errorf("%s is too long (%d characters, max %d)", field, n, maxRunes)
		}
	}
	return nil
}
