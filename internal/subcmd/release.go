package subcmd

import (
	"fmt"
	"os"
	"strings"
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
	modes := 0
	if len(args) == 1 {
		modes++
	}
	if latest {
		modes++
	}
	if allMine {
		modes++
	}
	if modes > 1 {
		return fmt.Errorf("release: <id>, --latest, and --all-mine are mutually exclusive")
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

// releaseAllClaimsForActor force-releases every active claim held by `actor`
// in one locked, single-fold pass. It is the engine behind the UI's "release
// all of an agent's claims" control: when an agent dies holding many locks the
// operator frees the whole set in one click instead of releasing each claim by
// hand. Unlike retire it leaves the actor on the roster — it only frees files.
// Releasing another actor's claims is an arbitrated release, so `reason` is
// auto-filled when blank and every event records the original holder for audit.
// Returns the number of claims released. Caller MUST hold the flock.
func releaseAllClaimsForActor(rt *Runtime, actor, reason, result string) (int, error) {
	actor = strings.TrimSpace(actor)
	if actor == "" {
		return 0, fmt.Errorf("release: actor is required")
	}
	targets := rt.State.ActiveClaimsByActor(actor)
	if len(targets) == 0 {
		return 0, fmt.Errorf("release: @%s holds no active claims", actor)
	}
	reason = strings.TrimSpace(reason)
	result = strings.TrimSpace(result)
	// Operator (rt.Actor) releasing someone else's claims is arbitrated and so
	// requires a reason; supply a sensible default rather than rejecting the call.
	if actor != rt.Actor && reason == "" {
		reason = "all claims released from UI by @" + rt.Actor
	}
	if result == "" {
		result = "released from UI"
	}
	if err := appendReleaseEvent(rt, targets, reason, result); err != nil {
		return 0, err
	}
	return len(targets), nil
}

func appendReleaseEvent(rt *Runtime, targets []*state.Claim, reason, result string) error {
	now := time.Now().UTC()
	evs := make([]event.Event, 0, len(targets))
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
		evs = append(evs, ev)
	}
	// Append every release and fold once, not once per claim — shorter lock hold.
	return rt.AppendBatch(evs)
}
