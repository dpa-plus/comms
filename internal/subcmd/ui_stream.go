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
	// newestSeen is the newest event timestamp already carried by a broadcast.
	// The next frame only has to repeat history from there, which is what keeps
	// a push proportional to what changed instead of to the whole log. It only
	// ever rises, so a frame can never be trimmed against a watermark that moved
	// backwards.
	newestSeen time.Time
	// issued/applied order concurrent rebuilds. Each rebuild takes a ticket before
	// it starts and publishes with it; a ticket older than one already applied
	// means a slower rebuild finished last, and its result is dropped. Wrapping
	// uint64 is left unguarded on purpose: one ticket is issued per rebuild, and a
	// rebuild happens per appended event, so exhausting it is not reachable.
	issued  uint64
	applied uint64
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
	// Prime BEFORE registering, while the channel is still invisible to
	// broadcasters. Registering first and sending after unlocking looks harmless
	// — the buffer is fresh — but a publish landing in between fills the cap-1
	// buffer, and the prime send then blocks forever on a channel nobody is
	// reading yet: the SSE handler is still inside this call. Sending under the
	// lock is safe precisely because an unregistered cap-1 channel cannot block.
	if h.last != nil {
		ch <- h.last
	}
	h.clients[ch] = struct{}{}
	h.mu.Unlock()
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

// beginPublish hands out the next rebuild ticket together with the watermark that
// rebuild should trim against, atomically. Ordering rebuilds by ticket rather than
// by their newest event keeps the decision independent of the clock and of the
// event data: a store whose newest event disappears (a repository removed, or a
// log that stopped being readable) lowers the newest timestamp, and rejecting
// frames on that basis would stall every later push until the wall clock caught
// up. A counter cannot go backwards.
func (h *hub) beginPublish() (ticket uint64, watermark time.Time) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.issued++
	return h.issued, h.newestSeen
}

// publish stores the FULL frame as the new prime value — every newly connected
// client still gets the complete history — and broadcasts the DELTA frame to
// clients that already have it. Delivery is coalescing: a slow client that has
// not drained the prior frame simply has it replaced with the newer one (cap-1
// buffer). The hub never blocks on a slow client, and a client can never fall
// more than one frame behind — exactly the right trade-off for a "latest state
// wins" dashboard. Coalescing can drop a delta, which is why every frame also
// carries events_total: the page notices it is short and refetches once.
func (h *hub) publish(ticket uint64, full, delta []byte, newest time.Time) {
	payload := delta
	if payload == nil {
		payload = full
	}
	h.mu.Lock()
	// Rebuilds run concurrently — the watcher goroutine and the cold-start call in
	// serveEvents both publish — so a slow one can finish AFTER a newer one. Drop
	// it: otherwise it overwrites the prime frame with a staler history that a new
	// tab would be handed, and coalescing could replace a queued newer frame with
	// it while carrying an events_total low enough that no client notices it is
	// short. A rebuild that simply found no new event has a fresh ticket and is
	// still delivered — claims, rosters and sessions change without one.
	if ticket <= h.applied {
		h.mu.Unlock()
		return
	}
	h.applied = ticket
	h.last = full
	if newest.After(h.newestSeen) {
		h.newestSeen = newest
	}
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
	full, _, _, err := s.snapshotFrames(time.Time{})
	return full, err
}

// snapshotFrames builds the snapshot ONCE and renders the two wire forms:
//
//   - full:  the complete history. What /api/status returns and what primes a
//     newly connected SSE client, so a fresh tab can filter over every row.
//   - delta: the same snapshot carrying only history at or after `since`. What
//     already-connected clients receive, so a push costs what changed instead of
//     re-sending the entire append-only log on every single event.
//
// A zero `since` means "no client has history yet"; only the full frame is built.
// The boundary is inclusive (rows AT the watermark are repeated) because two
// events can share a timestamp — the page merges by event ID, so a repeat is
// free and a miss would not be.
func (s uiServer) snapshotFrames(since time.Time) (full, delta []byte, newest time.Time, err error) {
	snap, err := s.buildSnapshot()
	if err != nil {
		return nil, nil, time.Time{}, err
	}
	finalizeSnapshot(&snap)
	newest = newestEventTS(snap.Events)
	full, err = json.Marshal(snap)
	if err != nil {
		return nil, nil, time.Time{}, err
	}
	if since.IsZero() {
		return full, nil, newest, nil
	}
	snap.Events = eventsAtOrAfter(snap.Events, since)
	snap.EventsDelta = true
	delta, err = json.Marshal(snap)
	if err != nil {
		return nil, nil, time.Time{}, err
	}
	return full, delta, newest, nil
}

// finalizeSnapshot prepares a freshly built snapshot for the wire, at the one
// chokepoint every mode passes through: stamp the front-end build (so an open
// page notices a redeployed binary), record the true history size (so a client
// can tell it is missing rows), and drop the per-session event copies.
//
// Those copies used to be the compatibility view the dashboard filtered on. Since
// history became one continuous list they are dead weight the page never reads —
// measured at roughly a fifth of the payload — while event_count, which it does
// read, is already carried alongside them.
func finalizeSnapshot(snap *uiSnapshot) {
	snap.Build = uiBuildID
	snap.EventsTotal = len(snap.Events)
	pruneSessionEvents(snap)
}

// pruneSessionEvents strips the duplicated per-session event slices from every
// session view in the snapshot, leaving the counts intact.
func pruneSessionEvents(snap *uiSnapshot) {
	if snap == nil {
		return
	}
	if snap.Current != nil {
		snap.Current.Events = nil
	}
	pruneCommsSessionEvents(snap.Active)
	pruneCommsSessionEvents(snap.CommsSessions)
	for i := range snap.ProjectSessions {
		ps := &snap.ProjectSessions[i]
		if ps.Current != nil {
			ps.Current.Events = nil
		}
		pruneCommsSessionEvents(ps.Active)
		pruneCommsSessionEvents(ps.CommsSessions)
	}
}

func pruneCommsSessionEvents(sessions []uiCommsSession) {
	for i := range sessions {
		sessions[i].Events = nil
	}
}

// newestEventTS returns the latest timestamp in a history slice. History is built
// newest-first, but this scans rather than trusting the order: the watermark it
// feeds decides what a delta may omit, so being wrong here would drop rows.
func newestEventTS(events []uiEvent) time.Time {
	var newest time.Time
	for _, ev := range events {
		if ev.TS.After(newest) {
			newest = ev.TS
		}
	}
	return newest
}

// eventsAtOrAfter returns the history rows at or after `since`, preserving order.
func eventsAtOrAfter(events []uiEvent, since time.Time) []uiEvent {
	out := make([]uiEvent, 0, 16)
	for _, ev := range events {
		if !ev.TS.Before(since) {
			out = append(out, ev)
		}
	}
	return out
}

func (s uiServer) buildSnapshot() (uiSnapshot, error) {
	if s.demo {
		return buildDemoUISnapshot(s.staleAfter), nil
	}
	if s.all {
		return buildGlobalUISnapshot(s.staleAfter)
	}
	rt, err := Open(OpenOpts{Mutating: false})
	if err != nil {
		return uiSnapshot{}, err
	}
	defer rt.Close()
	return buildUISnapshot(rt, s.staleAfter), nil
}

// publishSnapshot rebuilds the snapshot once and broadcasts it to every SSE
// client. Called once at startup (to prime the hub) and on every debounced log
// change detected by the watcher. A build failure is logged and skipped — the
// last good snapshot stays primed and the next change retries.
func (s uiServer) publishSnapshot() {
	ticket, watermark := s.hub.beginPublish()
	full, delta, newest, err := s.snapshotFrames(watermark)
	if err != nil {
		fmt.Fprintf(os.Stderr, "comms ui: snapshot rebuild failed: %v\n", err)
		return
	}
	s.hub.publish(ticket, full, delta, newest)
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
	defer func() { _ = w.Close() }()

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
