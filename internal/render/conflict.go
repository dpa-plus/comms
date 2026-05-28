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
}

// Conflict writes the structured conflict report to w.
func WriteConflict(w io.Writer, c Conflict) {
	if len(c.Holders) == 0 {
		// Shouldn't happen — caller only invokes this when there's an
		// actual conflict — but be defensive.
		fmt.Fprintln(w, "BLOCKED: scope claimed by an unknown holder.")
		return
	}
	primary := c.Holders[0]
	since := time.Since(primary.TS).Round(time.Minute)

	fmt.Fprintf(w, "BLOCKED: %s is claimed.\n", c.AttemptedScope)
	fmt.Fprintf(w, "  Holder:  @%s\n", primary.Actor)
	fmt.Fprintf(w, "  Claim:   %s\n", primary.ID)
	if primary.Intent != "" {
		fmt.Fprintf(w, "  Intent:  %q\n", primary.Intent)
	}
	fmt.Fprintf(w, "  Since:   %s (%s ago)\n", primary.TS.UTC().Format(time.RFC3339), formatDuration(since))

	if len(c.Holders) > 1 {
		fmt.Fprintf(w, "\nAdditional holders:\n")
		for _, h := range c.Holders[1:] {
			fmt.Fprintf(w, "  @%-12s %s\n", h.Actor, h.Scope.String())
		}
	}

	fmt.Fprintf(w, "\nSurface this to the user. Ask whether @%s's session is still active.\n", primary.Actor)
	fmt.Fprintf(w, "\nIf user confirms the prior session ended:\n")
	fmt.Fprintf(w, "  comms claim %q --intent %q --steal %s --reason \"user verified prior session ended\"\n",
		c.AttemptedScope, intentOr(c.AttemptedIntent, "<your-intent>"), shortID(primary.ID))
	fmt.Fprintf(w, "\nIf session is still active:\n")
	fmt.Fprintf(w, "  Choose a different scope, or `comms note \"@%s can I take this when you're done?\"`\n", primary.Actor)
}

func intentOr(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}

func shortID(id string) string {
	if len(id) > 6 {
		return id[:6]
	}
	return id
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

// EscapeActor renders an actor name for terminal output, stripping any
// shenanigans like ANSI escapes or newlines. The actor package already
// validates these at resolve-time, but be defensive on read paths.
func EscapeActor(actor string) string {
	var b strings.Builder
	for _, r := range actor {
		if r < 0x20 || r == 0x7f {
			b.WriteRune('?')
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}
