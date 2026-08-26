package subcmd

import (
	"strings"
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/state"
)

func taskEv(ts time.Time, actor string, typ event.Type, data map[string]interface{}) event.Event {
	return event.Event{TS: ts, ID: event.NewID(ts), Actor: actor, Type: typ, Data: data}
}

func TestBoardSeparatesConnectedWorkFromStandaloneWork(t *testing.T) {
	b := buildTaskBoard(state.Fold(fixtureEvents(t)), time.Now(), time.Hour)
	if len(b.Tasks) != 7 {
		t.Fatalf("expected 7 nodes, got %d", len(b.Tasks))
	}
	if b.SplitY == 0 {
		t.Fatal("docs and cleanup are joined to nothing, so a divider is required")
	}
	pos := map[string]uiTask{}
	for _, n := range b.Tasks {
		pos[n.ID] = n
	}
	for _, id := range []string{"docs", "cleanup"} {
		if pos[id].Y < b.SplitY {
			t.Errorf("%s is joined to nothing and must sit below the divider (y=%d, split=%d)", id, pos[id].Y, b.SplitY)
		}
	}
	for _, id := range []string{"schema", "api", "ui", "store", "audit"} {
		if pos[id].Y > b.SplitY {
			t.Errorf("%s is part of a chain and must sit above the divider", id)
		}
	}
}

// The layout guarantees this rather than the drawing being careful about it.
func TestBoardArrowsOnlyEverPointRightward(t *testing.T) {
	b := buildTaskBoard(state.Fold(fixtureEvents(t)), time.Now(), time.Hour)
	pos := map[string]uiTask{}
	for _, n := range b.Tasks {
		pos[n.ID] = n
	}
	if len(b.Edges) != 3 {
		t.Fatalf("expected 3 arrows, got %d", len(b.Edges))
	}
	for _, e := range b.Edges {
		from, to := pos[e.From], pos[e.To]
		if to.X <= from.X+from.W {
			t.Errorf("%s -> %s runs backwards: source ends at x=%d, target starts at x=%d",
				e.From, e.To, from.X+from.W, to.X)
		}
	}
}

func TestBoardCompressesClosedWorkAndKeepsOpenWorkFullSize(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	all := append(fixtureEvents(t),
		taskEv(base.Add(10*time.Minute), "codex-dev", event.TypeTaskState,
			map[string]interface{}{"task": "schema", "state": "done"}),
		taskEv(base.Add(11*time.Minute), "claude-dev", event.TypeTaskState,
			map[string]interface{}{"task": "schema", "state": "verified"}),
	)
	b := buildTaskBoard(state.Fold(all), time.Now(), time.Hour)
	var closed, open uiTask
	for _, n := range b.Tasks {
		if n.ID == "schema" {
			closed = n
		}
		if n.ID == "api" {
			open = n
		}
	}
	if closed.Phase != "closed" {
		t.Fatalf("schema should be closed, got %q", closed.Phase)
	}
	if closed.W >= open.W || closed.H >= open.H {
		t.Errorf("finished work should take less room than open work: closed %dx%d vs open %dx%d",
			closed.W, closed.H, open.W, open.H)
	}
}

// Same fixture, exposed as events so a test can extend it.
func fixtureEvents(t *testing.T) []event.Event {
	t.Helper()
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	at := func(m int) time.Time { return base.Add(time.Duration(m) * time.Minute) }
	evs := []event.Event{
		taskEv(at(0), "codex-dev", event.TypeHello, map[string]interface{}{"vendor": "openai"}),
		taskEv(at(0), "claude-dev", event.TypeHello, map[string]interface{}{"vendor": "anthropic"}),
	}
	for _, id := range []string{"schema", "api", "ui", "store", "audit", "docs", "cleanup"} {
		evs = append(evs, taskEv(at(1), "claude-dev", event.TypeTask,
			map[string]interface{}{"task": id, "title": id + " work", "size": "M"}))
	}
	for _, e := range [][3]string{
		{"schema", "api", state.EdgeArtifact},
		{"api", "ui", state.EdgeInterface},
		{"store", "audit", state.EdgeInterface},
	} {
		evs = append(evs, taskEv(at(2), "claude-dev", event.TypeTaskEdge,
			map[string]interface{}{"from": e[0], "to": e[1], "kind": e[2], "provides": "something"}))
	}
	return evs
}

// Past a point a picture stops being comprehension. The board says so rather
// than emitting a poster nobody can read.
func TestBoardRefusesToDrawTooManyNodes(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	var evs []event.Event
	for i := 0; i < taskMaxNodes+5; i++ {
		id := "task-" + string(rune('a'+i%26)) + string(rune('a'+i/26))
		evs = append(evs, taskEv(base, "claude-dev", event.TypeTask,
			map[string]interface{}{"task": id, "title": "work"}))
	}
	b := buildTaskBoard(state.Fold(evs), time.Now(), time.Hour)
	if !b.TooLarge {
		t.Fatalf("expected the board to decline at %d nodes", taskMaxNodes+5)
	}
	if len(b.Tasks) != 0 {
		t.Errorf("nothing should be laid out once it declines, got %d nodes", len(b.Tasks))
	}
	if b.Open == 0 {
		t.Error("the counts must still be reported so the page can say how much there is")
	}
}

// The dashboard rebuilds on every appended event. A cyclic plan must not hang it.
func TestBoardTerminatesOnACyclicPlan(t *testing.T) {
	base := time.Date(2026, 8, 19, 9, 0, 0, 0, time.UTC)
	evs := []event.Event{
		taskEv(base, "claude-dev", event.TypeTask, map[string]interface{}{"task": "alpha", "title": "A"}),
		taskEv(base, "claude-dev", event.TypeTask, map[string]interface{}{"task": "beta", "title": "B"}),
		taskEv(base, "claude-dev", event.TypeTaskEdge, map[string]interface{}{"from": "alpha", "to": "beta"}),
		taskEv(base, "claude-dev", event.TypeTaskEdge, map[string]interface{}{"from": "beta", "to": "alpha"}),
	}
	st := state.Fold(evs)
	done := make(chan *uiTaskBoard, 1)
	go func() { done <- buildTaskBoard(st, time.Now(), time.Hour) }()
	select {
	case b := <-done:
		if b.Cycles != 2 {
			t.Errorf("both tasks are in the cycle, got %d", b.Cycles)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("buildTaskBoard did not terminate on a cyclic plan")
	}
}

func TestBoardIsEmptyWithoutTasks(t *testing.T) {
	b := buildTaskBoard(state.Fold(nil), time.Now(), time.Hour)
	if b.Tasks == nil || b.Edges == nil {
		t.Fatal("empty slices, never null: the page indexes them without checking")
	}
	if len(b.Tasks) != 0 || b.W != 0 {
		t.Errorf("an empty graph occupies no space, got %d nodes and w=%d", len(b.Tasks), b.W)
	}
}

// The dashboard is one Go raw string, so a template literal would end it.
func TestUIHTMLHasNoBackticksInTheTaskRenderer(t *testing.T) {
	for _, required := range []string{
		"function renderTaskBoard(view)",
		"renderTaskBoard(view);",
		"task_board",
		`<div id="board" class="board"></div>`,
		`"graph graph graph"`,
	} {
		if !strings.Contains(uiHTML, required) {
			t.Errorf("the work graph panel is not wired up; missing %q", required)
		}
	}
}

// The rail is the whole top of the page now, and two of its invariants are the
// kind that break silently rather than loudly.
func TestTopRailInvariants(t *testing.T) {
	// An alarm chip sets a display, and any display rule outranks the hidden
	// attribute's UA display:none. Without this reset the rail permanently reads
	// "0 STALE 0 TO VERIFY 0 DEPENDENCY CYCLE" in red: an alarm that is always
	// on, which is the same as no alarm at all. This shipped broken once.
	if !strings.Contains(uiHTML, "[hidden] { display: none !important; }") {
		t.Error("the hidden reset is gone; the alarm chips will render permanently, showing zero")
	}
	for _, required := range []string{
		`<span id="alStale" class="al" hidden>`,
		`<span id="alVerify" class="al" hidden>`,
		`<span id="alCycle" class="al" hidden>`,
		"function renderTop(data, view, repoLabel, sel)",
		"renderTop(data, view, repoLabel, sel);",
		// The alert state must not change the rail's height, or every SSE frame
		// that trips an alarm shoves the page down under the operator's cursor.
		"header.alert { background: var(--red-soft); box-shadow: inset 0 -3px 0 var(--red); }",
		// Filled chips need a foreground that survives both themes.
		"--on-red:",
		"color: var(--on-red);",
	} {
		if !strings.Contains(uiHTML, required) {
			t.Errorf("the top rail is not wired up; missing %q", required)
		}
	}
	// The band and its render function are deleted, not merely unused: leaving
	// el('stats') behind is a null deref on every frame.
	for _, gone := range []string{"renderStats", `id="stats"`, "stat-value", `id="projectMeta"`, `id="logPath"`} {
		if strings.Contains(uiHTML, gone) {
			t.Errorf("%q should have been deleted with the stats band", gone)
		}
	}
	// task_board is absent from the merged all-projects snapshot by design, so
	// the alarms must sum across projects there rather than reading one board.
	if !strings.Contains(uiHTML, "(data.project_sessions || []).forEach(x => {") {
		t.Error("the all-projects alarm sum is gone; every project would report zero at once")
	}
}
