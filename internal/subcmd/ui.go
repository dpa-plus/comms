package subcmd

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/dpa-plus/comms/internal/actor"
	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/overlap"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewUICmd serves a small local dashboard over HTTP.
func NewUICmd() *cobra.Command {
	addr := "127.0.0.1:7878"
	demo := false
	staleAfter := 90 * time.Minute
	cmd := &cobra.Command{
		Use:   "ui",
		Short: "Serve a local dashboard",
		Long: `Serve a local dashboard for the current repo.

The UI binds to 127.0.0.1 by default, reads the same JSONL event log as the
CLI, and auto-refreshes in the browser.

Claims older than --stale-after are highlighted as suspicious. The UI can
append a release event for a stale claim when COMMS_ACTOR is set; it never
edits or deletes existing log lines.

Use --demo to show deterministic sample data without writing fake events to
the real comms log.`,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runUI(addr, demo, staleAfter)
		},
	}
	cmd.Flags().StringVar(&addr, "addr", addr, "listen address")
	cmd.Flags().BoolVar(&demo, "demo", false, "serve deterministic sample data without touching the real log")
	cmd.Flags().DurationVar(&staleAfter, "stale-after", staleAfter, "highlight claims older than this duration")
	return cmd
}

func runUI(addr string, demo bool, staleAfter time.Duration) error {
	if staleAfter < time.Minute {
		return fmt.Errorf("ui: --stale-after must be at least 1m")
	}
	server := uiServer{demo: demo, staleAfter: staleAfter}
	mux := http.NewServeMux()
	mux.HandleFunc("/", server.servePage)
	mux.HandleFunc("/api/status", server.serveStatus)
	mux.HandleFunc("/api/hello", server.serveHello)
	mux.HandleFunc("/api/check", server.serveCheck)
	mux.HandleFunc("/api/claims", server.serveCreateClaim)
	mux.HandleFunc("/api/claims/release", server.serveReleaseClaim)
	mux.HandleFunc("/api/notes", server.serveCreateNote)
	mux.HandleFunc("/api/findings", server.serveCreateFinding)
	mux.HandleFunc("/api/docs/", server.serveDoc)

	fmt.Printf("comms ui listening on http://%s\n", addr)
	fmt.Printf("Claims older than %s are marked stale. Press Ctrl-C to stop.\n", staleAfter)
	if demo {
		fmt.Println("Demo mode: serving sample data only; no fake events are written.")
	}
	return http.ListenAndServe(addr, mux)
}

type uiServer struct {
	demo       bool
	staleAfter time.Duration
}

func (s uiServer) servePage(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(uiHTML))
}

func (s uiServer) serveStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.demo {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		_ = enc.Encode(buildDemoUISnapshot(s.staleAfter))
		return
	}

	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	defer rt.Close()

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(buildUISnapshot(rt, s.staleAfter))
}

func (s uiServer) serveHello(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.rejectDemo(w, "demo mode is read-only; no hello event is written") {
		return
	}
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	defer rt.Close()

	now := time.Now().UTC()
	activeLeader := activeLeaderActor(rt.State, now.Add(-4*time.Hour))
	hostname, _ := os.Hostname()
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeHello,
		Data: map[string]interface{}{
			"base_name": baseNameOfActor(rt.Actor),
			"hostname":  hostname,
			"tty":       "",
			"leader":    activeLeader == "" || activeLeader == rt.Actor,
		},
	}
	if err := rt.Append(ev); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeSnapshot(w, rt)
}

func (s uiServer) serveCheck(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Path string `json:"path"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	if strings.TrimSpace(req.Path) == "" {
		http.Error(w, "missing path", http.StatusBadRequest)
		return
	}
	rt, err := Open(OpenOpts{Mutating: false, SkipLock: true})
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	defer rt.Close()

	rel, ok := makeRepoRelative(req.Path, rt.Repo.Root)
	if !ok {
		writeJSON(w, http.StatusOK, map[string]interface{}{"clear": true, "message": "outside repo"})
		return
	}
	scope, err := overlap.Parse(rel)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	conflicts := rt.State.ConflictsFor(scope, rt.Actor)
	resp := map[string]interface{}{"clear": len(conflicts) == 0, "scope": scope.String()}
	if len(conflicts) > 0 {
		resp["conflicts"] = uiClaimsFromState(conflicts, s.staleAfter)
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s uiServer) serveCreateClaim(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.rejectDemo(w, "demo mode is read-only; no claim event is written") {
		return
	}
	var req struct {
		Scope       string `json:"scope"`
		Intent      string `json:"intent"`
		StealID     string `json:"steal_id"`
		StealReason string `json:"steal_reason"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	scope, err := overlap.Parse(req.Scope)
	if err != nil {
		http.Error(w, "claim: "+err.Error(), http.StatusBadRequest)
		return
	}
	if strings.TrimSpace(req.Intent) == "" {
		http.Error(w, "claim: intent is required", http.StatusBadRequest)
		return
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	defer rt.Close()

	if rt.Policy.RequiresAnchor(scope.Path) && scope.Anchor.Kind == overlap.AnchorWhole {
		http.Error(w, fmt.Sprintf("claim: anchor required for risky file %q", scope.Path), http.StatusConflict)
		return
	}

	var displaceID string
	if req.StealID != "" {
		if strings.TrimSpace(req.StealReason) == "" {
			http.Error(w, "claim: steal requires a reason", http.StatusBadRequest)
			return
		}
		target := rt.State.ClaimByID(req.StealID)
		if target == nil {
			http.Error(w, "claim: steal id does not match any active claim", http.StatusBadRequest)
			return
		}
		displaceID = target.ID
	}
	conflicts := rt.State.ConflictsFor(scope, rt.Actor)
	if displaceID != "" {
		conflicts = filterOutClaim(conflicts, displaceID)
	}
	if len(conflicts) > 0 {
		writeJSON(w, http.StatusConflict, map[string]interface{}{
			"error":     "claim conflicts with active claim",
			"conflicts": uiClaimsFromState(conflicts, s.staleAfter),
		})
		return
	}

	now := time.Now().UTC()
	data := map[string]interface{}{"intent": strings.TrimSpace(req.Intent)}
	if displaceID != "" {
		data["steals"] = displaceID
		data["steal_reason"] = strings.TrimSpace(req.StealReason)
		if a := os.Getenv("COMMS_ARBITRATOR"); a != "" {
			data["arbitrator"] = a
		} else if u := os.Getenv("USER"); u != "" {
			data["arbitrator"] = u
		}
	}
	ev := event.Event{TS: now, ID: event.NewID(now), Actor: rt.Actor, Type: event.TypeClaim, Scope: []string{scope.String()}, Data: data}
	if err := rt.Append(ev); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeSnapshot(w, rt)
}

func (s uiServer) serveReleaseClaim(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no release events are written", http.StatusConflict)
		return
	}

	var req struct {
		ID     string `json:"id"`
		Reason string `json:"reason"`
		Force  bool   `json:"force"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	if req.ID == "" {
		http.Error(w, "missing claim id", http.StatusBadRequest)
		return
	}
	if req.Reason == "" {
		req.Reason = "cleared from comms ui by user"
	}

	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	defer rt.Close()

	target := rt.State.ClaimByID(req.ID)
	if target == nil {
		http.Error(w, "claim is no longer active", http.StatusConflict)
		return
	}
	stale := time.Since(target.TS) >= s.staleAfter
	if !stale && !req.Force {
		http.Error(w, "claim is not stale yet; pass force=true to clear an active claim from the UI", http.StatusConflict)
		return
	}
	result := "claim cleared from comms ui"
	if stale {
		result = "stale claim cleared from comms ui"
	}
	if err := appendReleaseEvent(rt, []*state.Claim{target}, req.Reason, result); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(buildUISnapshot(rt, s.staleAfter))
}

func (s uiServer) serveCreateNote(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.rejectDemo(w, "demo mode is read-only; no note event is written") {
		return
	}
	var req struct {
		Body     string `json:"body"`
		Priority bool   `json:"priority"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	body := strings.TrimSpace(req.Body)
	if body == "" {
		http.Error(w, "note: body is required", http.StatusBadRequest)
		return
	}
	if utf8.RuneCountInString(body) > maxNoteRunes {
		http.Error(w, fmt.Sprintf("note: body exceeds %d runes", maxNoteRunes), http.StatusBadRequest)
		return
	}
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	defer rt.Close()
	if req.Priority && !s.actorIsLeader(rt) {
		http.Error(w, s.leaderOnlyMessage(rt), http.StatusForbidden)
		return
	}
	now := time.Now().UTC()
	data := map[string]interface{}{"body": body}
	if req.Priority {
		data["priority"] = true
	}
	if err := rt.Append(event.Event{TS: now, ID: event.NewID(now), Actor: rt.Actor, Type: event.TypeNote, Data: data}); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeSnapshot(w, rt)
}

func (s uiServer) serveCreateFinding(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.rejectDemo(w, "demo mode is read-only; no finding event is written") {
		return
	}
	var req struct {
		Category string `json:"category"`
		Summary  string `json:"summary"`
		Refs     []struct {
			Kind  string `json:"kind"`
			Value string `json:"value"`
		} `json:"refs"`
		Priority bool `json:"priority"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	category := strings.TrimSpace(req.Category)
	if _, ok := findCategories[category]; !ok {
		http.Error(w, "finding: invalid category", http.StatusBadRequest)
		return
	}
	summary := strings.TrimSpace(req.Summary)
	if summary == "" {
		http.Error(w, "finding: summary is required", http.StatusBadRequest)
		return
	}
	rt, err := Open(OpenOpts{Mutating: true})
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	defer rt.Close()
	if req.Priority && !s.actorIsLeader(rt) {
		http.Error(w, s.leaderOnlyMessage(rt), http.StatusForbidden)
		return
	}
	refs := make([]map[string]string, 0, len(req.Refs))
	for _, ref := range req.Refs {
		kind := strings.TrimSpace(ref.Kind)
		value := strings.TrimSpace(ref.Value)
		if kind == "" || value == "" {
			http.Error(w, "finding: ref kind and value are required", http.StatusBadRequest)
			return
		}
		for _, c := range kind + value {
			if c < 0x20 {
				http.Error(w, "finding: refs cannot contain control characters", http.StatusBadRequest)
				return
			}
		}
		refs = append(refs, map[string]string{"kind": kind, "value": value})
	}
	now := time.Now().UTC()
	data := map[string]interface{}{"category": category, "summary": summary, "refs": refs}
	if req.Priority {
		data["priority"] = true
	}
	if err := rt.Append(event.Event{TS: now, ID: event.NewID(now), Actor: rt.Actor, Type: event.TypeFinding, Data: data}); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeSnapshot(w, rt)
}

func (s uiServer) serveDoc(w http.ResponseWriter, r *http.Request) {
	slug := strings.TrimPrefix(r.URL.Path, "/api/docs/")
	if slug == "" || strings.Contains(slug, "/") || !slugRE.MatchString(slug) {
		http.Error(w, "invalid doc slug", http.StatusBadRequest)
		return
	}
	switch r.Method {
	case http.MethodGet:
		rt, err := Open(OpenOpts{Mutating: false})
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		defer rt.Close()
		raw, err := os.ReadFile(rt.Paths.DocFilePath(slug))
		if err != nil {
			if os.IsNotExist(err) {
				http.Error(w, "doc not found", http.StatusNotFound)
				return
			}
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"slug": slug, "body": string(raw)})
	case http.MethodPut:
		if s.rejectDemo(w, "demo mode is read-only; no doc is written") {
			return
		}
		var req struct {
			Body string `json:"body"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "invalid JSON body", http.StatusBadRequest)
			return
		}
		rt, err := Open(OpenOpts{Mutating: true})
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		defer rt.Close()
		docPath := rt.Paths.DocFilePath(slug)
		if err := os.MkdirAll(filepath.Dir(docPath), 0o755); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		if strings.TrimSpace(req.Body) == "" {
			req.Body = fmt.Sprintf("# %s\n\n", slug)
		}
		if err := os.WriteFile(docPath, []byte(req.Body), 0o644); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		now := time.Now().UTC()
		ev := event.Event{
			TS:    now,
			ID:    event.NewID(now),
			Actor: rt.Actor,
			Type:  event.TypeFinding,
			Data: map[string]interface{}{
				"category": "decision",
				"summary":  fmt.Sprintf("updated doc:%s", slug),
				"refs":     []map[string]string{{"kind": "doc", "value": slug}},
			},
		}
		if err := rt.Append(ev); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		s.writeSnapshot(w, rt)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

type uiSnapshot struct {
	Project  uiProject   `json:"project"`
	Sessions []uiSession `json:"sessions"`
	Claims   []uiClaim   `json:"claims"`
	Findings []uiFinding `json:"findings"`
	Notes    []uiNote    `json:"notes"`
	Docs     []string    `json:"docs"`
	Events   []uiEvent   `json:"events"`
	Updated  time.Time   `json:"updated"`
}

type uiProject struct {
	Name             string `json:"name"`
	Root             string `json:"root"`
	Hash             string `json:"hash"`
	LogPath          string `json:"log_path"`
	Demo             bool   `json:"demo"`
	Actor            string `json:"actor,omitempty"`
	MutationsEnabled bool   `json:"mutations_enabled"`
	MutationMessage  string `json:"mutation_message,omitempty"`
	StaleAfter       string `json:"stale_after"`
}

type uiSession struct {
	Actor    string    `json:"actor"`
	BaseName string    `json:"base_name"`
	Hostname string    `json:"hostname"`
	TS       time.Time `json:"ts"`
	Leader   bool      `json:"leader"`
}

type uiClaim struct {
	ID      string    `json:"id"`
	Actor   string    `json:"actor"`
	Scope   string    `json:"scope"`
	Intent  string    `json:"intent"`
	TS      time.Time `json:"ts"`
	Age     string    `json:"age"`
	Stale   bool      `json:"stale"`
	StoleID string    `json:"stole_id,omitempty"`
}

type uiFinding struct {
	ID       string    `json:"id"`
	Actor    string    `json:"actor"`
	Category string    `json:"category"`
	Summary  string    `json:"summary"`
	Priority bool      `json:"priority"`
	TS       time.Time `json:"ts"`
}

type uiNote struct {
	ID       string    `json:"id"`
	Actor    string    `json:"actor"`
	Body     string    `json:"body"`
	Priority bool      `json:"priority"`
	TS       time.Time `json:"ts"`
}

type uiEvent struct {
	ID      string     `json:"id"`
	Actor   string     `json:"actor"`
	Type    event.Type `json:"type"`
	Scope   []string   `json:"scope,omitempty"`
	Summary string     `json:"summary"`
	TS      time.Time  `json:"ts"`
}

func buildUISnapshot(rt *Runtime, staleAfter time.Duration) uiSnapshot {
	now := time.Now()
	out := uiSnapshot{
		Project: uiProject{
			Name:       rt.Repo.Name,
			Root:       rt.Repo.Root,
			Hash:       rt.Repo.Hash,
			LogPath:    rt.Paths.Log,
			Actor:      rt.Actor,
			StaleAfter: staleAfter.String(),
		},
		Sessions: []uiSession{},
		Claims:   []uiClaim{},
		Findings: []uiFinding{},
		Notes:    []uiNote{},
		Docs:     listDocs(rt.Paths.Docs),
		Events:   []uiEvent{},
		Updated:  now.UTC(),
	}
	if out.Docs == nil {
		out.Docs = []string{}
	}
	if a, err := actor.Resolve(actor.Mutating); err == nil {
		out.Project.Actor = a
		out.Project.MutationsEnabled = true
	} else {
		out.Project.MutationMessage = err.Error()
	}
	sessions := collectActiveSessions(rt.State, now.Add(-4*time.Hour))
	markLeaderSessions(sessions)
	for _, s := range sessions {
		out.Sessions = append(out.Sessions, uiSession{
			Actor: s.Actor, BaseName: s.BaseName, Hostname: s.Hostname, TS: s.TS, Leader: s.Leader,
		})
	}
	for _, c := range sortedClaims(rt.State) {
		out.Claims = append(out.Claims, uiClaim{
			ID: c.ID, Actor: c.Actor, Scope: c.Scope.String(), Intent: c.Intent,
			TS: c.TS, Age: shortAge(now.Sub(c.TS)), Stale: now.Sub(c.TS) >= staleAfter, StoleID: c.StolenFromID,
		})
	}
	for _, f := range recentFindings(rt.State, now.Add(-24*time.Hour), 12) {
		out.Findings = append(out.Findings, uiFinding{
			ID: f.ID, Actor: f.Actor, Category: f.Category, Summary: f.Summary, Priority: f.Priority, TS: f.TS,
		})
	}
	for _, n := range recentNotes(rt.State, now.Add(-24*time.Hour), 8) {
		out.Notes = append(out.Notes, uiNote{ID: n.ID, Actor: n.Actor, Body: n.Body, Priority: n.Priority, TS: n.TS})
	}
	events := append([]event.Event(nil), rt.Events...)
	sort.Slice(events, func(i, j int) bool { return events[i].TS.After(events[j].TS) })
	if len(events) > 80 {
		events = events[:80]
	}
	for _, ev := range events {
		out.Events = append(out.Events, uiEvent{
			ID: ev.ID, Actor: ev.Actor, Type: ev.Type, Scope: ev.Scope,
			Summary: eventSummary(ev), TS: ev.TS,
		})
	}
	return out
}

func buildDemoUISnapshot(staleAfter time.Duration) uiSnapshot {
	base := time.Date(2026, 5, 27, 10, 24, 0, 0, time.UTC)
	claims := []uiClaim{
		{ID: "01JX2Q3Y7W5B6N9P0R1S2T3U4V", Actor: "codex-20260527-a", Scope: "src/aggregate/lead_counter.ts#L40-90", Intent: "fix lead double-counting in aggregation loop", TS: base.Add(-12 * time.Minute), Age: "12m"},
		{ID: "01JX2Q3W5V3B6N9P0R1S2T3U4V", Actor: "claude-20260527-a", Scope: "src/auth/token.ts#validateToken", Intent: "tighten JWT expiry validation", TS: base.Add(-18 * time.Minute), Age: "18m"},
		{ID: "01JX2Q3V4V2B6N9P0R1S2T3U4V", Actor: "codex-9b2c", Scope: "prisma/schema.prisma#User", Intent: "review user model constraints", TS: base.Add(-7 * time.Hour), Age: "7h"},
	}
	for i := range claims {
		claims[i].Stale = base.Sub(claims[i].TS) >= staleAfter
	}
	return uiSnapshot{
		Project: uiProject{
			Name:            "demo-project",
			Root:            "/demo/comms-project",
			Hash:            "3b9c1f2a77e4",
			LogPath:         "demo mode: sample events only; no log file is written",
			Demo:            true,
			MutationMessage: "Demo mode is read-only; release actions are disabled.",
			StaleAfter:      staleAfter.String(),
		},
		Sessions: []uiSession{
			{Actor: "codex-20260527-a", BaseName: "codex", Hostname: "MacBook-Pro.local", TS: base.Add(-13 * time.Minute), Leader: true},
			{Actor: "claude-20260527-a", BaseName: "claude", Hostname: "MacBook-Pro.local", TS: base.Add(-12 * time.Minute)},
			{Actor: "human-eli", BaseName: "human", Hostname: "MacBook-Pro.local", TS: base.Add(-2 * time.Hour)},
		},
		Claims: claims,
		Findings: []uiFinding{
			{ID: "01JX2Q3P9P9B6N9P0R1S2T3U4V", Actor: "codex-20260527-a", Category: "decision", Summary: "everyone should check live Meta numbers before shipping", Priority: true, TS: base.Add(-2 * time.Minute)},
			{ID: "01JX2Q3T2U9B6N9P0R1S2T3U4V", Actor: "codex-20260527-a", Category: "fix", Summary: "leads sourced only from tracker overlay", TS: base.Add(-4 * time.Minute)},
			{ID: "01JX2Q3S1T8B6N9P0R1S2T3U4V", Actor: "claude-20260527-a", Category: "decision", Summary: "tracker is source of truth for leads", TS: base.Add(-19 * time.Minute)},
			{ID: "01JX2Q3R0S7B6N9P0R1S2T3U4V", Actor: "codex-9b2c", Category: "gotcha", Summary: "whole-file prisma claims require an anchor", TS: base.Add(-47 * time.Minute)},
		},
		Notes: []uiNote{
			{ID: "01JX2Q3P8P8B6N9P0R1S2T3U4V", Actor: "codex-20260527-a", Body: "Everyone pause before touching aggregation until claim clears.", Priority: true, TS: base.Add(-1 * time.Minute)},
			{ID: "01JX2Q3Q0R6B6N9P0R1S2T3U4V", Actor: "claude-20260527-a", Body: "FYI Prisma schema migration coming next session", TS: base.Add(-8 * time.Minute)},
			{ID: "01JX2Q3P0Q5B6N9P0R1S2T3U4V", Actor: "codex-9b2c", Body: "@claude-20260527-a can I take src/auth/token.ts when you're done?", TS: base.Add(-14 * time.Minute)},
		},
		Docs: []string{"lead-counting", "tracker-architecture", "ui"},
		Events: []uiEvent{
			{ID: "01JX2Q3T2U9B6N9P0R1S2T3U4V", Actor: "codex-20260527-a", Type: event.TypeFinding, Summary: "fix: leads sourced only from tracker overlay", TS: base.Add(-4 * time.Minute)},
			{ID: "01JX2Q3Q0R6B6N9P0R1S2T3U4V", Actor: "claude-20260527-a", Type: event.TypeNote, Summary: "FYI Prisma schema migration coming next session", TS: base.Add(-8 * time.Minute)},
			{ID: "01JX2Q3Y7W5B6N9P0R1S2T3U4V", Actor: "codex-20260527-a", Type: event.TypeClaim, Scope: []string{"src/aggregate/lead_counter.ts#L40-90"}, Summary: "fix lead double-counting in aggregation loop", TS: base.Add(-12 * time.Minute)},
			{ID: "01JX2Q3X6V4B6N9P0R1S2T3U4V", Actor: "claude-20260527-a", Type: event.TypeHello, Summary: "", TS: base.Add(-12 * time.Minute)},
			{ID: "01JX2Q3Z5V6B6N9P0R1S2T3U4V", Actor: "codex-20260527-a", Type: event.TypeHello, Summary: "", TS: base.Add(-13 * time.Minute)},
			{ID: "01JX2Q3S1T8B6N9P0R1S2T3U4V", Actor: "claude-20260527-a", Type: event.TypeFinding, Summary: "decision: tracker is source of truth for leads", TS: base.Add(-19 * time.Minute)},
		},
		Updated: base.Add(18 * time.Second),
	}
}

func eventSummary(ev event.Event) string {
	switch ev.Type {
	case event.TypeClaim:
		if s, _ := ev.Data["intent"].(string); s != "" {
			return s
		}
	case event.TypeRelease:
		if s, _ := ev.Data["result"].(string); s != "" {
			return s
		}
		if s, _ := ev.Data["reason"].(string); s != "" {
			return s
		}
	case event.TypeFinding:
		cat, _ := ev.Data["category"].(string)
		sum, _ := ev.Data["summary"].(string)
		if dataBool(ev.Data, "priority") {
			sum = "PRIORITY: " + sum
		}
		if cat != "" {
			return cat + ": " + sum
		}
		return sum
	case event.TypeNote:
		if s, _ := ev.Data["body"].(string); s != "" {
			if dataBool(ev.Data, "priority") {
				return "PRIORITY: " + s
			}
			return s
		}
	}
	return ""
}

func dataBool(m map[string]interface{}, key string) bool {
	if v, ok := m[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return false
}

func (s uiServer) rejectDemo(w http.ResponseWriter, msg string) bool {
	if !s.demo {
		return false
	}
	http.Error(w, msg, http.StatusConflict)
	return true
}

func (s uiServer) writeSnapshot(w http.ResponseWriter, rt *Runtime) {
	writeJSON(w, http.StatusOK, buildUISnapshot(rt, s.staleAfter))
}

func writeJSON(w http.ResponseWriter, status int, value interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(value)
}

func uiClaimsFromState(claims []*state.Claim, staleAfter time.Duration) []uiClaim {
	now := time.Now()
	out := make([]uiClaim, 0, len(claims))
	for _, c := range claims {
		out = append(out, uiClaim{
			ID: c.ID, Actor: c.Actor, Scope: c.Scope.String(), Intent: c.Intent,
			TS: c.TS, Age: shortAge(now.Sub(c.TS)), Stale: now.Sub(c.TS) >= staleAfter, StoleID: c.StolenFromID,
		})
	}
	return out
}

func (s uiServer) actorIsLeader(rt *Runtime) bool {
	return activeLeaderActor(rt.State, time.Now().Add(-4*time.Hour)) == rt.Actor
}

func (s uiServer) leaderOnlyMessage(rt *Runtime) string {
	leader := activeLeaderActor(rt.State, time.Now().Add(-4*time.Hour))
	if leader == "" {
		return "priority messages require an active leader; run hello first"
	}
	return fmt.Sprintf("priority messages are leader-only; current leader is @%s", leader)
}

func shortAge(d time.Duration) string {
	if d < time.Minute {
		return "now"
	}
	if d < time.Hour {
		return fmt.Sprintf("%dm", int(d.Minutes()))
	}
	if d < 24*time.Hour {
		return fmt.Sprintf("%dh", int(d.Hours()))
	}
	return fmt.Sprintf("%dd", int(d.Hours()/24))
}

const uiHTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>comms dashboard</title>
<style>
:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --surface: #ffffff;
  --line: #dfe4ea;
  --text: #17202a;
  --muted: #667280;
  --soft: #eef2f5;
  --teal: #0f766e;
  --amber: #b45309;
  --red: #b42318;
  --red-soft: #fff1f0;
  --shadow: 0 10px 30px rgba(23, 32, 42, 0.07);
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0f1419;
  --surface: #151b22;
  --line: #2c3642;
  --text: #e7edf3;
  --muted: #9aa8b6;
  --soft: #202833;
  --teal: #4fd1c5;
  --amber: #f6ad55;
  --red: #ff6b6b;
  --red-soft: #3b1b1b;
  --shadow: 0 12px 32px rgba(0, 0, 0, 0.34);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  letter-spacing: 0;
}
header {
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  z-index: 2;
}
h1 { margin: 0; font-size: 18px; font-weight: 680; }
.sub { color: var(--muted); font-size: 12px; margin-top: 3px; }
.log-path {
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  margin-top: 4px;
  overflow-wrap: anywhere;
}
.demo-mark {
  color: var(--amber);
  font-weight: 700;
}
.top-actions { display: flex; gap: 8px; align-items: center; }
button {
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--text);
  height: 34px;
  padding: 0 12px;
  border-radius: 6px;
  font: inherit;
  cursor: pointer;
}
button:hover { border-color: #aab4c0; }
:root[data-theme="dark"] button:hover { border-color: #586679; }
button.danger {
  border-color: var(--red);
  color: var(--red);
}
button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.error-banner {
  display: none;
  margin: 12px 18px 0;
  padding: 10px 12px;
  border: 1px solid var(--red);
  border-radius: 8px;
  color: var(--red);
  background: var(--red-soft);
}
.status-dot {
  width: 9px;
  height: 9px;
  border-radius: 99px;
  background: var(--teal);
  display: inline-block;
  margin-right: 7px;
}
main {
  padding: 18px;
  display: grid;
  grid-template-columns: 280px minmax(420px, 1fr) 360px;
  gap: 14px;
}
.panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
  overflow: hidden;
}
.panel h2 {
  margin: 0;
  padding: 12px 14px;
  font-size: 13px;
  text-transform: uppercase;
  color: var(--muted);
  border-bottom: 1px solid var(--line);
  letter-spacing: 0;
}
.stack { display: grid; gap: 14px; }
.row {
  padding: 12px 14px;
  border-bottom: 1px solid var(--soft);
}
.row:last-child { border-bottom: 0; }
.actor { font-weight: 680; }
.meta { color: var(--muted); font-size: 12px; margin-top: 4px; overflow-wrap: anywhere; }
.scope { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; font-weight: 650; }
.intent { margin-top: 5px; }
.empty { padding: 16px 14px; color: var(--muted); }
.hint {
  padding: 10px 14px;
  color: var(--muted);
  border-bottom: 1px solid var(--soft);
  font-size: 12px;
}
.claims table, .events table {
  width: 100%;
  border-collapse: collapse;
}
th, td {
  text-align: left;
  padding: 10px 12px;
  border-bottom: 1px solid var(--soft);
  vertical-align: top;
}
th {
  font-size: 12px;
  color: var(--muted);
  font-weight: 650;
}
.pill {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  background: var(--soft);
  color: var(--muted);
  font-size: 12px;
  font-weight: 620;
}
.pill.claim { color: var(--teal); background: #def7f2; }
.pill.release { color: var(--amber); background: #fff0d6; }
.pill.note { color: #475467; background: #eef2f5; }
.pill.finding { color: #175cd3; background: #e7f0ff; }
.pill.stale { color: var(--red); background: var(--red-soft); }
.pill.priority { color: #7c2d12; background: #ffedd5; }
.pill.leader { color: var(--teal); background: #def7f2; margin-left: 6px; }
:root[data-theme="dark"] .pill.claim { color: #7ddbd3; background: #123d3a; }
:root[data-theme="dark"] .pill.release { color: #ffd39b; background: #4b310f; }
:root[data-theme="dark"] .pill.note { color: #c3ccd6; background: #28323d; }
:root[data-theme="dark"] .pill.finding { color: #9fc4ff; background: #18345c; }
:root[data-theme="dark"] .pill.priority { color: #ffd39b; background: #4b310f; }
:root[data-theme="dark"] .pill.leader { color: #7ddbd3; background: #123d3a; }
.claim-stale td {
  background: var(--red-soft);
}
.action-note {
  color: var(--muted);
  font-size: 12px;
  margin-top: 5px;
}
.events {
  grid-column: 1 / -1;
}
.actions {
  grid-column: 1 / -1;
}
.action-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(160px, 1fr));
  gap: 12px;
  padding: 14px;
}
.action-card {
  border: 1px solid var(--soft);
  border-radius: 8px;
  padding: 12px;
  display: grid;
  gap: 8px;
  align-content: start;
}
.action-card.wide {
  grid-column: span 2;
}
.action-title {
  font-weight: 680;
  font-size: 13px;
}
input, textarea, select {
  width: 100%;
  border: 1px solid var(--line);
  background: var(--bg);
  color: var(--text);
  border-radius: 6px;
  min-height: 34px;
  padding: 8px 10px;
  font: inherit;
}
textarea {
  min-height: 72px;
  resize: vertical;
}
label.checkline {
  display: flex;
  gap: 8px;
  align-items: center;
  color: var(--muted);
  font-size: 12px;
}
label.checkline input {
  width: auto;
  min-height: auto;
}
.result {
  grid-column: 1 / -1;
  min-height: 20px;
  color: var(--muted);
  font-size: 12px;
}
.copy {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}
@media (max-width: 1050px) {
  main { grid-template-columns: 1fr; }
  .events { grid-column: auto; }
  .actions { grid-column: auto; }
  .action-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
  header {
    height: auto;
    min-height: 64px;
    padding: 12px 18px;
    gap: 10px;
    align-items: flex-start;
  }
  h1 { font-size: 17px; }
  main { padding: 10px; gap: 12px; }
  .top-actions { align-items: flex-start; }
  .action-grid { grid-template-columns: 1fr; }
  .action-card.wide { grid-column: auto; }
  .claims table,
  .claims thead,
  .claims tbody,
  .claims tr,
  .claims th,
  .claims td,
  .events table,
  .events thead,
  .events tbody,
  .events tr,
  .events th,
  .events td {
    display: block;
    width: 100%;
  }
  .claims thead,
  .events thead { display: none; }
  .claims tr,
  .events tr {
    padding: 10px 12px;
    border-bottom: 1px solid var(--soft);
  }
  .claims td,
  .events td {
    border: 0;
    padding: 3px 0;
  }
  .claims td::before,
  .events td::before {
    display: inline-block;
    min-width: 72px;
    margin-right: 8px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 650;
  }
  .claims td:nth-child(1)::before { content: "Actor"; }
  .claims td:nth-child(2)::before { content: "Scope"; }
  .claims td:nth-child(3)::before { content: "Intent"; }
  .claims td:nth-child(4)::before { content: "Age"; }
  .claims td:nth-child(5)::before { content: "Action"; }
  .events td:nth-child(1)::before { content: "When"; }
  .events td:nth-child(2)::before { content: "Type"; }
  .events td:nth-child(3)::before { content: "Actor"; }
  .events td:nth-child(4)::before { content: "Scope"; }
  .events td:nth-child(5)::before { content: "Summary"; }
}
</style>
</head>
<body>
<header>
  <div>
    <h1 id="project">comms dashboard</h1>
    <div class="sub" id="projectMeta">Loading project state...</div>
    <div class="log-path" id="logPath"></div>
  </div>
  <div class="top-actions">
    <span class="sub"><span class="status-dot"></span><span id="updated">live</span></span>
    <button id="theme" type="button" aria-label="Toggle dark mode">Dark</button>
    <button id="refresh" type="button">Refresh</button>
  </div>
</header>
<div id="error" class="error-banner"></div>
<main>
  <section class="panel actions">
    <h2>Actions</h2>
    <div class="action-grid">
      <div class="action-card">
        <div class="action-title">Session</div>
        <button id="helloAction" type="button">Hello</button>
        <div class="meta">Announce this UI actor in the log.</div>
      </div>
      <form class="action-card" id="checkForm">
        <div class="action-title">Check Path</div>
        <input name="path" placeholder="src/file.ts or src/file.ts#L1-20" autocomplete="off">
        <button type="submit">Check</button>
      </form>
      <form class="action-card wide" id="claimForm">
        <div class="action-title">Claim</div>
        <input name="scope" placeholder="src/file.ts#symbol" autocomplete="off">
        <input name="intent" placeholder="intent" autocomplete="off">
        <input name="steal" placeholder="optional steal claim id" autocomplete="off">
        <input name="reason" placeholder="steal reason" autocomplete="off">
        <button type="submit">Claim</button>
      </form>
      <form class="action-card wide" id="noteForm">
        <div class="action-title">Note</div>
        <textarea name="body" maxlength="200" placeholder="short FYI for this repo"></textarea>
        <label class="checkline"><input name="priority" type="checkbox"> priority (leader only)</label>
        <button type="submit">Post Note</button>
      </form>
      <form class="action-card wide" id="findingForm">
        <div class="action-title">Finding</div>
        <select name="category">
          <option value="bug">bug</option>
          <option value="fix">fix</option>
          <option value="ship">ship</option>
          <option value="decision">decision</option>
          <option value="gotcha">gotcha</option>
        </select>
        <input name="summary" placeholder="summary" autocomplete="off">
        <input name="refs" placeholder="refs, comma-separated kind:value" autocomplete="off">
        <label class="checkline"><input name="priority" type="checkbox"> priority (leader only)</label>
        <button type="submit">Record Finding</button>
      </form>
      <form class="action-card wide" id="docForm">
        <div class="action-title">Doc</div>
        <input name="slug" placeholder="slug e.g. lead-counting" autocomplete="off">
        <button name="load" value="1" type="submit">Load Doc</button>
        <textarea name="body" placeholder="# slug&#10;&#10;Markdown body"></textarea>
        <button name="save" value="1" type="submit">Save Doc</button>
      </form>
      <div class="result" id="actionResult"></div>
    </div>
  </section>
  <section class="panel">
    <h2>Active Sessions</h2>
    <div id="sessions"></div>
  </section>
  <section class="panel claims">
    <h2>Active Claims</h2>
    <div id="claims"></div>
  </section>
  <div class="stack">
    <section class="panel">
      <h2>Recent Findings</h2>
      <div id="findings"></div>
    </section>
    <section class="panel">
      <h2>Recent Notes</h2>
      <div id="notes"></div>
    </section>
    <section class="panel">
      <h2>Docs</h2>
      <div id="docs"></div>
    </section>
  </div>
  <section class="panel events">
    <h2>Raw Event Log</h2>
    <div class="hint">This is already the raw append-only event feed. Releases add new rows; old rows are never deleted.</div>
    <div id="events"></div>
  </section>
</main>
<script>
const el = id => document.getElementById(id);
const fmtTime = ts => new Date(ts).toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const mediaPrefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
function preferredTheme() {
  const saved = localStorage.getItem('theme');
  if (saved === 'dark' || saved === 'light') return saved;
  return mediaPrefersDark && mediaPrefersDark.matches ? 'dark' : 'light';
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  el('theme').textContent = theme === 'dark' ? 'Light' : 'Dark';
  el('theme').setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
}
applyTheme(preferredTheme());
function empty(label) { return '<div class="empty">' + label + '</div>'; }
function renderRows(items, fn, label) {
  return items.length ? items.map(fn).join('') : empty(label);
}
function renderTable(items, headers, fn, label) {
  if (!items.length) return empty(label);
  return '<table><thead><tr>' + headers.map(h => '<th>' + h + '</th>').join('') + '</tr></thead><tbody>' + items.map(fn).join('') + '</tbody></table>';
}
function mutationHelp(data) {
  if (data.project.demo) return 'Demo mode is read-only.';
  if (data.project.mutations_enabled) return 'Clear buttons append release events by @' + data.project.actor + '; old log rows stay intact.';
  return data.project.mutation_message || 'Set COMMS_ACTOR before starting comms ui to clear claims here.';
}
function setActionResult(text, isError = false) {
  el('actionResult').textContent = text;
  el('actionResult').style.color = isError ? 'var(--red)' : 'var(--muted)';
}
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  const text = await res.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch {}
  if (!res.ok) throw new Error(json?.error || text || res.statusText);
  return json;
}
async function load() {
  const res = await fetch('/api/status', { cache: 'no-store' });
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  hideError();
  el('project').textContent = data.project.name + ' / comms';
  el('projectMeta').innerHTML = esc(data.project.hash) + ' · ' + esc(data.project.root) + (data.project.demo ? ' · <span class="demo-mark">demo mode</span>' : '');
  el('logPath').textContent = 'Log: ' + data.project.log_path;
  el('updated').textContent = 'updated ' + fmtTime(data.updated);
  el('sessions').innerHTML = renderRows(data.sessions, s =>
    '<div class="row"><div class="actor">@' + esc(s.actor) + (s.leader ? ' <span class="pill leader">leader</span>' : '') + '</div><div class="meta">' + esc(s.base_name || 'session') + ' · ' + esc(s.hostname || 'unknown host') + ' · hello ' + fmtTime(s.ts) + '</div></div>',
    'No active sessions in the last 4h.');
  el('claims').innerHTML = '<div class="hint">Claims older than ' + esc(data.project.stale_after) + ' are marked stale. ' + esc(mutationHelp(data)) + '</div>' +
    renderTable(data.claims, ['Actor', 'Scope', 'Intent', 'Age', 'Action'], c =>
    '<tr class="' + (c.stale ? 'claim-stale' : '') + '"><td><span class="actor">@' + esc(c.actor) + '</span></td><td><div class="scope">' + esc(c.scope) + '</div><div class="copy">' + esc(c.id.slice(0, 10)) + '</div></td><td>' + esc(c.intent) + '</td><td>' + esc(c.age) + (c.stale ? '<div><span class="pill stale">stale</span></div>' : '') + '</td><td>' + claimAction(data, c) + '</td></tr>',
    'No active claims.');
  el('findings').innerHTML = renderRows(data.findings, f =>
    '<div class="row">' + (f.priority ? '<span class="pill priority">priority</span> ' : '') + '<span class="pill finding">' + esc(f.category) + '</span><div class="intent">' + esc(f.summary) + '</div><div class="meta">@' + esc(f.actor) + ' · ' + fmtTime(f.ts) + '</div></div>',
    'No findings in the last 24h.');
  el('notes').innerHTML = renderRows(data.notes, n =>
    '<div class="row">' + (n.priority ? '<span class="pill priority">priority</span>' : '') + '<div>' + esc(n.body) + '</div><div class="meta">@' + esc(n.actor) + ' · ' + fmtTime(n.ts) + '</div></div>',
    'No notes in the last 24h.');
  el('docs').innerHTML = renderRows(data.docs, d =>
    '<div class="row"><span class="scope">' + esc(d) + '</span><div class="copy">comms doc ' + esc(d) + '</div></div>',
    'No docs yet.');
  el('events').innerHTML = renderTable(data.events, ['When', 'Type', 'Actor', 'Scope', 'Summary'], ev =>
    '<tr><td>' + fmtTime(ev.ts) + '</td><td><span class="pill ' + esc(ev.type) + '">' + esc(ev.type) + '</span></td><td>@' + esc(ev.actor) + '</td><td><span class="scope">' + esc((ev.scope || []).join(', ')) + '</span></td><td>' + esc(ev.summary) + '</td></tr>',
    'No log events yet. Run comms hello, comms claim, comms note, or start with comms ui --demo.');
}
function claimAction(data, c) {
  if (data.project.demo) return '<span class="action-note">demo only</span>';
  if (!data.project.mutations_enabled) return '<span class="action-note">start UI with COMMS_ACTOR to clear</span>';
  const label = c.stale ? 'Release' : 'Clear';
  return '<button class="danger" type="button" data-release="' + esc(c.id) + '" data-actor="' + esc(c.actor) + '" data-stale="' + (c.stale ? '1' : '0') + '">' + label + '</button>';
}
async function releaseClaim(id, holder, stale) {
  const action = stale ? 'Release stale claim' : 'Clear active claim';
  const defaultReason = stale ? 'user verified prior session ended' : 'user cleared claim from UI';
  const reason = window.prompt(action + ' held by @' + holder + '? Reason for audit log:', defaultReason);
  if (reason === null) return;
  const res = await fetch('/api/claims/release', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, reason, force: !stale })
  });
  if (!res.ok) throw new Error(await res.text());
  hideError();
  await load();
}
document.addEventListener('click', event => {
  const btn = event.target.closest('[data-release]');
  if (!btn) return;
  btn.disabled = true;
  releaseClaim(btn.getAttribute('data-release'), btn.getAttribute('data-actor'), btn.getAttribute('data-stale') === '1').catch(err => {
    btn.disabled = false;
    showError(err);
  });
});
el('helloAction').addEventListener('click', async () => {
  try {
    await postJSON('/api/hello', {});
    setActionResult('Session hello recorded.');
    await load();
  } catch (err) {
    setActionResult(err.message.trim(), true);
    showError(err);
  }
});
el('checkForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const result = await postJSON('/api/check', { path: form.path.value.trim() });
    if (result.clear) {
      setActionResult('Clear: ' + result.scope);
    } else {
      setActionResult('Blocked by ' + result.conflicts.map(c => '@' + c.actor + ' on ' + c.scope).join(', '), true);
    }
  } catch (err) {
    setActionResult(err.message.trim(), true);
    showError(err);
  }
});
el('claimForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await postJSON('/api/claims', {
      scope: form.scope.value.trim(),
      intent: form.intent.value.trim(),
      steal_id: form.steal.value.trim(),
      steal_reason: form.reason.value.trim()
    });
    form.reset();
    setActionResult('Claim recorded.');
    await load();
  } catch (err) {
    setActionResult(err.message.trim(), true);
    showError(err);
  }
});
el('noteForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await postJSON('/api/notes', { body: form.body.value.trim(), priority: form.priority.checked });
    form.reset();
    setActionResult('Note recorded.');
    await load();
  } catch (err) {
    setActionResult(err.message.trim(), true);
    showError(err);
  }
});
el('findingForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const refs = form.refs.value.split(',').map(v => v.trim()).filter(Boolean).map(v => {
    const idx = v.indexOf(':');
    return idx > 0 ? { kind: v.slice(0, idx), value: v.slice(idx + 1) } : { kind: '', value: v };
  });
  try {
    await postJSON('/api/findings', {
      category: form.category.value,
      summary: form.summary.value.trim(),
      refs,
      priority: form.priority.checked
    });
    form.reset();
    setActionResult('Finding recorded.');
    await load();
  } catch (err) {
    setActionResult(err.message.trim(), true);
    showError(err);
  }
});
el('docForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const slug = form.slug.value.trim();
  const submitter = event.submitter?.name || 'load';
  try {
    if (submitter === 'load') {
      const res = await fetch('/api/docs/' + encodeURIComponent(slug), { cache: 'no-store' });
      const text = await res.text();
      if (!res.ok) throw new Error(text || res.statusText);
      const doc = JSON.parse(text);
      form.body.value = doc.body;
      setActionResult('Loaded doc:' + slug);
      return;
    }
    const res = await fetch('/api/docs/' + encodeURIComponent(slug), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body: form.body.value })
    });
    const text = await res.text();
    if (!res.ok) throw new Error(text || res.statusText);
    setActionResult('Saved doc:' + slug);
    await load();
  } catch (err) {
    setActionResult(err.message.trim(), true);
    showError(err);
  }
});
el('theme').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('theme', next);
  applyTheme(next);
});
el('refresh').addEventListener('click', () => load().catch(showError));
function showError(err) {
  el('error').style.display = 'block';
  el('error').textContent = 'Error: ' + err.message.trim();
}
function hideError() {
  el('error').style.display = 'none';
  el('error').textContent = '';
}
load().catch(showError);
setInterval(() => load().catch(showError), 2000);
</script>
</body>
</html>`
