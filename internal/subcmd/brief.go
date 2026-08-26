package subcmd

import (
	"fmt"
	"strings"

	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewBriefCmd builds `comms brief`.
func NewBriefCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "brief <slug>",
		Short: "Everything you need before starting a task",
		Long: `What to read before you touch a task.

An arrow in the graph is not just an ordering: it records what the later task
CONSUMES from the earlier one, and the decisions the earlier agent made while
building it. Those decisions are the expensive part: without them you will
re-decide questions that were already settled and argued, and quite possibly
differently.

So this prints the task, what it is built on, the interface or artifact it
consumes, and the notes whoever built that wrote down at the time.`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error { return runBrief(args[0]) },
	}
}

func runBrief(slug string) error {
	slug = validateSlug("id", slug)
	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		return err
	}
	defer func() { _ = rt.Close() }()

	t := rt.State.Tasks[slug]
	if t == nil {
		Fatalf(2, "brief: no task %q", slug)
	}

	fmt.Printf("%s  %s\n", t.ID, t.Title)
	meta := []string{string(t.Phase)}
	if t.Size != "" {
		meta = append(meta, "size "+t.Size)
	}
	meta = append(meta, fmt.Sprintf("%d/%d slots taken", len(t.Doers), t.Slots))
	if t.Rejections > 0 {
		meta = append(meta, fmt.Sprintf("sent back %d time(s)", t.Rejections))
	}
	fmt.Printf("  %s\n", strings.Join(meta, " · "))

	// Where the real-world context lives. comms deliberately does not fetch it:
	// it is not this tool's data, and the agent asking has the tooling already.
	if t.Ref != "" {
		fmt.Printf("\nCONTEXT\n  %s\n", refHint(t.Ref))
	}
	if len(t.Checks) > 0 {
		fmt.Printf("\nMUST PASS BEFORE IT CAN BE MARKED DONE\n  %s\n", strings.Join(t.Checks, ", "))
	}

	incoming := rt.State.EdgesInto(slug)
	if len(incoming) == 0 {
		fmt.Printf("\nNothing comes before this. No inherited context.\n")
	}
	for _, e := range incoming {
		src := rt.State.Tasks[e.From]
		if src == nil {
			continue
		}
		status := string(src.Phase)
		if src.Independence != "" {
			status += ", " + src.Independence
		}
		fmt.Printf("\nBUILT ON  %s  (%s)\n", src.Title, status)
		if e.Kind == state.EdgeSequence {
			fmt.Printf("  ORDER ONLY   %s\n", firstNonEmpty(e.Provides, "nothing is consumed across this edge"))
			continue
		}
		fmt.Printf("  YOU CONSUME  %s\n", firstNonEmpty(e.Provides, "(nobody recorded what: ask before relying on it)"))
		if len(src.Notes) > 0 {
			fmt.Printf("  DECISIONS YOU INHERIT\n")
			for _, n := range src.Notes {
				fmt.Printf("    - %s\n", n)
			}
		}
	}

	if t.Phase == state.PhaseReview {
		fmt.Printf("\nTHIS IS WAITING FOR YOU TO VERIFY IT\n")
		fmt.Printf("  finished by %s. Read the task and the diff, not their session.\n", t.Did)
		if len(t.Notes) > 0 {
			fmt.Printf("  WHAT THEY DECIDED, AND WHY\n")
			for _, n := range t.Notes {
				fmt.Printf("    - %s\n", n)
			}
		}
		fmt.Printf("  A finding has to be checkable: an input that breaks it, a line that\n")
		fmt.Printf("  contradicts the spec, a case the tests miss. One verdict, no debate.\n")
	}

	if len(t.Findings) > 0 {
		fmt.Printf("\nSENT BACK FOR\n")
		for _, f := range t.Findings {
			fmt.Printf("  - %s\n", f.Claim)
			if f.Evidence != "" {
				fmt.Printf("    check: %s\n", f.Evidence)
			}
		}
	}

	if out := rt.State.EdgesOutOf(slug); len(out) > 0 {
		names := make([]string, 0, len(out))
		for _, e := range out {
			if dep := rt.State.Tasks[e.To]; dep != nil {
				names = append(names, dep.ID)
			}
		}
		fmt.Printf("\nFINISHING THIS UNBLOCKS  %s\n", strings.Join(names, ", "))
		fmt.Printf("  (once it has been verified, not when it is merely done)\n")
	}
	return nil
}

// refHint prints an opaque reference and nothing else.
//
// It used to special-case one scheme and print the command that resolves it,
// which contradicted both this comment and docs/PROTOCOL.md ("comms stores it
// and never resolves it"), and hard-wired one team's internal tool into a
// public repository. An agent that owns a reference already knows how to read
// it; where a scheme maps to a command belongs in that team's own skill, not
// in here.
func refHint(ref string) string {
	return ref
}
