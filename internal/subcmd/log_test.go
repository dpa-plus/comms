package subcmd

import (
	"bytes"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/dpa-plus/comms/internal/event"
)

func TestPrintEventHumanSummarizesSessionRetire(t *testing.T) {
	ev := event.Event{
		TS:    time.Date(2026, 5, 29, 13, 45, 0, 0, time.UTC),
		ID:    "01JX2Q3Y7W5B6N9P0R1S2T3L0G",
		Actor: "human-eli",
		Type:  event.TypeRelease,
		Data: map[string]interface{}{
			"session_retire": true,
			"retired_actor":  "claude-old",
			"refs":           []string{"01JX2Q3Y7W5B6N9P0R1S2T3R3A"},
			"reason":         "renamed to claude-dev",
		},
	}

	out := captureLogStdout(t, func() { printEventHuman(ev) })
	if !strings.Contains(out, "retired @claude-old") || !strings.Contains(out, "released 1 claim") {
		t.Fatalf("retire release output is not useful: %q", out)
	}
	if strings.Contains(out, `""`) {
		t.Fatalf("retire release output should not be blank result: %q", out)
	}
}

func captureLogStdout(t *testing.T, fn func()) string {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	os.Stdout = w
	fn()
	if err := w.Close(); err != nil {
		t.Fatalf("close writer: %v", err)
	}
	os.Stdout = old
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, r); err != nil {
		t.Fatalf("copy stdout: %v", err)
	}
	return buf.String()
}
