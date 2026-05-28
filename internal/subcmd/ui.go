package subcmd

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/actor"
	"github.com/dpa-plus/comms/internal/event"
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
append start/end boundary events when COMMS_ACTOR is set; it never edits or
deletes existing log lines. The dashboard slices the append-only JSONL into
per-session logs for the current comms session and archived comms sessions.

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
	mux.HandleFunc("/api/comms-session/start", server.serveStartCommsSession)
	mux.HandleFunc("/api/comms-session/end", server.serveEndCommsSession)
	mux.HandleFunc("/api/session/retire", server.serveRetireSessionActor)
	mux.HandleFunc("/api/session/lead", server.serveTransferLeader)

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

func (s uiServer) serveRetireSessionActor(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no actor retire event is written", http.StatusConflict)
		return
	}
	var req struct {
		Actor  string `json:"actor"`
		Reason string `json:"reason"`
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
	if _, err := appendSessionRetire(rt, req.Actor, req.Reason); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	s.writeSnapshot(w, rt)
}

func (s uiServer) serveTransferLeader(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no leader transfer event is written", http.StatusConflict)
		return
	}
	var req struct {
		Actor  string `json:"actor"`
		Reason string `json:"reason"`
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
	if err := appendLeaderTransfer(rt, req.Actor, req.Reason); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	s.writeSnapshot(w, rt)
}

func (s uiServer) serveStartCommsSession(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no comms session start event is written", http.StatusConflict)
		return
	}
	var req struct {
		Reason string `json:"reason"`
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
	current, _ := buildCommsSessionViews(rt.Events)
	if current != nil || len(rt.State.Sessions) > 0 || len(rt.State.Claims) > 0 {
		http.Error(w, "a comms session is already active; end it before starting a new one", http.StatusConflict)
		return
	}
	reason := strings.TrimSpace(req.Reason)
	if reason == "" {
		reason = "comms session started from ui"
	}
	now := time.Now().UTC()
	hostname, _ := os.Hostname()
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeHello,
		Data: map[string]interface{}{
			"base_name":           baseNameOfActor(rt.Actor),
			"hostname":            hostname,
			"comms_session_start": true,
			"reason":              reason,
		},
	}
	if err := rt.Append(ev); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeSnapshot(w, rt)
}

func (s uiServer) serveEndCommsSession(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no comms session end event is written", http.StatusConflict)
		return
	}
	var req struct {
		Reason string `json:"reason"`
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
	current, _ := buildCommsSessionViews(rt.Events)
	if current == nil {
		http.Error(w, "no active comms session to end", http.StatusConflict)
		return
	}
	refs := make([]interface{}, 0, len(rt.State.Claims))
	for _, claim := range sortedClaims(rt.State) {
		refs = append(refs, claim.ID)
	}
	endedActors := make([]interface{}, 0, len(rt.State.Sessions))
	for _, session := range collectActiveSessions(rt.State, time.Time{}) {
		endedActors = append(endedActors, session.Actor)
	}
	reason := strings.TrimSpace(req.Reason)
	if reason == "" {
		reason = "comms session ended from ui"
	}
	now := time.Now().UTC()
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeRelease,
		Data: map[string]interface{}{
			"refs":              refs,
			"comms_session_end": true,
			"ended_actors":      endedActors,
			"reason":            reason,
		},
	}
	if err := rt.Append(ev); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeSnapshot(w, rt)
}

type uiSnapshot struct {
	Project       uiProject        `json:"project"`
	Current       *uiCommsSession  `json:"current_session,omitempty"`
	Actions       []uiAction       `json:"actions"`
	Sessions      []uiSession      `json:"sessions"`
	CommsSessions []uiCommsSession `json:"comms_sessions"`
	Claims        []uiClaim        `json:"claims"`
	Findings      []uiFinding      `json:"findings"`
	Notes         []uiNote         `json:"notes"`
	Docs          []string         `json:"docs"`
	Lessons       []string         `json:"lessons"`
	Events        []uiEvent        `json:"events"`
	Updated       time.Time        `json:"updated"`
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

type uiAction struct {
	ID      string `json:"id"`
	Label   string `json:"label"`
	Method  string `json:"method,omitempty"`
	Path    string `json:"path,omitempty"`
	Enabled bool   `json:"enabled"`
	Reason  string `json:"reason,omitempty"`
}

type uiSession struct {
	Actor    string    `json:"actor"`
	Label    string    `json:"label,omitempty"`
	BaseName string    `json:"base_name"`
	Hostname string    `json:"hostname"`
	TS       time.Time `json:"ts"`
	Leader   bool      `json:"leader"`
}

type uiCommsSession struct {
	ID           string    `json:"id"`
	StartedAt    time.Time `json:"started_at"`
	EndedAt      time.Time `json:"ended_at"`
	EndedBy      string    `json:"ended_by"`
	Reason       string    `json:"reason"`
	Actors       []string  `json:"actors"`
	ReleasedRefs int       `json:"released_refs"`
	EventCount   int       `json:"event_count"`
	ClaimCount   int       `json:"claim_count"`
	FindingCount int       `json:"finding_count"`
	NoteCount    int       `json:"note_count"`
	Events       []uiEvent `json:"events,omitempty"`
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
		Actions:       []uiAction{},
		Sessions:      []uiSession{},
		CommsSessions: []uiCommsSession{},
		Claims:        []uiClaim{},
		Findings:      []uiFinding{},
		Notes:         []uiNote{},
		Docs:          listDocs(rt.Paths.Docs),
		Lessons:       listGlobalLessons(),
		Events:        []uiEvent{},
		Updated:       now.UTC(),
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
			Actor: s.Actor, Label: s.Label, BaseName: s.BaseName, Hostname: s.Hostname, TS: s.TS, Leader: s.Leader,
		})
	}
	out.Current, out.CommsSessions = buildCommsSessionViews(rt.Events)
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
	if out.Current != nil {
		out.Events = out.Current.Events
	} else if len(out.CommsSessions) > 0 {
		out.Events = out.CommsSessions[0].Events
	}
	out.Actions = buildUIActions(out)
	return out
}

func buildDemoUISnapshot(staleAfter time.Duration) uiSnapshot {
	base := time.Date(2026, 5, 27, 10, 24, 0, 0, time.UTC)
	currentEvents := []uiEvent{
		{ID: "01JX2Q3P8P8B6N9P0R1S2T3U4V", Actor: "codex-dev", Type: event.TypeNote, Summary: "PRIORITY: Everyone pause before touching aggregation until claim clears.", TS: base.Add(-1 * time.Minute)},
		{ID: "01JX2Q3P9P9B6N9P0R1S2T3U4V", Actor: "codex-dev", Type: event.TypeFinding, Summary: "PRIORITY: decision: everyone should check live Meta numbers before shipping", TS: base.Add(-2 * time.Minute)},
		{ID: "01JX2Q3T2U9B6N9P0R1S2T3U4V", Actor: "codex-dev", Type: event.TypeFinding, Summary: "fix: leads sourced only from tracker overlay", TS: base.Add(-4 * time.Minute)},
		{ID: "01JX2Q3Q0R6B6N9P0R1S2T3U4V", Actor: "claude-dev", Type: event.TypeNote, Summary: "FYI Prisma schema migration coming next session", TS: base.Add(-8 * time.Minute)},
		{ID: "01JX2Q3Y7W5B6N9P0R1S2T3U4V", Actor: "codex-dev", Type: event.TypeClaim, Scope: []string{"src/aggregate/lead_counter.ts#L40-90"}, Summary: "fix lead double-counting in aggregation loop", TS: base.Add(-12 * time.Minute)},
		{ID: "01JX2Q3X6V4B6N9P0R1S2T3U4V", Actor: "claude-dev", Type: event.TypeHello, Summary: "", TS: base.Add(-12 * time.Minute)},
		{ID: "01JX2Q3Z5V6B6N9P0R1S2T3U4V", Actor: "codex-dev", Type: event.TypeHello, Summary: "started comms session: demo preview", TS: base.Add(-13 * time.Minute)},
	}
	archivedEvents := []uiEvent{
		{ID: "01JX2Q3M6M6B6N9P0R1S2T3U4V", Actor: "human-eli", Type: event.TypeRelease, Summary: "ended comms session; released 2 claims", TS: base.Add(-30 * time.Minute)},
		{ID: "01JX2Q3S1T8B6N9P0R1S2T3U4V", Actor: "claude-morning", Type: event.TypeFinding, Summary: "decision: tracker is source of truth for leads", TS: base.Add(-45 * time.Minute)},
		{ID: "01JX2Q3A1A1B6N9P0R1S2T3U4V", Actor: "codex-morning", Type: event.TypeClaim, Scope: []string{"src/auth/token.ts#validateToken"}, Summary: "review token expiry handling", TS: base.Add(-55 * time.Minute)},
		{ID: "01JX2Q39191B6N9P0R1S2T3U4V", Actor: "claude-morning", Type: event.TypeHello, Summary: "started comms session: morning verification", TS: base.Add(-2 * time.Hour)},
	}
	claims := []uiClaim{
		{ID: "01JX2Q3Y7W5B6N9P0R1S2T3U4V", Actor: "codex-dev", Scope: "src/aggregate/lead_counter.ts#L40-90", Intent: "fix lead double-counting in aggregation loop", TS: base.Add(-12 * time.Minute), Age: "12m"},
		{ID: "01JX2Q3W5V3B6N9P0R1S2T3U4V", Actor: "claude-dev", Scope: "src/auth/token.ts#validateToken", Intent: "tighten JWT expiry validation", TS: base.Add(-18 * time.Minute), Age: "18m"},
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
			MutationMessage: "Demo mode is read-only; starting and ending sessions is disabled.",
			StaleAfter:      staleAfter.String(),
		},
		Actions: []uiAction{
			{ID: "start_comms_session", Label: "Start Comms Session", Method: http.MethodPost, Path: "/api/comms-session/start", Enabled: false, Reason: "demo mode is read-only"},
			{ID: "end_comms_session", Label: "End Comms Session", Method: http.MethodPost, Path: "/api/comms-session/end", Enabled: false, Reason: "demo mode is read-only"},
			{ID: "retire_session_actor", Label: "Retire Session Actor", Method: http.MethodPost, Path: "/api/session/retire", Enabled: false, Reason: "demo mode is read-only"},
			{ID: "transfer_leader", Label: "Transfer Leader", Method: http.MethodPost, Path: "/api/session/lead", Enabled: false, Reason: "demo mode is read-only"},
			{ID: "select_session_log", Label: "Select Session Event Log", Enabled: true, Reason: "client-side filtered view over current_session/events and comms_sessions/events"},
		},
		Current: &uiCommsSession{
			ID: "current", StartedAt: base.Add(-13 * time.Minute), Actors: []string{"claude-dev", "codex-dev", "human-eli"},
			Reason: "demo preview", EventCount: len(currentEvents), ClaimCount: 3, FindingCount: 3, NoteCount: 2, Events: currentEvents,
		},
		Sessions: []uiSession{
			{Actor: "codex-dev", Label: "Codex Dev", BaseName: "codex", Hostname: "MacBook-Pro.local", TS: base.Add(-13 * time.Minute), Leader: true},
			{Actor: "claude-dev", Label: "Claude Dev", BaseName: "claude", Hostname: "MacBook-Pro.local", TS: base.Add(-12 * time.Minute)},
			{Actor: "human-eli", Label: "Eli", BaseName: "human", Hostname: "MacBook-Pro.local", TS: base.Add(-2 * time.Hour)},
		},
		CommsSessions: []uiCommsSession{
			{
				ID: "01JX2Q3M6M6B6N9P0R1S2T3U4V", StartedAt: base.Add(-8 * time.Hour), EndedAt: base.Add(-30 * time.Minute),
				EndedBy: "human-eli", Reason: "morning verification pass finished",
				Actors: []string{"claude-morning", "codex-morning", "human-eli"}, ReleasedRefs: 2,
				EventCount: len(archivedEvents), ClaimCount: 1, FindingCount: 1, NoteCount: 0, Events: archivedEvents,
			},
		},
		Claims: claims,
		Findings: []uiFinding{
			{ID: "01JX2Q3P9P9B6N9P0R1S2T3U4V", Actor: "codex-dev", Category: "decision", Summary: "everyone should check live Meta numbers before shipping", Priority: true, TS: base.Add(-2 * time.Minute)},
			{ID: "01JX2Q3T2U9B6N9P0R1S2T3U4V", Actor: "codex-dev", Category: "fix", Summary: "leads sourced only from tracker overlay", TS: base.Add(-4 * time.Minute)},
			{ID: "01JX2Q3S1T8B6N9P0R1S2T3U4V", Actor: "claude-dev", Category: "decision", Summary: "tracker is source of truth for leads", TS: base.Add(-19 * time.Minute)},
			{ID: "01JX2Q3R0S7B6N9P0R1S2T3U4V", Actor: "codex-9b2c", Category: "gotcha", Summary: "whole-file prisma claims require an anchor", TS: base.Add(-47 * time.Minute)},
		},
		Notes: []uiNote{
			{ID: "01JX2Q3P8P8B6N9P0R1S2T3U4V", Actor: "codex-dev", Body: "Everyone pause before touching aggregation until claim clears.", Priority: true, TS: base.Add(-1 * time.Minute)},
			{ID: "01JX2Q3Q0R6B6N9P0R1S2T3U4V", Actor: "claude-dev", Body: "FYI Prisma schema migration coming next session", TS: base.Add(-8 * time.Minute)},
			{ID: "01JX2Q3P0Q5B6N9P0R1S2T3U4V", Actor: "codex-9b2c", Body: "@claude-dev can I take src/auth/token.ts when you're done?", TS: base.Add(-14 * time.Minute)},
		},
		Docs:    []string{"lead-counting", "tracker-architecture", "ui"},
		Lessons: []string{"verify-data-before-ui", "claim-smallest-scope", "capture-filter-context"},
		Events:  currentEvents,
		Updated: base.Add(18 * time.Second),
	}
}

func buildUIActions(snap uiSnapshot) []uiAction {
	start := uiAction{ID: "start_comms_session", Label: "Start Comms Session", Method: http.MethodPost, Path: "/api/comms-session/start"}
	end := uiAction{ID: "end_comms_session", Label: "End Comms Session", Method: http.MethodPost, Path: "/api/comms-session/end"}
	retire := uiAction{ID: "retire_session_actor", Label: "Retire Session Actor", Method: http.MethodPost, Path: "/api/session/retire"}
	lead := uiAction{ID: "transfer_leader", Label: "Transfer Leader", Method: http.MethodPost, Path: "/api/session/lead"}
	logs := uiAction{ID: "select_session_log", Label: "Select Session Event Log", Enabled: true, Reason: "client-side filtered view over current_session/events and comms_sessions/events"}

	if snap.Project.Demo {
		start.Reason = "demo mode is read-only"
		end.Reason = "demo mode is read-only"
		retire.Reason = "demo mode is read-only"
		lead.Reason = "demo mode is read-only"
		return []uiAction{start, end, retire, lead, logs}
	}
	if !snap.Project.MutationsEnabled {
		reason := snap.Project.MutationMessage
		if reason == "" {
			reason = "mutating UI actions require COMMS_ACTOR"
		}
		start.Reason = reason
		end.Reason = reason
		retire.Reason = reason
		lead.Reason = reason
		return []uiAction{start, end, retire, lead, logs}
	}
	if snap.Current == nil && len(snap.Sessions) == 0 && len(snap.Claims) == 0 {
		start.Enabled = true
	} else {
		start.Reason = "a comms session is already active"
	}
	if snap.Current != nil {
		end.Enabled = true
	} else {
		end.Reason = "no active comms session to end"
	}
	if len(snap.Sessions) > 0 {
		retire.Enabled = true
		lead.Enabled = true
	} else {
		retire.Reason = "no active session actor to retire"
		lead.Reason = "no active session actor can become leader"
	}
	return []uiAction{start, end, retire, lead, logs}
}

func buildCommsSessionViews(events []event.Event) (*uiCommsSession, []uiCommsSession) {
	if len(events) == 0 {
		return nil, nil
	}
	sorted := append([]event.Event(nil), events...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].ID < sorted[j].ID })

	start := 0
	var archived []uiCommsSession
	for i, ev := range sorted {
		if ev.Type != event.TypeRelease || !dataBool(ev.Data, "comms_session_end") {
			continue
		}
		window := sorted[start : i+1]
		refs := dataStringList(ev.Data, "refs")
		archived = append(archived, summarizeCommsWindow(ev.ID, window, false, ev.Actor, reasonOf(ev), refs))
		start = i + 1
	}

	sort.Slice(archived, func(i, j int) bool { return archived[i].EndedAt.After(archived[j].EndedAt) })
	if start >= len(sorted) {
		return nil, archived
	}
	current := summarizeCommsWindow("current", sorted[start:], true, "", "", nil)
	return &current, archived
}

func summarizeCommsWindow(id string, events []event.Event, current bool, endedBy string, reason string, refs []string) uiCommsSession {
	view := uiCommsSession{
		ID:           id,
		EndedBy:      endedBy,
		Reason:       reason,
		ReleasedRefs: len(refs),
		Events:       eventsToUI(events),
	}
	if len(events) == 0 {
		return view
	}
	view.StartedAt = events[0].TS
	if !current {
		view.EndedAt = events[len(events)-1].TS
	}
	actors := map[string]bool{}
	for _, ev := range events {
		actors[ev.Actor] = true
		view.EventCount++
		switch ev.Type {
		case event.TypeClaim:
			view.ClaimCount++
		case event.TypeFinding:
			view.FindingCount++
		case event.TypeNote:
			view.NoteCount++
		}
	}
	view.Actors = sortedStringSet(actors)
	return view
}

func eventsToUI(events []event.Event) []uiEvent {
	out := make([]uiEvent, 0, len(events))
	for i := len(events) - 1; i >= 0; i-- {
		ev := events[i]
		out = append(out, uiEvent{
			ID: ev.ID, Actor: ev.Actor, Type: ev.Type, Scope: ev.Scope,
			Summary: eventSummary(ev), TS: ev.TS,
		})
	}
	return out
}

func sortedStringSet(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for value := range set {
		if value != "" {
			out = append(out, value)
		}
	}
	sort.Strings(out)
	return out
}

func reasonOf(ev event.Event) string {
	if s, _ := ev.Data["reason"].(string); s != "" {
		return s
	}
	if s, _ := ev.Data["result"].(string); s != "" {
		return s
	}
	return ""
}

func eventSummary(ev event.Event) string {
	switch ev.Type {
	case event.TypeHello:
		if dataBool(ev.Data, "comms_session_start") {
			if s := reasonOf(ev); s != "" {
				return "started comms session: " + s
			}
			return "started comms session"
		}
	case event.TypeClaim:
		if s, _ := ev.Data["intent"].(string); s != "" {
			return s
		}
	case event.TypeRelease:
		if dataBool(ev.Data, "comms_session_end") {
			count := len(dataStringList(ev.Data, "refs"))
			return fmt.Sprintf("ended comms session; released %d claim%s", count, pluralS(count))
		}
		if dataBool(ev.Data, "session_retire") {
			target, _ := ev.Data["retired_actor"].(string)
			count := len(dataStringList(ev.Data, "refs"))
			return fmt.Sprintf("retired @%s from active sessions; released %d claim%s", target, count, pluralS(count))
		}
		if dataBool(ev.Data, "leader_transfer") {
			target, _ := ev.Data["leader_actor"].(string)
			return fmt.Sprintf("@%s became comms leader", target)
		}
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

func dataStringList(m map[string]interface{}, key string) []string {
	if m == nil {
		return nil
	}
	v, ok := m[key]
	if !ok {
		return nil
	}
	if s, ok := v.(string); ok {
		return []string{s}
	}
	if arr, ok := v.([]string); ok {
		return append([]string(nil), arr...)
	}
	arr, ok := v.([]interface{})
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, x := range arr {
		if s, ok := x.(string); ok {
			out = append(out, s)
		}
	}
	return out
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
.panel-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
}
.panel-title h2 {
  padding: 0;
  border: 0;
}
.panel-title select {
  min-width: 250px;
  max-width: 100%;
  height: 32px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 12px;
}
.stack { display: grid; gap: 14px; }
.row {
  padding: 12px 14px;
  border-bottom: 1px solid var(--soft);
}
.row:last-child { border-bottom: 0; }
.actor { font-weight: 680; }
.meta-inline { color: var(--muted); font-size: 12px; font-weight: 520; }
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
.events {
  grid-column: 1 / -1;
}
.session-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: start;
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
  .panel-title {
    display: block;
  }
  .panel-title select {
    width: 100%;
    margin-top: 8px;
  }
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
    <button id="startComms" type="button">Start Comms Session</button>
    <button id="endComms" class="danger" type="button">End Comms Session</button>
    <button id="theme" type="button" aria-label="Toggle dark mode">Dark</button>
    <button id="refresh" type="button">Refresh</button>
  </div>
</header>
<div id="error" class="error-banner"></div>
<main>
  <section class="panel">
    <h2>Active Sessions</h2>
    <div id="sessions"></div>
    <h2>Current Comms Session</h2>
    <div id="currentSession"></div>
    <h2>Comms Session Archive</h2>
    <div id="commsSessions"></div>
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
    <section class="panel">
      <h2>Global Lessons</h2>
      <div id="lessons"></div>
    </section>
  </div>
  <section class="panel events">
    <div class="panel-title">
      <h2>Session Event Log</h2>
      <select id="sessionSelect" aria-label="Choose comms session log"></select>
    </div>
    <div class="hint" id="eventHint">Choose a session to see only that session's log rows. The physical JSONL remains append-only.</div>
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
  if (data.project.mutations_enabled) return 'Agents create/release claims; Start and End control the whole coordination window.';
  return data.project.mutation_message || 'Set COMMS_ACTOR before starting comms ui to start or end the comms session here.';
}
function actionByID(data, id) {
  return (data.actions || []).find(a => a.id === id) || {};
}
let selectedSessionID = localStorage.getItem('selectedSessionID') || 'current';
let latestData = null;
async function load() {
  const res = await fetch('/api/status', { cache: 'no-store' });
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  hideError();
  el('project').textContent = data.project.name + ' / comms';
  el('projectMeta').innerHTML = esc(data.project.hash) + ' · ' + esc(data.project.root) + (data.project.demo ? ' · <span class="demo-mark">demo mode</span>' : '');
  el('logPath').textContent = 'Log: ' + data.project.log_path;
  el('updated').textContent = 'updated ' + fmtTime(data.updated);
  latestData = data;
  const startAction = actionByID(data, 'start_comms_session');
  const endAction = actionByID(data, 'end_comms_session');
  el('startComms').disabled = !startAction.enabled;
  el('startComms').title = startAction.reason || mutationHelp(data);
  el('endComms').disabled = !endAction.enabled;
  el('endComms').title = endAction.reason || mutationHelp(data);
  el('sessions').innerHTML = renderRows(data.sessions, s => {
    const title = s.label ? esc(s.label) + ' <span class="meta-inline">@' + esc(s.actor) + '</span>' : '@' + esc(s.actor);
    return '<div class="row session-row"><div><div class="actor">' + title + (s.leader ? ' <span class="pill leader">leader</span>' : '') + '</div><div class="meta">' + esc(s.base_name || 'session') + ' · ' + esc(s.hostname || 'unknown host') + ' · hello ' + fmtTime(s.ts) + '</div></div></div>';
  },
    'No active sessions in the last 4h.');
  el('currentSession').innerHTML = data.current_session
    ? '<div class="row"><div class="actor">Started ' + fmtTime(data.current_session.started_at) + '</div><div class="meta">' + esc(data.current_session.event_count) + ' event(s) · ' + esc(data.current_session.claim_count) + ' claim(s) · ' + esc(data.current_session.finding_count) + ' finding(s) · ' + esc(data.current_session.note_count) + ' note(s)</div><div class="meta">' + esc((data.current_session.actors || []).map(a => '@' + a).join(', ')) + '</div></div>'
    : empty('No comms session is open. Use Start Comms Session, or let the first agent run comms hello.');
  el('commsSessions').innerHTML = renderRows(data.comms_sessions, s =>
    '<div class="row"><div class="actor">' + fmtTime(s.started_at) + ' → ' + fmtTime(s.ended_at) + '</div><div class="meta">ended by @' + esc(s.ended_by) + ' · ' + esc(s.reason || 'comms session ended') + '</div><div class="meta">' + esc(s.event_count) + ' event(s) · ' + esc(s.claim_count) + ' claim(s) · ' + esc(s.finding_count) + ' finding(s) · ' + esc(s.note_count) + ' note(s)</div><div class="meta">' + esc((s.actors || []).map(a => '@' + a).join(', ')) + '</div></div>',
    'No archived comms sessions yet. Use End Comms Session when the project work window is done.');
  el('claims').innerHTML = '<div class="hint">Claims older than ' + esc(data.project.stale_after) + ' are marked stale. ' + esc(mutationHelp(data)) + '</div>' +
    renderTable(data.claims, ['Actor', 'Scope', 'Intent', 'Age'], c =>
    '<tr class="' + (c.stale ? 'claim-stale' : '') + '"><td><span class="actor">@' + esc(c.actor) + '</span></td><td><div class="scope">' + esc(c.scope) + '</div><div class="copy">' + esc(c.id.slice(0, 10)) + '</div></td><td>' + esc(c.intent) + '</td><td>' + esc(c.age) + (c.stale ? '<div><span class="pill stale">stale</span></div>' : '') + '</td></tr>',
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
  el('lessons').innerHTML = renderRows(data.lessons || [], d =>
    '<div class="row"><span class="scope">' + esc(d) + '</span><div class="copy">comms lesson ' + esc(d) + '</div></div>',
    'No global lessons yet.');
  renderSessionChoices(data);
}
function allSessionChoices(data) {
  const out = [];
  if (data.current_session) out.push({ id: 'current', label: 'Current session', session: data.current_session });
  for (const s of data.comms_sessions || []) {
    out.push({ id: s.id, label: 'Archive: ' + fmtTime(s.started_at) + ' → ' + fmtTime(s.ended_at), session: s });
  }
  return out;
}
function renderSessionChoices(data) {
  const choices = allSessionChoices(data);
  if (!choices.length) {
    el('sessionSelect').innerHTML = '<option value="">No sessions yet</option>';
    el('sessionSelect').disabled = true;
    el('events').innerHTML = empty('No session logs yet. Start a comms session or run comms ui --demo.');
    return;
  }
  if (!choices.some(c => c.id === selectedSessionID)) selectedSessionID = choices[0].id;
  el('sessionSelect').disabled = false;
  el('sessionSelect').innerHTML = choices.map(c => '<option value="' + esc(c.id) + '">' + esc(c.label) + '</option>').join('');
  el('sessionSelect').value = selectedSessionID;
  renderSelectedSessionLog(data);
}
function renderSelectedSessionLog(data) {
  const chosen = allSessionChoices(data).find(c => c.id === selectedSessionID);
  if (!chosen) {
    el('events').innerHTML = empty('No selected session log.');
    return;
  }
  const s = chosen.session;
  const range = chosen.id === 'current' ? 'Current session started ' + fmtTime(s.started_at) : 'Archived session ' + fmtTime(s.started_at) + ' → ' + fmtTime(s.ended_at);
  el('eventHint').textContent = range + ' · ' + s.event_count + ' event(s). The physical JSONL remains append-only; this table is filtered to the selected session.';
  el('events').innerHTML = renderTable(s.events || [], ['When', 'Type', 'Actor', 'Scope', 'Summary'], ev =>
    '<tr><td>' + fmtTime(ev.ts) + '</td><td><span class="pill ' + esc(ev.type) + '">' + esc(ev.type) + '</span></td><td>@' + esc(ev.actor) + '</td><td><span class="scope">' + esc((ev.scope || []).join(', ')) + '</span></td><td>' + esc(ev.summary) + '</td></tr>',
    'No log events in this session.');
}
async function startCommsSession() {
  const reason = window.prompt('Start a new comms session? This creates the first log row for a new coordination window.', 'project work session started');
  if (reason === null) return;
  const res = await fetch('/api/comms-session/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason })
  });
  if (!res.ok) throw new Error(await res.text());
  selectedSessionID = 'current';
  localStorage.setItem('selectedSessionID', selectedSessionID);
  hideError();
  await load();
}
async function endCommsSession() {
  const reason = window.prompt('End the whole comms session? This releases all active claims, clears active sessions, and archives this communication window for later analysis.', 'project work session done');
  if (reason === null) return;
  const res = await fetch('/api/comms-session/end', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason })
  });
  if (!res.ok) throw new Error(await res.text());
  selectedSessionID = '';
  localStorage.removeItem('selectedSessionID');
  hideError();
  await load();
}
el('startComms').addEventListener('click', () => {
  el('startComms').disabled = true;
  startCommsSession().catch(err => {
    el('startComms').disabled = false;
    showError(err);
  });
});
el('endComms').addEventListener('click', () => {
  el('endComms').disabled = true;
  endCommsSession().catch(err => {
    el('endComms').disabled = false;
    showError(err);
  });
});
el('sessionSelect').addEventListener('change', () => {
  selectedSessionID = el('sessionSelect').value;
  localStorage.setItem('selectedSessionID', selectedSessionID);
  if (latestData) renderSelectedSessionLog(latestData);
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
