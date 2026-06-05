package subcmd

import (
	"fmt"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/spf13/cobra"
)

// findCategories is the closed set per the plan's "5 categories" rule.
var findCategories = map[string]string{
	"bug":      "an open problem that needs fixing",
	"fix":      "a problem you just resolved",
	"ship":     "something now in production or released",
	"decision": "an architectural choice the team should remember",
	"gotcha":   "a non-obvious trap; persistent reminder for future agents",
}

// NewFindCmd builds `comms find <category> "<summary>" [--ref kind:value ...]`.
func NewFindCmd() *cobra.Command {
	var refs []string
	var priority bool
	cmd := &cobra.Command{
		Use:   `find [--priority] <bug|fix|ship|decision|gotcha> "<summary>"`,
		Short: "Record a finding (bug, fix, ship, decision, or gotcha)",
		Long: `Record a finding in one of five categories:

  bug       — an open problem that needs fixing
  fix       — a problem you just resolved
  ship      — something now in production or released
  decision  — an architectural choice the team should remember
  gotcha    — a non-obvious trap; persistent reminder for future agents

Use --ref kind:value (repeatable) to attach commit hashes, paths, PR
numbers, issue links. Example:

  comms find fix "leads now sourced only from tracker overlay" \
    --ref path:frontend/src/lib/aggregate.ts \
    --ref commit:cece752 \
    --ref pr:#321`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runFind(args[0], args[1], refs, priority)
		},
	}
	cmd.Flags().StringArrayVar(&refs, "ref", nil, "kind:value reference (repeatable)")
	cmd.Flags().BoolVar(&priority, "priority", false, "leader-only: pin this finding as high priority in status/UI")
	return cmd
}

func runFind(category, summary string, refs []string, priority bool) error {
	if _, ok := findCategories[category]; !ok {
		Fatalf(2, "find: invalid category %q. Choose: bug, fix, ship, decision, gotcha.", category)
	}
	if summary == "" {
		Fatalf(2, "find: summary is empty")
	}
	// The summary is rendered raw via %s in status/log output, so reject any
	// control character (terminal-injection vector) and cap the length.
	if err := rejectControlText("finding summary", summary, 280); err != nil {
		Fatalf(2, "find: %v", err)
	}
	parsedRefs, err := parseRefs(refs)
	if err != nil {
		Fatalf(2, "find: %v", err)
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer rt.Close()
	if priority {
		requireLeader(rt)
	}

	refsForJSON := make([]map[string]string, len(parsedRefs))
	for i, r := range parsedRefs {
		refsForJSON[i] = map[string]string{"kind": r.kind, "value": r.value}
	}

	now := time.Now().UTC()
	data := map[string]interface{}{
		"category": category,
		"summary":  summary,
		"refs":     refsForJSON,
	}
	if priority {
		data["priority"] = true
	}
	stampActiveCommsSession(rt, data)
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeFinding,
		Data:  data,
	}
	if err := rt.Append(ev); err != nil {
		return err
	}
	prefix := ""
	if priority {
		prefix = "PRIORITY "
	}
	fmt.Printf("%s[%s] @%s: %s\n", prefix, category, rt.Actor, summary)
	if len(parsedRefs) > 0 {
		for _, r := range parsedRefs {
			fmt.Printf("  ref: %s:%s\n", r.kind, r.value)
		}
	}
	return nil
}

type kindValue struct{ kind, value string }

func parseRefs(raw []string) ([]kindValue, error) {
	out := make([]kindValue, 0, len(raw))
	for _, r := range raw {
		colon := strings.IndexByte(r, ':')
		if colon < 1 || colon == len(r)-1 {
			return nil, fmt.Errorf("ref %q: expected kind:value", r)
		}
		kind := r[:colon]
		value := r[colon+1:]
		if kind == "" || value == "" {
			return nil, fmt.Errorf("ref %q: kind or value empty", r)
		}
		for _, c := range kind + value {
			// Reject C0 (<0x20), DEL (0x7f), and C1 (0x80-0x9f) — the same control
			// range every other free-text field is held to (rejectControlText), so
			// ref values can't smuggle escape bytes into the log.
			if c < 0x20 || c == 0x7f || (c >= 0x80 && c <= 0x9f) {
				return nil, fmt.Errorf("ref %q: contains control character", r)
			}
		}
		out = append(out, kindValue{kind: kind, value: value})
	}
	return out, nil
}
