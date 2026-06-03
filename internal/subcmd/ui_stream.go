package subcmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/dpa-plus/comms/internal/paths"
	"github.com/fsnotify/fsnotify"
)

// ─────────────────────────────────────────────────────────────────────────────
// Server-sent events: push, don't poll.
//
// The browser used to GET /api/status every 2 seconds, and each request made the
// server re-open the repo, re-read and re-parse the entire JSONL log, and
// pretty-print a fresh snapshot — N browsers × a full parse every 2s, forever,
// even when nothing changed.
//
// Now a single fsnotify watcher in the `comms ui` process is notified by the OS
// the instant the log changes. It rebuilds the snapshot ONCE and broadcasts the
// already-serialized bytes to every connected browser over an SSE stream
// (/api/events). Cost drops to O(one parse per actual change), and browsers
// receive updates the moment they happen instead of up to 2s later.
// ─────────────────────────────────────────────────────────────────────────────

// hub fans one snapshot out to every connected SSE client. New subscribers are
// primed immediately with the most recent snapshot so a freshly-opened tab
// paints without waiting for the next change.
type hub struct {
	mu      sync.Mutex
	clients map[chan []byte]struct{}
	last    []byte
}

func newHub() *hub {
	return &hub{clients: make(map[chan []byte]struct{})}
}

// subscribe registers a client and returns its delivery channel, primed with the
// latest snapshot when one exists. The channel is buffered to one frame; see
// broadcast for the coalescing policy.
func (h *hub) subscribe() chan []byte {
	ch := make(chan []byte, 1)
	h.mu.Lock()
	h.clients[ch] = struct{}{}
	last := h.last
	h.mu.Unlock()
	if last != nil {
		ch <- last // safe: freshly created cap-1 buffer is empty
	}
	return ch
}

// unsubscribe removes a client and closes its channel. The SSE handler that owns
// the channel is the only caller, and both unsubscribe and broadcast take the
// mutex, so a broadcast never sends on a closed channel (it is removed from the
// map under the same lock before the close).
func (h *hub) unsubscribe(ch chan []byte) {
	h.mu.Lock()
	if _, ok := h.clients[ch]; ok {
		delete(h.clients, ch)
		close(ch)
	}
	h.mu.Unlock()
}

// broadcast stores the snapshot as the new prime value and delivers it to every
// client with coalescing semantics: a slow client that has not drained the prior
// frame simply has it replaced with the newer one (cap-1 buffer). The hub never
// blocks on a slow client, and a client can never fall more than one frame
// behind — exactly the right trade-off for a "latest state wins" dashboard.
func (h *hub) broadcast(payload []byte) {
	h.mu.Lock()
	h.last = payload
	for ch := range h.clients {
		select {
		case ch <- payload:
		default:
			// Drop the stale queued frame, enqueue the latest.
			select {
			case <-ch:
			default:
			}
			select {
			case ch <- payload:
			default:
			}
		}
	}
	h.mu.Unlock()
}

// hasSnapshot reports whether any snapshot has been published yet.
func (h *hub) hasSnapshot() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.last != nil
}

// snapshotJSON builds the current UI snapshot for the server's mode and returns
// it as COMPACT JSON. Compact (not indented) is required for the SSE path: an
// event-stream "data:" frame must not contain raw newlines. The UI tests decode
// the body as JSON, so dropping the previous pretty-printing is transparent to
// them and also trims the payload on the wire.
func (s uiServer) snapshotJSON() ([]byte, error) {
	if s.demo {
		return json.Marshal(buildDemoUISnapshot(s.staleAfter))
	}
	if s.all {
		snap, err := buildGlobalUISnapshot(s.staleAfter)
		if err != nil {
			return nil, err
		}
		return json.Marshal(snap)
	}
	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		return nil, err
	}
	defer rt.Close()
	return json.Marshal(buildUISnapshot(rt, s.staleAfter))
}

// publishSnapshot rebuilds the snapshot once and broadcasts it to every SSE
// client. Called once at startup (to prime the hub) and on every debounced log
// change detected by the watcher. A build failure is logged and skipped — the
// last good snapshot stays primed and the next change retries.
func (s uiServer) publishSnapshot() {
	body, err := s.snapshotJSON()
	if err != nil {
		fmt.Fprintf(os.Stderr, "comms ui: snapshot rebuild failed: %v\n", err)
		return
	}
	s.hub.broadcast(body)
}

// serveEvents streams snapshots to the browser over Server-Sent Events. One
// long-lived response per open dashboard tab; the server pushes a "snapshot"
// event on every change plus a periodic comment heartbeat to keep the connection
// alive and detect dead peers.
func (s uiServer) serveEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	// Same same-origin / DNS-rebinding guard as serveStatus: a cross-origin page
	// must not be able to read coordination state. Non-browser clients send no
	// Origin and stay allowed.
	if r.Header.Get("Origin") != "" && !sameOriginRequest(r) {
		http.Error(w, "forbidden: cross-origin request rejected", http.StatusForbidden)
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	// SSE is a long-lived response; the server's WriteTimeout would otherwise
	// terminate it. Clear the per-connection write deadline (no-op on writers
	// that don't support it, e.g. in tests).
	rc := http.NewResponseController(w)
	_ = rc.SetWriteDeadline(time.Time{})

	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("Connection", "keep-alive")
	h.Set("X-Accel-Buffering", "no") // disable proxy/response buffering
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	ch := s.hub.subscribe()
	defer s.hub.unsubscribe(ch)

	// Cold start: if nothing has primed the hub yet (startup prime failed and the
	// watcher hasn't fired), build one now so this client isn't blank.
	if !s.hub.hasSnapshot() {
		s.publishSnapshot()
	}

	heartbeat := time.NewTicker(25 * time.Second)
	defer heartbeat.Stop()
	ctx := r.Context()
	for {
		select {
		case <-ctx.Done():
			return
		case payload, ok := <-ch:
			if !ok {
				return
			}
			if _, err := fmt.Fprintf(w, "event: snapshot\ndata: %s\n\n", payload); err != nil {
				return
			}
			flusher.Flush()
		case <-heartbeat.C:
			if _, err := io.WriteString(w, ": ping\n\n"); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}

// watchPlan describes which directories the watcher follows and how it
// interprets events, so the same loop serves both single-repo and --all mode.
type watchPlan struct {
	roots    []string          // directories to watch from the start
	isLog    func(string) bool // a write here means "rebuild + broadcast"
	watchDir func(string) bool // a newly-created dir here deserves its own watch
}

// buildWatchPlan resolves the watch plan for the server's mode:
//   - normal: watch this repo's log dir (plus the data root so the log dir's
//     first-ever creation is noticed); only this repo's log.jsonl is relevant.
//   - all:    watch the data root and every existing per-repo subdir; any
//     log.jsonl write is relevant, and new repo subdirs get watched as they
//     appear.
func (s uiServer) buildWatchPlan() watchPlan {
	dataHome, _ := paths.UserDataHome()
	dataRoot := filepath.Join(dataHome, "comms")

	if s.all {
		roots := []string{dataRoot}
		if entries, err := os.ReadDir(dataRoot); err == nil {
			for _, e := range entries {
				if e.IsDir() {
					roots = append(roots, filepath.Join(dataRoot, e.Name()))
				}
			}
		}
		return watchPlan{
			roots:    roots,
			isLog:    func(p string) bool { return filepath.Base(p) == "log.jsonl" },
			watchDir: func(p string) bool { return filepath.Dir(p) == dataRoot },
		}
	}

	// Normal mode: resolve this repo's log path once at startup.
	var logPath, logDir string
	if rt, err := Open(OpenOpts{Mutating: false}); err == nil {
		logPath = rt.Paths.Log
		logDir = rt.Paths.LogDir
		_ = rt.Close()
	}
	roots := []string{dataRoot}
	if logDir != "" {
		if _, err := os.Stat(logDir); err == nil {
			roots = append(roots, logDir)
		}
	}
	return watchPlan{
		roots:    roots,
		isLog:    func(p string) bool { return logPath != "" && p == logPath },
		watchDir: func(p string) bool { return logDir != "" && p == logDir },
	}
}

// watchLog runs the fsnotify loop until ctx is done, calling publishSnapshot —
// debounced — whenever a relevant log file is written or created. Demo mode is
// static and never starts a watcher. Any setup failure degrades gracefully:
// browsers still get the primed snapshot and heartbeats, just not live pushes.
func (s uiServer) watchLog(ctx context.Context) {
	w, err := fsnotify.NewWatcher()
	if err != nil {
		fmt.Fprintf(os.Stderr, "comms ui: file watcher unavailable, live push disabled: %v\n", err)
		return
	}
	defer w.Close()

	plan := s.buildWatchPlan()
	for _, d := range plan.roots {
		_ = w.Add(d)
	}

	// Debounce: coalesce a burst of writes (and the lock-file churn around each
	// append) into a single rebuild. The timer is armed only when a relevant
	// event arrives; timerC stays nil while idle so the select blocks cheaply.
	const debounce = 150 * time.Millisecond
	var timer *time.Timer
	var timerC <-chan time.Time
	defer func() {
		if timer != nil {
			timer.Stop()
		}
	}()
	arm := func() {
		if timer == nil {
			timer = time.NewTimer(debounce)
			timerC = timer.C
			return
		}
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
		timer.Reset(debounce)
		timerC = timer.C
	}

	for {
		select {
		case <-ctx.Done():
			return
		case ev, ok := <-w.Events:
			if !ok {
				return
			}
			// A newly created directory we care about (this repo's log dir on cold
			// start, or a new repo's dir in --all) needs its own watch so we see
			// writes inside it; arm too, in case it already holds a fresh log.
			if ev.Op&fsnotify.Create != 0 && plan.watchDir(ev.Name) {
				if fi, statErr := os.Stat(ev.Name); statErr == nil && fi.IsDir() {
					_ = w.Add(ev.Name)
					arm()
				}
			}
			if plan.isLog(ev.Name) && ev.Op&(fsnotify.Write|fsnotify.Create|fsnotify.Rename) != 0 {
				arm()
			}
		case werr, ok := <-w.Errors:
			if !ok {
				return
			}
			fmt.Fprintf(os.Stderr, "comms ui: watcher error: %v\n", werr)
		case <-timerC:
			timerC = nil
			s.publishSnapshot()
		}
	}
}
