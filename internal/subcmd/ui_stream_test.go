package subcmd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestHubPrimesNewSubscriber verifies a freshly subscribed client immediately
// receives the most recent snapshot (so a newly opened tab paints at once).
func TestHubPrimesNewSubscriber(t *testing.T) {
	h := newHub()
	h.publish(1, []byte("first"), nil, time.Time{})

	ch := h.subscribe()
	select {
	case got := <-ch:
		if string(got) != "first" {
			t.Fatalf("primed payload = %q, want %q", got, "first")
		}
	default:
		t.Fatal("new subscriber was not primed with the last snapshot")
	}
}

// TestHubBroadcastCoalesces verifies a slow client that has not drained the
// previous frame gets the prior frame replaced by the newest one, never blocking
// the hub and never falling more than one frame behind.
func TestHubBroadcastCoalesces(t *testing.T) {
	h := newHub()
	ch := h.subscribe() // not primed: no snapshot yet

	h.publish(1, []byte("b"), nil, time.Time{}) // fills the cap-1 buffer
	h.publish(2, []byte("c"), nil, time.Time{}) // must drop "b", enqueue "c"

	got := <-ch
	if string(got) != "c" {
		t.Fatalf("coalesced payload = %q, want latest %q", got, "c")
	}
	select {
	case extra := <-ch:
		t.Fatalf("expected a single coalesced frame, also got %q", extra)
	default:
	}
}

// TestHubPrimesWithFullFrameWhileBroadcastingDelta verifies the split that keeps
// a push proportional to what changed: connected clients receive the DELTA frame,
// but a client that subscribes afterwards is primed with the FULL one, so a fresh
// tab never starts from a partial history.
func TestHubPrimesWithFullFrameWhileBroadcastingDelta(t *testing.T) {
	h := newHub()
	existing := h.subscribe()

	newest := time.Date(2026, 8, 19, 10, 0, 0, 0, time.UTC)
	h.publish(1, []byte("full-history"), []byte("just-the-new-rows"), newest)

	if got := <-existing; string(got) != "just-the-new-rows" {
		t.Fatalf("connected client got %q, want the delta frame", got)
	}
	late := h.subscribe()
	if got := <-late; string(got) != "full-history" {
		t.Fatalf("new subscriber primed with %q, want the full frame", got)
	}
	if _, got := h.beginPublish(); !got.Equal(newest) {
		t.Fatalf("watermark handed to the next rebuild = %v, want %v", got, newest)
	}
}

// TestHubWatermarkNeverGoesBackwards verifies the delta watermark only rises. A
// lower one would make the next delta repeat rows (harmless), but letting it drift
// backwards on every push would defeat the trimming entirely.
//
// It also pins the reason ordering is by ticket and not by timestamp: a snapshot
// whose newest event vanished: a repository removed, or a log that stopped being
// readable: still has to be delivered, or the dashboard would stop updating until
// the wall clock passed the old watermark.
func TestHubWatermarkNeverGoesBackwards(t *testing.T) {
	h := newHub()
	late := time.Date(2026, 8, 19, 10, 0, 0, 0, time.UTC)
	early := late.Add(-time.Hour)

	t1, _ := h.beginPublish()
	h.publish(t1, []byte("a"), nil, late)
	ch := h.subscribe()
	<-ch // drain the prime

	t2, wm := h.beginPublish()
	if !wm.Equal(late) {
		t.Fatalf("rebuild handed watermark %v, want %v", wm, late)
	}
	h.publish(t2, []byte("b"), []byte("b-delta"), early)

	if _, got := h.beginPublish(); !got.Equal(late) {
		t.Fatalf("watermark = %v, want it to stay at %v", got, late)
	}
	select {
	case got := <-ch:
		if string(got) != "b-delta" {
			t.Fatalf("delivered %q, want the rebuild to be delivered anyway", got)
		}
	default:
		t.Fatal("a snapshot whose newest event vanished must still be published")
	}
}

// TestHubUnsubscribeClosesChannel verifies unsubscribe removes the client and
// closes its channel so the SSE handler's receive loop unblocks and exits.
func TestHubUnsubscribeClosesChannel(t *testing.T) {
	h := newHub()
	ch := h.subscribe()
	h.unsubscribe(ch)
	if _, ok := <-ch; ok {
		t.Fatal("unsubscribe should close the client channel")
	}
}

// TestServeStatusCompactJSON verifies the status payload is compact (no
// pretty-print indentation, which would also break SSE framing) yet still valid
// JSON that decodes into a snapshot.
func TestServeStatusCompactJSON(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/status", nil)
	rec := httptest.NewRecorder()
	uiServer{demo: true, staleAfter: 90 * time.Minute, hub: newHub()}.serveStatus(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	if bytes.Contains(rec.Body.Bytes(), []byte("\n  ")) {
		t.Fatalf("expected compact JSON, found pretty-print indentation:\n%s", rec.Body.String())
	}
	var snap uiSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if !snap.Project.Demo {
		t.Fatal("demo status snapshot should be marked demo")
	}
}

// fakeFlushWriter is a minimal http.ResponseWriter + http.Flusher used to drive
// the SSE handler without binding a real socket (the test sandbox blocks ports).
type fakeFlushWriter struct {
	mu      sync.Mutex
	buf     bytes.Buffer
	header  http.Header
	flushes int
}

func (f *fakeFlushWriter) Header() http.Header {
	if f.header == nil {
		f.header = http.Header{}
	}
	return f.header
}

func (f *fakeFlushWriter) Write(p []byte) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.buf.Write(p)
}

func (f *fakeFlushWriter) WriteHeader(int) {}

func (f *fakeFlushWriter) Flush() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.flushes++
}

func (f *fakeFlushWriter) String() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.buf.String()
}

// TestServeEventsStreamsPrimedSnapshot verifies serveEvents sets the SSE content
// type and pushes the primed snapshot as an "event: snapshot" frame on connect,
// then exits cleanly when the request context is canceled.
func TestServeEventsStreamsPrimedSnapshot(t *testing.T) {
	srv := uiServer{demo: true, staleAfter: 90 * time.Minute, hub: newHub()}
	srv.publishSnapshot() // prime the hub

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req := httptest.NewRequest(http.MethodGet, "/api/events", nil).WithContext(ctx)
	w := &fakeFlushWriter{}

	done := make(chan struct{})
	go func() {
		srv.serveEvents(w, req)
		close(done)
	}()

	deadline := time.Now().Add(2 * time.Second)
	for !strings.Contains(w.String(), "event: snapshot") {
		if time.Now().After(deadline) {
			cancel()
			t.Fatalf("no snapshot frame within timeout; got:\n%s", w.String())
		}
		time.Sleep(5 * time.Millisecond)
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("serveEvents did not return after context cancel")
	}

	out := w.String()
	if ct := w.Header().Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("Content-Type = %q, want text/event-stream", ct)
	}
	data, ok := sseData(out)
	if !ok {
		t.Fatalf("no data: line in SSE output:\n%s", out)
	}
	var snap uiSnapshot
	if err := json.Unmarshal([]byte(data), &snap); err != nil {
		t.Fatalf("decode pushed snapshot %q: %v", data, err)
	}
	if !snap.Project.Demo {
		t.Fatal("pushed snapshot should be the demo snapshot")
	}
}

// sseData returns the payload of the first "data: " line in an SSE stream.
func sseData(stream string) (string, bool) {
	for _, line := range strings.Split(stream, "\n") {
		if strings.HasPrefix(line, "data: ") {
			return strings.TrimPrefix(line, "data: "), true
		}
	}
	return "", false
}

// TestHubPublishIgnoresRacedStaleRebuild verifies a rebuild that finishes out of
// order cannot regress the hub. Each rebuild takes a ticket before it starts, so a
// slow one lands with an older ticket than a rebuild that already published; if it
// were allowed to overwrite the prime frame, a newly opened tab would be handed a
// stale history whose events_total is low enough that it never notices it is short.
func TestHubPublishIgnoresRacedStaleRebuild(t *testing.T) {
	h := newHub()
	late := time.Date(2026, 8, 19, 10, 0, 0, 0, time.UTC)
	early := late.Add(-time.Minute)

	h.publish(2, []byte("fresh-full"), nil, late)
	h.publish(1, []byte("stale-full"), []byte("stale-delta"), early)

	ch := h.subscribe()
	select {
	case got := <-ch:
		if string(got) != "fresh-full" {
			t.Fatalf("primed with %q, want the newer frame to survive the raced publish", got)
		}
	default:
		t.Fatal("subscriber was not primed at all")
	}
	if _, got := h.beginPublish(); !got.Equal(late) {
		t.Fatalf("watermark = %v, want %v", got, late)
	}
}

// TestHubPublishStillDeliversRebuildWithNoNewEvents verifies the ordering guard
// rejects only out-of-order rebuilds, never up-to-date ones. A rebuild that found
// no new event carries the same watermark but a fresh ticket, and must still reach
// clients: claims, rosters and sessions change without a new event.
func TestHubPublishStillDeliversRebuildWithNoNewEvents(t *testing.T) {
	h := newHub()
	at := time.Date(2026, 8, 19, 10, 0, 0, 0, time.UTC)
	h.publish(1, []byte("first"), nil, at)

	ch := h.subscribe()
	<-ch // drain the prime

	h.publish(2, []byte("second"), []byte("second-delta"), at)
	select {
	case got := <-ch:
		if string(got) != "second-delta" {
			t.Fatalf("delivered %q, want the same-watermark rebuild to be delivered", got)
		}
	default:
		t.Fatal("a rebuild carrying no new event must still be published")
	}
}

// TestHubSubscribeNeverBlocksDuringConcurrentPublish verifies subscribing while
// the hub is publishing cannot wedge the caller. Priming after registering the
// channel looks safe: the buffer is fresh, but a publish landing in between
// fills it, and the prime send then blocks forever on a channel whose only reader
// is the SSE handler still stuck inside subscribe().
func TestHubSubscribeNeverBlocksDuringConcurrentPublish(t *testing.T) {
	h := newHub()
	h.publish(1, []byte("primed"), nil, time.Date(2026, 8, 19, 10, 0, 0, 0, time.UTC))

	stop := make(chan struct{})
	var publishers sync.WaitGroup
	publishers.Add(1)
	go func() {
		defer publishers.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			ticket, _ := h.beginPublish()
			h.publish(ticket, []byte("full"), []byte("delta"), time.Date(2026, 8, 19, 10, 0, i%60, 0, time.UTC))
		}
	}()

	done := make(chan struct{})
	go func() {
		defer close(done)
		var subscribers sync.WaitGroup
		for i := 0; i < 64; i++ {
			subscribers.Add(1)
			go func() {
				defer subscribers.Done()
				ch := h.subscribe()
				<-ch
				h.unsubscribe(ch)
			}()
		}
		subscribers.Wait()
	}()

	select {
	case <-done:
	case <-time.After(10 * time.Second):
		close(stop)
		publishers.Wait()
		t.Fatal("subscribe blocked while a publish was in flight")
	}
	close(stop)
	publishers.Wait()
}

// TestEventsAtOrAfterRepeatsTheWatermarkRow verifies the delta boundary is
// inclusive. Two events can share a timestamp, so trimming with a strict "after"
// would drop the sibling of the watermark row. The page merges by event id, so
// repeating a row costs nothing and losing one is unrecoverable.
func TestEventsAtOrAfterRepeatsTheWatermarkRow(t *testing.T) {
	shared := time.Date(2026, 8, 19, 10, 0, 0, 0, time.UTC)
	events := []uiEvent{
		{ID: "c", TS: shared.Add(time.Second)},
		{ID: "b", TS: shared},
		{ID: "a", TS: shared},
		{ID: "old", TS: shared.Add(-time.Hour)},
	}
	got := eventsAtOrAfter(events, shared)
	if len(got) != 3 {
		t.Fatalf("carried %d rows, want the newer row plus BOTH rows sharing the watermark", len(got))
	}
	for i, want := range []string{"c", "b", "a"} {
		if got[i].ID != want {
			t.Fatalf("row %d = %q, want %q (order must be preserved)", i, got[i].ID, want)
		}
	}
	if all := eventsAtOrAfter(events, shared.Add(-2*time.Hour)); len(all) != len(events) {
		t.Fatalf("a watermark older than everything must carry all %d rows, got %d", len(events), len(all))
	}
}

// TestNewestEventTSScansRatherThanTrustingOrder verifies the watermark is the true
// maximum even if history arrives out of order, since it decides what a delta may
// omit.
func TestNewestEventTSScansRatherThanTrustingOrder(t *testing.T) {
	base := time.Date(2026, 8, 19, 10, 0, 0, 0, time.UTC)
	events := []uiEvent{
		{ID: "a", TS: base},
		{ID: "b", TS: base.Add(time.Hour)},
		{ID: "c", TS: base.Add(-time.Hour)},
	}
	if got := newestEventTS(events); !got.Equal(base.Add(time.Hour)) {
		t.Fatalf("newestEventTS = %v, want %v", got, base.Add(time.Hour))
	}
	if got := newestEventTS(nil); !got.IsZero() {
		t.Fatalf("empty history newest = %v, want the zero time", got)
	}
}
