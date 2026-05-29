package subcmd

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/overlap"
	"github.com/spf13/cobra"
)

// NewLogCmd builds `comms log`. Filterable view of the raw event stream.
func NewLogCmd() *cobra.Command {
	var (
		actor    string
		since    string
		scope    string
		types    string
		category string
		asJSON   bool
	)
	cmd := &cobra.Command{
		Use:   "log",
		Short: "Print filtered event log entries",
		RunE: func(cmd *cobra.Command, args []string) error {
			return runLog(actor, since, scope, types, category, asJSON)
		},
	}
	cmd.Flags().StringVar(&actor, "actor", "", "filter to events by this actor")
	cmd.Flags().StringVar(&since, "since", "24h", "lookback window")
	cmd.Flags().StringVar(&scope, "scope", "", "filter to events whose scope overlaps this path")
	cmd.Flags().StringVar(&types, "type", "", "comma-separated event types (hello,claim,release,note,finding)")
	cmd.Flags().StringVar(&category, "category", "", "for findings: filter by category (bug/fix/ship/decision/gotcha)")
	cmd.Flags().BoolVar(&asJSON, "json", false, "raw JSONL output")
	return cmd
}

func runLog(actor, since, scope, types, category string, asJSON bool) error {
	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		return err
	}
	defer rt.Close()

	dur, err := parseDuration(since)
	if err != nil {
		Fatalf(2, "log: %v", err)
	}
	cutoff := time.Now().Add(-dur)

	typeSet, err := parseTypeSet(types)
	if err != nil {
		Fatalf(2, "log: %v", err)
	}
	if err := validateLogCategory(category); err != nil {
		Fatalf(2, "log: %v", err)
	}
	var scopeFilter *overlap.Scope
	if scope != "" {
		s, err := overlap.Parse(scope)
		if err != nil {
			Fatalf(2, "log: %v", err)
		}
		scopeFilter = &s
	}

	enc := json.NewEncoder(os.Stdout)
	for _, ev := range rt.Events {
		if ev.TS.Before(cutoff) {
			continue
		}
		if actor != "" && ev.Actor != actor {
			continue
		}
		if typeSet != nil {
			if _, ok := typeSet[string(ev.Type)]; !ok {
				continue
			}
		}
		if scopeFilter != nil {
			if !eventScopeOverlaps(ev, *scopeFilter) {
				continue
			}
		}
		if category != "" && ev.Type == event.TypeFinding {
			if cat, _ := ev.Data["category"].(string); cat != category {
				continue
			}
		} else if category != "" && ev.Type != event.TypeFinding {
			continue
		}
		if asJSON {
			if err := enc.Encode(ev); err != nil {
				return err
			}
		} else {
			printEventHuman(ev)
		}
	}
	return nil
}

func parseTypeSet(s string) (map[string]struct{}, error) {
	if s == "" {
		return nil, nil
	}
	out := make(map[string]struct{})
	for _, p := range strings.Split(s, ",") {
		t := strings.TrimSpace(p)
		switch event.Type(t) {
		case event.TypeHello, event.TypeClaim, event.TypeRelease, event.TypeNote, event.TypeFinding:
			out[t] = struct{}{}
		default:
			return nil, fmt.Errorf("unknown event type %q; choose hello, claim, release, note, or finding", t)
		}
	}
	return out, nil
}

func validateLogCategory(category string) error {
	if category == "" {
		return nil
	}
	if _, ok := findCategories[category]; !ok {
		return fmt.Errorf("unknown category %q; choose bug, fix, ship, decision, or gotcha", category)
	}
	return nil
}

func eventScopeOverlaps(ev event.Event, want overlap.Scope) bool {
	for _, raw := range ev.Scope {
		s, err := overlap.Parse(raw)
		if err != nil {
			continue
		}
		if overlap.Scopes(s, want) {
			return true
		}
	}
	return false
}

func printEventHuman(ev event.Event) {
	ts := ev.TS.Local().Format("01-02 15:04:05")
	switch ev.Type {
	case event.TypeHello:
		fmt.Printf("%s  hello    @%s\n", ts, ev.Actor)
	case event.TypeClaim:
		intent, _ := ev.Data["intent"].(string)
		steal, _ := ev.Data["steals"].(string)
		if steal != "" {
			fmt.Printf("%s  claim    @%s  %s  %q  (stole %s)\n", ts, ev.Actor, joinScope(ev.Scope), intent, short(steal))
		} else {
			fmt.Printf("%s  claim    @%s  %s  %q\n", ts, ev.Actor, joinScope(ev.Scope), intent)
		}
	case event.TypeRelease:
		result := eventSummary(ev)
		orig, _ := ev.Data["original_actor"].(string)
		if orig != "" && orig != ev.Actor {
			fmt.Printf("%s  release  @%s arbitrated @%s's claim  %q\n", ts, ev.Actor, orig, result)
		} else {
			fmt.Printf("%s  release  @%s  %q\n", ts, ev.Actor, result)
		}
	case event.TypeNote:
		body, _ := ev.Data["body"].(string)
		fmt.Printf("%s  note     @%s  %s\n", ts, ev.Actor, body)
	case event.TypeFinding:
		cat, _ := ev.Data["category"].(string)
		sum, _ := ev.Data["summary"].(string)
		fmt.Printf("%s  finding  @%s  [%s] %s\n", ts, ev.Actor, cat, sum)
	}
}

func joinScope(s []string) string {
	if len(s) == 0 {
		return "-"
	}
	return strings.Join(s, ",")
}

func short(id string) string {
	if len(id) > 6 {
		return id[:6]
	}
	return id
}
