// Package render formats output destined for humans and Claude/Codex agents.
//
// The conflict template (this file) is used by both `comms claim` (exit 1)
// and `comms check` (exit 1). It is written to stderr so stdout stays clean
// for any structured output. The template embeds the literal next-command so
// agents can copy-paste rather than reconstruct from memory.
package render

import (
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/state"
)

// Conflict is the data carried into the stderr template.
type Conflict struct {
	// AttemptedScope is the scope the caller tried to claim/check. We echo
	// it verbatim in the BLOCKED line.
	AttemptedScope string

	// AttemptedActor is who tried (used to build the next-command).
	AttemptedActor string

	// AttemptedIntent is the --intent the caller supplied (used to build
	// the next-command). May be empty for `check`.
	AttemptedIntent string

	// Holders is the list of conflicting active claims (ordered oldest-first).
	Holders []*state.Claim

	// StaleAfter is the age at which a held claim is considered stale and may be
	// stolen without confirmation. When > 0 and the blocking claim is older than
	// this, WriteConflict tells the caller it can steal it directly.
	StaleAfter time.Duration
}

// WriteConflict writes the structured conflict report to w.
func WriteConflict(w io.Writer, c Conflict) {
	if len(c.Holders) == 0 {
		// Shouldn't happen — caller only invokes this when there's an
		// actual conflict — but be defensive.
		fmt.Fprintln(w, "BLOCKED: scope claimed by an unknown holder.")
		return
	}
	primary := c.Holders[0]
	raw := time.Since(primary.TS)   // exact age — drives the stale decision
	since := raw.Round(time.Minute) // rounded — display only

	// Every field below can originate from the append-only log (holder
	// actors/scopes/intents are only validated for non-emptiness at decode
	// time), so sanitize each one before it reaches the terminal to neutralize
	// ESC/C0/C1/DEL escape-sequence injection.
	primaryActor := EscapeActor(primary.Actor)
	fmt.Fprintf(w, "BLOCKED: %s is claimed.\n", EscapeScope(c.AttemptedScope))
	fmt.Fprintf(w, "  Holder:  @%s\n", primaryActor)
	fmt.Fprintf(w, "  Claim:   %s\n", EscapeScope(primary.ID))
	if primary.Intent != "" {
		fmt.Fprintf(w, "  Intent:  %q\n", EscapeScope(primary.Intent))
	}
	fmt.Fprintf(w, "  Since:   %s (%s ago)\n", primary.TS.UTC().Format(time.RFC3339), formatDuration(since))

	if len(c.Holders) > 1 {
		fmt.Fprintf(w, "\nAdditional holders:\n")
		for _, h := range c.Holders[1:] {
			fmt.Fprintf(w, "  @%-12s %s\n", EscapeActor(h.Actor), EscapeScope(h.Scope.String()))
		}
	}

	// A stale blocking claim (holder presumed gone) can be stolen directly — no
	// user confirmation needed. This is the common "took over a dead agent's
	// file" path, so lead with it when it applies.
	// Use the EXACT age (not the rounded display value) so this advice never tells
	// the caller to steal a claim that `comms claim` will then reject for being
	// just under the threshold. Steal the blocking holder's OWN single scope (the
	// attempted scope may be a comma-joined batch, which is not a valid --steal
	// claim target), and quote the full claim ID (a short prefix can be ambiguous).
	if c.StaleAfter > 0 && raw >= c.StaleAfter {
		fmt.Fprintf(w, "\n@%s's claim is STALE (idle %s, stale after %s) — its holder is presumed gone.\n", primaryActor, formatDuration(since), formatDuration(c.StaleAfter))
		fmt.Fprintf(w, "You may steal a stale claim directly (no --reason needed):\n")
		fmt.Fprintf(w, "  comms claim %q --intent %q --steal %s\n",
			EscapeScope(primary.Scope.String()), EscapeScope(intentOr(c.AttemptedIntent, "<your-intent>")), EscapeScope(primary.ID))
		return
	}

	staleNote := ""
	if c.StaleAfter > 0 {
		staleNote = fmt.Sprintf(" (or once it is stale, after %s idle)", formatDuration(c.StaleAfter))
	}
	fmt.Fprintf(w, "\nSurface this to the user. Ask whether @%s's session is still active.\n", primaryActor)
	fmt.Fprintf(w, "\nIf user confirms the prior session ended%s:\n", staleNote)
	fmt.Fprintf(w, "  comms claim %q --intent %q --steal %s --reason \"user verified prior session ended\"\n",
		EscapeScope(primary.Scope.String()), EscapeScope(intentOr(c.AttemptedIntent, "<your-intent>")), EscapeScope(primary.ID))
	fmt.Fprintf(w, "\nIf session is still active:\n")
	fmt.Fprintf(w, "  Choose a different scope, or `comms note \"@%s can I take this when you're done?\"`\n", primaryActor)
}

func intentOr(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}

func formatDuration(d time.Duration) string {
	if d < time.Minute {
		return d.String()
	}
	hours := int(d.Hours())
	mins := int(d.Minutes()) % 60
	switch {
	case hours == 0:
		return fmt.Sprintf("%dm", mins)
	case mins == 0:
		return fmt.Sprintf("%dh", hours)
	default:
		return fmt.Sprintf("%dh %dm", hours, mins)
	}
}

// sanitizeControl replaces every C0 control rune (< 0x20), DEL (0x7F), and C1
// control rune (U+0080–U+009F) with a visible '?' placeholder so
// attacker-controlled strings read back from the append-only log can't smuggle
// ANSI/terminal escape sequences (ESC, CSI — including the single-code-point C1
// CSI U+009B — newlines, carriage returns, etc.) into terminal output.
func sanitizeControl(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r < 0x20 || r == 0x7f || (r >= 0x80 && r <= 0x9f) {
			b.WriteRune('?')
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

// EscapeActor renders an actor name for terminal output, stripping any
// shenanigans like ANSI escapes or newlines. The actor package already
// validates these at resolve-time, but be defensive on read paths.
func EscapeActor(actor string) string {
	return sanitizeControl(actor)
}

// EscapeScope renders a scope string (or any other attacker-controlled field
// such as an intent) for terminal output, neutralizing control characters.
// Scopes are rejected at parse time, but they are read back from the
// append-only log where holder entries are only validated for non-emptiness,
// so keep this defense on the render path.
func EscapeScope(scope string) string {
	return sanitizeControl(scope)
}
