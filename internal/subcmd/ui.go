package subcmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"time"

	"github.com/dpa-plus/comms/internal/actor"
	"github.com/dpa-plus/comms/internal/event"
	"github.com/dpa-plus/comms/internal/paths"
	"github.com/dpa-plus/comms/internal/state"
	"github.com/spf13/cobra"
)

// NewUICmd serves a small local dashboard over HTTP.
func NewUICmd() *cobra.Command {
	addr := "127.0.0.1:7878"
	demo := false
	all := false
	forceOpen := false
	noOpen := false
	staleAfter := 90 * time.Minute
	cmd := &cobra.Command{
		Use:   "ui",
		Short: "Serve a local dashboard",
		Long: `Serve a local dashboard.

By default the dashboard is UNIFIED: it shows every comms project on this
machine in one place, with a sidebar to pick which project/session to view.
Run it once and you see all your agents across all repos — no need to start a
UI per project. Scope it to a single repo with --repo /path/to/repo.

The UI binds to 127.0.0.1 by default, reads the same JSONL event logs as the
CLI, and streams live updates to the browser over Server-Sent Events: a file
watcher detects each change to a log and pushes a fresh snapshot instantly, so
the browser never polls.

Claims older than --stale-after are highlighted as suspicious. The UI can
append start/end boundary events when COMMS_ACTOR is set; it never edits or
deletes existing log lines.

When run interactively, the dashboard opens in your browser automatically; use
--no-open to suppress that, or --open to force it (e.g. over SSH).

Use --demo to show deterministic sample data without writing fake events to
the real comms log.`,
		RunE: func(cmd *cobra.Command, args []string) error {
			// Unified (all-projects) is the default. Scope to a single repo only
			// when the user explicitly points at one via --repo or COMMS_REPO.
			unified := all || (globalRepoRoot == "" && os.Getenv("COMMS_REPO") == "")
			return runUI(addr, demo, unified, staleAfter, forceOpen, noOpen)
		},
	}
	cmd.Flags().StringVar(&addr, "addr", addr, "listen address")
	cmd.Flags().BoolVar(&demo, "demo", false, "serve deterministic sample data without touching the real log")
	cmd.Flags().BoolVar(&all, "all", false, "deprecated: the unified all-projects view is now the default; scope to one repo with --repo")
	cmd.Flags().BoolVar(&forceOpen, "open", false, "open the dashboard in your browser (default: auto when run interactively)")
	cmd.Flags().BoolVar(&noOpen, "no-open", false, "do not open a browser (useful for scripts, cron, and hooks)")
	cmd.Flags().DurationVar(&staleAfter, "stale-after", staleAfter, "highlight claims older than this duration")
	return cmd
}

func runUI(addr string, demo, all bool, staleAfter time.Duration, forceOpen, noOpen bool) error {
	if staleAfter < time.Minute {
		return fmt.Errorf("ui: --stale-after must be at least 1m")
	}
	server := uiServer{demo: demo, all: all, staleAfter: staleAfter, hub: newHub()}
	mux := http.NewServeMux()
	mux.HandleFunc("/", server.servePage)
	mux.HandleFunc("/favicon.svg", server.serveFavicon)
	mux.HandleFunc("/favicon.ico", server.serveFavicon)
	mux.HandleFunc("/api/status", server.serveStatus)
	mux.HandleFunc("/api/events", server.serveEvents)
	mux.HandleFunc("/api/comms-session/start", server.serveStartCommsSession)
	mux.HandleFunc("/api/comms-session/end", server.serveEndCommsSession)
	mux.HandleFunc("/api/session/retire", server.serveRetireSessionActor)
	mux.HandleFunc("/api/session/lead", server.serveTransferLeader)
	mux.HandleFunc("/api/claim/release", server.serveReleaseClaim)

	fmt.Printf("comms ui listening on http://%s\n", addr)
	fmt.Printf("Claims older than %s are marked stale. Press Ctrl-C to stop.\n", staleAfter)
	if demo {
		fmt.Println("Demo mode: serving sample data only; no fake events are written.")
	} else if all {
		fmt.Println("Unified mode: showing every comms project on this machine; pick a project/session in the sidebar.")
	} else {
		fmt.Println("Single-repo mode (--repo): scoped to one repo. Run without --repo to see all projects.")
	}

	// Prime the hub so the first browser to connect paints immediately, then
	// start the fsnotify watcher that pushes every later change. Demo data is
	// static, so it primes once and never needs a watcher.
	server.publishSnapshot()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if !demo {
		go server.watchLog(ctx)
	}

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	// Bind explicitly so we only open the browser once the port is actually
	// listening (no race between `open` and the server being ready).
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return fmt.Errorf("ui: listen on %s: %w", addr, err)
	}
	if wantBrowser(forceOpen, noOpen) {
		url := browseURL(addr)
		go func() {
			if err := launchBrowser(url); err != nil {
				fmt.Fprintf(os.Stderr, "comms ui: couldn't open a browser (%v); open %s yourself\n", err, url)
			}
		}()
	}
	return srv.Serve(ln)
}

// wantBrowser decides whether to auto-open the dashboard: --no-open always wins,
// --open always opens, otherwise open only when stdout is an interactive
// terminal (so PreToolUse hooks, scripts, cron, and systemd never spawn one).
func wantBrowser(forceOpen, noOpen bool) bool {
	if noOpen {
		return false
	}
	if forceOpen {
		return true
	}
	return stdoutIsTTY()
}

func stdoutIsTTY() bool {
	fi, err := os.Stdout.Stat()
	if err != nil {
		return false
	}
	return fi.Mode()&os.ModeCharDevice != 0
}

// browseURL turns a listen address into a URL a browser can reach, rewriting a
// wildcard/empty host to loopback.
func browseURL(addr string) string {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return "http://" + addr
	}
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	return "http://" + net.JoinHostPort(host, port)
}

// launchBrowser opens url in the user's default browser, best effort.
func launchBrowser(url string) error {
	switch runtime.GOOS {
	case "darwin":
		return exec.Command("open", url).Start()
	case "windows":
		return exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
	default:
		return exec.Command("xdg-open", url).Start()
	}
}

type uiServer struct {
	demo       bool
	all        bool
	staleAfter time.Duration
	hub        *hub // fans pushed snapshots out to connected SSE clients
}

// guardMutation enforces the security preconditions shared by every
// state-changing endpoint and caps the request body. It returns true when the
// request was rejected (and a response was already written), so callers do:
//
//	if guardMutation(w, r) { return }
//
// Protections:
//   - POST only (CSRF can't be a top-level navigation/GET).
//   - Same-origin — if the browser sends an Origin, it must match the Host AND
//     that host must be loopback. This blocks both classic cross-origin CSRF
//     (Origin != Host) and DNS-rebinding (Origin == Host but resolves to a
//     non-loopback attacker domain). Non-browser clients (no Origin header,
//     e.g. curl or the hook) are allowed; they are not a CSRF vector.
//   - Request body capped at 64 KiB.
func guardMutation(w http.ResponseWriter, r *http.Request) bool {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return true
	}
	if !sameOriginRequest(r) {
		http.Error(w, "forbidden: cross-origin request rejected", http.StatusForbidden)
		return true
	}
	r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
	return false
}

func hostIsLoopback(host string) bool {
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	if host == "" || strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func sameOriginRequest(r *http.Request) bool {
	origin := r.Header.Get("Origin")
	if origin == "" {
		return true // non-browser client (curl/hook) or same-origin nav without Origin
	}
	u, err := url.Parse(origin)
	if err != nil || u.Host == "" {
		return false
	}
	// Origin must match the Host we were reached on, and that host must be
	// loopback — otherwise a DNS-rebinding page (Origin == Host == attacker
	// domain pointed at 127.0.0.1) would slip through.
	return strings.EqualFold(u.Host, r.Host) && hostIsLoopback(u.Host)
}

// uiLockTimeout bounds how long a UI mutating handler waits for the per-repo
// flock. The CLI may legitimately hold it for a moment; if a process holds it
// longer than this, the handler fails fast with 503 rather than parking the
// request goroutine (and its .lock FD) indefinitely.
const uiLockTimeout = 5 * time.Second

// openMutatingUI opens a mutating runtime with a bounded lock timeout. On
// failure it writes the HTTP response (503 when another process holds the lock
// past the timeout, 400 otherwise) and returns ok=false, so handlers do:
//
//	rt, ok := s.openMutatingUI(w, OpenOpts{Mutating: true})
//	if !ok { return }
//	defer rt.Close()
func (s uiServer) openMutatingUI(w http.ResponseWriter, opts OpenOpts) (*Runtime, bool) {
	opts.Mutating = true
	if opts.LockTimeout == 0 {
		opts.LockTimeout = uiLockTimeout
	}
	rt, err := Open(opts)
	if err != nil {
		if errors.Is(err, ErrLockTimeout) {
			http.Error(w, "busy: another comms process holds the lock, try again", http.StatusServiceUnavailable)
			return nil, false
		}
		http.Error(w, err.Error(), http.StatusBadRequest)
		return nil, false
	}
	return rt, true
}

func (s uiServer) servePage(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(uiHTML))
}

func (s uiServer) serveFavicon(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "image/svg+xml")
	w.Header().Set("Cache-Control", "public, max-age=3600")
	_, _ = w.Write([]byte(faviconSVG))
}

func (s uiServer) serveStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	// Apply the same same-origin / DNS-rebinding guard the mutations use so a
	// cross-origin page cannot read the repo's coordination state. Non-browser
	// clients send no Origin and stay allowed.
	if r.Header.Get("Origin") != "" && !sameOriginRequest(r) {
		http.Error(w, "forbidden: cross-origin request rejected", http.StatusForbidden)
		return
	}
	// Shared with the SSE push path so the polled and pushed snapshots are byte
	// identical. Compact JSON (no indentation) keeps the payload small and is
	// required for SSE framing; the UI decodes JSON, so it's transparent.
	body, err := s.snapshotJSON()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_, _ = w.Write(body)
}

func (s uiServer) serveRetireSessionActor(w http.ResponseWriter, r *http.Request) {
	if guardMutation(w, r) {
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no actor retire event is written", http.StatusConflict)
		return
	}
	var req struct {
		Actor    string `json:"actor"`
		Reason   string `json:"reason"`
		RepoHash string `json:"repo_hash"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	// Unified mode: route the retire to the repo that owns the actor's session.
	if s.all {
		s.serveGlobalRetireSessionActor(w, strings.TrimSpace(req.RepoHash), req.Actor, req.Reason)
		return
	}
	rt, ok := s.openMutatingUI(w, OpenOpts{})
	if !ok {
		return
	}
	defer rt.Close()
	if _, err := appendSessionRetire(rt, req.Actor, req.Reason); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	s.writeSnapshot(w, rt)
}

// serveGlobalRetireSessionActor routes a "remove team member" (retire) from the
// unified dashboard to the repo that owns the actor's session, mirroring
// serveGlobalReleaseClaim / serveGlobalEndCommsSession.
func (s uiServer) serveGlobalRetireSessionActor(w http.ResponseWriter, repoHash, actor, reason string) {
	repoRoot, err := repoRootForGlobalHash(repoHash)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	rt, ok := s.openMutatingUI(w, OpenOpts{RepoRootOverride: repoRoot})
	if !ok {
		return
	}
	defer rt.Close()
	if rt.Repo.Hash != repoHash {
		http.Error(w, "repo hash "+repoHash+" no longer matches "+rt.Repo.Hash+" for "+repoRoot, http.StatusConflict)
		return
	}
	if _, err := appendSessionRetire(rt, actor, reason); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	snap, err := buildGlobalUISnapshot(s.staleAfter)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

func (s uiServer) serveReleaseClaim(w http.ResponseWriter, r *http.Request) {
	if guardMutation(w, r) {
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no claim release event is written", http.StatusConflict)
		return
	}
	var req struct {
		ID        string `json:"id"`
		ClaimID   string `json:"claim_id"`
		RepoHash  string `json:"repo_hash"`
		SessionID string `json:"session_id"`
		Reason    string `json:"reason"`
		Result    string `json:"result"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	id := strings.TrimSpace(req.ClaimID)
	if id == "" {
		id = strings.TrimSpace(req.ID)
	}
	if id == "" {
		http.Error(w, "claim id is required", http.StatusBadRequest)
		return
	}
	if s.all {
		s.serveGlobalReleaseClaim(w, id, strings.TrimSpace(req.RepoHash), strings.TrimSpace(req.SessionID), req.Reason, req.Result)
		return
	}
	rt, ok := s.openMutatingUI(w, OpenOpts{})
	if !ok {
		return
	}
	defer rt.Close()
	claim := rt.State.ClaimByID(id)
	if claim == nil {
		http.Error(w, "no active claim matches "+id, http.StatusConflict)
		return
	}
	reason := strings.TrimSpace(req.Reason)
	result := strings.TrimSpace(req.Result)
	if claim.Actor != rt.Actor && reason == "" {
		reason = "released from UI by @" + rt.Actor
	}
	if claim.Actor == rt.Actor && result == "" {
		result = "released from UI"
	}
	if err := appendReleaseEvent(rt, []*state.Claim{claim}, reason, result); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	s.writeSnapshot(w, rt)
}

func (s uiServer) serveGlobalReleaseClaim(w http.ResponseWriter, claimID, repoHash, sessionID, reasonText, resultText string) {
	if repoHash == "" {
		repoHash = repoHashFromPrefixedSessionID(sessionID)
	}
	repoRoot, err := repoRootForGlobalHash(repoHash)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	rt, ok := s.openMutatingUI(w, OpenOpts{RepoRootOverride: repoRoot})
	if !ok {
		return
	}
	defer rt.Close()
	if rt.Repo.Hash != repoHash {
		http.Error(w, "repo hash "+repoHash+" no longer matches "+rt.Repo.Hash+" for "+repoRoot, http.StatusConflict)
		return
	}
	claim := rt.State.ClaimByID(claimID)
	if claim == nil {
		http.Error(w, "no active claim matches "+claimID, http.StatusConflict)
		return
	}
	reason := strings.TrimSpace(reasonText)
	result := strings.TrimSpace(resultText)
	if claim.Actor != rt.Actor && reason == "" {
		reason = "released from global UI by @" + rt.Actor
	}
	if claim.Actor == rt.Actor && result == "" {
		result = "released from global UI"
	}
	if err := appendReleaseEvent(rt, []*state.Claim{claim}, reason, result); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	snap, err := buildGlobalUISnapshot(s.staleAfter)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

func (s uiServer) serveTransferLeader(w http.ResponseWriter, r *http.Request) {
	if guardMutation(w, r) {
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no leader transfer event is written", http.StatusConflict)
		return
	}
	if s.all {
		http.Error(w, "all-project mode is read-only; use a repo-specific UI or CLI for mutations", http.StatusConflict)
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
	rt, ok := s.openMutatingUI(w, OpenOpts{})
	if !ok {
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
	if guardMutation(w, r) {
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no comms session start event is written", http.StatusConflict)
		return
	}
	if s.all {
		http.Error(w, "all-project mode is read-only; use a repo-specific UI or CLI for mutations", http.StatusConflict)
		return
	}
	var req struct {
		Reason string `json:"reason"`
		Name   string `json:"name"`
		Label  string `json:"label"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	rt, ok := s.openMutatingUI(w, OpenOpts{})
	if !ok {
		return
	}
	defer rt.Close()
	name := strings.TrimSpace(req.Name)
	if name == "" {
		name = strings.TrimSpace(req.Reason)
	}
	if name == "" {
		http.Error(w, "comms session name is required", http.StatusBadRequest)
		return
	}
	if id, _ := activeCommsSessionByName(rt.State, name, time.Now().Add(-4*time.Hour)); id != "" {
		http.Error(w, "a comms session named "+name+" is already active", http.StatusConflict)
		return
	}
	helloAt := time.Now().UTC().Add(time.Millisecond)
	id := event.NewID(helloAt)
	if err := releaseActorClaimsBeforeSessionSwitch(rt, id, name, helloAt.Add(-time.Millisecond)); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	if err := appendSessionHello(rt, helloAt, id, name, req.Label, true); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeSnapshot(w, rt)
}

func (s uiServer) serveEndCommsSession(w http.ResponseWriter, r *http.Request) {
	if guardMutation(w, r) {
		return
	}
	if s.demo {
		http.Error(w, "demo mode is read-only; no comms session end event is written", http.StatusConflict)
		return
	}
	var req struct {
		Reason    string `json:"reason"`
		Name      string `json:"name"`
		SessionID string `json:"session_id"`
		RepoHash  string `json:"repo_hash"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	// Unified (all-projects) mode: route the end to the repo that owns the
	// selected project's session, exactly like serveGlobalReleaseClaim does.
	if s.all {
		s.serveGlobalEndCommsSession(w, strings.TrimSpace(req.RepoHash), req.SessionID, req.Name, req.Reason)
		return
	}
	rt, ok := s.openMutatingUI(w, OpenOpts{})
	if !ok {
		return
	}
	defer rt.Close()
	if code, err := endCommsSessionOnRuntime(rt, req.Name, req.SessionID, req.Reason); err != nil {
		http.Error(w, err.Error(), code)
		return
	}
	s.writeSnapshot(w, rt)
}

// serveGlobalEndCommsSession routes an End-Comms-Session mutation from the
// unified dashboard to the repo that owns the session. repoHash identifies the
// project the operator selected in the sidebar; it mirrors serveGlobalReleaseClaim.
func (s uiServer) serveGlobalEndCommsSession(w http.ResponseWriter, repoHash, sessionID, name, reason string) {
	if repoHash == "" {
		repoHash = repoHashFromPrefixedSessionID(sessionID)
	}
	repoRoot, err := repoRootForGlobalHash(repoHash)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	rt, ok := s.openMutatingUI(w, OpenOpts{RepoRootOverride: repoRoot})
	if !ok {
		return
	}
	defer rt.Close()
	if rt.Repo.Hash != repoHash {
		http.Error(w, "repo hash "+repoHash+" no longer matches "+rt.Repo.Hash+" for "+repoRoot, http.StatusConflict)
		return
	}
	if code, err := endCommsSessionOnRuntime(rt, name, sessionID, reason); err != nil {
		http.Error(w, err.Error(), code)
		return
	}
	snap, err := buildGlobalUISnapshot(s.staleAfter)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, snap)
}

// endCommsSessionOnRuntime appends the comms_session_end event for the named or
// current session on rt (which archives it and releases its claims). It returns
// (0, nil) on success, or (httpStatus, err) explaining why it could not. Caller
// holds the flock via openMutatingUI.
func endCommsSessionOnRuntime(rt *Runtime, reqName, reqSessionID, reqReason string) (int, error) {
	reason := strings.TrimSpace(reqReason)
	if reason == "" {
		reason = "comms session ended from ui"
	}
	sessionID := strings.TrimSpace(reqSessionID)
	sessionName := strings.TrimSpace(reqName)
	// "current" is the sentinel for the legacy/global window (claims and
	// sessions with no comms_session_id). Blanking it must drive the no-session
	// sweep below, NOT the operator fallback — otherwise ending the legacy
	// current window would wrongly end this operator's own NAMED session and
	// release that session's claims.
	explicitCurrent := sessionID == "current"
	if explicitCurrent {
		sessionID = ""
		sessionName = ""
	}
	if sessionID == "" && sessionName != "" {
		sessionID, sessionName = activeCommsSessionByName(rt.State, sessionName, time.Now().Add(-4*time.Hour))
		if sessionID == "" {
			return http.StatusConflict, fmt.Errorf("no active comms session named %s", strings.TrimSpace(reqName))
		}
	}
	if sessionID == "" && !explicitCurrent {
		if sess := rt.State.Sessions[rt.Actor]; sess != nil {
			sessionID = sess.SessionID
			sessionName = sess.SessionName
		}
	}
	sessionCutoff := time.Now().Add(-4 * time.Hour)
	var refs []interface{}
	var endedActors []interface{}
	if sessionID == "" {
		if len(rt.State.Sessions) == 0 && len(rt.State.Claims) == 0 {
			return http.StatusConflict, fmt.Errorf("no active comms session to end")
		}
		refs = make([]interface{}, 0, len(rt.State.Claims))
		for _, claim := range sortedClaims(rt.State) {
			refs = append(refs, claim.ID)
		}
		endedActors = make([]interface{}, 0, len(rt.State.Sessions))
		for _, session := range collectActiveSessions(rt.State, sessionCutoff) {
			endedActors = append(endedActors, session.Actor)
		}
	} else {
		activeSessionName := ""
		activeActors := make([]interface{}, 0, len(rt.State.Sessions))
		for _, session := range collectActiveSessions(rt.State, sessionCutoff) {
			if session.SessionID == sessionID {
				activeActors = append(activeActors, session.Actor)
				if activeSessionName == "" {
					activeSessionName = session.SessionName
				}
			}
		}
		if sessionName == "" {
			sessionName = activeSessionName
		}
		claims := activeClaimsByCommsSession(rt.State, sessionID)
		if sessionName == "" {
			for _, claim := range claims {
				if claim.SessionName != "" {
					sessionName = claim.SessionName
					break
				}
			}
		}
		if len(activeActors) == 0 && len(claims) == 0 {
			return http.StatusConflict, fmt.Errorf("no active comms session matches %s", sessionID)
		}
		refs = make([]interface{}, 0, len(claims))
		for _, claim := range claims {
			refs = append(refs, claim.ID)
		}
		endedActors = activeActors
	}
	now := time.Now().UTC()
	ev := event.Event{
		TS:    now,
		ID:    event.NewID(now),
		Actor: rt.Actor,
		Type:  event.TypeRelease,
		Data: map[string]interface{}{
			"refs":               refs,
			"comms_session_end":  true,
			"comms_session_id":   sessionID,
			"comms_session_name": sessionName,
			"ended_actors":       endedActors,
			"reason":             reason,
		},
	}
	if err := rt.Append(ev); err != nil {
		return http.StatusInternalServerError, err
	}
	return 0, nil
}

type uiSnapshot struct {
	Project       uiProject        `json:"project"`
	Current       *uiCommsSession  `json:"current_session,omitempty"`
	Active        []uiCommsSession `json:"active_comms_sessions"`
	Actions       []uiAction       `json:"actions"`
	Sessions      []uiSession      `json:"sessions"`
	CommsSessions []uiCommsSession `json:"comms_sessions"`
	Claims        []uiClaim        `json:"claims"`
	Findings      []uiFinding      `json:"findings"`
	Notes         []uiNote         `json:"notes"`
	Releases      []uiRelease      `json:"releases"`
	Docs          []string         `json:"docs"`
	Lessons       []string         `json:"lessons"`
	Events        []uiEvent        `json:"events"`
	// ProjectSessions is populated only in unified (all-projects) mode. Each
	// entry is one project's own data with UN-prefixed ids/names, so the
	// dashboard's sidebar can scope the whole view to a single project. The
	// flat arrays above stay populated (project-prefixed) for the "All
	// projects" merged view and backward compatibility.
	ProjectSessions []uiProjectSession `json:"project_sessions,omitempty"`
	Updated         time.Time          `json:"updated"`
}

// uiProjectSession is one project's self-contained slice of the unified
// snapshot. Field names mirror uiSnapshot so the frontend can render a scoped
// project view through the same code paths as the merged view.
type uiProjectSession struct {
	RepoHash      string           `json:"repo_hash"`
	RepoName      string           `json:"repo_name"`
	Root          string           `json:"root"`
	LogPath       string           `json:"log_path"`
	Current       *uiCommsSession  `json:"current_session,omitempty"`
	Active        []uiCommsSession `json:"active_comms_sessions"`
	CommsSessions []uiCommsSession `json:"comms_sessions"`
	Sessions      []uiSession      `json:"sessions"`
	Claims        []uiClaim        `json:"claims"`
	Findings      []uiFinding      `json:"findings"`
	Notes         []uiNote         `json:"notes"`
	Releases      []uiRelease      `json:"releases"`
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
	Actor       string    `json:"actor"`
	Label       string    `json:"label,omitempty"`
	BaseName    string    `json:"base_name"`
	Hostname    string    `json:"hostname"`
	TS          time.Time `json:"ts"`
	Leader      bool      `json:"leader"`
	SessionID   string    `json:"session_id,omitempty"`
	SessionName string    `json:"session_name,omitempty"`
}

type uiCommsSession struct {
	ID           string    `json:"id"`
	Name         string    `json:"name,omitempty"`
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
	Claims       []uiClaim `json:"claims,omitempty"`
}

type uiClaim struct {
	ID          string    `json:"id"`
	Actor       string    `json:"actor"`
	Scope       string    `json:"scope"`
	Intent      string    `json:"intent"`
	TS          time.Time `json:"ts"`
	Age         string    `json:"age"`
	Stale       bool      `json:"stale"`
	StoleID     string    `json:"stole_id,omitempty"`
	RepoHash    string    `json:"repo_hash,omitempty"`
	SessionID   string    `json:"session_id,omitempty"`
	SessionName string    `json:"session_name,omitempty"`
}

type uiFinding struct {
	ID          string    `json:"id"`
	Actor       string    `json:"actor"`
	Category    string    `json:"category"`
	Summary     string    `json:"summary"`
	Priority    bool      `json:"priority"`
	TS          time.Time `json:"ts"`
	SessionID   string    `json:"session_id,omitempty"`
	SessionName string    `json:"session_name,omitempty"`
}

type uiNote struct {
	ID          string    `json:"id"`
	Actor       string    `json:"actor"`
	Body        string    `json:"body"`
	Priority    bool      `json:"priority"`
	TS          time.Time `json:"ts"`
	SessionID   string    `json:"session_id,omitempty"`
	SessionName string    `json:"session_name,omitempty"`
}

type uiRelease struct {
	ID          string    `json:"id"`
	Actor       string    `json:"actor"`
	Result      string    `json:"result"`
	Scopes      []string  `json:"scopes,omitempty"`
	TS          time.Time `json:"ts"`
	SessionID   string    `json:"session_id,omitempty"`
	SessionName string    `json:"session_name,omitempty"`
}

// toUIReleases converts state releases (recent first) to the UI shape.
func toUIReleases(rs []*state.Release) []uiRelease {
	out := make([]uiRelease, 0, len(rs))
	for _, r := range rs {
		out = append(out, uiRelease{
			ID: r.ID, Actor: r.Actor, Result: r.Result, Scopes: r.Scopes,
			TS: r.TS, SessionID: r.SessionID, SessionName: r.SessionName,
		})
	}
	return out
}

type uiEvent struct {
	ID      string     `json:"id"`
	Actor   string     `json:"actor"`
	Type    event.Type `json:"type"`
	Scope   []string   `json:"scope,omitempty"`
	Summary string     `json:"summary"`
	TS      time.Time  `json:"ts"`
}

type globalLogCandidate struct {
	hash     string
	repoRoot string
	repoName string
	events   []event.Event
	lastTS   time.Time
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
		Active:        []uiCommsSession{},
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
	if out.Lessons == nil {
		out.Lessons = []string{}
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
			SessionID: s.SessionID, SessionName: s.SessionName,
		})
	}
	out.Active, out.CommsSessions = buildCommsSessionViews(rt.Events)
	out.Active = filterActiveCommsSessionViews(out.Active, rt.State, now.Add(-4*time.Hour))
	if len(out.Active) > 0 {
		out.Current = &out.Active[0]
	}
	if out.Active == nil {
		out.Active = []uiCommsSession{}
	}
	if out.CommsSessions == nil {
		out.CommsSessions = []uiCommsSession{}
	}
	for _, c := range sortedClaims(rt.State) {
		out.Claims = append(out.Claims, uiClaim{
			ID: c.ID, Actor: c.Actor, Scope: c.Scope.String(), Intent: c.Intent,
			TS: c.TS, Age: shortAge(now.Sub(c.TS)), Stale: now.Sub(c.TS) >= staleAfter, StoleID: c.StolenFromID,
			RepoHash: rt.Repo.Hash, SessionID: c.SessionID, SessionName: c.SessionName,
		})
	}
	for _, f := range recentFindings(rt.State, now.Add(-24*time.Hour), 12) {
		out.Findings = append(out.Findings, uiFinding{
			ID: f.ID, Actor: f.Actor, Category: f.Category, Summary: f.Summary, Priority: f.Priority, TS: f.TS,
			SessionID: f.SessionID, SessionName: f.SessionName,
		})
	}
	for _, n := range recentNotes(rt.State, now.Add(-24*time.Hour), 8) {
		out.Notes = append(out.Notes, uiNote{ID: n.ID, Actor: n.Actor, Body: n.Body, Priority: n.Priority, TS: n.TS, SessionID: n.SessionID, SessionName: n.SessionName})
	}
	out.Releases = toUIReleases(recentReleases(rt.State, now.Add(-24*time.Hour), 12))
	attachClaimsToActiveSessions(&out)
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
	current := uiCommsSession{
		ID: "01JX2Q3Z5V6B6N9P0R1S2T3U4V", Name: "demo preview", StartedAt: base.Add(-13 * time.Minute), Actors: []string{"claude-dev", "codex-dev", "human-eli"},
		Reason: "demo preview", EventCount: len(currentEvents), ClaimCount: 3, FindingCount: 3, NoteCount: 2, Events: currentEvents, Claims: claims,
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
			{ID: "release_claim", Label: "Release Claim", Method: http.MethodPost, Path: "/api/claim/release", Enabled: false, Reason: "demo mode is read-only"},
			{ID: "retire_session_actor", Label: "Retire Session Actor", Method: http.MethodPost, Path: "/api/session/retire", Enabled: false, Reason: "demo mode is read-only"},
			{ID: "transfer_leader", Label: "Transfer Leader", Method: http.MethodPost, Path: "/api/session/lead", Enabled: false, Reason: "demo mode is read-only"},
			{ID: "select_session_log", Label: "Select Session Event Log", Enabled: true, Reason: "client-side filtered view over current_session/events and comms_sessions/events"},
		},
		Current: &current,
		Active:  []uiCommsSession{current},
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
		Releases: []uiRelease{
			{ID: "01JX2Q3M6M6B6N9P0R1S2T3U4V", Actor: "claude-dev", Result: "tests green, merged PR #214", Scopes: []string{"frontend/src/Dashboard.tsx"}, TS: base.Add(-6 * time.Minute)},
			{ID: "01JX2Q3L5L5B6N9P0R1S2T3U4V", Actor: "codex-dev", Result: "fixed lead double-counting; added regression test", Scopes: []string{"src/aggregate/lead_counter.ts#L40-90"}, TS: base.Add(-22 * time.Minute)},
		},
		Docs:    []string{"lead-counting", "tracker-architecture", "ui"},
		Lessons: []string{"verify-data-before-ui", "claim-smallest-scope", "capture-filter-context"},
		Events:  currentEvents,
		Updated: base.Add(18 * time.Second),
	}
}

func buildGlobalUISnapshot(staleAfter time.Duration) (uiSnapshot, error) {
	now := time.Now()
	dataHome, err := paths.UserDataHome()
	if err != nil {
		return uiSnapshot{}, err
	}
	root := filepath.Join(dataHome, "comms")
	out := uiSnapshot{
		Project: uiProject{
			Name:             "All comms projects",
			Root:             root,
			Hash:             "global",
			LogPath:          root,
			MutationMessage:  "All-project mode can release claims; use a repo-specific UI or CLI to start/end sessions.",
			StaleAfter:       staleAfter.String(),
			MutationsEnabled: false,
		},
		Active:        []uiCommsSession{},
		Actions:       []uiAction{},
		Sessions:      []uiSession{},
		CommsSessions: []uiCommsSession{},
		Claims:        []uiClaim{},
		Findings:      []uiFinding{},
		Notes:         []uiNote{},
		Releases:      []uiRelease{},
		Docs:          []string{},
		Lessons:       listGlobalLessons(),
		Events:        []uiEvent{},
		Updated:       now.UTC(),
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		if os.IsNotExist(err) {
			out.Actions = buildUIActions(out)
			return out, nil
		}
		return uiSnapshot{}, err
	}
	if a, err := actor.Resolve(actor.Mutating); err == nil {
		out.Project.Actor = a
	} else {
		out.Project.MutationMessage = err.Error()
	}
	candidates := map[string]globalLogCandidate{}
	var candidateKeys []string
	for _, entry := range entries {
		if !entry.IsDir() || entry.Name() == "global" {
			continue
		}
		hash := entry.Name()
		logDir := filepath.Join(root, hash)
		repoRoot := strings.TrimSpace(readSmallFile(filepath.Join(logDir, "repo-path.txt")))
		if repoRoot != "" {
			if _, err := os.Stat(repoRoot); err != nil {
				if os.IsNotExist(err) {
					continue
				}
			}
			if isScratchRepoRoot(repoRoot) {
				continue
			}
		}
		repoName := hash
		if repoRoot != "" {
			repoName = filepath.Base(repoRoot)
		}
		events, err := event.Read(filepath.Join(logDir, "log.jsonl"))
		if err != nil {
			continue
		}
		key := globalProjectKey(repoRoot, hash)
		candidate := globalLogCandidate{
			hash:     hash,
			repoRoot: repoRoot,
			repoName: repoName,
			events:   events,
			lastTS:   latestEventTS(events),
		}
		if existing, ok := candidates[key]; !ok {
			candidates[key] = candidate
			candidateKeys = append(candidateKeys, key)
		} else if globalLogCandidateNewer(candidate, existing) {
			candidates[key] = candidate
		}
	}
	cutoff := now.Add(-4 * time.Hour)
	findingCutoff := now.Add(-24 * time.Hour)
	for _, key := range candidateKeys {
		candidate := candidates[key]
		hash := candidate.hash
		repoRoot := candidate.repoRoot
		repoName := candidate.repoName
		events := candidate.events
		st := state.Fold(events)
		active, archived := buildCommsSessionViews(events)
		active = filterActiveCommsSessionViews(active, st, cutoff)
		sessions := collectActiveSessions(st, cutoff)
		markLeaderSessions(sessions)

		// Per-project container: this project's own data with UN-prefixed
		// ids/names, so the unified UI can scope the whole dashboard to just
		// this project. The merged (prefixed) flat arrays are derived from it
		// below so the "All projects" view stays exactly as before.
		ps := uiProjectSession{
			RepoHash:      hash,
			RepoName:      repoName,
			Root:          repoRoot,
			LogPath:       filepath.Join(root, hash, "log.jsonl"),
			Active:        append([]uiCommsSession(nil), active...),
			CommsSessions: append([]uiCommsSession(nil), archived...),
		}
		for _, s := range sessions {
			ps.Sessions = append(ps.Sessions, uiSession{
				Actor: s.Actor, Label: s.Label, BaseName: s.BaseName, Hostname: s.Hostname,
				TS: s.TS, Leader: s.Leader, SessionID: s.SessionID, SessionName: s.SessionName,
			})
		}
		for _, c := range sortedClaims(st) {
			ps.Claims = append(ps.Claims, uiClaim{
				ID: c.ID, Actor: c.Actor, Scope: c.Scope.String(), Intent: c.Intent, TS: c.TS,
				Age: shortAge(now.Sub(c.TS)), Stale: now.Sub(c.TS) >= staleAfter, StoleID: c.StolenFromID,
				RepoHash: hash, SessionID: c.SessionID, SessionName: c.SessionName,
			})
		}
		for _, f := range recentFindings(st, findingCutoff, 12) {
			ps.Findings = append(ps.Findings, uiFinding{
				ID: f.ID, Actor: f.Actor, Category: f.Category, Summary: f.Summary,
				Priority: f.Priority, TS: f.TS, SessionID: f.SessionID, SessionName: f.SessionName,
			})
		}
		for _, n := range recentNotes(st, findingCutoff, 8) {
			ps.Notes = append(ps.Notes, uiNote{
				ID: n.ID, Actor: n.Actor, Body: n.Body, Priority: n.Priority, TS: n.TS,
				SessionID: n.SessionID, SessionName: n.SessionName,
			})
		}
		ps.Releases = toUIReleases(recentReleases(st, findingCutoff, 12))
		attachClaimsToProjectSession(&ps)
		out.ProjectSessions = append(out.ProjectSessions, ps)

		// Merged (project-prefixed) flat arrays, derived from the container in
		// the same order/shape the previous code produced.
		for _, s := range ps.Active {
			out.Active = append(out.Active, prefixCommsSessionForProject(cloneCommsSession(s), repoName, hash))
		}
		for _, s := range ps.CommsSessions {
			out.CommsSessions = append(out.CommsSessions, prefixCommsSessionForProject(cloneCommsSession(s), repoName, hash))
		}
		for _, s := range ps.Sessions {
			s.SessionID = projectSessionID(hash, s.SessionID)
			s.SessionName = projectSessionName(repoName, s.SessionName)
			out.Sessions = append(out.Sessions, s)
		}
		for _, c := range ps.Claims {
			c.SessionID = projectSessionID(hash, c.SessionID)
			c.SessionName = projectSessionName(repoName, c.SessionName)
			out.Claims = append(out.Claims, c)
		}
		for _, f := range ps.Findings {
			f.Summary = repoName + ": " + f.Summary
			f.SessionID = projectSessionID(hash, f.SessionID)
			f.SessionName = projectSessionName(repoName, f.SessionName)
			out.Findings = append(out.Findings, f)
		}
		for _, n := range ps.Notes {
			n.Body = repoName + ": " + n.Body
			n.SessionID = projectSessionID(hash, n.SessionID)
			n.SessionName = projectSessionName(repoName, n.SessionName)
			out.Notes = append(out.Notes, n)
		}
		for _, r := range ps.Releases {
			if r.Result != "" {
				r.Result = repoName + ": " + r.Result
			} else {
				r.Result = repoName
			}
			r.SessionID = projectSessionID(hash, r.SessionID)
			r.SessionName = projectSessionName(repoName, r.SessionName)
			out.Releases = append(out.Releases, r)
		}
		if repoRoot != "" {
			for _, doc := range listDocs(filepath.Join(repoRoot, ".comms", "docs")) {
				out.Docs = append(out.Docs, repoName+"/"+doc)
			}
		}
	}
	sort.Slice(out.Active, func(i, j int) bool { return out.Active[i].StartedAt.After(out.Active[j].StartedAt) })
	sort.Slice(out.CommsSessions, func(i, j int) bool { return out.CommsSessions[i].EndedAt.After(out.CommsSessions[j].EndedAt) })
	sort.Slice(out.Claims, func(i, j int) bool { return out.Claims[i].TS.Before(out.Claims[j].TS) })
	// Priority-first, then newest — matches recentFindings/recentNotes so the
	// merged all-projects view orders the same way the per-repo view does.
	sort.SliceStable(out.Findings, func(i, j int) bool {
		if out.Findings[i].Priority != out.Findings[j].Priority {
			return out.Findings[i].Priority
		}
		return out.Findings[i].TS.After(out.Findings[j].TS)
	})
	sort.SliceStable(out.Notes, func(i, j int) bool {
		if out.Notes[i].Priority != out.Notes[j].Priority {
			return out.Notes[i].Priority
		}
		return out.Notes[i].TS.After(out.Notes[j].TS)
	})
	sort.Slice(out.Releases, func(i, j int) bool { return out.Releases[i].TS.After(out.Releases[j].TS) })
	if len(out.Active) > 0 {
		out.Current = &out.Active[0]
		out.Events = out.Current.Events
	} else if len(out.CommsSessions) > 0 {
		out.Events = out.CommsSessions[0].Events
	}
	attachClaimsToActiveSessions(&out)
	out.Actions = buildUIActions(out)
	return out, nil
}

func globalProjectKey(repoRoot, hash string) string {
	repoRoot = strings.TrimSpace(repoRoot)
	if repoRoot == "" {
		return "hash:" + hash
	}
	if resolved, err := filepath.EvalSymlinks(repoRoot); err == nil {
		return "repo:" + resolved
	}
	return "repo:" + filepath.Clean(repoRoot)
}

func latestEventTS(events []event.Event) time.Time {
	var latest time.Time
	for _, ev := range events {
		if ev.TS.After(latest) {
			latest = ev.TS
		}
	}
	return latest
}

func globalLogCandidateNewer(candidate, existing globalLogCandidate) bool {
	if !candidate.lastTS.Equal(existing.lastTS) {
		return candidate.lastTS.After(existing.lastTS)
	}
	return candidate.hash > existing.hash
}

func repoHashFromPrefixedSessionID(sessionID string) string {
	left, _, ok := strings.Cut(strings.TrimSpace(sessionID), ":")
	if !ok {
		return ""
	}
	return left
}

func repoRootForGlobalHash(hash string) (string, error) {
	hash = strings.TrimSpace(hash)
	if !regexp.MustCompile(`^[a-f0-9]{12}$`).MatchString(hash) {
		return "", fmt.Errorf("valid repo_hash is required")
	}
	dataHome, err := paths.UserDataHome()
	if err != nil {
		return "", err
	}
	logDir := filepath.Join(dataHome, "comms", hash)
	info, err := os.Stat(logDir)
	if err != nil {
		return "", fmt.Errorf("repo log %s: %w", hash, err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("repo log %s is not a directory", hash)
	}
	repoRoot := strings.TrimSpace(readSmallFile(filepath.Join(logDir, "repo-path.txt")))
	if repoRoot == "" {
		return "", fmt.Errorf("repo log %s has no repo-path.txt", hash)
	}
	if isScratchRepoRoot(repoRoot) {
		return "", fmt.Errorf("repo log %s belongs to a generated scratch repo", hash)
	}
	return repoRoot, nil
}

func isScratchRepoRoot(repoRoot string) bool {
	repoRoot = strings.TrimSpace(repoRoot)
	if repoRoot == "" {
		return false
	}
	root := filepath.Clean(repoRoot)
	if abs, err := filepath.Abs(root); err == nil {
		root = abs
	}
	if resolved, err := filepath.EvalSymlinks(root); err == nil {
		root = resolved
	}
	base := filepath.Base(root)
	if !strings.HasPrefix(base, "comms-") && !strings.HasPrefix(base, "test-comms-") {
		return false
	}
	for _, temp := range scratchTempRoots() {
		rel, err := filepath.Rel(temp, root)
		if err == nil && rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && !filepath.IsAbs(rel) {
			return true
		}
	}
	return false
}

func scratchTempRoots() []string {
	seen := map[string]bool{}
	var roots []string
	for _, root := range []string{os.TempDir(), "/tmp", "/private/tmp"} {
		root = filepath.Clean(root)
		if abs, err := filepath.Abs(root); err == nil {
			root = abs
		}
		if resolved, err := filepath.EvalSymlinks(root); err == nil {
			root = resolved
		}
		if !seen[root] {
			seen[root] = true
			roots = append(roots, root)
		}
	}
	return roots
}

func readSmallFile(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	if len(b) > 4096 {
		b = b[:4096]
	}
	return string(b)
}

func prefixCommsSessionForProject(in uiCommsSession, repoName, hash string) uiCommsSession {
	in.ID = hash + ":" + in.ID
	in.Name = projectSessionName(repoName, in.Name)
	for i := range in.Events {
		in.Events[i].ID = hash + ":" + in.Events[i].ID
	}
	return in
}

func projectSessionName(repoName, sessionName string) string {
	if strings.TrimSpace(sessionName) == "" {
		return repoName + " / legacy"
	}
	return repoName + " / " + sessionName
}

func projectSessionID(hash, sessionID string) string {
	if strings.TrimSpace(sessionID) == "" {
		return hash + ":current"
	}
	return hash + ":" + sessionID
}

func attachClaimsToActiveSessions(snap *uiSnapshot) {
	if snap == nil || len(snap.Active) == 0 {
		return
	}
	for i := range snap.Active {
		snap.Active[i].Claims = nil
		for _, claim := range snap.Claims {
			if claimMatchesSessionID(claim, snap.Active[i].ID) {
				snap.Active[i].Claims = append(snap.Active[i].Claims, claim)
			}
		}
	}
	if len(snap.Active) > 0 {
		snap.Current = &snap.Active[0]
	}
}

// cloneCommsSession returns a copy whose Events/Claims slices are independent,
// so prefixing the clone's event ids does not mutate the un-prefixed original
// stored in a uiProjectSession.
func cloneCommsSession(in uiCommsSession) uiCommsSession {
	in.Events = append([]uiEvent(nil), in.Events...)
	in.Claims = append([]uiClaim(nil), in.Claims...)
	return in
}

// attachClaimsToProjectSession is attachClaimsToActiveSessions for a single
// project's un-prefixed container: hang each active session's claims off it and
// point Current at the first active session.
func attachClaimsToProjectSession(ps *uiProjectSession) {
	if ps == nil {
		return
	}
	for i := range ps.Active {
		ps.Active[i].Claims = nil
		for _, claim := range ps.Claims {
			if claimMatchesSessionID(claim, ps.Active[i].ID) {
				ps.Active[i].Claims = append(ps.Active[i].Claims, claim)
			}
		}
	}
	if len(ps.Active) > 0 {
		ps.Current = &ps.Active[0]
	}
}

func claimMatchesSessionID(claim uiClaim, sessionID string) bool {
	if sessionID == "current" {
		return claim.SessionID == ""
	}
	return claim.SessionID == sessionID
}

func filterActiveCommsSessionViews(in []uiCommsSession, st *state.State, sessionCutoff time.Time) []uiCommsSession {
	if len(in) == 0 || st == nil {
		return in
	}
	actors := map[string]map[string]bool{}
	for _, sess := range collectActiveSessions(st, sessionCutoff) {
		key := sess.SessionID
		if key == "" {
			key = "current"
		}
		if actors[key] == nil {
			actors[key] = map[string]bool{}
		}
		actors[key][sess.Actor] = true
	}
	claims := map[string]int{}
	for _, claim := range st.Claims {
		key := claim.SessionID
		if key == "" {
			key = "current"
		}
		claims[key]++
		if actors[key] == nil {
			actors[key] = map[string]bool{}
		}
		actors[key][claim.Actor] = true
	}
	out := make([]uiCommsSession, 0, len(in))
	for _, view := range in {
		key := view.ID
		if key == "" {
			key = "current"
		}
		view.Actors = sortedStringSet(actors[key])
		view.ClaimCount = claims[key]
		if len(view.Actors) == 0 && view.ClaimCount == 0 {
			continue
		}
		out = append(out, view)
	}
	return out
}

func buildUIActions(snap uiSnapshot) []uiAction {
	start := uiAction{ID: "start_comms_session", Label: "Start Comms Session", Method: http.MethodPost, Path: "/api/comms-session/start"}
	end := uiAction{ID: "end_comms_session", Label: "End Comms Session", Method: http.MethodPost, Path: "/api/comms-session/end"}
	releaseClaim := uiAction{ID: "release_claim", Label: "Release Claim", Method: http.MethodPost, Path: "/api/claim/release"}
	retire := uiAction{ID: "retire_session_actor", Label: "Retire Session Actor", Method: http.MethodPost, Path: "/api/session/retire"}
	lead := uiAction{ID: "transfer_leader", Label: "Transfer Leader", Method: http.MethodPost, Path: "/api/session/lead"}
	logs := uiAction{ID: "select_session_log", Label: "Select Session Event Log", Enabled: true, Reason: "client-side filtered view over current_session/events and comms_sessions/events"}

	if snap.Project.Demo {
		start.Reason = "demo mode is read-only"
		end.Reason = "demo mode is read-only"
		releaseClaim.Reason = "demo mode is read-only"
		retire.Reason = "demo mode is read-only"
		lead.Reason = "demo mode is read-only"
		return []uiAction{start, end, releaseClaim, retire, lead, logs}
	}
	if snap.Project.Hash == "global" {
		reason := "All-project mode supports releasing claims, ending a session, and removing a team member; use a repo-specific UI or CLI for other mutations."
		start.Reason = reason
		lead.Reason = reason
		if snap.Project.Actor == "" {
			msg := snap.Project.MutationMessage
			if msg == "" {
				msg = "mutating UI actions require COMMS_ACTOR"
			}
			releaseClaim.Reason = msg
			end.Reason = msg
			retire.Reason = msg
		} else {
			// End and retire are routed to the owning repo (serveGlobal*), the
			// same way release is — enable them; the frontend scopes each to the
			// selected project.
			end.Enabled = true
			retire.Enabled = true
			if len(snap.Claims) > 0 {
				releaseClaim.Enabled = true
			} else {
				releaseClaim.Reason = "no active claim to release"
			}
		}
		return []uiAction{start, end, releaseClaim, retire, lead, logs}
	}
	if !snap.Project.MutationsEnabled {
		reason := snap.Project.MutationMessage
		if reason == "" {
			reason = "mutating UI actions require COMMS_ACTOR"
		}
		start.Reason = reason
		end.Reason = reason
		releaseClaim.Reason = reason
		retire.Reason = reason
		lead.Reason = reason
		return []uiAction{start, end, releaseClaim, retire, lead, logs}
	}
	start.Enabled = true
	if len(snap.Active) > 0 {
		end.Enabled = true
	} else {
		end.Reason = "no active comms session to end"
	}
	if len(snap.Sessions) > 0 {
		lead.Enabled = true
	} else {
		lead.Reason = "no active session actor can become leader"
	}
	if len(snap.Claims) > 0 {
		releaseClaim.Enabled = true
	} else {
		releaseClaim.Reason = "no active claim to release"
	}
	if len(snap.Sessions) > 0 || len(snap.Claims) > 0 {
		retire.Enabled = true
	} else {
		retire.Reason = "no active session or claim actor to retire"
	}
	return []uiAction{start, end, releaseClaim, retire, lead, logs}
}

func buildCommsSessionViews(events []event.Event) ([]uiCommsSession, []uiCommsSession) {
	if len(events) == 0 {
		return nil, nil
	}
	sorted := append([]event.Event(nil), events...)
	// Match the authoritative reducer (internal/state.Fold), which orders events
	// by timestamp with a STABLE sort. events arrives in append order, so a
	// stable TS sort preserves causal order for same-millisecond events instead
	// of reordering by ULID ID, which would diverge from the reduced state.
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].TS.Before(sorted[j].TS) })

	type namedWindow struct {
		id      string
		name    string
		events  []event.Event
		ended   bool
		endedBy string
		reason  string
		refs    []string
	}
	named := map[string]*namedWindow{}
	var order []string
	var legacy []event.Event
	for _, ev := range sorted {
		sessionID := dataString(ev.Data, "comms_session_id")
		if sessionID == "" {
			legacy = append(legacy, ev)
			continue
		}
		win := named[sessionID]
		if win == nil {
			win = &namedWindow{id: sessionID}
			named[sessionID] = win
			order = append(order, sessionID)
		}
		if name := dataString(ev.Data, "comms_session_name"); name != "" {
			win.name = name
		}
		win.events = append(win.events, ev)
		if ev.Type == event.TypeRelease && dataBool(ev.Data, "comms_session_end") {
			win.ended = true
			win.endedBy = ev.Actor
			win.reason = reasonOf(ev)
			win.refs = dataStringList(ev.Data, "refs")
		}
	}

	var active []uiCommsSession
	var archived []uiCommsSession
	for _, id := range order {
		win := named[id]
		if win == nil || len(win.events) == 0 {
			continue
		}
		if win.ended {
			view := summarizeCommsWindow(win.id, win.name, win.events, false, win.endedBy, win.reason, win.refs)
			archived = append(archived, view)
		} else {
			view := summarizeCommsWindow(win.id, win.name, win.events, true, "", "", nil)
			active = append(active, view)
		}
	}

	start := 0
	for i, ev := range legacy {
		if ev.Type != event.TypeRelease || !dataBool(ev.Data, "comms_session_end") {
			continue
		}
		window := legacy[start : i+1]
		refs := dataStringList(ev.Data, "refs")
		archived = append(archived, summarizeCommsWindow(ev.ID, "", window, false, ev.Actor, reasonOf(ev), refs))
		start = i + 1
	}

	sort.Slice(archived, func(i, j int) bool { return archived[i].EndedAt.After(archived[j].EndedAt) })
	if start < len(legacy) {
		current := summarizeCommsWindow("current", "Current session", legacy[start:], true, "", "", nil)
		active = append(active, current)
	}
	sort.Slice(active, func(i, j int) bool { return active[i].StartedAt.After(active[j].StartedAt) })
	return active, archived
}

func summarizeCommsWindow(id, name string, events []event.Event, current bool, endedBy string, reason string, refs []string) uiCommsSession {
	view := uiCommsSession{
		ID:           id,
		Name:         name,
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
			if s := dataString(ev.Data, "comms_session_name"); s != "" {
				return "started comms session: " + s
			}
			if s := reasonOf(ev); s != "" {
				return "started comms session: " + s
			}
			return "started comms session"
		}
		if dataBool(ev.Data, "comms_session_join") {
			if s := dataString(ev.Data, "comms_session_name"); s != "" {
				return "joined comms session: " + s
			}
			return "joined comms session"
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

func dataString(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
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

// faviconSVG is the comms logo (assets/logo.svg), served at /favicon.svg so the
// browser tab shows the brand mark instead of a generic globe.
const faviconSVG = `<svg width="512" height="512" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="comms logo">
  <defs>
    <linearGradient id="tile" x1="256" y1="16" x2="256" y2="496" gradientUnits="userSpaceOnUse">
      <stop stop-color="#1b2733"/>
      <stop offset="1" stop-color="#0a0f14"/>
    </linearGradient>
    <linearGradient id="bubble" x1="120" y1="124" x2="404" y2="332" gradientUnits="userSpaceOnUse">
      <stop stop-color="#5fdccf"/>
      <stop offset="1" stop-color="#0f766e"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.46" r="0.55">
      <stop stop-color="#2dd4bf" stop-opacity="0.28"/>
      <stop offset="1" stop-color="#2dd4bf" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect x="16" y="16" width="480" height="480" rx="116" fill="url(#tile)"/>
  <rect x="17" y="17" width="478" height="478" rx="115" fill="none" stroke="#2a3a48" stroke-width="2"/>
  <rect x="56" y="60" width="400" height="360" fill="url(#glow)"/>
  <path d="M176 322 L150 380 L228 324 Z" fill="url(#bubble)"/>
  <rect x="100" y="124" width="312" height="200" rx="56" fill="url(#bubble)"/>
  <g fill="#0a141a" opacity="0.92">
    <rect x="144" y="166" width="130" height="22" rx="11"/>
    <rect x="144" y="213" width="212" height="22" rx="11"/>
    <rect x="144" y="260" width="158" height="22" rx="11"/>
  </g>
  <circle cx="378" cy="224" r="15" fill="#0a141a"/>
  <circle cx="378" cy="224" r="6.5" fill="#5fdccf"/>
</svg>`

const uiHTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>comms dashboard</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --surface: #ffffff;
  --surface-2: #f3f5f8;
  --line: #e4e8ee;
  --line-strong: #d3dae3;
  --text: #1a2230;
  --muted: #647082;
  --soft: #eef2f6;
  --teal: #0d7d72;
  --teal-soft: #e1f6f2;
  --amber: #b45309;
  --red: #c0392b;
  --red-soft: #fdf0ee;
  --blue: #2563eb;
  --accent: #0d7d72;
  --shadow: 0 1px 2px rgba(20,30,45,0.05), 0 10px 28px rgba(20,30,45,0.05);
  --ring: 0 0 0 3px rgba(13,125,114,0.22);
  --content-max: 1680px;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #090d14;
  --surface: #111722;
  --surface-2: #161e2b;
  --line: #212a39;
  --line-strong: #2f3a4d;
  --text: #e7eef6;
  --muted: #94a2b4;
  --soft: #19212e;
  --teal: #52d7c9;
  --teal-soft: #0e3b38;
  --amber: #f3b15e;
  --red: #f87171;
  --red-soft: #361c1f;
  --blue: #82aaff;
  --accent: #52d7c9;
  --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 14px 34px rgba(0,0,0,0.34);
  --ring: 0 0 0 3px rgba(82,215,201,0.26);
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  letter-spacing: 0;
  overflow: auto;
}
header {
  min-height: 78px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 14px 24px;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  z-index: 5;
}
h1 { margin: 0; font-size: 19px; font-weight: 740; }
.hdr-session {
  display: inline-block;
  margin-left: 10px;
  padding: 2px 11px;
  border-radius: 999px;
  background: var(--teal-soft);
  color: var(--teal);
  font-size: 13px;
  font-weight: 700;
  vertical-align: 2px;
}
.sub { color: var(--muted); font-size: 12px; margin-top: 5px; }
.log-path {
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  margin-top: 4px;
  overflow-wrap: anywhere;
}
.header-main { min-width: 0; }
.demo-mark {
  color: var(--amber);
  font-weight: 700;
}
.top-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
}
button {
  border: 1px solid var(--line-strong);
  background: var(--surface);
  color: var(--text);
  height: 34px;
  padding: 0 13px;
  border-radius: 8px;
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background .14s ease, border-color .14s ease, color .14s ease, box-shadow .14s ease, filter .14s ease;
}
button:hover { border-color: var(--muted); background: var(--surface-2); }
button:focus-visible { outline: none; box-shadow: var(--ring); border-color: var(--accent); }
button.danger { color: var(--red); }
button.danger:hover { border-color: var(--red); background: var(--red-soft); }
button.primary {
  border-color: transparent;
  background: var(--teal);
  color: #03211e;
  font-weight: 650;
}
button.primary:hover { background: var(--teal); filter: brightness(1.07); }
button.small {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
  border-radius: 7px;
}
button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
button:disabled:hover { border-color: var(--line-strong); background: var(--surface); }
.icon-btn {
  width: 34px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
}
.icon-btn:hover { color: var(--text); }
.icon-btn svg { display: block; }
.error-banner {
  display: none;
  margin: 12px 18px 0;
  padding: 10px 12px;
  border: 1px solid var(--red);
  border-radius: 8px;
  color: var(--red);
  background: var(--red-soft);
}
.stats {
  max-width: var(--content-max);
  margin: 0 auto;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 18px 24px 6px;
  background: transparent;
}
.stat {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-width: 148px;
  height: 50px;
  padding: 0 15px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  transition: border-color .14s ease, background .14s ease;
}
.stat:hover { border-color: var(--line-strong); }
.stat-label {
  color: var(--muted);
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.stat-value {
  color: var(--text);
  font-size: 19px;
  font-weight: 720;
  font-variant-numeric: tabular-nums;
}
.stat.warn .stat-value { color: var(--red); }
.status-dot {
  width: 9px;
  height: 9px;
  border-radius: 99px;
  background: var(--teal);
  display: inline-block;
  margin-right: 7px;
}
main {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: 12px 24px 28px;
  display: grid;
  grid-template-columns: minmax(260px, 300px) minmax(680px, 1fr) minmax(300px, 360px);
  grid-template-rows: minmax(560px, 62vh) minmax(420px, auto);
  grid-template-areas:
    "roster claims signals"
    "events events events";
  gap: 18px;
}
.panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  box-shadow: var(--shadow);
  overflow: hidden;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.panel h2 {
  margin: 0;
  padding: 13px 16px;
  font-size: 11px;
  font-weight: 650;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
  border-bottom: 1px solid var(--line);
}
.panel-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 13px 16px;
  border-bottom: 1px solid var(--line);
  flex: 0 0 auto;
}
.panel-title h2 {
  padding: 0;
  border: 0;
}
.panel-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.panel-title select,
.filter-input {
  min-width: 180px;
  max-width: 100%;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 12px;
  padding: 0 11px;
}
.filter-input { width: 220px; }
.roster { grid-area: roster; }
.roster,
.claims {
  height: 100%;
}
.claims {
  grid-area: claims;
}
.signals {
  grid-area: signals;
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-height: 0;
  height: 100%;
  overflow: hidden;
}
.signals .panel {
  box-shadow: var(--shadow);
  min-height: 0;
}
.signals .panel:nth-child(1) { flex: 1.2 1 0; }
.signals .panel:nth-child(2) { flex: 1 1 0; }
.signals .panel:nth-child(3) { flex: 1 1 0; }
.row {
  padding: 14px 16px;
  border-bottom: 1px solid var(--soft);
}
.row:last-child { border-bottom: 0; }
.actor { font-weight: 680; }
.meta-inline { color: var(--muted); font-size: 12px; font-weight: 520; }
.meta { color: var(--muted); font-size: 12px; margin-top: 4px; overflow-wrap: anywhere; }
.scope {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  font-weight: 650;
  overflow-wrap: anywhere;
}
.intent { margin-top: 5px; overflow-wrap: anywhere; }
.note-body { overflow-wrap: anywhere; }
.empty { padding: 16px 14px; color: var(--muted); }
.hint {
  padding: 12px 16px;
  color: var(--muted);
  border-bottom: 1px solid var(--soft);
  font-size: 12px;
  flex: 0 0 auto;
}
.scroll {
  min-height: 0;
  overflow: auto;
}
.claims > .scroll,
.events > .scroll,
.signals .scroll {
  flex: 1 1 auto;
}
.claims table,
.events table {
  width: 100%;
  table-layout: fixed;
}
.events table {
  border-collapse: collapse;
}
.claims table {
  border-collapse: separate;
  border-spacing: 0 8px;
  padding: 0 12px 12px;
}
th, td {
  text-align: left;
  padding: 14px 12px;
  border-bottom: 1px solid var(--soft);
  vertical-align: top;
  line-height: 1.32;
}
.claims td {
  background: var(--surface-2);
  border-bottom: 0;
}
.claims td:first-child {
  border-radius: 8px 0 0 8px;
}
.claims td:last-child {
  border-radius: 0 8px 8px 0;
}
th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--surface);
  font-size: 12px;
  color: var(--muted);
  font-weight: 650;
}
.claims th:nth-child(1), .claims td:nth-child(1) { width: 116px; }
.claims th:nth-child(2), .claims td:nth-child(2) { width: 132px; }
.claims th:nth-child(3), .claims td:nth-child(3) { width: 30%; }
.claims th:nth-child(5), .claims td:nth-child(5) { width: 78px; }
.claims th:nth-child(6), .claims td:nth-child(6) { width: 112px; }
.events th:nth-child(1), .events td:nth-child(1) { width: 120px; }
.events th:nth-child(2), .events td:nth-child(2) { width: 86px; }
.events th:nth-child(3), .events td:nth-child(3) { width: 128px; }
.events th:nth-child(4), .events td:nth-child(4) { width: 26%; }
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
.pill.hello { color: var(--teal); background: var(--teal-soft); }
.pill.claim { color: var(--teal); background: #def7f2; }
.pill.release { color: var(--amber); background: #fff0d6; }
.pill.note { color: #475467; background: #eef2f5; }
.pill.finding { color: #175cd3; background: #e7f0ff; }
.pill.stale { color: var(--red); background: var(--red-soft); }
.pill.priority { color: #7c2d12; background: #ffedd5; }
.pill.leader { color: var(--teal); background: #def7f2; margin-left: 6px; }
:root[data-theme="dark"] .pill.claim { color: #7ddbd3; background: #123d3a; }
:root[data-theme="dark"] .pill.hello { color: #7ddbd3; background: #123d3a; }
:root[data-theme="dark"] .pill.release { color: #ffd39b; background: #4b310f; }
:root[data-theme="dark"] .pill.note { color: #c3ccd6; background: #28323d; }
:root[data-theme="dark"] .pill.finding { color: #9fc4ff; background: #18345c; }
:root[data-theme="dark"] .pill.priority { color: #ffd39b; background: #4b310f; }
:root[data-theme="dark"] .pill.leader { color: #7ddbd3; background: #123d3a; }
.claim-stale td {
  background: var(--red-soft);
}
.events {
  grid-area: events;
  grid-column: 1 / -1;
  min-height: 420px;
}
.session-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: start;
}
.roster-act { align-self: center; display: flex; }
.roster-act button { opacity: 0; transition: opacity .14s ease; }
.session-row:hover .roster-act button,
.roster-act button:focus-visible { opacity: 1; }
.copy {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}
.truncate {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* ── Unified (all-projects) layout: a left sidebar of projects ── */
.sidebar { grid-area: sidebar; display: none; }
body.unified .sidebar { display: flex; }
body.unified main {
  grid-template-columns: minmax(208px, 244px) minmax(218px, 262px) minmax(520px, 1fr) minmax(280px, 344px);
  grid-template-areas:
    "sidebar roster claims signals"
    "sidebar events events events";
}
.project-row {
  padding: 11px 14px;
  border-bottom: 1px solid var(--soft);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 9px;
}
.project-row:last-child { border-bottom: 0; }
.project-row:hover { background: var(--surface-2); }
.project-row.selected { background: var(--teal-soft); box-shadow: inset 3px 0 0 var(--teal); }
.project-row .pdot { width: 8px; height: 8px; border-radius: 99px; background: var(--muted); flex: 0 0 auto; }
.project-row.live .pdot { background: var(--teal); }
.project-row.alert .pdot { background: var(--red); }
.project-row .pbody { min-width: 0; flex: 1 1 auto; }
.project-row .pname { font-weight: 680; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-row .pmeta { color: var(--muted); font-size: 12px; margin-top: 2px; }

/* ── Polish: accessible cues, scanning hierarchy, friendly empty states ── */
.claim-stale td:first-child { box-shadow: inset 4px 0 0 var(--red); }
.events tbody tr:nth-child(even) td { background: var(--surface-2); }
.row.priority-row { box-shadow: inset 3px 0 0 var(--amber); }
.filter-input:not(:placeholder-shown) { border-color: var(--blue); background: var(--soft); }
.stat.warn { background: var(--red-soft); border-color: var(--red); }
.empty-state { text-align: center; padding: 30px 18px; color: var(--muted); }
.empty-state .es-icon { font-size: 20px; color: var(--teal); font-weight: 700; }
.empty-state .es-title { color: var(--text); font-weight: 680; margin-top: 6px; }
.empty-state .es-sub { font-size: 12px; margin-top: 3px; }
.empty-state .es-cta { margin-top: 14px; }
.es-clear { color: var(--blue); cursor: pointer; text-decoration: underline; margin-left: 6px; }
.rel-result { font-weight: 620; line-height: 1.34; overflow-wrap: anywhere; }
.rel-result + .meta.scope { margin-top: 5px; color: var(--teal); }

/* ── Active Claims as readable cards (replaces the cramped fixed table) ── */
.claim-list { padding: 10px 12px 12px; }
.claim-card {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 12px 14px;
  margin-bottom: 8px;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 10px;
}
.claim-card:last-child { margin-bottom: 0; }
.claim-card.stale {
  background: var(--red-soft);
  border-color: var(--red);
  box-shadow: inset 3px 0 0 var(--red);
}
.claim-main { min-width: 0; flex: 1 1 auto; }
.claim-top { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.claim-top .actor { font-weight: 680; }
.claim-sess {
  color: var(--muted);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.claim-age {
  margin-left: auto;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.claim-age.is-stale { color: var(--red); font-weight: 650; }
.claim-card .scope { margin-top: 6px; }
.claim-intent { margin-top: 6px; color: var(--text); line-height: 1.5; overflow-wrap: anywhere; }
.claim-act { flex: 0 0 auto; display: flex; flex-direction: column; gap: 6px; padding-top: 1px; }

@media (max-width: 1180px) {
  body { overflow: auto; }
  body.unified main {
    grid-template-columns: 1fr;
    grid-template-areas: "sidebar" "roster" "claims" "signals" "events";
  }
  body.unified .sidebar { max-height: 320px; }
  header { height: auto; min-height: 76px; align-items: flex-start; padding-top: 12px; padding-bottom: 12px; }
  .stats { padding: 14px 16px 2px; }
  .stat { flex: 1 1 160px; }
  main {
    height: auto;
    grid-template-columns: 1fr;
    grid-template-rows: auto;
    grid-template-areas:
      "roster"
      "claims"
      "signals"
      "events";
    overflow: visible;
    padding: 12px 16px 24px;
  }
  .events { grid-column: auto; }
  .claims { min-height: 0; }
  .scroll { max-height: 520px; }
}
@media (max-width: 620px) {
  body { overflow: auto; }
  header {
    height: auto;
    min-height: 64px;
    padding: 12px;
    gap: 10px;
    align-items: flex-start;
    display: block;
  }
  h1 { font-size: 17px; }
  .top-actions { justify-content: flex-start; margin-top: 10px; }
  .stats { padding: 12px 10px 2px; gap: 8px; }
  .stat { flex: 1 1 calc(50% - 8px); min-width: 0; height: 44px; padding: 7px 10px; }
  main { padding: 10px; gap: 14px; }
  .panel-title {
    display: block;
  }
  .panel-title select,
  .filter-input {
    width: 100%;
    margin-top: 8px;
  }
  .panel-tools { display: block; }
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
  .claims td:nth-child(2)::before { content: "Session"; }
  .claims td:nth-child(3)::before { content: "Scope"; }
  .claims td:nth-child(4)::before { content: "Intent"; }
  .claims td:nth-child(5)::before { content: "Age"; }
  .claims td:nth-child(6)::before { content: "Action"; }
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
  <div class="header-main">
    <h1 id="project">comms dashboard</h1>
    <div class="sub" id="projectMeta">Loading project state...</div>
    <div class="log-path" id="logPath"></div>
  </div>
  <div class="top-actions">
    <span class="sub"><span class="status-dot"></span><span id="updated">live</span></span>
    <button id="endComms" class="danger" type="button">End Comms Session</button>
    <button id="theme" class="icon-btn" type="button" aria-label="Toggle dark mode"></button>
  </div>
</header>
<section id="stats" class="stats" aria-label="Comms summary"></section>
<div id="error" class="error-banner"></div>
<main>
  <section class="panel sidebar">
    <h2>Projects</h2>
    <div id="projectList" class="scroll"></div>
  </section>
  <section class="panel roster">
    <h2>Team Roster</h2>
    <div id="sessions" class="scroll"></div>
    <h2>Current Comms Session</h2>
    <div id="currentSession"></div>
    <h2>Comms Session Archive</h2>
    <div id="commsSessions" class="scroll"></div>
  </section>
  <section class="panel claims">
    <div class="panel-title">
      <h2>Active Claims</h2>
      <input id="claimFilter" class="filter-input" type="search" placeholder="Filter claims">
    </div>
    <div id="claims" class="scroll"></div>
  </section>
  <div class="signals">
    <section class="panel">
      <h2>Recent Findings</h2>
      <div id="findings" class="scroll"></div>
    </section>
    <section class="panel">
      <h2>Recent Notes</h2>
      <div id="notes" class="scroll"></div>
    </section>
    <section class="panel">
      <h2>Recently Completed</h2>
      <div id="releases" class="scroll"></div>
    </section>
  </div>
  <section class="panel events">
    <div class="panel-title">
      <h2>Session Event Log</h2>
      <div class="panel-tools">
        <input id="eventFilter" class="filter-input" type="search" placeholder="Filter events">
        <select id="sessionSelect" aria-label="Choose comms session log"></select>
      </div>
    </div>
    <div class="hint" id="eventHint">Choose a session to see only that session's log rows. The physical JSONL remains append-only.</div>
    <div id="events" class="scroll"></div>
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
const SUN_ICON = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"></path></svg>';
const MOON_ICON = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>';
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  // Icon-only toggle: show the icon for the mode you'd switch TO — a sun while
  // dark, a moon while light.
  el('theme').innerHTML = theme === 'dark' ? SUN_ICON : MOON_ICON;
  el('theme').setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
}
applyTheme(preferredTheme());
function empty(label) { return '<div class="empty">' + label + '</div>'; }
function renderRows(items, fn, label) {
  items = items || [];
  return items.length ? items.map(fn).join('') : empty(label);
}
function renderTable(items, headers, fn, label) {
  items = items || [];
  if (!items.length) return empty(label);
  return '<table><thead><tr>' + headers.map(h => '<th>' + h + '</th>').join('') + '</tr></thead><tbody>' + items.map(fn).join('') + '</tbody></table>';
}
function mutationHelp(data) {
  if (data.project.demo) return 'Demo mode is read-only.';
  if (data.project.hash === 'global') return data.project.mutation_message || 'All-project mode can release claims; use a repo-specific UI or CLI to start/end sessions.';
  if (data.project.mutations_enabled) return 'Agents start and join named sessions; End archives the selected project\'s session.';
  return data.project.mutation_message || 'Set COMMS_ACTOR before starting comms ui to start or end the comms session here.';
}
function actionByID(data, id) {
  return (data.actions || []).find(a => a.id === id) || {};
}
let selectedSessionID = localStorage.getItem('selectedSessionID') || 'current';
let latestData = null;
let latestView = null;
let selectedProjectHash = localStorage.getItem('selectedProjectHash') || '';
function isUnified(data) { return Array.isArray(data.project_sessions) && data.project_sessions.length > 0; }
// currentView returns the slice of the snapshot the panels should render: a
// single project's container when one is selected in unified mode, otherwise the
// snapshot itself (the merged "All projects" view, or a single-repo snapshot).
function currentView(data) {
  const ps = data.project_sessions || [];
  if (!ps.length || !selectedProjectHash) return data;
  return ps.find(p => p.repo_hash === selectedProjectHash) || data;
}
function renderProjectList(data) {
  const list = el('projectList');
  if (!list) return;
  const projects = data.project_sessions || [];
  if (!projects.length) { list.innerHTML = ''; list.dataset.sig = ''; return; }
  const rows = [];
  const totalActive = projects.reduce((a, p) => a + (p.active_comms_sessions || []).length, 0);
  const totalClaims = projects.reduce((a, p) => a + (p.claims || []).length, 0);
  rows.push({
    hash: '', name: 'All projects',
    meta: projects.length + ' project' + (projects.length === 1 ? '' : 's') + (totalActive ? ' · ' + totalActive + ' active' : '') + (totalClaims ? ' · ' + totalClaims + ' claim' + (totalClaims === 1 ? '' : 's') : ''),
    cls: (totalActive || totalClaims) ? 'live' : ''
  });
  for (const p of projects) {
    // Reflect ANY recent activity, not just named sessions + open claims — a
    // project used without a named session (or whose claims are all released)
    // still has findings/notes/completed work and must not look dead.
    const act = (p.active_comms_sessions || []).length;
    const claims = (p.claims || []).length;
    const findings = (p.findings || []).length;
    const notes = (p.notes || []).length;
    const done = (p.releases || []).length;
    const stale = (p.claims || []).some(c => c.stale);
    const active = act || claims || findings || notes || done;
    const bits = [];
    if (act) bits.push(act + ' active');
    if (claims) bits.push(claims + ' claim' + (claims === 1 ? '' : 's'));
    if (findings) bits.push(findings + ' finding' + (findings === 1 ? '' : 's'));
    if (done) bits.push(done + ' done');
    rows.push({
      hash: p.repo_hash,
      name: p.repo_name,
      meta: bits.length ? bits.join(' · ') : 'no recent activity',
      cls: stale ? 'alert' : (active ? 'live' : '')
    });
  }
  const sig = JSON.stringify(rows.map(r => [r.hash, r.name, r.meta, r.cls, r.hash === selectedProjectHash]));
  if (list.dataset.sig === sig) return; // nothing changed; don't thrash the DOM
  list.dataset.sig = sig;
  list.innerHTML = rows.map(r =>
    '<div class="project-row ' + r.cls + (r.hash === selectedProjectHash ? ' selected' : '') + '" data-project-hash="' + esc(r.hash) + '" role="button" tabindex="0">' +
      '<span class="pdot"></span>' +
      '<div class="pbody"><div class="pname">' + esc(r.name) + '</div><div class="pmeta">' + esc(r.meta) + '</div></div>' +
    '</div>'
  ).join('');
  list.querySelectorAll('[data-project-hash]').forEach(row => {
    const pick = () => selectProject(row.getAttribute('data-project-hash'));
    row.addEventListener('click', pick);
    row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
  });
}
function selectProject(hash) {
  selectedProjectHash = hash || '';
  if (selectedProjectHash) localStorage.setItem('selectedProjectHash', selectedProjectHash);
  else localStorage.removeItem('selectedProjectHash');
  selectedSessionID = 'current'; // re-default the session log within the new project
  if (latestData) applySnapshot(latestData);
}
function filterText(id) {
  return (el(id)?.value || '').trim().toLowerCase();
}
function includesFilter(values, filter) {
  if (!filter) return true;
  return values.join(' ').toLowerCase().includes(filter);
}
function renderStats(data) {
  const claims = data.claims || [];
  const findings = data.findings || [];
  const notes = data.notes || [];
  const archive = data.comms_sessions || [];
  const activeSessions = data.active_comms_sessions || [];
  const stat = (label, value, warn) => '<div class="stat ' + (warn ? 'warn' : '') + '"><span class="stat-label">' + label + '</span><span class="stat-value">' + esc(value) + '</span></div>';
  el('stats').innerHTML = [
    stat('named sessions', activeSessions.length, false),
    stat('actors', (data.sessions || []).length, false),
    stat('claims', claims.length, false),
    stat('stale', claims.filter(c => c.stale).length, claims.some(c => c.stale)),
    stat('findings', findings.length, false),
    stat('notes', notes.length, false),
    stat('archives', archive.length, false)
  ].join('');
}
async function load() {
  const res = await fetch('/api/status', { cache: 'no-store' });
  if (!res.ok) throw new Error(await res.text());
  applySnapshot(await res.json());
}
function applySnapshot(data) {
  hideError();
  latestData = data;
  const unified = isUnified(data);
  document.body.classList.toggle('unified', unified);
  // Self-heal a stale project selection (project gone, or no longer unified).
  if (selectedProjectHash && !(data.project_sessions || []).some(p => p.repo_hash === selectedProjectHash)) {
    selectedProjectHash = '';
    localStorage.removeItem('selectedProjectHash');
  }
  const view = currentView(data);
  latestView = view;
  // Header reflects the selected project (or the all-projects roll-up).
  const sel = unified && selectedProjectHash ? (data.project_sessions || []).find(p => p.repo_hash === selectedProjectHash) : null;
  // Show the active comms-session name(s) in the header — that's the name
  // agents use ("immofanten-build"), so it must not be hidden behind the repo
  // name. Only when a single project is in focus (not the merged all view).
  const focused = sel || !isUnified(data);
  const activeSessNames = focused ? (view.active_comms_sessions || []).map(s => s.name).filter(Boolean) : [];
  const repoLabel = sel ? sel.repo_name : data.project.name;
  el('project').innerHTML = esc(repoLabel) + activeSessNames.map(n => ' <span class="hdr-session">' + esc(n) + '</span>').join('');
  el('projectMeta').innerHTML = (activeSessNames.length ? 'session in repo · ' : '') + esc(sel ? sel.repo_hash : data.project.hash) + ' · ' + esc(sel ? sel.root : data.project.root) + (data.project.demo ? ' · <span class="demo-mark">demo mode</span>' : '');
  el('logPath').textContent = 'Log: ' + (sel ? sel.log_path : data.project.log_path);
  el('updated').textContent = 'updated ' + fmtTime(data.updated);
  renderProjectList(data);
  renderStats(view);
  const endAction = actionByID(data, 'end_comms_session');
  const endTarget = endSessionTarget(data);
  el('endComms').disabled = !(endAction.enabled && endTarget);
  el('endComms').title = endTarget ? ('End "' + (endTarget.name || endTarget.id) + '" and archive it') : (endAction.reason || mutationHelp(data));
  const rosterRetire = actionByID(data, 'retire_session_actor');
  const rosterRepo = isUnified(data) ? (selectedProjectHash || '') : '';
  el('sessions').innerHTML = renderRows(view.sessions, s => {
    const title = s.label ? esc(s.label) + ' <span class="meta-inline">@' + esc(s.actor) + '</span>' : '@' + esc(s.actor);
    const rm = rosterRetire.enabled ? '<div class="roster-act"><button class="small danger" type="button" data-retire-actor="' + esc(s.actor) + '" data-retire-repo="' + esc(rosterRepo) + '" title="Remove @' + esc(s.actor) + ' from the team — releases their claims; history is kept">Remove</button></div>' : '';
    return '<div class="row session-row"><div><div class="actor">' + title + (s.leader ? ' <span class="pill leader">leader</span>' : '') + '</div><div class="meta">' + esc(s.base_name || 'session') + ' · ' + esc(s.hostname || 'unknown host') + ' · hello ' + fmtTime(s.ts) + '</div>' + (s.session_name ? '<div class="meta">in session: ' + esc(s.session_name) + '</div>' : '') + '</div>' + rm + '</div>';
  },
    'No active sessions in the last 4h.');
  el('sessions').querySelectorAll('[data-retire-actor]').forEach(b => {
    b.addEventListener('click', () => retireActor(b.getAttribute('data-retire-actor'), b.getAttribute('data-retire-repo')).catch(showError));
  });
  el('currentSession').innerHTML = renderRows(view.active_comms_sessions, s =>
    '<div class="row"><div class="actor">' + esc(s.name || 'Unnamed session') + '</div><div class="meta">Started ' + fmtTime(s.started_at) + ' · ' + esc(s.event_count || 0) + ' event(s) · ' + esc(s.claim_count || 0) + ' claim(s) · ' + esc(s.finding_count || 0) + ' finding(s) · ' + esc(s.note_count || 0) + ' note(s)</div><div class="meta">' + esc((s.actors || []).map(a => '@' + a).join(', ')) + '</div></div>',
    'No named comms session is open. Use Start Comms Session, or ask an agent to run comms session start "<name>".');
  el('commsSessions').innerHTML = renderRows(view.comms_sessions, s =>
    '<div class="row"><div class="actor">' + esc(s.name || 'Archived session') + '</div><div class="meta">' + fmtTime(s.started_at) + ' → ' + fmtTime(s.ended_at) + '</div><div class="meta">ended by @' + esc(s.ended_by) + ' · ' + esc(s.reason || 'comms session ended') + '</div><div class="meta">' + esc(s.event_count || 0) + ' event(s) · ' + esc(s.claim_count || 0) + ' claim(s) · ' + esc(s.finding_count || 0) + ' finding(s) · ' + esc(s.note_count || 0) + ' note(s)</div><div class="meta">' + esc((s.actors || []).map(a => '@' + a).join(', ')) + '</div></div>',
    'No archived comms sessions yet. Use End Comms Session when the project work window is done.');
  renderSessionChoices(view);
  renderClaims(data, view);
  el('findings').innerHTML = renderRows(view.findings, f =>
    '<div class="row' + (f.priority ? ' priority-row' : '') + '">' + (f.priority ? '<span class="pill priority">priority</span> ' : '') + '<span class="pill finding">' + esc(f.category) + '</span><div class="intent">' + esc(f.summary) + '</div><div class="meta">@' + esc(f.actor) + ' · ' + fmtTime(f.ts) + '</div></div>',
    'No findings in the last 24h.');
  el('notes').innerHTML = renderRows(view.notes, n =>
    '<div class="row' + (n.priority ? ' priority-row' : '') + '">' + (n.priority ? '<span class="pill priority">priority</span> ' : '') + '<div class="note-body">' + esc(n.body) + '</div><div class="meta">@' + esc(n.actor) + ' · ' + fmtTime(n.ts) + '</div></div>',
    'No notes in the last 24h.');
  el('releases').innerHTML = renderRows(view.releases, r =>
    '<div class="row"><div class="rel-result">' + esc(r.result || 'released a claim') + '</div>' +
    ((r.scopes && r.scopes.length) ? '<div class="meta scope">' + esc(r.scopes.join(', ')) + '</div>' : '') +
    '<div class="meta">@' + esc(r.actor) + ' · ' + fmtTime(r.ts) + '</div></div>',
    'No completed work in the last 24h.');
}
function renderClaims(data, view) {
  view = view || data;
  const releaseAction = actionByID(data, 'release_claim');
  const retireAction = actionByID(data, 'retire_session_actor');
  const claimFilter = filterText('claimFilter');
  const chosen = allSessionChoices(view).find(c => c.id === selectedSessionID);
  const sourceClaims = chosen && chosen.active ? (chosen.session.claims || []) : (chosen ? [] : (view.claims || []));
  const claims = sourceClaims
    .filter(c => includesFilter([c.actor, c.session_name, c.scope, c.intent, c.age, c.id], claimFilter));
  const scopeText = chosen ? (chosen.active ? 'Showing active claims for "' + (chosen.session.name || chosen.session.id) + '". ' : 'Archived sessions have no active claims. ') : 'Showing all active claims. ';
  // Archive CTA: when an active named session has no open claims left, offer to
  // archive it right from the empty state (it ended cleanly).
  const endAction = actionByID(data, 'end_comms_session');
  const canArchive = endAction.enabled && chosen && chosen.active && chosen.id !== 'current' && claims.length === 0 && !claimFilter;
  const archiveBtn = canArchive ? '<div class="es-cta"><button class="small primary" type="button" data-archive-session>Archive this session</button></div>' : '';
  let claimBody;
  if (claims.length) {
    // Readable claim CARDS (not a cramped fixed table): intent + scope get the
    // full column width, so long text never collapses to one word per line.
    claimBody = '<div class="claim-list">' + claims.map(c => {
      const acts = [];
      if (releaseAction.enabled) acts.push('<button class="small primary" type="button" data-release-claim="' + esc(c.id) + '" data-release-repo="' + esc(c.repo_hash || '') + '" data-release-session="' + esc(c.session_id || '') + '" data-release-actor="' + esc(c.actor) + '" data-release-scope="' + esc(c.scope) + '">Release</button>');
      if (c.stale && retireAction.enabled) acts.push('<button class="small danger" type="button" data-retire-actor="' + esc(c.actor) + '" data-retire-repo="' + esc(c.repo_hash || '') + '">Retire</button>');
      const actBox = acts.length ? '<div class="claim-act">' + acts.join('') + '</div>' : '';
      return '<div class="claim-card' + (c.stale ? ' stale' : '') + '">' +
        '<div class="claim-main">' +
          '<div class="claim-top"><span class="actor">@' + esc(c.actor) + '</span>' +
            (c.session_name ? '<span class="claim-sess">' + esc(c.session_name) + '</span>' : '') +
            '<span class="claim-age' + (c.stale ? ' is-stale' : '') + '">' + esc(c.age) + (c.stale ? ' · stale' : '') + '</span></div>' +
          '<div class="scope">' + esc(c.scope) + '</div>' +
          (c.intent ? '<div class="claim-intent">' + esc(c.intent) + '</div>' : '') +
        '</div>' + actBox +
      '</div>';
    }).join('') + '</div>';
  } else if (claimFilter) {
    claimBody = '<div class="empty">No claims matching "' + esc(claimFilter) + '" <span class="es-clear" data-clear="claimFilter" role="button" tabindex="0">Clear</span></div>';
  } else if (chosen && !chosen.active) {
    claimBody = '<div class="empty">No active claims in archived sessions.</div>';
  } else {
    claimBody = '<div class="empty-state"><div class="es-icon">&#10003;</div><div class="es-title">No active claims</div><div class="es-sub">All work in this view is released.</div>' + archiveBtn + '</div>';
  }
  el('claims').innerHTML = '<div class="hint">' + esc(scopeText) + ' Claims older than ' + esc(data.project.stale_after) + ' are stale. ' + esc(mutationHelp(data)) + '</div>' + claimBody;
  el('claims').querySelectorAll('[data-release-claim]').forEach(button => {
    button.addEventListener('click', () => releaseClaim(button.getAttribute('data-release-claim'), button.getAttribute('data-release-actor'), button.getAttribute('data-release-scope'), button.getAttribute('data-release-repo'), button.getAttribute('data-release-session')).catch(showError));
  });
  el('claims').querySelectorAll('[data-retire-actor]').forEach(button => {
    button.addEventListener('click', () => retireActor(button.getAttribute('data-retire-actor'), button.getAttribute('data-retire-repo')).catch(showError));
  });
  const clear = el('claims').querySelector('[data-clear="claimFilter"]');
  if (clear) clear.addEventListener('click', () => { el('claimFilter').value = ''; renderClaims(latestData, latestView); });
  const archiveEl = el('claims').querySelector('[data-archive-session]');
  if (archiveEl) archiveEl.addEventListener('click', () => endCommsSession().catch(showError));
}
function allSessionChoices(data) {
  const out = [];
  for (const s of data.active_comms_sessions || []) {
    out.push({ id: s.id, label: 'Active: ' + (s.name || s.id.slice(0, 10)), session: s, active: true });
  }
  if (!out.length && data.current_session) out.push({ id: 'current', label: 'Current session', session: data.current_session, active: true });
  for (const s of data.comms_sessions || []) {
    out.push({ id: s.id, label: 'Archive: ' + (s.name || fmtTime(s.started_at) + ' → ' + fmtTime(s.ended_at)), session: s, active: false });
  }
  return out;
}
function renderSessionChoices(data) {
  const sel = el('sessionSelect');
  const choices = allSessionChoices(data);
  if (!choices.length) {
    sel.innerHTML = '<option value="">No sessions yet</option>';
    sel.disabled = true;
    sel.style.display = 'none';
    sel.dataset.sig = '';
    el('events').innerHTML = empty('No session logs yet. An agent starts one with comms session start, or run comms ui --demo.');
    return;
  }
  if (!choices.some(c => c.id === selectedSessionID)) selectedSessionID = choices[0].id;
  sel.disabled = false;
  // The selector only earns its place when there's more than one log to switch
  // between (the active session plus archives). With a single session it does
  // nothing, so hide it — the event log just shows that one session.
  sel.style.display = choices.length > 1 ? '' : 'none';
  // Only rebuild the <option> set when it actually changed, and never while the
  // user has the dropdown open (focused). A live snapshot push that arrived
  // mid-interaction would otherwise collapse an open menu and reset the
  // selection. We still keep the selected log in sync below.
  const sig = JSON.stringify(choices.map(c => [c.id, c.label]));
  if (sel.dataset.sig !== sig && document.activeElement !== sel) {
    sel.innerHTML = choices.map(c => '<option value="' + esc(c.id) + '">' + esc(c.label) + '</option>').join('');
    sel.dataset.sig = sig;
  }
  if (document.activeElement !== sel && sel.value !== selectedSessionID) sel.value = selectedSessionID;
  renderSelectedSessionLog(data);
}
function renderSelectedSessionLog(data) {
  const chosen = allSessionChoices(data).find(c => c.id === selectedSessionID);
  if (!chosen) {
    el('events').innerHTML = empty('No selected session log.');
    return;
  }
  const s = chosen.session;
  const range = chosen.active ? 'Active session "' + (s.name || s.id) + '" started ' + fmtTime(s.started_at) : 'Archived session "' + (s.name || s.id) + '" ' + fmtTime(s.started_at) + ' → ' + fmtTime(s.ended_at);
  el('eventHint').textContent = range + ' · ' + (s.event_count || 0) + ' event(s). The physical JSONL remains append-only; this table is filtered to the selected session.';
  const eventFilter = filterText('eventFilter');
  const events = (s.events || []).filter(ev => includesFilter([fmtTime(ev.ts), ev.type, ev.actor, (ev.scope || []).join(', '), ev.summary], eventFilter));
  el('events').innerHTML = renderTable(events, ['When', 'Type', 'Actor', 'Scope', 'Summary'], ev =>
    '<tr><td>' + fmtTime(ev.ts) + '</td><td><span class="pill ' + esc(ev.type) + '">' + esc(ev.type) + '</span></td><td>@' + esc(ev.actor) + '</td><td><span class="scope">' + esc((ev.scope || []).join(', ')) + '</span></td><td>' + esc(ev.summary) + '</td></tr>',
    eventFilter ? 'No events matching "' + esc(eventFilter) + '" <span class="es-clear" data-clear="eventFilter" role="button" tabindex="0">Clear</span>' : 'No log events in this session.');
  const evClear = el('events').querySelector('[data-clear="eventFilter"]');
  if (evClear) evClear.addEventListener('click', () => { el('eventFilter').value = ''; renderSelectedSessionLog(latestView); });
}
// endSessionTarget resolves which active named session the End button acts on:
// the selected project's active session in unified mode (so the request routes
// to the owning repo via repo_hash), or the single repo's active session.
function endSessionTarget(data) {
  if (!data) return null;
  if (isUnified(data) && !selectedProjectHash) return null; // pick one project first
  const view = currentView(data);
  const active = (view.active_comms_sessions || [])[0];
  if (!active) return null;
  return { id: active.id, name: active.name, repo_hash: isUnified(data) ? selectedProjectHash : '' };
}
async function endCommsSession() {
  const target = endSessionTarget(latestData);
  if (!target) throw new Error('Select a project with an active comms session first.');
  const reason = window.prompt('End "' + (target.name || target.id) + '"? This releases its claims and archives the session log so you can review it later in the Comms Session Archive.', 'project work session done');
  if (reason === null) return;
  const res = await fetch('/api/comms-session/end', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason, session_id: target.id, name: target.name, repo_hash: target.repo_hash })
  });
  if (!res.ok) throw new Error(await res.text());
  selectedSessionID = 'current';
  localStorage.removeItem('selectedSessionID');
  hideError();
  await load();
}
async function retireActor(actor, repoHash) {
  const reason = window.prompt('Remove @' + actor + ' from the team? This releases all their active claims and removes them from the roster. History stays in the log.', 'removed from team via UI');
  if (reason === null) return;
  const res = await fetch('/api/session/retire', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actor, reason, repo_hash: repoHash || '' })
  });
  if (!res.ok) throw new Error(await res.text());
  hideError();
  await load();
}
async function releaseClaim(claimID, actor, scope, repoHash, sessionID) {
  const result = window.prompt('Release claim ' + claimID.slice(0, 10) + ' held by @' + actor + ' on ' + scope + '?', 'done');
  if (result === null) return;
  const res = await fetch('/api/claim/release', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ claim_id: claimID, repo_hash: repoHash || '', session_id: sessionID || '', result, reason: result })
  });
  if (!res.ok) throw new Error(await res.text());
  hideError();
  await load();
}
el('endComms').addEventListener('click', () => {
  const b = el('endComms'); const label = b.textContent;
  b.disabled = true; b.textContent = 'Ending…';
  endCommsSession().catch(showError).finally(() => { b.disabled = false; b.textContent = label; });
});
el('sessionSelect').addEventListener('change', () => {
  selectedSessionID = el('sessionSelect').value;
  localStorage.setItem('selectedSessionID', selectedSessionID);
  if (latestView) renderSelectedSessionLog(latestView);
  if (latestData) renderClaims(latestData, latestView);
});
el('claimFilter').addEventListener('input', () => {
  if (latestData) renderClaims(latestData, latestView);
});
el('eventFilter').addEventListener('input', () => {
  if (latestView) renderSelectedSessionLog(latestView);
});
el('theme').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('theme', next);
  applyTheme(next);
});
function showError(err) {
  el('error').style.display = 'block';
  el('error').textContent = 'Error: ' + err.message.trim();
}
function hideError() {
  el('error').style.display = 'none';
  el('error').textContent = '';
}
function setLive(on) {
  document.body.classList.toggle('disconnected', !on);
  if (!on) el('updated').textContent = 'reconnecting…';
}
// Push, not poll. Open one EventSource: the server primes us with the current
// snapshot the instant we connect and pushes a fresh one on every change, so
// there is no polling loop. A single initial fetch gives a fast first paint and
// a graceful fallback if the stream can't be established. EventSource reconnects
// on its own after a drop.
load().catch(showError);
let eventStream = null;
function connectStream() {
  if (eventStream) eventStream.close();
  eventStream = new EventSource('/api/events');
  eventStream.addEventListener('open', () => setLive(true));
  eventStream.addEventListener('snapshot', e => {
    setLive(true);
    try { applySnapshot(JSON.parse(e.data)); }
    catch (err) { showError(err); }
  });
  eventStream.addEventListener('error', () => setLive(false));
}
connectStream();
</script>
</body>
</html>`
