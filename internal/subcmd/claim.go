package subcmd

import (
	"fmt"
	"os"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/overlap"
	"github.com/dpa-plus/comms/internal/render"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewClaimCmd builds the `comms claim` subcommand.
//
// Exit codes:
//
//	0 — claim granted
//	1 — blocked by another active claim (stderr = render.WriteConflict)
//	2 — system error
func NewClaimCmd() *cobra.Command {
	var (
		intent      string
		stealID     string
		stealReason string
	)
	cmd := &cobra.Command{
		Use:   `claim "<scope>"`,
		Short: "Open an exclusive claim on a scope",
		Long: `Open an exclusive claim. Scope is path[#anchor] (POSIX path, optional
line range L<n>-<m> or symbol name).

If the scope conflicts with another actor's active claim, exits 1 with a
structured stderr report telling you exactly which next-command to run.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runClaim(args[0], intent, stealID, stealReason)
		},
	}
	cmd.Flags().StringVar(&intent, "intent", "", "one-line description of the change you're making (required)")
	cmd.Flags().StringVar(&stealID, "steal", "", "claim ID to displace (use with --reason for arbitrated takeover)")
	cmd.Flags().StringVar(&stealReason, "reason", "", "justification for --steal (printed in the audit trail)")
	return cmd
}

func runClaim(scopeRaw, intent, stealID, stealReason string) error {
	if intent == "" {
		Fatalf(2, "claim: --intent is required")
	}
	scope, err := overlap.Parse(scopeRaw)
	if err != nil {
		Fatalf(2, "claim: %v", err)
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()

	// Policy check: if the scope path matches a risky entry, an anchor is required.
	if rt.Policy.RequiresAnchor(scope.Path) && scope.Anchor.Kind == overlap.AnchorWhole {
		Fatalf(1, "claim: anchor required for risky file %q (policy: .comms/policy.txt). Add #L<n>-<m> or #<symbol>.", scope.Path)
	}

	// Steal validation: --reason required, --steal must resolve to an active claim.
	var displaceID string
	if stealID != "" {
		if stealReason == "" {
			Fatalf(2, "claim: --steal requires --reason")
		}
		target := rt.State.ClaimByID(stealID)
		if target == nil {
			Fatalf(2, "claim: --steal %q does not match any active claim", stealID)
		}
		displaceID = target.ID
	}

	// Conflict detection: other actors' overlapping claims (excluding the one we're stealing).
	conflicts := rt.State.ConflictsFor(scope, rt.Actor)
	if displaceID != "" {
		conflicts = filterOutClaim(conflicts, displaceID)
	}
	if len(conflicts) > 0 {
		render.WriteConflict(os.Stderr, render.Conflict{
			AttemptedScope:  scope.String(),
			AttemptedActor:  rt.Actor,
			AttemptedIntent: intent,
			Holders:         conflicts,
		})
		os.Exit(1)
	}

	// Build the claim event.
	now := time.Now().UTC()
	data := map[string]interface{}{"intent": intent}
	stampActiveCommsSession(rt, data)
	if displaceID != "" {
		data["steals"] = displaceID
		data["steal_reason"] = stealReason
		// Best-effort arbitrator marker: the human's identity. We use COMMS_ARBITRATOR
		// if set, otherwise fall back to $USER for audit purposes.
		if a := os.Getenv("COMMS_ARBITRATOR"); a != "" {
			data["arbitrator"] = a
		} else if u := os.Getenv("USER"); u != "" {
			data["arbitrator"] = u
		}
	}
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeClaim,
		Scope: []string{scope.String()},
		Data:  data,
	}
	if err := rt.Append(ev); err != nil {
		return err
	}

	// Print the claim ID + short summary on stdout.
	if displaceID != "" {
		fmt.Printf("@%s claimed %s (stole %s from prior holder)\n  ID: %s\n",
			rt.Actor, scope.String(), short(displaceID), ev.ID)
	} else {
		fmt.Printf("@%s claimed %s\n  ID: %s\n", rt.Actor, scope.String(), ev.ID)
	}
	return nil
}

func filterOutClaim(in []*state.Claim, id string) []*state.Claim {
	out := in[:0]
	for _, c := range in {
		if c.ID == id {
			continue
		}
		out = append(out, c)
	}
	return out
}
