// Package actor resolves the $COMMS_ACTOR environment variable into a
// validated session-actor identity.
//
// The load-bearing rule (Round 4 Patch #1): the actor MUST be a per-session
// name like `claude-3a1f`, `codex-9b2c`, `human-eli`. Per-user generic names
// like `eli`, `claude`, `codex` are rejected by default because they break
// the conflict model — `comms check` would treat every other live agent's
// claim as "held by same actor" and wave through edits.
package actor

import (
	"fmt"
	"os"
	"strings"
	"unicode"
)

// EnvVar is the canonical environment variable name.
const EnvVar = "COMMS_ACTOR"

// AllowGenericEnvVar is the opt-out override.
const AllowGenericEnvVar = "COMMS_ALLOW_GENERIC_ACTOR"

// Mode tells Resolve how to treat unset / generic values.
type Mode int

const (
	// ReadOnly: missing COMMS_ACTOR is fine; returns the empty string and no
	// error. Used by `status`, `log`, `check` (read forms).
	ReadOnly Mode = iota
	// Mutating: COMMS_ACTOR is required; missing or generic values fail.
	// Used by `claim`, `release`, `note`, `find`, `doc --edit`, mutating `hello`.
	Mutating
)

// Resolve reads $COMMS_ACTOR from env, validates it, and returns the actor
// name plus an explanatory error if validation fails.
//
// In ReadOnly mode an unset env var returns ("", nil).
// In Mutating mode an unset or generic name returns ("", err) — err.Error()
// is the user-facing message the CLI prints to stderr.
func Resolve(mode Mode) (string, error) {
	raw := os.Getenv(EnvVar)
	if raw == "" {
		if mode == ReadOnly {
			return "", nil
		}
		return "", fmt.Errorf(
			"COMMS_ACTOR unset. Set in your shell (e.g., human-eli, claude-3a1f) " +
				"or run `comms hello <name>` explicitly.",
		)
	}
	if err := validateCharset(raw); err != nil {
		return "", err
	}
	if mode == Mutating && !genericAllowed() && isGeneric(raw) {
		return "", fmt.Errorf(
			"COMMS_ACTOR=%q looks like a per-user name, not a per-session actor. "+
				"Use 'claude-3a1f', 'codex-9b2c', or 'human-eli'. "+
				"Set COMMS_ALLOW_GENERIC_ACTOR=1 to override.", raw,
		)
	}
	return raw, nil
}

// IsGeneric reports whether name is a reserved per-user generic name
// (case-insensitive equality, NOT substring). Exported for tests and for
// the `hello` command's warning output.
func IsGeneric(name string) bool {
	return isGeneric(name)
}

// genericNames is the case-insensitive equality blocklist. Per the plan's
// Round 4 patch: equality, not substring — so `claude-3a1f`, `codex-9b2c`,
// `agent-0`, `human-eli` all pass.
var genericNames = []string{"eli", "claude", "codex", "agent", "user"}

func isGeneric(name string) bool {
	low := strings.ToLower(name)
	for _, g := range genericNames {
		if low == g {
			return true
		}
	}
	if u := strings.ToLower(os.Getenv("USER")); u != "" && low == u {
		return true
	}
	return false
}

func genericAllowed() bool {
	v := os.Getenv(AllowGenericEnvVar)
	return v == "1" || strings.EqualFold(v, "true") || strings.EqualFold(v, "yes")
}

// validateCharset enforces a conservative printable charset on the actor
// name: letters, digits, dot, dash, underscore. We refuse control bytes,
// whitespace, and anything that could confuse terminal rendering or
// downstream JSON consumers.
//
// Max length is 64 runes — long enough for `claude-` + a UUID prefix, short
// enough to keep status output legible.
func validateCharset(name string) error {
	if name == "" {
		return fmt.Errorf("COMMS_ACTOR cannot be empty")
	}
	if runeCount(name) > 64 {
		return fmt.Errorf("COMMS_ACTOR exceeds 64-rune limit: %q", name)
	}
	for _, r := range name {
		if r > unicode.MaxASCII {
			return fmt.Errorf("COMMS_ACTOR must be ASCII, got %q", name)
		}
		if !isActorRune(byte(r)) {
			return fmt.Errorf("COMMS_ACTOR contains invalid character %q (allowed: a-z A-Z 0-9 . - _)", string(r))
		}
	}
	return nil
}

func isActorRune(b byte) bool {
	switch {
	case b >= 'a' && b <= 'z':
		return true
	case b >= 'A' && b <= 'Z':
		return true
	case b >= '0' && b <= '9':
		return true
	case b == '.' || b == '-' || b == '_':
		return true
	}
	return false
}

func runeCount(s string) int {
	n := 0
	for range s {
		n++
	}
	return n
}
