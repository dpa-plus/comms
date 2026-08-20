package subcmd

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewNextCmd builds `comms next`.
func NewNextCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "next",
		Short: "What you could pick up right now",
		Long: `What is actually available to you, in the order worth caring about.

Work waiting to be verified comes first. It is finished work that is holding up
everything downstream, and it is cheap: read the task, the handover notes and the
diff. You will not be offered anything you did yourself — the whole value of a
second pair of eyes is that they are somebody else's.

Then tasks nobody has claimed, then tasks with a free slot you could join.`,
		Args: cobra.NoArgs,
		RunE: func(_ *cobra.Command, _ []string) error { return runNext() },
	}
}

func runNext() error {
	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		return err
	}
	defer func() { _ = rt.Close() }()

	me := baseName(rt.Actor)
	var toVerify, toStart, toJoin []*state.Task
	for _, t := range rt.State.SortedTasks() {
		switch t.Phase {
		case state.PhaseReview:
			if baseName(t.Did) != me {
				toVerify = append(toVerify, t)
			}
		case state.PhaseReady:
			toStart = append(toStart, t)
		case state.PhaseDoing:
			if t.FreeSlots() > 0 && !containsActor(t.Doers, rt.Actor) {
				toJoin = append(toJoin, t)
			}
		}
	}

	if len(toVerify)+len(toStart)+len(toJoin) == 0 {
		fmt.Println("Nothing is available to you right now.")
		if blocked := countPhase(rt.State, state.PhaseBlocked); blocked > 0 {
			fmt.Printf("  %d task(s) are waiting on work that has not been verified yet.\n", blocked)
		}
		if own := ownReviews(rt.State, me); own > 0 {
			fmt.Printf("  %d task(s) you finished are waiting for a verifier who is not you.\n", own)
		}
		return nil
	}

	for _, t := range toVerify {
		fmt.Printf("VERIFY  %-14s %s\n", t.ID, t.Title)
		fmt.Printf("        done by %s · %s\n", t.Did, briefHint(t))
	}
	for _, t := range toStart {
		fmt.Printf("START   %-14s %s\n", t.ID, t.Title)
		fmt.Printf("        %s · %d slot(s) · %s\n", sizeOrDash(t.Size), t.Slots, briefHint(t))
	}
	for _, t := range toJoin {
		fmt.Printf("JOIN    %-14s %s\n", t.ID, t.Title)
		fmt.Printf("        %s already on it · %d slot(s) free\n", strings.Join(t.Doers, ", "), t.FreeSlots())
	}
	return nil
}

func briefHint(t *state.Task) string { return "comms brief " + t.ID }

func sizeOrDash(s string) string {
	if s == "" {
		return "size unset"
	}
	return "size " + s
}

func containsActor(list []string, a string) bool {
	for _, v := range list {
		if v == a || baseName(v) == baseName(a) {
			return true
		}
	}
	return false
}

func countPhase(s *state.State, p state.TaskPhase) int {
	n := 0
	for _, t := range s.Tasks {
		if t.Phase == p {
			n++
		}
	}
	return n
}

func ownReviews(s *state.State, me string) int {
	n := 0
	for _, t := range s.Tasks {
		if t.Phase == state.PhaseReview && baseName(t.Did) == me {
			n++
		}
	}
	return n
}

func readAllStdin() ([]byte, error) { return io.ReadAll(os.Stdin) }
