package subcmd

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// Task ids are chosen by whoever writes the plan and are meant to be SAID —
// quoted between agents, typed into a claim, read off a dashboard. They are not
// ULIDs on purpose: on a real store of 11,000 events, 99.5% of ULIDs share their
// six-character short prefix, so no agent can refer to one unambiguously.
var taskSlugRe = regexp.MustCompile(`^[a-z][a-z0-9-]{1,31}$`)

var taskSizes = map[string]bool{"S": true, "M": true, "L": true}

func validateSlug(kind, slug string) string {
	s := strings.TrimSpace(slug)
	if !taskSlugRe.MatchString(s) {
		Fatalf(2, "task: %s %q must be 2-32 chars, lower-case letters, digits and hyphens, starting with a letter", kind, slug)
	}
	return s
}

// NewTaskCmd builds `comms task`.
func NewTaskCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "task",
		Short: "Declare work, order it, finish it, verify it",
		Long: `Work the graph.

A task is what should happen. An edge is the order two tasks must happen in and
what the later one consumes from the earlier. Every task has two steps: an agent
does it, then a DIFFERENT agent verifies it, and a task unblocks what comes
after it only once it has been verified, not merely finished.

Status is never written by hand. Ready, blocked, doing and closed are computed
from the log, and "who is on it" comes from live file claims tagged to the task.`,
	}
	cmd.AddCommand(newTaskAddCmd(), newTaskEdgeCmd(), newTaskDoneCmd(), newTaskReviewCmd(), newTaskShowCmd())
	return cmd
}

func newTaskAddCmd() *cobra.Command {
	var title, size, ref string
	var slots int
	var checks []string
	cmd := &cobra.Command{
		Use:   `add <slug> --title "<what should happen>"`,
		Short: "Declare a task",
		Long: `Declare a task, or restate one that already exists.

Re-running add with the same slug edits the description in place. It does not
touch the task's progress: re-titling verified work does not reopen it.

Write the title as an instruction, not a label: "Rotate refresh tokens on every
use", not "Token rotation". It is what another agent reads before picking it up.`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			return runTaskAdd(args[0], title, size, ref, slots, checks)
		},
	}
	cmd.Flags().StringVar(&title, "title", "", "what should happen, phrased as an instruction (required on first declaration)")
	cmd.Flags().StringVar(&size, "size", "", "S, M or L: a rough sense of scale for whoever picks it up. Advice, not a gate, and nothing refuses a claim on an L")
	cmd.Flags().IntVar(&slots, "slots", 0, "how many agents may work it at once (default 1)")
	cmd.Flags().StringArrayVar(&checks, "check", nil, "a check that must pass before this can be marked done (repeatable, e.g. --check test)")
	cmd.Flags().StringVar(&ref, "ref", "", "opaque reference to wherever the real context lives, e.g. tracker:PROJ-1234")
	return cmd
}

func runTaskAdd(slug, title, size, ref string, slots int, checks []string) error {
	slug = validateSlug("id", slug)
	if err := rejectControlText("task title", title, 200); err != nil {
		Fatalf(2, "task add: %v", err)
	}
	if size != "" {
		size = strings.ToUpper(size)
		if !taskSizes[size] {
			Fatalf(2, "task add: --size must be S, M or L")
		}
	}
	if slots < 0 {
		Fatalf(2, "task add: --slots cannot be negative")
	}
	if ref != "" {
		if err := rejectControlText("task ref", ref, 120); err != nil {
			Fatalf(2, "task add: %v", err)
		}
	}
	for _, c := range checks {
		if err := rejectControlText("check name", c, 40); err != nil {
			Fatalf(2, "task add: %v", err)
		}
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer func() { _ = rt.Close() }()

	if rt.State.Tasks[slug] == nil && title == "" {
		Fatalf(2, "task add: --title is required when declaring %q for the first time", slug)
	}

	data := map[string]interface{}{"task": slug}
	if title != "" {
		data["title"] = title
	}
	if size != "" {
		data["size"] = size
	}
	if slots > 0 {
		data["slots"] = slots
	}
	if len(checks) > 0 {
		data["checks"] = toIfaceSlice(checks)
	}
	if ref != "" {
		data["ref"] = ref
	}
	stampActiveCommsSession(rt, data)
	if err := rt.Append(newTaskEvent(rt, event.TypeTask, data)); err != nil {
		return err
	}
	fmt.Printf("task %s: %s\n", slug, firstNonEmpty(title, rt.State.Tasks[slug].Title))
	return nil
}

func newTaskEdgeCmd() *cobra.Command {
	var kind, provides string
	cmd := &cobra.Command{
		Use:   "edge <from> <to>",
		Short: "Say that one task comes after another",
		Long: `Record that <to> comes after <from>.

The kind decides what a rejection costs later:

  interface   <to> calls a surface <from> provides
  artifact    <to> uses a file or schema <from> produced
  sequence    ordering only: nothing flows across it

Reworking a task only forces a recheck of successors that CONSUME something from
it. A successor that merely follows in sequence is left alone. --provides is what
travels to whoever picks <to> up, so write what they will actually rely on.`,
		Args: cobra.ExactArgs(2),
		RunE: func(_ *cobra.Command, args []string) error {
			return runTaskEdge(args[0], args[1], kind, provides)
		},
	}
	cmd.Flags().StringVar(&kind, "kind", state.EdgeSequence, "interface, artifact or sequence")
	cmd.Flags().StringVar(&provides, "provides", "", "what the later task consumes from the earlier one")
	return cmd
}

func runTaskEdge(from, to, kind, provides string) error {
	from, to = validateSlug("from", from), validateSlug("to", to)
	if from == to {
		Fatalf(2, "task edge: a task cannot come after itself")
	}
	kind = strings.ToLower(strings.TrimSpace(kind))
	switch kind {
	case state.EdgeInterface, state.EdgeArtifact, state.EdgeSequence:
	default:
		Fatalf(2, "task edge: --kind must be interface, artifact or sequence")
	}
	if err := rejectControlText("provides", provides, 240); err != nil {
		Fatalf(2, "task edge: %v", err)
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer func() { _ = rt.Close() }()

	for _, id := range []string{from, to} {
		if rt.State.Tasks[id] == nil {
			Fatalf(2, "task edge: no task %q: declare it first with `comms task add %s --title ...`", id, id)
		}
	}
	if cycle := wouldCycle(rt.State, [][2]string{{from, to}}); cycle != "" {
		Fatalf(1, "task edge: %s would depend on itself (%s)", to, cycle)
	}

	data := map[string]interface{}{"from": from, "to": to, "kind": kind}
	if provides != "" {
		data["provides"] = provides
	}
	stampActiveCommsSession(rt, data)
	if err := rt.Append(newTaskEvent(rt, event.TypeTaskEdge, data)); err != nil {
		return err
	}
	fmt.Printf("%s comes after %s (%s)\n", to, from, kind)
	return nil
}

func newTaskDoneCmd() *cobra.Command {
	var notes, checks []string
	cmd := &cobra.Command{
		Use:   "done <slug> --note \"<a decision you made>\"",
		Short: "Say the implementation is finished and hand it to a verifier",
		Long: `Mark the implementation finished. The task is NOT closed: it now waits for a
different agent to verify it, and nothing downstream moves until that happens.

Notes are the handover. Write the decisions you made and why, not a summary of
the diff: the arguable choices are what a reviewer needs and cannot recover from
the code. They travel to whoever picks up the tasks that come after this one.

If the task declared checks, report each one. Work whose checks did not pass is
refused before review is offered.`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			return runTaskDone(args[0], notes, checks)
		},
	}
	cmd.Flags().StringArrayVar(&notes, "note", nil, "a decision you made and why (repeatable)")
	cmd.Flags().StringArrayVar(&checks, "check", nil, "result of a declared check, name=pass|fail (repeatable)")
	return cmd
}

func runTaskDone(slug string, notes, checks []string) error {
	slug = validateSlug("id", slug)
	for _, n := range notes {
		if err := rejectControlText("note", n, 400); err != nil {
			Fatalf(2, "task done: %v", err)
		}
	}
	results := map[string]interface{}{}
	for _, c := range checks {
		name, verdict, ok := strings.Cut(c, "=")
		if !ok {
			Fatalf(2, "task done: --check wants name=pass or name=fail, got %q", c)
		}
		results[strings.TrimSpace(name)] = strings.ToLower(strings.TrimSpace(verdict))
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer func() { _ = rt.Close() }()

	t := rt.State.Tasks[slug]
	if t == nil {
		Fatalf(2, "task done: no task %q", slug)
	}
	if t.Phase == state.PhaseClosed {
		Fatalf(2, "task done: %s is already verified", slug)
	}
	// Fail here rather than let the reducer silently refuse the event: the agent
	// should learn it is not done from the command it just ran.
	var failed []string
	for _, c := range t.Checks {
		if v, _ := results[c].(string); v != "pass" {
			failed = append(failed, c)
		}
	}
	if len(failed) > 0 {
		Fatalf(1, "task done: %s declares checks that have not passed: %s\n  report them with --check %s=pass",
			slug, strings.Join(failed, ", "), failed[0])
	}
	if len(notes) == 0 {
		fmt.Printf("note: %s has no handover notes, so the verifier will only see the diff\n", slug)
	}

	data := map[string]interface{}{"task": slug, "state": "done"}
	if len(notes) > 0 {
		data["notes"] = toIfaceSlice(notes)
	}
	if len(results) > 0 {
		data["checks"] = results
	}
	stampActiveCommsSession(rt, data)
	if err := rt.Append(newTaskEvent(rt, event.TypeTaskState, data)); err != nil {
		return err
	}
	fmt.Printf("%s is finished and waiting to be verified, by someone other than %s\n", slug, baseName(rt.Actor))
	return nil
}

func newTaskReviewCmd() *cobra.Command {
	var pass, fail bool
	var claims, evidence []string
	cmd := &cobra.Command{
		Use:   "review <slug> --pass | --fail",
		Short: "Verify someone else's finished work",
		Long: `Verify a task somebody else finished. You cannot verify your own.

Come to it fresh: read the task and the diff, not the author's session. A finding
must be checkable: an input that breaks it, a line that contradicts the spec, a
case the tests do not cover, because the rule downstream is "if the reason is
real, fix it, whoever gave it". An opinion cannot be acted on that way.

A verdict is one shot. Do not iterate to agreement with the author; if you
disagree after this, escalate to a third agent or to a human.`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			return runTaskReview(args[0], pass, fail, claims, evidence)
		},
	}
	cmd.Flags().BoolVar(&pass, "pass", false, "the work holds up")
	cmd.Flags().BoolVar(&fail, "fail", false, "send it back: requires at least one --finding")
	cmd.Flags().StringArrayVar(&claims, "finding", nil, "what is wrong (repeatable, pairs with --evidence)")
	cmd.Flags().StringArrayVar(&evidence, "evidence", nil, "how to check that finding is real (repeatable, same order)")
	return cmd
}

func runTaskReview(slug string, pass, fail bool, claims, evidence []string) error {
	slug = validateSlug("id", slug)
	if pass == fail {
		Fatalf(2, "task review: choose exactly one of --pass or --fail")
	}
	if fail && len(claims) == 0 {
		Fatalf(2, "task review: --fail needs at least one --finding saying what is wrong")
	}
	if len(evidence) > len(claims) {
		Fatalf(2, "task review: more --evidence than --finding")
	}
	for _, c := range append(append([]string{}, claims...), evidence...) {
		if err := rejectControlText("finding", c, 400); err != nil {
			Fatalf(2, "task review: %v", err)
		}
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		return err
	}
	defer func() { _ = rt.Close() }()

	t := rt.State.Tasks[slug]
	if t == nil {
		Fatalf(2, "task review: no task %q", slug)
	}
	if t.Did == "" {
		Fatalf(2, "task review: nothing is awaiting review on %s", slug)
	}
	if baseName(rt.Actor) == baseName(t.Did) {
		Fatalf(1, "task review: %s did this work, so verification has to come from somewhere else\n"+
			"  a fresh session of another agent is enough; a different model family is better", t.Did)
	}

	data := map[string]interface{}{"task": slug, "state": "verified"}
	if fail {
		data["state"] = "rejected"
		found := make([]interface{}, 0, len(claims))
		for i, c := range claims {
			f := map[string]interface{}{"claim": c}
			if i < len(evidence) {
				f["evidence"] = evidence[i]
			}
			found = append(found, f)
		}
		data["findings"] = found
	}
	stampActiveCommsSession(rt, data)
	if err := rt.Append(newTaskEvent(rt, event.TypeTaskState, data)); err != nil {
		return err
	}

	if fail {
		fmt.Printf("%s sent back to %s with %d finding(s)\n", slug, t.Did, len(claims))
		return nil
	}
	indep := rt.State.Tasks[slug].Independence
	fmt.Printf("%s verified", slug)
	if indep != "" {
		fmt.Printf(" (%s)", indep)
	}
	fmt.Println()
	if unblocked := readyAfter(rt.State, slug); len(unblocked) > 0 {
		fmt.Printf("  unblocked: %s\n", strings.Join(unblocked, ", "))
	}
	return nil
}

func newTaskShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "show",
		Short: "The whole graph, grouped by what you can do about it",
		Args:  cobra.NoArgs,
		RunE:  func(_ *cobra.Command, _ []string) error { return runTaskShow() },
	}
}

func runTaskShow() error {
	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		return err
	}
	defer func() { _ = rt.Close() }()
	printTaskBoard(rt.State)
	return nil
}

// ---- helpers shared with plan/next/brief ----

func newTaskEvent(rt *Runtime, typ event.Type, data map[string]interface{}) event.Event {
	now := time.Now().UTC()
	return event.Event{TS: now, ID: event.NewID(now), Actor: rt.Actor, Type: typ, Data: data}
}

func baseName(actor string) string {
	if i := strings.IndexByte(actor, '/'); i >= 0 {
		return actor[:i]
	}
	return actor
}

func toIfaceSlice(in []string) []interface{} {
	out := make([]interface{}, 0, len(in))
	for _, s := range in {
		out = append(out, s)
	}
	return out
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

// wouldCycle reports the offending chain if adding these edges would make a task
// depend on itself. Iterative, so it terminates on an already-cyclic graph.
func wouldCycle(s *state.State, add [][2]string) string {
	out := map[string][]string{}
	for _, e := range s.TaskEdges {
		out[e.From] = append(out[e.From], e.To)
	}
	for _, e := range add {
		out[e[0]] = append(out[e[0]], e[1])
	}
	for _, e := range add {
		start := e[1]
		seen := map[string]bool{start: true}
		frontier := []string{start}
		for len(frontier) > 0 {
			cur := frontier[0]
			frontier = frontier[1:]
			for _, next := range out[cur] {
				if next == e[0] {
					return e[0] + " -> " + e[1] + " -> ... -> " + e[0]
				}
				if !seen[next] {
					seen[next] = true
					frontier = append(frontier, next)
				}
			}
		}
	}
	return ""
}

// readyAfter names the tasks that this verification just released.
func readyAfter(s *state.State, verified string) []string {
	var out []string
	for _, e := range s.EdgesOutOf(verified) {
		if t := s.Tasks[e.To]; t != nil && t.Phase == state.PhaseReady {
			out = append(out, e.To)
		}
	}
	sort.Strings(out)
	return out
}

func printTaskBoard(s *state.State) {
	tasks := s.SortedTasks()
	if len(tasks) == 0 {
		fmt.Println("No tasks yet. Declare one with `comms task add <slug> --title \"...\"`.")
		return
	}
	groups := []struct {
		phase state.TaskPhase
		head  string
	}{
		{state.PhaseReview, "WAITING TO BE VERIFIED"},
		{state.PhaseReady, "READY"},
		{state.PhaseDoing, "BEING WORKED ON"},
		{state.PhaseBlocked, "WAITING ON SOMETHING"},
		{state.PhaseCycle, "DEPENDS ON ITSELF: the plan is wrong"},
		{state.PhaseClosed, "CLOSED"},
	}
	for _, g := range groups {
		var in []*state.Task
		for _, t := range tasks {
			if t.Phase == g.phase {
				in = append(in, t)
			}
		}
		if len(in) == 0 {
			continue
		}
		fmt.Printf("\n%s\n", g.head)
		for _, t := range in {
			fmt.Printf("  %-14s %-3s %s\n", t.ID, t.Size, t.Title)
			if detail := taskDetailLine(t); detail != "" {
				fmt.Printf("  %-14s     %s\n", "", detail)
			}
		}
	}
	if len(s.RefusedTaskStates) > 0 {
		fmt.Printf("\nREFUSED\n")
		for _, r := range s.RefusedTaskStates {
			fmt.Printf("  %-14s %s\n", r.Task, r.Reason)
		}
	}
}

func taskDetailLine(t *state.Task) string {
	switch t.Phase {
	case state.PhaseReview:
		return "done by " + t.Did + ": needs someone else"
	case state.PhaseDoing:
		s := strings.Join(t.Doers, ", ")
		if free := t.FreeSlots(); free > 0 {
			s += fmt.Sprintf(" · %d slot(s) free", free)
		}
		return s
	case state.PhaseBlocked:
		return "after " + strings.Join(t.BlockedBy, ", ")
	case state.PhaseClosed:
		s := "verified by " + t.VerifiedBy
		if t.Independence != "" {
			s += " (" + t.Independence + ")"
		}
		return s
	case state.PhaseReady:
		if t.Rejections > 0 {
			return fmt.Sprintf("sent back %d time(s)", t.Rejections)
		}
	}
	return ""
}
