package state

import (
	"sort"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/event"
)

// The task graph.
//
// A task is what should happen. An edge is the order two tasks must happen in
// AND what the later one consumes from the earlier. Every task has two steps:
// an agent does it, then a DIFFERENT agent verifies it — and a task unblocks
// what comes after it only once it has been verified, not merely finished.
// That is what keeps review on the critical path instead of at the end.
//
// Everything a caller reads below is DERIVED by replaying the log. Nothing here
// is authored: an agent cannot write "ready" or "blocked" into an event, and a
// self-review is refused by the reducer rather than by a convention somebody has
// to remember.

// TaskPhase is a task's position in its lifecycle. Derived, never authored.
type TaskPhase string

const (
	// PhaseReady — every dependency is verified and nobody has claimed it.
	PhaseReady TaskPhase = "ready"
	// PhaseDoing — at least one agent holds a file claim tagged to this task.
	PhaseDoing TaskPhase = "doing"
	// PhaseReview — the implementation is finished and is waiting for a second
	// pair of eyes. This is the most valuable thing on the board: it is holding
	// up everything downstream.
	PhaseReview TaskPhase = "review"
	// PhaseClosed — verified. Only now does it unblock its successors.
	PhaseClosed TaskPhase = "closed"
	// PhaseBlocked — at least one dependency is not verified yet.
	PhaseBlocked TaskPhase = "blocked"
	// PhaseCycle — the task can be reached from itself. A plan bug; surfaced
	// rather than hidden, and never allowed to hang the reducer.
	PhaseCycle TaskPhase = "cycle"
)

// Edge kinds. The distinction is load-bearing, not decoration: it decides what a
// rejection invalidates. Reworking a task that another one CONSUMES from means
// the consumer must be rechecked; reworking one it merely follows does not.
const (
	// EdgeInterface — B calls a surface A provides.
	EdgeInterface = "interface"
	// EdgeArtifact — B uses a file or schema A produced.
	EdgeArtifact = "artifact"
	// EdgeSequence — ordering only. Nothing flows across it.
	EdgeSequence = "sequence"
)

// Task is one node of the work graph.
type Task struct {
	ID     string // human-speakable slug, e.g. "auth-api"
	Title  string
	Size   string   // S, M or L
	Slots  int      // how many agents may work it at once; at least 1
	Checks []string // check names that must pass before it can be marked done
	Ref    string   // opaque external reference, e.g. "omni:AUF-2291"

	TS        time.Time
	UpdatedAt time.Time

	// Doers is derived from ACTIVE claims tagged to this task, so it empties
	// itself when an agent releases. Nobody has to remember to update it.
	Doers []string

	// Did is the actor whose implementation is awaiting review, or whose work was
	// verified. Cleared by a rejection — that is the rework edge.
	Did          string
	Notes        []string // decisions written while working; what a verifier reads
	VerifiedBy   string
	Independence string // "independent" or "same-family"
	Rejections   int
	Findings     []TaskFinding
	LastActivity time.Time

	Phase     TaskPhase
	BlockedBy []string
}

// TaskFinding is one reason a verifier gave for sending work back. Evidence is
// what makes "is this reason real?" a checkable question rather than a matter of
// who outranks whom.
type TaskFinding struct {
	Claim    string
	Evidence string
}

// TaskEdge means: To comes after From.
type TaskEdge struct {
	From string
	To   string
	Kind string // interface, artifact or sequence
	// Provides describes what To consumes from From — an interface, a schema, a
	// file's public surface. It is what travels to whoever picks up To, and what
	// makes a rejection precise instead of invalidating everything downstream.
	Provides string
	TS       time.Time
}

// RefusedTransition records a task_state event the reducer would not apply. They
// are kept rather than dropped so the operator can see that an agent tried to
// mark its own work verified, or to close a task whose tests were failing.
type RefusedTransition struct {
	TS     time.Time
	Task   string
	Actor  string
	Phase  string
	Reason string
}

// baseActor strips a role suffix, so claude-dev/review is recognised as
// claude-dev. Without this an agent could verify its own work simply by
// spawning a reviewer under its own name.
func baseActor(a string) string {
	if i := strings.IndexByte(a, '/'); i >= 0 {
		return a[:i]
	}
	return a
}

// applyTask upserts a node. Only the DESCRIPTION is replaced — re-titling a task
// must not unverify it, so lifecycle fields are left alone.
func (s *State) applyTask(ev event.Event) {
	id := stringOf(ev.Data, "task")
	if id == "" {
		return
	}
	t := s.Tasks[id]
	if t == nil {
		t = &Task{ID: id, TS: ev.TS}
		s.Tasks[id] = t
	}
	if v := stringOf(ev.Data, "title"); v != "" {
		t.Title = v
	}
	if v := stringOf(ev.Data, "size"); v != "" {
		t.Size = strings.ToUpper(v)
	}
	if n := intOf(ev.Data, "slots"); n > 0 {
		t.Slots = n
	}
	if t.Slots < 1 {
		t.Slots = 1
	}
	if v := refList(ev.Data, "checks"); len(v) > 0 {
		t.Checks = v
	}
	if v := stringOf(ev.Data, "ref"); v != "" {
		t.Ref = v
	}
	t.UpdatedAt = ev.TS
	t.LastActivity = ev.TS
}

// applyTaskEdge upserts an edge, replacing any existing one with the same
// endpoints. Edges are their own events so any agent can add one at the moment
// it discovers the dependency — the only moment it is actually known.
func (s *State) applyTaskEdge(ev event.Event) {
	from, to := stringOf(ev.Data, "from"), stringOf(ev.Data, "to")
	if from == "" || to == "" || from == to {
		return
	}
	kind := strings.ToLower(stringOf(ev.Data, "kind"))
	switch kind {
	case EdgeInterface, EdgeArtifact, EdgeSequence:
	default:
		kind = EdgeSequence
	}
	e := &TaskEdge{From: from, To: to, Kind: kind, Provides: stringOf(ev.Data, "provides"), TS: ev.TS}
	for i, existing := range s.TaskEdges {
		if existing.From == from && existing.To == to {
			s.TaskEdges[i] = e
			return
		}
	}
	s.TaskEdges = append(s.TaskEdges, e)
}

// applyTaskState moves a task along its lifecycle, refusing anything that would
// break the two rules the graph rests on: work is not done until its declared
// checks pass, and nobody verifies their own work.
func (s *State) applyTaskState(ev event.Event) {
	id := stringOf(ev.Data, "task")
	t := s.Tasks[id]
	if t == nil {
		return
	}
	refuse := func(reason string) {
		s.RefusedTaskStates = append(s.RefusedTaskStates, &RefusedTransition{
			TS: ev.TS, Task: id, Actor: ev.Actor,
			Phase: stringOf(ev.Data, "state"), Reason: reason,
		})
	}
	t.LastActivity = ev.TS

	switch strings.ToLower(stringOf(ev.Data, "state")) {
	case "done":
		if failed := failedChecks(t.Checks, ev.Data); len(failed) > 0 {
			refuse("required checks did not pass: " + strings.Join(failed, ", "))
			return
		}
		t.Did = ev.Actor
		t.VerifiedBy = ""
		if notes := refList(ev.Data, "notes"); len(notes) > 0 {
			t.Notes = append(t.Notes, notes...)
		}

	case "verified":
		if t.Did == "" {
			refuse("nothing is awaiting review on this task")
			return
		}
		if baseActor(ev.Actor) == baseActor(t.Did) {
			refuse("self-review: " + ev.Actor + " cannot verify work done by " + t.Did)
			return
		}
		t.VerifiedBy = ev.Actor
		t.Independence = s.independenceOf(ev.Actor, t.Did)
		t.Findings = nil

	case "rejected":
		if t.Did == "" {
			refuse("nothing is awaiting review on this task")
			return
		}
		if baseActor(ev.Actor) == baseActor(t.Did) {
			refuse("self-review: " + ev.Actor + " cannot reject work done by " + t.Did)
			return
		}
		// The rework edge. The graph is not redrawn; the task simply goes back to
		// being worked on, and the findings travel with it.
		t.Did = ""
		t.VerifiedBy = ""
		t.Rejections++
		t.Findings = findingsFrom(ev.Data)
	}
}

// independenceOf records HOW a task was verified. A verifier from the same model
// family shares the doer's blind spots, so "verified" and "verified by something
// that thinks the same way" are different claims and should not be blurred.
func (s *State) independenceOf(verifier, doer string) string {
	v, d := s.vendorOf(verifier), s.vendorOf(doer)
	if v == "" || d == "" {
		return ""
	}
	if v != d {
		return "independent"
	}
	return "same-family"
}

func (s *State) vendorOf(actor string) string {
	if sess := s.Sessions[actor]; sess != nil && sess.Vendor != "" {
		return sess.Vendor
	}
	if sess := s.Sessions[baseActor(actor)]; sess != nil {
		return sess.Vendor
	}
	return ""
}

func failedChecks(required []string, data map[string]interface{}) []string {
	if len(required) == 0 {
		return nil
	}
	results, _ := data["checks"].(map[string]interface{})
	var failed []string
	for _, c := range required {
		if v, _ := results[c].(string); !strings.EqualFold(v, "pass") {
			failed = append(failed, c)
		}
	}
	return failed
}

func findingsFrom(data map[string]interface{}) []TaskFinding {
	raw, _ := data["findings"].([]interface{})
	out := make([]TaskFinding, 0, len(raw))
	for _, r := range raw {
		m, ok := r.(map[string]interface{})
		if !ok {
			continue
		}
		f := TaskFinding{Claim: stringOf(m, "claim"), Evidence: stringOf(m, "evidence")}
		if f.Claim != "" {
			out = append(out, f)
		}
	}
	return out
}

// deriveTaskPhases computes every task's phase from the graph and the live
// claims. Runs once after the fold.
//
// It must be safe on a cyclic plan: Fold returns no error and runs on the hot
// path of `comms check`, which fires before every agent file edit. So the
// traversal here is iterative with an explicit frontier — a cycle resolves to
// PhaseCycle and the reducer still returns.
func (s *State) deriveTaskPhases() {
	if len(s.Tasks) == 0 {
		return
	}
	// Doers come from ACTIVE claims, so releasing a file empties them for free.
	for _, t := range s.Tasks {
		t.Doers = t.Doers[:0]
		t.BlockedBy = nil
	}
	for _, c := range s.Claims {
		t := s.Tasks[c.Task]
		if c.Task == "" || t == nil {
			continue
		}
		if !containsString(t.Doers, c.Actor) {
			t.Doers = append(t.Doers, c.Actor)
		}
		if c.TS.After(t.LastActivity) {
			t.LastActivity = c.TS
		}
	}
	for _, t := range s.Tasks {
		sort.Strings(t.Doers)
	}

	// Kahn's algorithm over the dependency edges. Whatever cannot be peeled off
	// is in, or downstream of, a cycle. No recursion, so it always terminates.
	indeg := make(map[string]int, len(s.Tasks))
	out := make(map[string][]string, len(s.Tasks))
	for _, e := range s.TaskEdges {
		if s.Tasks[e.From] == nil || s.Tasks[e.To] == nil {
			continue // an edge to a task nobody declared is not a cycle, just noise
		}
		indeg[e.To]++
		out[e.From] = append(out[e.From], e.To)
	}
	queue := make([]string, 0, len(s.Tasks))
	for id := range s.Tasks {
		if indeg[id] == 0 {
			queue = append(queue, id)
		}
	}
	settled := make(map[string]bool, len(s.Tasks))
	for len(queue) > 0 {
		id := queue[0]
		queue = queue[1:]
		settled[id] = true
		for _, next := range out[id] {
			indeg[next]--
			if indeg[next] == 0 {
				queue = append(queue, next)
			}
		}
	}

	for _, t := range s.Tasks {
		switch {
		case t.VerifiedBy != "":
			t.Phase = PhaseClosed
		case !settled[t.ID]:
			t.Phase = PhaseCycle
		case t.Did != "":
			t.Phase = PhaseReview
		default:
			for _, e := range s.TaskEdges {
				if e.To != t.ID {
					continue
				}
				if dep := s.Tasks[e.From]; dep != nil && dep.VerifiedBy == "" {
					t.BlockedBy = append(t.BlockedBy, e.From)
				}
			}
			switch {
			case len(t.BlockedBy) > 0:
				sort.Strings(t.BlockedBy)
				t.Phase = PhaseBlocked
			case len(t.Doers) > 0:
				t.Phase = PhaseDoing
			default:
				t.Phase = PhaseReady
			}
		}
	}
}

// ---- read helpers used by the CLI and the dashboard ----

// SortedTasks returns every task in a stable order: oldest first.
func (s *State) SortedTasks() []*Task {
	if s == nil {
		return nil
	}
	out := make([]*Task, 0, len(s.Tasks))
	for _, t := range s.Tasks {
		out = append(out, t)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].TS.Equal(out[j].TS) {
			return out[i].ID < out[j].ID
		}
		return out[i].TS.Before(out[j].TS)
	})
	return out
}

// EdgesInto returns the dependencies of a task — what it comes after.
func (s *State) EdgesInto(id string) []*TaskEdge {
	if s == nil {
		return nil
	}
	var out []*TaskEdge
	for _, e := range s.TaskEdges {
		if e.To == id {
			out = append(out, e)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].From < out[j].From })
	return out
}

// EdgesOutOf returns what a task comes before.
func (s *State) EdgesOutOf(id string) []*TaskEdge {
	if s == nil {
		return nil
	}
	var out []*TaskEdge
	for _, e := range s.TaskEdges {
		if e.From == id {
			out = append(out, e)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].To < out[j].To })
	return out
}

// AffectedBy answers "if this task were reworked, what has to be looked at
// again?" Only edges that carry something propagate — a successor that merely
// follows in sequence consumed nothing and is untouched. Iterative, so a cyclic
// plan cannot hang it.
func (s *State) AffectedBy(id string) (recheck []string, unaffected []string) {
	if s == nil {
		return nil, nil
	}
	seen := map[string]bool{id: true}
	frontier := []string{id}
	for len(frontier) > 0 {
		cur := frontier[0]
		frontier = frontier[1:]
		for _, e := range s.EdgesOutOf(cur) {
			if e.Kind == EdgeSequence {
				if !containsString(unaffected, e.To) {
					unaffected = append(unaffected, e.To)
				}
				continue
			}
			if seen[e.To] {
				continue
			}
			seen[e.To] = true
			recheck = append(recheck, e.To)
			frontier = append(frontier, e.To)
		}
	}
	sort.Strings(recheck)
	sort.Strings(unaffected)
	return recheck, unaffected
}

// FreeSlots reports how many more agents could join a task right now.
func (t *Task) FreeSlots() int {
	if t == nil {
		return 0
	}
	n := t.Slots - len(t.Doers)
	if n < 0 {
		return 0
	}
	return n
}

func containsString(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func intOf(m map[string]interface{}, key string) int {
	if m == nil {
		return 0
	}
	switch v := m[key].(type) {
	case float64: // every JSON number decodes to float64
		return int(v)
	case int:
		return v
	}
	return 0
}
