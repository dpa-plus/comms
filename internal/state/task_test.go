package state

import (
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
)

// taskLog builds a small plan: db-schema -> auth-api -> login-ui, plus a
// standalone task connected to nothing.
func taskLog(t *testing.T, base time.Time) []event.Event {
	t.Helper()
	at := func(m int) time.Time { return base.Add(time.Duration(m) * time.Minute) }
	return []event.Event{
		mkEvent(t, at(0), "claude-dev", event.TypeHello, nil, map[string]interface{}{"model": "claude-opus-5", "vendor": "anthropic"}),
		mkEvent(t, at(0), "codex-dev", event.TypeHello, nil, map[string]interface{}{"model": "gpt-5.5-codex", "vendor": "openai"}),
		mkEvent(t, at(0), "fable-api", event.TypeHello, nil, map[string]interface{}{"model": "claude-fable-5", "vendor": "anthropic"}),

		mkEvent(t, at(1), "claude-dev", event.TypeTask, nil, map[string]interface{}{
			"task": "db-schema", "title": "Add session tables", "size": "s", "checks": []interface{}{"test"}}),
		mkEvent(t, at(1), "claude-dev", event.TypeTask, nil, map[string]interface{}{
			"task": "auth-api", "title": "Build the session API", "size": "L", "slots": float64(2)}),
		mkEvent(t, at(1), "claude-dev", event.TypeTask, nil, map[string]interface{}{
			"task": "login-ui", "title": "Rebuild the login screen"}),
		mkEvent(t, at(1), "claude-dev", event.TypeTask, nil, map[string]interface{}{
			"task": "docs", "title": "Document the lifecycle"}),

		mkEvent(t, at(2), "claude-dev", event.TypeTaskEdge, nil, map[string]interface{}{
			"from": "db-schema", "to": "auth-api", "kind": "artifact", "provides": "sessions table"}),
		mkEvent(t, at(2), "claude-dev", event.TypeTaskEdge, nil, map[string]interface{}{
			"from": "auth-api", "to": "login-ui", "kind": "interface", "provides": "POST /session"}),
	}
}

func TestTaskPhasesAreDerivedNotAuthored(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	s := Fold(taskLog(t, base))

	if got := s.Tasks["db-schema"].Phase; got != PhaseReady {
		t.Errorf("db-schema has no dependencies, want %q, got %q", PhaseReady, got)
	}
	if got := s.Tasks["auth-api"].Phase; got != PhaseBlocked {
		t.Errorf("auth-api waits on db-schema, want %q, got %q", PhaseBlocked, got)
	}
	if got := s.Tasks["docs"].Phase; got != PhaseReady {
		t.Errorf("docs is connected to nothing, want %q, got %q", PhaseReady, got)
	}
	if got := s.Tasks["auth-api"].Slots; got != 2 {
		t.Errorf("slots should survive the JSON round trip as an int, got %d", got)
	}
	if got := s.Tasks["login-ui"].Slots; got != 1 {
		t.Errorf("an unspecified slot count defaults to 1, got %d", got)
	}
	if got := s.Tasks["db-schema"].Size; got != "S" {
		t.Errorf("size should normalise to upper case, got %q", got)
	}
}

// The rule the whole design rests on: finishing a task is not enough to release
// the work that comes after it. Only verification is.
func TestSuccessorStaysBlockedUntilPredecessorIsVerifiedNotMerelyDone(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	evs := taskLog(t, base)
	evs = append(evs, mkEvent(t, base.Add(10*time.Minute), "codex-dev", event.TypeTaskState, nil,
		map[string]interface{}{"task": "db-schema", "state": "done", "checks": map[string]interface{}{"test": "pass"}}))

	s := Fold(evs)
	if got := s.Tasks["db-schema"].Phase; got != PhaseReview {
		t.Fatalf("finished work waits for a second pair of eyes, want %q, got %q", PhaseReview, got)
	}
	if got := s.Tasks["auth-api"].Phase; got != PhaseBlocked {
		t.Fatalf("auth-api must stay blocked while db-schema is only DONE, got %q", got)
	}

	evs = append(evs, mkEvent(t, base.Add(20*time.Minute), "claude-dev/review", event.TypeTaskState, nil,
		map[string]interface{}{"task": "db-schema", "state": "verified"}))
	s = Fold(evs)
	if got := s.Tasks["db-schema"].Phase; got != PhaseClosed {
		t.Fatalf("want %q after verification, got %q", PhaseClosed, got)
	}
	if got := s.Tasks["auth-api"].Phase; got != PhaseReady {
		t.Fatalf("verification is what unblocks the successor, want %q, got %q", PhaseReady, got)
	}
}

func TestSelfReviewIsRefusedByTheReducer(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	evs := append(taskLog(t, base),
		mkEvent(t, base.Add(10*time.Minute), "codex-dev", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "done", "checks": map[string]interface{}{"test": "pass"}}),
		// Same agent, and the same agent wearing a role suffix. Both must fail.
		mkEvent(t, base.Add(11*time.Minute), "codex-dev", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "verified"}),
		mkEvent(t, base.Add(12*time.Minute), "codex-dev/review", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "verified"}),
	)
	s := Fold(evs)
	if got := s.Tasks["db-schema"].Phase; got != PhaseReview {
		t.Fatalf("a self-review must not close the task, got %q", got)
	}
	if len(s.RefusedTaskStates) != 2 {
		t.Fatalf("both attempts should be recorded as refused, got %d", len(s.RefusedTaskStates))
	}
	for _, r := range s.RefusedTaskStates {
		if r.Reason == "" || r.Task != "db-schema" {
			t.Errorf("refusal should say what and why, got %+v", r)
		}
	}
}

func TestDoneIsRefusedWhenADeclaredCheckFailed(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	evs := append(taskLog(t, base),
		mkEvent(t, base.Add(10*time.Minute), "codex-dev", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "done", "checks": map[string]interface{}{"test": "fail"}}),
	)
	s := Fold(evs)
	if got := s.Tasks["db-schema"].Phase; got == PhaseReview || got == PhaseClosed {
		t.Fatalf("failing checks must not reach review, got %q", got)
	}
	if len(s.RefusedTaskStates) != 1 {
		t.Fatalf("expected one refusal, got %d", len(s.RefusedTaskStates))
	}
}

// A rejection is a rework edge: the task goes back to being worked on and the
// graph is not redrawn.
func TestRejectionSendsTheTaskBackWithItsFindings(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	evs := append(taskLog(t, base),
		mkEvent(t, base.Add(10*time.Minute), "codex-dev", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "done", "checks": map[string]interface{}{"test": "pass"}}),
		mkEvent(t, base.Add(15*time.Minute), "claude-dev/review", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "rejected", "findings": []interface{}{
				map[string]interface{}{"claim": "revoke will table-scan", "evidence": "EXPLAIN shows Seq Scan"},
			}}),
	)
	s := Fold(evs)
	db := s.Tasks["db-schema"]
	if db.Phase != PhaseReady {
		t.Fatalf("a rejected task returns to open work, want %q, got %q", PhaseReady, db.Phase)
	}
	if db.Did != "" {
		t.Errorf("the doer is cleared so the task can be picked up again, got %q", db.Did)
	}
	if db.Rejections != 1 {
		t.Errorf("rework count should be remembered, got %d", db.Rejections)
	}
	if len(db.Findings) != 1 || db.Findings[0].Evidence == "" {
		t.Fatalf("findings must travel with the rejection, got %+v", db.Findings)
	}
	if s.Tasks["auth-api"].Phase != PhaseBlocked {
		t.Errorf("the successor stays blocked through the rework")
	}
}

func TestVerificationRecordsWhetherItWasIndependent(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	done := mkEvent(t, base.Add(10*time.Minute), "codex-dev", event.TypeTaskState, nil,
		map[string]interface{}{"task": "db-schema", "state": "done", "checks": map[string]interface{}{"test": "pass"}})

	// openai work verified by anthropic: different blind spots.
	s := Fold(append(taskLog(t, base), done,
		mkEvent(t, base.Add(20*time.Minute), "claude-dev", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "verified"})))
	if got := s.Tasks["db-schema"].Independence; got != "independent" {
		t.Errorf("anthropic verifying openai is independent, got %q", got)
	}

	// anthropic work verified by anthropic: the weaker claim, and it must be said.
	done2 := mkEvent(t, base.Add(10*time.Minute), "claude-dev", event.TypeTaskState, nil,
		map[string]interface{}{"task": "db-schema", "state": "done", "checks": map[string]interface{}{"test": "pass"}})
	s = Fold(append(taskLog(t, base), done2,
		mkEvent(t, base.Add(20*time.Minute), "fable-api", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "verified"})))
	if got := s.Tasks["db-schema"].Independence; got != "same-family" {
		t.Errorf("two anthropic models share blind spots, want same-family, got %q", got)
	}
}

// Who is on a task is read off live claims, so releasing a file empties it with
// no bookkeeping step for the agent to forget.
func TestDoersComeFromLiveClaimsAndClearOnRelease(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	claim := mkEvent(t, base.Add(5*time.Minute), "codex-dev", event.TypeClaim, []string{"db/schema.sql"},
		map[string]interface{}{"intent": "add tables", "task": "db-schema"})
	evs := append(taskLog(t, base), claim)

	s := Fold(evs)
	db := s.Tasks["db-schema"]
	if db.Phase != PhaseDoing || len(db.Doers) != 1 || db.Doers[0] != "codex-dev" {
		t.Fatalf("a claim tagged to a task puts its actor on it, got phase %q doers %v", db.Phase, db.Doers)
	}
	if db.FreeSlots() != 0 {
		t.Errorf("one slot, one doer, no room left, got %d", db.FreeSlots())
	}

	evs = append(evs, mkEvent(t, base.Add(9*time.Minute), "codex-dev", event.TypeRelease, nil,
		map[string]interface{}{"refs": []interface{}{claim.ID}, "result": "done"}))
	s = Fold(evs)
	if got := s.Tasks["db-schema"]; len(got.Doers) != 0 || got.Phase != PhaseReady {
		t.Fatalf("releasing the file takes the agent off the task, got phase %q doers %v", got.Phase, got.Doers)
	}
}

// Fold has no error return and runs on the hot path of `comms check`, which
// fires before every agent file edit. A cyclic plan must resolve, not hang.
func TestCyclicPlanTerminatesAndIsSurfaced(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	evs := []event.Event{
		mkEvent(t, base, "claude-dev", event.TypeTask, nil, map[string]interface{}{"task": "a", "title": "A"}),
		mkEvent(t, base, "claude-dev", event.TypeTask, nil, map[string]interface{}{"task": "b", "title": "B"}),
		mkEvent(t, base, "claude-dev", event.TypeTask, nil, map[string]interface{}{"task": "c", "title": "C"}),
		mkEvent(t, base, "claude-dev", event.TypeTaskEdge, nil, map[string]interface{}{"from": "a", "to": "b"}),
		mkEvent(t, base, "claude-dev", event.TypeTaskEdge, nil, map[string]interface{}{"from": "b", "to": "c"}),
		mkEvent(t, base, "claude-dev", event.TypeTaskEdge, nil, map[string]interface{}{"from": "c", "to": "a"}),
	}
	done := make(chan *State, 1)
	go func() { done <- Fold(evs) }()
	select {
	case s := <-done:
		for _, id := range []string{"a", "b", "c"} {
			if got := s.Tasks[id].Phase; got != PhaseCycle {
				t.Errorf("task %s is in a cycle, want %q, got %q", id, PhaseCycle, got)
			}
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Fold did not terminate on a cyclic plan")
	}
}

// What a rejection actually invalidates. Only edges that carry something
// propagate; a successor that merely follows consumed nothing.
func TestAffectedByPropagatesOnlyThroughEdgesThatCarrySomething(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	evs := append(taskLog(t, base),
		mkEvent(t, base.Add(3*time.Minute), "claude-dev", event.TypeTask, nil,
			map[string]interface{}{"task": "ship", "title": "Deploy"}),
		mkEvent(t, base.Add(3*time.Minute), "claude-dev", event.TypeTaskEdge, nil,
			map[string]interface{}{"from": "login-ui", "to": "ship", "kind": "sequence", "provides": "ordering only"}),
	)
	s := Fold(evs)

	recheck, unaffected := s.AffectedBy("auth-api")
	if len(recheck) != 1 || recheck[0] != "login-ui" {
		t.Fatalf("login-ui consumes an interface from auth-api, want it rechecked, got %v", recheck)
	}
	if len(unaffected) != 1 || unaffected[0] != "ship" {
		t.Fatalf("ship only follows login-ui, so it is untouched, got %v", unaffected)
	}
}

func TestUnknownEdgeKindFallsBackToOrderingOnly(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	s := Fold([]event.Event{
		mkEvent(t, base, "claude-dev", event.TypeTask, nil, map[string]interface{}{"task": "a"}),
		mkEvent(t, base, "claude-dev", event.TypeTask, nil, map[string]interface{}{"task": "b"}),
		mkEvent(t, base, "claude-dev", event.TypeTaskEdge, nil,
			map[string]interface{}{"from": "a", "to": "b", "kind": "whatever"}),
	})
	if len(s.TaskEdges) != 1 || s.TaskEdges[0].Kind != EdgeSequence {
		t.Fatalf("an unrecognised kind must degrade to the weakest meaning, got %+v", s.TaskEdges)
	}
}

func TestRetitlingATaskDoesNotUnverifyIt(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	evs := append(taskLog(t, base),
		mkEvent(t, base.Add(10*time.Minute), "codex-dev", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "done", "checks": map[string]interface{}{"test": "pass"}}),
		mkEvent(t, base.Add(20*time.Minute), "claude-dev", event.TypeTaskState, nil,
			map[string]interface{}{"task": "db-schema", "state": "verified"}),
		mkEvent(t, base.Add(30*time.Minute), "claude-dev", event.TypeTask, nil,
			map[string]interface{}{"task": "db-schema", "title": "Add session and refresh-token tables"}),
	)
	s := Fold(evs)
	db := s.Tasks["db-schema"]
	if db.Phase != PhaseClosed {
		t.Fatalf("editing the description must not reopen the task, got %q", db.Phase)
	}
	if db.Title != "Add session and refresh-token tables" {
		t.Errorf("the new title should stick, got %q", db.Title)
	}
}
