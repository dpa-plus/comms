package subcmd

import (
	"fmt"
	"sort"
	"time"

	"github.com/dpa-plus/comms/internal/state"
)

// The dashboard's route map.
//
// Layout is computed HERE, in Go, and shipped as coordinates. The alternative:
// laying out in the browser: means hand-rolled graph code inside a Go raw
// string that cannot contain a backtick, untestable except through the DOM. This
// way the geometry is ordinary Go with ordinary tests, and the page only draws.
//
// Three rules the layout enforces by construction rather than by care:
//
//   - Tasks joined by an arrow are laid out together, left to right by depth, so
//     an arrow can only ever point rightward.
//   - Tasks joined to nothing are placed on their own below a divider. A shared
//     row or column would imply a relationship they do not have.
//   - One node per row inside a group, so an edge always has an empty lane to
//     run along and can never be routed through another node.
const (
	taskNodeW     = 212
	taskNodeH     = 64
	taskClosedW   = 158
	taskClosedH   = 28
	taskColPitch  = 254
	taskRowPitch  = 84
	taskGroupGap  = 44
	taskBoardPad  = 28
	taskAloneGap  = 34
	taskMaxNodes  = 60 // above this the board stops drawing and the list stands in
	taskEdgeInset = 8
)

// uiTask is one node, already positioned.
type uiTask struct {
	ID           string   `json:"id"`
	Title        string   `json:"title"`
	Phase        string   `json:"phase"`
	Size         string   `json:"size,omitempty"`
	Slots        int      `json:"slots"`
	FreeSlots    int      `json:"free_slots"`
	Doers        []string `json:"doers,omitempty"`
	Did          string   `json:"done_by,omitempty"`
	VerifiedBy   string   `json:"verified_by,omitempty"`
	Independence string   `json:"independence,omitempty"`
	Rejections   int      `json:"rejections,omitempty"`
	BlockedBy    []string `json:"blocked_by,omitempty"`
	Ref          string   `json:"ref,omitempty"`
	Stale        bool     `json:"stale,omitempty"`

	X int `json:"x"`
	Y int `json:"y"`
	W int `json:"w"`
	H int `json:"h"`
}

// uiTaskEdge is one arrow, already routed.
type uiTaskEdge struct {
	From     string `json:"from"`
	To       string `json:"to"`
	Kind     string `json:"kind"`
	Provides string `json:"provides,omitempty"`
	Path     string `json:"d"`
	// Satisfied means the task it leaves has been verified; Live means work is
	// happening there right now. Both only change how the arrow is drawn.
	Satisfied bool `json:"satisfied,omitempty"`
	Live      bool `json:"live,omitempty"`
}

// uiTaskBoard is everything the page needs to draw the graph.
type uiTaskBoard struct {
	Tasks []uiTask     `json:"tasks"`
	Edges []uiTaskEdge `json:"edges"`
	W     int          `json:"w"`
	H     int          `json:"h"`
	// SplitY is where the divider goes: above it, work joined by arrows; below
	// it, work joined to nothing at all.
	SplitY int `json:"split_y,omitempty"`

	Open     int  `json:"open"`
	Closed   int  `json:"closed"`
	Review   int  `json:"review"`
	Ready    int  `json:"ready"`
	Doing    int  `json:"doing"`
	Blocked  int  `json:"blocked"`
	Cycles   int  `json:"cycles"`
	Refused  int  `json:"refused"`
	TooLarge bool `json:"too_large,omitempty"`
}

// buildTaskBoard lays the graph out. staleAfter marks an agent that has gone
// quiet while holding work: the only red thing on the board.
func buildTaskBoard(st *state.State, now time.Time, staleAfter time.Duration) *uiTaskBoard {
	b := &uiTaskBoard{Tasks: []uiTask{}, Edges: []uiTaskEdge{}}
	if st == nil || len(st.Tasks) == 0 {
		return b
	}
	all := st.SortedTasks()
	b.Refused = len(st.RefusedTaskStates)
	for _, t := range all {
		switch t.Phase {
		case state.PhaseClosed:
			b.Closed++
		case state.PhaseReview:
			b.Review++
		case state.PhaseReady:
			b.Ready++
		case state.PhaseDoing:
			b.Doing++
		case state.PhaseBlocked:
			b.Blocked++
		case state.PhaseCycle:
			b.Cycles++
		}
	}
	b.Open = len(all) - b.Closed
	if len(all) > taskMaxNodes {
		// Past this a picture stops being comprehension. Say so rather than
		// drawing a poster nobody can read.
		b.TooLarge = true
		return b
	}

	// ---- connected groups: anything joined by an arrow, directly or not ----
	parent := make(map[string]string, len(all))
	for _, t := range all {
		parent[t.ID] = t.ID
	}
	var find func(string) string
	find = func(a string) string {
		if parent[a] != a {
			parent[a] = find(parent[a])
		}
		return parent[a]
	}
	edges := st.TaskEdges
	for _, e := range edges {
		if st.Tasks[e.From] == nil || st.Tasks[e.To] == nil {
			continue
		}
		if ra, rb := find(e.From), find(e.To); ra != rb {
			parent[ra] = rb
		}
	}
	groups := map[string][]string{}
	for _, t := range all {
		r := find(t.ID)
		groups[r] = append(groups[r], t.ID)
	}

	// ---- depth: longest path, iterative so a cycle cannot hang the dashboard ----
	depth := taskDepths(st)

	linked := make([][]string, 0, len(groups))
	alone := make([]string, 0, len(groups))
	for _, ids := range groups {
		if len(ids) > 1 {
			linked = append(linked, ids)
		} else {
			alone = append(alone, ids[0])
		}
	}
	sort.Slice(linked, func(i, j int) bool {
		if len(linked[i]) != len(linked[j]) {
			return len(linked[i]) > len(linked[j])
		}
		return linked[i][0] < linked[j][0]
	})
	sort.Strings(alone)

	pos := map[string][2]int{}
	cy := 0
	for _, ids := range linked {
		cols := map[int][]string{}
		for _, id := range ids {
			cols[depth[id]] = append(cols[depth[id]], id)
		}
		keys := make([]int, 0, len(cols))
		for k := range cols {
			keys = append(keys, k)
		}
		sort.Ints(keys)
		rowOf := map[string]float64{}
		maxRows := 0
		for _, k := range keys {
			col := cols[k]
			// Order each column by the average row of what it depends on, so
			// arrows stay short and mostly parallel.
			sort.SliceStable(col, func(i, j int) bool {
				return taskBarycentre(st, col[i], k, rowOf) < taskBarycentre(st, col[j], k, rowOf)
			})
			for i, id := range col {
				rowOf[id] = float64(i)
			}
			if len(col) > maxRows {
				maxRows = len(col)
			}
		}
		groupH := maxRows * taskRowPitch
		for _, k := range keys {
			col := cols[k]
			colH := len(col) * taskRowPitch
			for i, id := range col {
				pos[id] = [2]int{k * taskColPitch, cy + (groupH-colH)/2 + i*taskRowPitch + taskRowPitch/2}
			}
		}
		cy += groupH + taskGroupGap
	}
	if len(alone) > 0 {
		b.SplitY = cy + taskBoardPad - taskAloneGap/2
		for i, id := range alone {
			pos[id] = [2]int{i * taskColPitch, cy + taskAloneGap + taskRowPitch/2}
		}
		cy += taskAloneGap + taskRowPitch
	}

	maxX := 0
	for _, t := range all {
		p := pos[t.ID]
		w, h := taskNodeW, taskNodeH
		if t.Phase == state.PhaseClosed {
			w, h = taskClosedW, taskClosedH
		}
		if p[0]+w > maxX {
			maxX = p[0] + w
		}
		b.Tasks = append(b.Tasks, uiTask{
			ID: t.ID, Title: t.Title, Phase: string(t.Phase), Size: t.Size,
			Slots: t.Slots, FreeSlots: t.FreeSlots(), Doers: t.Doers,
			Did: t.Did, VerifiedBy: t.VerifiedBy, Independence: t.Independence,
			Rejections: t.Rejections, BlockedBy: t.BlockedBy, Ref: t.Ref,
			Stale: taskIsStale(st, t, now, staleAfter),
			X:     p[0] + taskBoardPad, Y: p[1] + taskBoardPad - h/2, W: w, H: h,
		})
	}
	b.W = maxX + taskBoardPad*2
	b.H = cy + taskBoardPad*2

	byID := map[string]*uiTask{}
	for i := range b.Tasks {
		byID[b.Tasks[i].ID] = &b.Tasks[i]
	}
	for _, e := range edges {
		from, to := byID[e.From], byID[e.To]
		if from == nil || to == nil {
			continue
		}
		src := st.Tasks[e.From]
		x1, y1 := from.X+from.W, from.Y+from.H/2
		x2, y2 := to.X, to.Y+to.H/2
		c := (x2 - x1) / 2
		if c < 32 {
			c = 32
		}
		b.Edges = append(b.Edges, uiTaskEdge{
			From: e.From, To: e.To, Kind: e.Kind, Provides: e.Provides,
			Satisfied: src.Phase == state.PhaseClosed,
			Live:      src.Phase == state.PhaseDoing || src.Phase == state.PhaseReview,
			Path: fmt.Sprintf("M%d %d C%d %d, %d %d, %d %d",
				x1, y1, x1+c, y1, x2-c, y2, x2-taskEdgeInset, y2),
		})
	}
	return b
}

// taskDepths is the longest path to each task, computed by repeated relaxation
// so a cyclic plan settles instead of recursing forever.
func taskDepths(st *state.State) map[string]int {
	depth := make(map[string]int, len(st.Tasks))
	for id := range st.Tasks {
		depth[id] = 0
	}
	for pass := 0; pass <= len(st.Tasks); pass++ {
		changed := false
		for _, e := range st.TaskEdges {
			if st.Tasks[e.From] == nil || st.Tasks[e.To] == nil {
				continue
			}
			if d := depth[e.From] + 1; d > depth[e.To] {
				depth[e.To] = d
				changed = true
			}
		}
		if !changed {
			break
		}
	}
	return depth
}

func taskBarycentre(st *state.State, id string, col int, rowOf map[string]float64) float64 {
	sum, n := 0.0, 0
	for _, e := range st.EdgesInto(id) {
		if r, ok := rowOf[e.From]; ok {
			sum += r
			n++
		}
	}
	if n == 0 {
		return 0
	}
	return sum / float64(n)
}

// taskIsStale reports an agent that is holding a task but has gone quiet. It is
// the one thing on the board that should be red.
func taskIsStale(st *state.State, t *state.Task, now time.Time, staleAfter time.Duration) bool {
	if t.Phase != state.PhaseDoing || len(t.Doers) == 0 {
		return false
	}
	for _, actor := range t.Doers {
		sess := st.Sessions[actor]
		if sess == nil {
			continue
		}
		if now.Sub(lastSeenOf(sess)) >= staleAfter {
			return true
		}
	}
	return false
}
