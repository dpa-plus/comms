package subcmd

import (
	"fmt"
	"os"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewReleaseCmd builds `comms release`.
//
// One of {id arg, --latest, --all-mine} must be supplied. Releasing someone
// else's claim requires --reason (= arbitrated release, recorded in audit).
func NewReleaseCmd() *cobra.Command {
	var (
		latest  bool
		allMine bool
		reason  string
		result  string
	)
	cmd := &cobra.Command{
		Use:   "release [<id>|--latest|--all-mine] [--result <text>]",
		Short: "Close one or more of your claims",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRelease(args, latest, allMine, reason, result)
		},
	}
	cmd.Flags().BoolVar(&latest, "latest", false, "release your most recently opened claim")
	cmd.Flags().BoolVar(&allMine, "all-mine", false, "release every claim held by COMMS_ACTOR")
	cmd.Flags().StringVar(&reason, "reason", "", "required when releasing another actor's claim (arbitrated release)")
	cmd.Flags().StringVar(&result, "result", "", "short outcome string (PR number, summary, etc.)")
	return cmd
}

func runRelease(args []string, latest, allMine bool, reason, result string) error {
	if (len(args) == 0) && !latest && !allMine {
		Fatalf(2, "release: must supply <id>, --latest, or --all-mine")
	}
	if latest && allMine {
		Fatalf(2, "release: --latest and --all-mine are mutually exclusive")
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()

	var targets []*state.Claim
	switch {
	case len(args) == 1:
		c := rt.State.ClaimByID(args[0])
		if c == nil {
			Fatalf(2, "release: no active claim matches %q", args[0])
		}
		targets = []*state.Claim{c}
	case latest:
		c := rt.State.LatestClaimByActor(rt.Actor)
		if c == nil {
			Fatalf(2, "release: @%s holds no active claims", rt.Actor)
		}
		targets = []*state.Claim{c}
	case allMine:
		targets = rt.State.ActiveClaimsByActor(rt.Actor)
		if len(targets) == 0 {
			Fatalf(2, "release: @%s holds no active claims", rt.Actor)
		}
	}

	if err := appendReleaseEvent(rt, targets, reason, result); err != nil {
		return err
	}
	for _, c := range targets {
		isArbitrated := c.Actor != rt.Actor

		if isArbitrated {
			fmt.Printf("Released @%s's claim %s (arbitrated by @%s, reason: %s)\n", c.Actor, short(c.ID), rt.Actor, reason)
		} else {
			fmt.Printf("Released claim %s (%s)\n", short(c.ID), c.Scope.String())
		}
	}
	return nil
}

func appendReleaseEvent(rt *Runtime, targets []*state.Claim, reason, result string) error {
	now := time.Now().UTC()
	for _, c := range targets {
		isArbitrated := c.Actor != rt.Actor
		if isArbitrated && reason == "" {
			return fmt.Errorf("release: closing @%s's claim %s requires --reason (arbitrated release)", c.Actor, short(c.ID))
		}

		data := map[string]interface{}{
			"refs": []interface{}{c.ID},
		}
		if result != "" {
			data["result"] = result
		}
		if isArbitrated {
			data["reason"] = reason
			data["original_actor"] = c.Actor
			if u := os.Getenv("COMMS_ARBITRATOR"); u != "" {
				data["arbitrator"] = u
			} else if u := os.Getenv("USER"); u != "" {
				data["arbitrator"] = u
			}
		} else if reason != "" {
			data["reason"] = reason
		}
		if c.SessionID != "" {
			data["comms_session_id"] = c.SessionID
			data["comms_session_name"] = c.SessionName
		} else {
			stampActiveCommsSession(rt, data)
		}

		ev := event.Event{
			TS:    now,
			ID:    event.NewID(now),
			Actor: rt.Actor,
			Type:  event.TypeRelease,
			Data:  data,
		}
		if err := rt.Append(ev); err != nil {
			return err
		}
	}
	return nil
}
