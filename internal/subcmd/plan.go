package subcmd

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// planFile is what an agent hands over when it has decomposed an objective.
type planFile struct {
	Tasks []struct {
		ID     string   `json:"id"`
		Title  string   `json:"title"`
		Size   string   `json:"size"`
		Slots  int      `json:"slots"`
		Checks []string `json:"checks"`
		Ref    string   `json:"ref"`
		After  []string `json:"after"` // shorthand for a sequence edge from each
	} `json:"tasks"`
	Edges []struct {
		From     string `json:"from"`
		To       string `json:"to"`
		Kind     string `json:"kind"`
		Provides string `json:"provides"`
	} `json:"edges"`
}

// NewPlanCmd builds `comms plan`.
func NewPlanCmd() *cobra.Command {
	var from string
	cmd := &cobra.Command{
		Use:   "plan --from <plan.json>",
		Short: "Append a whole decomposition at once, or nothing at all",
		Long: `Append a whole plan in one write.

Every task and every edge is validated first: slugs, sizes, unknown endpoints,
duplicate edges and dependency cycles. If any of it is wrong, nothing is written.
A half-created graph is worse than none: agents would start picking work off a
plan that does not say what its author meant.

  {
    "tasks": [
      {"id": "db-schema", "title": "Add session tables", "size": "S",
       "checks": ["test"], "ref": "tracker:PROJ-1234"},
      {"id": "auth-api",  "title": "Build session create / refresh / revoke",
       "size": "L", "slots": 2, "after": ["db-schema"]}
    ],
    "edges": [
      {"from": "auth-api", "to": "login-ui", "kind": "interface",
       "provides": "POST /session: httpOnly cookie, no bearer token"}
    ]
  }

"after" is shorthand for an ordering-only edge. Use the "edges" list when the
later task actually consumes something, and say what in "provides": that is
what reaches whoever picks it up, and what decides how much a rework costs.`,
		Args: cobra.NoArgs,
		RunE: func(_ *cobra.Command, _ []string) error { return runPlan(from) },
	}
	cmd.Flags().StringVar(&from, "from", "", "path to the plan JSON, or - for stdin (required)")
	return cmd
}

func runPlan(from string) error {
	if from == "" {
		Fatalf(2, "plan: --from is required")
	}
	raw, err := readPlanBytes(from)
	if err != nil {
		Fatalf(2, "plan: %v", err)
	}
	var p planFile
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil {
		Fatalf(2, "plan: %v", err)
	}
	if len(p.Tasks) == 0 && len(p.Edges) == 0 {
		Fatalf(2, "plan: nothing to append")
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer func() { _ = rt.Close() }()

	// ---- validate everything before writing a single byte ----
	seen := map[string]bool{}
	for _, t := range p.Tasks {
		id := validateSlug("id", t.ID)
		if seen[id] {
			Fatalf(2, "plan: %q appears twice", id)
		}
		seen[id] = true
		if t.Title == "" && rt.State.Tasks[id] == nil {
			Fatalf(2, "plan: %q needs a title", id)
		}
		if err := rejectControlText("task title", t.Title, 200); err != nil {
			Fatalf(2, "plan: %v", err)
		}
		if t.Size != "" && !taskSizes[strings.ToUpper(t.Size)] {
			Fatalf(2, "plan: %q has size %q; use S, M or L", id, t.Size)
		}
		if t.Slots < 0 {
			Fatalf(2, "plan: %q has a negative slot count", id)
		}
	}
	known := func(id string) bool { return seen[id] || rt.State.Tasks[id] != nil }

	type edge struct{ from, to, kind, provides string }
	var edges []edge
	addEdge := func(from, to, kind, provides string) {
		from, to = validateSlug("from", from), validateSlug("to", to)
		if from == to {
			Fatalf(2, "plan: %q cannot come after itself", from)
		}
		if !known(from) || !known(to) {
			missing := from
			if known(from) {
				missing = to
			}
			Fatalf(2, "plan: edge %s -> %s references %q, which this plan does not declare and the log does not have", from, to, missing)
		}
		switch kind {
		case state.EdgeInterface, state.EdgeArtifact, state.EdgeSequence:
		case "":
			kind = state.EdgeSequence
		default:
			Fatalf(2, "plan: edge %s -> %s has kind %q; use interface, artifact or sequence", from, to, kind)
		}
		if err := rejectControlText("provides", provides, 240); err != nil {
			Fatalf(2, "plan: %v", err)
		}
		edges = append(edges, edge{from, to, kind, provides})
	}
	for _, t := range p.Tasks {
		for _, a := range t.After {
			addEdge(a, t.ID, state.EdgeSequence, "")
		}
	}
	for _, e := range p.Edges {
		addEdge(e.From, e.To, strings.ToLower(strings.TrimSpace(e.Kind)), e.Provides)
	}

	pairs := make([][2]string, 0, len(edges))
	dedupe := map[string]bool{}
	for _, e := range edges {
		key := e.from + "\x00" + e.to
		if dedupe[key] {
			Fatalf(2, "plan: the edge %s -> %s is given twice", e.from, e.to)
		}
		dedupe[key] = true
		pairs = append(pairs, [2]string{e.from, e.to})
	}
	if cycle := wouldCycle(rt.State, pairs); cycle != "" {
		Fatalf(1, "plan: rejected, a task would depend on itself (%s)", cycle)
	}

	// ---- one batch, one fold, all or nothing ----
	evs := make([]event.Event, 0, len(p.Tasks)+len(edges))
	for _, t := range p.Tasks {
		data := map[string]interface{}{"task": t.ID}
		if t.Title != "" {
			data["title"] = t.Title
		}
		if t.Size != "" {
			data["size"] = strings.ToUpper(t.Size)
		}
		if t.Slots > 0 {
			data["slots"] = t.Slots
		}
		if len(t.Checks) > 0 {
			data["checks"] = toIfaceSlice(t.Checks)
		}
		if t.Ref != "" {
			data["ref"] = t.Ref
		}
		stampActiveCommsSession(rt, data)
		evs = append(evs, newTaskEvent(rt, event.TypeTask, data))
	}
	for _, e := range edges {
		data := map[string]interface{}{"from": e.from, "to": e.to, "kind": e.kind}
		if e.provides != "" {
			data["provides"] = e.provides
		}
		stampActiveCommsSession(rt, data)
		evs = append(evs, newTaskEvent(rt, event.TypeTaskEdge, data))
	}
	if err := rt.AppendBatch(evs); err != nil {
		return err
	}

	fmt.Printf("%d task(s) and %d edge(s) appended as one write\n", len(p.Tasks), len(edges))
	var ready []string
	for _, t := range rt.State.SortedTasks() {
		if t.Phase == state.PhaseReady {
			ready = append(ready, t.ID)
		}
	}
	sort.Strings(ready)
	if len(ready) > 0 {
		fmt.Printf("ready to start: %s\n", strings.Join(ready, ", "))
	}
	return nil
}

func readPlanBytes(from string) ([]byte, error) {
	if from == "-" {
		return readAllStdin()
	}
	return os.ReadFile(from)
}
