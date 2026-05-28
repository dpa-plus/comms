package subcmd

import (
	"fmt"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/spf13/cobra"
)

// NewSessionCmd builds admin commands for the active comms session roster.
func NewSessionCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "session",
		Short: "Manage active comms session actors",
		Long: `Manage the active comms session roster without editing the append-only log.

Retiring an actor appends an audit event that removes that actor from the
active-session view and releases any claims it still holds. It does not delete
historical log rows.

Leadership can be transferred to exactly one active actor. The leader's only
extra privilege is posting --priority notes/findings.`,
	}
	cmd.AddCommand(newSessionRetireCmd(), newSessionLeadCmd())
	return cmd
}

func newSessionRetireCmd() *cobra.Command {
	var reason string
	cmd := &cobra.Command{
		Use:   "retire <actor>",
		Short: "Remove an actor from active sessions",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSessionRetire(args[0], reason)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "audit reason for retiring the actor")
	return cmd
}

func newSessionLeadCmd() *cobra.Command {
	var reason string
	cmd := &cobra.Command{
		Use:   "lead [<actor>]",
		Short: "Make one active actor the leader",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			target := ""
			if len(args) == 1 {
				target = args[0]
			}
			return runSessionLead(target, reason)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "audit reason for leader transfer")
	return cmd
}

func runSessionRetire(target, reason string) error {
	target = strings.TrimSpace(target)
	if target == "" {
		return fmt.Errorf("session retire: actor is required")
	}
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()
	released, err := appendSessionRetire(rt, target, reason)
	if err != nil {
		return err
	}
	fmt.Printf("Retired @%s from active sessions; released %d claim%s. History remains in the append-only log.\n", target, released, pluralS(released))
	return nil
}

func runSessionLead(target, reason string) error {
	target = strings.TrimSpace(target)
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()
	if target == "" {
		target = rt.Actor
	}
	if err := appendLeaderTransfer(rt, target, reason); err != nil {
		return err
	}
	fmt.Printf("@%s is now the comms leader.\n", target)
	return nil
}

func appendSessionRetire(rt *Runtime, target, reason string) (int, error) {
	target = strings.TrimSpace(target)
	if target == "" {
		return 0, fmt.Errorf("session retire: actor is required")
	}
	claims := rt.State.ActiveClaimsByActor(target)
	if rt.State.Sessions[target] == nil && len(claims) == 0 {
		return 0, fmt.Errorf("session retire: @%s has no active session or claims", target)
	}
	if reason = strings.TrimSpace(reason); reason == "" {
		reason = "retired from active sessions"
	}
	refs := make([]interface{}, 0, len(claims))
	for _, c := range claims {
		refs = append(refs, c.ID)
	}
	now := time.Now().UTC()
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeRelease,
		Data: map[string]interface{}{
			"refs":           refs,
			"session_retire": true,
			"retired_actor":  target,
			"reason":         reason,
		},
	}
	if err := rt.Append(ev); err != nil {
		return 0, err
	}
	return len(refs), nil
}

func appendLeaderTransfer(rt *Runtime, target, reason string) error {
	target = strings.TrimSpace(target)
	if target == "" {
		target = rt.Actor
	}
	if rt.State.Sessions[target] == nil {
		return fmt.Errorf("session lead: @%s is not active; run `COMMS_ACTOR=%s comms hello --label \"...\"` first", target, target)
	}
	if reason = strings.TrimSpace(reason); reason == "" {
		reason = "leader transfer"
	}
	now := time.Now().UTC()
	return rt.Append(event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeRelease,
		Data: map[string]interface{}{
			"leader_transfer": true,
			"leader_actor":    target,
			"reason":          reason,
		},
	})
}
