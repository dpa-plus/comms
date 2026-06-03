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
	h.broadcast([]byte("first"))

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

	h.broadcast([]byte("b")) // fills the cap-1 buffer
	h.broadcast([]byte("c")) // must drop "b", enqueue "c"

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
